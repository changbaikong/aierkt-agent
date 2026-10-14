# Windows 沙箱阶段 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不接入模型、WorkHost 和自动回写的前提下，实现一个普通 Windows 用户可运行、失败时不降级的 AppContainer 命令执行器。

**Architecture:** 宿主验证任务目录、受管运行时和命令参数，为每个 Attempt 创建独立 AppContainer 并配置目录权限，再以 suspended 状态创建进程、加入 Job Object 后恢复。阶段 1 禁止子进程且不授予网络能力；磁盘容量使用明确标注的监控软上限。

**Tech Stack:** Python 3.12、PowerShell 7、pywin32 312、Win32 AppContainer API、扩展进程启动属性、Job Object、pytest。

**执行状态（2026-09-24）：** 技术实现与验收已完成。工作区在本阶段开始前已有用户未提交修改，因此计划中的分步提交和最终提交均未执行，避免混入或覆盖既有工作。

## Global Constraints

- 仅支持 Windows 10/11；其他系统返回明确的不支持错误。
- PowerShell 7 和 Python 3.12 必须来自应用自管目录，不自动下载，不搜索系统 PATH。
- `pywin32>=312,<313; platform_system == "Windows"` 必须写入 `requirements.txt`。
- 阶段 1 禁止沙箱进程创建子进程，强制机制是 Job 活动进程上限固定为 1。不要设置
  `PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY`：真实验证表明限制值会让受管 Python、
  PowerShell 和 `cmd.exe` 在初始化阶段失败，而零值不会禁止子进程。
- AppContainer 不授予互联网、私网或回环网络 capability。
- 默认命令超时 300 秒、作业内存 4 GiB、总输出 1 MiB、任务目录软预算 2 GiB、磁盘检查周期 0.5 秒。
- AppContainer、ACL 或 Job Object 任一步失败都必须终止，不得退化为普通进程。
- 自定义环境块必须提供指向 Attempt 任务目录内部的 `LOCALAPPDATA`；不得暴露宿主真实 AppData 路径。
- 本阶段不得修改 `WorkHost`、路由、模型循环、网络代理或原项目回写逻辑。
- 所有生产代码先有能正确失败的测试，再写最小实现。

---

## 文件结构

- `agent_core/sandbox_contracts.py`：公开数据类型、错误类型和纯结构校验。
- `agent_core/windows_sandbox_native.py`：AppContainer、ACL、扩展启动属性和 Job Object 的 Win32 封装。
- `agent_core/windows_sandbox.py`：Attempt 生命周期、命令验证、环境构造、进程监督、取消和清理。
- `tests/test_sandbox_contracts.py`：跨平台契约和配置验证。
- `tests/test_windows_sandbox.py`：Windows 行为、超时、取消、输出和资源测试。
- `tests/test_windows_sandbox_security.py`：真实 AppContainer 文件、网络、子进程和失败关闭测试。

## 稳定接口

```python
@dataclass(frozen=True)
class SandboxRuntimeConfig:
    runtime_root: Path
    powershell_executable: Path
    python_executable: Path
    search_executable: Path | None = None


@dataclass(frozen=True)
class SandboxLimits:
    command_timeout_seconds: int = 300
    job_memory_bytes: int = 4 * 1024**3
    output_bytes: int = 1024**2
    soft_disk_bytes: int = 2 * 1024**3
    disk_poll_seconds: float = 0.5
    active_process_limit: int = 1


@dataclass(frozen=True)
class SandboxCommand:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: datetime
    finished_at: datetime
    timed_out: bool
    cancelled: bool
    killed: bool
    output_truncated: bool
    termination_reason: str | None
```

```python
class WindowsSandboxRunner:
    def prepare_attempt(self, attempt_id: str, task_root: Path) -> None: ...
    async def run(self, attempt_id: str, command: SandboxCommand) -> SandboxResult: ...
    async def cancel(self, attempt_id: str) -> bool: ...
    def cleanup_attempt(self, attempt_id: str) -> None: ...
    def cleanup_stale_attempts(self) -> None: ...
```

`termination_reason` 只允许 `None`、`timeout`、`cancelled`、`output_limit`、`memory_limit`、`disk_limit`、`process_failed`。启动前参数不合法时抛 `SandboxValidationError`；平台或隔离能力不可用时抛 `SandboxUnavailableError`。

### Task 1: 契约、错误类型和运行时配置

**Files:**
- Create: `agent_core/sandbox_contracts.py`
- Modify: `requirements.txt`
- Test: `tests/test_sandbox_contracts.py`

**Interfaces:**
- Produces: 四个 dataclass、`SandboxValidationError`、`SandboxUnavailableError`、`validate_runtime_config(config)`。

- [x] **Step 1: 写契约失败测试**

覆盖默认限制、冻结数据类、空 argv、非正超时、运行时文件缺失、可执行文件越出 `runtime_root`、Python 非 3.12、PowerShell 非 7.x、运行时路径含重解析点。

- [x] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_sandbox_contracts.py -q`  
Expected: 因 `agent_core.sandbox_contracts` 尚不存在而失败。

- [x] **Step 3: 实现最小契约和配置校验**

- 路径全部 `resolve(strict=True)`。
- 禁止 UNC、设备路径和任一父级含 `FILE_ATTRIBUTE_REPARSE_POINT`。
- 可执行文件必须位于解析后的 `runtime_root` 内。
- 版本探测只能执行已经通过路径校验的绝对路径。
- PowerShell 使用 `--version`，Python 使用 `--version`；版本不符抛 `SandboxValidationError`。
- 不读取或修改 PATH。

- [x] **Step 4: 写依赖声明**

```text
pywin32>=312,<313; platform_system == "Windows"
```

- [x] **Step 5: 运行 GREEN**

Run: `python -m pytest tests/test_sandbox_contracts.py -q`  
Expected: PASS。

```powershell
git add agent_core/sandbox_contracts.py tests/test_sandbox_contracts.py requirements.txt
git commit -m "feat: define windows sandbox contracts"
```

分步提交未执行，原因见文首执行状态。

### Task 2: 命令、路径和环境闸门

**Files:**
- Create: `agent_core/windows_sandbox.py`
- Modify: `tests/test_sandbox_contracts.py`

**Interfaces:**
- Consumes: `SandboxRuntimeConfig`、`SandboxLimits`、`SandboxCommand`。
- Produces: `validate_command(attempt, command)`、`build_sandbox_environment(attempt, command)`。

- [x] **Step 1: 写路径和环境失败测试**

证明：argv 为空或是字符串时拒绝；入口程序未登记时拒绝；cwd 越出任务根目录时拒绝；`..`、UNC、设备路径和重解析点被拒绝；调用方不能覆盖保留环境变量；宿主密钥、代理和用户变量不会泄漏；`LOCALAPPDATA` 固定指向 Attempt 任务目录内部且调用方不能覆盖。

- [x] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_sandbox_contracts.py -q`  
Expected: 新增测试因验证器不存在而失败。

- [x] **Step 3: 实现路径验证**

先做词法规范化，再用目录句柄和 `GetFinalPathNameByHandleW` 取得最终路径核对。可执行文件必须与配置中的解析后绝对路径精确相等；cwd 必须在当前 Attempt 根目录内；不接受命令字符串或 `shell=True`。

- [x] **Step 4: 实现干净环境**

环境只包含沙箱内的 `TEMP`、`TMP`、`HOME`、`USERPROFILE`、`LOCALAPPDATA`，宿主 `SystemRoot`，以及允许表中的 `CI`、`LANG`、`LC_ALL`、`TZ`。`LOCALAPPDATA` 是 AppContainer 使用自定义环境块启动时的必需变量，必须指向 Attempt 任务目录内部，不得使用宿主真实路径。固定加入：

```text
PATH=
PYTHONNOUSERSITE=1
PYTHONDONTWRITEBYTECODE=1
POWERSHELL_TELEMETRY_OPTOUT=1
POWERSHELL_UPDATECHECK=Off
```

PowerShell 内部补充 `-NoLogo -NoProfile -NonInteractive`；禁止通过 `PYTHONPATH`、`PYTHONHOME` 或用户 site-packages 注入代码。

- [x] **Step 5: 运行 GREEN**

Run: `python -m pytest tests/test_sandbox_contracts.py -q`  
Expected: PASS。

```powershell
git add agent_core/windows_sandbox.py tests/test_sandbox_contracts.py
git commit -m "feat: validate sandbox commands and environment"
```

分步提交未执行，原因见文首执行状态。

### Task 3: AppContainer 身份、ACL 和崩溃清理

**Files:**
- Create: `agent_core/windows_sandbox_native.py`
- Modify: `agent_core/windows_sandbox.py`
- Test: `tests/test_windows_sandbox_security.py`

**Interfaces:**
- Produces: `create_profile(name)`、`delete_profile(name)`、`grant_task_access(sid, root)`、`grant_runtime_access(sid, root)`、`revoke_access(sid, root)`。
- Produces: `prepare_attempt()`、`cleanup_attempt()`、`cleanup_stale_attempts()`。

- [x] **Step 1: 写真实 AppContainer 失败测试**

断言 profile 名称由 Attempt ID 的 SHA-256 派生且不含用户输入；准备后存在状态清单；清理后 profile、ACL 和状态清单都消失。

- [x] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_windows_sandbox_security.py -q`  
Expected: 因 native 封装和生命周期不存在而失败。

- [x] **Step 3: 实现 AppContainer 与 ACL**

使用 `CreateAppContainerProfile`；已存在时使用 `DeriveAppContainerSidFromAppContainerName`；capability 数量固定为 0。策略拒绝时抛 `SandboxUnavailableError`。任务目录授予读写权限，运行时目录只授予读取执行权限；通过 Windows 安全接口修改 ACL，不调用 `icacls`；清理时只删除本 Attempt SID 的 ACE。

- [x] **Step 4: 实现状态清单和清理**

状态清单只保存 profile 名称、SID、任务根目录、运行时根目录和创建时间。清理顺序：关闭 Job → 撤销 ACL → 删除 profile → 删除状态清单。陈旧清理必须幂等。

- [x] **Step 5: 运行 GREEN**

Run: `python -m pytest tests/test_windows_sandbox_security.py -q`  
Expected: 生命周期用例 PASS。

```powershell
git add agent_core/windows_sandbox_native.py agent_core/windows_sandbox.py tests/test_windows_sandbox_security.py
git commit -m "feat: manage appcontainer sandbox identities"
```

分步提交未执行，原因见文首执行状态。

### Task 4: 受限进程、Job Object 和结果采集

**Files:**
- Modify: `agent_core/windows_sandbox_native.py`
- Modify: `agent_core/windows_sandbox.py`
- Test: `tests/test_windows_sandbox.py`

**Interfaces:**
- Produces: `WindowsSandboxRunner.run()`、`cancel()`，返回 `SandboxResult`。

- [x] **Step 1: 写执行失败测试**

覆盖正常与非零退出、超时、取消、1 MiB 输出截断、较小测试内存上限、磁盘软上限和重复执行同一 Attempt 的拒绝。

- [x] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_windows_sandbox.py -q`  
Expected: 因 `run` 和 `cancel` 尚未实现而失败。

- [x] **Step 3: 实现扩展进程启动**

使用 `STARTUPINFOEXW` 和 `UpdateProcThreadAttribute` 设置 AppContainer 安全能力以及标准管道句柄白名单。命令行由 argv 使用 Windows 标准引用规则生成，不经过 shell。以 `CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT` 创建。子进程边界由下一步的 Job 活动进程上限强制执行。

- [x] **Step 4: 实现 Job Object**

设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`、活动进程上限 1 和作业内存上限。进程必须先加入 Job 再恢复；加入失败时终止 suspended 进程。

- [x] **Step 5: 实现监督和输出采集**

同时等待进程、超时、取消、磁盘软上限和 stdout/stderr。总共只保留 1 MiB，超出后继续读取并丢弃，避免管道死锁。终止原因按稳定枚举返回，不自动重放状态不明的命令。
单条命令可以缩短超时，但不得把它提高到 `SandboxLimits.command_timeout_seconds` 以上。

- [x] **Step 6: 运行 GREEN**

Run: `python -m pytest tests/test_windows_sandbox.py -q`  
Expected: PASS。

```powershell
git add agent_core/windows_sandbox_native.py agent_core/windows_sandbox.py tests/test_windows_sandbox.py
git commit -m "feat: run commands in constrained windows jobs"
```

分步提交未执行，原因见文首执行状态。

### Task 5: 真实安全边界测试

**Files:**
- Modify: `tests/test_windows_sandbox_security.py`

**Interfaces:**
- Consumes: 完整 `WindowsSandboxRunner`。
- Produces: 阶段 1 安全门槛的自动化证据。

- [x] **Step 1: 增加真实攻击场景**

每条测试启动真实 AppContainer，不只 mock Win32 调用。覆盖：任务目录内可写；目录外不可读写；UNC、设备路径、符号链接和目录联接被拒；未登记程序不能作为入口；Python 和 PowerShell 不能创建子进程；无法连接宿主 localhost 服务；看不到宿主密钥、代理、Profile 和用户注册表；隔离创建失败时没有普通进程启动；陈旧 Attempt 可清理。

- [x] **Step 2: 运行安全测试**

Run: `python -m pytest tests/test_windows_sandbox_security.py -q`  
Expected: 全部 PASS；真实隔离能力不可用必须明确失败，不能静默跳过核心安全用例。

```powershell
git add tests/test_windows_sandbox_security.py
git commit -m "test: verify windows sandbox boundaries"
```

分步提交未执行，原因见文首执行状态。

### Task 6: 阶段验收和经验记录

**Files:**
- Modify: `LESSONS_LEARNED.md`

**Interfaces:**
- Produces: 阶段 1 完成证据；不新增生产接口。

- [x] **Step 1: 运行分层验收**

```powershell
python -m pytest tests/test_sandbox_contracts.py -q
python -m pytest tests/test_windows_sandbox.py -q
python -m pytest tests/test_windows_sandbox_security.py -q
```

Expected: 全部 PASS，且安全测试确认进程令牌属于目标 AppContainer。

- [x] **Step 2: 运行全量验收**

```powershell
python -m pytest -q
python -m compileall -q agent_core agent_tools web_api.py chat_cli.py
git diff --check
```

Expected: 全量测试无新增失败；编译和补丁格式检查退出码为 0。

- [x] **Step 3: 核对范围并记录经验**

确认没有修改 `WorkHost`、路由、模型循环、网络代理或回写路径；没有把 2 GiB 软上限描述为硬边界。在 `LESSONS_LEARNED.md` 记录：AppContainer 不是程序白名单、阶段 1 禁止子进程的原因、磁盘软上限的边界、隔离不可用时失败关闭。

- [ ] **Step 4: 最终提交（未执行）**

```powershell
git add LESSONS_LEARNED.md
git commit -m "docs: record windows sandbox boundaries"
```

## 完成条件

- 三组沙箱测试全部通过。
- 全量测试没有新增失败。
- 安全测试使用真实 AppContainer，而不是普通进程或模拟对象。
- AppContainer、ACL 或 Job Object 失败时不会执行命令。
- 没有接入模型、路由、自动回写或直接网络。
- 磁盘限制在代码、结果和文档中都明确标记为软上限。
- `LESSONS_LEARNED.md` 已记录本阶段可复用经验。
