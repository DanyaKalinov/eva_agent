"""Тесты окна/кликов: подбор окна по названию и клик по вкладке.

Покрывают восстановление из двух реальных сбоев:
- переплан модели назвал окно так, как его нет («браузер», выдуманное имя) —
  подбор должен уметь браузер-окна и близкие по словам окна;
- клик по вкладке не тот, что кнопка — в Chrome вкладки это TabItem,
  а не Button, поэтому ui_click должен сначала пробовать кнопку,
  затем вкладку.
"""
import actions
from actions import _pick_window, _score_window, ui_click, ui_type_text, _find_app_exe


# ----- Подбор окна (_pick_window) -----

def test_pick_window_exact_substring_wins():
    candidates = [
        ("OmniRoute — AI Gateway - Google Chrome", "Chrome_WidgetWin_1"),
        ("Блокнот", "Notepad"),
    ]
    assert _pick_window(candidates, "Google Chrome") == "OmniRoute — AI Gateway - Google Chrome"


def test_pick_window_browser_hint_finds_browser_class():
    """Переплан про «браузер»: точной подстроки нет, но есть Chrome — выбираем."""

    candidates = [
        ("OmniRoute — AI Gateway - Google Chrome", "Chrome_WidgetWin_1"),
        ("Документы — Word", "OpusApp"),
    ]
    assert _pick_window(candidates, "браузер") == "OmniRoute — AI Gateway - Google Chrome"


def test_pick_window_fuzzy_by_common_words():
    """Нет точной подстроки — берём окно с максимальным пересечением слов."""

    candidates = [
        ("Настройки — документы", "Editor"),
        ("Отчёт по проекту", "Notepad"),
    ]
    assert _pick_window(candidates, "проект отчёт") == "Отчёт по проекту"


def test_pick_window_no_match_returns_none():
    candidates = [("Блокнот", "Notepad"), ("Калькулятор", "Calc")]
    assert _pick_window(candidates, "вебмани форум по вкладкам") is None


def test_score_window_counts_shared_words():
    assert _score_window("открой второй вкладку", "вкладка про браузеры") == 1
    assert _score_window("google", "Sports.ru — Google Chrome") == 1


# ----- ui_click: клик по вкладке, если кнопки нет -----

class FakeControl:
    def __init__(self, ctype, text, clickable=True):
        self._type = ctype
        self._text = text
        self.clickable = clickable
        self.clicked = False

    def friendly_class_name(self):
        return self._type

    def window_text(self):
        return self._text

    def click_input(self):
        self.clicked = True


class FakeWin:
    def __init__(self, controls):
        self.controls = controls
        self.focus_called = False

    def descendants(self):
        return self.controls

    def set_focus(self):
        self.focus_called = True


def test_ui_click_clicks_tab_when_button_missing(monkeypatch):
    """Верно для UI Automation: вкладки браузера это TabItem, а не Button.
    ui_click должен найти кнопку, не найти её, затем кликнуть по вкладке."""
    win = FakeWin([
        FakeControl("TabControl", ""),
        FakeControl("TabItem", 'Вкладка "фильм железный человек 3"'),
    ])
    monkeypatch.setattr("actions._find_window", lambda hint: win)

    status, text = ui_click("Chrome", "фильм железный человек")
    assert status == "done"
    assert win.focus_called
    assert win.controls[-1].clicked, "должен кликнуть по вкладке, если кнопки нет"


def test_ui_click_prefers_button_over_tab(monkeypatch):
    win = FakeWin([
        FakeControl("Button", "Отправить"),
        FakeControl("TabItem", "Отправить"),
    ])
    monkeypatch.setattr("actions._find_window", lambda hint: win)

    status, text = ui_click("Chrome", "отправить")
    assert status == "done"
    assert win.controls[0].clicked, "должен предпочесть кнопку вкладке"


# ----- ui_type_text: ввод текста в поле (latent bug: child_window на UIAWrapper) -----

class FakeInput:
    def __init__(self):
        self._text = ""

    def friendly_class_name(self):
        return "Edit"

    def window_text(self):
        return "Поиск"

    def set_text(self, t):
        self._text = t


def test_ui_type_text_finds_field_by_descendants(monkeypatch):
    """ui_type_text использует descendants() — UIAWrapper не имеет child_window()."""
    fake_input = FakeInput()
    win = FakeWin([fake_input])
    monkeypatch.setattr("actions._find_window", lambda h: win)

    status, text = ui_type_text("Блокнот", "Поиск", "тест123")
    assert status == "done"
    assert fake_input._text == "тест123"


def test_ui_type_text_error_when_no_field(monkeypatch):
    monkeypatch.setattr("actions._find_window", lambda h: FakeWin([]))
    status, text = ui_type_text("Блокнот", "НесуществующееПоле", "текст")
    assert status == "error"
    assert "Не нашёл поле" in text


# ----- Глубина UI-контроля: ui_do_task и тонкие клики (spec 2.3) -----

# ----- open_app: фолбэк на известный путь установки (Telegram без ярлыка) -----

def test_find_app_exe_from_known_install_path(tmp_path, monkeypatch):
    """exe по стандартному пути установки (Telegram Desktop), нет ярлыка."""
    exe_path = tmp_path / "Telegram Desktop" / "Telegram.exe"
    exe_path.parent.mkdir()
    exe_path.write_text("x")
    monkeypatch.setattr(actions, "_BROWSER_SEARCH_ROOTS", [str(tmp_path)])
    p = _find_app_exe("telegram")
    assert p is not None and p.name == "Telegram.exe"


def test_find_app_exe_none_for_unknown():
    assert _find_app_exe("что-то-неизвестное") is None


def test_open_app_falls_back_to_install_path(monkeypatch, tmp_path):
    """Итог живого прогона 2026-09-16: «telegram» НЕ нашёлся ни алиасом, ни
    ярлыком — уходит в проверку пути установки вместо ошибки."""
    exe = tmp_path / "Telegram Desktop" / "Telegram.exe"
    exe.parent.mkdir()
    exe.write_text("x")
    started = []
    monkeypatch.setattr(actions, "_load_learned_aliases", lambda: {})
    monkeypatch.setattr(actions, "_find_shortcut", lambda name: None)
    monkeypatch.setattr(actions, "_BROWSER_SEARCH_ROOTS", [str(tmp_path)])
    monkeypatch.setattr(actions, "_remember_app_alias", lambda n, p: None)
    monkeypatch.setattr("os.startfile", lambda p: started.append(str(p)))
    status, text = actions.open_app("telegram")
    assert status == "done", text
    assert started and started[0].endswith("Telegram.exe"), "должен запустить exe по пути"


# ----- Store/MSIX-приложения (WindowsApps закрыт ACL, .lnk нет) -----

def test_launch_store_app_starts_via_shell_appsfolder(monkeypatch):
    """AppID из Get-StartApps -> запуск os.startfile('shell:AppsFolder\\AppID')."""
    started = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: type("R", (), {
            "stdout": (b"TelegramMessengerLLP.TelegramDesktop_t4vj0pshhgkwm!"
                       b"Telegram.TelegramDesktop.Store\r\n")})(),
    )
    monkeypatch.setattr("os.startfile", lambda p: started.append(str(p)))
    aumid = actions._launch_store_app("telegram")
    assert aumid == "TelegramMessengerLLP.TelegramDesktop_t4vj0pshhgkwm!Telegram.TelegramDesktop.Store"
    assert started and started[0].startswith("shell:AppsFolder\\")


def test_open_app_falls_back_to_store_app(monkeypatch):
    """Telegram из Магазина Windows: нет алиаса/ярлыка/exe по пути —
    срабатывает запуск Store-приложения и запоминается «aumid:...»."""
    started = []
    remembered = []
    monkeypatch.setattr(actions, "APP_ALIASES", {})
    monkeypatch.setattr(actions, "_load_learned_aliases", lambda: {})
    monkeypatch.setattr(actions, "_find_shortcut", lambda name: None)
    monkeypatch.setattr(actions, "_find_app_exe", lambda name: None)
    monkeypatch.setattr(actions, "_launch_store_app",
                        lambda name: "Family!App" if name == "telegram" else None)
    monkeypatch.setattr(actions, "_remember_app_alias",
                        lambda n, p: remembered.append((n, p)))
    monkeypatch.setattr("os.startfile", lambda p: started.append(str(p)))
    status, text = actions.open_app("telegram")
    assert status == "done", text
    assert "магазин" in text
    assert ("telegram", "aumid:Family!App") in remembered


def test_open_app_resumes_learned_aumid(monkeypatch):
    """Уже запомненный «aumid:...» — сразу запуск через shell:AppsFolder,
    без повторного поиска."""
    started = []
    monkeypatch.setattr(actions, "APP_ALIASES", {})
    monkeypatch.setattr(actions, "_load_learned_aliases",
                        lambda: {"telegram": "aumid:Family!App"})
    monkeypatch.setattr("os.startfile", lambda p: started.append(str(p)))
    status, text = actions.open_app("telegram")
    assert status == "done", text
    assert started and started[0].startswith("shell:AppsFolder\\")


def test_ui_do_task_dispatches_to_ui_agent(monkeypatch):
    seen = []

    def fake_run_ui_task(goal, window=None):
        seen.append((goal, window))
        return "done", "ok"

    monkeypatch.setattr("ui_agent.run_ui_task", fake_run_ui_task)
    from actions import execute_action
    status, text, img = execute_action("ui_do_task", {"goal": "написать привет"})
    assert status == "done"
    assert seen and "привет" in seen[0][0]
    assert img is None


def test_ui_click_by_text_dispatch(monkeypatch):
    clicked = []
    monkeypatch.setattr("ui_automation.get_active_window", lambda: "Тест")
    monkeypatch.setattr(
        "ui_automation.click_by_text",
        lambda w, t: clicked.append((w, t)) or (w, t) and True,
    )
    from actions import execute_action
    status, text, _ = execute_action("ui_click_by_text", {"window": "", "text": "Отправить"})
    assert status == "done"
    assert clicked and clicked[0] == ("Тест", "Отправить")


def test_ui_select_item_error_when_missing(monkeypatch):
    monkeypatch.setattr("ui_automation.get_active_window", lambda: "Тест")
    monkeypatch.setattr("ui_automation.select_item", lambda w, t: False)
    from actions import execute_action
    status, text, _ = execute_action("ui_select_item", {"text": "Дмитрий"})
    assert status == "error"