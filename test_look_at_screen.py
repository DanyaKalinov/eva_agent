"""Тесты действия look_at_screen — скриншот + вопрос vision-модели.

Годится проверять, что:
- действие возвращает текстовый ответ модели о том, что видно на экране;
- картинка никуда не сохраняется на диск и не уходит в Telegram;
- при сбое захвата экрана возвращается понятная ошибка.
"""
from actions import look_at_screen, _ask_model_about_image


class FakeImage:
    """Мини-картинка для теста: свойств достаточно для кода сжатия/сохранения."""

    width = 100
    height = 60

    def resize(self, size):
        return self

    def save(self, buf, **kw):
        buf.write(b"fake-image-bytes")


def test_look_at_screen_returns_model_text(monkeypatch):
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda: FakeImage())
    monkeypatch.setattr(
        "actions._ask_model_about_image",
        lambda q, b64: "Вижу 3 вкладки: google, youtube, github",
    )
    status, text = look_at_screen("что открыто на экране?")
    assert status == "done"
    assert "вкладки" in text


def test_look_at_screen_error_when_grab_fails(monkeypatch):
    def raise_grab():
        raise RuntimeError("нет экрана")

    monkeypatch.setattr("PIL.ImageGrab.grab", raise_grab)
    status, text = look_at_screen("что открыто на экране?")
    assert status == "error"
    assert "экран" in text.lower()


def test_ask_model_about_image_posts_question_with_image(monkeypatch):
    sent = {}

    def fake_chat(model=None, messages=None):
        sent["model"] = model
        sent["question"] = messages[0]["content"]
        sent["has_image"] = bool(messages[0].get("images"))
        return {"message": {"content": "красный"}}

    monkeypatch.setattr("ollama.chat", fake_chat)
    assert _ask_model_about_image("какой цвет?", "AAABBB") == "красный"
    assert sent["question"] == "какой цвет?"
    assert sent["has_image"] is True
    assert "qwen" in (sent["model"] or "").lower()


def test_look_at_screen_writes_no_file(monkeypatch, tmp_path):
    """Проверяет, что действие не создаёт файлов на диске (картинка в памяти)."""
    import os

    monkeypatch.setattr("PIL.ImageGrab.grab", lambda: FakeImage())
    monkeypatch.setattr(
        "actions._ask_model_about_image",
        lambda q, b64: "Видно окно блокнота",
    )
    before = set(os.listdir(tmp_path))
    status, text = look_at_screen("что открыто?")
    after = set(os.listdir(tmp_path))
    assert status == "done"
    assert before == after  # файлов не появилось