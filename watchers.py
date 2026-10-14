"""Наблюдатели «следить за конкретным»: правила, оценка, уведомление."""
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from config import config

RULES_PATH = Path(__file__).parent / "screen_watch_rules.json"
_shared_manager: Optional["WatchManager"] = None


@dataclass
class WatchRule:
    target: str
    when: str = "change"          # change | appear | disappear
    what: str = ""
    capture: str = "title"        # title | screen
    id: str = ""
    created: str = ""
    cooldown_until: Optional[float] = None


class WatchStore:
    def __init__(self, path=RULES_PATH):
        self.path = path

    def load(self) -> list[WatchRule]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) or []
        except Exception:
            return []
        return [WatchRule(**{k: r.get(k) for k in ("target", "when", "what", "capture", "id", "created", "cooldown_until") if k in r})
                for r in raw]

    def save(self, rules: list[WatchRule]) -> None:
        self.path.write_text(json.dumps([asdict(r) for r in rules], ensure_ascii=False, indent=2), encoding="utf-8")

    def add(self, rules: list[WatchRule], target: str, when: str, what: str) -> WatchRule:
        r = WatchRule(target=target, when=when, what=what,
                      id=uuid.uuid4().hex[:8], created=datetime.now().isoformat(timespec="seconds"))
        rules.append(r)
        self.save(rules)
        return r

    def remove(self, rules: list[WatchRule], target: str) -> bool:
        before = len(rules)
        rules[:] = [r for r in rules if r.target != target]
        if len(rules) != before:
            self.save(rules)
        return len(rules) != before


def _target_matches(state: dict, target: str, capture: str) -> bool:
    aw = state.get("active_window") or {}
    hay = " ".join([aw.get("title", ""), aw.get("class", "")]).lower()
    needle = (target or "").strip().lower()
    return bool(needle) and needle in hay


def evaluate_rules(rules: list[WatchRule], event: str, state: dict, memory: dict, now: float):
    """Чистая функция: по событию яруса 1 (change/focus) возвращает
    (сработавшие правила, обновлённый memory). memory = {id: last_caption}.

    Маппинг событий из яруса 1: when='change' → событие 'change';
    'appear' → 'focus' (окно с target появилось/стало активным);
    'disappear' → 'focus'/'change' при отсутствии target-окна. Cooldown
    проверяется по now. 'change' требует, чтобы подпись отличалась от
    последнего снапшота; первый снапшот — это база, не событие.
    """
    fired = []
    for r in rules:
        expected = "change" if r.when == "change" else ("focus" if r.when == "appear" else "disappear")
        if event != expected and not (r.when == "disappear" and event in ("focus", "change")):
            continue
        if r.cooldown_until and now < r.cooldown_until:
            continue
        if r.when == "disappear":
            titles = " ".join(state.get("window_titles", [])).lower()
            if (r.target or "").lower() in titles:
                continue
            if not _target_matches(state, r.target, r.capture):
                fired.append(r)
            continue
        if not _target_matches(state, r.target, r.capture):
            if r.when == "appear":
                continue
        if r.when == "change":
            last = memory.get(r.id)
            cap = state.get("caption", "") or ""
            if not cap:
                continue
            if last is None:
                memory[r.id] = cap  # первый снапшот — база, не событие
                continue
            if cap == last:
                continue
            memory[r.id] = cap
            fired.append(r)
        else:  # appear
            fired.append(r)
    return fired, memory


def _describe_window(rect, question="Что видно в этом окне? Опиши кратко.") -> str:
    """Селективный взгляд на region окна (для change-правил)."""
    try:
        from PIL import ImageGrab
        import base64, io
        img = ImageGrab.grab(bbox=tuple(rect))
        img = img.convert("RGB")
        if img.width > 640:
            h = int(img.height * 640 / img.width)
            img = img.resize((640, h))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        import actions
        return actions._ask_model_about_image(question, base64.b64encode(buf.getvalue()).decode("ascii"))
    except Exception:
        return ""


class WatchManager:
    def __init__(self, screen, store=None, notify=None, worker_delay=1.0):
        self.screen = screen
        self.store = store or WatchStore()
        self.notify = notify or (lambda t: None)
        self.worker_delay = worker_delay
        self._rules = self.store.load()
        self._memory: dict = {}
        self._events = deque()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def on_event(self, event: str) -> None:
        self._events.append(event)

    def _drain(self) -> list[WatchRule]:
        fired_all = []
        while self._events:
            event = self._events.popleft()
            self._rules = self.store.load()  # свежие правила с диска
            state = self.screen.get_state()
            if event in ("change", "focus"):
                state["window_titles"] = _all_window_titles()
            # change-правила: свежая подпись активного окна
            need_caption = any(r.when == "change" for r in self._rules)
            if need_caption:
                aw = state.get("active_window") or {}
                if aw.get("rect"):
                    cap = _describe_window(aw["rect"])
                    if cap:
                        state["caption"] = cap
            fired, self._memory = evaluate_rules(self._rules, event, state, self._memory, time.time())
            for r in fired:
                r.cooldown_until = time.time() + config.watch_cooldown_s
                self._rules = [rr if rr.id != r.id else r for rr in self._rules]
                self.store.save(self._rules)
                self.notify(r.what or f"Заметил: {r.target} ({r.when})")
                fired_all.append(r)
        return fired_all

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain()
            except Exception:
                pass
            time.sleep(self.worker_delay)

    def start(self) -> "WatchManager":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="watch-manager")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def _all_window_titles() -> list[str]:
    try:
        import actions
        return actions.list_window_titles()
    except Exception:
        return []


def start_watch_manager(screen) -> WatchManager:
    """Единственный общий менеджер (по процессу): и старт приложения, и
    действие start_watch пользуются одним экземпляром — так notify из
    app.py применяется к тому же менеджеру, что подписан на события."""
    global _shared_manager
    if _shared_manager is None:
        m = WatchManager(screen)
        screen.on_change(m.on_event)
        m.start()
        _shared_manager = m
    return _shared_manager