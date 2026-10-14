"""UIA-адаптер: строгие запросы к дереву активного окна Windows.

Без LLM: клики/ввод по имени и роли элемента. Живёт в фоновом потоке
(вызывается через asyncio.to_thread из ui_agent / actions).
"""
import logging
import time
from typing import Optional

_log = logging.getLogger(__name__)

ROLE_MAP = {
    "ButtonControl": "button",
    "EditControl": "edit",
    "ListItemControl": "list_item",
    "MenuItemControl": "menu_item",
    "TabItemControl": "tab",
    "TextControl": "text",
    "ComboBoxControl": "combobox",
    "CheckBoxControl": "checkbox",
    "TreeItemControl": "tree_item",
    "HyperlinkControl": "link",
    "ListViewControl": "list",
    "TreeControl": "tree",
}


def _uia():
    try:
        import uiautomation as uia
        return uia
    except ImportError:
        return None


def _resolve_root(window: str):
    """Возвращает корневой Control для окна или None.

    Несколько попыток: GetChildren корня на UWP/Store-провайдерах (Telegram)
    иногда транзиентно отдаёт пусто — из-за этого find_message_input давал None
    при живом поле. Повтор со свежим корнем закрывает гонку.

    Telegram при поиске пересоздаёт окно (меняет PID в заголовке). Точный
    подстроковый матч "Telegram (22921)" ломается. Фолбэк: класс MainWindow +
    процесс Telegram.exe — устойчивый сигнал."""
    uia = _uia()
    if uia is None:
        return None
    target_lower = (window or "").lower()
    is_telegram = "telegram" in target_lower
    for _ in range(3):
        try:
            root = uia.GetRootControl()
            for c in root.GetChildren():
                # 1) Точный матч (как было)
                if c.Name and target_lower and target_lower in c.Name.lower():
                    return c
                # 2) Фолбэк для Telegram: класс "class MainWindow" + процесс Telegram.exe
                if is_telegram:
                    try:
                        if (c.ClassName or "").lower() == "class mainwindow" and \
                           _process_name(c.ProcessId).lower() == "telegram.exe":
                            return c
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(0.05)
    return None


def _iter_controls(control, max_depth: int = 12):
    if control is None:
        return
    try:
        yield control
        if max_depth > 0:
            for child in control.GetChildren():
                yield from _iter_controls(child, max_depth - 1)
    except Exception:
        pass


def _to_dict(c) -> dict:
    try:
        rect = c.BoundingRectangle
        x, y = rect.left, rect.top
        w, h = rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        x = y = w = h = 0
    try:
        role = ROLE_MAP.get(c.ControlTypeName, c.ControlTypeName)
        name = c.Name or ""
    except Exception:
        role, name = "unknown", ""
    return {"role": role, "text": (name or "").strip(), "name": name,
            "x": x, "y": y, "width": w, "height": h}


def get_active_window() -> Optional[str]:
    uia = _uia()
    if uia is None:
        return None
    try:
        cur = uia.GetForegroundControl()
        return (cur.Name or "") if cur is not None else None
    except Exception:
        return None


def get_window_elements(window: str) -> list[dict]:
    root = _resolve_root(window)
    if root is None:
        return []
    return [_to_dict(c) for c in _iter_controls(root)]


def _find(window: str, role: str, text: str) -> Optional[dict]:
    root = _resolve_root(window)
    if root is None:
        return None
    target = text.lower()
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == role and d["name"].lower() == target:
            return d
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == role and target in d["name"].lower():
            return d
    return None


def click_by_text(window: str, text: str) -> bool:
    root = _resolve_root(window)
    target = text.lower()
    for c in _iter_controls(root):
        d = _to_dict(c)
        if target in d["name"].lower():
            try:
                c.GetClickablePoint()
                c.Click(simulateMove=False)
                return True
            except Exception:
                try:
                    c.Click()
                    return True
                except Exception:
                    return False
    return False


def _click_control(c) -> bool:
    """Клик по UIA-контролу: через clickable point, иначе обычным Click."""
    try:
        c.GetClickablePoint()
        c.Click(simulateMove=False)
        return True
    except Exception:
        try:
            c.Click()
            return True
        except Exception:
            return False


def click_name(window: str, text: str) -> bool:
    """Клик по элементу с ТОЧНЫМ именем (без учёта регистра). В отличие от
    click_by_text (подстрока — «Дима» попадает и в «Дима Шуриков»), здесь
    ищем равенство: не откроем не тот чат. Точного нет — False (не кликаем
    похожего подстрокой: пусть идёт в поиск приложения)."""
    root = _resolve_root(window)
    if root is None:
        return False
    target = text.lower()
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["text"] and d["text"].lower() == target:
            if _click_control(c):
                return True
    return False


def find_edit_control(window: str) -> Optional[dict]:
    root = _resolve_root(window)
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == "edit":
            return d
    return None


_SEARCH_LIKE = ("поиск", "search", "lookup", "найти")


def find_message_input(window: str) -> Optional[dict]:
    """Поле ввода сообщения в чатах: НИЖНИЙ edit (композер), а не строка
    поиска (в Telegram верхний edit — «Поиск»; писать туда и жать Enter нельзя).

    Если в окне единственный edit и он похож на поиск — композера нет,
    возвращаем None: Ева не должна «отправлять» в строку поиска."""
    root = _resolve_root(window)
    edits = []
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == "edit":
            edits.append(d)
    if not edits:
        return None
    if len(edits) == 1:
        nm = (edits[0]["name"] or "").strip().lower()
        if any(s in nm for s in _SEARCH_LIKE):
            return None
        return edits[0]
    # Telegram рисует «Поиск» двумя вложенными edit-ами (обёртка + внутренний)
    # — len>1 обходил проверку «единственный поиск». Если все edit-ы похожи
    # на поиск, поля сообщения НЕТ (None), даже если их несколько.
    if all(any(s in (e["name"] or "").strip().lower() for s in _SEARCH_LIKE)
           for e in edits):
        return None
    return max(edits, key=lambda e: e["y"])


def find_search_edit(window: str) -> Optional[dict]:
    """Поле поиска в приложении (для шага «найти контакт») — инверсия
    find_message_input: edit, похожий на поиск, иначе самый ВЕРХНИЙ по y.

    В Telegram верхнее поле — «Поиск»: туда вводим имя контакта и жмём
    Enter, он открывает первый результат чатом."""
    root = _resolve_root(window)
    edits = []
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == "edit":
            edits.append(d)
    if not edits:
        return None
    for e in edits:
        nm = (e["name"] or "").strip().lower()
        if any(s in nm for s in _SEARCH_LIKE):
            return e
    return min(edits, key=lambda e: e["y"])


def _try_set_value(control, text: str) -> bool:
    """Best-effort ввод в control:SetValue, иначе SendKeys с очисткой поля."""
    try:
        control.SetValue(text)
        return True
    except Exception:
        try:
            # Очищаем поле перед вводом: Ctrl+A (выделить всё) + Del (удалить)
            # или Shift+End+Del для текстовых полей
            control.SendKeys("^{HOME}+{END}{DEL}")
            control.SendKeys(text)
            return True
        except Exception:
            return False


def _bottom_edit(root):
    """Самый НИЖНИЙ edit по y (композер в чатах), иначе None. Telegram рисует
    вложенные edit-ы («Сообщение...»), а строка поиска — верхняя («Поиск»);
    первый по дереву может оказаться поиском — пишем туда нельзя."""
    best = None
    best_y = -1
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == "edit" and d["y"] >= best_y:
            best, best_y = c, d["y"]
    return best


def set_text(window: str, text: str) -> bool:
    """Ввод текста в композер. Целит в НИЖНИЙ edit (не первый по дереву —
    иначе писали бы в строку поиска). Несколько попыток: _resolve_root на
    Store/Telegram транзиентно отдаёт None (гонка между find_* и set_*)."""
    for _ in range(3):
        root = _resolve_root(window)
        if root is not None:
            edit = _bottom_edit(root)
            if edit is not None and _try_set_value(edit, text):
                return True
            if _try_set_value(root, text):
                return True
        time.sleep(0.05)
    return False


def set_text_search(window: str, text: str) -> bool:
    """Ввод текста в строку поиска (не композер). Использует find_search_edit.
    Несколько попыток: _resolve_root на Store/Telegram транзиентно отдаёт None."""
    for _ in range(3):
        root = _resolve_root(window)
        if root is not None:
            search = find_search_edit(root)
            if search is not None and _try_set_value(search, text):
                return True
        time.sleep(0.05)
    return False


def select_item(window: str, text: str) -> bool:
    n = text.lower()
    for role in ("list_item", "menu_item", "tree_item"):
        if _find(window, role, n):
            if click_by_text(window, n):
                return True
    return False


def _process_name(pid: int) -> str:
    """Имя процесса по PID (для идентификации окна, когда класс универсален —
    как у Telegram: MainWindow на любой чат)."""
    try:
        import psutil
        return (psutil.Process(int(pid)).name() or "")
    except Exception:
        return ""


def list_windows() -> list[tuple[str, str, str]]:
    """Все топ-уровневые окна как (заголовок, класс, процесс) — для подбора
    окна под цель (например «в телеграм»), а не слепого взятия активного."""
    uia = _uia()
    if uia is None:
        return []
    out: list[tuple[str, str, str]] = []
    try:
        root = uia.GetRootControl()
        for c in root.GetChildren():
            try:
                out.append((c.Name or "", c.ClassName or "",
                            _process_name(c.ProcessId)))
            except Exception:
                continue
    except Exception:
        pass
    return out


def send_enter(window: str) -> bool:
    """Отправка ввода: Enter в поле ввода (для чатов — отправка сообщения).
    Целит в НИЖНИЙ edit (композер), как и find_message_input — иначе Enter
    уходит в строку поиска (первый edit). Несколько попыток — та же гонка
    _resolve_root, что в set_text.

    Telegram (Store/XAML island) игнорирует UIA SendKeys на вложенных контролах
    («Поиск» Inner, «Сообщение» Inner) — вызов успешен (нет исключения), но
    клавиша не доходит. Фолбэк: физический Enter через SendInput после фокуса
    на окне/контроле."""
    for _ in range(3):
        root = _resolve_root(window)
        if root is not None:
            target = _bottom_edit(root) or root
            try:
                try:
                    target.SetFocus()
                except Exception:
                    pass
                target.SendKeys("{ENTER}")
                # UIA SendKeys может "успешно" вернуться, но не доставить клавишу
                # (Telegram Inner-контролы). Физический Enter гарантирует доставку.
                _physical_enter()
                return True
            except Exception:
                try:
                    root.SendKeys("{ENTER}")
                    _physical_enter()
                    return True
                except Exception:
                    pass
        time.sleep(0.05)
    return False


def _physical_enter() -> None:
    """Физическое нажатие Enter через SendInput (обходит UIA)."""
    try:
        import ctypes
        USER32 = ctypes.windll.user32
        # VK_RETURN = 0x0D, scan-code = 0x1C
        USER32.keybd_event(0x0D, 0x1C, 0, 0)      # key down
        time.sleep(0.03)
        USER32.keybd_event(0x0D, 0x1C, 0x0002, 0) # key up
    except Exception:
        pass


def restore_window(window: str) -> bool:
    """Разворачивает/показывает сворачиваемое в трей или минимизированное
    окно (UIA-клики по свёрнутому окну уходят в пустоту). Best-effort:
    False просто говорит «окно не нашлось/не удалось» — не исключение."""
    uia = _uia()
    if uia is None:
        return False
    try:
        wc = uia.WindowControl(searchDepth=1, SubName=window)
        if not wc.Exists(0.5):
            return False
        wc.SetWindowVisualState(uia.VisualState.Normal)
        try:
            wc.SetActive()
        except Exception:
            pass
        return True
    except Exception:
        return False


# Re-mount helpers so tests can swap _resolve_root.
class FakeWindow:
    """Минимальный фейковый корень для юнит-тестов (без uiautomation)."""
    def __init__(self, controls=None):
        self.controls = controls or []

    def GetChildren(self):
        return self.controls