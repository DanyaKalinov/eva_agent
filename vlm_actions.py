"""
Клик по элементу в любой программе (этап 2, вариант B — гибрид).

Порядок: 1) UIA (быстро и точно) -> 2) если не нашёл, Qwen2.5-VL по скриншоту окна
-> клик -> проверка, что экран отреагировал.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional

from vlm_locator import (
    get_model,
    Region,
    enable_dpi_awareness,
    locate_element,
    prepare_image,
)

PAUSE_AFTER_CLICK = 0.6    # сек: ждём, пока интерфейс отреагирует
CHANGE_THRESHOLD = 0.01    # доля изменившихся пикселей, чтобы считать «экран изменился»
PIXEL_DELTA = 24           # насколько должна измениться яркость пикселя


def get_active_window_region() -> Optional[Region]:
    """Область активного окна (left, top, width, height) или None, если окно свёрнуто/не найдено."""
    enable_dpi_awareness()
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    # У свёрнутых окон координаты около -32000, слишком мелкие окна тоже игнорируем
    if rect.left <= -30000 or width < 100 or height < 100:
        return None
    return rect.left, rect.top, width, height


def _grab(region: Optional[Region]):
    """Скриншот области (или всего экрана, если region=None)."""
    from PIL import ImageGrab

    if region:
        left, top, width, height = region
        return ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
    return ImageGrab.grab(all_screens=True)


def screen_changed(before, after, threshold: float = CHANGE_THRESHOLD, pixel_delta: int = PIXEL_DELTA) -> bool:
    """Дешёвая проверка без модели: изменилась ли картинка после клика."""
    from PIL import ImageChops

    size = (320, 180)
    a, b = before.convert("L").resize(size), after.convert("L").resize(size)
    diff = ImageChops.difference(a, b)
    # Считаем пиксели ярче порога через гистограмму (getdata в новых Pillow устарел)
    changed = sum(diff.histogram()[pixel_delta + 1:])
    return changed / (size[0] * size[1]) >= threshold


def vlm_confirm(question: str, shot) -> bool:
    """Спрашиваем модель «да/нет» по скриншоту, например «Открылось ли меню Файл?»."""
    import ollama

    png, _ = prepare_image(shot)
    resp = ollama.chat(
        model=get_model(),
        messages=[{
            "role": "user",
            "content": f"{question} Answer with one word: yes or no.",
            "images": [png],
        }],
        options={"temperature": 0},
    )
    return resp["message"]["content"].strip().lower().startswith("yes")


def vlm_click(
    description: str,
    uia_text: Optional[str] = None,
    window_title: Optional[str] = None,
    expect: Optional[str] = None,
    verify: bool = True,
    use_uia: bool = True,
    region: Optional[Region] = None,
) -> dict:
    """
    Кликает по элементу и возвращает словарь:
      ok        — клик выполнен
      method    — "uia" или "vlm"
      verified  — True/False по результату проверки, None если проверка не делалась
      x, y      — куда кликнули (только для vlm)
      message   — текст для пользователя / логов

    description — описание для модели («кнопка Отправить»)
    uia_text    — точный текст элемента для UIA (по умолчанию = description)
    expect      — что должно произойти («открылось меню Файл»), проверит модель
    use_uia     — False, если UIA уже пробовали (например, в цикле ui_agent)
    region      — область окна для скриншота; None = активное окно
    """
    # 1) Сначала UIA — быстро и без риска промахнуться
    if use_uia:
        try:
            import ui_automation

            win = window_title or ui_automation.get_active_window()
            if win and ui_automation.click_by_text(win, uia_text or description):
                return {"ok": True, "method": "uia", "verified": None, "message": f"UIA: клик по «{uia_text or description}»"}
        except Exception:
            pass  # UIA недоступен или не нашёл элемент — идём к VLM

    # 2) Запасной вариант — VLM по кропу окна
    region = region or get_active_window_region()
    found = locate_element(description, region)
    if not found.found:
        return {"ok": False, "method": "vlm", "verified": None, "message": f"Не нашла на экране: {description}"}

    from actions import mouse_click  # ленивый импорт: actions сам лениво импортирует этот модуль

    before = _grab(region) if verify else None
    click_status, click_text = mouse_click(found.x, found.y)
    if click_status != "done":
        return {"ok": False, "method": "vlm", "verified": None, "message": click_text}
    time.sleep(PAUSE_AFTER_CLICK)

    verified = None
    if verify:
        after = _grab(region)
        verified = vlm_confirm(expect, after) if expect else screen_changed(before, after)

    message = f"VLM: клик в ({found.x}, {found.y})"
    if verified is False:
        message += " — но экран не отреагировал, результат не подтверждён"
    return {"ok": True, "method": "vlm", "verified": verified, "x": found.x, "y": found.y, "message": message}
