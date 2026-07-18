# Samsonite WhatsApp Bot

FastAPI webhook service for a WhatsApp bot connected through Wazzup.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Required environment variables:

```bash
DB_PATH=/Users/levineduard/HYPER/AIAgent-WA/data/database.db
WAZZUP_API_KEY=your_wazzup_api_key
WAZZUP_CHANNEL_ID=your_wazzup_channel_id
WAZZUP_CHAT_LINK_BASE=https://app.wazzup24.ru/your-account/chat/whatsapp/
SUPERADMIN_ID=77000000000
GPT_KEY=...
GPT_MODEL=...
GPT_SPARE_MODEL=...
GPT_TRANSCRIPTION_MODEL=...
LIMIT_PER_USER=...
ABSOLUTE_LIMIT=...
INPUT_PRICE=...
OUTPUT_PRICE=...
KZ_UTC=5
USD_KZT=...
BITRIX_WEBHOOK_URL=...
BITRIX_APPLICATION_TOKEN=copy-from-bitrix-trigger-auth-application-token
BITRIX_SERVICE_CATEGORY_ID=5
BITRIX_DEAL_ENTITY_TYPE_ID=2
BITRIX_BOT_STAGE_ID=C5:UC_SRW3R8
WEBHOOK_DOMAIN=wa.example.com
```

Optional:

```bash
DATA_DIR=./data
MEDIA_DIR=./data/media
INTERNAL_API_KEY=secret-for-/send-endpoint
ADMIN_IDS=77000000000,77111111111
ENABLE_CHAT_ALLOWLIST=1
ALLOWED_CHAT_IDS=77000000000,77111111111
MANAGER_HANDOFF_TIMEOUT_MINUTES=30
MANAGER_HANDOFF_POLL_SECONDS=15
BITRIX_STAGE_STATUS_MAP={"C5:UC_SRW3R8":"Принят","C5:EXECUTING":"В работе","C5:FINAL_INVOICE":"Готов","C5:WON":"Выдан"}
```

`ADMIN_IDS` is only an optional startup seed. Runtime admins are stored in the SQLite `admin` table and can be changed with `/newadmin` and `/deladmin`.

`ENABLE_CHAT_ALLOWLIST=1` makes the bot process inbound Wazzup messages only from `ALLOWED_CHAT_IDS`. Leave it unset/false for production traffic from all customers.

For server test launch, keep the allowlist enabled:

```bash
ENABLE_CHAT_ALLOWLIST=1
ALLOWED_CHAT_IDS=77767114154,77086975789,77076809448,77474334987,77768305757,77071759248,77027055049
```

Inbound messages from any other WhatsApp number are ignored before the bot logs the dialog or sends a reply. New Bitrix repair deals are labeled `ТЕСТ Заявка на ремонт`, then renamed to `ТЕСТ Заявка на ремонт №<номер>` after the local request number is created.

`BITRIX_STAGE_STATUS_MAP` maps Bitrix deal `STAGE_ID` values to local repair statuses. It is optional; default values cover the service center funnel described in `BITRIX.md`.

`BITRIX_APPLICATION_TOKEN` is optional but recommended. Bitrix sends it as `auth[application_token]` in CRM trigger payloads; when set, `/webhook/bitrix` rejects requests with a different token.

Every distinct human outbound Wazzup message pauses the agent and resets the manager handoff timer. After `MANAGER_HANDOFF_TIMEOUT_MINUTES` without another manager message, the handoff closes and the agent processes future customer messages again. The default is 30 minutes; `MANAGER_HANDOFF_POLL_SECONDS` controls how often expired handoffs are checked.

`DB_PATH` must be a full SQLite file path. If unset, the app uses `./data/database.db`.

## Running Locally

```bash
uvicorn bot:app --host 0.0.0.0 --port 8000
```

Webhook URL:

```text
POST /webhook/wazzup
POST /webhook/bitrix
```

Manual send endpoint:

```text
POST /send
```

CRM status endpoint:

```text
POST /crm/status
```

`/send` accepts:

```json
{
  "chatId": "77071759248",
  "text": "Message",
  "channelId": "optional-channel-uuid",
  "chatType": "whatsapp"
}
```

If `INTERNAL_API_KEY` is set, pass it as `X-API-Key`.

`/crm/status` accepts:

```json
{
  "chatId": "77071759248",
  "status": "Диагностика",
  "requestNumber": "12345",
  "text": "Optional custom text"
}
```

The built-in statuses are: `Принят`, `Диагностика`, `В работе`, `Готов`, `Выдан`.

## Runtime Behavior

- Wazzup tester payload `{"test": true}` returns `200 OK`.
- Outgoing Wazzup messages (`isEcho=true`) are used only to record the first manager response after an open handoff. Other echo/status updates are ignored.
- Duplicate `messageId` values are ignored.
- Accepted inbound types: text, image, video, audio.
- One request can store up to 5 image/video files.
- Audio is transcribed before being sent to the AI.
- A greeting-only customer message starts a fresh dialogue and sends the fixed text from `static/greeting.txt` without involving the LLM. Slash commands remain supported for compatibility.
- `/operator` pauses the bot for that client and sends recent dialog history to admins.
- `/resume` or `/start` re-enables the bot.
- Feedback can be sent as `/feedback 5 text` or `оценка 5 text`.
- Superadmin command `/analytics` returns basic counters.

## Persistent Data

The app writes SQLite and received media under `DATA_DIR`.

- Local default: `./data`
- Docker default: `/data`
- Database path: `DB_PATH`, or `${DATA_DIR}/database.db` when `DB_PATH` is unset
- Media path: `${DATA_DIR}/media`

In Docker, mount a host directory to `/data`:

```bash
docker run -d \
  -p 127.0.0.1:8000:8000 \
  -v /var/data/AIAgent-WA:/data \
  --name samsonite-bot-wa \
  --env-file .env \
  samsonite-bot-wa
```

This keeps `database.db` and received files outside the container.

## HTTPS Webhook Domain

Create a DNS record:

```text
Type: A
Name: wa
Value: your_server_ip
```

Set the matching value in the server `.env`:

```bash
WEBHOOK_DOMAIN=wa.example.com
```

The deploy workflow starts a Caddy container named `samsonite-bot-wa-caddy` when `WEBHOOK_DOMAIN` is set. Caddy listens on ports 80/443, obtains a Let's Encrypt certificate, and proxies:

```text
https://wa.example.com/webhook/wazzup -> http://127.0.0.1:8000/webhook/wazzup
https://wa.example.com/webhook/bitrix -> http://127.0.0.1:8000/webhook/bitrix
```

Use this URL in Wazzup:

```text
https://wa.example.com/webhook/wazzup
```

Make sure ports 80 and 443 are open on the server firewall.
