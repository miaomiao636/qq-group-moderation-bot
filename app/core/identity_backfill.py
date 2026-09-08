"""T-305 中立身份回填 SQL（Alembic 迁移与测试共用单一事实来源）。

expand/migrate/contract 的 migrate 步骤：把旧镜像列（group_openid /
member_openid / message_id）回填到中立列（provider / external_group_id /
external_user_id / external_message_id），provider 统一为 ``qq_official``
——历史数据全部产生自官方通道。

语句均为幂等：只回填中立列仍为空的行，不覆盖任何已写入的中立值
（混合版本期间新代码可能已写入 onebot 数据）。
"""

from __future__ import annotations

# 每条语句：表名 -> 回填 SQL（仅补空行）。
NEUTRAL_BACKFILL_STATEMENTS: tuple[str, ...] = (
    # shadow_decisions
    """
    UPDATE shadow_decisions
    SET provider = 'qq_official',
        external_group_id = group_openid,
        external_user_id = member_openid
    WHERE external_group_id = ''
    """,
    # violation_records
    """
    UPDATE violation_records
    SET provider = 'qq_official',
        external_group_id = group_openid,
        external_user_id = member_openid
    WHERE external_group_id = ''
    """,
    # cases
    """
    UPDATE cases
    SET provider = 'qq_official',
        external_group_id = group_openid,
        external_user_id = member_openid
    WHERE external_group_id = ''
    """,
    # action_intents
    """
    UPDATE action_intents
    SET provider = 'qq_official',
        external_group_id = group_openid,
        external_user_id = target_member_openid,
        external_message_id = message_id
    WHERE external_group_id = ''
    """,
    # action_logs
    """
    UPDATE action_logs
    SET provider = 'qq_official',
        external_group_id = group_openid,
        external_user_id = target_member_openid,
        external_message_id = message_id
    WHERE external_group_id = ''
    """,
    # feedback_records
    """
    UPDATE feedback_records
    SET provider = 'qq_official',
        external_group_id = group_openid,
        external_user_id = member_openid
    WHERE external_group_id = ''
    """,
    # ai_usage_logs（provider 列此处含义为AI供应商，只回填群中立标识）
    """
    UPDATE ai_usage_logs
    SET external_group_id = group_openid
    WHERE external_group_id = ''
    """,
)
