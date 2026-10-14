# 媒体任务服务设计（第二阶段）

## 问题

当前 TTS/ASR 如果直接作为 Agent 工具调用会出现：

1. **阻塞 turn** — TTS 生成需要数秒，会阻塞整个 Agent loop
2. **音频二进制污染 history** — 音频数据不应该塞进对话记录
3. **重试困难** — 失败了整个 turn 要重来，无法单独重试媒体任务
4. **资源争用** — 多个并发请求会争抢 GPU/CPU
5. **无法取消** — 用户无法中断正在生成的音频

## 架构

```
┌─────────────────────────────────────────────────────┐
│ Agent Tool (media.submit_tts / media.submit_asr)    │
│ 提交任务，立即返回 job_id，不阻塞 turn               │
└──────────────────┬──────────────────────────────────┘
                   │ 提交
                   ▼
┌─────────────────────────────────────────────────────┐
│ MediaTaskService (核心协调器)                        │
│ • 任务队列管理                                       │
│ • 状态跟踪                                           │
│ • Worker 池调度                                     │
│ • 超时、取消、重试                                   │
└──────────────────┬──────────────────────────────────┘
                   │ 调度
                   ▼
┌─────────────────────────────────────────────────────┐
│ Worker Pool (并发控制)                               │
│ • GPU worker 池（TTS）                              │
│ • CPU worker 池（ASR）                              │
│ • 队列背压                                           │
└──────────────────┬──────────────────────────────────┘
                   │ 执行
                   ▼
┌─────────────────────────────────────────────────────┐
│ TTS/ASR Backend (实际执行)                           │
│ • easy_TTS.py (已有)                                │
│ • ASR service (待实现)                               │
└──────────────────┬──────────────────────────────────┘
                   │ 结果
                   ▼
┌─────────────────────────────────────────────────────┐
│ Artifact Store (音频存储)                            │
│ • 文件系统存储                                       │
│ • 过期清理                                           │
│ • 按 scope 隔离                                     │
└─────────────────────────────────────────────────────┘
```

## 核心接口

### 1. 提交任务

```python
@dataclass
class MediaJobRequest:
    kind: Literal["tts", "asr"]
    scope: Scope
    # TTS 参数
    text: str | None = None
    language: str = "zh"
    voice: str | None = None
    # ASR 参数
    input_artifact_id: str | None = None
    # 通用
    options: dict[str, Any] = field(default_factory=dict)

@dataclass
class MediaJobResponse:
    job_id: str
    status: Literal["queued"]
    queued_at: str  # ISO 8601

async def submit_media_job(request: MediaJobRequest) -> MediaJobResponse:
    """
    提交媒体任务，立即返回 job_id
    不阻塞，不返回音频二进制
    """
```

### 2. 查询状态

```python
@dataclass
class MediaJobStatus:
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    # 完成时
    artifact_id: str | None = None
    transcript: str | None = None  # ASR 结果
    # 失败时
    error: str | None = None
    # 时间戳
    queued_at: str
    started_at: str | None = None
    completed_at: str | None = None

async def get_media_job(job_id: str) -> MediaJobStatus:
    """
    查询任务状态
    """
```

### 3. 取消任务

```python
async def cancel_media_job(job_id: str) -> MediaJobStatus:
    """
    取消队列中或运行中的任务
    """
```

### 4. 获取 Artifact

```python
@dataclass
class Artifact:
    artifact_id: str
    scope: Scope
    media_type: str  # "audio/wav", "audio/mp3"
    size_bytes: int
    created_at: str
    expires_at: str
    path: Path  # 本地文件路径

async def get_artifact(artifact_id: str, scope: Scope) -> Artifact:
    """
    获取 artifact 元信息
    实际音频文件通过 path 读取
    """
```

## 持久化

### 任务表（SQLite）

```sql
CREATE TABLE media_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,  -- 'tts' or 'asr'
    status TEXT NOT NULL,  -- 'queued', 'running', 'completed', 'failed', 'cancelled'
    
    -- Scope
    owner_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    
    -- 请求参数（JSON）
    request_json TEXT NOT NULL,
    
    -- 结果
    artifact_id TEXT,
    transcript TEXT,
    error TEXT,
    
    -- 时间戳
    queued_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    
    -- 索引
    INDEX idx_status ON media_jobs(status),
    INDEX idx_scope ON media_jobs(owner_id, channel, conversation_id, module_id)
);
```

### Artifact 表（SQLite）

```sql
CREATE TABLE media_artifacts (
    artifact_id TEXT PRIMARY KEY,
    
    -- Scope
    owner_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    
    -- 文件信息
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    
    -- 时间戳
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    
    INDEX idx_expires ON media_artifacts(expires_at),
    INDEX idx_scope ON media_artifacts(owner_id, channel, conversation_id, module_id)
);
```

## Agent 工具集成

### TTS 工具

```python
def make_media_tts_tool() -> ToolSpec:
    async def handler(text: str, voice: str | None, scope: Scope) -> ToolResult:
        request = MediaJobRequest(
            kind="tts",
            scope=scope,
            text=text,
            voice=voice,
        )
        response = await submit_media_job(request)
        return ToolResult(
            status=ToolStatus.OK,
            content=f"TTS 任务已提交，job_id: {response.job_id}",
            metadata={"job_id": response.job_id},
        )
    
    return ToolSpec(
        name="media.submit_tts",
        description="提交 TTS 任务，将文本转为语音",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要转换的文本"},
                "voice": {"type": "string", "description": "语音模型（可选）"},
            },
            "required": ["text"],
        },
        handler=handler,
        risk_level=RiskLevel.LOW,
    )
```

### 查询状态工具

```python
def make_media_status_tool() -> ToolSpec:
    async def handler(job_id: str, scope: Scope) -> ToolResult:
        status = await get_media_job(job_id)
        if status.status == "completed":
            return ToolResult(
                status=ToolStatus.OK,
                content=f"任务完成。artifact_id: {status.artifact_id}",
                metadata={"artifact_id": status.artifact_id},
            )
        elif status.status == "failed":
            return ToolResult(
                status=ToolStatus.FAILED,
                content=f"任务失败：{status.error}",
            )
        else:
            return ToolResult(
                status=ToolStatus.OK,
                content=f"任务状态：{status.status}",
            )
    
    return ToolSpec(
        name="media.get_status",
        description="查询媒体任务状态",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["job_id"],
        },
        handler=handler,
        risk_level=RiskLevel.LOW,
    )
```

## Worker 池设计

### TTS Worker

```python
class TTSWorker:
    def __init__(self, tts_url: str, max_concurrent: int = 2):
        self.tts_url = tts_url
        self.semaphore = asyncio.Semaphore(max_concurrent)
    
    async def execute(self, job: MediaJobRequest) -> tuple[Path, str]:
        """
        执行 TTS 任务
        返回：(音频文件路径, media_type)
        """
        async with self.semaphore:
            # 调用 easy_TTS.py 的 /tts 端点
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(
                    f"{self.tts_url}/tts",
                    json={"text": job.text},
                )
            
            if response.status_code != 200:
                raise RuntimeError(f"TTS failed: {response.status_code}")
            
            # 保存到临时文件
            artifact_path = self._generate_artifact_path(job.scope)
            artifact_path.write_bytes(response.content)
            
            return artifact_path, "audio/wav"
```

### ASR Worker

```python
class ASRWorker:
    def __init__(self, model_path: str, max_concurrent: int = 4):
        self.model_path = model_path
        self.semaphore = asyncio.Semaphore(max_concurrent)
    
    async def execute(self, job: MediaJobRequest) -> str:
        """
        执行 ASR 任务
        返回：转录文本
        """
        async with self.semaphore:
            # 加载音频文件
            artifact = await get_artifact(job.input_artifact_id, job.scope)
            
            # 调用 ASR 模型（待实现）
            transcript = await self._transcribe(artifact.path)
            
            return transcript
```

## 生命周期管理

### 任务超时

- TTS 任务：90 秒超时
- ASR 任务：120 秒超时
- 超时后自动标记为 `failed`

### Artifact 过期

- 默认保留 24 小时
- 每小时清理过期 artifact
- 清理顺序：先删文件，再删数据库记录

### 任务清理

- 已完成/失败/取消的任务保留 7 天
- 7 天后归档到 cold storage（可选）

## 配置

### 环境变量

```bash
# TTS 服务
TTS_SERVICE_URL=http://127.0.0.1:8766
TTS_MAX_WORKERS=2
TTS_TIMEOUT=90

# ASR 服务（待实现）
ASR_SERVICE_URL=http://127.0.0.1:8767
ASR_MAX_WORKERS=4
ASR_TIMEOUT=120

# Artifact 存储
MEDIA_ARTIFACT_DIR=./data/media_artifacts
MEDIA_ARTIFACT_EXPIRY_HOURS=24

# 任务持久化
MEDIA_TASK_DB=./data/media_tasks.db
```

### 模块私有配置

```python
ModuleSpec(
    module_id="media",
    description="TTS/ASR media tasks",
    tool_specs=[
        make_media_tts_tool(),
        make_media_asr_tool(),
        make_media_status_tool(),
    ],
    provider_config={
        "tts_url": os.getenv("TTS_SERVICE_URL", "http://127.0.0.1:8766"),
        "tts_max_workers": int(os.getenv("TTS_MAX_WORKERS", "2")),
        "asr_url": os.getenv("ASR_SERVICE_URL", "http://127.0.0.1:8767"),
        "asr_max_workers": int(os.getenv("ASR_MAX_WORKERS", "4")),
        "artifact_dir": Path(os.getenv("MEDIA_ARTIFACT_DIR", "./data/media_artifacts")),
        "artifact_expiry_hours": int(os.getenv("MEDIA_ARTIFACT_EXPIRY_HOURS", "24")),
    },
    memory_policy="module",  # 模块私有状态
)
```

## 使用示例

### Agent 提交 TTS 任务

```python
# Turn 1: 提交任务
user: "把这段话转成语音：你好，欢迎回家"
assistant: 调用 media.submit_tts(text="你好，欢迎回家")
  → job_id: "tts-20260820-abc123"
assistant: "TTS 任务已提交（ID: tts-20260820-abc123），正在生成中..."

# Turn 2: 查询状态
user: "生成好了吗？"
assistant: 调用 media.get_status(job_id="tts-20260820-abc123")
  → status: "completed", artifact_id: "art-20260820-xyz789"
assistant: "已生成完成！artifact_id: art-20260820-xyz789"

# Turn 3: 播放（由前端处理）
# 前端通过 artifact_id 从后端下载音频文件播放
```

### 自动轮询（可选）

```python
# Agent 可以在等待期间做其他事情
# 或者由前端定期轮询状态
# 不阻塞对话 turn
```

## 测试场景

1. ✅ 提交 TTS 任务立即返回 job_id
2. ✅ 任务状态正确跟踪：queued → running → completed
3. ✅ 任务超时自动标记为 failed
4. ✅ 取消队列中的任务
5. ✅ 取消运行中的任务（尽力而为）
6. ✅ Artifact 过期自动清理
7. ✅ Worker 池并发上限生效
8. ✅ 重启后从持久化状态恢复
9. ✅ 同一 job_id 幂等查询
10. ✅ Scope 隔离：不同模块的 artifact 互不可见

## 后续优化

### Phase 2.1: 基础实现
- 任务接口和持久化
- Worker 池
- Artifact 存储和过期

### Phase 2.2: 高级特性
- 进度回调（WebSocket）
- 任务优先级
- 动态 worker 扩缩容
- 分布式任务队列（Redis/RabbitMQ）

### Phase 2.3: 监控告警
- 任务成功率
- Worker 健康检查
- Artifact 存储空间监控
- 慢任务告警
