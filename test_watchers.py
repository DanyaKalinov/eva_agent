import time
from watchers import WatchRule, WatchStore, _target_matches, evaluate_rules, RULES_PATH


def rule(**kw):
    base = {"target": "курс валют", "when": "change", "what": "курс обновился", "capture": "title"}
    base.update(kw)
    return WatchRule(**base)


def test_target_matches_title_and_class():
    st = {"active_window": {"title": "Блокнот — курс валют", "class": "Chrome_WidgetWin_1"}}
    assert _target_matches(st, "курс валют", "title")
    assert _target_matches(st, "валю", "title")          # подстрока
    assert not _target_matches(st, "ютуб", "title")
    assert _target_matches({"active_window": {"class": "Notepad", "title": "x"}}, "notepad", "title")


def test_change_fires_only_when_caption_differs():
    st = {"active_window": {"title": "курс валют", "class": "Chrome"},
          "mode": "normal", "caption": "курс 90.5"}
    fired1, mem1 = evaluate_rules([rule()], "change", st, {}, 0.0)
    assert fired1 == []                                  # первый раз — это снапшот, не событие
    fired2, mem2 = evaluate_rules([rule()], "change", st, mem1, 0.0)
    assert fired2 == []
    st2 = {**st, "caption": "курс 91.2"}
    fired3, mem3 = evaluate_rules([rule()], "change", st2, mem2, 0.0)
    assert len(fired3) == 1 and fired3[0].target == "курс валют"


def test_disappear_fires_when_window_gone():
    st = {"window_titles": [], "active_window": {"title": "другое окно"}}
    fired, _ = evaluate_rules([rule(when="disappear")], "focus", st, {}, 0.0)
    assert len(fired) == 1


def test_disappear_silent_while_window_present():
    st = {"window_titles": ["курс валют"], "active_window": {"title": "курс валют"}}
    fired, _ = evaluate_rules([rule(when="disappear")], "focus", st, {}, 0.0)
    assert fired == []


def test_change_cooldown_blocks_repeat():
    r = rule()
    st = {"active_window": {"title": "курс валют"}, "caption": "курс 95"}
    _, mem = evaluate_rules([r], "change", st, {}, 0.0)
    st2 = {**st, "caption": "курс 96"}
    fired, _ = evaluate_rules([r], "change", st2, mem, 0.0)
    assert fired and fired[0] is r
    r.cooldown_until = 100.0
    st3 = {**st, "caption": "курс 97"}
    fired2, _ = evaluate_rules([r], "change", st3, mem, 50.0)
    assert fired2 == []


def test_store_round_trip(tmp_path):
    store = WatchStore(tmp_path / "rules.json")
    rules = []
    store.add(rules, "ютуб", "appear", "открылся ютуб")
    rules2 = store.load()
    assert rules2[0].target == "ютуб" and rules2[0].when == "appear"
    assert store.remove(store.load(), "ютуб")


# ----- Task 5: WatchManager -----
from watchers import WatchManager
from config import config


def make_screen(active_title, caption, titles):
    class S:
        def get_state(self):
            return {"active_window": {"title": active_title, "class": "Chrome"},
                    "mode": "normal", "caption": caption,
                    "window_titles": titles, "recent_events": []}
        def describe_screen(self, q): return caption
    return S()


def test_manager_appear_fires_notification(tmp_path):
    store = WatchStore(tmp_path / "r.json")
    rules = []
    store.add(rules, "ютуб", "appear", "открылся ютуб")
    screen = make_screen("ютуб — главная", "", [])
    notified = []
    m = WatchManager(screen, store=store, notify=lambda t: notified.append(t))
    m.on_event("focus")
    m._drain()
    assert notified == ["открылся ютуб"]


def test_manager_silent_when_window_absent(tmp_path):
    store = WatchStore(tmp_path / "r2.json")
    rules = []
    store.add(rules, "ютуб", "appear", "открылся ютуб")
    screen = make_screen("Блокнот", "", [])
    notified = []
    m = WatchManager(screen, store=store, notify=lambda t: notified.append(t))
    m.on_event("focus")
    m._drain()
    assert notified == []