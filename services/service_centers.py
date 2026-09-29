"""Service-center addresses from static/service_centers.json.

The bot never names addresses from the model's own knowledge: the prompt gets
this list, and the address for the customer's city is sent as a fixed message
once a request is created.
"""
import json

from config import SERVICE_CENTERS_PATH


def load_service_centers() -> dict:
    with open(SERVICE_CENTERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


SERVICE_CENTERS = load_service_centers()


def find_service_center(city: str | None) -> dict | None:
    normalized = (city or "").strip().lower()
    if not normalized:
        return None
    for center in SERVICE_CENTERS["centers"]:
        if any(alias in normalized for alias in center["aliases"]):
            return center
    return None


def service_centers_prompt_text() -> str:
    lines = [f"- {center['city']}: {center['address']}." for center in SERVICE_CENTERS["centers"]]
    lines.append(f"- Телефон: {SERVICE_CENTERS['phone']}.")
    return "\n".join(lines)


def service_center_message(city: str | None) -> str:
    center = find_service_center(city)
    if center:
        return (
            f"Сдать изделие можно в сервисный центр в городе {center['city']}: {center['address']}.\n"
            f"Телефон: {SERVICE_CENTERS['phone']}."
        )
    return (
        "Менеджер сервисного центра подскажет, куда сдать изделие в вашем городе.\n"
        f"Телефон: {SERVICE_CENTERS['phone']}."
    )
