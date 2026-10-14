"""
Точка входа приложения "Ева — desktop-агент".

pywebview обязан работать в главном потоке (особенно на Windows), поэтому
сетевой WebSocket-клиент (ws_client.py) и голосовое прослушивание
(voice.py) запускаются в отдельных фоновых потоках. UI (ui/index.html)
обращается к Python через объект Api ниже.
"""
import threading

import webview

import voice
import ws_client
from config import config


class Api:
    """Методы, доступные из JS как window.pywebview.api.<метод>(...)."""

    def get_status(self) -> dict:
        return {
            "connected": ws_client.is_connected(),
            "configured": config.is_configured(),
            "voice_configured": config.is_voice_configured(),
            "listening": voice.is_listening(),
        }

    def get_log(self) -> list:
        # Возвращаем копию списком — deque сам по себе не сериализуется в JSON
        return list(ws_client.LOG_BUFFER)

    def get_settings(self) -> dict:
        return config.as_dict()

    def save_settings(self, settings: dict) -> dict:
        config.update(**settings)
        return {"ok": True}

    def toggle_listening(self) -> dict:
        if voice.is_listening():
            voice.stop_listening()
        else:
            voice.start_listening()
        return {"listening": voice.is_listening()}

    def send_chat(self, text: str) -> dict:
        ws_client.submit_chat(text)
        return {"ok": True}

    def get_chat_history(self) -> list:
        return ws_client.get_ui_chat()

    def get_brain_status(self) -> dict:
        try:
            from local_brain import is_ollama_ready
            return {"ready": is_ollama_ready()}
        except Exception:
            return {"ready": False}

    def get_notification_settings(self) -> dict:
        import notifications
        return notifications.get_notification_settings()

    def set_notification_settings(self, settings: dict) -> dict:
        import notifications
        return notifications.set_notification_settings(settings)


def main() -> None:
    # Фоновый поток с сетевым циклом — стартует сразу, даже если настройки
    # ещё не заполнены (ws_client сам будет ждать и повторять попытку).
    net_thread = threading.Thread(target=ws_client.start_background_loop, daemon=True)
    net_thread.start()

    # Автозапуск локального мозга (Ollama) — проверяет/запускает сервер и
    # при необходимости скачивает модель в фоне, не блокируя UI.
    brain_thread = threading.Thread(target=ws_client.start_brain_background, daemon=True)
    brain_thread.start()

    # Экран осознаётся по запросу (describe_screen / get_screen_state в
    # ws_client); фонового watcher-потока нет — он грузил CPU постоянными
    # скриншотами. По запросу агент отвечает «что открыто» без слежки.

    # Голос запускается автоматически, если уже настроен (путь к модели
    # Vosk, ключ Groq) — иначе просто ждёт, пока настроят
    # через вкладку «Настройки» и нажмут переключатель там.
    if config.is_voice_configured():
        voice.start_listening()

    api = Api()
    webview.create_window(
        "Ева — desktop-агент",
        "ui/index.html",
        js_api=api,
        width=480,
        height=680,
        min_size=(400, 550),
    )
    webview.start()


if __name__ == "__main__":
    main()
