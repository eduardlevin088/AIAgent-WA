# Bitrix entities description

Funnel - Сервисный центр - id = 5 - entityTypeId = 2

stageId - name:
C5:NEW - Консультация
C5:UC_SRW3R8 - Заявки бота
C5:UC_KG8OHE - Сервисное обращение
C5:PREPARATION - Передан в сервисный центр
C5:PREPAYMENT_INVOICE - Заказ запчастей
C5:EXECUTING - Чемодан в ремонте
C5:UC_QACO2C - Чемодан в мастерской
C5:UC_0CWJKY - Передан в бутик
C5:FINAL_INVOICE - Готов к выдаче
C5:WON - Успешно
C5:LOSE - Товар отправлен на утилизацию



Client status equivalent

| Колонка битрикс | Статус для клиента |
|-----------------|--------------------|
| Консультация | Принят |
| Заявки бота | Принят |
| Сервисное обращение | Принят |
| Передан в сервисный центр | Диагностика |
| Заказ запчастей | Диагностика |
| Чемодан в ремонте | В работе |
| Чемодан в мастерской | В работе |
| Передан в бутик | Передан в бутик |
| Готов к выдаче | Готов |
| Успешно | Выдан |
| Товар отправлен на утилизацию | Передан на утилизацию |

Client notifications are sent only when a deal reaches a stage strictly further than the furthest stage previously recorded for that repair request. Moving a deal backward, or returning it to the previous furthest stage, updates the local status but does not send the notification again.

## Planned

- Import and export all service-funnel deals from Bitrix, including deals not created by the agent.



Get stage of deal by id:

```python
response = requests.post(f'{BITRIX_WEBHOOK_URL}crm.item.get', json={"entityTypeId":2, "id":id})

stageId = response.json()['result']['item']['stageId']
```
