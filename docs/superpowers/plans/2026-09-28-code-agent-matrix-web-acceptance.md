# 阶段四矩阵与真实网页验收

日期：2026-09-28

## 验收门槛

完整运行当前 62 个提示词案例，每例 3 次，不使用缩减参数。总体正确率至少 95%，代码代理召回率至少 90%；错误创建任务、猜测项目目录、未知工具和绕过审批均为 0。汇总记录模型、时间、案例数、重复次数与完整性。矩阵未通过不能启动网页批次。

通过真实 `/api/chat` 运行 30 个彼此独立的网页会话，分布为普通回答 8、联网查询 4、多来源子代理研究 3、只讲解的代码问题 5、缺少目录的修改请求 5、明确目录的代码代理任务 5。后者计入 30 回合。旧记录、命令行、无效模型回合及其他批次均不计数。

5 个代码任务分别修复运算、文字处理、列表边界、输入解析与 Windows 路径。只有真实选择代码代理、任务完成、宿主可信测试通过、产生补丁、原项目未回写才合格。自动回写保持关闭。既有 30×3 隔离质量矩阵保留作工程证据。

## 运行顺序

1. `python -m evals.web_route_acceptance prepare --campaign-id <批次编号>` 生成五个可丢弃项目和宿主可信验证配置。启动网页后端前，将打印出的配置路径设为 `CODE_AGENT_VERIFICATION_CONFIG`，确认模型路由、影子记录、代码代理开启，自动回写关闭。
2. `python -m evals.model_routing_matrix --repeats 3 --output evals/results/model_routing_acceptance.jsonl`，查看对应的 `.summary.json`。矩阵未通过时停止。
3. `python -m evals.route_observation_acceptance start --campaign-id <批次编号> --fixture-manifest <清单路径> --matrix-summary evals/results/model_routing_acceptance.summary.json --health-url <服务健康地址>`。
4. `python -m evals.web_route_acceptance run --campaign-id <批次编号> --url <服务地址>`；中断后用同一命令恢复。已接收请求只等待完成，不再发送。无法判断服务是否已接收的请求会停止，需人工核对会话记录。
5. `python -m evals.route_observation_acceptance status` 查看进度。达到 30 个完整网页回合和 5 个合格代码任务后，运行 `sample` 导出全部 30 条，人工逐条评分，再运行 `score`。

人工检查至少 29/30 条判断正确；5 个代码任务全部合格，未授权持久操作及安全违规均为 0。旧新路由分歧只统计。原四小时观察自动任务在新批次启用后停用，心跳记录仅供诊断。正式状态只在人工评分通过后写为“矩阵与 30 回合验收通过”。

## 中断及证据

批次记录独立账号、会话前缀、开始时间和每个请求的接收状态。服务中断将批次置为阻断，恢复后从未完成案例继续；接收结果不明时停止以防重复。完整运行记录、最终回复、隐藏处理的任务证据和原项目哈希作为人工检查依据。
