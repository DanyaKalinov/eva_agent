"""Тесты модуля notifications: тосты, звук, настройки, graceful degradation."""

import pytest

import notifications


class FakeNotification:
    """Подделка Notification из winotify для проверки вызовов."""

    instances = []

    def __init__(self, app_id=None, title=None, msg=None):
        self.app_id = app_id
        self.title = title
        self.msg = msg
        self.show_called = False
        FakeNotification.instances.append(self)

    def show(self):
        self.show_called = True
        return self


@pytest.fixture(autouse=True)
def _clean_settings(tmp_path, monkeypatch):
    """Каждый тест — чистые настройки в tmp_path, без настоящего JSON."""
    monkeypatch.setattr(notifications, "_SETTINGS_PATH", tmp_path / "settings.json")
    yield


def test_show_toast_creates_and_shows(monkeypatch):
    import sys
    import types

    monkeypatch.setattr(
        "notifications.get_notification_settings",
        lambda: {"toast_enabled": True, "sound_enabled": True},
    )
    fake_mod = types.ModuleType("winotify")
    fake_mod.Notification = FakeNotification
    monkeypatch.setitem(sys.modules, "winotify", fake_mod)

    FakeNotification.instances.clear()
    notifications.show_toast("Напоминание", "Вынести мусор")

    assert len(FakeNotification.instances) == 1
    inst = FakeNotification.instances[0]
    assert inst.show_called
    assert inst.title == "Напоминание"
    assert inst.msg == "Вынести мусор"


def test_show_toast_disabled_skips(monkeypatch):
    import sys
    import types

    monkeypatch.setattr(
        "notifications.get_notification_settings",
        lambda: {"toast_enabled": False, "sound_enabled": True},
    )
    fake_mod = types.ModuleType("winotify")

    def boom(*a, **k):
        raise ImportError("не должен вызываться")

    fake_mod.Notification = boom
    monkeypatch.setitem(sys.modules, "winotify", fake_mod)

    FakeNotification.instances.clear()
    # show_toast не должен даже доходить до winotify — вернётся раньше
    notifications.show_toast("t", "b")
    assert FakeNotification.instances == []


def test_show_toast_swallows_errors(monkeypatch, caplog):
    monkeypatch.setattr(
        "notifications.get_notification_settings",
        lambda: {"toast_enabled": True, "sound_enabled": True},
    )
    import sys
    import types

    fake_mod = types.ModuleType("winotify")

    class Broken:
        def __init__(self, **kwargs):
            raise RuntimeError("winotify broken")

    fake_mod.Notification = Broken
    monkeypatch.setitem(sys.modules, "winotify", fake_mod)

    # Не должно бросить исключение
    notifications.show_toast("t", "b")


def test_get_settings_defaults():
    s = notifications.get_notification_settings()
    assert s["toast_enabled"] is True
    assert s["sound_enabled"] is True


def test_set_settings_merges_and_persists():
    result = notifications.set_notification_settings({"sound_enabled": False})
    assert result["sound_enabled"] is False
    assert result["toast_enabled"] is True

    # Перечитываем с диска
    s2 = notifications.get_notification_settings()
    assert s2["sound_enabled"] is False


def test_set_settings_ignores_unknown_keys():
    result = notifications.set_notification_settings({"nonexistent_key": 42})
    assert "nonexistent_key" not in result


def test_play_sound_calls_messagebeep(monkeypatch):
    monkeypatch.setattr(
        "notifications.get_notification_settings",
        lambda: {"toast_enabled": True, "sound_enabled": True},
    )
    import types
    import sys

    winsound = types.ModuleType("winsound")
    winsound.MB_ICONEXCLAMATION = 0x00000030
    calls = []
    winsound.MessageBeep = lambda type_: calls.append(type_)
    monkeypatch.setitem(sys.modules, "winsound", winsound)

    notifications.play_notification_sound()
    assert calls == [0x00000030]


def test_play_sound_disabled_skips(monkeypatch):
    monkeypatch.setattr(
        "notifications.get_notification_settings",
        lambda: {"toast_enabled": True, "sound_enabled": False},
    )
    # module winsound подменяем, но он не должен трогаться
    import types
    import sys

    winsound = types.ModuleType("winsound")
    winsound.MB_ICONEXCLAMATION = 0
    winsound.MessageBeep = lambda type_: pytest.fail("не должен вызываться")
    monkeypatch.setitem(sys.modules, "winsound", winsound)

    notifications.play_notification_sound()


def test_play_sound_swallows_errors(monkeypatch):
    monkeypatch.setattr(
        "notifications.get_notification_settings",
        lambda: {"toast_enabled": True, "sound_enabled": True},
    )
    import types
    import sys

    winsound = types.ModuleType("winsound")
    winsound.MB_ICONEXCLAMATION = 0
    winsound.MessageBeep = lambda type_: (_ for _ in ()).throw(RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "winsound", winsound)

    notifications.play_notification_sound()  # не должно бросить