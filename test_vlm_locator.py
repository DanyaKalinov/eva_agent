"""Тесты чистых функций vlm_locator (без Ollama и без экрана)."""
from vlm_locator import compute_target_size, parse_model_reply, to_screen_coords


def test_размер_кратен_28_и_не_больше_лимита():
    w, h = compute_target_size(2560, 1440)
    assert w % 28 == 0 and h % 28 == 0
    assert max(w, h) <= 1280


def test_маленькая_картинка_не_увеличивается():
    w, h = compute_target_size(560, 280)
    assert (w, h) == (560, 280)


def test_bbox_даёт_центр():
    assert parse_model_reply('{"bbox_2d": [100, 200, 200, 300]}', 1000, 1000) == (150, 250)


def test_json_в_markdown_блоке():
    text = '```json\n{"bbox_2d": [0, 0, 100, 100]}\n```'
    assert parse_model_reply(text, 500, 500) == (50, 50)


def test_не_найдено():
    assert parse_model_reply('{"found": false}', 500, 500) is None


def test_мусор_и_выход_за_границы():
    assert parse_model_reply("не знаю", 500, 500) is None
    assert parse_model_reply('{"x": 9999, "y": 10}', 500, 500) is None


def test_пересчёт_с_учётом_кропа_окна():
    # окно 1000x500 со смещением (100, 50); модель видела картинку 500x250
    assert to_screen_coords(250, 125, (500, 250), (100, 50, 1000, 500)) == (600, 300)
