from openai import OpenAI
from io import BytesIO
from typing import Callable
from config import GPT_KEY, GPT_MODEL, AGENT_PROMPT_MAIN_PATH, WARRANTY_RULES_PATH
from config import GPT_SPARE_MODEL, GPT_TRANSCRIPTION_MODEL
from .miscellaneous import current_time_utc_offset, is_manager_working_time
from .integrations import create_bitrix_lead, find_bitrix_client_deals
from .integrations import update_bitrix_repair_request_number
from database import create_repair_request, get_bitrix_id, set_bitrix_id, run_coro_on_db_loop
from database import get_request_numbers_by_deal_ids
import json
import logging

with open(AGENT_PROMPT_MAIN_PATH, "r", encoding="utf-8") as f:
    agent_prompt_main = f.read()

with open(WARRANTY_RULES_PATH, "r", encoding="utf-8") as f:
    WARRANTY_RULES_TEXT = f.read().strip()

agent_instructions = (
    f"{agent_prompt_main}\n\nПравила гарантийного блока:\n{WARRANTY_RULES_TEXT}"
)

logger = logging.getLogger(__name__)

client = OpenAI(api_key=GPT_KEY)

# Only the newest applications go back to the model, to keep the context short.
MAX_CLIENT_APPLICATIONS = 10

# Hard cap on chained tool rounds per turn. The final round is sent without
# tools so the model cannot emit yet another function_call we would have to
# answer -- every call is always answered before we return.
MAX_TOOL_ROUNDS = 4


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
        "name": "get_client_applications",
        "description": (
            "Find the customer's existing repair applications in the CRM by phone number: "
            "status, creation date and problem description of each. Use it when the customer "
            "asks about the status of an order or mentions an earlier application."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": (
                        "Phone number strictly in the form +7XXXXXXXXXX, e.g. +77071234567. "
                        "Pass it only when the customer named a number other than the one they "
                        "are writing from; omit it to search by their WhatsApp number."
                    ),
                },
            },
            "required": [],
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
                "reason": {
                    "type": "string",
                    "description": "Short category of the handoff, e.g. 'наличие запчасти', 'претензия', 'клиент просит человека'.",
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "1-2 sentences of concrete context the manager needs: what the customer "
                        "wants, which device and problem it concerns. No generic wording."
                    ),
                },
                "client_question": {
                    "type": "string",
                    "description": (
                        "The customer's actual question or demand, quoted or closely paraphrased "
                        "in their own words."
                    ),
                },
                "requested_action": {
                    "type": "string",
                    "description": "What exactly the manager has to do or find out to close this question.",
                },
                "bot_already_did": {
                    "type": "string",
                    "description": (
                        "What has already been told or done in the chat, so the manager does not "
                        "repeat it. Empty string if nothing relevant."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": "Set true only when the customer explicitly insists on a manager outside working hours.",
                    "default": False,
                },
            },
            "required": ["reason", "summary", "client_question", "requested_action"]
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
    bitrix_id = run_coro_on_db_loop(get_bitrix_id(user_id))
    result = create_bitrix_lead(data, username, bitrix_id)

    data["deal_id"] = result["deal_id"]
    request_number = run_coro_on_db_loop(
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
        run_coro_on_db_loop(set_bitrix_id(user_id, result["bitrix_id"]))
    return f"Заявка создана в CRM. Номер заявки: {request_number}", data


def get_client_applications(phone: str) -> str:
    try:
        deals = find_bitrix_client_deals(phone)
    except ValueError:
        return (
            f"Номер {phone or '(пусто)'} не в формате +7XXXXXXXXXX. "
            "Уточни у клиента номер телефона и повтори поиск."
        )
    if not deals:
        return f"Заявки по номеру {phone} не найдены."

    deals = deals[:MAX_CLIENT_APPLICATIONS]
    try:
        request_numbers = run_coro_on_db_loop(
            get_request_numbers_by_deal_ids([deal["deal_id"] for deal in deals])
        )
    except Exception:
        logger.exception("Failed to load request numbers for client deals")
        request_numbers = {}

    applications = [
        {
            "request_number": request_numbers.get(deal["deal_id"]),
            "status": deal["status"],
            "created": deal["created"],
            "description": deal["description"],
        }
        for deal in deals
    ]
    return json.dumps(
        {"phone": phone, "applications": applications}, ensure_ascii=False
    )


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

    instructions = f"{agent_instructions}\n\nCurrent time is {current_time}"
    
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

    def run_function_call(item) -> str:
        nonlocal data_to_send, handoff

        if not can_continue():
            return "Запрос отменён: клиент отправил новое сообщение."

        try:
            if item.name == "send_contact_details":
                args = json.loads(item.arguments)
                args["model"] = args.get("model") or "Не указана"
                func_response, data_to_send = send_contact_details(
                    data=args, username=username, user_id=user_id
                )
                return func_response

            if item.name == "get_client_applications":
                args = json.loads(item.arguments)
                phone = str(args.get("phone") or "").strip() or f"+{user_id.lstrip('+')}"
                return get_client_applications(phone)

            if item.name == "handoff_to_operator":
                args = json.loads(item.arguments)
                force = args.get("force") is True
                if force or is_manager_working_time():
                    handoff = {
                        "reason": args.get("reason", "Не указана"),
                        "summary": args.get("summary", "Нет краткого описания"),
                        "client_question": args.get("client_question"),
                        "requested_action": args.get("requested_action"),
                        "bot_already_did": args.get("bot_already_did"),
                    }
                    return (
                        "Диалог передан оператору. Клиенту нужно коротко сообщить, "
                        "что менеджер подключится."
                    )
                return (
                    "Сейчас менеджеры находятся вне рабочего времени. Передача не выполнена. "
                    "Сообщи клиенту, что менеджер ответит в рабочее время. "
                    "Если клиент явно настаивает на разговоре с менеджером, повторно вызови "
                    "handoff_to_operator с force=true."
                )

            logger.error("Unknown function call requested by the model: %s", item.name)
            return f"Неизвестная функция {item.name}. Ответь клиенту текстом."
        except Exception as error:
            # Never let a tool failure escape: the function_call is already
            # recorded in the stateful conversation, so it MUST get an output
            # or every future request on this conversation returns 400
            # "No tool output found for function call ...".
            logger.exception("Function call %s failed", item.name)
            return (
                f"Внутренняя ошибка при выполнении {item.name}: {error}. "
                "Извинись перед клиентом и предложи повторить чуть позже "
                "или передай диалог оператору."
            )

    # The OpenAI-side `conversation` is stateful: once a function_call is in
    # response.output it is already recorded there, and every future request on
    # this conversation is rejected with "No tool output found for function
    # call ..." until it gets an output. So every function_call -- including
    # ones emitted by the follow-up responses below -- must be answered before
    # this function returns, even when should_continue() went false mid-turn.
    for round_index in range(MAX_TOOL_ROUNDS):
        function_call_items = [item for item in response.output if item.type == "function_call"]
        if not function_call_items:
            break

        agent_input = [
            {
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": json.dumps(
                    {"func_response": run_function_call(item)}, ensure_ascii=False
                ),
            }
            for item in function_call_items
        ]

        is_last_round = round_index == MAX_TOOL_ROUNDS - 1
        if is_last_round:
            logger.warning(
                "Tool round limit reached for user %s; forcing a text-only reply", user_id
            )

        response = client.responses.create(
            model=model,
            instructions=agent_instructions,
            tools=[] if is_last_round else tools,
            input=agent_input,
            conversation=conversation,
        )

        usage = response.usage
        input_tokens += usage.input_tokens
        cache_tokens += usage.input_tokens_details.cached_tokens
        output_tokens += usage.output_tokens

    return result_payload()
