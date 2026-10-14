"""Тесты проверки результата клика (без Ollama и без реального экрана)."""
from PIL import Image

from vlm_actions import screen_changed


def _картинка(цвет: int = 255) -> Image.Image:
    return Image.new("RGB", (640, 360), (цвет, цвет, цвет))


def test_одинаковые_картинки_не_изменились():
    assert screen_changed(_картинка(), _картинка()) is False


def test_заметное_изменение_засчитывается():
    после = _картинка()
    после.paste((0, 0, 0), (0, 0, 320, 180))  # четверть экрана стала чёрной
    assert screen_changed(_картинка(), после) is True


def test_мелкий_шум_игнорируется():
    после = _картинка()
    после.paste((0, 0, 0), (0, 0, 4, 4))  # крошечная точка (курсор, мерцание)
    assert screen_changed(_картинка(), после) is False
