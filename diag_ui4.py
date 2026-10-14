"""Диагностика Bug C: click_name находит list_item «Дима», но клик не срабатывает.

Тест: открывает Telegram (если не открыт — просто пишет список окон), находит
контрол с точным текстом «Дима», и шаг за шагом проверяет почему _click_control
возвращает False: GetClickablePoint(), физический клик мышью, InvokePattern.
Каждую попытку сверяет с заголовком окна: стал ли он «Дима – (pid)».

ВНИМАНИЕ: реально кликает в список чатов (открывает чат «Дима»), но НИЧЕГО
не отправляет и не вводит.
"""
import ctypes
import time

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import ui_automation as ui  # noqa: E402


def _click_mouse(x: int, y: int) -> None:
    """Физический клик мыши в экранные координаты (ctypes, без uiautomation)."""
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)   # left down
    time.sleep(0.03)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)   # left up


def _find_list_items(win: str, text: str) -> list:
    root = ui._resolve_root(win)
    if root is None:
        return []
    out = []
    for c in ui._iter_controls(root):
        d = ui._to_dict(c)
        if d["role"] == "list_item" and (d["text"] or "").strip().lower() == text.lower():
            out.append((c, d))
    return out


def _telegram_window() -> str:
    wins = ui.list_windows()
    tg = [(t, c, p) for t, c, p in wins if "telegram" in (p or "").lower()]
    return tg[0][0] if tg else None


def _window_head(win: str) -> str:
    return (win or "").split(" – ", 1)[0].split(" - ", 1)[0].replace("‎", "").strip()


def main() -> int:
    win = _telegram_window()
    if not win:
        print("Telegram-окна нет — открой Telegram и повтори.")
        return 1
    print(f"Окно: {win!r}")

    items = _find_list_items(win, "Дима")
    if not items:
        print("list_item с точным текстом «Дима» НЕ найден — кликать не по чему.")
        return 1
    print(f"Найдено list_item «Дима»: {len(items)}")

    c, d = items[0]
    print(f"  rect: x={d['x']} y={d['y']} w={d['width']} h={d['height']}")
    cx, cy = d["x"] + d["width"] // 2, d["y"] + d["height"] // 2
    print(f"  центр (экранные): ({cx}, {cy})")

    # 1) GetClickablePoint — что бросает
    try:
        pt = c.GetClickablePoint()
        print(f"  GetClickablePoint() → {pt!r}")
        gcp_ok = True
    except Exception as e:
        print(f"  GetClickablePoint() → ИСКЛЮЧЕНИЕ: {type(e).__name__}: {e}")
        gcp_ok = False

    before = _window_head(_telegram_window())
    print(f"  Заголовок до клика: {before!r}")

    # 2) _click_control (как в click_name)
    ok = ui._click_control(c)
    time.sleep(1.2)
    after = _window_head(_telegram_window())
    opened = "дима" in after.lower() and "telegram" not in after.lower()
    print(f"  _click_control → {ok}; заголовок после: {after!r}; чат открыт: {opened}")
    if opened:
        print("   ✅ Открылось! click_name работал бы (но, видимо, не в момент сбоя).")
        return 0

    # 3) Физический клик мышью по центру
    print(f"  Физический клик мышью в ({cx}, {cy})…")
    _click_mouse(cx, cy)
    time.sleep(1.2)
    after2 = _window_head(_telegram_window())
    opened2 = "дима" in after2.lower() and "telegram" not in after2.lower()
    print(f"  после: {after2!r}; чат открыт: {opened2}")

    # 4) GetClickablePoint точкой (если доступна)
    if gcp_ok and not opened2:
        try:
            px, py = int(pt[0]), int(pt[1])
            print(f"  Клик мышью в GetClickablePoint ({px}, {py})…")
            _click_mouse(px, py)
            time.sleep(1.2)
            after3 = _window_head(_telegram_window())
            opened3 = "дима" in after3.lower() and "telegram" not in after3.lower()
            print(f"  после: {after3!r}; чат открыт: {opened3}")
        except Exception as e:
            print(f"  Клик по точке упал: {e}")

    # 5) Invoke-паттерн (если доступен)
    try:
        invoke = c.GetInvokePattern()
        print("  Есть InvokePattern — пробую Invoke()…")
        invoke.Invoke()
        time.sleep(1.2)
        after4 = _window_head(_telegram_window())
        opened4 = "дима" in after4.lower() and "telegram" not in after4.lower()
        print(f"  после: {after4!r}; чат открыт: {opened4}")
        if opened4:
            print("   ✅ Invoke() открывает чат! Это фикс для _click_control.")
    except Exception as e:
        print(f"  InvokePattern: {type(e).__name__}: {e}")

    head = _window_head(_telegram_window())
    if "дима" in head.lower() and "telegram" not in head.lower():
        print("\n✅ В итоге чат «Дима» открыт.")
    else:
        print(f"\n❌ Чат так и не открыт. Заголовок: {_telegram_window()!r}")
        print("   Возможно, click точка была за пределами экрана/скролла — проверь список.")
    return 0


if __name__ == "__main__":
    sys.exit(main())