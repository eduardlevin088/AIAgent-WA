import requests
from config import BITRIX_BOT_STAGE_ID, BITRIX_DEAL_ENTITY_TYPE_ID, BITRIX_SERVICE_CATEGORY_ID
from config import BITRIX_WEBHOOK_URL


def bitrix_method_url(method: str) -> str:
    if not BITRIX_WEBHOOK_URL:
        raise RuntimeError("BITRIX_WEBHOOK_URL is not configured")
    return f"{BITRIX_WEBHOOK_URL.rstrip('/')}/{method}"


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
