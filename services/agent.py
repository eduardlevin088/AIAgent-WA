from openai import OpenAI
from io import BytesIO
from typing import Callable
from config import GPT_KEY, GPT_MODEL, AGENT_PROMPT_MAIN_PATH
from config import GPT_SPARE_MODEL, GPT_TRANSCRIPTION_MODEL
from .miscellaneous import current_time_utc_offset
from .integrations import create_bitrix_lead, update_bitrix_repair_request_number
from database import create_repair_request, get_bitrix_id, set_bitrix_id
import json
import asyncio

with open(AGENT_PROMPT_MAIN_PATH, "r", encoding="utf-8") as f:
    agent_prompt_main = f.read()

client = OpenAI(api_key=GPT_KEY)


tools = [
    {
        "type": "function",
        "name": "send_contact_details",
        "description": "Send FULLY COLLECTED repair request to manager ONLY after all required fields are known.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "phone": {"type": "string"},
                "city": {"type": "string"},
                "product_type": {"type": "string"},
                "model": {"type": "string"},
                "problem": {"type": "string"},
                "brand": {"type": "string"},
                "service_type": {"type": "string"},
                "article": {"type": "string"},
                "diagnostic_summary": {"type": "string"},
                "estimated_price_range": {"type": "string"},
                "convenient_time": {"type": "string"},
                "warranty_context": {"type": "string"}
            },
            "required": ["name", "phone", "city", "service_type", "product_type", "model", "problem"]
        },
    },
    {
        "type": "function",
        "name": "handoff_to_operator",
        "description": (
            "Immediately hand the chat to a human manager when the customer's question is outside "
            "the repair-service script, cannot be answered only from the provided instructions, "
            "requires current external information, or the customer asks for a person."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["reason", "summary"]
        },
    },
]


def transcribe(voice_buffer: BytesIO) -> str:
    try:
        response = client.audio.transcriptions.create(
            model=GPT_TRANSCRIPTION_MODEL,
            file=voice_buffer
        )
        return response.text
    except Exception:
        return


def send_contact_details(data: dict, username: str, user_id: str) -> tuple[str, dict]:
    bitrix_id = asyncio.run(get_bitrix_id(user_id))
    result = create_bitrix_lead(data, username, bitrix_id)
    
    data["deal_id"] = result["deal_id"]
    request_number = asyncio.run(
        create_repair_request(
            user_id=user_id,
            data=data,
            deal_id=result["deal_id"],
            bitrix_contact_id=result["bitrix_id"] or bitrix_id,
        )
    )
    data["request_number"] = request_number
    if result["deal_id"]:
        update_bitrix_repair_request_number(result["deal_id"], request_number)

    if result["bitrix_id"]:
        asyncio.run(set_bitrix_id(user_id, result["bitrix_id"]))
    return f"Заявка создана в CRM. Номер заявки: {request_number}", data


def generate_response(user_message: str | None,
                      conversation: str,
                      username: str,
                      user_id: str,
                      system_message: str | None = None,
                      exceeded: bool = False,
                      should_continue: Callable[[], bool] | None = None) -> dict:

    data_to_send = None
    handoff = None
    input_tokens = 0
    cache_tokens = 0
    output_tokens = 0
    current_time = current_time_utc_offset()
    response = None

    def can_continue() -> bool:
        return should_continue is None or should_continue()

    def result_payload() -> dict:
        return {
            "response": response.output_text if response else "",
            "data to send": data_to_send,
            "handoff": handoff,
            "input": input_tokens,
            "cache": cache_tokens,
            "output": output_tokens,
            "response_id": response.id if response else None
        }

    instructions = f"{agent_prompt_main}\n\nCurrent time is {current_time}"
    
    agent_input = []
    if user_message:
        agent_input += [{"role": "user", "content": user_message}]
    if system_message:
        agent_input += [{"role": "system", "content": system_message}]
    
    if not agent_input and not system_message:
        agent_input = [{"role": "system", "content": "No data received"}]
        
    model = GPT_MODEL if not exceeded else GPT_SPARE_MODEL

    if not can_continue():
        return result_payload()

    response = client.responses.create(
        model=model,
        tools=tools,
        input=agent_input,
        conversation=conversation,
        instructions=instructions,
    )
    
    usage = response.usage
    input_tokens += usage.input_tokens
    cache_tokens += usage.input_tokens_details.cached_tokens
    output_tokens += usage.output_tokens

    for item in response.output:
        if not can_continue():
            break

        if item.type == "function_call":
            if item.name == "send_contact_details":
                args = json.loads(item.arguments)
                args["model"] = args.get("model") or "Не указана"

                if not can_continue():
                    break

                func_response, data_to_send = send_contact_details(data=args, username=username, user_id=user_id)

                agent_input = [{
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": json.dumps({
                        "func_response": func_response
                    })
                }]

                if not can_continue():
                    break

                response = client.responses.create(
                    model=model,
                    instructions=agent_prompt_main,
                    tools=tools,
                    input=agent_input,
                    conversation=conversation,
                )

                usage = response.usage
                input_tokens += usage.input_tokens
                cache_tokens += usage.input_tokens_details.cached_tokens
                output_tokens += usage.output_tokens
            elif item.name == "handoff_to_operator":
                args = json.loads(item.arguments)
                handoff = {
                    "reason": args.get("reason", "Не указана"),
                    "summary": args.get("summary", "Нет краткого описания")
                }

                agent_input = [{
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": json.dumps({
                        "func_response": "Диалог передан оператору. Клиенту нужно коротко сообщить, что менеджер подключится."
                    }, ensure_ascii=False)
                }]

                if not can_continue():
                    break

                response = client.responses.create(
                    model=model,
                    instructions=agent_prompt_main,
                    tools=tools,
                    input=agent_input,
                    conversation=conversation,
                )

                usage = response.usage
                input_tokens += usage.input_tokens
                cache_tokens += usage.input_tokens_details.cached_tokens
                output_tokens += usage.output_tokens

    return result_payload()
