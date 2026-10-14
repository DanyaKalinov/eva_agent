import asyncio
import json

from ws_client import _handle_chat, start_brain_background


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def test_start_brain_background_logs_ready(monkeypatch):
    logs = []
    monkeypatch.setattr("local_brain.ensure_ollama_ready", lambda: (True, "Локальный мозг готов."))
    monkeypatch.setattr("ws_client._log", lambda msg, level="info": logs.append((msg, level)))
    start_brain_background()
    assert logs, "должен записаться лог"
    assert logs[0][1] == "success"
    assert "готов" in logs[0][0]


def test_start_brain_background_logs_failure(monkeypatch):
    logs = []
    monkeypatch.setattr("local_brain.ensure_ollama_ready", lambda: (False, "не удалось запустить Ollama"))
    monkeypatch.setattr("ws_client._log", lambda msg, level="info": logs.append((msg, level)))
    start_brain_background()
    assert logs[0][1] == "error"
    assert "не удалось" in logs[0][0]


def test_handle_chat_returns_error_when_brain_unavailable(monkeypatch):
    monkeypatch.setattr("ws_client.ensure_ollama_ready", lambda: (False, "модель qwen2.5vl:7b скачивается"))
    ws = FakeWs()
    asyncio.run(_handle_chat(ws, {"message_id": 7, "text": "привет", "origin": "app"}))
    assert ws.sent, "должен отправить ответ"
    payload = json.loads(ws.sent[0])
    assert payload["type"] == "chat_result"
    assert payload["kind"] == "error"
    assert "модель" in payload["text"]


def test_resolve_plan_actions_uses_steps():
    from ws_client import resolve_plan

    plan = {"kind": "actions", "steps": [{"action": "open_app", "params": {"app": "блокнот"}}]}
    msg_type, payload = resolve_plan(plan, 1, "telegram")
    assert msg_type == "execute_local"
    assert payload["steps"][0]["action"] == "open_app"


def test_resolve_plan_empty_steps_returns_error_not_silence():
    from ws_client import resolve_plan

    plan = {"kind": "actions", "steps": []}
    msg_type, payload = resolve_plan(plan, 1, "telegram")
    assert msg_type == "chat_result"
    assert payload["kind"] == "error"
    assert payload["text"]


def test_handle_chat_proceeds_when_brain_ready(monkeypatch):
    monkeypatch.setattr("ws_client.ensure_ollama_ready", lambda: (True, "Локальный мозг готов."))
    monkeypatch.setattr(
        "local_brain._call_model", lambda text, memory_context: '{"kind": "answer", "text": "привет, человек"}'
    )
    ws = FakeWs()
    asyncio.run(_handle_chat(ws, {"message_id": 7, "text": "привет", "origin": "app"}))
    assert ws.sent, "должен отправить ответ"
    payload = json.loads(ws.sent[0])
    assert payload["kind"] == "answer"
    assert payload["message_id"] == 7