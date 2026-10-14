def test_start_screen_watcher_returns_running(monkeypatch):
    from screen_watcher import ScreenWatcher
    w = ScreenWatcher().start()
    assert w._thread is not None and w._thread.is_alive()
    w.stop()


def test_watch_manager_wiring(monkeypatch):
    from watchers import WatchManager
    from screen_watcher import ScreenWatcher
    screen = ScreenWatcher()
    m = WatchManager(screen, notify=lambda t: None)
    screen.on_change(m.on_event)
    # bound method каждый доступ создаёт новый объект — сравнение по `is`
    # всегда False, поэтому по равенству (+ проверка, что это наш обработчик).
    assert m.on_event in screen._callbacks


def test_api_notification_settings_methods_exist():
    from app import Api
    api = Api()
    s = api.get_notification_settings()
    assert "sound_enabled" in s and "toast_enabled" in s
    # запись не должна падать — вернёт смерженный словарь
    res = api.set_notification_settings({"sound_enabled": True})
    assert res["sound_enabled"] is True
