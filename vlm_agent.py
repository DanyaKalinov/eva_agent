"""
Шаг VLM-цикла (этап 3): по цели и скриншоту окна модель называет
СЛЕДУЮЩИЙ элемент, по которому нужно кликнуть, либо сообщает, что цель достигнута.

Сам клик и проверку результата делает vlm_actions.vlm_click, а цикл с лимитами
и защитой от зацикливания живёт в ui_agent.run_ui_task.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from vlm_locator import Region, enable_dpi_awareness, get_model, prepare_image

MAX_HISTORY_IN_PROMPT = 5   # сколько последних кликов показываем модели


def build_step_prompt(goal: str, history: list[str]) -> str:
    """Промпт для выбора следующего шага. История нужна, чтобы модель не кликала по кругу."""
    done = "; ".join(history[-MAX_HISTORY_IN_PROMPT:]) or "nothing yet"
    return (
        "You control a Windows app using mouse clicks only.\n"
        f'Goal: "{goal}"\n'
        f"Already clicked: {done}\n"
        "Look at the screenshot and choose the single NEXT element to click to get closer to the goal. "
        "Never choose an element that is in the already-clicked list.\n"
        'Reply with JSON only: {"done": false, "target": "short visual description of the element incl. its visible text"}.\n'
        'If the goal is already achieved reply {"done": true}. '
        'If nothing on screen helps reply {"done": false, "target": null}.'
    )


def parse_step_reply(text: str) -> dict:
    """Разбор ответа модели в {"done": bool, "target": str | None}. На мусоре — «ничего не нашла»."""
    empty = {"done": False, "target": None}
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return empty
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return empty
    if not isinstance(data, dict):
        return empty
    target = data.get("target")
    target = target.strip() if isinstance(target, str) and target.strip() else None
    return {"done": data.get("done") is True, "target": target}


def region_of_window(window: str) -> Optional[Region]:
    """Область окна по UIA (left, top, width, height) или None — тогда берётся активное окно."""
    try:
        import ui_automation

        enable_dpi_awareness()
        root = ui_automation._resolve_root(window)
        if root is None:
            return None
        rect = root.BoundingRectangle
        width, height = rect.right - rect.left, rect.bottom - rect.top
        # У свёрнутых окон координаты около -32000; слишком мелкие окна не кропаем
        if rect.left <= -30000 or width < 100 or height < 100:
            return None
        return rect.left, rect.top, width, height
    except Exception:
        return None


def plan_next_click(goal: str, history: list[str], region: Optional[Region] = None) -> dict:
    """Скриншот окна -> вопрос модели -> {"done": bool, "target": str | None}."""
    import ollama
    from PIL import ImageGrab

    if region:
        left, top, width, height = region
        shot = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
    else:
        shot = ImageGrab.grab(all_screens=True)

    png, _ = prepare_image(shot)
    resp = ollama.chat(
        model=get_model(),
        messages=[{"role": "user", "content": build_step_prompt(goal, history), "images": [png]}],
        format="json",
        options={"temperature": 0},
    )
    return parse_step_reply(resp["message"]["content"])
