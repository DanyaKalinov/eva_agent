"""Локальный мозг desktop-агента: Qwen2.5-VL 7B через Ollama.

Переводит произвольную фразу пользователя в план:
- answer  — обычный ответ текстом;
- actions — безопасные локальные действия (исполняет сам агент);
- confirm — разрушительные локальные действия (нужно подтверждение в Telegram);
- cloud   — облачное действие (память/напоминания/интернет-сырьё) — закрывает
            сервер через cloud_action;
- error   — модель не смогла.

Whitelist действий — единый для задач «локально/облачно» (см. спецификацию, §5).
Модель строго обязана выбирать только из него.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

import ollama

from config import config

OLLAMA_MODEL = "qwen2.5vl:7b"

# Таймауты (секунды) для автозапуска Ollama.
_OLLAMA_START_TIMEOUT = 60
_OLLAMA_START_SLEEP = 1.0
# True, когда pull модели уже ушёл в фоновый поток — чтобы не запускать
# второй параллельный pull из чата и из старта приложения одновременно.
_PULL_STARTED = False

# Действия, которые реально исполняет агент локально (actions.py) — перечисляем
# имена и параметры, чтобы модель не выдумывала.
_LOCAL_ACTIONS = """
- open_app: {"app": "название программы"}
- open_website: {"url": "адрес сайта"}
- look_at_screen: {"question": "что спросить, глядя на экран"} — внутренний взгляд: сделать скриншот и глазами модели посмотреть, что открыто на экране (что за окна/вкладки/сайты, в каком порядке). Использовать, если для команды нужно понять, что реально видно на экране. Картинка никуда не отправляется, вернётся только текст описания.
- open_file: {"path": "путь"}
- open_folder: {"name": "название папки"}
- create_file: {"path": "имя", "content": "текст"}
- read_file: {"path": "имя"}
- delete_file: {"path": "имя"}
- edit_file: {"path": "имя", "mode": "append/overwrite", "content": "текст"}
- copy_file: {"src": "откуда", "dst": "куда"}
- move_file: {"src": "откуда", "dst": "куда"}
- list_files: {"folder": "название"}
- search_file: {"query": "что искать"}
- screenshot: {}
- set_volume: {"direction": "up/down/mute/unmute"} или {"level": 0..100}
- power_action: {"action": "shutdown/restart/lock"}
- type_text: {"text": "текст"}
- press_key: {"keys": "ctrl+s"}
- web_search: {"query": "...", "engine": "google/yandex/bing", "browser": "chrome/opera/firefox/edge"}
- ui_click: {"window": "окно", "control": "кнопка"}
- ui_list_controls: {"window": "окно"}
- close_app: {"window": "окно"}
- minimize_window: {"window": "окно"}
- maximize_window: {"window": "окно"}
- mouse_move: {"x": 0, "y": 0}
- mouse_click: {"x": 0, "y": 0, "button": "left/right/middle"}
- mouse_scroll: {"direction": "up/down", "amount": 3}
- mouse_drag: {"x_start": 0, "y_start": 0, "x_end": 0, "y_end": 0}
- set_default_browser: {"browser": "chrome/opera/firefox/edge"}
- ui_type_text: {"window": "окно", "control": "поле", "text": "текст"}
- ui_do_task: {"goal": "короткая цель в приложении"} — Ева сама находит и кликает элементы в активном окне, чтобы выполнить цель (например написать сообщение в мессенджере). Никаких координат и кнопок не указывать — она разберётся.
- ui_click_by_text: {"window": "окно", "text": "текст кнопки/пункта"} — кликнуть по элементу с этим текстом
- ui_set_text: {"window": "окно", "text": "текст"} — ввести текст в первое поле ввода
- ui_select_item: {"window": "окно", "text": "название"} — выбрать пункт из списка/меню
- vlm_click: {"description": "как выглядит элемент, например «синяя кнопка Отправить»", "text": "точный текст элемента, если известен", "expect": "что должно произойти после клика"} — кликнуть по элементу, которого нет в UIA-дереве: Ева ищет его глазами по скриншоту. Использовать, когда ui_click/ui_click_by_text не нашли элемент.
- get_screen_state: {} — спросить: что сейчас активно на экране (окно, режим), мгновенно, без скриншота
- start_watch: {"target": "курс валют", "when": "change|appear|disappear", "what": "что сообщить"} — следить за окном и уведомить при событии
- stop_watch: {"target": "курс валют"} — перестать следить
- list_watches: {} — показать активные наблюдения
"""

_CLOUD_ACTIONS = """
- create_reminder: {"text": "что напомнить", "when": "через 15 минут"}
- memory_add: {"text": "факт, который стоит запомнить"}
- memory_search: {"query": "что искать в памяти пользователя"}
- fetch_web_results: {"query": "вопрос для поиска в интернете"}
- fetch_url: {"url": "ссылка на статью"}
"""

_DESTRUCTIVE_LOCAL = {
    "create_file", "delete_file", "edit_file", "copy_file", "move_file",
    "power_action", "close_app", "mouse_click", "mouse_drag", "vlm_click",
}

# Объединённое описание для промпта — перечисляет и локальные, и облачные действия.
ACTIONS_BRAIN = f"{_LOCAL_ACTIONS}\nОблачные действия (выполняет сервер):\n{_CLOUD_ACTIONS}"


def is_ollama_ready() -> bool:
    try:
        return bool(ollama.list().get("models"))
    except Exception:
        return False


def _model_in_list(model: str) -> bool:
    """Есть ли модель в списке установленных в Ollama. При недоступном
    сервере считаем «нет» — это не сломает поток автозапуска."""
    try:
        models = ollama.list().get("models") or []
    except Exception:
        return False
    return any(m.get("name") == model for m in models)


def find_ollama_exe() -> Optional[str]:
    """Ищет исполняемый файл Ollama: сначала в PATH (CLI `ollama`), затем в
    стандартной папке установки Windows. Возвращает None, если не найдено."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = os.path.join(local_app_data, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def ensure_ollama(wait: bool = True, timeout: int = _OLLAMA_START_TIMEOUT) -> bool:
    """Возвращает True, если сервер Ollama доступен. При необходимости
    запускает `ollama serve` отдельным процессом. Не бросает исключений."""
    if is_ollama_ready():
        return True
    exe = find_ollama_exe()
    if exe is None:
        return False
    try:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):  # Windows: без окна консоли
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen([exe, "serve"], **kwargs)
    except OSError:
        return False
    if not wait:
        return is_ollama_ready()
    deadline = time.monotonic() + max(timeout, 0)
    while time.monotonic() < deadline:
        time.sleep(_OLLAMA_START_SLEEP)
        if is_ollama_ready():
            return True
    return is_ollama_ready()


def _perform_pull(exe: Optional[str], model: str) -> None:
    """Синхронный `ollama pull` — исполняется в фоновом потоке. Работает и из
    чата, и из старта приложения; очищает флаг _PULL_STARTED по завершении."""
    global _PULL_STARTED
    try:
        if exe:
            subprocess.run([exe, "pull", model], check=True)
    finally:
        _PULL_STARTED = False


def ensure_model() -> tuple[bool, str]:
    """Проверяет, что модель установлена; если нет — ставит качать в фоновом
    потоке. Возвращает (ok, пояснение): ok=True — модель можно использовать."""
    if _model_in_list(config.ollama_model):
        return True, ""
    global _PULL_STARTED
    if _PULL_STARTED:
        return False, "модель уже скачивается — вернись через несколько минут"
    _PULL_STARTED = True
    threading.Thread(
        target=_perform_pull,
        args=(find_ollama_exe(), config.ollama_model),
        daemon=True,
    ).start()
    return False, (
        f"модель {config.ollama_model} ещё не установлена — начинаю скачивание "
        "(~6 ГБ, займёт время). Скажи «готово» через несколько минут или "
        "просто напиши ещё раз."
    )


def ensure_ollama_ready() -> tuple[bool, str]:
    """Полный цикл автозапуска: сервер Ollama + модель. Возвращает
    (ok, сообщение) — сообщение показывается в чате/логе."""
    if is_ollama_ready():
        return True, "Локальный мозг готов."
    if not ensure_ollama(wait=True, timeout=_OLLAMA_START_TIMEOUT):
        return False, "Не удалось запустить Ollama — проверь, что она установлена."
    ok, note = ensure_model()
    if not ok:
        return False, note
    return True, "Локальный мозг готов."


def classify_steps(steps: list[dict]) -> str:
    """'confirm', если среди шагов есть разрушительные действия, иначе 'actions'."""
    if any(s.get("action") in _DESTRUCTIVE_LOCAL for s in steps):
        return "confirm"
    return "actions"


# Слова-маркеры «просьба написать/отправить» и адресаты («куда писать»).
_SEND_WORDS = ("напиш", "отправ", "сообщ", "смс")
_DEST_KEYWORDS = ("телеграм", "telegram", "вотсап", "whatsapp", "ватсап",
                  "вайбер", "viber", "сигнал", "signal",
                  "вк", "вконтакте", "дискорд", "discord", "браузер",
                  "почт", "gmail", "mail", "мессендж")


def _extract_message(text: str) -> Optional[str]:
    """Текст сообщения из просьбы: кавычки, либо после «:», либо после глагола."""
    m = re.search(r"[«\"']([^»\"']+)[»\"']", text)
    if m:
        return m.group(1).strip()
    for sep in (":", "—", "-"):
        if sep in text:
            return text.split(sep, 1)[1].strip()
    parts = text.split()
    return " ".join(parts[1:]) if len(parts) > 1 else None


def _no_destination_send(text: str) -> Optional[str]:
    """Если просьба написать/отправить сообщение, но адресат НЕ назван —
    вернуть текст сообщения (для заготовки need_destination), иначе None.

    Страховка от причуд qwen2.5vl: модель иногда планирует ui_do_task в
    активном окне на «напиши привет». Строка цели может нести суффикс
    «(на экране сейчас активно: ...)» от ws_client — его срезаем.
    """
    clean = re.split(r"\s*\(на экране", text)[0]
    gl = clean.lower()
    if not any(w in gl for w in _SEND_WORDS):
        return None
    if any(d in gl for d in _DEST_KEYWORDS):
        return None
    msg = _extract_message(clean) or ""
    return msg or None


def _system_prompt(memory_context: str) -> str:
    return (
        "Ты — Ева, персональный ИИ-ассистент на ПК пользователя. "
        "Пользователь разговаривает с тобой текстом или голосом. Вот что ты "
        f"знаешь о нём из памяти (может не относиться к вопросу):\n{memory_context or '(пусто)'}\n\n"
        "Список действий, которые ты реально умеешь выполнять ЛОКАЛЬНО (на этом ПК):\n"
        f"{_LOCAL_ACTIONS}\n"
        "Список ОБЛАЧНЫХ действий (их выполняет сервер, тебе придёт результат):\n"
        f"{_CLOUD_ACTIONS}\n\n"
        "Ответь СТРОГО одним JSON без пояснений и markdown:\n"
        '{"kind": "answer" | "actions" | "cloud" | "error", "text": "краткий ответ или описание", '
        '"steps": [{"action": "...", "params": {...}, "description": "что будет сделано"}], '
        '"cloud": {"action": "...", "params": {...}}}\n\n'
        "Примеры:\n"
        '- «привет» -> {"kind": "answer", "text": "Привет!"}\n'
        '- «сделай скриншот» -> {"kind": "actions", "steps": [{"action": "screenshot", "params": {}, "description": "Сделаю скриншот"}]}\n'
        '- «найди в яндекс фильм железный человек» -> {"kind": "actions", "steps": [{"action": "web_search", "params": {"query": "фильм железный человек", "engine": "yandex"}, "description": "Найду в Яндексе фильм «Железный человек»"}]}\n'
        '- «открой ютуб» -> {"kind": "actions", "steps": [{"action": "open_website", "params": {"url": "https://www.youtube.com"}, "description": "Открою YouTube"}]}\n'
        '- «напомни через 15 минут выпить воды» -> {"kind": "cloud", "text": "Напомню через 15 минут", "cloud": {"action": "create_reminder", "params": {"text": "выпить воды", "when": "через 15 минут"}}}\n'
        '- «открой третий сайт» -> сначала посмотреть на экран, затем открыть нужный:\n'
        '  {"kind": "actions", "steps": [{"action": "look_at_screen", "params": {"question": "Какие сайты открыты на экране и в каком порядке?"}, "description": "Посмотрю на экран, чтобы понять, какой сайт третий"}]}\n'
        '- «переключись на вкладку с курсом валют» -> {"kind": "actions", "steps": [{"action": "ui_click", "params": {"window": "Google Chrome", "control": "курс валют"}, "description": "Переключусь на вкладку с курсом валют"}]}\n'
        '- «что сейчас открыто?» -> {"kind": "actions", "steps": [{"action": "get_screen_state", "params": {}, "description": "Посмотрю, что сейчас активно на экране"}]}\n'
        '- «следи за курсом доллара на вкладке» -> {"kind": "actions", "steps": [{"action": "start_watch", "params": {"target": "курс доллара", "when": "change", "what": "курс доллара обновился"}, "description": "Начну следить за курсом доллара"}]}\n'
        '- «напиши Диме в телеграм: встретимся в 12» -> {"kind": "actions", "steps": [{"action": "open_app", "params": {"app": "telegram"}, "description": "Открою Telegram"}, {"action": "ui_do_task", "params": {"goal": "отправить сообщение «встретимся в 12» Диме"}, "description": "Найду чат и отправлю сообщение"}]}\n'
        '- «напиши Ане в вк: привет» -> {"kind": "actions", "steps": [{"action": "open_website", "params": {"url": "https://vk.com"}, "description": "Открою VK"}, {"action": "ui_do_task", "params": {"goal": "отправить сообщение «привет» Ане"}, "description": "Найду чат Аня и отправлю сообщение"}]}\n'
        '- «напиши Диме в вотсап: привет» -> {"kind": "actions", "steps": [{"action": "open_app", "params": {"app": "whatsapp"}, "description": "Открою WhatsApp"}, {"action": "ui_do_task", "params": {"goal": "отправить сообщение «привет» Диме"}, "description": "Найду чат и отправлю сообщение"}]}\n'
        '- «напиши привет» (не сказано, куда писать) -> {"kind": "need_destination", "text": "Куда написать? Назови приложение или сайт — например: в телеграм Диме.", "pending": {"prefix": "напиши", "text": "привет"}}\n'
        '- «заполни поле названия в программе» -> {"kind": "actions", "steps": [{"action": "ui_do_task", "params": {"goal": "ввести текст в поле названия"}, "description": "Найду поле и введу текст"}]}\n\n'
        "Правила:\n"
        "- Если человек просто спрашивает/общается — kind=answer.\n"
        "- Если человек ПРОСИТ ЧТО-ТО СДЕЛАТЬ (найти, открыть, сделать) — kind=actions, "
        "даже если просьба вежливая («найди», «открой» — это команда, а не вопрос).\n"
        "- Для локального действия kind=actions; skill с несколькими шагами — несколько в steps.\n"
        "- Если человек просит удалить/перезаписать/закрыть/выключить — всё равно kind=actions, "
        "сервер сам потребует подтверждение по опасным.\n"
        "- Для напоминаний/памяти/интернета kind=cloud и укажи cloud.\n"
        "- Если просят написать/отправить сообщение и НЕ названо, куда "
        "(нет «в телеграм», «в вк» и т.п.) — kind=need_destination с текстом "
        "сообщения в pending.\n"
        "- Не выдумывай действие вне списков.\n"
        "- Если сайт/вкладка уже открыты на экране (в тексте взгляда или в списке окон) — "
        "НЕ открывай его заново через open_website, а переключись на существующую вкладку "
        "через ui_click (window — имя окна браузера, control — название вкладки)."
    )


def parse_plan(raw: str) -> dict:
    """Парсит модель->JSON, устойчив к обрамлению ```json ... ```."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"kind": "error", "text": "Не смогла разобрать запрос."}
    if not isinstance(data, dict):
        return {"kind": "error", "text": "Не смогла разобрать запрос."}
    kind = data.get("kind")
    if kind not in ("answer", "actions", "cloud", "error", "need_destination"):
        return {"kind": "error", "text": "Не смогла разобрать запрос."}
    if kind == "need_destination":
        pending = data.get("pending")
        if not isinstance(pending, dict) or not pending.get("text"):
            return {"kind": "error",
                    "text": "Не смогла сформулировать вопрос «куда написать»."}
        data["pending"] = {"prefix": pending.get("prefix", "напиши"),
                           "text": pending["text"]}
    # qwen2.5vl иногда кладёт шаги в "actions" вместо задокументированного
    # "steps" — нормализуем, иначе действия молча теряются.
    if kind == "actions" and not data.get("steps") and data.get("actions"):
        data["steps"] = data.pop("actions")
    return data


def _call_model(text: str, memory_context: str) -> str:
    """Синхронный вызов Ollama (уводится в поток вызывающим). Формальный
    контракт: возвращает сырой JSON-текст модели."""
    response = ollama.chat(
        model=config.ollama_model,
        messages=[
            {"role": "system", "content": _system_prompt(memory_context)},
            {"role": "user", "content": text},
        ],
        format="json",
    )
    return response["message"]["content"]


def build_plan(text: str, memory_context: str) -> dict:
    """Полный цикл: зовёт локальную модель и парсит план. Если модели нет —
    kind=error с подсказкой."""
    try:
        raw = _call_model(text, memory_context)
    except Exception as e:
        return {"kind": "error", "text": "Мозг (Ollama) недоступен — проверь, что Ollama запущена и модель установлена."}
    plan = parse_plan(raw)
    # Страховочный детектор: проходя написать без адресата — обязательный
    # уточняющий вопрос, даже если модель ошиблась (см. _no_destination_send).
    content = _no_destination_send(text)
    if content is not None:
        return {"kind": "need_destination",
                "text": "Куда написать? Назови приложение или сайт — например: в телеграм Диме.",
                "pending": {"prefix": "напиши", "text": content}}
    return plan