"""Consent forms over the wire.

`ConsentContent` is the form itself — structured, not free layout: a title and
an introduction, the patient details to print, an ordered list of sections, and
the signatures. A section is one of three kinds:

- `text` — a heading and paragraphs (a line starting "- " prints as a bullet)
- `fields` — things the clinic fills in for this patient: the procedure, the site
- `acknowledgements` — statements the patient ticks, each optional or required

Everything a clinic saves passes through this model, so the editor, the
signing screen and the PDF can all trust its shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import ApiModel

#: the patient's own details a form may print, in the order they print
PATIENT_FIELDS = ("full_name", "mrn", "dob", "sex", "phone", "email", "address")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentField(_Strict):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    label: str = Field(min_length=1, max_length=160)
    required: bool = False
    multiline: bool = False


class ConsentStatement(_Strict):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=1000)
    required: bool = True


class ConsentSection(_Strict):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    kind: Literal["text", "fields", "acknowledgements"]
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(default="", max_length=6000)
    fields: list[ConsentField] = Field(default_factory=list, max_length=20)
    statements: list[ConsentStatement] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _kind_has_its_parts(self) -> ConsentSection:
        if self.kind == "fields" and not self.fields:
            raise ValueError(f"“{self.title}” needs at least one field.")
        if self.kind == "acknowledgements" and not self.statements:
            raise ValueError(f"“{self.title}” needs at least one statement.")
        if self.kind != "fields":
            self.fields = []
        if self.kind != "acknowledgements":
            self.statements = []
        return self


class ConsentSignatures(_Strict):
    #: the sentence printed above the patient's signature
    declaration: str = Field(min_length=1, max_length=1000)
    #: the treating clinician countersigns
    provider: bool = True
    #: a member of staff witnesses the signature
    witness: bool = False


class ConsentContent(_Strict):
    title: str = Field(min_length=1, max_length=160)
    intro: str = Field(default="", max_length=4000)
    patient_fields: list[str] = Field(default_factory=lambda: ["full_name", "mrn", "dob"])
    sections: list[ConsentSection] = Field(min_length=1, max_length=30)
    signatures: ConsentSignatures
    #: how long a request stays open before it expires unsigned
    validity_days: int = Field(default=30, ge=1, le=365)

    @field_validator("patient_fields")
    @classmethod
    def _known_patient_fields(cls, value: list[str]) -> list[str]:
        unknown = [v for v in value if v not in PATIENT_FIELDS]
        if unknown:
            raise ValueError(f"Unknown patient detail: {', '.join(unknown)}.")
        # always the patient's name; then the rest in print order, once each
        chosen = set(value) | {"full_name"}
        return [f for f in PATIENT_FIELDS if f in chosen]

    @model_validator(mode="after")
    def _ids_are_unique(self) -> ConsentContent:
        for kind, ids in (
            ("section", [s.id for s in self.sections]),
            ("field", [f.id for s in self.sections for f in s.fields]),
            ("statement", [a.id for s in self.sections for a in s.statements]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"Two {kind}s share an id.")
        return self


# --- forms and versions ------------------------------------------------------------


class ConsentTemplateOut(ApiModel):
    key: str
    label: str
    clinical_type: str | None
    description: str
    content: ConsentContent | None = None


class ConsentVersionOut(ApiModel):
    id: str
    number: int
    status: str
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    published_by: str
    published_at: datetime | None
    archived_at: datetime | None
    #: how many patients have answered this version — a reason it can never change
    consents: int = 0
    content: ConsentContent | None = None


class ConsentFormOut(ApiModel):
    id: str
    name: str
    template_key: str
    template_label: str
    created_by: str
    created_at: datetime
    published: ConsentVersionOut | None
    draft: ConsentVersionOut | None
    versions: list[ConsentVersionOut] = []


class ConsentFormIn(_Strict):
    template_key: str = Field(min_length=1, max_length=40)


class DraftIn(_Strict):
    #: start the draft from this earlier version instead of the published one
    from_version: int | None = Field(default=None, ge=1)


class DraftContentIn(_Strict):
    content: ConsentContent


# --- patients' consents -------------------------------------------------------------


class PatientConsentIn(_Strict):
    patient_id: str = Field(min_length=1, max_length=40)
    form_id: str = Field(min_length=1, max_length=40)
    provider_id: str | None = Field(default=None, max_length=40)
    field_values: dict[str, str] = Field(default_factory=dict)

    @field_validator("field_values")
    @classmethod
    def _bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 60 or any(len(k) > 40 or len(v) > 4000 for k, v in value.items()):
            raise ValueError("Too much text in the form's fields.")
        return {k: v.strip() for k, v in value.items()}


class SignIn(_Strict):
    signer_name: str = Field(min_length=1, max_length=160)
    #: "Patient", or who is signing for them — "Parent", "Guardian" …
    signer_relationship: str = Field(default="Patient", min_length=1, max_length=60)
    acknowledged: list[str] = Field(default_factory=list, max_length=40)
    #: a PNG data URL drawn on the signing screen
    patient_signature: str = Field(min_length=1, max_length=400_000)
    provider_signature: str | None = Field(default=None, max_length=400_000)


class DeclineIn(_Strict):
    reason: str = Field(default="", max_length=1000)


class PatientConsentOut(ApiModel):
    id: str
    patient_id: str
    form_id: str
    form_name: str
    title: str
    version_number: int
    status: Literal["pending", "signed", "declined", "expired"]
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    provider_id: str | None
    provider_name: str
    signer_name: str
    signer_relationship: str
    signed_at: datetime | None
    witnessed_by: str
    declined_at: datetime | None
    decline_reason: str
    has_patient_signature: bool
    has_provider_signature: bool
    acknowledged: list[str]
    field_values: dict[str, str]
    patient: dict
    content: ConsentContent | None = None
