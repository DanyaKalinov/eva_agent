def test_screen_awareness_defaults(monkeypatch):
    monkeypatch.delenv("EVA_SCREEN_FRAME_INTERVAL", raising=False)
    monkeypatch.delenv("EVA_CAPTION_TTL", raising=False)
    monkeypatch.delenv("EVA_CAPTION_REFRESH_S", raising=False)
    monkeypatch.delenv("EVA_WATCH_COOLDOWN_S", raising=False)
    from config import AgentConfig
    c = AgentConfig()
    assert c.screen_frame_interval == 1.0
    assert c.caption_ttl == 12.0
    assert c.caption_refresh_s == 45.0
    assert c.watch_cooldown_s == 300.0