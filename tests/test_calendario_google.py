import json
import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from contextlib import asynccontextmanager

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import httpx
from pydantic import ValidationError

from app.tools import calendario_google as calendar


class CalendarTests(unittest.TestCase):
    def test_no_login_returns_error_without_sending_event(self):
        with patch.object(calendar, "_access_token", return_value=None), \
                patch.object(calendar, "_executar") as mcp, \
                patch.object(calendar, "_criar_via_rest") as rest:
            result = calendar.add_google_event.invoke({
                "title": "Teste", "start_time": "2026-10-30T12:00:00-03:00",
            })
        self.assertEqual(result["status"], "error")
        self.assertIn("autenticar", result["message"])
        mcp.assert_not_called()
        rest.assert_not_called()

    def test_default_duration_and_original_details_reach_google(self):
        with patch.object(calendar, "chamar_google_calendar", return_value={"status": "ok", "id": "g1"}) as call:
            calendar.add_google_event.invoke({
                "title": "Reunião", "start_time": "2026-10-30T12:00:00-03:00",
                "location": "Pátio", "description": "Encontro da turma",
            })
        name, payload = call.call_args.args
        self.assertEqual(name, "create_event")
        self.assertEqual(payload["endTime"], "2026-10-30T13:00:00-03:00")
        self.assertEqual(payload["location"], "Pátio")
        self.assertEqual(payload["description"], "Encontro da turma")

    def test_invalid_times_are_rejected_before_network(self):
        with patch.object(calendar, "chamar_google_calendar") as call:
            for start, end in [
                ("2026-10-30T12:00:00", None),
                ("2026-10-30T12:00:00-03:00", "2026-10-30T11:00:00-03:00"),
                ("invalido", None),
            ]:
                with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                    calendar.add_google_event.invoke({"title": "Teste", "start_time": start, "end_time": end})
        call.assert_not_called()

    def test_confirmed_google_event_becomes_success(self):
        result = calendar._normalizar_evento({
            "id": "g1", "status": "confirmed", "summary": "Teste",
            "start": {"dateTime": "2026-10-30T12:00:00-03:00"}, "htmlLink": "https://example.com/event",
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["id"], "g1")
        self.assertEqual(result["inicio"], "2026-10-30T12:00:00-03:00")

    def test_google_without_id_is_not_success(self):
        self.assertEqual(calendar._normalizar_evento({"status": "ok"})["status"], "error")

    def test_mcp_structured_event_and_tool_error(self):
        event = {"id": "g1", "status": "confirmed"}
        response = SimpleNamespace(structured_content=event, is_error=False)
        self.assertEqual(calendar._resultado_mcp(response), event)
        response.is_error = True
        self.assertEqual(calendar._resultado_mcp(response)["status"], "error")

    def test_mcp_text_json_is_read(self):
        response = SimpleNamespace(structured_content=None, is_error=False,
                                   content=[SimpleNamespace(text=json.dumps({"event": {"id": "g1"}}))])
        result = calendar._normalizar_evento(calendar._resultado_mcp(response))
        self.assertEqual(result["id"], "g1")

    def test_disabled_mcp_uses_rest(self):
        payload = {"summary": "Teste"}
        with patch.object(calendar, "_access_token", return_value="test-token"), \
                patch.object(calendar, "_executar", return_value={"status": "error", "http_status": 403}), \
                patch.object(calendar, "_criar_via_rest", return_value={"status": "ok", "id": "g1"}) as rest:
            result = calendar.chamar_google_calendar("create_event", payload)
        self.assertEqual(result["id"], "g1")
        rest.assert_called_once_with(payload, "test-token")

    def test_mcp_timeout_does_not_repeat_write_via_rest(self):
        with patch.object(calendar, "_access_token", return_value="test-token"), \
                patch.object(calendar, "_executar", side_effect=TimeoutError("sem resposta")), \
                patch.object(calendar, "_criar_via_rest") as rest:
            result = calendar.chamar_google_calendar("create_event", {})
        self.assertEqual(result["status"], "error")
        rest.assert_not_called()

    def test_mcp_generic_error_preserves_http_403(self):
        from mcp.shared.exceptions import MCPError

        client = SimpleNamespace(event_hooks={})

        @asynccontextmanager
        async def fake_client(**kwargs):
            yield client

        @asynccontextmanager
        async def fake_transport(*args, **kwargs):
            yield (None, None, None)

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def initialize(self):
                for hook in client.event_hooks["response"]:
                    await hook(SimpleNamespace(request=SimpleNamespace(method="POST"), status_code=403))
                raise MCPError(-32603, "Server returned an error response")

        with patch("mcp.shared._httpx_utils.create_mcp_http_client", fake_client), \
                patch("mcp.client.streamable_http.streamable_http_client", fake_transport), \
                patch("mcp.ClientSession", return_value=FakeSession()):
            result = asyncio.run(calendar._chamar_via_http("create_event", {}, "test-token"))
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(result["status"], "error")

    def test_rest_keeps_event_times_and_location(self):
        payload = {"summary": "Teste", "startTime": "2026-10-30T12:00:00-03:00",
                   "endTime": "2026-10-30T13:00:00-03:00", "timeZone": "America/Sao_Paulo",
                   "location": "Pátio"}
        response = httpx.Response(200, json={"id": "g1", "status": "confirmed"})
        with patch.object(calendar.httpx, "post", return_value=response) as post:
            result = calendar._criar_via_rest(payload, "test-token")
        self.assertEqual(result["status"], "ok")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["start"]["dateTime"], payload["startTime"])
        self.assertEqual(body["end"]["dateTime"], payload["endTime"])
        self.assertEqual(body["location"], "Pátio")

    def test_expired_token_is_refreshed_and_saved(self):
        creds = MagicMock(expired=True, refresh_token="fake-refresh", valid=True)
        creds.to_json.return_value = '{"token":"fake-new-token"}'
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(calendar, "ARQUIVO_TOKEN", path), \
                    patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds):
                self.assertIs(calendar._carregar_token(), creds)
            self.assertEqual(path.read_text(encoding="utf-8"), creds.to_json.return_value)
        creds.refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
