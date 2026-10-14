"""Уведомления десктопного агента: Windows-тосты и звуковые сигналы.

show_toast() — системное уведомление в центре уведомлений Windows.
play_notification_sound() — короткий бип (system sound).
get/set_notification_settings() — хранение настроек (вкл/выкл) в JSON.
"""

import json
import logging
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

# --- Настройки (хранилище) ---

_SETTINGS_PATH = Path(__file__).parent / "notification_settings.json"
_DEFAULTS = {"toast_enabled": True, "sound_enabled": True}
_lock = threading.Lock()


def get_notification_settings() -> dict:
    """Возвращает текущие настройки уведомлений."""
    if _SETTINGS_PATH.exists():
        try:
            return {**_DEFAULTS, **json.loads(_SETTINGS_PATH.read_text("utf-8"))}
        except Exception:
            pass
    return dict(_DEFAULTS)


def set_notification_settings(settings: dict) -> dict:
    """Обновляет настройки уведомлений. Принимает частичный dict."""
    current = get_notification_settings()
    current.update({k: v for k, v in settings.items() if k in _DEFAULTS})
    with _lock:
        _SETTINGS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), "utf-8")
    return current


# --- Windows Toast ---

def show_toast(title: str, body: str) -> None:
    """Показать системное уведомление Windows (toast).

    Тихо проглатывает ошибки — уведомление не должно крашить агента.
    """
    settings = get_notification_settings()
    if not settings.get("toast_enabled", True):
        return
    try:
        from winotify import Notification

        toast = Notification(
            app_id="Ева — AI-ассистент",
            title=title,
            msg=body,
        )
        toast.show()
    except Exception as e:
        _log.warning("Не удалось показать toast: %s", e)


# --- Звук ---

def play_notification_sound() -> None:
    """Воспроизвести короткий системный звук уведомления.

    Использует winsound (Windows). Тихо проглатывает ошибки.
    """
    settings = get_notification_settings()
    if not settings.get("sound_enabled", True):
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception as e:
        _log.warning("Не удалось воспроизвести звук: %s", e)
