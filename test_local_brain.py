from unittest.mock import patch
import pytest

import local_brain
from local_brain import (
    _model_in_list,
    _perform_pull,
    build_plan,
    classify_steps,
    ensure_model,
    ensure_ollama,
    ensure_ollama_ready,
    find_ollama_exe,
    parse_plan,
    _system_prompt,
)


def test_build_plan_answer():
    plan = parse_plan('{"kind": "answer", "text": "Привет!"}')
    assert plan == {"kind": "answer", "text": "Привет!"}


def test_system_prompt_has_web_search_example():
    # qwen2.5vl:7b склонна отвечать «Найду...» вместо того, чтобы действовать.
    # В промпте должен быть наглядный пример «найди в яндекс X -> web_search»,
    # иначе модель трактует команду поиска как обычный чат (см. kвquirks).
    prompt = _system_prompt("")
    assert '"engine": "yandex"' in prompt
    assert "web_search" in prompt


def test_system_prompt_teaches_tab_switch_not_reopen():
    # Открытые вкладки переключаются через ui_click, а не открываются заново
    # через open_website — иначе на «открой вторую вкладку» модель лезет в
    # интернет за новой страницей вместо переключения на существующую.
    prompt = _system_prompt("")
    assert '"ui_click"' in prompt
    assert "переключусь на вкладку" in prompt.lower()
    assert "НЕ открывай его заново через open_website" in prompt


def test_parse_plan_local_action():
    plan = parse_plan('{"kind":"actions","steps":[{"action":"open_app","params":{"app":"блокнот"}}]}')
    assert plan["kind"] == "actions"
    assert plan["steps"][0]["action"] == "open_app"


def test_parse_plan_accepts_actions_key():
    # qwen2.5vl кладёт шаги в "actions" вместо "steps" — парсер должен
    # сдвинуть их в steps, иначе действия молча теряются.
    plan = parse_plan('{"kind":"actions","actions":[{"action":"screenshot","params":{}}]}')
    assert plan["kind"] == "actions"
    assert plan.get("steps") == [{"action": "screenshot", "params": {}}]


def test_classify_steps_danger():
    steps = [{"action": "delete_file", "params": {"path": "x"}}]
    assert classify_steps(steps) == "confirm"
    steps2 = [{"action": "open_app", "params": {"app": "блокнот"}}]
    assert classify_steps(steps2) == "actions"


def test_parse_plan_with_markdown_fence():
    plan = parse_plan('```json\n{"kind": "answer", "text": "ok"}\n```')
    assert plan["kind"] == "answer"
    assert plan["text"] == "ok"


def test_parse_plan_bad_json_returns_error():
    plan = parse_plan("not json at all")
    assert plan["kind"] == "error"


def test_parse_plan_invalid_kind_returns_error():
    plan = parse_plan('{"kind": "nonsense", "text": "hi"}')
    assert plan["kind"] == "error"


def test_build_plan_uses_model(monkeypatch):
    fake_raw = '{"kind": "answer", "text": "мозг ответил"}'
    monkeypatch.setattr("local_brain._call_model", lambda t, m: fake_raw)
    plan = build_plan("привет", "")
    assert plan["kind"] == "answer"
    assert plan["text"] == "мозг ответил"


def test_build_plan_model_unavailable(monkeypatch):
    def raising(text, memory_context):
        raise RuntimeError("Ollama down")
    monkeypatch.setattr("local_brain._call_model", raising)
    plan = build_plan("привет", "")
    assert plan["kind"] == "error"
    assert "Ollama" in plan["text"]


def test_find_ollama_exe_uses_shutil_which(monkeypatch):
    monkeypatch.setattr("local_brain.shutil.which", lambda name: "C:/ollama.exe")
    assert find_ollama_exe() == "C:/ollama.exe"


def test_find_ollama_exe_falls_back_to_localappdata(monkeypatch, tmp_path):
    default_path = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    default_path.parent.mkdir(parents=True)
    default_path.write_text("")
    monkeypatch.setattr("local_brain.shutil.which", lambda name: None)
    monkeypatch.setattr("local_brain.os.path.isfile", lambda p: p == str(default_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert find_ollama_exe() == str(default_path)


def test_find_ollama_exe_returns_none(monkeypatch):
    monkeypatch.setattr("local_brain.shutil.which", lambda name: None)
    monkeypatch.setattr("local_brain.os.path.isfile", lambda p: False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert find_ollama_exe() is None


def test_ensure_ollama_already_ready(monkeypatch):
    monkeypatch.setattr("local_brain.is_ollama_ready", lambda: True)
    popen = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: popen.append(a[0]) or object())
    assert ensure_ollama(wait=True, timeout=5) is True
    assert popen == []  # уже работает — запускать не надо


def test_ensure_ollama_starts_serve(monkeypatch):
    states = iter([False, True])  # до старта сервера — не готово, после — готово
    monkeypatch.setattr("local_brain.is_ollama_ready", lambda: next(states))
    monkeypatch.setattr("local_brain.find_ollama_exe", lambda: "C:/ollama.exe")
    popen = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: popen.append(a[0]) or object())
    assert ensure_ollama(wait=True, timeout=5) is True
    assert popen == [["C:/ollama.exe", "serve"]]


def test_ensure_ollama_no_exe_returns_false(monkeypatch):
    monkeypatch.setattr("local_brain.is_ollama_ready", lambda: False)
    monkeypatch.setattr("local_brain.find_ollama_exe", lambda: None)
    popen = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: popen.append(a[0]) or object())
    assert ensure_ollama(wait=True, timeout=5) is False
    assert popen == []  # exe не найден — запускать нечего


def test_ensure_ollama_times_out(monkeypatch):
    monkeypatch.setattr("local_brain.is_ollama_ready", lambda: False)
    monkeypatch.setattr("local_brain.find_ollama_exe", lambda: "C:/ollama.exe")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: object())
    assert ensure_ollama(wait=True, timeout=0) is False


def test_model_in_list_finds_model(monkeypatch):
    monkeypatch.setattr(
        "local_brain.ollama.list",
        lambda: {"models": [{"name": "qwen2.5vl:7b"}]},
    )
    assert _model_in_list("qwen2.5vl:7b") is True


def test_model_in_list_missing_or_offline(monkeypatch):
    monkeypatch.setattr(
        "local_brain.ollama.list", lambda: {"models": [{"name": "llama3.2"}]}
    )
    assert _model_in_list("qwen2.5vl:7b") is False
    monkeypatch.setattr("local_brain.ollama.list", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert _model_in_list("qwen2.5vl:7b") is False


def test_perform_pull_runs_ollama_pull(monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.run", lambda args, **k: calls.append(args))
    _perform_pull("C:/ollama.exe", "qwen2.5vl:7b")
    assert calls == [["C:/ollama.exe", "pull", "qwen2.5vl:7b"]]


def test_perform_pull_clears_flag(monkeypatch):
    monkeypatch.setattr(local_brain, "_PULL_STARTED", True)
    monkeypatch.setattr("subprocess.run", lambda args, **k: (_ for _ in ()).throw(RuntimeError("нет интернета")))
    with pytest.raises(RuntimeError):
        _perform_pull("C:/ollama.exe", "qwen2.5vl:7b")
    assert local_brain._PULL_STARTED is False


def test_ensure_model_present_returns_ok(monkeypatch):
    monkeypatch.setattr("local_brain._model_in_list", lambda model: True)
    ok, note = ensure_model()
    assert ok is True
    assert note == ""


def test_ensure_model_starts_pull_when_missing(monkeypatch):
    monkeypatch.setattr("local_brain._model_in_list", lambda model: False)
    monkeypatch.setattr("local_brain.find_ollama_exe", lambda: "C:/ollama.exe")
    monkeypatch.setattr(local_brain, "_PULL_STARTED", False)
    captured = {}

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=False, **kw):
            captured["target"] = target

            captured["args"] = args

        def start(self):
            pass  # не запускаем реальный поток — он покрыт тестом _perform_pull

    monkeypatch.setattr("threading.Thread", FakeThread)
    ok, note = ensure_model()
    assert ok is False
    assert "скачиван" in note
    assert captured["args"] == ("C:/ollama.exe", "qwen2.5vl:7b")


def test_ensure_model_skips_when_already_pulling(monkeypatch):
    monkeypatch.setattr("local_brain._model_in_list", lambda model: False)
    monkeypatch.setattr(local_brain, "_PULL_STARTED", True)
    calls = []
    monkeypatch.setattr("threading.Thread", lambda *a, **k: object())
    monkeypatch.setattr("subprocess.run", lambda args: calls.append(args))
    ok, note = ensure_model()
    assert ok is False
    assert "уже" in note
    assert calls == []  # второй pull не запущен


def test_ensure_ollama_ready_already_up(monkeypatch):
    monkeypatch.setattr("local_brain.is_ollama_ready", lambda: True)
    ok, msg = ensure_ollama_ready()
    assert ok is True
    assert "готов" in msg.lower()


def test_ensure_ollama_ready_starts_and_waits_for_model(monkeypatch):
    monkeypatch.setattr("local_brain.is_ollama_ready", lambda: False)
    monkeypatch.setattr("local_brain.ensure_ollama", lambda **kw: True)
    monkeypatch.setattr("local_brain.ensure_model", lambda: (True, ""))
    ok, msg = ensure_ollama_ready()
    assert ok is True


def test_ensure_ollama_ready_returns_model_note_when_pulling(monkeypatch):
    monkeypatch.setattr("local_brain.is_ollama_ready", lambda: False)
    monkeypatch.setattr("local_brain.ensure_ollama", lambda **kw: True)
    monkeypatch.setattr("local_brain.ensure_model", lambda: (False, "модель скачивается"))
    ok, msg = ensure_ollama_ready()
    assert ok is False
    assert "модель" in msg


def test_build_plan_generates_ui_do_task(monkeypatch):
    """Глубина UI-контроля: интерфейсная команда планируется через ui_do_task."""
    fake_out = ('{"kind": "actions", "steps": ['
                '{"action": "open_app", "params": {"app": "telegram"}},'
                '{"action": "ui_do_task", "params": {"goal": "отправить сообщение привет Диме"}}]}')
    monkeypatch.setattr("local_brain._call_model", lambda text, mem: fake_out)
    plan = build_plan("напиши Диме в телеграм привет", "")
    actions = [s["action"] for s in plan.get("steps", [])]
    assert "ui_do_task" in actions


# ----- Поток «не сказано, куда написать» -> need_destination -----
def test_parse_plan_accepts_need_destination():
    plan = parse_plan('{"kind": "need_destination", "text": "Куда?", "pending": {"prefix": "напиши", "text": "привет"}}')
    assert plan["kind"] == "need_destination"
    assert plan["pending"]["text"] == "привет"


def test_no_destination_send_detects():
    assert local_brain._no_destination_send("напиши привет") == "привет"
    assert local_brain._no_destination_send("напиши привет (на экране сейчас активно: Блокнот)") == "привет"
    assert local_brain._no_destination_send("отправь сообщение «встретимся в 12»") == "встретимся в 12"


def test_no_destination_send_none_with_dest():
    assert local_brain._no_destination_send("напиши Диме в телеграм: привет") is None
    assert local_brain._no_destination_send("привет") is None


def test_build_plan_overrides_to_need_destination(monkeypatch):
    """Модель ошиблась и спланировала ui_do_task без адресата — детектор
    страхует и переопределяет план в need_destination."""
    def fake_call_model(text, memory_context):
        return '{"kind": "actions", "steps": [{"action": "ui_do_task", "params": {"goal": "отправить сообщение привет"}}]}'

    monkeypatch.setattr("local_brain._call_model", fake_call_model)
    plan = build_plan("напиши привет", "")
    assert plan["kind"] == "need_destination"
    assert plan["pending"]["text"] == "привет"
