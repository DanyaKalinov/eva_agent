# Юниты UIA-адаптера. Не зависят от живого окна и установленной
# uiautomation: дерево мокается через FakeWindow + подмену модульного
# `_resolve_root`. Живой тест (Блокнот) помечен skip, если окно не активно.
import pytest

import ui_automation as ui

# Реальная _resolve_root ДО загрязнения тестами ниже (они переназначают её
# прямым присваиванием и не возвращают). Нужна тесту ретрая, чтобы гонять
# настоящую логику, а не протёкшую лямбду.
_REAL_RESOLVE_ROOT = ui._resolve_root


def test_get_window_elements_uses_uia():
    # Мок-дерево через monkeypatch нижнего слоя: подставляем control-обёртку
    fake = ui.FakeWindow()  # реальная реализация в нижней части модуля

    ui._resolve_root = lambda window: fake
    els = ui.get_window_elements("Блокнот")
    assert isinstance(els, list)
    assert all(set(e) >= {"role", "text", "name", "x", "y", "width", "height"} for e in els)


def test_get_window_elements_empty_when_no_root():
    ui._resolve_root = lambda window: None
    assert ui.get_window_elements("Блокнот") == []


def test_click_by_text_found():
    class FakeControl:
        ControlTypeName = "ButtonControl"

        def __init__(self, name):
            self._name = name
        @property
        def Name(self):
            return self._name
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 1, "top": 2, "right": 31, "bottom": 22})()
        def GetClickablePoint(self):
            return (10, 10)
        def Click(self, simulateMove=False):
            self.clicked = True

    c = FakeControl("Отправить")
    fake = ui.FakeWindow(controls=[c])
    ui._resolve_root = lambda window: fake

    assert ui.click_by_text("Тест", "отправ") is True
    assert getattr(c, "clicked", False) is True


def test_find_edit_control_returns_first():
    class FakeEdit:
        ControlTypeName = "EditControl"

        @property
        def Name(self):
            return ""
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": 0, "right": 10, "bottom": 5})()

    fake = ui.FakeWindow(controls=[FakeEdit()])
    ui._resolve_root = lambda window: fake
    d = ui.find_edit_control("Тест")
    assert d is not None and d["role"] == "edit"


def test_set_text_on_edit():
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self):
            self.value = ""
        @property
        def Name(self):
            return ""
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": 0, "right": 10, "bottom": 5})()
        def SetValue(self, text):
            self.value = text

    e = FakeEdit()
    fake = ui.FakeWindow(controls=[e])
    ui._resolve_root = lambda window: fake
    assert ui.set_text("Тест", "привет") is True
    assert e.value == "привет"


def test_set_text_no_edit_falls_back_to_root():
    class FakeRoot:
        def __init__(self):
            self.value = ""
        def GetChildren(self):
            return []
        def SetValue(self, text):
            self.value = text

    root = FakeRoot()
    ui._resolve_root = lambda window: root
    assert ui.set_text("Тест", "привет") is True
    assert root.value == "привет"


def test_set_text_targets_bottom_edit_not_top():
    """БАГ: set_text писал в ПЕРВЫЙ edit по дереву (может быть строка поиска
    сверху) вместо НИЖНЕГО (композер). Как send_enter — цель в нижний."""
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self, name, y):
            self._name = name
            self._y = y
            self.value = ""
        @property
        def Name(self):
            return self._name
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": self._y,
                                  "right": 100, "bottom": self._y + 20})()
        def SetValue(self, text):
            self.value = text

    search = FakeEdit("Поиск", 50)
    composer = FakeEdit("", 700)
    fake = ui.FakeWindow(controls=[search, composer])
    ui._resolve_root = lambda window: fake
    assert ui.set_text("Тест", "привет") is True
    assert composer.value == "привет", "текст должен уйти в НИЖНИЙ edit (композер)"
    assert search.value == "", "в строку поиска писать нельзя"


def test_set_text_retries_when_root_transiently_none(monkeypatch):
    """БАГ: _resolve_root на Store/Telegram транзиентно отдаёт None (гонка —
    find_message_input выстрелил, а следующий set_text уже нет). set_text должен
    повторять попытку свежим корнем, прежде чем сдаться."""
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self):
            self.value = ""
        @property
        def Name(self):
            return ""
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": 10, "right": 100, "bottom": 30})()
        def SetValue(self, text):
            self.value = text

    state = {"calls": 0}
    edit = FakeEdit()
    fake = ui.FakeWindow(controls=[edit])

    def flaky_root(_win):
        state["calls"] += 1
        return None if state["calls"] == 1 else fake

    monkeypatch.setattr(ui, "_resolve_root", flaky_root)
    assert ui.set_text("Тест", "привет") is True
    assert edit.value == "привет"
    assert state["calls"] >= 2, "должна была повторить попытку после None"


def test_send_enter_sends_to_edit(monkeypatch):
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self):
            self.keys = None
        @property
        def Name(self):
            return ""
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": 0, "right": 10, "bottom": 5})()
        def SetFocus(self):
            pass
        def SendKeys(self, keys):
            self.keys = keys

    e = FakeEdit()
    fake = ui.FakeWindow(controls=[e])
    monkeypatch.setattr(ui, "_resolve_root", lambda window: fake)
    assert ui.send_enter("Тест") is True
    assert e.keys == "{ENTER}"


class _FakeUiaChildren:
    def GetChildren(self):
        return [_FakeUiaChild("Амина – (82)", "TDeskTopWindow", 10),
                _FakeUiaChild("C:\\bat\\cmd", "ConsoleWindowClass", 20)]


class _FakeUiaChild:
    def __init__(self, name, cls, pid):
        self.Name = name
        self.ClassName = cls
        self.ProcessId = pid
        self.BoundingRectangle = type("R", (), {"left": 0, "top": 0,
                                               "right": 10, "bottom": 5})()


class _FakeUia:
    def GetRootControl(self):
        return _FakeUiaChildren()


def test_list_windows_includes_process_name(monkeypatch):
    monkeypatch.setattr(ui, "_uia", lambda: _FakeUia())
    monkeypatch.setattr(ui, "_process_name",
                        lambda pid: "Telegram.exe" if pid == 10 else "")
    assert ui.list_windows() == [
        ("Амина – (82)", "TDeskTopWindow", "Telegram.exe"),
        ("C:\\bat\\cmd", "ConsoleWindowClass", "")]


def test_list_windows_empty_without_uia(monkeypatch):
    monkeypatch.setattr(ui, "_uia", lambda: None)
    assert ui.list_windows() == []


def test_process_name_resolves_via_psutil(monkeypatch):
    import ui_automation as ui

    class FakeProc:
        def name(self):
            return "Telegram.exe"

    monkeypatch.setattr("psutil.Process", lambda pid: FakeProc())
    assert ui._process_name(16900) == "Telegram.exe"
    monkeypatch.setattr("psutil.Process",
                        lambda pid: (_ for _ in ()).throw(Exception("нет процесса")))
    assert ui._process_name(16900) == ""


def test_resolve_root_retries_when_children_transiently_empty(monkeypatch):
    """_resolve_root флакал: GetChildren корня иногда отдавал пусто (частичный
    пакет на Store/Telegram), и find_message_input давал None при живом поле.
    Теперь повторяет попытку со свежим корнем, прежде чем сдаться."""
    state = {"calls": 0}

    class FakeWin:
        Name = "Тест"

    class FakeRoot:
        def GetChildren(self):
            state["calls"] += 1
            if state["calls"] < 3:
                return []
            return [FakeWin()]

        def FindControl(self, **kw):
            return None

    class FakeUIA:
        def GetRootControl(self):
            return FakeRoot()

    monkeypatch.setattr(ui, "_uia", lambda: FakeUIA())
    # На всякий случай вернуть настоящую (а не протёкшую из тестов выше).
    monkeypatch.setattr(ui, "_resolve_root", _REAL_RESOLVE_ROOT)
    assert ui._resolve_root("Тест") is not None
    assert state["calls"] >= 3


def test_send_enter_targets_bottom_edit_not_top():
    """БАГ: send_enter брал ПЕРВЫЙ edit (поиск вверху) вместо НИЖНЕГО (композер).
    Enter уходил в строку поиска — сообщение не отправлялось. Теперь целит в
    нижний edit (как find_message_input)."""
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self, name, y):
            self._name = name
            self._y = y
            self.entered = False
        @property
        def Name(self):
            return self._name
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": self._y,
                                  "right": 100, "bottom": self._y + 20})()
        def SetFocus(self):
            pass
        def SendKeys(self, keys):
            if keys == "{ENTER}":
                self.entered = True

    search = FakeEdit("Поиск", 50)
    composer = FakeEdit("", 700)
    fake = ui.FakeWindow(controls=[search, composer])
    ui._resolve_root = lambda window: fake
    assert ui.send_enter("Тест") is True
    assert composer.entered is True, "Enter должен уйти в НИЖНИЙ edit (композер)"
    assert search.entered is False, "Enter НЕ должен уходить в поиск (верхний edit)"


def test_find_message_input_prefers_bottom_edit():
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self, name, y):
            self._name = name
            self._y = y
        @property
        def Name(self):
            return self._name
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": self._y,
                                  "right": 100, "bottom": self._y + 20})()

    search = FakeEdit("Поиск", 50)
    composer = FakeEdit("", 720)
    ui._resolve_root = lambda window: ui.FakeWindow(controls=[search, composer])
    d = ui.find_message_input("Тест")
    assert d is not None and d["y"] == 720


def test_find_message_input_none_when_only_search():
    class FakeEdit:
        ControlTypeName = "EditControl"
        @property
        def Name(self):
            return "Поиск"
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": 50, "right": 100, "bottom": 70})()

    ui._resolve_root = lambda window: ui.FakeWindow(controls=[FakeEdit()])
    assert ui.find_message_input("Тест") is None


def test_find_message_input_none_when_only_search_duplicates():
    """Telegram рисует строку поиска ДВУМЯ вложенными edit-ами («Поиск»×2:
    обёртка y=50 + внутриний y=62) — len(edits)==2 обходил проверку «единственный
    поиск», и find_message_input возвращал поиск за «поле сообщения». Если все
    edit-ы похожи на поиск → композера НЕТ (None)."""
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self, y):
            self._y = y
        @property
        def Name(self):
            return "Поиск"
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": self._y,
                                  "right": 100, "bottom": self._y + 20})()

    ui._resolve_root = lambda w: ui.FakeWindow(controls=[FakeEdit(50), FakeEdit(62)])
    assert ui.find_message_input("Тест") is None


def test_find_message_input_returns_plain_single_edit():
    class FakeEdit:
        ControlTypeName = "EditControl"
        @property
        def Name(self):
            return ""
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": 10, "right": 100, "bottom": 30})()

    ui._resolve_root = lambda window: ui.FakeWindow(controls=[FakeEdit()])
    assert ui.find_message_input("Тест") is not None


# ----- Поле поиска контакта: инверсия find_message_input (верх/поиск) -----
def test_find_search_edit_prefers_named_search():
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self, name, y):
            self._name = name
            self._y = y
        @property
        def Name(self):
            return self._name
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": self._y,
                                  "right": 100, "bottom": self._y + 20})()

    # Поиск — выше (50), но и без него нижний не должен быть выбран: поиск есть.
    search = FakeEdit("Поиск", 50)
    other = FakeEdit("", 30)
    ui._resolve_root = lambda window: ui.FakeWindow(controls=[other, search])
    d = ui.find_search_edit("Тест")
    assert d is not None and d["name"] == "Поиск"


def test_find_search_edit_takes_topmost_when_unnamed():
    class FakeEdit:
        ControlTypeName = "EditControl"

        def __init__(self, y):
            self._y = y
        @property
        def Name(self):
            return ""
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": self._y,
                                  "right": 100, "bottom": self._y + 20})()

    ui._resolve_root = lambda window: ui.FakeWindow(controls=[FakeEdit(80), FakeEdit(20)])
    d = ui.find_search_edit("Тест")
    assert d is not None and d["y"] == 20


def test_find_search_edit_none_without_edits():
    ui._resolve_root = lambda window: ui.FakeWindow()
    assert ui.find_search_edit("Тест") is None


# ----- Точный клик по имени (против промаха на похожий контакт) -----
def test_click_name_exact_before_substring():
    """ПРОМАХ: «Дима» кликается ТОЧНЫМ именем, а не первым подстроковым
    («Дима Шуриков»). Иначе отправили бы не в тот чат."""
    class FakeCtrl:
        ControlTypeName = "ListItemControl"

        def __init__(self, name):
            self._name = name
            self._clicked = False
        @property
        def Name(self):
            return self._name
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": 0, "right": 10, "bottom": 5})()
        def GetClickablePoint(self):
            return (5, 2)
        def Click(self, simulateMove=False):
            self._clicked = True

    near = FakeCtrl("Дима Шуриков")
    exact = FakeCtrl("Дима")
    fake = ui.FakeWindow(controls=[near, exact])
    ui._resolve_root = lambda window: fake
    assert ui.click_name("Тест", "Дима") is True
    assert exact._clicked is True, "должен кликнуть точный «Дима», а не «Дима Шуриков»"
    assert near._clicked is False


def test_click_name_returns_false_when_not_exact():
    """Точного «Диме» нет в списке — False (не кликаем похожих подстрокой)."""
    class FakeCtrl:
        ControlTypeName = "ListItemControl"
        @property
        def Name(self):
            return "Дима"
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": 0, "right": 10, "bottom": 5})()

    fake = ui.FakeWindow(controls=[FakeCtrl()])
    ui._resolve_root = lambda window: fake
    assert ui.click_name("Тест", "Диме") is False


REQUIRES_NOTEPAD = pytest.mark.skipif(
    not ui.get_active_window() or "блокнот" not in (ui.get_active_window() or "").lower(),
    reason="Блокнот не активен",
)


@REQUIRES_NOTEPAD
def test_live_notepad_edit():
    ok = ui.set_text("Блокнот", "привет")
    assert ok