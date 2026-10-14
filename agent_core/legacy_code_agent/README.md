# 旧代码代理归档

这里完整保留原来的进程内代码代理循环、工具、网络代理、预算及组装工厂，供历史测试和单独研究复用。
生产的 Web、CLI 和主聊天均只使用 DeepSeek Harness 的 ACP 桥接；这个目录没有自动注册、回退或配置开关。

需要独立研究时显式导入 `agent_core.legacy_code_agent.factory.build_legacy_code_provider`。
共用的快照、验证、结果协议和补丁审批仍属于 Host，未搬入这个目录。
历史测试直接导入这里的实现，不能据此判断生产默认路由。
