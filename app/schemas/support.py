"""Support-request payloads: a clinic asking the platform for something."""

from __future__ import annotations

from pydantic import Field, model_validator

from app.models.enums import SupportRequestKind, SupportRequestStatus
from app.schemas.common import ApiModel, InputModel, stamp


class SupportRequestIn(InputModel):
    kind: SupportRequestKind
    #: optional for a `walkthrough`; a `general` request has to say something
    message: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _general_asks_say_something(self) -> SupportRequestIn:
        if self.kind == SupportRequestKind.GENERAL and not self.message.strip():
            raise ValueError("say what you need help with")
        return self


class SupportRequestOut(ApiModel):
    id: str
    tenant_id: str
    kind: SupportRequestKind
    message: str
    requested_by: str
    #: whether the support desk has been emailed — the request stands either way
    status: SupportRequestStatus
    created_at: str | None
    sent_at: str | None

    @classmethod
    def of(cls, r, tz: str | None = None) -> SupportRequestOut:
        return cls(
            id=str(r.id), tenant_id=str(r.tenant_id), kind=r.kind,
            message=r.message, requested_by=r.requested_by_label, status=r.status,
            created_at=stamp(r.created_at, tz), sent_at=stamp(r.sent_at, tz),
        )
