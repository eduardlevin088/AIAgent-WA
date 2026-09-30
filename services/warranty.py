from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WarrantyAssessment:
    is_warranty_question: bool
    exclusions: list[str]
    wear_parts: list[str]
    conclusion: str


_NON_WARRANTY_KEYWORDS = [
    "механ",
    "удар",
    "пад",
    "перегруз",
    "перевоз",
    "аэропорт",
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


# Hardware that wears out in normal use; its failure is usually not a
# manufacturing defect.
_WEAR_PART_KEYWORDS = [
    "колес",
    "колёс",
    "ручк",
    "молни",
    "замок",
    "замк",
    "бегун",
    "собачк",
]


_REQUIRED_DOCUMENTS = "чек или другой документ о покупке и гарантийный талон"


def _text_norm(value: str) -> str:
    return (value or "").lower()


def assess_warranty_from_message(message: str) -> WarrantyAssessment:
    normalized = _text_norm(message)
    if "гарант" not in normalized:
        return WarrantyAssessment(
            is_warranty_question=False,
            exclusions=[],
            wear_parts=[],
            conclusion="",
        )

    exclusions = [keyword for keyword in _NON_WARRANTY_KEYWORDS if keyword in normalized]
    wear_parts = [keyword for keyword in _WEAR_PART_KEYWORDS if keyword in normalized]

    if exclusions:
        conclusion = (
            "В описании есть признаки, которые гарантия не покрывает "
            f"({', '.join(exclusions)}). Предварительно: скорее всего, это не гарантийный случай."
        )
    elif wear_parts:
        conclusion = (
            "Речь об изнашиваемой фурнитуре (колёса, ручки, молнии, замки). "
            "Её поломка обычно не является гарантийным случаем."
        )
    else:
        conclusion = (
            "Гарантия распространяется только на производственные дефекты. "
            "Является ли случай гарантийным, по переписке определить нельзя."
        )

    return WarrantyAssessment(
        is_warranty_question=True,
        exclusions=exclusions,
        wear_parts=wear_parts,
        conclusion=conclusion,
    )


def warranty_assessment_message(message: str) -> str | None:
    assessment = assess_warranty_from_message(message)
    if not assessment.is_warranty_question:
        return None

    return (
        "Клиент спрашивает о гарантии.\n"
        f"{assessment.conclusion}\n"
        "Не говори, что случай может быть или будет признан гарантийным, и не обещай "
        "бесплатный ремонт — даже если у клиента есть чек и гарантийный талон. "
        "Наличие документов не делает случай гарантийным.\n"
        "Решение о гарантии принимает только сервисный центр после диагностики. "
        f"Для обращения по гарантии понадобятся: {_REQUIRED_DOCUMENTS}."
    )
