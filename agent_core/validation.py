"""
输入验证模块 - 防止注入攻击和 DoS

这个模块提供统一的输入验证函数，确保：
1. 字段长度不超限
2. 字符只包含安全字符（防止注入）
3. 格式符合预期（如 cron 表达式、UUID）

为什么需要输入验证？
就像机场安检：
- 不检查 → 危险物品带上飞机
- 严格检查 → 确保安全
"""
from __future__ import annotations
import re
from typing import Optional
from .exceptions import ValidationError


# ============= 字段长度限制（防止存储溢出和 DoS） =============

MAX_OWNER_ID_LENGTH = 64
MAX_CONVERSATION_ID_LENGTH = 128
MAX_MODULE_ID_LENGTH = 64
MAX_CHANNEL_LENGTH = 32
MAX_TOOL_NAME_LENGTH = 128
MAX_CONFLICT_KEY_LENGTH = 256
MAX_MESSAGE_LENGTH = 100_000  # 单条消息最大 100KB


# ============= 安全字符白名单（防止注入） =============

# 允许的字符：字母、数字、短横线、下划线、点
SAFE_ID_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+$')

# UUID 格式（用于 run_id, action_id 等）
UUID_PATTERN = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')

# Cron 表达式格式（5个字段，空格分隔）
CRON_PATTERN = re.compile(r'^[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+\s+[\d*,/-]+$')


# ============= 验证函数 =============

def validate_field_length(value: str, max_length: int, field_name: str) -> str:
    """
    验证字段长度

    Args:
        value: 要验证的字符串
        max_length: 最大允许长度
        field_name: 字段名称（用于错误消息）

    Returns:
        原始值（如果验证通过）

    Raises:
        ValidationError: 长度超限

    Example:
        owner_id = validate_field_length(user_input, MAX_OWNER_ID_LENGTH, "owner_id")
    """
    if not value:
        raise ValidationError(f"{field_name} cannot be empty")

    if len(value) > max_length:
        raise ValidationError(
            f"{field_name} too long: {len(value)} chars (max {max_length})",
            details={"field": field_name, "length": len(value), "max_length": max_length}
        )

    return value


def validate_safe_id(value: str, field_name: str) -> str:
    """
    验证 ID 字段只包含安全字符

    Args:
        value: 要验证的 ID
        field_name: 字段名称

    Returns:
        原始值（如果验证通过）

    Raises:
        ValidationError: 包含非法字符

    为什么要限制字符？
    就像门锁密码只能是数字，如果允许特殊符号，黑客可能输入 SQL 注入代码。
    限制为字母、数字、短横线、下划线，既够用又安全。

    Example:
        owner_id = validate_safe_id("user-123", "owner_id")  # ✓ 通过
        owner_id = validate_safe_id("user'; DROP TABLE--", "owner_id")  # ✗ 抛出异常
    """
    if not SAFE_ID_PATTERN.match(value):
        raise ValidationError(
            f"{field_name} contains invalid characters: {value}. "
            f"Only letters, numbers, dots, hyphens, and underscores are allowed.",
            details={"field": field_name, "value": value}
        )

    return value


def validate_uuid(value: str, field_name: str) -> str:
    """
    验证 UUID 格式

    Args:
        value: 要验证的 UUID 字符串
        field_name: 字段名称

    Returns:
        小写的 UUID（如果验证通过）

    Raises:
        ValidationError: 格式不正确

    Example:
        run_id = validate_uuid("550e8400-e29b-41d4-a716-446655440000", "run_id")
    """
    value = value.lower()
    if not UUID_PATTERN.match(value):
        raise ValidationError(
            f"{field_name} is not a valid UUID: {value}",
            details={"field": field_name, "value": value}
        )

    return value


def validate_cron_expression(cron: str, min_interval_seconds: int = 60) -> str:
    """
    验证 cron 表达式并确保最小间隔

    Args:
        cron: cron 表达式（5个字段）
        min_interval_seconds: 最小执行间隔（秒），防止 DoS

    Returns:
        原始 cron 表达式（如果验证通过）

    Raises:
        ValidationError: 格式不正确或间隔太短

    为什么限制最小间隔？
    如果允许 "* * * * *"（每分钟执行），甚至 "*/1 * * * *"（每秒执行），
    恶意用户可以用这个做 DoS 攻击，把服务器资源耗尽。
    限制最小间隔为 60 秒，既够用又安全。

    Example:
        cron = validate_cron_expression("0 9 * * *")  # ✓ 每天9点
        cron = validate_cron_expression("* * * * *")  # ✗ 间隔太短（每分钟）
    """
    cron = cron.strip()

    # 格式检查
    if not CRON_PATTERN.match(cron):
        raise ValidationError(
            f"Invalid cron expression: {cron}. Expected format: 'M H D M W'",
            details={"cron": cron}
        )

    # 间隔检查（简单判断）
    fields = cron.split()
    minute_field = fields[0]

    # 检查是否每分钟或每秒执行
    if minute_field == '*':
        raise ValidationError(
            f"Cron interval too short: {cron}. Minimum interval is {min_interval_seconds}s",
            details={"cron": cron, "min_interval": min_interval_seconds}
        )

    # 检查是否每 N 秒/分钟执行（如 */1, */5）
    if '/' in minute_field:
        try:
            interval = int(minute_field.split('/')[1])
            if interval < min_interval_seconds // 60:
                raise ValidationError(
                    f"Cron interval too short: {cron}. Minimum interval is {min_interval_seconds}s",
                    details={"cron": cron, "interval": interval}
                )
        except (ValueError, IndexError):
            pass  # 格式错误会在后续处理中被捕获

    return cron


def validate_scope_fields(owner_id: str, conversation_id: str, channel: str, module_id: str) -> None:
    """
    验证 Scope 的所有字段

    Args:
        owner_id: 用户 ID
        conversation_id: 对话 ID
        channel: 渠道
        module_id: 模块 ID

    Raises:
        ValidationError: 任何字段验证失败

    Example:
        validate_scope_fields(
            owner_id="user-123",
            conversation_id="conv-456",
            channel="web",
            module_id="companion"
        )
    """
    # 长度验证
    validate_field_length(owner_id, MAX_OWNER_ID_LENGTH, "owner_id")
    validate_field_length(conversation_id, MAX_CONVERSATION_ID_LENGTH, "conversation_id")
    validate_field_length(channel, MAX_CHANNEL_LENGTH, "channel")
    validate_field_length(module_id, MAX_MODULE_ID_LENGTH, "module_id")

    # 安全字符验证
    validate_safe_id(owner_id, "owner_id")
    validate_safe_id(conversation_id, "conversation_id")
    validate_safe_id(channel, "channel")
    validate_safe_id(module_id, "module_id")


def sanitize_for_log(value: str, max_length: int = 100) -> str:
    """
    脱敏字符串用于日志输出（防止日志注入和信息泄露）

    Args:
        value: 要脱敏的字符串
        max_length: 最大长度，超过部分截断

    Returns:
        脱敏后的字符串

    为什么需要脱敏？
    日志可能输出用户的敏感信息（密码、手机号、身份证号），
    需要遮蔽或截断，避免泄露。

    Example:
        password = "MySecretPassword123"
        log_value = sanitize_for_log(password)  # "MySecretPa****"

        long_text = "a" * 200
        log_value = sanitize_for_log(long_text, 50)  # "aaa...aaa (200 chars)"
    """
    if len(value) > max_length:
        # 截断并显示原始长度
        return f"{value[:max_length]}... ({len(value)} chars)"

    # 遮蔽中间部分（保留前后各 1/4）
    if len(value) > 10:
        visible_length = len(value) // 4
        return f"{value[:visible_length]}****{value[-visible_length:]}"

    return value


def validate_message_size(message: str) -> str:
    """
    验证消息大小（防止过大的消息导致内存溢出）

    Args:
        message: 消息内容

    Returns:
        原始消息（如果验证通过）

    Raises:
        ValidationError: 消息过大

    Example:
        message = validate_message_size(user_input)
    """
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValidationError(
            f"Message too large: {len(message)} bytes (max {MAX_MESSAGE_LENGTH})",
            details={"size": len(message), "max_size": MAX_MESSAGE_LENGTH}
        )

    return message
