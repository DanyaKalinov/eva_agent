import pytest
from ws_client import resolve_plan, prepare_chat_payload


def test_prepare_chat_answer():
    plan = {"kind": "answer", "text": "Привет!"}
    payload = prepare_chat_payload(41, plan)
    assert payload == {"type": "chat_result", "message_id": 41, "kind": "answer", "text": "Привет!"}


def test_resolve_confirm_marks_voice():
    plan = {"kind": "actions", "steps": [{"action": "delete_file", "params": {"path": "x"}, "description": "удалить"}]}
    msg_type, payload = resolve_plan(plan, message_id=7, origin="voice")
    assert msg_type == "confirm"
    assert payload["local_steps"][0]["action"] == "delete_file"


def test_resolve_answer():
    plan = {"kind": "answer", "text": "Привет!"}
    msg_type, payload = resolve_plan(plan, message_id=1, origin="telegram")
    assert msg_type == "chat_result"
    assert payload["text"] == "Привет!"


def test_resolve_cloud():
    plan = {"kind": "cloud", "cloud": {"action": "memory_search", "params": {"query": "тест"}}}
    msg_type, payload = resolve_plan(plan, message_id=2, origin="telegram")
    assert msg_type == "cloud_action"
    assert payload["action"] == "memory_search"


def test_resolve_safe_actions():
    plan = {"kind": "actions", "steps": [{"action": "open_app", "params": {"app": "блокнот"}}]}
    msg_type, payload = resolve_plan(plan, message_id=3, origin="telegram")
    assert msg_type == "execute_local"


# ----- Цикл взгляда (look_at_screen) -----
import asyncio
import json

from ws_client import _handle_chat


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def test_resolve_look_at_screen_is_safe_local():
    plan = {"kind": "actions", "steps": [{"action": "look_at_screen", "params": {"question": "что открыто?"}}]}
    msg_type, payload = resolve_plan(plan, message_id=7, origin="telegram")
    assert msg_type == "execute_local"


def test_handle_chat_replans_after_look_at_screen(monkeypatch):
    """Если мозг сначала вернул только взгляд — агент смотрит на экран и
    переспрашивает мозг, затем выполняет полученные действия."""
    monkeypatch.setattr("ws_client.ensure_ollama_ready", lambda: (True, "готов"))
    calls = {"n": 0}
    replan_text = {}

    def fake_build_plan(text, memory_context):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "kind": "actions",
                "steps": [{"action": "look_at_screen", "params": {"question": "какие сайты открыты?"}, "description": "посмотрю"}],
            }
        replan_text["t"] = text
        return {"kind": "actions", "steps": [{"action": "open_website", "params": {"url": "https://github.com"}}]}

    monkeypatch.setattr("ws_client.build_plan", fake_build_plan)
    executed = []

    def fake_execute(action, params):
        executed.append(action)
        return ("done", "ок", None)

    monkeypatch.setattr("ws_client.execute_action", fake_execute)
    # Взгляд после Task 9 идёт через кэш describe_screen, а не execute_action.
    monkeypatch.setattr("ws_client.describe_screen", lambda q: "Вижу три вкладки")
    # Мокаем имя, которое реально зовёт _resolve_local_plan: ws_client делает
    # `from actions import list_window_titles` (ссылка по имени, не модуль),
    # поэтому патч actions.list_window_titles до неё не достаёт.
    monkeypatch.setattr("ws_client.list_window_titles", lambda: ["OmniRoute — AI Gateway - Google Chrome"])

    async def scenario():
        ws = FakeWs()
        await _handle_chat(ws, {"message_id": 1, "text": "открой третий сайт", "origin": "app"})
        return ws

    ws = asyncio.run(scenario())
    assert calls["n"] == 2, "должен переспросить мозг после взгляда"
    assert executed == ["open_website"], "взгляд идёт через describe_screen, а не execute_action"
    assert "вкладки" in replan_text["t"], "второй план должен знать, что увидел агент"
    assert "OmniRoute" in replan_text["t"], "второй план должен знать точное имя окна браузера"
    assert ws.sent, "должен отправить результат"
    payload = json.loads(ws.sent[-1])
    assert payload["type"] == "chat_result"


def test_handle_chat_limits_look_loop(monkeypatch):
    """Если мозг упрямо возвращает взгляд — цикл останавливается через 3
    перепланирования, а не зависает навсегда."""
    monkeypatch.setattr("ws_client.ensure_ollama_ready", lambda: (True, "готов"))
    calls = {"n": 0}

    def fake_build_plan(text, memory_context):
        calls["n"] += 1
        return {"kind": "actions", "steps": [{"action": "look_at_screen", "params": {"question": "всё ещё смотрю"}, "description": "посмотрю"}]}

    monkeypatch.setattr("ws_client.build_plan", fake_build_plan)
    # Взгляд идёт через кэш describe_screen — мозг «упрямо» возвращает взгляд,
    # цикл обязан остановиться после 3 перепланирований.
    describe_calls = {"n": 0}

    def fake_describe(question):
        describe_calls["n"] += 1
        return "всё ещё вижу окно"

    monkeypatch.setattr("ws_client.describe_screen", fake_describe)
    # Без моков цикл дёргал живую pywinauto Desktop(backend="uia") (~17с) и
    # живой VLM-вызов в look_at_screen (~6с) — выглядело как зависание.
    monkeypatch.setattr("ws_client.list_window_titles", lambda: ["Блокнот"])
    monkeypatch.setattr("ws_client.execute_action",
                        lambda action, params: ("done", "ок", None))

    async def scenario():
        ws = FakeWs()
        await _handle_chat(ws, {"message_id": 2, "text": "открой третью вкладку", "origin": "app"})
        return ws

    asyncio.run(scenario())
    assert calls["n"] == 4, "1 начальный план + 3 перепланирования (максимум)"


def test_notify_owner_pushes_ui(monkeypatch):
    import ws_client
    pushed = []
    toast_calls = []
    monkeypatch.setattr(ws_client, "push_ui_message", lambda role, t: pushed.append((role, t)))
    monkeypatch.setattr(ws_client, "_loop", None)
    monkeypatch.setattr(ws_client, "_current_ws", None)
    monkeypatch.setattr("notifications.show_toast", lambda title, body: toast_calls.append((title, body)))
    monkeypatch.setattr("notifications.play_notification_sound", lambda: None)
    ws_client.notify_owner("курс обновился")
    assert any("курс" in t for _r, t in pushed)
    assert any("курс" in body for _, body in toast_calls)


def test_handle_reminder_shows_toast_and_sound(monkeypatch):
    import ws_client
    toast_calls = []
    sounds = []
    pushed = []
    monkeypatch.setattr("notifications.show_toast", lambda title, body: toast_calls.append((title, body)))
    monkeypatch.setattr("notifications.play_notification_sound", lambda: sounds.append(1))
    monkeypatch.setattr(ws_client, "push_ui_message", lambda role, t: pushed.append((role, t)))
    monkeypatch.setattr(ws_client, "_speak_if_possible", lambda t: None)
    ws_client._handle_reminder({"text": "Вынести мусор"})
    assert toast_calls == [("⏰ Напоминание", "Вынести мусор")]
    assert sounds == [1]
    assert any("Вынести мусор" in t for _r, t in pushed)


@pytest.mark.asyncio
async def test_look_at_screen_uses_cached_describe(monkeypatch):
    import ws_client

    seen = []

    def fake_describe(question):
        seen.append(question)
        return "Свежая подпись"

    monkeypatch.setattr(ws_client, "describe_screen", fake_describe)
    monkeypatch.setattr(ws_client, "list_window_titles", lambda: ["Блокнот"])

    def fake_replan(text, memory):
        assert "Свежая подпись" in text
        return {"kind": "answer", "text": "готово"}

    monkeypatch.setattr(ws_client, "build_plan", fake_replan)

    plan = {"kind": "actions", "steps": [{"action": "look_at_screen", "params": {"question": "Что?"}}]}
    out = await ws_client._resolve_local_plan(plan, "текст", "")
    assert out["kind"] == "answer"
    assert seen  # describe_screen реально вызван, а не execute_action('look_at_screen')


@pytest.mark.asyncio
async def test_handle_chat_injects_active_window(monkeypatch):
    import ws_client

    seen = {}

    def capture_built(text, memory):
        seen["text"] = text
        return {"kind": "answer", "text": "Привет"}

    monkeypatch.setattr(ws_client, "build_plan", capture_built)
    monkeypatch.setattr(ws_client, "ensure_ollama_ready", lambda: (True, "ок"))
    monkeypatch.setattr("ws_client.get_screen_state",
                        lambda: {"active_window": {"title": "Блокнот"}, "mode": "normal"})
    ws = FakeWs()
    await ws_client._handle_chat(ws, {"message_id": 1, "text": "привет", "origin": "app"})
    assert "Блокнот" in seen["text"]


# ----- Поток двухходовки «куда написать?» (pending_intent) -----
def _patch_env(monkeypatch):
    """Общая заготовка для _handle_chat: мозг готов, экран не трогаем."""
    monkeypatch.setattr("ws_client.ensure_ollama_ready", lambda: (True, "готов"))
    monkeypatch.setattr("ws_client.get_screen_state",
                        lambda: {"active_window": {"title": "Блокнот"}, "mode": "normal"})


def test_resolve_need_destination_is_chat_result():
    plan = {"kind": "need_destination", "text": "Куда написать?"}
    msg_type, payload = resolve_plan(plan, message_id=9, origin="telegram")
    assert msg_type == "chat_result"
    assert payload["kind"] == "need_destination"
    # поле pending НЕ должно уходить в чат — это внутренняя заготовка агента
    assert payload.get("pending") is None


def test_handle_chat_merges_pending_answer(monkeypatch):
    import ws_client
    import pending_intent as pi
    pi.clear()
    pi.store("напиши", "привет")
    _patch_env(monkeypatch)
    seen = {}

    def fake_build_plan(text, memory_context):
        seen["text"] = text
        return {"kind": "answer", "text": "Отправила"}

    monkeypatch.setattr(ws_client, "build_plan", fake_build_plan)
    ws = FakeWs()
    asyncio.run(ws_client._handle_chat(ws, {"message_id": 1, "text": "в телеграм Диме", "origin": "app"}))
    assert seen["text"].startswith("напиши в телеграм Диме: привет")
    assert pi.get() is None, "после слияния pending чистится"


def test_handle_chat_cancels_pending(monkeypatch):
    import ws_client
    import pending_intent as pi
    pi.clear()
    pi.store("напиши", "привет")
    _patch_env(monkeypatch)
    called = {"n": 0}

    def fake_build_plan(text, memory_context):
        called["n"] += 1
        return {"kind": "answer", "text": "не должно планировать"}

    monkeypatch.setattr(ws_client, "build_plan", fake_build_plan)
    ws = FakeWs()
    asyncio.run(ws_client._handle_chat(ws, {"message_id": 1, "text": "отмена", "origin": "telegram"}))
    assert called["n"] == 0, "отмена не планирует заново"
    assert pi.get() is None
    assert json.loads(ws.sent[-1])["text"] == "Отменила."


def test_handle_chat_stores_pending_on_need_destination(monkeypatch):
    import ws_client
    import pending_intent as pi
    pi.clear()
    _patch_env(monkeypatch)

    def fake_build_plan(text, memory_context):
        return {"kind": "need_destination",
                "text": "Куда написать? Например: в телеграм Диме",
                "pending": {"prefix": "напиши", "text": "привет"}}

    monkeypatch.setattr(ws_client, "build_plan", fake_build_plan)
    ws = FakeWs()
    asyncio.run(ws_client._handle_chat(ws, {"message_id": 1, "text": "напиши привет", "origin": "telegram"}))
    got = pi.get()
    assert got is not None and got.text == "привет"
