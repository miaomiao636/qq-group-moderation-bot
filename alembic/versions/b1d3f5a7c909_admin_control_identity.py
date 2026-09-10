"""R-105 expand provider settings, action ownership and human change plans.

Revision ID: b1d3f5a7c909
Revises: e1a4b8c2d3f5
"""

import sqlalchemy as sa
from alembic import op

revision = "b1d3f5a7c909"
down_revision = "e1a4b8c2d3f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_group_settings",
        sa.Column("provider", sa.String(16), primary_key=True),
        sa.Column("external_group_id", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("moderation_enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("action_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "group_action_owners",
        sa.Column("external_group_id", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
    )
    op.create_table(
        "admin_change_plans",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("requestor", sa.String(64), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("expected_state_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Unknown/ambiguous legacy settings remain readable in the legacy table but
    # cannot authorize actions. Even uniquely identified rows require re-arming
    # after this safety migration; previous approval did not bind a provider.
    connection = op.get_bind()
    identities = sa.text("""
        SELECT provider, external_group_id FROM shadow_decisions
        WHERE provider IN ('onebot','qq_official') AND external_group_id != ''
        UNION SELECT message_provider, external_group_id FROM group_provider_routes
        WHERE message_provider IN ('onebot','qq_official')
    """)
    seen: dict[str, set[str]] = {}
    for provider, group in connection.execute(identities):
        seen.setdefault(group, set()).add(provider)
    for row in connection.execute(sa.text("SELECT * FROM group_settings")).mappings():
        providers = seen.get(row["group_openid"], set())
        for provider in providers:
            connection.execute(
                sa.text("""
                INSERT INTO provider_group_settings
                (provider, external_group_id, name, moderation_enabled, action_enabled, version, updated_at)
                VALUES (:provider, :group_id, :name, :moderation, 0, 1, :updated)
            """),
                {
                    "provider": provider,
                    "group_id": row["group_openid"],
                    "name": row["name"],
                    "moderation": row["moderation_enabled"] if len(providers) == 1 else True,
                    "updated": row["updated_at"],
                },
            )
    for group, providers in seen.items():
        connection.execute(
            sa.text("INSERT INTO group_action_owners (external_group_id,provider) VALUES (:g,:p)"),
            {"g": group, "p": next(iter(providers)) if len(providers) == 1 else ""},
        )

    # Clear the legacy authorization immediately as well as on downgrade: a
    # mixed/rolled-back binary must never reuse the former ambiguous permission.
    op.execute("UPDATE group_settings SET action_enabled = 0")


def downgrade() -> None:
    # Do not restore old action=true values when reverting safety controls.
    op.execute("UPDATE group_settings SET action_enabled = 0")
    op.drop_table("admin_change_plans")
    op.drop_table("group_action_owners")
    op.drop_table("provider_group_settings")
