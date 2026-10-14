"""Живой E2E-прогон: Ева отправляет сообщение в открытый в Telegram чат.

Это диагностический раннер (не продакшен-код): дёргает уже
протестированный ui_agent.run_ui_task и печатает статус + честную проверку.

Запуск из ЛЮБОГО места (ни cd, ни PYTHONIOENCODING не нужны — UTF-8
ставится сам, импорты берутся из папки скрипта):
    python "C:\\bat\\projects\\Eva\\eva_agent\\run_e2e_send.py"

ДО запуска:
    - в Telegram ОТКРЫТО окно нужного чата (или «Избранное»), а не трей —
      при сворачивании в трей Telegram уничтожает окно, и UIA его не видит;
    - помни: текст уйдёт в ОТКРЫТЫЙ чат (Ева адресата по имени не выбирает).
"""
import os
import sys
import time

# Windows-консоль по умолчанию cp1251 рубит кириллицу/эмодзи в выводе.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ui_agent             # noqa: E402  (после sys.path.insert)
import ui_automation as ui  # noqa: E402


GOAL = "напиши Диме в телеграм: встретимся в 12"
MSG = "встретимся в 12"


def _find_bubble() -> str:
    """Честная проверка (Ruling 6): реально ли сообщение ушло. Ищем в окнах
    Telegram элемент, в имени которого есть текст сообщения."""
    for title, _cls, proc in ui.list_windows():
        if "telegram" in (proc or "").lower():
            for el in ui.get_window_elements(title):
                if MSG in (el.get("name") or ""):
                    return f"нашла плашку с текстом: «{el.get('name')}»"
    return ("плашку через UIA не вижу (Telegram рисует сообщения кастомно) — "
            "проверь глазами в открытом окне")


def main() -> int:
    print(f"ЦЕЛЬ: {GOAL}")
    print("В Telegram ОТКРЫТ нужный чат / «Избранное»? Окно не в трее?")
    print("Начинаю через 3 с — после этого не переключай окна...")
    time.sleep(3)

    status, desc = ui_agent.run_ui_task(GOAL)
    print("\nСТАТУС:", status)
    print(desc)

    if status == "done":
        print("\nПроверка отправки (Ruling 6):", _find_bubble())
    return 0


if __name__ == "__main__":
    sys.exit(main())