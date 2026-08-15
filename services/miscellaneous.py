from datetime import datetime, time, timezone, timedelta
from config import KZ_UTC
from config import MANAGER_WORKING_DAYS
from config import MANAGER_WORKING_HOURS_ENABLED
from config import MANAGER_WORKING_HOURS_END, MANAGER_WORKING_HOURS_START


def current_time_utc_offset(offset_hours: int = KZ_UTC) -> int:
    tz = timezone(timedelta(hours=offset_hours))
    now = datetime.now(tz)
    return f'{now.hour}:{now.minute}'


def _parse_working_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


_runtime_working_hours = {
    "enabled": MANAGER_WORKING_HOURS_ENABLED,
    "start": MANAGER_WORKING_HOURS_START,
    "end": MANAGER_WORKING_HOURS_END,
    "days": frozenset(MANAGER_WORKING_DAYS),
}


def set_manager_working_hours(
    *,
    enabled: bool,
    start: str,
    end: str,
    days: frozenset[int] | set[int] | list[int],
) -> None:
    cleaned_days = sorted(
        int(day)
        for day in days
        if isinstance(day, int) and 0 <= day <= 6
    )
    if not cleaned_days:
        cleaned_days = sorted(MANAGER_WORKING_DAYS)

    _runtime_working_hours.update({
        "enabled": bool(enabled),
        "start": start,
        "end": end,
        "days": frozenset(cleaned_days),
    })


def get_manager_working_hours() -> dict[str, object]:
    return {
        "enabled": bool(_runtime_working_hours["enabled"]),
        "start": str(_runtime_working_hours["start"]),
        "end": str(_runtime_working_hours["end"]),
        "days": sorted(set(_runtime_working_hours["days"])),
    }


def is_manager_working_time(now: datetime | None = None) -> bool:
    """Return whether manager handoff is currently allowed in Kazakhstan time."""
    if not bool(_runtime_working_hours["enabled"]):
        return True

    tz = timezone(timedelta(hours=KZ_UTC))
    current = (now or datetime.now(tz)).astimezone(tz)
    if current.weekday() not in _runtime_working_hours["days"]:
        return False

    start = _parse_working_time(_runtime_working_hours["start"])
    end = _parse_working_time(_runtime_working_hours["end"])
    current_time = current.time()

    if start == end:
        return True
    if start < end:
        return start <= current_time < end
    return current_time >= start or current_time < end


def manager_working_hours_description() -> str:
    if not bool(_runtime_working_hours["enabled"]):
        return "круглосуточно"
    return (
        f"{_runtime_working_hours['start']}-{_runtime_working_hours['end']} "
        "по времени Казахстана"
    )


def format_repair_text_minimal(d: dict) -> str:
    return f"""
Новая заявка на ремонт Samsonite / American Tourister
Номер заявки: {d.get('request_number') or d.get('deal_id') or 'Не указан'}
Bitrix ID: {d.get('deal_id') or 'Не указан'}

Клиент: {d['name']}
Телефон: {d['phone']}
Город: {d['city']}

Услуга: {d.get('service_type') or 'Не указана'}
Изделие: {d['product_type']}
Бренд: {d.get('brand') or 'Не указан'}
Модель: {d.get('model') or 'Не указана'}
Артикул: {d.get('article') or 'Не указан'}

Проблема: {d['problem']}
Первичная диагностика: {d.get('diagnostic_summary') or 'Не указана'}
Предварительная стоимость: {d.get('estimated_price_range') or 'Не указана'}
Удобное время: {d.get('convenient_time') or 'Не указано'}
"""
