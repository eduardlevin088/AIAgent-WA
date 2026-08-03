import re

import requests
from config import BITRIX_BOT_STAGE_ID, BITRIX_DEAL_ENTITY_TYPE_ID, BITRIX_SERVICE_CATEGORY_ID
from config import BITRIX_WEBHOOK_URL


PHONE_SEGMENT_PATTERN = re.compile(r"^77\d{9}$")


def bitrix_method_url(method: str) -> str:
    if not BITRIX_WEBHOOK_URL:
        raise RuntimeError("BITRIX_WEBHOOK_URL is not configured")
    return f"{BITRIX_WEBHOOK_URL.rstrip('/')}/{method}"


def call_bitrix_method(method: str, payload: dict, timeout: int = 30) -> dict:
    response = requests.post(bitrix_method_url(method), json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(data.get("error_description") or data.get("error"))
    return data


def list_bitrix_deal_categories() -> list[dict]:
    data = call_bitrix_method(
        "crm.category.list",
        {"entityTypeId": BITRIX_DEAL_ENTITY_TYPE_ID},
    )
    categories = (data.get("result") or {}).get("categories") or []
    normalized = [
        {
            "id": int(category["id"]),
            "name": str(category.get("name") or f"Воронка {category['id']}"),
            "sort": int(category.get("sort") or 0),
        }
        for category in categories
        if category.get("id") is not None
    ]
    return sorted(normalized, key=lambda category: (category["sort"], category["id"]))


def list_bitrix_deal_stages(category_id: int) -> list[dict]:
    entity_id = "DEAL_STAGE" if int(category_id) == 0 else f"DEAL_STAGE_{int(category_id)}"
    data = call_bitrix_method(
        "crm.status.list",
        {
            "entityTypeId": BITRIX_DEAL_ENTITY_TYPE_ID,
            "filter": {
                "CATEGORY_ID": int(category_id),
                "ENTITY_ID": entity_id,
            },
        },
    )
    stages = data.get("result") or []
    normalized = [
        {
            "category_id": int(stage.get("CATEGORY_ID") or category_id),
            "status_id": str(stage["STATUS_ID"]),
            "name": str(stage.get("NAME") or stage["STATUS_ID"]),
            "sort": int(stage.get("SORT") or 0),
        }
        for stage in stages
        if stage.get("STATUS_ID")
    ]
    return sorted(normalized, key=lambda stage: (stage["sort"], stage["status_id"]))


def list_bitrix_deal_stages_by_category(category_ids: list[int]) -> dict[int, list[dict]]:
    return {
        int(category_id): list_bitrix_deal_stages(int(category_id))
        for category_id in category_ids
    }


def _extract_contact_ids_from_items(items: list[dict]) -> set[int]:
    contact_ids: set[int] = set()
    for item in items:
        raw_contact_ids = item.get("contactIds")
        if raw_contact_ids is None and item.get("contactId"):
            raw_contact_ids = [item["contactId"]]
        if not raw_contact_ids:
            continue
        for contact_id in raw_contact_ids:
            try:
                contact_ids.add(int(contact_id))
            except (TypeError, ValueError):
                continue
    return contact_ids


def get_bitrix_deal_contact_ids(category_id: int, stage_id: str) -> dict:
    start = 0
    total_deals = 0
    contact_ids: set[int] = set()

    while True:
        data = call_bitrix_method(
            "crm.item.list",
            {
                "entityTypeId": BITRIX_DEAL_ENTITY_TYPE_ID,
                "filter": {
                    "categoryId": int(category_id),
                    "stageId": stage_id,
                },
                "select": ["contactIds"],
                "start": start,
            },
            timeout=60,
        )
        result = data.get("result") or {}
        items = result.get("items") or []
        total_deals = int(result.get("total") or total_deals or len(items))
        contact_ids.update(_extract_contact_ids_from_items(items))

        next_start = result.get("next", data.get("next"))
        if next_start is None:
            break
        start = int(next_start)

    return {
        "deal_count": total_deals,
        "contact_ids": contact_ids,
    }


def _normalize_phone(phone: object) -> str | None:
    if phone is None:
        return None
    if isinstance(phone, dict):
        phone = phone.get("value") or phone.get("VALUE")
    value = str(phone).strip()
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if PHONE_SEGMENT_PATTERN.fullmatch(digits):
        return digits
    return None


def _extract_contact_phones(item: dict) -> list[str]:
    phones: list[str] = []
    raw_phone = item.get("phone") or item.get("PHONE")
    if isinstance(raw_phone, list):
        phones.extend(filter(None, (_normalize_phone(phone) for phone in raw_phone)))
    elif raw_phone:
        phones.append(_normalize_phone(raw_phone))

    fm = item.get("fm") or item.get("FM")
    if isinstance(fm, list):
        for entry in fm:
            if str(entry.get("typeId") or entry.get("TYPE_ID") or "").upper() == "PHONE":
                phones.append(_normalize_phone(entry.get("value") or entry.get("VALUE")))
    elif isinstance(fm, dict):
        for entry in fm.get("PHONE") or []:
            phones.append(_normalize_phone(entry.get("VALUE") or entry.get("value")))

    return sorted({phone for phone in phones if phone})


def get_bitrix_contact_phones(contact_id: int) -> list[str]:
    data = call_bitrix_method(
        "crm.item.get",
        {"entityTypeId": 3, "id": int(contact_id)},
    )
    item = (data.get("result") or {}).get("item") or {}
    return _extract_contact_phones(item)


def build_bitrix_customer_segment(selections: list[dict]) -> dict:
    stage_rows: list[dict] = []
    all_contact_ids: set[int] = set()

    for selection in selections:
        category_id = int(selection["category_id"])
        stage_id = str(selection["stage_id"])
        deal_contacts = get_bitrix_deal_contact_ids(category_id, stage_id)
        contact_ids = deal_contacts["contact_ids"]
        all_contact_ids.update(contact_ids)
        stage_rows.append(
            {
                "category_id": category_id,
                "category_name": selection.get("category_name") or f"Воронка {category_id}",
                "stage_id": stage_id,
                "stage_name": selection.get("stage_name") or stage_id,
                "deal_count": deal_contacts["deal_count"],
                "contact_count": len(contact_ids),
            }
        )

    phones_by_contact: list[dict] = []
    unique_phones: set[str] = set()
    for contact_id in sorted(all_contact_ids):
        phones = get_bitrix_contact_phones(contact_id)
        unique_phones.update(phones)
        phones_by_contact.append(
            {
                "contact_id": contact_id,
                "phones": phones,
            }
        )

    phones = sorted(unique_phones)
    return {
        "stage_rows": stage_rows,
        "phones_by_contact": phones_by_contact,
        "phones": phones,
        "phone_text": "\n".join(phones),
        "total_deals": sum(row["deal_count"] for row in stage_rows),
        "unique_contacts": len(all_contact_ids),
        "unique_phones": len(phones),
    }


def repair_problem_text(data: dict) -> str:
    parts = [data.get("problem") or "Не указано"]
    if data.get("service_type"):
        parts.append(f"Услуга: {data['service_type']}")
    if data.get("brand"):
        parts.append(f"Бренд: {data['brand']}")
    if data.get("article"):
        parts.append(f"Артикул: {data['article']}")
    if data.get("estimated_price_range"):
        parts.append(f"Предварительная стоимость: {data['estimated_price_range']}")
    if data.get("diagnostic_summary"):
        parts.append(f"Первичная диагностика: {data['diagnostic_summary']}")
    if data.get("convenient_time"):
        parts.append(f"Удобное время: {data['convenient_time']}")
    if data.get("warranty_context"):
        parts.append(f"Гарантия: {data['warranty_context']}")
    return "\n".join(parts)


def update_bitrix_repair_request_number(deal_id: int, request_number: int) -> bool:
    try:
        response = requests.post(
            bitrix_method_url("crm.item.update"),
            json={
                "entityTypeId": BITRIX_DEAL_ENTITY_TYPE_ID,
                "id": deal_id,
                "fields": {
                    "TITLE": f"ТЕСТ Заявка на ремонт №{request_number}",
                },
            },
        )
        return response.ok
    except Exception:
        return False


def get_bitrix_deal_stage_id(deal_id: int) -> str | None:
    if not deal_id:
        return None

    try:
        response = requests.post(
            bitrix_method_url("crm.item.get"),
            json={"entityTypeId": BITRIX_DEAL_ENTITY_TYPE_ID, "id": deal_id},
            timeout=15,
        )
        if response.ok:
            item = (response.json().get("result") or {}).get("item") or {}
            stage_id = item.get("stageId") or item.get("STAGE_ID")
            if stage_id:
                return str(stage_id)
    except Exception:
        pass

    try:
        response = requests.post(
            bitrix_method_url("crm.deal.get"),
            json={"id": deal_id},
            timeout=15,
        )
        if response.ok:
            result = response.json().get("result") or {}
            stage_id = result.get("STAGE_ID") or result.get("stageId")
            if stage_id:
                return str(stage_id)
    except Exception:
        return None

    return None


def create_bitrix_lead(data: dict, username: str, bitrix_id: int | None) -> dict:
    try:
        data["model"] = data.get("model") or "Не указана"
        problem_text = repair_problem_text(data)

        if not bitrix_id:
            contact_data = {
                "entityTypeId": 3,
                "fields": {
                    "name": data["name"],
                    "opened":"Y",
                    "fm": [
                        {
                            "valueType": "WORK",
                            "value": data["phone"],
                            "typeId": "PHONE"
                        }
                    ]
                }
            }
            response = requests.post(bitrix_method_url("crm.item.add"), json=contact_data)
            contact_id = response.json()["result"]["item"]["id"]
            
            address_data = {
                "fields": {
                    "TYPE_ID": 1,
                    "ENTITY_TYPE_ID": 3,
                    "ENTITY_ID": contact_id,
                    "CITY": data["city"]
                }
            }
            response = requests.post(bitrix_method_url("crm.address.add"), json=address_data)
            
            deal_data = {
                "entityTypeId": 2,
                "fields": {
                    "TITLE": "ТЕСТ " + "Заявка на ремонт",
                    "categoryId": BITRIX_SERVICE_CATEGORY_ID,
                    "stageId": BITRIX_BOT_STAGE_ID,
                    "opened": "Y",
                    "contactId": contact_id,
                    "sourceId": "AIAgent",
                    "ufCrm_696A02431022F": username,
                    "ufCrm_69E34D81BCB10": data["product_type"],
                    "ufCrm_69E35492B27DA": data["model"],
                    "ufCrm_69E35492D0A18": problem_text
                }
            }
            response = requests.post(bitrix_method_url("crm.item.add"), json=deal_data)
            
            if response.json()["result"]:
                deal_id = response.json()["result"]["item"]["id"]
                return {"message": f"Заявка создана в CRM. Номер заявки: {deal_id}", "bitrix_id": contact_id, "deal_id": deal_id}
            else:
                return {"message": "Failed to create deal", "bitrix_id": contact_id, "deal_id": None}
        
        else:
            deal_data = {
                "entityTypeId": 2,
                "fields": {
                    "TITLE": "ТЕСТ " + "Заявка на ремонт",
                    "categoryId": BITRIX_SERVICE_CATEGORY_ID,
                    "stageId": BITRIX_BOT_STAGE_ID,
                    "opened": "Y",
                    "contactId": bitrix_id,
                    "sourceId": "AIAgent",
                    "ufCrm_696A02431022F": username,
                    "ufCrm_69E34D81BCB10": data["product_type"],
                    "ufCrm_69E35492B27DA": data["model"],
                    "ufCrm_69E35492D0A18": problem_text
                }
            }
            response = requests.post(bitrix_method_url("crm.item.add"), json=deal_data)
            
            if response.json()["result"]:
                deal_id = response.json()["result"]["item"]["id"]
                return {"message": f"Заявка создана в CRM. Номер заявки: {deal_id}", "bitrix_id": None, "deal_id": deal_id}
            else:
                return {"message": "Failed to create lead", "bitrix_id": None, "deal_id": None}

    except Exception as e:
        return {
            "message": f"Error creating lead: {e}",
            "bitrix_id": contact_id if 'contact_id' in locals() else None,
            "deal_id": None
        }


def upload_files_to_bitrix(deal_id: int, files: list[str | dict]) -> str | None:
    try:
        formatted_files = []
        for i, file_data in enumerate(files):
            if isinstance(file_data, dict):
                formatted_files.append([
                    file_data.get("filename") or f"file{i+1}.jpg",
                    file_data["content"]
                ])
            else:
                formatted_files.append([f"photo{i+1}.jpg", file_data])

        files_data = {
            "entityTypeId": BITRIX_DEAL_ENTITY_TYPE_ID,
            "id": deal_id,
            "fields": {
                "ufCrm_69EC6E8F08E09": formatted_files
            }
        }
        response = requests.post(bitrix_method_url("crm.item.update"), json=files_data)
        return True

    except Exception: 
        return False
