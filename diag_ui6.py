"""Диагностика: доставляется ли физический Enter после ввода «Дима» в поиск.

Чиню send_enter (UIA SendKeys на вложенном «Поиск» падает → False). Этот скрипт:
1) вводит «Дима» через ui.set_text (как production),
2) отлаживает: что именно бросает ui.send_enter (печать исключения),
3) пробует ФИЗИЧЕСКИЙ Enter (SendInput через ctypes) — с фокусом на окне,
4) при неудаче пробует физический клик в поле поиска, потом Enter,
5) после каждого шага сверяет заголовок окна 'Дима – (pid)'.
"""
import ctypes
import sys
import time
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import ui_automation as ui  # noqa: E402


USER32 = ctypes.windll.user32


def _press_enter() -> None:
    """Физическое нажатие Enter (SendInput, без UIA)."""
    key = ctypes.c_ushort(0x0D)  # VK_RETURN
    flags_down = 0
    flags_up = 0x0002           # KEYEVENTF_KEYUP
    # SCAN-code для Enter = 0x1C
    try:
        keybd = USER32.keybd_event
        keybd(0x0D, 0x1C, flags_down, 0)
        time.sleep(0.05)
        keybd(0x0D, 0x1C, flags_up, 0)
    except Exception as e:
        print(f"  keybd_event упал: {type(e).__name__}: {e}")


def _focus_window(heading: str) -> None:
    """Ставит окно поверх/в фокус по UIA WindowControl (как restore_window)."""
    try:
        wc = _uia_mod().WindowControl(searchDepth=1, SubName=heading)
        if wc and wc.Exists(0.3):
            wc.SetActive()
            time.sleep(0.2)
    except Exception as e:
        print(f"  focus упал: {type(e).__name__}: {e}")


def _uia_mod():
    import uiautomation as uia
    return uia


def _telegram_window() -> str:
    tg = [(t, c, p) for t, c, p in ui.list_windows() if "telegram" in (p or "").lower()]
    return tg[0][0] if tg else None


def _head(win: str) -> str:
    return (win or "").split(" – ", 1)[0].split(" - ", 1)[0].replace("‎", "").strip()


def _mouse_click(x: int, y: int) -> None:
    USER32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    USER32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.03)
    USER32.mouse_event(0x0004, 0, 0, 0, 0)


def _entered_value(win: str) -> str:
    """Текущий текст нижнего edit (композера/поиска) для проверки ввода."""
    root = ui._resolve_root(win)
    if root is None:
        return "<нет дерева>"
    best, best_y = None, -1
    for c in ui._iter_controls(root):
        d = ui._to_dict(c)
        if d["role"] == "edit" and d["y"] >= best_y:
            best, best_y = c, d["y"]
    if best is None:
        return "<edit нет>"
    try:
        return str(best.Value) if hasattr(best, "Value") else "<нет Value>"
    except Exception as e:
        return f"<Value упал: {e}>"


def main() -> int:
    win = _telegram_window()
    if not win:
        print("Telegram-окно не найдено.")
        return 1
    print(f"Окно: {win!r}  |  заголовок: {_head(win)!r}")

    # 1) Ввод «Дима» как в production
    print("\n>>> set_text(win, 'Дима') …")
    t0 = time.time()
    ok = ui.set_text(win, "Дима")
    print(f"  set_text → {ok}  ({(time.time()-t0)*1000:.0f} мс)")
    time.sleep(0.6)
    print(f"  значение поля сейчас: {_entered_value(win)!r}")

    # 2) Отладка send_enter: что падает
    print("\n>>> отладка send_enter (вручную, с печатью исключений) …")
    root = ui._resolve_root(win)
    target = ui._bottom_edit(root or ui.FakeWindow())
    print(f"  _bottom_edit → {target!r}")
    if target is not None:
        try:
            target.SetFocus()
            print("  SetFocus() ok")
        except Exception as e:
            print(f"  SetFocus() → {type(e).__name__}: {e}")
        try:
            target.SendKeys("{ENTER}")
            print("  target.SendKeys('{ENTER}') ок — наверное Enter ушёл")
        except Exception as e:
            print(f"  target.SendKeys → {type(e).__name__}: {e}")

    # 3) Физический Enter (фокус уже, вероятно, у окна/поля)
    print("\n>>> ФИЗИЧЕСКИЙ Enter …")
    _focus_window("Telegram")  # окно поверх, чтобы Enter не ушёл в чат Клода
    time.sleep(0.3)
    _press_enter()
    time.sleep(1.5)
    win2 = _telegram_window()
    head2 = _head(win2)
    opened = "дима" in head2.lower() and "telegram" not in head2.lower()
    print(f"  после физ.E enter: {head2!r}; чат открыт: {opened}")
    if opened:
        print("\n✅ ФИЗИЧЕСКИЙ Enter открывает чат — это фикс для send_enter.")
        return 0

    # 4) Клик в поле поиска (центр edit y=50..102, x=124..968) + Enter
    cx, cy = 546, 76
    print(f"\n>>> клик мыши в поле поиска ({cx},{cy}) + физич. Enter …")
    _mouse_click(cx, cy)
    time.sleep(0.5)
    _press_enter()
    time.sleep(1.5)
    win3 = _telegram_window()
    head3 = _head(win3)
    opened3 = "дима" in head3.lower() and "telegram" not in head3.lower()
    print(f"  после клик+Enter: {head3!r}; чат открыт: {opened3}")
    if opened3:
        print("\n✅ Клик+физ.Enter работает — фикс: кликать в поле перед Enter.")

    # 5) Последняя проверка: появился ли список результатов (list_item больше?)
    root5 = ui._resolve_root(win3)
    n_li = sum(1 for c in ui._iter_controls(root5) if ui._to_dict(c)["role"] == "list_item") if root5 else -1
    print(f"  list_item теперь: {n_li}")
    return 0


if __name__ == "__main__":
    sys.exit(main())