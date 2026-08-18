from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WarrantyAssessment:
    is_likely_warranty: bool | None
    reason: str
    required_documents: list[str]
    conclusion: str


_NON_WARRANTY_KEYWORDS = [
    "механ",
    "удар",
    "пад",
    "перегруз",
    "перевоз",
    "износ",
    "порез",
    "прокол",
    "разрыв",
    "хим",
    "влага",
    "температ",
    "самостоятель",
    "чуж",
    "треть",
]


_GUARANTEE_REQUIRED_DOCS_KEYWORDS = [
    "чек",
    "квитанц",
    "талон",
    "чек-касс",
    "гарантийный талон",
]


def _text_norm(value: str) -> str:
    return (value or "").lower()


def assess_warranty_from_message(message: str) -> WarrantyAssessment:
    normalized = _text_norm(message)
    if "гарант" not in normalized:
        return WarrantyAssessment(
            is_likely_warranty=None,
            reason="",
            required_documents=[],
            conclusion="",
        )

    found_non_warranty = [
        keyword
        for keyword in _NON_WARRANTY_KEYWORDS
        if keyword in normalized
    ]

    required_documents: list[str] = [
        "чек/документ о покупке",
        "гарантийный талон",
    ]

    if found_non_warranty:
        reasons = ", ".join(found_non_warranty)
        return WarrantyAssessment(
            is_likely_warranty=False,
            reason=(
                "В описании есть признаки, которые по правилам не покрываются гарантией: "
                f"{reasons}."
            ),
            required_documents=required_documents,
            conclusion=(
                "Предварительная оценка: вероятно, это не гарантийный случай. "
                "После диагностики менеджер может подтвердить отказ или оплатный характер ремонта."
            ),
        )

    docs_present = any(keyword in normalized for keyword in _GUARANTEE_REQUIRED_DOCS_KEYWORDS)
    conclusion = (
        "По описанию явных исключений не видно. "
        "Предварительно можно считать случай потенциально гарантийным, "
        "если есть покупной документ и/или гарантийный талон. "
        "Окончательно решение — после диагностики в сервисном центре."
    )
    if not docs_present:
        conclusion += (
            " Требуются документы для подтверждения: чек/документ о покупке и гарантийный талон, "
            "иначе возможен платный ремонт."
        )
        "иначе возможен платный ремонт."

    return WarrantyAssessment(
        is_likely_warranty=True,
        reason="Явные основания для отказа по гарантии не указаны.",
        required_documents=required_documents,
        conclusion=conclusion,
    )


def warranty_assessment_message(message: str) -> str | None:
    assessment = assess_warranty_from_message(message)
    if assessment.is_likely_warranty is None:
        return None

    docs = ", ".join(assessment.required_documents)
    return (
        "Оценка по гарантии (предварительная, до диагностики):\n"
        f"{assessment.conclusion}\n"
        f"Основание: {assessment.reason}\n"
        f"Документы, которые нужно уточнить: {docs}."
    )
