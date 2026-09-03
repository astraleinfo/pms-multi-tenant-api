"""invitations, forced first-password, and platform support accounts

Three things arrive together because they are one story: nobody is handed a
password any more.

*   `auth_tokens` — a one-time link, by digest only, for "set your password"
    and "reset your password". It follows the same policy as the other auth
    tables: your clinic's rows, or the platform's when no clinic is bound.
*   `users.must_change_password` — an account given a password it did not
    choose can sign in, and the session it gets is good for exactly one thing
    until it chooses its own.
*   `users.is_support` — the account a platform impersonation session writes
    through. It has no password hash and never gets one, so it is not a way in;
    it exists so a support session's rows have a real author, and it is hidden
    from the clinic's staff directory.

Existing rows keep their behaviour: `must_change_password` and `is_support`
both default to false, so nothing already in the database changes meaning.

Revision ID: 0006_invitations
Revises: 0005_reschedule_messages
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_invitations"
down_revision = "0005_reschedule_messages"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"

purpose = postgresql.ENUM("invite", "reset", name="auth_token_purpose", create_type=False)
subject_kind = postgresql.ENUM("user", "platform", name="subject_kind", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    purpose.create(bind, checkfirst=True)

    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("is_support", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "auth_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("purpose", purpose, nullable=False),
        sa.Column("subject_kind", subject_kind, nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_by_label", sa.String(160), nullable=False, server_default=""),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(300), nullable=False, server_default=""),
        sa.CheckConstraint(
            "(subject_kind = 'user' and tenant_id is not null) "
            "or (subject_kind = 'platform' and tenant_id is null)",
            name="ck_auth_token_subject_tenant",
        ),
    )
    op.create_index("ix_auth_tokens_token_hash", "auth_tokens", ["token_hash"], unique=True)
    op.create_index("ix_auth_tokens_subject", "auth_tokens", ["subject_kind", "subject_id"])
    op.create_index("ix_auth_tokens_expires", "auth_tokens", ["expires_at"])
    op.create_index("ix_auth_tokens_tenant_id", "auth_tokens", ["tenant_id"])

    # The same policy `refresh_tokens` and `login_attempts` carry: a clinic sees
    # only its own rows, and a row belonging to no clinic is visible only to a
    # session that has bound no clinic.
    op.execute("ALTER TABLE auth_tokens ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON auth_tokens
        USING (
            tenant_id = {CURRENT}
            OR (tenant_id IS NULL AND {CURRENT} IS NULL)
        )
        WITH CHECK (
            tenant_id = {CURRENT}
            OR (tenant_id IS NULL AND {CURRENT} IS NULL)
        )
        """
    )

    # 0002 granted the application role every table that existed then; this one
    # did not exist yet.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON auth_tokens TO {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON auth_tokens")
    op.drop_index("ix_auth_tokens_tenant_id", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens_expires", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens_subject", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens_token_hash", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    purpose.drop(op.get_bind(), checkfirst=True)

    # Support accounts only make sense while impersonation exists. `users` is
    # FORCE ROW LEVEL SECURITY, so even the owner sees nothing without a clinic
    # bound — the force is lifted for the length of this one statement.
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM users WHERE is_support")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.drop_column("users", "is_support")
    op.drop_column("users", "must_change_password")
