"""Диагностика состояния «Telegram открыт, но чат НЕ открыт» (заголовок
«Telegram (pid)»): почему _locate_contact не находит контакт в этом состоянии.

Только ЧТЕНИЕ дерева UIA — НИЧЕГО не кликает, не вводит, не отправляет.
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ui_automation as ui  # noqa: E402
import ui_agent             # noqa: E402


CONTACT = "Диме"   # как в реальной цели агента «отправить сообщение "привет" Диме»


def main() -> int:
    print("=== ВСЕ окна ===")
    wins = ui.list_windows()
    for i, (t, cls, proc) in enumerate(wins):
        mark = "  <-- TELEGRAM" if "telegram" in (proc or "").lower() else ""
        print(f"  [{i}] {t!r}  {cls!r}  {proc!r}{mark}")

    tg = [(t, c, p) for t, c, p in wins if "telegram" in (p or "").lower()]
    if not tg:
        print("\nTelegram-окна нет — завершаю.")
        return 1
    win = tg[0][0]
    print(f"\nРаботаем с окном: {win!r}")

    # 1) Заголовок окна говорит ли нам про открытый чат?
    shown = ui_agent._window_shows_contact(win, CONTACT)
    print(f"_window_shows_contact({CONTACT!r}) → {shown!r}")
    if shown:
        print("  → чат УЖЕ открыт, контакт искать не надо")
        return 0
    print("  → чат НЕ открыт; идёт путь клика/поиска")

    # 2) Дерево: все контролы по типам, edit-ы с координатами
    root = ui._resolve_root(win)
    print(f"\n_resolve_root → {root}")
    if root is None:
        print("  *** None! ***")
        return 1
    ctrls = list(ui._iter_controls(root))
    print(f"Всего контролов: {len(ctrls)}")

    from collections import Counter
    roles = Counter(ui._to_dict(c)["role"] for c in ctrls)
    print("Роли:", dict(roles.most_common()))

    print("\n--- edit-ы ---")
    edits = [c for c in ctrls if ui._to_dict(c)["role"] == "edit"]
    for c in edits:
        d = ui._to_dict(c)
        print(f"  edit: name={d['name']!r} y={d['y']} x={d['x']} w={d['width']} h={d['height']}")
    if not edits:
        print("  *** edit-ов НЕТ — некуда вводить/искать ***")

    # 3) Что функции поиска видят
    mi = ui.find_message_input(win)
    se = ui.find_search_edit(win)
    print(f"\nfind_message_input → {mi}")
    print(f"find_search_edit    → {se}")

    # 4) Что клик найдет — есть ли вообще имена-контакты в дереве (text/list_item)?
    print("\n--- list_item/text с именами (первые 25) ---")
    found = 0
    for c in ctrls:
        d = ui._to_dict(c)
        if d["role"] in ("list_item", "text") and (d["name"] or "").strip():
            print(f"  {d['role']}: {d['name']!r}  y={d['y']}")
            found += 1
            if found >= 25:
                break
    if not found:
        print("  *** никаких имён/элементов списка нет — контакт не кликнуть ***")

    # 5) Кандидаты клика для «Диме»
    print(f"\n_contact_candidates({CONTACT!r}) → {ui_agent._contact_candidates(CONTACT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())