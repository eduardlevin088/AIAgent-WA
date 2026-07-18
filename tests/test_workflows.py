import os
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import aiosqlite
from starlette.requests import Request


os.environ.setdefault("GPT_KEY", "test-key")
os.environ.setdefault("GPT_MODEL", "gpt-test")
os.environ.setdefault("GPT_SPARE_MODEL", "gpt-test-spare")
os.environ.setdefault("GPT_TRANSCRIPTION_MODEL", "gpt-test-transcription")
os.environ.setdefault("LIMIT_PER_USER", "100000")
os.environ.setdefault("ABSOLUTE_LIMIT", "200000")
os.environ.setdefault("KZ_UTC", "5")
os.environ.setdefault("WAZZUP_CHANNEL_ID", "test-channel")

import bot
import database
from services.wazzup import WazzupClient


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_db = database.db
        database.db = await aiosqlite.connect(":memory:")
        database.db.row_factory = aiosqlite.Row
        await database.create_tables()

    async def asyncTearDown(self):
        await database.db.close()
        database.db = self.previous_db

    async def create_request(
        self,
        deal_id: int = 42,
        city: str = "Almaty",
        user_id: str = "77000000000",
    ) -> None:
        await database.create_repair_request(
            user_id=user_id,
            data={
                "name": "Test Customer",
                "phone": user_id,
                "city": city,
                "service_type": "Repair",
                "product_type": "Suitcase",
                "model": "Test",
                "problem": "Wheel",
            },
            deal_id=deal_id,
        )

    def admin_get_request(
        self,
        path: str,
        query: dict[str, str],
        session_token: str,
    ) -> Request:
        return Request({
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": urlencode(query).encode("utf-8"),
            "headers": [(
                b"cookie",
                f"{bot.ADMIN_COOKIE_NAME}={session_token}".encode("ascii"),
            )],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        })

    def admin_request(self, form: dict[str, str], session_token: str) -> Request:
        body = urlencode(form).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/admin/applications/1/status",
                "query_string": b"",
                "headers": [
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (
                        b"cookie",
                        f"{bot.ADMIN_COOKIE_NAME}={session_token}".encode("ascii"),
                    ),
                ],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
            },
            receive,
        )

    async def test_only_stage_beyond_furthest_is_advanced(self):
        await self.create_request()

        preparation = await database.sync_repair_request_status_by_deal_id(
            42,
            "Диагностика",
            stage_id="C5:PREPARATION",
        )
        self.assertTrue(preparation["stage_advanced"])
        self.assertTrue(preparation["changed"])

        parts = await database.sync_repair_request_status_by_deal_id(
            42,
            "Диагностика",
            stage_id="C5:PREPAYMENT_INVOICE",
        )
        self.assertTrue(parts["stage_advanced"])
        self.assertFalse(parts["changed"])

        executing = await database.sync_repair_request_status_by_deal_id(
            42,
            "В работе",
            stage_id="C5:EXECUTING",
        )
        self.assertTrue(executing["stage_advanced"])

        moved_back = await database.sync_repair_request_status_by_deal_id(
            42,
            "Диагностика",
            stage_id="C5:PREPARATION",
        )
        self.assertFalse(moved_back["stage_advanced"])
        self.assertTrue(moved_back["changed"])

        returned_to_furthest = await database.sync_repair_request_status_by_deal_id(
            42,
            "В работе",
            stage_id="C5:EXECUTING",
        )
        self.assertFalse(returned_to_furthest["stage_advanced"])
        self.assertTrue(returned_to_furthest["changed"])

        beyond_furthest = await database.sync_repair_request_status_by_deal_id(
            42,
            "Готов",
            stage_id="C5:FINAL_INVOICE",
        )
        self.assertTrue(beyond_furthest["stage_advanced"])

    async def test_legacy_request_initializes_checkpoint_without_notification(self):
        await self.create_request()
        await database.db.execute("""
            UPDATE repair_requests
            SET furthest_bitrix_stage_id = NULL,
                furthest_bitrix_stage_rank = NULL
            WHERE deal_id = 42
        """)
        await database.db.commit()

        result = await database.sync_repair_request_status_by_deal_id(
            42,
            "Готов",
            stage_id="C5:FINAL_INVOICE",
        )

        self.assertFalse(result["stage_advanced"])
        self.assertEqual(result["furthest_stage_id"], "C5:FINAL_INVOICE")

    async def test_backward_stage_never_sends_customer_notification(self):
        fake_wazzup = Mock()
        fake_wazzup.send_text = AsyncMock(return_value={})
        original_wazzup = bot.wazzup
        bot.wazzup = fake_wazzup
        try:
            backward_result = await bot.notify_customer_about_bitrix_status(
                {
                    "application": {
                        "user_id": "77000000000",
                        "request_number": 10500,
                    },
                    "changed": True,
                    "stage_advanced": False,
                    "new_status": "Диагностика",
                },
                stage_id="C5:PREPARATION",
            )
            forward_result = await bot.notify_customer_about_bitrix_status(
                {
                    "application": {
                        "user_id": "77000000000",
                        "request_number": 10500,
                    },
                    "changed": False,
                    "stage_advanced": True,
                    "new_status": "Диагностика",
                },
                stage_id="C5:PREPAYMENT_INVOICE",
            )
        finally:
            bot.wazzup = original_wazzup

        self.assertFalse(backward_result)
        self.assertTrue(forward_result)
        fake_wazzup.send_text.assert_awaited_once()


    async def test_applications_are_read_only_and_header_uses_registered_name(self):
        await self.create_request()
        applications = await database.list_repair_requests()

        html = bot.templates.env.get_template("admin_applications.html").render(
            admin={
                "username": "internal.login",
                "display_name": "Айжан Садыкова",
                "role": "operator",
            },
            admin_sections=bot.ADMIN_SECTIONS,
            active_section="applications",
            applications=applications,
            application_columns=bot.repair_request_columns(applications),
            stats=await database.get_repair_request_stats(),
            statuses=database.REPAIR_REQUEST_STATUSES,
            q="",
        )

        self.assertIn("Айжан Садыкова · operator", html)
        self.assertNotIn("internal.login · operator", html)
        self.assertNotIn("/admin/applications/1/status", html)
        self.assertNotIn('name="status"', html)
        self.assertNotIn(">Обновить</button>", html)

    async def test_statistics_can_group_applications_by_city(self):
        await self.create_request(deal_id=42, city="Алматы", user_id="77000000001")
        await self.create_request(deal_id=43, city="Алматы", user_id="77000000002")
        await self.create_request(deal_id=44, city="Астана", user_id="77000000003")
        await self.create_request(deal_id=45, city="", user_id="77000000004")

        city_groups = await database.get_repair_request_group_stats("city")

        self.assertEqual(
            {row["label"]: row["count"] for row in city_groups},
            {"Алматы": 2, "Астана": 1, "Не указан": 1},
        )

    async def test_statistics_route_preserves_city_grouping_choice(self):
        await self.create_request(city="Алматы")
        await database.upsert_admin_user("operator", "unused-hash", role="operator")
        admin = await database.get_admin_user_by_username("operator")
        session = bot.sign_session(int(admin["id"]), bot.ADMIN_SESSION_SECRET)

        response = await bot.admin_statistics(
            self.admin_get_request(
                "/admin/statistics",
                {"group_by": "city"},
                session,
            )
        )
        html = response.body.decode("utf-8")

        self.assertIn('name="group_by"', html)
        self.assertIn('<option value="city" selected>Город</option>', html)
        self.assertIn("Заявки по городу", html)
        self.assertIn("Алматы", html)

    async def test_templates_are_edited_together_and_used_for_notifications(self):
        await self.create_request()
        await database.upsert_admin_user("operator", "unused-hash", role="operator")
        admin = await database.get_admin_user_by_username("operator")
        session = bot.sign_session(int(admin["id"]), bot.ADMIN_SESSION_SECRET)
        templates = await database.list_notification_templates()
        updated_texts = {
            item["stage_id"]: f"Обновлённый текст: {item['stage_name']}"
            for item in templates
        }

        response = await bot.admin_templates_save(
            self.admin_request(
                {
                    f"template_{stage_id}": text
                    for stage_id, text in updated_texts.items()
                },
                session,
            )
        )
        saved_templates = await database.list_notification_templates()
        saved_html = bot.templates.env.get_template("admin_templates.html").render(
            admin={"username": "operator", "role": "operator"},
            admin_sections=bot.ADMIN_SECTIONS,
            active_section="templates",
            notification_templates=saved_templates,
            saved=True,
            error=None,
        )
        fake_wazzup = Mock()
        fake_wazzup.send_text = AsyncMock(return_value={})
        original_wazzup = bot.wazzup
        bot.wazzup = fake_wazzup
        try:
            sent = await bot.notify_customer_about_bitrix_status(
                {
                    "application": {
                        "user_id": "77000000000",
                        "request_number": 10500,
                    },
                    "stage_advanced": True,
                    "new_status": "Диагностика",
                },
                stage_id="C5:PREPARATION",
            )
        finally:
            bot.wazzup = original_wazzup

        self.assertEqual(response.status_code, 303)
        self.assertTrue(sent)
        self.assertEqual(
            fake_wazzup.send_text.await_args.kwargs["text"],
            updated_texts["C5:PREPARATION"],
        )
        self.assertEqual(saved_html.count("<textarea"), len(templates))
        self.assertEqual(saved_html.count(">Сохранить</button>"), 1)
        self.assertIn("Шаблоны сохранены", saved_html)

    async def test_manager_first_response_is_recorded_once(self):
        handoff = await database.create_operator_handoff(
            "77000000000",
            "Outside script",
            "Customer asks about product availability",
        )
        requested_at = database.parse_utc_timestamp(handoff["requested_at"])
        responded_at = (requested_at + timedelta(seconds=75)).isoformat()

        await database.set_bot_paused("77000000000", True, "Waiting for manager")
        response = await database.record_operator_message(
            user_id="77000000000",
            manager_message_id="manager-message-1",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=responded_at,
        )
        duplicate = await database.record_operator_message(
            user_id="77000000000",
            manager_message_id="manager-message-1",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=responded_at,
        )
        second_message_at = (requested_at + timedelta(seconds=120)).isoformat()
        second_message = await database.record_operator_message(
            user_id="77000000000",
            manager_message_id="manager-message-2",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=second_message_at,
        )
        not_expired = await database.close_expired_operator_handoffs(
            timeout_minutes=1,
            now=requested_at + timedelta(seconds=179),
        )
        expired = await database.close_expired_operator_handoffs(
            timeout_minutes=1,
            now=requested_at + timedelta(seconds=181),
        )
        stats = await database.get_operator_handoff_stats()

        self.assertIsNotNone(response)
        self.assertEqual(response["status"], "active")
        self.assertEqual(response["response_time_seconds"], 75)
        self.assertTrue(response["first_response_recorded"])
        self.assertIsNone(duplicate)
        self.assertEqual(second_message["last_manager_message_at"], second_message_at)
        self.assertFalse(second_message["first_response_recorded"])
        self.assertEqual(not_expired, [])
        self.assertEqual(len(expired), 1)
        self.assertFalse(await database.is_bot_paused("77000000000"))
        self.assertEqual(stats["answered"], 1)
        self.assertEqual(stats["waiting"], 0)
        self.assertEqual(stats["active"], 0)
        self.assertEqual(stats["average_seconds"], 75.0)

    async def test_manager_can_take_over_without_agent_handoff(self):
        message_at = database.utc_now_iso()
        takeover = await database.record_operator_message(
            user_id="77000000000",
            manager_message_id="manager-takeover-1",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=message_at,
        )

        self.assertEqual(takeover["initiated_by"], "manager")
        self.assertEqual(takeover["status"], "active")
        self.assertEqual(takeover["last_manager_message_at"], message_at)
        self.assertIsNone(takeover["response_time_seconds"])

    async def test_manager_webhook_pauses_chat_and_deduplicates_status_updates(self):
        first_message_at = database.parse_utc_timestamp(database.utc_now_iso())
        original_is_allowed_chat = bot.is_allowed_chat
        bot.is_allowed_chat = lambda _: True
        try:
            message = {
                "messageId": "manager-webhook-1",
                "chatId": "77000000000",
                "type": "text",
                "text": "Я подключился",
                "authorId": "crm-user-1",
                "authorName": "Manager",
                "dateTime": first_message_at.isoformat(),
            }
            await bot.process_manager_outbound_message(message)
            await bot.process_manager_outbound_message({
                **message,
                "dateTime": (first_message_at + timedelta(minutes=5)).isoformat(),
            })

            async with database.db.execute("""
                SELECT COUNT(*) AS total, last_manager_message_at
                FROM operator_handoffs
                WHERE user_id = ?
            """, ("77000000000",)) as cursor:
                handoff = await cursor.fetchone()
        finally:
            bot.is_allowed_chat = original_is_allowed_chat

        self.assertTrue(await database.is_bot_paused("77000000000"))
        self.assertEqual(handoff["total"], 1)
        self.assertEqual(handoff["last_manager_message_at"], first_message_at.isoformat())

    async def test_admin_users_and_handoff_recipient_selection(self):
        first_id = await database.create_admin_user(
            username="operator.one",
            password_hash="hash-one",
            display_name="Operator One",
            role="operator",
            whatsapp_id="77000000001",
        )
        second_id = await database.create_admin_user(
            username="operator.two",
            password_hash="hash-two",
            display_name="Operator Two",
            role="operator",
            whatsapp_id="77000000002",
        )

        await database.set_handoff_recipients([second_id])

        users = await database.list_admin_users()
        self.assertEqual([user["username"] for user in users], ["operator.one", "operator.two"])
        self.assertFalse(users[0]["receives_handoffs"])
        self.assertTrue(users[1]["receives_handoffs"])
        self.assertEqual(await database.get_handoff_recipient_ids(), ["77000000002"])

        await database.update_admin_user(
            second_id,
            username="operator.two",
            display_name="Operator Two",
            role="operator",
            whatsapp_id="77000000002",
            is_active=False,
        )
        self.assertEqual(await database.get_handoff_recipient_ids(), [])

    async def test_handoff_notifications_use_selected_users_with_legacy_fallback(self):
        await database.create_admin("legacy-admin")
        operator_id = await database.create_admin_user(
            username="operator",
            password_hash="hash",
            display_name="Selected Operator",
            role="operator",
            whatsapp_id="selected-admin",
        )
        send_text = AsyncMock()

        with unittest.mock.patch.object(bot.wazzup, "send_text", send_text):
            await bot.notify_handoff_recipients("handoff", "channel", "whatsapp")
            await database.set_handoff_recipients([operator_id])
            await bot.notify_handoff_recipients("handoff", "channel", "whatsapp")

        self.assertEqual(
            [call.kwargs["chat_id"] for call in send_text.await_args_list],
            ["legacy-admin", "selected-admin"],
        )

    async def test_only_superadmin_can_manage_users_but_operator_can_change_settings(self):
        await database.upsert_admin_user("owner", "owner-hash", role="superadmin")
        await database.upsert_admin_user("operator", "operator-hash", role="operator")
        owner = await database.get_admin_user_by_username("owner")
        operator = await database.get_admin_user_by_username("operator")
        recipient_id = await database.create_admin_user(
            username="recipient",
            password_hash="recipient-hash",
            display_name="Айжан Садыкова",
            role="operator",
            whatsapp_id="77000000003",
        )
        operator_session = bot.sign_session(int(operator["id"]), bot.ADMIN_SESSION_SECRET)
        request = self.admin_request({}, operator_session)

        settings_response = await bot.admin_settings(request)
        settings_save_response = await bot.admin_settings_handoff_recipients(
            self.admin_request({"recipient_id": str(recipient_id)}, operator_session)
        )

        with self.assertRaises(bot.HTTPException) as users_error:
            await bot.admin_users_page(request)
        with self.assertRaises(bot.HTTPException) as create_error:
            await bot.admin_user_create(
                self.admin_request(
                    {
                        "display_name": "Новый пользователь",
                        "username": "new.user",
                        "password": "password123",
                        "role": "operator",
                        "whatsapp_id": "77000000004",
                    },
                    operator_session,
                )
            )

        self.assertEqual(settings_response.status_code, 200)
        self.assertEqual(settings_save_response.status_code, 303)
        self.assertEqual(await database.get_handoff_recipient_ids(), ["77000000003"])
        self.assertEqual(users_error.exception.status_code, 403)
        self.assertEqual(create_error.exception.status_code, 403)
        self.assertEqual(owner["role"], "superadmin")

    async def test_superadmin_creates_user_with_required_name_role_and_phone(self):
        await database.upsert_admin_user("owner", "owner-hash", role="superadmin")
        owner = await database.get_admin_user_by_username("owner")
        owner_session = bot.sign_session(int(owner["id"]), bot.ADMIN_SESSION_SECRET)

        response = await bot.admin_user_create(
            self.admin_request(
                {
                    "display_name": "Айжан Садыкова",
                    "username": "a.sadykova",
                    "password": "password123",
                    "role": "operator",
                    "whatsapp_id": "+7 (700) 000-00-05",
                },
                owner_session,
            )
        )
        created = await database.get_admin_user_by_username("a.sadykova")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(created["display_name"], "Айжан Садыкова")
        self.assertEqual(created["role"], "operator")
        self.assertEqual(created["whatsapp_id"], "77000000005")

    async def test_superadmin_cannot_create_user_without_name_role_or_phone(self):
        await database.upsert_admin_user("owner", "owner-hash", role="superadmin")
        owner = await database.get_admin_user_by_username("owner")
        owner_session = bot.sign_session(int(owner["id"]), bot.ADMIN_SESSION_SECRET)
        valid_form = {
            "display_name": "Айжан Садыкова",
            "username": "required.fields",
            "password": "password123",
            "role": "operator",
            "whatsapp_id": "77000000005",
        }

        for missing_field in ("display_name", "role", "whatsapp_id"):
            with self.subTest(missing_field=missing_field):
                form = {**valid_form, missing_field: ""}
                response = await bot.admin_user_create(
                    self.admin_request(form, owner_session)
                )
                self.assertEqual(response.status_code, 400)

        self.assertIsNone(
            await database.get_admin_user_by_username(valid_form["username"])
        )

    async def test_handoff_settings_show_only_recipient_names(self):
        html = bot.templates.env.get_template("admin_settings.html").render(
            admin={"username": "owner", "role": "superadmin"},
            admin_sections=bot.ADMIN_SECTIONS,
            active_section="settings",
            users=[{
                "id": 7,
                "username": "internal.login",
                "display_name": "Айжан Садыкова",
                "role": "operator",
                "whatsapp_id": "77000000003",
                "receives_handoffs": True,
            }],
            saved=False,
        )

        self.assertIn('name="recipient_id"', html)
        self.assertIn("Передача диалога оператору", html)
        self.assertIn("Айжан Садыкова", html)
        self.assertNotIn("internal.login", html)
        self.assertNotIn("77000000003", html)
        self.assertNotIn('name="recipient_ids"', html)

    async def test_wazzup_client_registers_api_message_id_before_echo(self):
        class FakeResponse:
            status = 201

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def text(self):
                return '{"messageId":"bot-message-1","chatId":"77000000000"}'

            async def json(self):
                return {
                    "messageId": "bot-message-1",
                    "chatId": "77000000000",
                }

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse()

        recorder = AsyncMock()
        client = WazzupClient(outbound_message_recorder=recorder)
        client._session = FakeSession()
        client.headers = lambda: {}

        result = await client.send_text("77000000000", "Bot reply")

        self.assertEqual(result["messageId"], "bot-message-1")
        recorder.assert_awaited_once_with("bot-message-1", "77000000000")

    def test_wazzup_echo_identifies_manager_outbound_message(self):
        self.assertTrue(bot.is_manager_outbound_message({
            "isEcho": True,
            "status": "delivered",
            "authorName": "Manager",
        }))
        self.assertFalse(bot.is_manager_outbound_message({
            "isEcho": False,
            "status": "inbound",
        }))
        self.assertFalse(bot.is_manager_outbound_message({
            "isEcho": True,
            "status": "error",
        }))


if __name__ == "__main__":
    unittest.main()
