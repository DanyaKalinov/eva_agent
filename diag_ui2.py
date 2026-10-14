"""Диагностика загадки None в find_*: как _resolve_root и _iter_controls
ведут себя на повторных вызовах для окна Telegram."""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ui_automation as ui  # noqa: E402


def main() -> int:
    wins = ui.list_windows()
    title = next((t for t, cls, p in wins if "telegram" in (p or "").lower()), None)
    if not title:
        print("Telegram-окна нет")
        return 1
    print(f"title={title!r}")

    mm = ui.__dict__
    _resolve_root = mm["_resolve_root"]
    _iter_controls = mm["_iter_controls"]
    _to_dict = mm["_to_dict"]

    # 1) что за root, сколько контролов, сколько edit
    root = _resolve_root(title)
    print("\nroot:", type(root).__name__, "| name:", repr(getattr(root, "Name", None)))
    if root is not None:
        ctr = list(_iter_controls(root))
        print("контролов в дереве:", len(ctr))
        edits = [_to_dict(c) for c in ctr if _to_dict(c)["role"] == "edit"]
        print("edit в дереве:", len(edits))
        for e in edits[:8]:
            print("   edit", e["name"], e["x"], e["y"])

    # 2) теперь повторно — стабильно ли get_window_elements
    print("\nповторный get_window_elements:")
    els = ui.get_window_elements(title)
    print("  элементов:", len(els))
    print("  edit-ов:", sum(1 for e in els if e["role"] == "edit"))

    # 3) и find_message_input / find_search_edit ещё раз
    print("\nfind_message_input:", ui.find_message_input(title))
    print("find_search_edit:", ui.find_search_edit(title))

    # 4) active-окно что отдаёт
    print("\nactive:", repr(ui.get_active_window()))
    return 0


if __name__ == "__main__":
    sys.exit(main())