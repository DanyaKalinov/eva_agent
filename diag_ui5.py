"""Диагностика пути «поиск → открыть чат» (Bug C, шаг 2).

Реально ВВОДИТ «Дима» в строку поиска Telegram и жмёт Enter — открывает чат
«Дима», НИЧЕГО не отправляя. Печатает результат каждого шага, чтобы найти,
где рвётся _locate_contact: find_search_edit → set_text → send_enter →
find_message_input (композер появился?).

Запуск при открытом главном экране Telegram (чат «Дима» закрыт).
"""
import sys
import time
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import ui_automation as ui  # noqa: E402


def _telegram_window() -> str:
    tg = [(t, c, p) for t, c, p in ui.list_windows() if "telegram" in (p or "").lower()]
    return tg[0][0] if tg else None


def _head(win: str) -> str:
    return (win or "").split(" – ", 1)[0].split(" - ", 1)[0].replace("‎", "").strip()


def _count(win: str, role: str) -> int:
    root = ui._resolve_root(win)
    if root is None:
        return -1
    return sum(1 for c in ui._iter_controls(root) if ui._to_dict(c)["role"] == role)


def main() -> int:
    win = _telegram_window()
    if not win:
        print("Telegram-окно не найдено.")
        return 1
    print(f"Окно: {win!r}  |  заголовок: {_head(win)!r}")

    for role in ("list_item", "edit", "button"):
        print(f"  контролов {role}: {_count(win, role)}")

    # Роли edit-ов в дереве (для понимания, что видит set_text/_bottom_edit)
    root = ui._resolve_root(win)
    edits = [(ui._to_dict(c)["y"], ui._to_dict(c)["name"]) for c in ui._iter_controls(root)
             if ui._to_dict(c)["role"] == "edit"]
    print(f"  edit-ы (y, name): {edits}")

    search = ui.find_search_edit(win)
    print(f"find_search_edit → {search}")
    if search is None:
        print("   ✋ поиск-поля нет — путь поиска недоступен.")
        return 1

    # РЕАЛЬНЫЙ поиск: ввести «Дима» + Enter, с проверкой каждого шага.
    print("\n>>> set_text(win, 'Дима') …")
    t0 = time.time()
    ok_t = ui.set_text(win, "Дима")
    print(f"  set_text → {ok_t}  ({(time.time()-t0)*1000:.0f} мс)")
    time.sleep(0.8)

    print(">>> send_enter(win) …")
    ok_e = ui.send_enter(win)
    print(f"  send_enter → {ok_e}")
    time.sleep(1.5)

    win2 = _telegram_window()
    head2 = _head(win2)
    print(f"\nЗаголовок после поиска: {head2!r}  (было: {_head(win)!r})")
    opened = "дима" in head2.lower() and "telegram" not in head2.lower()
    print(f"  чат открылся: {opened}")
    if not opened:
        print("  ✋ Enter не открыл чат (или открыл не тот).")
        return 1

    mi = ui.find_message_input(win2)
    print(f"find_message_input → {mi}")
    if mi is None:
        print("   ✋ композер не нашли даже при открытом чате — Bug B регрессия?")
        return 1
    print("\n✅ ПОИСК РАБОТАЕТ полностью: set_text→send_enter→композер появился.")
    print("   Остаётся понять, почему в _locate_contact тот же путь давал «нет».")
    return 0


if __name__ == "__main__":
    sys.exit(main())