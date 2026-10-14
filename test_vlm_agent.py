"""Тесты vlm_agent (без Ollama и без экрана)."""
from vlm_agent import build_step_prompt, parse_step_reply


def test_разбор_цели_с_кликом():
    assert parse_step_reply('{"done": false, "target": "кнопка Отправить"}') == {
        "done": False, "target": "кнопка Отправить"}


def test_цель_достигнута():
    assert parse_step_reply('{"done": true}') == {"done": True, "target": None}


def test_json_в_markdown_блоке():
    text = '```json\n{"done": false, "target": "поле поиска"}\n```'
    assert parse_step_reply(text)["target"] == "поле поиска"


def test_мусор_и_пустая_цель_дают_пустой_шаг():
    empty = {"done": False, "target": None}
    assert parse_step_reply("не понимаю") == empty
    assert parse_step_reply('{"done": false, "target": "  "}') == empty
    assert parse_step_reply('{"done": "yes"}') == empty  # только настоящий true считается


def test_промпт_содержит_цель_и_историю():
    prompt = build_step_prompt("открыть настройки", ["шестерёнка", "пункт Общие"])
    assert "открыть настройки" in prompt
    assert "шестерёнка; пункт Общие" in prompt


def test_в_промпт_попадают_только_последние_клики():
    history = [f"шаг{i}" for i in range(10)]
    prompt = build_step_prompt("цель", history)
    assert "шаг9" in prompt and "шаг0" not in prompt
