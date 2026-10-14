"""Диагностика живого UIA-дерева (НЕ продакшен): что Ева реально видит.

Дёргает те же функции, что ui_agent, и печатает:
    1) список окон (заголовок | класс | процесс)
    2) для окна Telegram — дерево элементов с ролью/именем/текстом/y
    3) что возвращают find_search_edit / find_message_input / click_name("Дима")
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ui_automation as ui  # noqa: E402

ROLLS = {"edit", "document", "list item", "listitem", "button", "text"}


def main() -> int:
    print("=== ОКНА ===")
    wins = ui.list_windows()
    for t, cls, proc in wins:
        print(f"  {t!r:70} | {cls} | {proc}")

    tg = next(((t, cls, p) for t, cls, p in wins
               if "telegram" in (p or "").lower()), None)
    if not tg:
        print("\nTelegram-окна НЕ найдено (в трее? не запущен?).")
        return 1
    title, cls, proc = tg
    print(f"\n=== ДЕРЕВО окна {title!r} ({cls}/{proc}) ===")
    for d in ui.get_window_elements(title):
        role = d["role"]
        if role in ROLLS:
            print(f"  role={role:12} name={d['name']!r:50} "
                  f"text={d['text']!r:30} x={d['x']} y={d['y']}")
    print("\n=== КЛЮЧЕВЫЕ ФУНКЦИИ ===")
    print("find_search_edit   ->", ui.find_search_edit(title))
    print("find_message_input ->", ui.find_message_input(title))
    print("click_name('Дима') ->", ui.click_name(title, "Дима"))
    return 0


if __name__ == "__main__":
    sys.exit(main())