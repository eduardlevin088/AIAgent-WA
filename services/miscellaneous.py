from datetime import datetime, timezone, timedelta
from config import KZ_UTC


def current_time_utc_offset(offset_hours: int = KZ_UTC) -> int:
    tz = timezone(timedelta(hours=offset_hours))
    now = datetime.now(tz)
    return f'{now.hour}:{now.minute}'


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
