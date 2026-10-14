"""Живой E2E-прогон с ДИАГНОСТИКОЙ: что видит _resolve_root, сколько
edit-ов, почему find_message_input вернул None.

Запуск из Claude Code через ! (не программно — классификатор блокирует):
    ! python "C:\\bat\\projects\\Eva\\eva_agent\\run_e2e_send_diag.py"
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ui_automation as ui  # noqa: E402
import ui_agent             # noqa: E402


GOAL = "напиши Диме в телеграм: встречимся в 12"


def diag():
    # 1. list_windows — ВСЕ окна (важен порядок и полный список)
    print("=== ВСЕ окна (порядок list_windows) ===")
    for i, (t, cls, proc) in enumerate(ui.list_windows()):
        marker = "  <-- TELEGRAM" if "telegram" in (proc or "").lower() else ""
        print(f"  [{i}] title={t!r}  class={cls!r}  proc={proc!r}{marker}")

    # 2. _pick_ui_window — какое окно выберет Ева
    win = ui_agent._pick_ui_window(GOAL, ui.list_windows(), None)
    print(f"\n_win from _pick_ui_window: {win!r}")

    if not win:
        print("Окно не найдено — выходим")
        return

    # 3. _resolve_root — видит ли UIA это окно
    root = ui._resolve_root(win)
    print(f"_resolve_root({win!r}): {root}")
    if root is None:
        print("  *** _resolve_root вернул None! ***")
        return

    # 4. Детали корня
    print(f"  root.Name = {root.Name!r}")
    print(f"  root.ClassName = {getattr(root, 'ClassName', 'N/A')!r}")

    # 5. Все контролы: edit / list / итп
    ctrls = list(ui._iter_controls(root))
    print(f"\nВсего контроллов в дереве: {len(ctrls)}")
    edits = []
    for c in ctrls:
        d = ui._to_dict(c)
        if d["role"] == "edit":
            edits.append(d)
            print(f"  EDIT: name={d['name']!r}  y={d['y']}  x={d['x']}")
    if not edits:
        print("  *** 0 edit-ов в дереве! ***")

    # 6. find_message_input / find_search_edit
    mi = ui.find_message_input(win)
    se = ui.find_search_edit(win)
    print(f"\nfind_message_input: {mi}")
    print(f"find_search_edit: {se}")

    # 7. Теперь сам прогон — с логом
    print("\n=== run_ui_task ===")
    print("Начинаю через 3 с...")
    time.sleep(3)
    # Промежуточная диагностика: что УЖЕ видит run_ui_task
    _dest = ui_agent._parse_send_goal(GOAL)[0]
    print(f"_parse_send_goal → dest={_dest!r}")
    wins_now = ui.list_windows()
    print(f"list_windows NOW: {[(t, p) for t, _, p in wins_now]}")
    pick = ui_agent._pick_ui_window(GOAL, wins_now, None)
    print(f"_pick_ui_window NOW → {pick!r}")
    if pick:
        r = ui._resolve_root(pick)
        print(f"_resolve_root({pick!r}) → {r}")
    status, desc = ui_agent.run_ui_task(GOAL)
    print(f"СТАТУС: {status}")
    print(desc)

    if status == "stuck":
        # Повторная диагностика
        print("\n=== Диагностика ПОСЛЕ stuck ===")
        root2 = ui._resolve_root(win)
        if root2 is None:
            print(f"_resolve_root({win!r}) после stuck: None")
        else:
            ctrls2 = list(ui._iter_controls(root2))
            print(f"Контролов после stuck: {len(ctrls2)}")
            for c in ctrls2:
                d = ui._to_dict(c)
                if d["role"] == "edit":
                    print(f"  EDIT: name={d['name']!r}  y={d['y']}")
        print(f"find_message_input после stuck: {ui.find_message_input(win)}")


if __name__ == "__main__":
    diag()
