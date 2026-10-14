"""
Поиск элемента на экране через Qwen2.5-VL (Ollama).

Этап 1 плана «универсальная работа в программах» (вариант B, гибрид):
UIA пробуем первым, а этот модуль — запасной вариант, когда UIA ничего не нашёл.

Пайплайн: скриншот -> ресайз (кратно 28) -> Qwen2.5-VL -> bbox -> центр
-> пересчёт в координаты экрана. Клик здесь НЕ выполняется — только поиск.
"""
from __future__ import annotations

import ctypes
import io
import json
import re
from dataclasses import dataclass
from typing import Optional

DEFAULT_MODEL = "qwen2.5vl:7b"
MAX_SIDE = 1280   # длинная сторона картинки для модели
FACTOR = 28       # Qwen2.5-VL работает с размерами, кратными 28

Region = tuple[int, int, int, int]  # (left, top, width, height) в пикселях экрана


def get_model() -> str:
    """Модель из конфига агента (config.ollama_model) — как в actions.py и local_brain.py."""
    try:
        from config import config
        return getattr(config, "ollama_model", None) or DEFAULT_MODEL
    except Exception:
        return DEFAULT_MODEL


@dataclass
class LocateResult:
    """Результат поиска элемента."""
    found: bool
    x: int = 0          # координаты центра в пикселях ЭКРАНА
    y: int = 0
    raw: str = ""       # сырой ответ модели (для логов и отладки)


def enable_dpi_awareness() -> None:
    """Без этого при масштабе Windows 125–150% клики смещаются. Вызвать один раз при старте."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def compute_target_size(w: int, h: int, max_side: int = MAX_SIDE, factor: int = FACTOR) -> tuple[int, int]:
    """Размер картинки для модели: длинная сторона <= max_side, обе стороны кратны factor."""
    k = min(1.0, max_side / max(w, h))
    nw = max(factor, int(w * k // factor) * factor)
    nh = max(factor, int(h * k // factor) * factor)
    return nw, nh


def prepare_image(shot) -> tuple[bytes, tuple[int, int]]:
    """Уменьшает скриншот до размера для модели. Возвращает (PNG-байты, (ширина, высота))."""
    target = compute_target_size(shot.width, shot.height)
    buf = io.BytesIO()
    shot.resize(target).save(buf, format="PNG")
    return buf.getvalue(), target


def parse_model_reply(text: str, img_w: int, img_h: int) -> Optional[tuple[int, int]]:
    """
    Достаёт центр элемента (в пикселях картинки, которую видела модель).
    Понимает bbox_2d, point_2d и пару x/y. Возвращает None, если элемент не найден.
    """
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    match = re.search(r"\{.*\}|\[.*\]", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else None
    if not isinstance(data, dict) or data.get("found") is False:
        return None

    try:
        if "bbox_2d" in data:
            x1, y1, x2, y2 = data["bbox_2d"]
            x, y = (x1 + x2) / 2, (y1 + y2) / 2
        elif "point_2d" in data:
            x, y = data["point_2d"]
        else:
            x, y = data["x"], data["y"]
    except (KeyError, TypeError, ValueError):
        return None

    # Защита от галлюцинаций: точка должна лежать внутри картинки
    if not (0 <= x <= img_w and 0 <= y <= img_h):
        return None
    return int(x), int(y)


def to_screen_coords(x: int, y: int, img_size: tuple[int, int], region: Region) -> tuple[int, int]:
    """Пересчёт из пикселей уменьшенной картинки в пиксели экрана (с учётом кропа окна)."""
    left, top, width, height = region
    img_w, img_h = img_size
    return left + round(x * width / img_w), top + round(y * height / img_h)


def locate_element(description: str, region: Optional[Region] = None) -> LocateResult:
    """
    Находит элемент по текстовому описанию («кнопка Отправить», «поле поиска»).
    region — область экрана (например, активное окно): точнее, чем весь экран.
    """
    import ollama
    from PIL import ImageGrab

    enable_dpi_awareness()

    if region:
        left, top, width, height = region
        shot = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
    else:
        shot = ImageGrab.grab(all_screens=True)
        left, top, width, height = 0, 0, shot.width, shot.height
        region = (left, top, width, height)

    png, target = prepare_image(shot)

    prompt = (
        f'Locate the UI element: "{description}". '
        'Reply with JSON only: {"bbox_2d": [x1, y1, x2, y2]}. '
        'If the element is not visible, reply {"found": false}.'
    )
    resp = ollama.chat(
        model=get_model(),
        messages=[{"role": "user", "content": prompt, "images": [png]}],
        options={"temperature": 0},
    )
    raw = resp["message"]["content"]

    point = parse_model_reply(raw, *target)
    if point is None:
        return LocateResult(found=False, raw=raw)

    sx, sy = to_screen_coords(point[0], point[1], target, region)
    return LocateResult(found=True, x=sx, y=sy, raw=raw)
