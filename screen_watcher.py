"""Картина экрана по запросу (непрерывное осознание выключено).

Раньше фон (ярус 1) постоянно захватывал кадр и грузил CPU — решили снять.
Теперь: get_screen_state() — мгновенный опрос активного окна, describe_screen()
— живой скриншот+VLM ТОЛЬКО при вызове. Фоновый цикл (start_screen_watcher /
WatchManager) не запускается и не вызывается из приложения.
Там же хранится кэш подписи, чтобы повторные вопросы в TTL окне были дешёвыми.
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import config

FRAME_CHANGE_THRESHOLD = 16  # хэмминг dHash 16x16 — сколько бит считаем «изменением»
CAPTION_DEFAULT_QUESTION = "Что открыто на экране и в каком порядке?"


@dataclass
class ScreenState:
    """Снимок текущей картины экрана. Хранится только в памяти."""
    active_window: dict = field(default_factory=dict)   # {title, class, hwnd, rect}
    last_change_ts: Optional[float] = None
    last_magnitude: int = 0
    caption: str = ""                                   # подпись яруса 2
    caption_ts: Optional[float] = None
    caption_question: str = ""
    mode: str = "normal"                                # normal | locked
    events: deque = field(default_factory=lambda: deque(maxlen=64))


def _fit_frame(img, max_w: int = 320):
    """Уменьшает кадр до ширины max_w (пропорционально)."""
    if img.width > max_w:
        h = int(img.height * max_w / img.width)
        img = img.resize((max_w, h))
    return img


def _frame_hash(img, size: int = 16) -> int:
    """dHash по серому: >= смещение пикселя по строке даёт бит 1."""
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    h = 0
    for row in range(size):
        for col in range(size):
            h <<= 1
            if px[row * (size + 1) + col] > px[row * (size + 1) + col + 1]:
                h |= 1
    return h


def _diff_magnitude(h1: int, h2: int) -> int:
    """Биты Хэмминга между двумя dHash."""
    return bin(h1 ^ h2).count("1")


def _capture_frame():
    """Скриншот → уменьшенный кадр. Возвращает (img|None, error|None)."""
    try:
        from PIL import ImageGrab
        return _fit_frame(ImageGrab.grab()), None
    except Exception as e:
        return None, str(e)


def _is_locked_frame(img) -> bool:
    """Чёрный/тёмный экран — заблокирован или пуст. Однотонный яркий кадр
    (например, пустой белый документ) замком не считается."""
    try:
        small = img.convert("L").resize((8, 8))
        vals = list(small.getdata())
        avg = sum(vals) / len(vals)
        spread = max(vals) - min(vals)
        return avg < 12 or (spread < 6 and avg < 60)
    except Exception:
        return True


def _active_window_info() -> dict:
    """Активное окно через win32gui: title, class, hwnd, rect."""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        return {
            "title": win32gui.GetWindowText(hwnd),
            "class": win32gui.GetClassName(hwnd),
            "hwnd": hwnd,
            "rect": list(win32gui.GetWindowRect(hwnd)),
        }
    except Exception:
        return {}


class ScreenWatcher:
    def __init__(self, frame_capture=None, is_locked=None, compute=None, win_info=None):
        self.state = ScreenState()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callbacks: list[Callable[[str], None]] = []
        self._last_hash: Optional[int] = None
        self._last_focus_key: Optional[tuple] = None
        self._last_refresh_ts: Optional[float] = None
        self._refresh_lock = threading.Lock()
        # инжектируемые хуки (для тестов)
        from PIL import Image
        self._frame_capture = frame_capture or _capture_frame
        self._is_locked = is_locked or _is_locked_frame
        self._compute = compute or (lambda img: (_frame_hash(img), None))
        self._win_info = win_info or _active_window_info

    def on_change(self, callback: Callable[[str], None]) -> None:
        self._callbacks.append(callback)

    def _emit(self, event: str) -> None:
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def _process_once(self) -> None:
        img, err = self._frame_capture()
        if err or img is None:
            return
        locked = self._is_locked(img)
        if locked:
            self.state.mode = "locked"
            return
        if self.state.mode == "locked":  # разблокировался
            self.state.mode = "normal"
            self._emit("unlock")
        h, _mag = self._compute(img)
        if self._last_hash is None:
            self._last_hash = h
        else:
            mag = _diff_magnitude(self._last_hash, h)
            self._last_hash = h
            if mag >= FRAME_CHANGE_THRESHOLD:
                self.state.last_change_ts = time.time()
                self.state.last_magnitude = mag
                self.state.events.append({"t": "change", "ts": self.state.last_change_ts, "mag": mag})
                self._emit("change")
        focus = self._win_info()
        key = (focus.get("title"), focus.get("class"))
        if key and key[0] and key != self._last_focus_key:
            self._last_focus_key = key
            self.state.active_window = focus
            self.state.events.append({"t": "focus", "ts": time.time(), "title": focus.get("title")})
            self._emit("focus")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_once()
                if (self._last_refresh_ts is None
                        or time.time() - self._last_refresh_ts >= config.caption_refresh_s):
                    self._last_refresh_ts = time.time()
                    self.describe_screen(CAPTION_DEFAULT_QUESTION)
            except Exception:
                pass
            time.sleep(config.screen_frame_interval)

    def start(self) -> "ScreenWatcher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="screen-watcher")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def get_state(self) -> dict:
        """Публичный снимок для мозга/UI — без скриншота и VLM."""
        return {
            "active_window": self.state.active_window,
            "last_change_ts": self.state.last_change_ts,
            "last_magnitude": self.state.last_magnitude,
            "mode": self.state.mode,
            "caption": self.state.caption,
            "caption_ts": self.state.caption_ts,
            "recent_events": list(self.state.events)[-20:],
        }

    def describe_screen(self, question: str = CAPTION_DEFAULT_QUESTION) -> str:
        """Ярус 2: свежая подпись из кэша, иначе живой взгляд (VLM)."""
        now = time.time()
        if self.state.caption_ts is not None and now - self.state.caption_ts <= config.caption_ttl:
            return self.state.caption
        return self._refresh_caption(question)

    def _refresh_caption(self, question: str) -> str:
        with self._refresh_lock:
            import actions
            status, text = actions.look_at_screen(question)
            if status == "done" and text:
                self.state.caption = text
                self.state.caption_ts = time.time()
                self.state.caption_question = question
            return text


_watcher: Optional[ScreenWatcher] = None
# По-запросный «лентяй» без фонового цикла — единый держатель кэша подписи.
_on_demand: Optional[ScreenWatcher] = None


def _ensure_on_demand() -> ScreenWatcher:
    """Легковесный инстанс без .start() — НИКАКОГО фонового потока."""
    global _on_demand
    if _on_demand is None:
        _on_demand = ScreenWatcher()
    return _on_demand


def start_screen_watcher() -> ScreenWatcher:
    """Явный запуск фонового цикла (раньше звали из app.py / describe).
    Сейчас приложение его НЕ вызывает — слежка отключена по решению."""
    global _watcher
    if _watcher is None:
        _watcher = ScreenWatcher().start()
    return _watcher


def get_screen_state() -> dict:
    """Мгновенная картина активного окна (win32, ~мс, без скриншота)."""
    if _watcher is not None:
        return _watcher.get_state()
    w = _ensure_on_demand()
    try:
        info = w._win_info()
        w.state.active_window = info or w.state.active_window
    except Exception:
        pass
    return w.get_state()


def describe_screen(question: str = CAPTION_DEFAULT_QUESTION) -> str:
    """Живой взгляд по запросу: свежая из кэша, иначе скриншот+VLM.
    Фоновый цикл НЕ запускается — видеосъёмка только здесь и только по вызову."""
    if _watcher is not None:
        return _watcher.describe_screen(question)
    return _ensure_on_demand().describe_screen(question)