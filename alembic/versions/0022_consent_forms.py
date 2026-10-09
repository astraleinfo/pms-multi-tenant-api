"""consent forms: a clinic's forms, their versions, and patients' consents

Three tenant-owned tables, each under the same forced row-level security as
every other clinical table:

- `consent_forms` — one clinic's form, made from a system template (the
  templates themselves are code, and read-only).
- `consent_form_versions` — v1, v2 … of a form. At most one draft and one
  published version per form, held by two partial unique indexes.
- `patient_consents` — a patient's consent against one exact version, with the
  content, the patient's details and the signatures copied onto it.

Two triggers hold the rules the service also enforces, so no other path can
break them: a version's content is frozen once it leaves `draft`, and a consent
that has been signed or declined cannot be changed at all.

No data is written: a clinic's default form is made the first time its consent
forms are listed (and when a clinic is created), not backfilled here.

Revision ID: 0022_consent_forms
Revises: 0021_drop_module_entitlements
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022_consent_forms"
down_revision = "0021_drop_module_entitlements"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
#: read by `test_every_tenant_owned_table_has_a_policy`
OWNED_TABLES = ["consent_forms", "consent_form_versions", "patient_consents"]

version_status = postgresql.ENUM(
    "draft", "published", "archived", name="consent_version_status", create_type=False
)
consent_status = postgresql.ENUM(
    "pending", "signed", "declined", "expired", name="consent_status", create_type=False
)


def _uuid(name: str, *args, **kw) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kw)


def _tenant() -> sa.Column:
    return _uuid("tenant_id", sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _label(name: str) -> sa.Column:
    return sa.Column(name, sa.String(160), nullable=False, server_default="")


def upgrade() -> None:
    bind = op.get_bind()
    version_status.create(bind, checkfirst=True)
    consent_status.create(bind, checkfirst=True)

    op.create_table(
        "consent_forms",
        _uuid("id", primary_key=True),
        _tenant(),
        sa.Column("template_key", sa.String(40), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        _uuid("created_by_id", nullable=True),
        _label("created_by_label"),
        *_stamps(),
    )
    op.create_index("ix_consent_forms_tenant_id", "consent_forms", ["tenant_id"])
    op.create_index("ix_consent_forms_tenant_id_id", "consent_forms", ["tenant_id", "id"])
    op.create_index("ix_consent_forms_tenant_template", "consent_forms", ["tenant_id", "template_key"])

    op.create_table(
        "consent_form_versions",
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("form_id", sa.ForeignKey("consent_forms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", version_status, nullable=False, server_default="draft"),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _label("created_by_label"),
        _label("updated_by_label"),
        _label("published_by_label"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_consent_form_versions_tenant_id", "consent_form_versions", ["tenant_id"])
    op.create_index("ix_consent_form_versions_tenant_id_id", "consent_form_versions", ["tenant_id", "id"])
    op.create_index(
        "uq_consent_versions_form_number", "consent_form_versions", ["form_id", "number"], unique=True
    )
    op.create_index(
        "uq_consent_versions_one_draft",
        "consent_form_versions",
        ["form_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.create_index(
        "uq_consent_versions_one_published",
        "consent_form_versions",
        ["form_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )

    op.create_table(
        "patient_consents",
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("patient_id", sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        _uuid("form_id", sa.ForeignKey("consent_forms.id", ondelete="RESTRICT"), nullable=False),
        _uuid("version_id", sa.ForeignKey("consent_form_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("patient_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "field_values",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _uuid("provider_id", nullable=True),
        _label("provider_name"),
        sa.Column("status", consent_status, nullable=False, server_default="pending"),
        _label("requested_by_label"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "acknowledged",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        _label("signer_name"),
        sa.Column("signer_relationship", sa.String(60), nullable=False, server_default=""),
        sa.Column("patient_signature", sa.LargeBinary(), nullable=True),
        sa.Column("provider_signature", sa.LargeBinary(), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        _label("witnessed_by_label"),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=False, server_default=""),
        *_stamps(),
    )
    op.create_index("ix_patient_consents_tenant_id", "patient_consents", ["tenant_id"])
    op.create_index("ix_patient_consents_tenant_id_id", "patient_consents", ["tenant_id", "id"])
    op.create_index(
        "ix_patient_consents_tenant_patient", "patient_consents", ["tenant_id", "patient_id", "created_at"]
    )

    for table in OWNED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = {CURRENT})
                WITH CHECK (tenant_id = {CURRENT})
            """
        )
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}';
                END IF;
            END
            $$;
            """
        )

    # --- the rules, underneath the service --------------------------------------
    op.execute(
        """
        CREATE FUNCTION consent_version_frozen() RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'draft' AND (
                NEW.content IS DISTINCT FROM OLD.content
                OR NEW.number <> OLD.number
                OR NEW.form_id <> OLD.form_id
                OR NEW.status = 'draft'
            ) THEN
                RAISE EXCEPTION 'consent form version % is %, and cannot be changed', OLD.number, OLD.status
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER consent_version_frozen BEFORE UPDATE ON consent_form_versions
            FOR EACH ROW EXECUTE FUNCTION consent_version_frozen()
        """
    )
    op.execute(
        """
        CREATE FUNCTION patient_consent_final() RETURNS trigger AS $$
        BEGIN
            IF OLD.status IN ('signed', 'declined') THEN
                RAISE EXCEPTION 'a % consent cannot be changed', OLD.status
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER patient_consent_final BEFORE UPDATE ON patient_consents
            FOR EACH ROW EXECUTE FUNCTION patient_consent_final()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS patient_consent_final ON patient_consents")
    op.execute("DROP FUNCTION IF EXISTS patient_consent_final()")
    op.execute("DROP TRIGGER IF EXISTS consent_version_frozen ON consent_form_versions")
    op.execute("DROP FUNCTION IF EXISTS consent_version_frozen()")
    for table in reversed(OWNED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
    consent_status.drop(bind=op.get_bind(), checkfirst=True)
    version_status.drop(bind=op.get_bind(), checkfirst=True)
