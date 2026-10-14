import sys
import time

from PIL import Image

import screen_watcher as sw


def _solid(color):
    return Image.new("L", (320, 180), color)


def _two_halfs(left, right):
    img = Image.new("L", (320, 180))
    for x in range(160):
        for y in range(180):
            img.putpixel((x, y), left)
    for x in range(160, 320):
        for y in range(180):
            img.putpixel((x, y), right)
    return img


def _vertical_stripes(dark, light, width=40):
    """Вертикальные полосы: перепады по X, которые dHash видит (в отличие
    от вертикального сплошного раздела)."""
    img = Image.new("L", (320, 180))
    for x in range(320):
        for y in range(180):
            img.putpixel((x, y), light if (x // width) % 2 else dark)
    return img


def test_identical_frames_have_zero_diff():
    a = sw._frame_hash(_solid(100))
    b = sw._frame_hash(_solid(100))
    assert sw._diff_magnitude(a, b) == 0


def test_different_layout_has_high_diff():
    a = sw._frame_hash(_solid(100))
    b = sw._frame_hash(_vertical_stripes(0, 255))
    assert sw._diff_magnitude(a, b) >= sw.FRAME_CHANGE_THRESHOLD


def test_fit_frame_scales_down_wide_images():
    wide = Image.new("L", (2560, 1440), 0)
    out = sw._fit_frame(wide)
    assert out.width <= 320 and out.height == 180


def test_small_perturbation_below_threshold():
    # Кадр с полосами реально несёт биты dHash (не однотонный) — иначе тест
    # проходил бы тривиально (diff 0), как с вертикальным сплошным разделом.
    assert sw._frame_hash(_vertical_stripes(0, 255)) != 0
    img = _vertical_stripes(0, 255)
    img.putpixel((0, 0), 128)  # один пиксель из 28800 отличается
    a = sw._frame_hash(_vertical_stripes(0, 255))
    b = sw._frame_hash(img)
    assert sw._diff_magnitude(a, b) < sw.FRAME_CHANGE_THRESHOLD


# ----- Task 2: захват кадра, детект блокировки, активное окно -----


class _FakeWin32:
    def __init__(self, funcs):
        self._f = funcs

    def __getattr__(self, name):
        if name in self._f:
            return self._f[name]
        raise AttributeError(name)


def test_is_locked_frame_detects_black():
    black = Image.new("L", (320, 180), 0)
    assert sw._is_locked_frame(black)


def test_is_locked_frame_false_on_content():
    img = Image.new("L", (320, 180), 128)
    assert not sw._is_locked_frame(img)


def test_active_window_info(monkeypatch):
    fake = {"GetForegroundWindow": lambda: 5,
            "GetWindowText": lambda h: "Блокнот",
            "GetClassName": lambda h: "Notepad",
            "GetWindowRect": lambda h: (0, 0, 800, 600)}
    # _active_window_info делает `import win32gui` ВНУТРИ функции — моким
    # запись в sys.modules, чтобы импорт подхватил фейк (win32gui недоступен).
    monkeypatch.setitem(sys.modules, "win32gui", _FakeWin32(fake))
    info = sw._active_window_info()
    assert info["title"] == "Блокнот" and info["class"] == "Notepad"
    assert info["rect"] == [0, 0, 800, 600]


# ----- Task 3: класс ScreenWatcher, loop, API -----


def test_loop_records_change_and_active_window(monkeypatch):
    from screen_watcher import ScreenWatcher
    watcher = ScreenWatcher()
    watcher._frame_capture = lambda: (Image.new("L", (320, 180), 100), None)  # стабильный кадр
    watcher._is_locked = lambda img: False
    watcher._compute = lambda img: (0b0000000000000000_0000000000000000, sw.FRAME_CHANGE_THRESHOLD)
    watcher._win_info = lambda: {"title": "Блокнот", "class": "Notepad", "hwnd": 1, "rect": [0, 0, 1, 1]}
    events = []
    watcher.on_change(lambda e: events.append(e))
    watcher._process_once()   # первый прогон: хэш None → запишет, сменит фокус
    watcher._process_once()   # второй прогон: той же кадр → изменений нет
    state = watcher.get_state()
    assert state["active_window"]["title"] == "Блокнот"
    assert "focus" in events
    assert state["recent_events"][-1]["t"] == "focus"


def test_describe_screen_uses_cache_when_fresh(monkeypatch):
    from screen_watcher import ScreenWatcher
    watcher = ScreenWatcher()
    # ВАЖНО: писать в watcher.state напрямую — get_state() возвращает копию
    # (новый dict), запись в неё не дойдёт до self.state.
    watcher.state.caption = "старая подпись"
    watcher.state.caption_ts = time.time()  # свежая
    calls = []
    watcher._refresh_caption = lambda q: calls.append(q) or "новое"
    out = watcher.describe_screen("вопрос")
    assert out == "старая подпись" and calls == []


def test_describe_screen_live_when_stale(monkeypatch):
    from screen_watcher import ScreenWatcher
    watcher = ScreenWatcher()
    watcher.state.caption_ts = time.time() - 999  # протухла
    watcher._refresh_caption = lambda q: "живой взгляд"
    assert watcher.describe_screen("вопрос") == "живой взгляд"