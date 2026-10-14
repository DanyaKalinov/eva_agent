"""UI-цикл: агент сам ведёт взаимодействие с окном до достижения цели.

Стратегия: сначала строгий UIA (клик по тексту; ввод в первое поле),
фолбэк — VLM-скрин (описательный). Лимиты против петель: 12 итераций,
стоп без прогресса, таймаут ~90 с.
"""
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import ui_automation as ui
from screen_watcher import describe_screen

_log = logging.getLogger(__name__)

MAX_ITERATIONS = 12
STUCK_THRESHOLD = 2   # подряд одинаковое состояние окна без изменений
TASK_TIMEOUT = timedelta(seconds=90)
MAX_VLM_CLICKS = 6    # лимит кликов по скриншоту за одну задачу

# Ключевые слова «секретов» (spec §4): ввод таких данных циклом запрещён.
_SECRET_RE = re.compile(
    r"пароль|пассворд|password|\bпин\b|\bpin\b|\bcvv\b|"
    r"промокод|номер[ \t]+карт|карт[аы]")


def _looks_secret(text: str) -> bool:
    """True, если текст похож на просьбу ввести секретные данные."""
    return bool(_SECRET_RE.search(text.lower()))


# Подбор окна под цель: ключевые слова в goal -> маркеры класса/заголовка.
_APP_HINTS = (
    (("телеграм", "telegram"),
     ("telegram", "tdesktop", "tgram")),
    (("вотсап", "whatsapp", "ватсап"),
     ("whatsapp", "ватс")),
    (("вайбер", "viber"),
     ("viber", "вайбер")),
    (("дискорд", "discord"),
     ("discord", "дискорд")),
    (("вк", "вконтакте"),
     ("vk", "вконтакте")),
    (("браузер", "хром", "browser"),
     ("chrome", "edge", "opera", "firefox", "яндекс", "browser")),
)


def _has_window_hint(goal: str) -> bool:
    gl = goal.lower()
    return any(any(k in gl for k in keys) for keys, _ in _APP_HINTS)


def _pick_ui_window(goal: str, windows: list[tuple[str, str, str]],
                    active: Optional[str]) -> str:
    """Окно для работы: по приложению из цели (заголовок/класс/процесс),
    иначе — активное. Так «в телеграм» не уедет в терминал, что в фокусе:
    у Telegram класс универсальный (MainWindow), надёжный сигнал — процесс."""
    gl = goal.lower()
    for keys, markers in _APP_HINTS:
        if any(k in gl for k in keys):
            for entry in windows:
                proc = entry[2] if len(entry) > 2 else ""
                hay = ((entry[1] or "") + " " + (entry[0] or "") + " " + proc).lower()
                if any(m in hay for m in markers):
                    return entry[0]
            break
    return active


_SEND_WORDS = ("напиш", "отправ", "сообщ", "смс")


def _is_send_goal(goal: str) -> bool:
    """Цель про отправку сообщения — после ввода нужен Enter (иначе текст
    останется в поле, а в чате не уйдёт)."""
    gl = goal.lower()
    return any(w in gl for w in _SEND_WORDS)


def run_ui_task(goal: str, window: Optional[str] = None) -> tuple[str, str]:
    """Выполняет цель в активном (или заданном) окне.

    Возвращает (status, description): status 'done' | 'stuck' | 'error'.
    Секреты (пароли/PIN/карты) не вводятся — status 'error'.
    """
    win = window
    if not win:
        # send-цель с адресатом («в телеграм», «в вк»): окно адресата ищем
        # ПРИОРИТЕТНО и НЕ фолбэчимся на активное — иначе написали бы в
        # терминал/не в тот чат. Если окна нет (Telegram закрыт/в трее) —
        # ОТКРЫВАЕМ адресат сам и работаем в новом окне.
        dest_phrase = _parse_send_goal(goal)[0]
        if dest_phrase:
            all_wins = ui.list_windows()
            _log.debug("run_ui_task: dest_phrase=%r  windows=%s", dest_phrase,
                        [(t, p) for t, _, p in all_wins])
            win = _pick_ui_window(goal, all_wins, None)
            _log.debug("run_ui_task: _pick_ui_window → %r", win)
            if not win:
                win, open_note = _ensure_dest_window(goal, None)
                if not win:
                    return ("stuck",
                            f"Не удалось открыть место для сообщения: {open_note}")
        elif _has_window_hint(goal):
            win = _pick_ui_window(goal, ui.list_windows(), ui.get_active_window())
        else:
            win = ui.get_active_window()
        # Окно адресата свёрнуто (в трее/минимайзнуто) — разворачиваем, чтобы
        # UIA-клики не уходили в пустоту. Best-effort: если не вышло — не фатал.
        if dest_phrase and win:
            ui.restore_window(win)
    if not win:
        return "error", "Не удалось определить активное окно."
    _log.debug("run_ui_task: win=%r", win)
    if _looks_secret(goal):
        return ("error",
                "Это похоже на секретные данные — не ввожу пароли, ПИН или "
                "карты без твоего участия.")
    start = datetime.now()
    attempts = 0
    prev_sig: Optional[str] = None
    stuck_count = 0

    goal_l = goal.lower()
    actions_log: list[str] = []
    did_action = False   # реальное действие (клик/ввод), не просто взгляд
    vlm_history: list[str] = []   # элементы, по которым уже кликнули через VLM (против петель)
    for attempt in range(1, MAX_ITERATIONS + 1):
        if datetime.now() - start > TASK_TIMEOUT:
            return "stuck", "Таймаут — не успела сделать за 90 секунд."
        attempts = attempt
        elems = ui.get_window_elements(win)
        sig = _window_signature(elems)
        if sig == prev_sig:
            stuck_count += 1
        else:
            stuck_count = 0
        prev_sig = sig
        if stuck_count >= STUCK_THRESHOLD:
            break

        # 1) Send-цель («напиши …»): найти контакт (клик строгим UIA, иначе
        #    поиск приложения), проверить композер и отправить. Контакт ищется
        #    ДО выбора поля — иначе старый путь написал бы прямо в открытый
        #    чат, а мы должны сначала найти нужный.
        # Send-цель сама подразумевает поле ввода (композер), даже если глагол
        # переформулирован («написать…» → «отправить сообщение…»), — иначе ветка
        # не срабатывала и агент вставал в петлю «посмотрела на экран».
        in_field = _is_send_goal(goal) or any(g in goal_l for g in ("введ", "напиш"))
        if _is_send_goal(goal) and in_field:
            _dest, contact, payload = _parse_send_goal(goal)
            sent_payload = payload or _extract_payload(goal)
            if contact:
                if not _locate_contact(win, contact, actions_log):
                    return ("stuck",
                            f"Не нашла чат/контакт «{contact}» в окне {win}. "
                            f"Последнее: {'; '.join(actions_log[-2:]) or 'попытки не было'}")
                time.sleep(0.8)
                if ui.find_message_input(win) is None:
                    return "stuck", f"Открыла, но нет поля ввода сообщения для «{contact}»."
            if sent_payload:
                target = ui.find_message_input(win)
                if target is None:
                    return "stuck", "Нет поля ввода сообщения в этом окне."
                if ui.set_text(win, sent_payload):
                    actions_log.append(f"ввела текст: {sent_payload}")
                    if ui.send_enter(win):
                        actions_log.append("нажала Enter (отправка)")
                        return "done", "Сделала: " + "; ".join(actions_log)
                    return "stuck", f"Ввела текст, но Enter не нажался — сообщение не отправлено."
                return "stuck", "Поле есть, но не могу ввести текст."

        # 2) Обычный ввод текста (не send) — в первое поле ввода, без Enter.
        target = ((ui.find_edit_control(win)) if in_field else None)
        if target:
            text = _extract_payload(goal)
            if text:
                if ui.set_text(win, text):
                    actions_log.append(f"ввела текст: {text}")
                    if _is_send_goal(goal):
                        if ui.send_enter(win):
                            actions_log.append("нажала Enter (отправка)")
                            did_action = True
                            return "done", "Сделала: " + "; ".join(actions_log)
                        return ("stuck",
                                "Ввела текст, но Enter не нажался — сообщение "
                                f"не отправлено. Поле: {text}")
                    did_action = True
                    return "done", "Сделала: " + "; ".join(actions_log)
                return "stuck", "Поле есть, но не могу ввести текст."

        # 3) Клик по тексту внутри goal (например «Дмитрий», «Отправить»).
        for token in _click_targets(goal):
            if ui.click_by_text(win, token):
                actions_log.append(f"кликнула «{token}»")
                did_action = True
                break
        else:
            # 4) Фолбэк VLM: модель выбирает следующий элемент по скриншоту,
            #    Ева кликает и проверяет, что экран изменился.
            step = _vlm_fallback(win, goal, actions_log, vlm_history)
            if step == "clicked":
                did_action = True
            elif step == "done" and did_action:
                return "done", "Сделала: " + "; ".join(actions_log)
            else:
                break

        time.sleep(0.9)   # пауза перед проверкой изменения окна

    return _final_status(attempts, did_action, actions_log, win, goal)


def _extract_payload(goal: str) -> Optional[str]:
    """Текст для ввода из goal: содержимое в кавычках, либо после «:»/«-»."""
    m = re.search(r"[«\"']([^»\"']+)[»\"']", goal)
    if m:
        return m.group(1).strip()
    for sep in (":", "—", "-"):
        if sep in goal:
            return goal.split(sep, 1)[1].strip()
    return None


def _click_targets(goal: str) -> list[str]:
    """Кандидаты для клика: имена собственные (с большой буквы) и слова не
    из стоп-списка. Простая эвристика без LLM."""
    stop = {"отправить", "написать", "ввести", "кликнуть", "найти", "сообщение",
            "в", "на", "и", "что", "как", "где", "привет", "тест"}
    words = [w.strip(" ,.!?«»\"'-") for w in goal.split()]
    names = [w for w in words if w and w[0].isupper() and w.lower() not in stop]
    names += [w for w in words if w.lower() in ("отправить",) and len(words) > 4]
    return names or [w for w in words if w.lower() and w.lower() not in stop]


# Адресат «куда писать»: цель-фраза -> (способ, цель). app=приложение,
# site=сайт, app_or_web=приложение с фолбэком на web-версию.
_DEST_MAP = {
    "телеграм": ("app_or_web", "telegram"), "telegram": ("app_or_web", "telegram"),
    "вотсап": ("app_or_web", "whatsapp"), "whatsapp": ("app_or_web", "whatsapp"),
    "ватсап": ("app_or_web", "whatsapp"),
    "вайбер": ("app_or_web", "viber"), "viber": ("app_or_web", "viber"),
    "сигнал": ("app", "signal"), "signal": ("app", "signal"),
    "вк": ("site", "https://vk.com"), "вконтакте": ("site", "https://vk.com"),
    "дискорд": ("app_or_web", "discord"), "discord": ("app_or_web", "discord"),
}
_DEST_PREP_RE = re.compile(r"\b(в|на)\b")


def _dest_phrase(goal: str) -> Optional[str]:
    """Фраза-адресат из цели: «в вк», «в телеграм» — или None, если адресата
    нет. Берём от последнего предлога «в»/«на» перед ключевым словом."""
    gl = goal.lower()
    for key in _DEST_MAP:
        if key in gl:
            idx = gl.find(key)
            pre = gl[:idx]
            starts = [m.start() for m in _DEST_PREP_RE.finditer(pre)]
            start = starts[-1] if starts else 0
            return goal[start:idx + len(key)].strip() or None
    return None


def _parse_send_goal(goal: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Разбор send-цели на (dest_phrase, contact, payload). dest_phrase — куда
    писать («в вк»), contact — имя контакта (первое заглавное слово минус
    стоп/адресаты/payload), payload — текст сообщения из _extract_payload."""
    dest = _dest_phrase(goal)
    payload = _extract_payload(goal)
    contact = None
    stop = {"в", "на", "и", "что", "как", "где"} | set(_DEST_MAP) | {"напиши", "написать"}
    for w in _click_targets(goal):
        wc = w.strip(" ,.!?«»\"'-:;")
        if wc.lower() in stop or (payload and wc == payload):
            continue
        contact = wc
        break
    return dest, contact, payload


# Маппинг уменьшительных имён к полным (для поиска контактов в мессенджерах)
_DIMINUTIVE_MAP = {
    # Мужские
    "саня": "саша", "саш": "саша", "сашу": "саша", "алекс": "александр",
    "алешка": "александр", "олег": "олег", "ольга": "олег",  # Ольга→Олег (тезка)
    "дима": "дима", "дим": "дима", "димка": "дима", "дмитрий": "дима",
    "коля": "коля", " nik": "николай", "ник": "николай", "колян": "коля",
    "ваня": "ваня", "ван": "ivan", "иван": "ваня",
    "петя": "петр", "петр": "петр", "пет": "петр",
    "миша": "михаил", "миха": "михаил", "мих": "михаил",
    "федя": "федор", "федор": "федор", "фед": "федор",
    "женя": "евгений", "женек": "евгений", "евгений": "евгений",
    "сергей": "сергей", "серёжа": "сергей", "серёг": "сергей",
    "андрей": "андрей", "андрюша": "андрей", "андрюх": "андрей",
    "макс": "максим", "максим": "максим", "максимка": "максим",
    "alex": "alexander",
    # Женские
    "лена": "лена", "ленка": "лена", "елена": "лена",
    "лиза": "елизавета", "лиз": "елизавета", "елизавета": "елизавета", "лизавета": "елизавета",
    "маша": "мария", "маш": "мария", "марья": "мария", "мария": "мария", "машу": "мария",
    "катя": "катя", "катюша": "катя", "катрин": "катя", "екатерина": "катя",
    "надя": "надежда", "надюша": "надежда", "надежда": "надежда",
    "саша": "саша",  # может быть и Александр, и Александра
    "аня": "анна", "анюта": "анна", "анна": "анна", "анют": "анна",
    "даша": "дарья", "дарьюшка": "дарья", "дарья": "дарья", "дашу": "дарья",
    "юля": "юлия", "юлька": "юлия", "юлия": "юлия",
    "оля": "ольга", "олька": "ольга", "ольга": "ольга",
    "вика": "виктория", "вика": "виктория", "викуля": "виктория",
    "галя": "галина", "галина": "галина", "галюня": "галина",
    "ира": "ирина", "ирина": "ирина", "ирочка": "ирина",
    "таня": "татьяна", "таня": "татьяна", "татьяна": "татьяна",
    "света": "светлана", "светлана": "светлана", "светка": "светлана",
}


def _contact_candidates(contact: str) -> list[str]:
    """Формы контакта для клика/поиска: исходная + кандидаты из маппинга
    уменьшительных имён + простых склонений русских имён («Диме»->«Дима»,
    «Кате»->«Катя», «Дмитрию»->«Дмитрий»). «Диме» в списке чатов нет, а «Дима»
    есть — иначе клик/поиск промахнулся бы. Короткие слова и уже именительные
    формы не трогаем."""
    out = [contact]
    if len(contact) < 3:
        return out

    # Проверяем уменьшительные формы (Саня→Саша, Димка→Дима)
    c_lower = contact.lower()
    if c_lower in _DIMINUTIVE_MAP:
        mapped = _DIMINUTIVE_MAP[c_lower]
        # Сохраняем регистр первой буквы исходного контакта
        if contact[0].isupper() and mapped:
            mapped = mapped[0].upper() + mapped[1:]
        if mapped and mapped not in out:
            out.append(mapped)

    # Также проверим случай, если контакт уже в канонической форме (Саша→Саша)
    # но в нижнем регистре может быть ключом маппинга
    if contact.lower() in _DIMINUTIVE_MAP.values():
        # Контакт уже в канонической форме
        pass

    base = contact[:-1]
    if base and base[0].isupper():
        last = contact[-1].lower()
        if last in ("е", "ё"):
            for suffix in ("а", "я"):
                cand = base + suffix
                if cand not in out:
                    out.append(cand)
        elif last in ("у", "ю"):
            for suffix in ("а", "я", "й", "ь", ""):
                cand = base + suffix
                if cand not in out:
                    out.append(cand)
    return out


def _window_shows_contact(win: str, contact: str) -> Optional[str]:
    """Если окно УЖЕ показывает чат контакта — вернуть найденную форму имени
    (иначе None). Telegram: заголовок окна = имя чата («Дима – (20735)»),
    список чатов — просто «Telegram». LRM (‎) в начале заголовка — мусор
    UIA, срезаем. Точное равенство головы заголовка форме имени: «Иванов» не
    сойдёт за «Иван» — не открыть не тот чат."""
    head = win.split(" – ", 1)[0].split(" - ", 1)[0].strip()
    head = head.replace("‎", "").replace("‏", "").strip()
    if len(head) < 2:
        return None
    for form in _contact_candidates(contact):
        if head.lower() == form.lower():
            return form
    return None


def _locate_contact(win: str, contact: str, actions_log: list[str]) -> bool:
    """Найти и открыть чат/контакт: если нужный чат УЖЕ открыт (заголовок
    окна = имя) — не ищем (Telegram имена чатов через UIA не отдаёт, клик/поиск
    бессмысленны). Иначе — строгий ТОЧНЫЙ клик по каждой форме склонения (не
    подстрокой — иначе бьёт в «Дима Шуриков»), а если клик не сработал — поиск
    приложения базовой (именительной) формой, не «Диме».

    Если UIA не находит контакт — фолбэк на визуальный поиск через VLM
    (скриншот + распознавание, без сохранения файлов)."""
    shown = _window_shows_contact(win, contact)
    if shown:
        actions_log.append(f"чат «{shown}» уже открыт (заголовок окна)")
        return True

    # 1. Пробуем UIA-клик по точному имени
    for form in _contact_candidates(contact):
        if ui.click_name(win, form):
            actions_log.append(f"кликнула чат «{form}»")
            return True

    # 2. Пробуем поиск через UIA
    if ui.find_search_edit(win) is not None:
        # Поиск: вбиваем contact как есть — это то, как пользователь назвал чат/контакт.
        # VLM при визуальном поиске также будет искать по точному названию.
        # Используем set_text_search для ввода в строку поиска (а не set_text,
        # который вводит в самый нижний edit — композер).
        if ui.set_text_search(win, contact) and ui.send_enter(win):
            if ui.find_message_input(win) is not None:
                actions_log.append(f"нашла через поиск: {contact}")
                return True

        # Фолбэк: если точное название не сработало — пробуем именительную форму
        # из кандидатов (вдруг чат сохранён как "Саша" вместо "Саня")
        for form in _contact_candidates(contact):
            if form == contact:
                continue
            if ui.set_text_search(win, form) and ui.send_enter(win):
                if ui.find_message_input(win) is not None:
                    actions_log.append(f"нашла через поиск: {form} (фолбэк)")
                    return True

    # 3. Фолбэк: визуальный поиск через VLM (скриншот + распознавание)
    actions_log.append(f"UIA не нашёл контакт, пробую визуальный поиск: {contact}")
    return _locate_contact_visual(win, contact, actions_log)


def _locate_contact_visual(win: str, contact: str, actions_log: list[str]) -> bool:
    """Визуальный поиск контакта: Qwen2.5-VL находит имя чата в списке по
    скриншоту окна, vlm_click кликает. Скриншот живёт только в памяти.
    Проверка результата — по появлению поля ввода сообщения."""
    try:
        from vlm_actions import vlm_click
        from vlm_agent import region_of_window

        ui.restore_window(win)   # вывести окно на передний план (best-effort)
        time.sleep(0.3)
        forms = _contact_candidates(contact)[:3]   # максимум 3 формы имени
        description = "чат или контакт в списке с именем " + " или ".join(f"«{f}»" for f in forms)
        res = vlm_click(description, window_title=win, use_uia=False,
                        region=region_of_window(win), verify=False)
        if not res["ok"]:
            actions_log.append(f"визуальный поиск не нашёл: {contact}")
            return False
        actions_log.append(f"VLM кликнула по контакту в ({res['x']}, {res['y']})")
        time.sleep(0.5)
        if ui.find_message_input(win) is not None:
            actions_log.append(f"чат «{contact}» открыт через визуальный поиск")
            return True
        actions_log.append("клик по координатам не открыл чат")
        return False
    except Exception as e:
        actions_log.append(f"ошибка визуального поиска: {e}")
        return False


def _open_dest(dest_phrase: str) -> tuple[bool, str]:
    """Открыть приложение/сайт адресата («в телеграм» -> telegram и т.п.).
    Возвращает (ok, note). Для app_or_web при отсутствии приложения — фолбэк
    на web-версию."""
    import actions as _act  # лениво: не тянем pywinauto/старт-меню на импорте
    gl = (dest_phrase or "").lower()
    for key, (mode, target) in _DEST_MAP.items():
        if key in gl:
            if mode == "site":
                return _act.open_website(target)
            ok, note = _act.open_app(target)
            if ok == "done" or mode == "app":
                return ok == "done", note
            # app_or_web: приложения нет -> web-версия
            web = {"whatsapp": "https://web.whatsapp.com",
                   "discord": "https://discord.com",
                   "telegram": "https://web.telegram.org",
                   "viber": "https://web.viber.com"}.get(target)
            if web:
                ok, wnote = _act.open_website(web)
                if ok:
                    return True, (f"Приложение {target} не найдено — открыла "
                                  f"веб-версию: {wnote}")
                return True, wnote
            return False, note
    return False, f"Не понимаю адресат «{dest_phrase}»."


def _ensure_dest_window(goal: str, win: Optional[str]) -> tuple[Optional[str], str]:
    """Окно для работы с адресатом: выбрать по цели (как _pick_ui_window),
    а если подходящего окна нет — ОТКРЫТЬ адресат и выбрать новое.
    Возвращает (window|None, note)."""
    dest_phrase, _contact, _payload = _parse_send_goal(goal)
    if not dest_phrase:
        return win, ""
    if win:
        return win, ""
    ok, note = _open_dest(dest_phrase)
    if not ok:
        return None, note
    time.sleep(1.5)  # дать приложению/браузеру открыться
    return _pick_ui_window(goal, ui.list_windows(), ui.get_active_window()), ""


def _window_signature(elems: list[dict]) -> str:
    return "|".join(
        f"{e.get('role')}:{e.get('name', '')}"
        for e in elems[:6]
    )


def _vlm_fallback(window: str, goal: str, log: list[str], history: list[str]) -> str:
    """VLM-шаг, когда UIA не нашёл ничего по словам цели. Модель смотрит на
    окно и называет следующий элемент, vlm_click кликает и проверяет, что
    экран отреагировал. Возвращает:
      'clicked' — кликнули, цикл идёт дальше;
      'done'    — модель считает цель достигнутой;
      'fail'    — шаг не удался (цикл остановится, а не будет кликать вслепую)."""
    if len(history) >= MAX_VLM_CLICKS:
        log.append("достигнут лимит VLM-кликов")
        return "fail"
    try:
        from vlm_actions import vlm_click
        from vlm_agent import plan_next_click, region_of_window

        ui.restore_window(window)   # вывести окно на передний план (best-effort)
        time.sleep(0.3)
        region = region_of_window(window)
        plan = plan_next_click(goal, history, region)
        if plan["done"]:
            return "done"
        target = plan["target"]
        if not target or target in history:   # пусто или повтор — защита от петли
            log.append("VLM не нашла следующий шаг")
            return "fail"
        history.append(target)
        res = vlm_click(target, window_title=window, use_uia=False, region=region)
        if not res["ok"]:
            log.append(res["message"])
            return "fail"
        log.append(f"VLM кликнула «{target}»")
        if res.get("verified") is False:
            log.append("экран не отреагировал на клик")
            return "fail"
        return "clicked"
    except Exception as e:
        log.append(f"VLM-шаг не удался: {e}")
        return "fail"


def _final_status(attempts: int, did_action: bool, actions_log: list[str],
                  win: str, goal: str) -> tuple[str, str]:
    """done — только если было реальное действие (клик/ввод/отправка), а не
    один взгляд («посмотрела») и тем более не провал взгляда."""
    if did_action and attempts >= 1:
        return "done", "Сделала: " + "; ".join(actions_log)
    tail = ""
    if actions_log:
        tail = " Последнее: " + "; ".join(actions_log[-3:])
    return ("stuck",
            f"Не получилось достичь цели «{goal}» в окне {win}.{tail} Вот что "
            f"я вижу: " + str(ui.get_window_elements(win)[:3]))