"""The system consent templates — one per kind of practice, read-only.

They are code, not rows: nothing a clinic does can reach them. A clinic gets
its own copy (`consent_forms` + a first version) and edits that; "reset to
default" copies the template again. The wording is a sound starting point that
each clinic is expected to review with its own clinicians — it is not legal
advice, and the forms say what the clinic asks them to say.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.consent import ConsentContent


@dataclass(frozen=True)
class Template:
    key: str
    label: str
    #: the clinical type this is the default for; `None` for the general form
    clinical_type: str | None
    description: str
    content: dict


def _text(id_: str, title: str, body: str) -> dict:
    return {"id": id_, "kind": "text", "title": title, "body": body}


def _fields(id_: str, title: str, fields: list[tuple[str, str, bool, bool]], body: str = "") -> dict:
    return {
        "id": id_,
        "kind": "fields",
        "title": title,
        "body": body,
        "fields": [
            {"id": fid, "label": label, "required": required, "multiline": multiline}
            for fid, label, required, multiline in fields
        ],
    }


def _acks(id_: str, title: str, statements: list[str], body: str = "") -> dict:
    return {
        "id": id_,
        "kind": "acknowledgements",
        "title": title,
        "body": body,
        "statements": [
            {"id": f"{id_}-{n}", "text": t, "required": True} for n, t in enumerate(statements, 1)
        ],
    }


_DECLARATION = (
    "I confirm that I have read, or had read to me, the information in this form; that I have "
    "had the chance to ask questions and they have been answered to my satisfaction; and that I "
    "give my consent freely."
)

_COMMON_ACKS = [
    "I have been told what the treatment involves, its expected benefits and its material risks.",
    "I have been told about the reasonable alternatives, including having no treatment.",
    "I understand that I may withdraw my consent at any time before the treatment begins.",
]

_RESPONSIBILITIES = (
    "- Tell the clinician about every medicine you take, any allergy, and any change in your health.\n"
    "- Follow the aftercare instructions you are given, and attend the follow-up appointments.\n"
    "- Contact the clinic straight away if you have severe pain, bleeding, swelling or fever."
)


TEMPLATES: dict[str, Template] = {}


def _register(t: Template) -> None:
    ConsentContent.model_validate(t.content)  # a template that does not validate fails at import
    TEMPLATES[t.key] = t


_register(
    Template(
        key="general",
        label="General treatment consent",
        clinical_type=None,
        description="A general practice consent for an examination, procedure or treatment.",
        content={
            "title": "Consent to Treatment",
            "intro": (
                "This form records your agreement to the treatment described below. Please read it "
                "carefully and ask the clinician about anything you do not understand before you sign."
            ),
            "patient_fields": ["full_name", "mrn", "dob", "sex", "phone"],
            "sections": [
                _fields(
                    "treatment",
                    "Treatment information",
                    [
                        ("procedure", "Proposed examination, procedure or treatment", True, False),
                        ("diagnosis", "Diagnosis or reason for treatment", True, True),
                    ],
                ),
                _text(
                    "benefits",
                    "Expected benefits",
                    "The clinician has explained what the treatment is intended to achieve and how likely "
                    "it is to help in your case.",
                ),
                _text(
                    "risks",
                    "Risks and side effects",
                    "Every treatment carries some risk. Common risks include discomfort, bruising, "
                    "infection and an allergic reaction. Rarer, serious risks specific to this treatment "
                    "have been discussed with you and are noted below where relevant.",
                ),
                _fields(
                    "specific", "Risks discussed", [("risks_noted", "Specific risks discussed", False, True)]
                ),
                _text(
                    "alternatives",
                    "Alternatives",
                    "Other options, including not having the treatment, have been explained to you along "
                    "with their own benefits and risks.",
                ),
                _text("responsibilities", "Patient responsibilities", _RESPONSIBILITIES),
                _acks("declaration", "Consent declaration", _COMMON_ACKS),
            ],
            "signatures": {"declaration": _DECLARATION, "provider": True, "witness": False},
            "validity_days": 30,
        },
    )
)

_register(
    Template(
        key="dental",
        label="Dental treatment consent",
        clinical_type="dental",
        description="Restorative, surgical and preventive dental treatment, including anaesthesia.",
        content={
            "title": "Dental Treatment Consent",
            "intro": (
                "This form records your consent to the dental treatment described below. Your dentist "
                "will explain the treatment, its risks and the alternatives before you are asked to sign."
            ),
            "patient_fields": ["full_name", "mrn", "dob", "phone"],
            "sections": [
                _fields(
                    "treatment",
                    "Treatment information",
                    [
                        ("procedure", "Proposed treatment", True, False),
                        ("teeth", "Teeth or area involved", True, False),
                        ("diagnosis", "Diagnosis", True, True),
                        ("anaesthesia", "Anaesthesia (local, none, other)", False, False),
                        ("estimate", "Estimated cost and number of visits", False, False),
                    ],
                ),
                _text(
                    "benefits",
                    "Benefits",
                    "The treatment is intended to relieve pain, treat disease, and restore or protect the "
                    "function and appearance of your teeth.",
                ),
                _text(
                    "risks",
                    "Risks",
                    "- Temporary discomfort, sensitivity, swelling or bruising.\n"
                    "- Numbness from local anaesthetic, usually lasting a few hours; rarely longer.\n"
                    "- Infection, or a dry socket after an extraction.\n"
                    "- A filling, crown or root canal treatment may later fail and need to be redone.\n"
                    "- Rarely, damage to neighbouring teeth, restorations or nerves.",
                ),
                _text(
                    "alternatives",
                    "Alternatives",
                    "Depending on your diagnosis, alternatives may include a different restoration, "
                    "extraction instead of saving the tooth, referral to a specialist, or no treatment — "
                    "in which case the condition may get worse.",
                ),
                _text(
                    "responsibilities",
                    "Patient responsibilities",
                    _RESPONSIBILITIES + "\n- Keep up your oral hygiene and regular check-ups.",
                ),
                _acks(
                    "declaration",
                    "Consent declaration",
                    [
                        *_COMMON_ACKS,
                        "I understand that the plan may need to change once treatment begins, "
                        "and that I will be told if it does.",
                    ],
                ),
            ],
            "signatures": {"declaration": _DECLARATION, "provider": True, "witness": False},
            "validity_days": 30,
        },
    )
)

_register(
    Template(
        key="ophthalmology",
        label="Eye procedure consent",
        clinical_type="ophthalmology",
        description="Eye examinations with dilation, laser and minor procedures, and injections.",
        content={
            "title": "Consent for Eye Examination and Treatment",
            "intro": (
                "This form records your consent to the eye examination or procedure described below. "
                "Your clinician will explain what will happen and answer your questions first."
            ),
            "patient_fields": ["full_name", "mrn", "dob", "phone"],
            "sections": [
                _fields(
                    "treatment",
                    "Procedure information",
                    [
                        ("procedure", "Proposed examination or procedure", True, False),
                        ("eye", "Eye (right, left, both)", True, False),
                        ("diagnosis", "Diagnosis", True, True),
                        ("drops", "Drops or anaesthesia to be used", False, False),
                    ],
                ),
                _text(
                    "benefits",
                    "Benefits",
                    "The procedure is intended to diagnose, protect or improve your sight, or to relieve "
                    "symptoms affecting your eyes.",
                ),
                _text(
                    "risks",
                    "Risks",
                    "- Blurred vision and sensitivity to light for several hours after dilating drops.\n"
                    "- Discomfort, redness, watering or a gritty feeling.\n"
                    "- Raised eye pressure, infection or inflammation, which may need further treatment.\n"
                    "- Rarely, a reduction in vision.",
                ),
                _text(
                    "driving",
                    "Driving and travel",
                    "You must not drive until your vision has returned to normal. Please arrange for "
                    "someone to take you home if your pupils are dilated or you have had a procedure.",
                ),
                _text(
                    "alternatives",
                    "Alternatives",
                    "Other options, including monitoring without treatment, have been explained to you.",
                ),
                _text("responsibilities", "Patient responsibilities", _RESPONSIBILITIES),
                _acks("declaration", "Consent declaration", _COMMON_ACKS),
            ],
            "signatures": {"declaration": _DECLARATION, "provider": True, "witness": False},
            "validity_days": 30,
        },
    )
)

_register(
    Template(
        key="orthopaedic",
        label="Orthopaedic procedure consent",
        clinical_type="orthopaedic",
        description="Joint injections, fracture care, minor orthopaedic procedures and physiotherapy.",
        content={
            "title": "Orthopaedic Procedure Consent",
            "intro": (
                "This form records your consent to the orthopaedic procedure described below. Please ask "
                "your clinician about anything that is not clear before signing."
            ),
            "patient_fields": ["full_name", "mrn", "dob", "sex", "phone"],
            "sections": [
                _fields(
                    "treatment",
                    "Procedure information",
                    [
                        ("procedure", "Proposed procedure", True, False),
                        ("site", "Site and side", True, False),
                        ("diagnosis", "Diagnosis", True, True),
                        ("anaesthesia", "Anaesthesia", False, False),
                    ],
                ),
                _text(
                    "benefits",
                    "Benefits",
                    "The procedure is intended to reduce pain, restore movement or stability, and help "
                    "you return to your usual activities.",
                ),
                _text(
                    "risks",
                    "Risks",
                    "- Pain, swelling, bruising and stiffness.\n"
                    "- Infection at the site.\n"
                    "- Blood clots in the leg or lung, particularly after lower-limb procedures.\n"
                    "- Injury to nearby nerves or blood vessels.\n"
                    "- The problem may not fully resolve, or may return.",
                ),
                _text(
                    "alternatives",
                    "Alternatives",
                    "Alternatives may include rest, physiotherapy, bracing, medication or a different "
                    "procedure. Not having treatment is also an option.",
                ),
                _text(
                    "rehabilitation",
                    "Recovery and rehabilitation",
                    "Recovery depends on following the rehabilitation plan. You will be told about weight "
                    "bearing, exercises and when you can return to work, driving and sport.",
                ),
                _text("responsibilities", "Patient responsibilities", _RESPONSIBILITIES),
                _acks("declaration", "Consent declaration", _COMMON_ACKS),
            ],
            "signatures": {"declaration": _DECLARATION, "provider": True, "witness": False},
            "validity_days": 30,
        },
    )
)

_register(
    Template(
        key="dermatology",
        label="Dermatology procedure consent",
        clinical_type="dermatology",
        description="Biopsies, excisions, cryotherapy, laser and cosmetic skin procedures, with photographs.",
        content={
            "title": "Dermatology Procedure Consent",
            "intro": (
                "This form records your consent to the skin procedure described below, and to clinical "
                "photographs being taken for your record."
            ),
            "patient_fields": ["full_name", "mrn", "dob", "phone"],
            "sections": [
                _fields(
                    "treatment",
                    "Procedure information",
                    [
                        ("procedure", "Proposed procedure", True, False),
                        ("site", "Site(s) to be treated", True, False),
                        ("diagnosis", "Diagnosis or reason", True, True),
                        ("sessions", "Number of sessions expected", False, False),
                    ],
                ),
                _text(
                    "benefits",
                    "Benefits",
                    "The procedure is intended to diagnose or treat your skin condition, or to improve "
                    "its appearance.",
                ),
                _text(
                    "risks",
                    "Risks",
                    "- Redness, swelling, crusting or blistering at the treated site.\n"
                    "- Infection, bleeding or a delay in healing.\n"
                    "- Scarring, or a change in skin colour (lighter or darker), which may be permanent.\n"
                    "- The condition may not fully clear, or may come back.",
                ),
                _text(
                    "photographs",
                    "Clinical photographs",
                    "Photographs taken before and after treatment are part of your medical record and are "
                    "kept confidential. They are not used for any other purpose without your separate "
                    "written consent.",
                ),
                _text(
                    "alternatives",
                    "Alternatives",
                    "Alternatives, including creams, other procedures, or no treatment, have been explained.",
                ),
                _text(
                    "responsibilities",
                    "Patient responsibilities",
                    _RESPONSIBILITIES + "\n- Protect the treated area from the sun as advised.",
                ),
                _acks(
                    "declaration",
                    "Consent declaration",
                    [
                        *_COMMON_ACKS,
                        "I agree to clinical photographs being taken for my medical record.",
                    ],
                ),
            ],
            "signatures": {"declaration": _DECLARATION, "provider": True, "witness": False},
            "validity_days": 30,
        },
    )
)


def template(key: str) -> Template | None:
    return TEMPLATES.get(key)


def default_for(clinical_type: str | None) -> Template:
    """The template a clinic of this type starts with — the general one when
    there is no template for its type."""
    for t in TEMPLATES.values():
        if t.clinical_type and t.clinical_type == clinical_type:
            return t
    return TEMPLATES["general"]
