import actions


def test_execute_get_screen_state(monkeypatch):
    fake = {"active_window": {"title": "Блокнот"}, "mode": "normal"}
    monkeypatch.setattr("actions.get_screen_state", lambda: ("done", f"Активно: Блокнот"))
    status, text, img = actions.execute_action("get_screen_state", {})
    assert status == "done" and "Блокнот" in text and img is None


def test_execute_start_watch(monkeypatch):
    calls = {}
    monkeypatch.setattr("actions.start_watch",
                        lambda p: (calls.update(p), ("done", "Наблюдатель создан"))[1])
    status, text, img = actions.execute_action("start_watch", {"target": "ютуб", "when": "appear", "what": "открылся ютуб"})
    assert status == "done" and calls == {"target": "ютуб", "when": "appear", "what": "открылся ютуб"}