# BigModel 嵌入

记忆向量使用 Zhipu BigModel 的 `/embeddings` 接口和 `embedding-3` 模型；聊天补全配置保持独立。

应用始终通过 `agent_core.project_env.load_project_env()` 加载项目根目录的 `.env`，因此启动时的工作目录不会影响配置。请将以下设置加入该文件：

```dotenv
ZHIPUAI_API_KEY=your_zhipu_api_key
ZHIPUAI_EMBEDDING_MODEL=embedding-3
ZHIPUAI_EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
ZHIPUAI_EMBEDDING_DIMENSIONS=2048
```

Qdrant 集合大小必须与 `ZHIPUAI_EMBEDDING_DIMENSIONS` 一致。新仓库默认使用 2048 维。使用不同维度创建的现有持久化集合，必须先重建到新的向量目录，之后才能使用该 provider。
