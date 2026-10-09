"""merge the two heads left by the parallel referral and flat-fee work

`0021_referral_credits` and `0021_drop_module_entitlements` both branched off
`0020_tenant_config_logo`; the latter has since grown `0022`–`0025` on top. The
two touch different columns of `platform_settings` and otherwise unrelated
tables, so nothing needs migrating here — this just gives the tree one head
again, whichever branch a database is already on.

Revision ID: 0026_merge_heads
Revises: 0021_referral_credits, 0025_patient_notes
Create Date: 2026-10-09 00:00:00.000000
"""

from __future__ import annotations

revision = '0026_merge_heads'
down_revision = ('0021_referral_credits', '0025_patient_notes')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
