"""
媒体模块工具

提供 TTS、ASR 任务提交和状态查询工具。
"""
from typing import Any

from agent_core.contracts import EFFECT_PERSISTENT, EFFECT_READ, Scope, ToolResult, ToolSpec
from agent_core.media_service import MediaJobRequest, MediaTaskService
from agent_core.tools import classify_tool_error


def make_media_submit_tts_tool(service: MediaTaskService) -> ToolSpec:
    """创建 TTS 任务提交工具"""

    async def handler(text: str, voice: str | None, scope: Scope) -> ToolResult:
        """
        提交 TTS 任务，将文本转为语音

        Args:
            text: 要转换的文本
            voice: 语音模型（可选）
            scope: 作用域

        Returns:
            包含 job_id 的工具结果
        """
        if not text or not text.strip():
            return ToolResult(
                status="failed",
                error="文本不能为空",
                error_kind="permanent",
            )

        request = MediaJobRequest(
            kind="tts",
            scope=scope,
            text=text.strip(),
            voice=voice,
        )

        try:
            response = await service.submit_job(request)
            return ToolResult(
                status="ok",
                output={
                    "message": f"TTS 任务已提交，job_id: {response.job_id}",
                    "job_id": response.job_id,
                },
            )
        except Exception as e:
            return ToolResult(
                status="failed",
                error=f"提交 TTS 任务失败: {e}",
                error_kind=classify_tool_error(e),
            )

    return ToolSpec(
        name="media.submit_tts",
        schema={
            "type": "function",
            "function": {
                "name": "media.submit_tts",
                "description": "提交 TTS 任务，将文本转为语音。任务异步执行，立即返回 job_id",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "要转换为语音的文本",
                        },
                        "voice": {
                            "type": "string",
                            "description": "语音模型（可选）",
                        },
                    },
                    "required": ["text"],
                },
            },
        },
        handler=handler,
        risk="low",
        effect=EFFECT_PERSISTENT,
    )


def make_media_submit_asr_tool(service: MediaTaskService) -> ToolSpec:
    """创建 ASR 任务提交工具"""

    async def handler(input_artifact_id: str, scope: Scope) -> ToolResult:
        """
        提交 ASR 任务，将语音转为文本

        Args:
            input_artifact_id: 输入音频的 artifact ID
            scope: 作用域

        Returns:
            包含 job_id 的工具结果
        """
        request = MediaJobRequest(
            kind="asr",
            scope=scope,
            input_artifact_id=input_artifact_id,
        )

        try:
            response = await service.submit_job(request)
            return ToolResult(
                status="ok",
                output={
                    "message": f"ASR 任务已提交，job_id: {response.job_id}",
                    "job_id": response.job_id,
                },
            )
        except Exception as e:
            return ToolResult(
                status="failed",
                error=f"提交 ASR 任务失败: {e}",
            )

    return ToolSpec(
        name="media.submit_asr",
        schema={
            "type": "function",
            "function": {
                "name": "media.submit_asr",
                "description": "提交 ASR 任务，将语音转为文本。任务异步执行，立即返回 job_id",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input_artifact_id": {
                            "type": "string",
                            "description": "输入音频的 artifact ID",
                        },
                    },
                    "required": ["input_artifact_id"],
                },
            },
        },
        handler=handler,
        risk="low",
        effect=EFFECT_PERSISTENT,
    )


def make_media_get_status_tool(service: MediaTaskService) -> ToolSpec:
    """创建任务状态查询工具"""

    async def handler(job_id: str, scope: Scope) -> ToolResult:
        """
        查询媒体任务状态

        Args:
            job_id: 任务 ID
            scope: 作用域

        Returns:
            任务状态信息
        """
        try:
            status = await service.get_job_status(job_id, scope)
            if not status:
                return ToolResult(
                    status="failed",
                    error="任务不存在或无权访问",
                    error_kind="permanent",
                )

            output = {
                "job_id": job_id,
                "status": status.status,
            }

            if status.status == "completed":
                if status.artifact_id:
                    output["message"] = f"任务已完成。artifact_id: {status.artifact_id}"
                    output["artifact_id"] = status.artifact_id
                elif status.transcript:
                    output["message"] = f"任务已完成。转录结果: {status.transcript}"
                    output["transcript"] = status.transcript
                else:
                    output["message"] = "任务已完成"
                return ToolResult(status="ok", output=output)

            elif status.status == "failed":
                return ToolResult(
                    status="failed",
                    error=f"任务失败: {status.error or '未知错误'}",
                    output={"job_id": job_id, "status": status.status},
                )

            elif status.status == "cancelled":
                output["message"] = "任务已取消"
                return ToolResult(status="ok", output=output)

            else:  # queued or running
                output["message"] = f"任务状态: {status.status}"
                return ToolResult(status="ok", output=output)

        except Exception as e:
            return ToolResult(
                status="failed",
                error=f"查询任务状态失败: {e}",
                error_kind=classify_tool_error(e),
            )

    return ToolSpec(
        name="media.get_status",
        schema={
            "type": "function",
            "function": {
                "name": "media.get_status",
                "description": "查询媒体任务（TTS/ASR）的状态。返回任务是否完成、结果或错误信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "任务 ID（由 submit_tts 或 submit_asr 返回）",
                        },
                    },
                    "required": ["job_id"],
                },
            },
        },
        handler=handler,
        risk="low",
        effect=EFFECT_READ,
    )


def make_media_cancel_tool(service: MediaTaskService) -> ToolSpec:
    """创建任务取消工具

    取消是**高风险**操作：它会把用户排队/正在跑的活儿直接掐掉，可能丢掉
    已经算了一半的产物、也可能让用户白等一场。这类"不可逆地打断用户意图"
    的动作必须暂停等确认，和查询状态不是一回事。

    handler 签名固定为 ToolExecutor 的形状 ``(arguments, scope)`` ——
    ToolExecutor 是按**位置**调 ``handler(call.arguments, scope)`` 的。
    媒体工具里其余几个还是关键字签名，属于既有缺陷，本轮不动它们的调用链；
    但取消必须能在真实链路上被调起来，所以这里按执行器的形状写。
    """

    async def handler(arguments: dict[str, Any], scope: Scope) -> ToolResult:
        """
        取消媒体任务

        Args:
            arguments: 工具入参，需要有 job_id
            scope: 作用域

        Returns:
            取消结果
        """
        # 空 job_id 直接失败：没有目标就去调服务，等于让服务去猜一个
        # 不存在的任务，返回的错误还会误导模型以为"任务没了"。
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id:
            return ToolResult(
                status="failed",
                error="缺少要取消的任务 ID",
                error_kind="permanent",
            )

        try:
            status = await service.cancel_job(job_id, scope)
            if not status:
                # 任务不存在或不属于这个 Scope 时返回同一个说法 ——
                # 区分两者等于替人确认"这个 id 在别人那儿存在"。
                return ToolResult(
                    status="failed",
                    error="任务不存在或无权访问",
                    error_kind="permanent",
                )

            if status.status in ("completed", "failed"):
                # 已经结束了就说结束了：把它说成"已取消"是让模型对用户撒谎。
                return ToolResult(
                    status="ok",
                    output={
                        "message": f"任务已终止，无法取消（状态: {status.status}）",
                        "job_id": job_id,
                        "status": status.status,
                    },
                )

            return ToolResult(
                status="ok",
                output={
                    "message": f"任务已标记为取消（当前状态: {status.status}）",
                    "job_id": job_id,
                    "status": status.status,
                },
            )

        except Exception as e:
            return ToolResult(
                status="failed",
                error=f"取消任务失败: {e}",
                error_kind=classify_tool_error(e),
            )

    return ToolSpec(
        name="media.cancel",
        schema={
            "type": "function",
            "function": {
                "name": "media.cancel",
                "description": "取消正在队列中或运行中的媒体任务",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "要取消的任务 ID",
                        },
                    },
                    "required": ["job_id"],
                },
            },
        },
        handler=handler,
        risk="high",
        effect=EFFECT_PERSISTENT,
        confirmation_title="取消语音任务",
    )


def make_media_get_artifact_tool(service: MediaTaskService) -> ToolSpec:
    """创建 artifact 获取工具"""

    async def handler(artifact_id: str, scope: Scope) -> ToolResult:
        """
        获取 artifact 元信息

        Args:
            artifact_id: Artifact ID
            scope: 作用域

        Returns:
            Artifact 元信息
        """
        try:
            artifact = await service.get_artifact(artifact_id, scope)
            if not artifact:
                return ToolResult(
                    status="failed",
                    error=f"Artifact 不存在或无权访问: {artifact_id}",
                    error_kind="permanent",
                )

            return ToolResult(
                status="ok",
                output={
                    "message": (
                        f"Artifact 信息:\n"
                        f"- ID: {artifact.artifact_id}\n"
                        f"- 类型: {artifact.media_type}\n"
                        f"- 大小: {artifact.size_bytes} 字节\n"
                        f"- 创建时间: {artifact.created_at}\n"
                        f"- 过期时间: {artifact.expires_at}\n"
                        f"- 文件路径: {artifact.path}"
                    ),
                    "artifact_id": artifact.artifact_id,
                    "media_type": artifact.media_type,
                    "size_bytes": artifact.size_bytes,
                    "path": str(artifact.path),
                },
            )

        except Exception as e:
            return ToolResult(
                status="failed",
                error=f"获取 artifact 失败: {e}",
                error_kind=classify_tool_error(e),
            )

    return ToolSpec(
        name="media.get_artifact",
        schema={
            "type": "function",
            "function": {
                "name": "media.get_artifact",
                "description": "获取 artifact（音频文件）的元信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {
                            "type": "string",
                            "description": "Artifact ID",
                        },
                    },
                    "required": ["artifact_id"],
                },
            },
        },
        handler=handler,
        risk="low",
        effect=EFFECT_READ,
    )
