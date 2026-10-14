# Юниты UI-цикла. Все UIA-функции и _vlm_fallback замоканы — без живого
# окна, без VLM. (Девайд от brief: тест-изоляция — патчим весь
# потребляемый adapter-интерфейс + VLM; иначе тесты били бы по реальному
# uiautomation/Ollama.)
import ui_agent
import ui_automation as ui

from ui_agent import (_parse_send_goal, _dest_phrase, _open_dest,
                      _ensure_dest_window, _contact_candidates)


def test_parse_send_goal_full():
    dest, contact, payload = _parse_send_goal("напиши в вк Ане: привет")
    assert dest == "в вк"
    assert contact == "Ане"
    assert payload == "привет"


def test_parse_send_goal_telegram():
    dest, contact, payload = _parse_send_goal("напиши Диме в телеграм: встретимся в 12")
    assert dest == "в телеграм"
    assert contact == "Диме"
    assert payload == "встретимся в 12"


def test_parse_send_goal_no_destination():
    dest, contact, payload = _parse_send_goal("напиши привет")
    assert dest is None


def test_dest_phrase_detects():
    assert _dest_phrase("напиши в вк Ане привет") == "в вк"
    assert _dest_phrase("напиши Диме в телеграм: привет") == "в телеграм"
    assert _dest_phrase("напиши привет") is None


def test_open_dest_site(monkeypatch):
    """Адресат-сайт (в вк) открывается через open_website."""
    import actions as _act
    opened = {}
    monkeypatch.setattr(_act, "open_website",
                        lambda url: (opened.setdefault("s", url) or "done", "ок"))
    ok, _note = _open_dest("в вк")
    assert ok and opened.get("s") == "https://vk.com"


def test_ensure_dest_window_opens_when_missing(monkeypatch):
    """Окно адресата не открыто -> _open_dest открывает приложение ->
    повторный выбор окна попадает в открытое."""
    import actions as _act
    opened = []
    seen = []
    monkeypatch.setattr(ui_agent, "_parse_send_goal",
                        lambda goal: ("в телеграм", "Диме", "привет"))
    monkeypatch.setattr(ui, "get_active_window", lambda: None)
    monkeypatch.setattr(ui, "list_windows", lambda: [])

    def fake_open_app(app):
        seen.append(app)
        opened.append("Telegram (11905)")
        return "done", "Открыл"

    monkeypatch.setattr(_act, "open_app", fake_open_app)

    def fake_pick(goal, windows, active):
        return "Telegram (11905)" if opened else None

    monkeypatch.setattr(ui_agent, "_pick_ui_window", fake_pick)
    win, note = _ensure_dest_window("напиши Диме в телеграм: привет", None)
    assert seen == ["telegram"], "без открытого окна — открываем telegram"
    assert win == "Telegram (11905)"


# ----- Send-поток: найти контакт -> композер -> отправить -----
def _fake_env(monkeypatch):
    """Окно мессенджера с композером: get_window_elements, композер, VLM."""
    monkeypatch.setattr(ui, "get_active_window", lambda: "Тест")
    monkeypatch.setattr(ui, "get_window_elements", lambda w: CHAT_ELEMS)
    monkeypatch.setattr(ui, "find_message_input",
                        lambda w: next((e for e in CHAT_ELEMS if e["role"] == "edit"), None))
    # set_text — для композера (старый интерфейс, 2 аргумента)
    monkeypatch.setattr(ui, "set_text", lambda w, t: True)
    # set_text_search — для строки поиска (новая функция, тоже 2 аргумента)
    monkeypatch.setattr(ui, "set_text_search", lambda w, t: True)
    monkeypatch.setattr(ui_agent, "_vlm_fallback", lambda w, g, log: False)


def test_run_ui_task_finds_contact_click_then_sends(monkeypatch):
    """Контакт кликается строгим UIA -> композер -> отправка Enter. Окно со
    СПИСКОМ чатов («Telegram»), не с открытым чатом: иначе _window_shows_contact
    справедливо решил бы, что чат уже открыт, и клик не понадобился бы."""
    typed = []
    entered = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "find_search_edit", lambda w: None)
    monkeypatch.setattr(ui, "restore_window", lambda w: True)
    monkeypatch.setattr(ui, "list_windows",
                        lambda: [("Telegram", "MainWindow", "Telegram.exe")])
    clicked = []
    monkeypatch.setattr(ui, "click_name",
                        lambda w, t: (clicked.append(t) or True))
    monkeypatch.setattr(ui, "set_text", lambda w, t: (typed.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: (entered.append(w) or True))
    status, desc = ui_agent.run_ui_task("напиши Диме в телеграм: встретимся в 12")
    assert status == "done", desc
    assert clicked, "контакт должен кликнуться"
    assert clicked[0] == "Диме", "первая попытка — исходная форма"
    assert "встретимся в 12" in typed
    assert entered


def test_run_ui_task_searches_contact_when_no_click(monkeypatch):
    """Клик не сработал -> приложение-поиск по имени + Enter -> отправка."""
    typed = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "restore_window", lambda w: True)
    monkeypatch.setattr(ui, "list_windows",
                        lambda: [("Дима – (11817)", "MainWindow", "Telegram.exe")])
    monkeypatch.setattr(ui, "click_name", lambda w, t: False)
    monkeypatch.setattr(ui, "find_search_edit", lambda w: {"role": "edit", "name": "Поиск", "y": 20})
    monkeypatch.setattr(ui, "set_text", lambda w, t, s: (typed.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: (typed.append("ENTER") or True))
    monkeypatch.setattr(ui, "find_message_input", lambda w: {"role": "edit", "y": 200})
    status, desc = ui_agent.run_ui_task("напиши Ане в телеграм: привет")
    assert status == "done", desc
    assert typed and typed[0], "искала контакт в поиске"
    assert "ENTER" in typed
    assert "привет" in typed


def test_run_ui_task_stuck_when_no_composer_after_search(monkeypatch):
    """Контакт не нашёлся (композера нет) -> stuck, текст никуда не ушёл."""
    sent = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "restore_window", lambda w: True)
    monkeypatch.setattr(ui, "list_windows",
                        lambda: [("Дима – (11817)", "MainWindow", "Telegram.exe")])
    monkeypatch.setattr(ui, "find_message_input", lambda w: None)  # композер нет
    monkeypatch.setattr(ui, "click_by_text", lambda w, t: False)
    monkeypatch.setattr(ui, "find_search_edit", lambda w: {"role": "edit", "name": "Поиск", "y": 20})
    monkeypatch.setattr(ui, "set_text", lambda w, t, s: (sent.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: (sent.append("ENTER") or True))
    status, desc = ui_agent.run_ui_task("напиши Диме в телеграм: встретимся в 12")
    assert status == "stuck"
    assert "встретимся в 12" not in sent, "текст не должен уйти без чата"


def test_run_ui_task_opens_dest_app_when_window_missing(monkeypatch):
    """Телеграм ЗАКРЫТ совсем: send-цель «в телеграм» НЕ должна брать активное
    окно (терминал), а ОТКРЫТЬ приложение (open_app) и работать в появившемся
    окне. Раньше dest-ветка фолбэчилась на active терминал и писала не туда."""
    opened = []
    monkeypatch.setattr(ui, "get_active_window", lambda: "C:\\bat\\cmd")

    def windows_after_open():
        out = [("C:\\bat\\cmd", "ConsoleWindowClass", "claude.exe")]
        if opened:
            out.insert(0, ("Дима – (11817)", "MainWindow", "Telegram.exe"))
        return out

    monkeypatch.setattr(ui, "list_windows", windows_after_open)
    monkeypatch.setattr(ui, "get_window_elements", lambda w: CHAT_ELEMS)
    monkeypatch.setattr(ui, "find_message_input",
                        lambda w: next((e for e in CHAT_ELEMS if e["role"] == "edit"), None))
    import actions as _act
    monkeypatch.setattr(_act, "open_app",
                        lambda app: (opened.append(app) or "done", "ок"))
    monkeypatch.setattr(ui, "click_name", lambda w, t: True)
    monkeypatch.setattr(ui, "click_by_text", lambda w, t: True)
    monkeypatch.setattr(ui, "set_text", lambda w, t, s: True)
    monkeypatch.setattr(ui, "send_enter", lambda w: True)
    status, desc = ui_agent.run_ui_task("напиши Диме в телеграм: встретимся в 12")
    assert opened == ["telegram"], "должна сама открыть telegram, а не писать в терминал"
    assert status == "done", desc


def test_run_ui_task_restores_dest_window(monkeypatch):
    """Свёрнутое окно адресата: перед отправкой Ева разворачивает окно
    (restore), чтобы UIA-клики не уходили в пустоту свёрнутого окна."""
    restored = []
    _mock_ui(monkeypatch, CHAT_ELEMS)
    monkeypatch.setattr(ui, "send_enter", lambda w: True)
    monkeypatch.setattr(ui, "restore_window",
                        lambda w: restored.append(w) or True)
    monkeypatch.setattr(ui, "list_windows",
                        lambda: [("Дима – (11817)", "MainWindow", "Telegram.exe"),
                                 ("C:\\bat\\cmd", "ConsoleWindowClass", "claude.exe")])
    monkeypatch.setattr(ui, "get_active_window", lambda: "C:\\bat\\cmd")
    status, _ = ui_agent.run_ui_task("напиши Диме в телеграм: встретимся в 12")
    assert status == "done"
    assert restored, "окно адресата должно быть развёрнуто перед работой"


def test_contact_candidates_normalizes_declension():
    """«Диме» (дат.) -> кандидаты включают именительную «Дима» для клика/поиска."""
    c = ui_agent._contact_candidates("Диме")
    assert c[0] == "Диме", "исходная форма первой"
    assert "Дима" in c


def test_contact_candidates_dative_forms():
    assert "Катя" in ui_agent._contact_candidates("Кате")
    assert "Дмитрий" in ui_agent._contact_candidates("Дмитрию")
    assert "Саша" in ui_agent._contact_candidates("Саше")


def test_contact_candidates_keeps_nominative_alone():
    """«Дима» уже в именительном — ничего лишнего не добавляем."""
    c = ui_agent._contact_candidates("Дима")
    assert c == ["Дима"]


def test_contact_candidates_short_names_untouched():
    assert ui_agent._contact_candidates("Ля") == ["Ля"]


def test_run_ui_task_clicks_exact_form_then_base(monkeypatch):
    """ПРОМАХ: клик по контакту точный (no подстроки). Список может содержать
    «Дима» и «Дима Шуриков» — Ева кликает базовую форму «Дима» (точный матч),
    а не «Диме» (её нет в списке) и не похожего «Дима Ш.». Расширяется на
    кандидатов склонений до первого успеха. Окно — список чатов «Telegram» (чат
    ещё не открыт), иначе короткое замыкание по заголовку не дало бы кликнуть."""
    clicked = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "restore_window", lambda w: True)
    monkeypatch.setattr(ui, "list_windows",
                        lambda: [("Telegram", "MainWindow", "Telegram.exe")])
    # точный матч: «Диме» в списке нет, «Дима» есть — кликнет по «Дима»
    monkeypatch.setattr(ui, "click_name",
                        lambda w, t: (clicked.append(t) or False) if t == "Диме"
                        else (clicked.append(t) or True))
    monkeypatch.setattr(ui, "set_text", lambda w, t, s: True)
    monkeypatch.setattr(ui, "send_enter", lambda w: True)
    status, desc = ui_agent.run_ui_task("напиши Диме в телеграм: встретимся в 12")
    assert status == "done", desc
    assert "Дима" in clicked, "клик по именительной «Дима», а не по «Диме»"


def test_search_uses_base_contact_form(monkeypatch):
    """Клик совсем не сработал -> поиск приложения: сначала вводим contact как есть
    («Диме»), если не сработало — пробуем именительную форму («Дима»).
    Окно — список чатов (чат не открыт по заголовку)."""
    typed = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "restore_window", lambda w: True)
    monkeypatch.setattr(ui, "list_windows",
                        lambda: [("Telegram", "MainWindow", "Telegram.exe")])
    monkeypatch.setattr(ui, "click_name", lambda w, t: False)
    monkeypatch.setattr(ui, "find_search_edit", lambda w: {"role": "edit", "name": "Поиск", "y": 20})
    monkeypatch.setattr(ui, "find_message_input", lambda w: {"role": "edit", "y": 200})
    # Сначала вводим как есть ("Диме"), потом фолбэк на базовую форму ("Дима")
    search_typed = []
    monkeypatch.setattr(ui, "set_text_search", lambda w, t: (search_typed.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: (search_typed.append("ENTER") or True))
    # Потом вводим payload ("привет")
    payload_typed = []
    monkeypatch.setattr(ui, "set_text", lambda w, t: (payload_typed.append(t) or True))
    status, desc = ui_agent.run_ui_task("напиши Диме в телеграм: привет")
    assert status == "done", desc
    # Сначала вводим как есть ("Диме"), потом фолбэк на базовую форму ("Дима")
    assert "Диме" in search_typed, f"поиск как есть, search_typed: {search_typed}"
    assert "Дима" in search_typed, f"фолбэк на базовую форму, search_typed: {search_typed}"
    assert "привет" in payload_typed, f"payload должен быть в payload_typed: {payload_typed}"


def test_dest_phrase_viber():
    assert _dest_phrase("напиши в вайбер Диме: привет") == "в вайбер"
    assert _dest_phrase("напиши Диме в viber: привет") == "в viber"


def test_dest_phrase_signal():
    assert _dest_phrase("напиши в сигнал Игорю: тест") == "в сигнал"
    assert _dest_phrase("напиши Игорю в signal: тест") == "в signal"


def test_open_dest_telegram_app_or_web_fallback(monkeypatch):
    """Telegram: приложение не установлено → web-фолбэк на web.telegram.org."""
    import actions as _act
    monkeypatch.setattr(_act, "open_app",
                        lambda app: ("error", "не найдено"))
    opened = {}
    monkeypatch.setattr(_act, "open_website",
                        lambda url: (opened.setdefault("s", url) or "done", "ок"))
    ok, _ = _open_dest("в телеграм")
    assert ok, "должен открыть web.telegram.org как фолбэк"
    assert opened.get("s") == "https://web.telegram.org"


def test_open_dest_viber_app_or_web_fallback(monkeypatch):
    """Viber: приложение не установлено → web-фолбэк на web.viber.com."""
    import actions as _act
    monkeypatch.setattr(_act, "open_app",
                        lambda app: ("error", "не найдено"))
    opened = {}
    monkeypatch.setattr(_act, "open_website",
                        lambda url: (opened.setdefault("s", url) or "done", "ок"))
    ok, _ = _open_dest("в вайбер")
    assert ok, "должен открыть web.viber.com как фолбэк"
    assert opened.get("s") == "https://web.viber.com"


def test_open_dest_signal_no_web_fallback(monkeypatch):
    """Signal: режим app, приложение не найдено → нет web-фолбэка (stuck)."""
    import actions as _act
    monkeypatch.setattr(_act, "open_app",
                        lambda app: ("error", "не найдено"))
    ok, note = _open_dest("в сигнал")
    assert not ok, "signal не имеет web-фолбэка"
    assert "не найдено" in note


def test_pick_ui_window_finds_whatsapp_by_process(monkeypatch):
    windows = [
        ("Чат с Димой", "MainWindow", "Telegram.exe"),
        ("WhatsApp", "Chrome_WidgetWin_1", "WhatsApp.exe"),
        ("C:\\bat\\cmd", "ConsoleWindowClass", "claude.exe"),
    ]
    result = ui_agent._pick_ui_window("напиши в вотсап Диме: привет", windows, None)
    assert result == "WhatsApp", f"должен найти WhatsApp по процессу, got {result}"


def test_pick_ui_window_finds_vk_by_class(monkeypatch):
    windows = [
        ("ВКонтакте | Моя страница", "Chrome_WidgetWin_1", "chrome.exe"),
        ("C:\\bat\\cmd", "ConsoleWindowClass", "claude.exe"),
    ]
    result = ui_agent._pick_ui_window("напиши в вк Ане: привет", windows, None)
    assert result == "ВКонтакте | Моя страница"


def test_pick_ui_window_finds_discord_by_process(monkeypatch):
    windows = [
        ("Discord", "CefSharp", "Discord.exe"),
        ("C:\\bat\\cmd", "ConsoleWindowClass", "claude.exe"),
    ]
    result = ui_agent._pick_ui_window("напиши в дискорд Игорю: привет", windows, None)
    assert result == "Discord"


def test_pick_ui_window_finds_viber_by_class(monkeypatch):
    windows = [
        ("Viber", "ViberDesktop", "viber.exe"),
        ("C:\\bat\\cmd", "ConsoleWindowClass", "claude.exe"),
    ]
    result = ui_agent._pick_ui_window("напиши в вайбер Диме: привет", windows, None)
    assert result == "Viber"


def test_contact_candidates_masculine_solid_suffixes():
    """«у/ю»-окончания дают и «пустую»/«ь»-формы: Максиму->Максим, Глебу->Глеб,
    Игорю->Игорь, Андрею->Андрей (раньше до них не доходили)."""
    assert "Максим" in ui_agent._contact_candidates("Максиму")
    assert "Глеб" in ui_agent._contact_candidates("Глебу")
    assert "Игорь" in ui_agent._contact_candidates("Игорю")
    assert "Андрей" in ui_agent._contact_candidates("Андрею")


def test_contact_candidates_diminutives():
    """Уменьшительные имена: Саня→Саша, Димка→Дима, Ленк→Лена."""
    c = ui_agent._contact_candidates("Саня")
    assert c[0] == "Саня", "исходная форма первая"
    assert "Саша" in c, "Саня — уменьшительное от Саша"

    c2 = ui_agent._contact_candidates("Димка")
    assert "Дима" in c2, "Димка — уменьшительное от Дима"

    c3 = ui_agent._contact_candidates("Ленка")
    assert "Лена" in c3, "Ленка — уменьшительное от Лена"


def test_window_shows_contact_detects_open_chat():
    """Заголовок окна = «Дима – (20735)» (с LRM-мусором) -> чат Дима УЖЕ
    открыт, возвращаем именительную форму. Раньше код этого не видел и искал
    контакт по имени, а Telegram имена чатов через UIA не отдаёт -> stuck."""
    assert (ui_agent._window_shows_contact("‎Дима – (20735)", "Диме")
            == "Дима")


def test_window_shows_contact_chat_list_is_none():
    """Окно со списком чатов (заголовок просто «Telegram», без « – (PID)») —
    контакт не показан, ищем дальше."""
    assert ui_agent._window_shows_contact("Telegram", "Диме") is None


def test_window_shows_contact_wrong_chat_is_none():
    assert ui_agent._window_shows_contact("‎Пётр – (22)", "Диме") is None


def test_window_shows_contact_no_false_positive_on_partial_name():
    """«Иванов» НЕ сходится за «Иван»: иначе приняли бы не тот чат за нужный."""
    assert ui_agent._window_shows_contact("‎Иванов – (22)", "Ивану") is None


def test_locate_contact_skips_when_chat_already_open(monkeypatch):
    """Чат уже открыт (заголовок окна = имя контакта) -> клик по списку и
    поиск НЕ нужны: вернула True сразу, с честным логом."""
    _fake_env(monkeypatch)
    spy: list = []

    def no_click(w, t):
        spy.append(("click", t))
        return False

    def no_search(w):
        spy.append(("search",))
        return None

    monkeypatch.setattr(ui, "click_name", no_click)
    monkeypatch.setattr(ui, "find_search_edit", no_search)
    log = []
    assert ui_agent._locate_contact("‎Дима – (20735)", "Диме", log) is True
    assert spy == [], f"клик/поиск не должны вызываться: {spy}"
    assert any("уже открыт" in x for x in log), log


def test_locate_contact_search_tries_forms_until_composer(monkeypatch):
    """Поиск: сначала вводим contact как есть («Ане»), если не сработало —
    пробуем именительную форму («Аня»). Композер появляется на 2-й попытке."""
    typed = []
    comp = iter([None, {"role": "edit", "y": 200}])  # композер появляется на 2-й форме
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "click_name", lambda w, t: False)
    monkeypatch.setattr(ui, "find_search_edit",
                        lambda w: {"role": "edit", "name": "Поиск", "y": 20})
    monkeypatch.setattr(ui, "set_text", lambda w, t, s: (typed.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: True)
    monkeypatch.setattr(ui, "find_message_input", lambda w: next(comp))
    ok = ui_agent._locate_contact("Тест", "Ане", [])
    assert ok is True
    # Сначала вводим как есть ("Ане"), потом фолбэк на "Аня"
    assert "Аня" in typed, f"должна быть фолбэк-форма «Аня», набрала: {typed}"
    assert "Ане" in typed, f"должна быть исходная форма «Ане», набрала: {typed}"


def test_locate_contact_search_false_when_no_composer(monkeypatch):
    """Поиском Enter нажат, но композер так и не открылся (нет такого чата) —
    честный False, а не «нашла»."""
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "click_name", lambda w, t: False)
    monkeypatch.setattr(ui, "find_search_edit",
                        lambda w: {"role": "edit", "name": "Поиск", "y": 20})
    monkeypatch.setattr(ui, "set_text", lambda w, t, s: True)
    monkeypatch.setattr(ui, "send_enter", lambda w: True)
    monkeypatch.setattr(ui, "find_message_input", lambda w: None)
    assert ui_agent._locate_contact("Тест", "Диме", []) is False


def test_locate_contact_click_succeeds_on_candidate_form(monkeypatch):
    """Клик точным именем: «Диме» нет, «Дима» есть — кликается базовая форма,
    поиск не нужен."""
    clicked = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "click_name",
                        lambda w, t: (clicked.append(t) or False) if t == "Диме"
                        else (clicked.append(t) or True))
    log = []
    ok = ui_agent._locate_contact("Тест", "Диме", log)
    assert ok is True
    assert "Дима" in clicked
    assert any("кликнула чат«" in x or "кликнула чат «" in x for x in log)


CHAT_ELEMS = [
    {"role": "list_item", "name": "Дмитрий", "text": "Дмитрий",
     "x": 0, "y": 0, "width": 10, "height": 10},
    {"role": "edit", "name": "", "text": "", "x": 0, "y": 0,
     "width": 10, "height": 10},
]


def test_locate_contact_visual_found_by_vlm(monkeypatch):
    """Визуальный поиск через VLM: скриншот делается, координаты есть, клик успешен."""
    _fake_env(monkeypatch)

    # Мокаем find_element_on_screen - возвращает координаты
    monkeypatch.setattr(
        ui_agent._act if hasattr(ui_agent, '_act') else __import__('actions'),
        'find_element_on_screen',
        lambda desc: ("found", {"x": 100, "y": 200, "width": 50, "height": 30})
    )

    # Мокаем pyautogui.click
    monkeypatch.setattr("pyautogui.click", lambda x, y: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    # Мокаем find_message_input чтобы вернуть None (не найдено)
    monkeypatch.setattr(ui, "find_message_input", lambda w: None)

    log = []
    # VLM найдёт, но клик не откроет чат (find_message_input None)
    ok = ui_agent._locate_contact_visual("Тест", "Саша", log)

    assert any("VLM нашёл контакт" in x for x in log)
    # Чат не открылся, т.к. find_message_input вернул None
    assert ok is False


def _mock_ui(monkeypatch, elems, click=None, set_text=None):
    """Мокаем весь потребляемый UIA-интерфейс; кастомные click/set_text
    запоминают вызовы, если переданы."""
    monkeypatch.setattr(ui, "get_active_window", lambda: "Тест")
    monkeypatch.setattr(ui, "get_window_elements", lambda w: elems)
    monkeypatch.setattr(ui, "click_by_text", click or (lambda w, t: True))
    # Точный клик контакта — тоже через click_name (см. _locate_contact)
    monkeypatch.setattr(ui, "click_name", click or (lambda w, t: True))
    monkeypatch.setattr(ui, "find_edit_control",
                        lambda w: next((e for e in elems if e["role"] == "edit"), None))
    monkeypatch.setattr(ui, "find_message_input",
                        lambda w: next((e for e in elems if e["role"] == "edit"), None))
    # set_text — для композера (старый интерфейс, 2 аргумента)
    monkeypatch.setattr(ui, "set_text", set_text or (lambda w, t: True))
    # set_text_search — для строки поиска (новая функция, тоже 2 аргумента)
    monkeypatch.setattr(ui, "set_text_search", set_text or (lambda w, t: True))
    monkeypatch.setattr(ui_agent, "_vlm_fallback", lambda w, g, log: False)


def test_run_ui_task_finds_and_clicks(monkeypatch):
    clicked = []

    def fake_click(w, t):
        clicked.append((w, t))
        return True

    _mock_ui(monkeypatch, CHAT_ELEMS, click=fake_click)

    status, desc = ui_agent.run_ui_task("отправить сообщение «привет» Дмитрию")
    assert clicked                     # был клик по списку
    assert status in ("done", "stuck")


def test_run_ui_task_stops_when_stuck(monkeypatch):
    _mock_ui(monkeypatch, [], click=(lambda w, t: False))
    status, desc = ui_agent.run_ui_task("найти кнопку Отправить")
    assert status == "stuck"


def test_run_ui_task_types_text_when_field_present(monkeypatch):
    typed = []

    def fake_set_text(w, t):
        typed.append(t)
        return True

    _mock_ui(monkeypatch, CHAT_ELEMS, set_text=fake_set_text)

    status, desc = ui_agent.run_ui_task("введи в поле название «привет»")
    assert typed == ["привет"]
    assert status == "done"


def test_run_ui_task_blocks_secret_payload(monkeypatch):
    """Стоп-лист секретов (spec §4): пароли/PIN/карты не вводятся."""
    _mock_ui(monkeypatch, CHAT_ELEMS)
    status, desc = ui_agent.run_ui_task("введи пароль «s3cret» в поле")
    assert status == "error"
    assert "секрет" in desc


def test_vlm_fallback_calls_describe_screen_directly(monkeypatch):
    """Bug B: describe_screen — синхронная функция (строку), asyncio.run вокруг
    неё падал «An asyncio.Future, a coroutine or an awaitable is required»."""
    monkeypatch.setattr(ui_agent, "describe_screen",
                        lambda q: "Вижу кнопку «Отправить»")
    log = []
    ok = ui_agent._vlm_fallback("Окно", "цель", log)
    assert ok is True                     # раньше: False из-за тип-ошибки asyncio
    assert any("Вижу кнопку" in x for x in log)


def test_run_ui_task_not_done_when_vlm_failed(monkeypatch):
    """Bug A: если VLM-взгляд упал — это не «сделала», статус не done.
    Гоняем НАСТОЯЩИЙ _vlm_fallback (лог «не смогла посмотреть»), не заглушку:
    _mock_ui стубит _vlm_fallback, поэтому сохраняем оригинал до её вызова."""
    real_vlm = ui_agent._vlm_fallback
    _mock_ui(monkeypatch, [], click=lambda w, t: False)
    monkeypatch.setattr(ui_agent, "_vlm_fallback", real_vlm)

    def boom(q):
        raise RuntimeError("сбой VLM")

    monkeypatch.setattr(ui_agent, "describe_screen", boom)
    status, _ = ui_agent.run_ui_task("сделай что-нибудь необычное")
    assert status != "done"               # раньше: ложный done «Сделала: не смогла»


def test_run_ui_task_not_done_on_look_only(monkeypatch):
    """Bug A: один только взгляд (без клика/ввода) не считается действием."""
    real_vlm = ui_agent._vlm_fallback
    _mock_ui(monkeypatch, [], click=lambda w, t: False)
    monkeypatch.setattr(ui_agent, "_vlm_fallback", real_vlm)
    monkeypatch.setattr(ui_agent, "describe_screen",
                        lambda q: "Вижу пустой экран, элементов нет")
    status, _ = ui_agent.run_ui_task("найти кнопку Отправить")
    assert status != "done"


def test_run_ui_task_sends_enter_after_typing_for_send_goal(monkeypatch):
    """Spec §3: после ввода текста в «напиши/отправь» — Enter, чтобы сообщение
    реально ушло (в Telegram одного набора текста мало)."""
    sent = []
    _mock_ui(monkeypatch, CHAT_ELEMS, set_text=lambda w, t: True)
    monkeypatch.setattr(ui, "send_enter",
                        lambda w: (sent.append(w) or True))
    status, _ = ui_agent.run_ui_task("напиши Диме «встретимся в 12»")
    assert status == "done"
    assert sent, "не нажала Enter для отправки"


def test_run_ui_task_send_goal_with_otpravit_verb(monkeypatch):
    """БАГ: цель «отправить сообщение «привет» Диме» (глагол «отправ», не
    «напиш/введ») НЕ заходила в send-ветку — in_field={введ,напиш} не содержит
    «отправ», агент «посмотрел на экран» и вставал в петлю. Теперь send-цели
    идут в send-ветку независимо от глагола (ввод + Enter)."""
    typed = []
    entered = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "restore_window", lambda w: True)
    monkeypatch.setattr(ui, "list_windows",
                        lambda: [("Telegram", "MainWindow", "Telegram.exe")])
    monkeypatch.setattr(ui, "click_name", lambda w, t: True)
    monkeypatch.setattr(ui, "click_by_text", lambda w, t: False)
    monkeypatch.setattr(ui, "set_text", lambda w, t: (typed.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: (entered.append(w) or True))
    status, desc = ui_agent.run_ui_task("отправить сообщение «привет» Диме")
    assert status == "done", desc
    assert "привет" in typed, f"должна ввести текст сообщения: {typed}"
    assert entered, "send-цель должна жать Enter (отправка)"


def test_run_ui_task_types_without_enter_for_plain_input(monkeypatch):
    """Ввод без отправки (введи/название) Enter не жмёт — защита от нечаянной
    отправки в чатах при обычном заполнении форм."""
    sent = []
    _mock_ui(monkeypatch, CHAT_ELEMS, set_text=lambda w, t: True)
    monkeypatch.setattr(ui, "send_enter",
                        lambda w: (sent.append(w) or True))
    status, _ = ui_agent.run_ui_task("введи в поле «привет»")
    assert status == "done"
    assert sent == []


def test_run_ui_task_picks_telegram_window_when_active_is_terminal(monkeypatch):
    """Е2Е-окно: цель «в телеграм», но активное окно — терминал. Ева должна
    взять окно Telegram по классу/заголовку, а не слепо активное."""
    seen = []

    def fake_elems(w):
        seen.append(w)
        return CHAT_ELEMS

    _mock_ui(monkeypatch, CHAT_ELEMS)
    monkeypatch.setattr(ui, "get_window_elements", fake_elems)
    monkeypatch.setattr(ui, "get_active_window", lambda: "C:\\bat\\cmd")
    # Реальные сигналы Telegram на этой машине: класс MainWindow (универсальный),
    # заголовок — имя чата (без слова «telegram»). Надёжный маркер — процесс.
    monkeypatch.setattr(
        ui, "list_windows",
        lambda: [("🫦Любимая❤️🔥 – (11817)", "MainWindow", "Telegram.exe"),
                 ("C:\\bat\\cmd", "ConsoleWindowClass", "claude.exe")])
    status, _ = ui_agent.run_ui_task(
        "напиши Диме в телеграм «встретимся в 12»")
    assert seen, "не снималось окно"
    assert seen[0] == "🫦Любимая❤️🔥 – (11817)"


def test_extract_payload_quotes():
    assert ui_agent._extract_payload("напиши «встретимся в 12» Диме") == "встретимся в 12"


def test_looks_secret():
    assert ui_agent._looks_secret("введи пароль «x»") is True
    assert ui_agent._looks_secret("номер карты 1234 5678") is True
    assert ui_agent._looks_secret("отправить сообщение «привет» Диме") is False