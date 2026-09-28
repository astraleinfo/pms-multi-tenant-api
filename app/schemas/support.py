"""Support-request payloads: a clinic asking the platform for something."""

from __future__ import annotations

from pydantic import Field, model_validator

from app.models.enums import SupportRequestKind, SupportRequestStatus
from app.schemas.common import ApiModel, InputModel, stamp


class SupportRequestIn(InputModel):
    kind: SupportRequestKind
    #: required for `activation` and `walkthrough` — those are about one module
    feature_key: str | None = Field(default=None, max_length=40)
    message: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _module_asks_name_a_module(self) -> SupportRequestIn:
        if self.kind != SupportRequestKind.GENERAL and not self.feature_key:
            raise ValueError("feature_key is required for an activation or walkthrough request")
        if self.kind == SupportRequestKind.GENERAL and not (self.message or self.feature_key):
            raise ValueError("say what you need help with")
        return self


class SupportRequestOut(ApiModel):
    id: str
    tenant_id: str
    kind: SupportRequestKind
    feature_key: str | None
    message: str
    requested_by: str
    #: whether the support desk has been emailed — the request stands either way
    status: SupportRequestStatus
    created_at: str | None
    sent_at: str | None

    @classmethod
    def of(cls, r, tz: str | None = None) -> SupportRequestOut:
        return cls(
            id=str(r.id), tenant_id=str(r.tenant_id), kind=r.kind, feature_key=r.feature_key,
            message=r.message, requested_by=r.requested_by_label, status=r.status,
            created_at=stamp(r.created_at, tz), sent_at=stamp(r.sent_at, tz),
        )
