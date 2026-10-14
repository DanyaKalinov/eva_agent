"""
Действия, которые desktop-агент реально выполняет на ПК.

Файловые операции по умолчанию (просто имя файла, без пути) ограничены
рабочей папкой (~/EvaFiles) — чтобы неудачно сформулированная команда не
задела системные файлы. Но если явно назвать одну из известных системных
папок («в загрузках», «на рабочем столе» и т.п.) или абсолютный путь —
агент работает уже с ней. Разрушительные операции (удаление, перезапись,
перемещение, выключение ПК) в любом случае проходят через подтверждение
в Telegram ещё до того, как долетают досюда (см. handlers.py на сервере).
"""
import base64
import io
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

from config import config as agent_config

EVA_FILES_DIR = Path.home() / "EvaFiles"
EVA_FILES_DIR.mkdir(parents=True, exist_ok=True)

# Частые русские названия приложений -> реальные команды Windows.
# Если имени нет в словаре, пробуем передать его "как есть" через os.startfile
# (сработает, если это точное имя exe, доступное в PATH).
APP_ALIASES = {
    "блокнот": "notepad.exe",
    "калькулятор": "calc.exe",
    "проводник": "explorer.exe",
    "paint": "mspaint.exe",
    "паинт": "mspaint.exe",
    "командная строка": "cmd.exe",
    "cmd": "cmd.exe",
}

# Известные системные папки — по ним можно называть место файла словами,
# а не полным путём. Ключи — то, что могло встретиться в команде (с учётом
# частых падежных форм), уже в нижнем регистре.
_HOME = Path.home()
FOLDER_ALIASES = {
    "рабочий стол": _HOME / "Desktop", "рабочем столе": _HOME / "Desktop",
    "рабочего стола": _HOME / "Desktop", "desktop": _HOME / "Desktop",
    "загрузки": _HOME / "Downloads", "загрузках": _HOME / "Downloads",
    "загрузок": _HOME / "Downloads", "downloads": _HOME / "Downloads",
    "документы": _HOME / "Documents", "документах": _HOME / "Documents",
    "документов": _HOME / "Documents", "documents": _HOME / "Documents",
    "картинки": _HOME / "Pictures", "картинках": _HOME / "Pictures",
    "изображения": _HOME / "Pictures", "изображениях": _HOME / "Pictures",
    "pictures": _HOME / "Pictures",
    "видео": _HOME / "Videos", "видеозаписи": _HOME / "Videos", "videos": _HOME / "Videos",
    "музыка": _HOME / "Music", "музыке": _HOME / "Music", "music": _HOME / "Music",
    "eva": EVA_FILES_DIR, "evafiles": EVA_FILES_DIR, "евафайлы": EVA_FILES_DIR,
    "рабочая папка": EVA_FILES_DIR, "рабочей папке": EVA_FILES_DIR,
}
# Папки, в которых разрешён поиск/листинг по умолчанию (без явного указания).
# Специально НЕ весь диск C: — это было бы медленно и могло бы находить
# системные файлы, которые лучше не трогать. Если понадобится конкретная
# папка за пределами этого списка — можно назвать её полным путём.
SEARCH_ROOTS = [
    EVA_FILES_DIR, _HOME / "Desktop", _HOME / "Downloads", _HOME / "Documents",
    _HOME / "Videos", _HOME / "Music",
]


def _safe_path(filename: str) -> Path:
    """Резолвит ГОЛОЕ имя файла (без пути) ВНУТРИ EVA_FILES_DIR — берём
    только имя файла, отбрасывая любые ../ и абсолютные части."""
    name_only = Path(filename).name
    return EVA_FILES_DIR / name_only


def _alias_at_boundary(lower: str, alias: str) -> bool:
    """True, если lower начинается с alias И дальше идёт граница слова
    (конец строки, пробел или разделитель пути), а не символ имени файла.
    Защита от ложного срабатывания алиасов-префиксов: "eva" — это папка
    Евы, но файл "eva_mouse_test.txt" начинается с "eva" и при этом
    совершенно обычное имя файла, а не упоминание папки."""
    if not lower.startswith(alias):
        return False
    after = lower[len(alias):]
    if not after:
        return True
    return after[0] in ("/", "\\", " ")


def resolve_path(raw: str) -> Path:
    """Более широкий резолвер — понимает абсолютные пути и упоминания
    системных папок ("в загрузках"), а голое имя файла по-прежнему
    трактует как файл в песочнице EvaFiles (обратная совместимость)."""
    raw = raw.strip().strip('"').strip("'")
    p = Path(raw)
    if p.is_absolute():
        return p

    lower = raw.lower()
    for alias, folder in FOLDER_ALIASES.items():
        if _alias_at_boundary(lower, alias):
            # Снимаем ведущие разделители пути: иначе folder / "/x.txt" на
            # Windows уедет в корень диска (C:\x.txt), а не внутрь папки.
            rest = raw[len(alias):].lstrip("/\\").strip(" :,-")
            return (folder / rest) if rest else folder

    return _safe_path(raw)


def _resolve_folder(raw: str) -> Optional[Path]:
    """Для команд вида 'покажи файлы в загрузках' — ищет папку по алиасу."""
    if not raw or not raw.strip():
        return EVA_FILES_DIR
    raw_clean = raw.strip().rstrip("?.!").lower()
    for alias, folder in FOLDER_ALIASES.items():
        if alias in raw_clean:
            return folder
    p = Path(raw.strip())
    if p.is_absolute() and p.exists():
        return p
    return None


# ----- Приложения и сайты -----
# Папки, где Windows хранит ярлыки установленных программ (меню «Пуск»).
# Почти любая программа при установке кладёт сюда .lnk — это надёжнее,
# чем гадать реальный путь к .exe (у Discord/Steam/VS Code он у каждого
# пользователя разный и меняется при обновлениях).
_START_MENU_DIRS = [
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
]


def _search_shortcuts_in(base: Path, name_norm: str) -> tuple[Optional[Path], Optional[Path]]:
    """Возвращает (точное_совпадение, первое_частичное) среди .lnk внутри base."""
    exact: Optional[Path] = None
    partial: Optional[Path] = None
    if not base.exists():
        return None, None
    for lnk in base.rglob("*.lnk"):
        stem = lnk.stem.lower()
        if stem == name_norm:
            return lnk, partial
        if partial is None and name_norm in stem:
            partial = lnk
    return exact, partial


def _find_shortcut(name: str) -> Optional[Path]:
    """Ищет .lnk-ярлык по названию. Сначала быстрые места (меню «Пуск»,
    Рабочий стол) — там находится подавляющее большинство программ. Если
    не нашли — расширяем поиск на весь диск (с исключением системных/
    тяжёлых папок и бюджетом времени, как и в search_file, чтобы не
    зависнуть на большом диске навсегда)."""
    name_norm = name.strip().lower()
    if not name_norm:
        return None

    partial_match: Optional[Path] = None
    for base in _START_MENU_DIRS + [_HOME / "Desktop"]:
        exact, partial = _search_shortcuts_in(base, name_norm)
        if exact:
            return exact
        if partial_match is None:
            partial_match = partial
    if partial_match:
        return partial_match

    # Не нашли в быстрых местах — расширяем поиск на весь диск.
    system_drive = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\")
    deadline = time.time() + 20
    partial_match = None
    for dirpath, dirnames, filenames in os.walk(system_drive):
        dirnames[:] = [d for d in dirnames if d.lower() not in _EXCLUDED_DIR_NAMES and not d.startswith(".")]
        for fname in filenames:
            if fname.lower().endswith(".lnk"):
                stem = Path(fname).stem.lower()
                full_path = Path(dirpath) / fname
                if stem == name_norm:
                    return full_path
                if partial_match is None and name_norm in stem:
                    partial_match = full_path
        if time.time() > deadline:
            break
    return partial_match


# ----- Самообучение: агент запоминает, как называть уже найденные программы -----
# Безопасный вариант "самообучения" — никакого изменения кода или логики,
# просто плоский JSON-файл внутри песочницы EvaFiles: название -> путь.
# Пользователь в любой момент может посмотреть его (/eva прочитай файл
# learned_app_aliases.json) или сбросить (/eva удали файл ...), это уже
# работает через обычные файловые команды, без отдельного действия.
LEARNED_ALIASES_PATH = EVA_FILES_DIR / "learned_app_aliases.json"


def _load_learned_aliases() -> dict:
    if LEARNED_ALIASES_PATH.exists():
        try:
            return json.loads(LEARNED_ALIASES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _remember_app_alias(name: str, resolved_path: str) -> None:
    aliases = _load_learned_aliases()
    aliases[name.strip().lower()] = resolved_path
    try:
        LEARNED_ALIASES_PATH.write_text(
            json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception as e:
        print(f"⚠️ Не удалось сохранить выученный alias: {e}")


def open_app(app_name: str) -> tuple[str, str]:
    app_name = app_name.strip()
    name_lower = app_name.lower()

    # 1. Известный встроенный alias (блокнот, калькулятор и т.п.)
    alias_command = APP_ALIASES.get(name_lower)
    if alias_command:
        try:
            os.startfile(alias_command)
            return "done", f"Открыл: {app_name}"
        except Exception:
            pass  # попробуем дальше

    # 2. Ранее выученный alias — с прошлого раза уже знаем, куда вело это название
    learned = _load_learned_aliases()
    learned_path = learned.get(name_lower)
    if learned_path:
        if learned_path.startswith("aumid:"):
            # Store-приложение (WindowsApps) — запуск через AppUserModelID
            try:
                os.startfile("shell:AppsFolder\\" + learned_path[len("aumid:"):])
                return "done", f"Открыл: {app_name} (запомненный вариант)"
            except Exception:
                pass  # запись устарела — поищем заново ниже
        elif Path(learned_path).exists():
            try:
                os.startfile(learned_path)
                return "done", f"Открыл: {app_name} (запомненный вариант)"
            except Exception:
                pass  # запись устарела — поищем заново ниже

    # 3. Ярлык в меню «Пуск» — покрывает почти все установленные программы.
    # Если нашли — запоминаем это название на будущее (это и есть
    # "самообучение": в следующий раз не придётся сканировать Пуск заново).
    shortcut = _find_shortcut(app_name)
    if shortcut:
        try:
            os.startfile(str(shortcut))
            _remember_app_alias(app_name, str(shortcut))
            return "done", f"Открыл: {shortcut.stem} (запомнил это название на будущее)"
        except Exception as e:
            return "error", f"Нашёл ярлык «{shortcut.stem}», но не смог его запустить: {e}"

    # 4. Известное место установки (Program Files / LocalAppData): ярлыка нет,
    #    но exe лежит в стандартной папке — «Telegram Desktop\Telegram.exe».
    app_exe = _find_app_exe(app_name)
    if app_exe:
        try:
            os.startfile(str(app_exe))
            _remember_app_alias(app_name, str(app_exe))
            return "done", f"Открыл: {app_exe.parent.name} (путь установки)"
        except Exception as e:
            return "error", f"Нашёл exe по пути установки, но не смог запустить: {e}"

    # 5. Store/MSIX-приложение (WindowsApps). У Store-приложений нет обычного
    #    .lnk в меню «Пуск», а папка WindowsApps закрыта ACL — обычный поиск
    #    их не видит. AppID узнаём через Get-StartApps и запускаем через
    #    shell:AppsFolder. (Telegram Desktop из Магазина Windows.)
    aumid = _launch_store_app(app_name)
    if aumid:
        _remember_app_alias(app_name, "aumid:" + aumid)
        return "done", f"Открыл: {app_name} (магазин Windows)"

    # 6. Последняя попытка — вдруг это точное имя .exe, известное системе
    # (зарегистрировано в PATH или реестре App Paths)
    try:
        os.startfile(app_name)
        return "done", f"Открыл: {app_name}"
    except Exception:
        return "error", (
            f"Не нашёл «{app_name}» ни среди известных программ, ни среди "
            "ярлыков в меню «Пуск». Попробуй назвать программу точнее — "
            "как она называется в меню «Пуск»."
        )


def _find_app_exe(name: str) -> Optional[Path]:
    r"""Находит exe приложение в известных местах установки (Program Files,
    LocalAppData\Programs) — для случая, когда ярлыка в меню «Пуск» нет
    (портативная/необычная установка). Ищем подпапку, в названии которой
    есть запрос («Telegram Desktop»), и внутри неё exe, в имени которого
    есть запрос («Telegram.exe»). Возвращает None, если не найдено."""
    name_l = (name or "").strip().lower()
    if not name_l or len(name_l) < 2:
        return None
    for root in _BROWSER_SEARCH_ROOTS:
        root_p = Path(root)
        if not root_p.is_dir():
            continue
        try:
            for sub in root_p.iterdir():
                if not sub.is_dir():
                    continue
                if name_l not in sub.name.lower():
                    continue
                for exe in sub.rglob("*.exe"):
                    if name_l in exe.name.lower():
                        return exe
        except Exception:
            continue
    return None


def _launch_store_app(name: str) -> Optional[str]:
    """Запускает Store/MSIX-приложение (WindowsApps) по названию через
    Get-StartApps + shell:AppsFolder. Возвращает AppID (AppUserModelID) или
    None, если такого приложения нет/не запустилось."""
    name_l = (name or "").strip().lower()
    if not name_l or len(name_l) < 2:
        return None
    import subprocess
    script = (
        "(Get-StartApps | Where-Object { $_.Name -like '*" + name_l
        + "*' } | Select-Object -First 1).AppID"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=15,
        ).stdout.decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line and "!" in line:  # AppID вида PackageFamilyName!AppId
            try:
                os.startfile("shell:AppsFolder\\" + line)
                return line
            except Exception:
                return None
    return None


def open_website(url: str) -> tuple[str, str]:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        # Без конкретного браузера — открываем тем, что задан "по умолчанию"
        # (или системным по умолчанию, если свой не настроен).
        _open_url_in_browser(url, None)
        return "done", f"Открыл сайт: {url}"
    except Exception as e:
        return "error", f"Не удалось открыть сайт «{url}»: {e}"


def open_file(path: str) -> tuple[str, str]:
    """Открывает файл или папку штатным ассоциативным приложением Windows."""
    try:
        resolved = resolve_path(path)
        if not resolved.exists():
            return "error", f"Файл не найден: {resolved}"
        os.startfile(str(resolved))
        return "done", f"Открыл: {resolved}"
    except Exception as e:
        return "error", f"Не удалось открыть «{path}»: {e}"


def open_folder(name: str) -> tuple[str, str]:
    """Открывает папку по имени. Сначала пытается по известному алиасу
    («загрузки», «документы»...) или абсолютному пути; если имя не совпало —
    ищет ПАПКУ с таким именем по диску и открывает первую найденную в
    Проводнике."""
    name = name.strip().strip('"').strip("'")
    if not name:
        return "error", "Не указано, какую папку открыть."

    # 1. Известный алиас системной папки ("открой папку загрузки")
    resolved = _resolve_folder(name)
    if resolved is not None and resolved.exists():
        try:
            os.startfile(str(resolved))
            return "done", f"Открыл папку: {resolved}"
        except Exception as e:
            return "error", f"Не удалось открыть папку «{name}»: {e}"

    # 2. Абсолютный путь или путь через resolve_path, который уже существует
    candidate = resolve_path(name)
    if candidate.is_dir():
        try:
            os.startfile(str(candidate))
            return "done", f"Открыл папку: {candidate}"
        except Exception as e:
            return "error", f"Не удалось открыть папку «{name}»: {e}"

    # 3. Поиск ПАПКИ по имени на диске (бюджет времени, как в search_file)
    name_lower = name.lower()
    found_dir = None
    deadline = time.time() + 25
    system_drive = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    for dirpath, dirnames, filenames in os.walk(system_drive):
        dirnames[:] = [d for d in dirnames if d.lower() not in _EXCLUDED_DIR_NAMES and not d.startswith(".")]
        for d in dirnames:
            if name_lower in d.lower():
                found_dir = Path(dirpath) / d
                break
        if found_dir is not None:
            break
        if time.time() > deadline:
            break

    if found_dir is None:
        return "error", (
            f"Не нашёл папку «{name}» ни среди известных папок, ни на диске. "
            "Попробуй точное имя или полный путь."
        )
    try:
        os.startfile(str(found_dir))
        return "done", f"Открыл папку: {found_dir}"
    except Exception as e:
        return "error", f"Нашёл папку «{found_dir}», но не смог открыть: {e}"


# ----- Поиск в интернете (конкретный поисковик + опционально конкретный браузер) -----
SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q=",
    "гугл": "https://www.google.com/search?q=",
    "yandex": "https://yandex.ru/search/?text=",
    "яндекс": "https://yandex.ru/search/?text=",
    "bing": "https://www.bing.com/search?q=",
    "бинг": "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
}

# Относительные пути, где чаще всего лежат браузеры на Windows — проверяем
# по очереди в стандартных папках программ, не полагаясь на PATH (браузеры
# туда себя обычно не добавляют).
_BROWSER_RELATIVE_PATHS = {
    "chrome": [r"Google\Chrome\Application\chrome.exe"],
    "opera": [r"Programs\Opera\launcher.exe", r"Opera\launcher.exe"],
    "firefox": [r"Mozilla Firefox\firefox.exe"],
    "edge": [r"Microsoft\Edge\Application\msedge.exe"],
    "yandex_browser": [r"Yandex\YandexBrowser\Application\browser.exe"],
}
BROWSER_ALIASES = {
    "chrome": "chrome", "хром": "chrome", "гугл хром": "chrome",
    "opera": "opera", "опера": "opera",
    "firefox": "firefox", "фаерфокс": "firefox", "файрфокс": "firefox", "мозилла": "firefox",
    "edge": "edge", "эдж": "edge",
    "yandex_browser": "yandex_browser", "яндекс": "yandex_browser", "яндекс браузер": "yandex_browser", "яндекс.браузер": "yandex_browser",
}
# Название, по которому искать ярлык в меню «Пуск», если браузер не нашёлся
# по стандартному пути установки — должно совпадать с тем, как реально
# называется ярлык в системе (кириллица для Яндекс.Браузера и т.п.).
_BROWSER_SHORTCUT_HINTS = {
    "yandex_browser": "яндекс браузер",
}
_BROWSER_SEARCH_ROOTS = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.environ.get("LocalAppData", ""),
]


def _find_browser_exe(browser_key: str) -> Optional[Path]:
    rel_paths = _BROWSER_RELATIVE_PATHS.get(browser_key, [])
    for root in _BROWSER_SEARCH_ROOTS:
        if not root:
            continue
        for rel in rel_paths:
            candidate = Path(root) / rel
            if candidate.exists():
                return candidate
    # Запасной вариант — вдруг есть ярлык в меню «Пуск» (используем
    # "человеческое" название для поиска, а не внутренний ключ)
    shortcut_hint = _BROWSER_SHORTCUT_HINTS.get(browser_key, browser_key)
    return _find_shortcut(shortcut_hint)


def _open_url_in_browser(url: str, browser_key: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Открывает URL в заданном браузере (или настроенном по умолчанию, или
    системном). Возвращает (error, browser_label): при ошибке — текст ошибки
    в error, при успехе error=None и browser_label — человекочитаемое имя
    браузера для ответа пользователю."""
    # Если явный браузер не указан — берём настроенный "по умолчанию" (если есть)
    if not browser_key or browser_key in ("default", "браузер по умолчанию"):
        browser_key = agent_config.get_browser()

    if browser_key and browser_key not in ("default", "браузер по умолчанию"):
        browser_key = BROWSER_ALIASES.get(browser_key.lower(), browser_key.lower())
        exe = _find_browser_exe(browser_key)
        if exe is None:
            known = ", ".join(sorted(set(BROWSER_ALIASES.values())))
            return (
                f"Не нашёл браузер «{browser_key}» на этом ПК. Известные: {known}",
                None,
            )
        try:
            subprocess.Popen([str(exe), url])
            return None, browser_key
        except Exception as e:
            return f"Нашёл браузер «{browser_key}», но не смог его запустить: {e}", None

    # Системный браузер по умолчанию
    webbrowser.open(url)
    return None, "браузер по умолчанию"


def set_default_browser(browser: str) -> tuple[str, str]:
    """Запоминает, каким браузером открывать сайты/поиск, если команда
    не назвала конкретный. Значение хранится в .env агента."""
    browser_key = BROWSER_ALIASES.get(browser.strip().lower(), browser.strip().lower())
    if browser_key not in BROWSER_ALIASES.values():
        known = ", ".join(sorted(set(BROWSER_ALIASES.values())))
        return "error", f"Не знаю браузер «{browser}». Известные: {known}"
    agent_config.default_browser = browser_key
    agent_config.update(default_browser=browser_key)
    return "done", f"Теперь сайты и поиск открываю в: {browser_key}."


def web_search(query: str, engine: Optional[str] = None, browser: Optional[str] = None) -> tuple[str, str]:
    if not query or not query.strip():
        return "error", "Не указано, что искать."

    engine_key = (engine or "google").strip().lower()
    search_url_base = SEARCH_ENGINES.get(engine_key, SEARCH_ENGINES["google"])
    url = search_url_base + urllib.parse.quote(query.strip())
    engine_label = engine_key if engine_key in SEARCH_ENGINES else "google"

    error, browser_label = _open_url_in_browser(url, browser)
    if error:
        return "error", error
    return "done", f"Ищу «{query}» в {engine_label} через {browser_label}."


# ----- Файлы -----
def create_file(filename: str, content: str) -> tuple[str, str]:
    try:
        path = resolve_path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content or "", encoding="utf-8")
        return "done", f"Файл создан: {path}"
    except Exception as e:
        return "error", f"Не удалось создать файл «{filename}»: {e}"


def read_file(filename: str) -> tuple[str, str]:
    try:
        path = resolve_path(filename)
        if not path.exists():
            return "error", f"Файл не найден: {path}"
        text = path.read_text(encoding="utf-8", errors="replace")
        preview = text[:3000]
        if len(text) > 3000:
            preview += "\n... (обрезано)"
        return "done", preview
    except Exception as e:
        return "error", f"Не удалось прочитать файл «{filename}»: {e}"


def delete_file(filename: str) -> tuple[str, str]:
    try:
        path = resolve_path(filename)
        if not path.exists():
            return "error", f"Файл не найден: {path}"
        path.unlink()
        return "done", f"Файл удалён: {path}"
    except Exception as e:
        return "error", f"Не удалось удалить файл «{filename}»: {e}"


def edit_file(filename: str, mode: str, content: str) -> tuple[str, str]:
    """mode: 'append' — дописать в конец, 'overwrite' — заменить содержимое целиком."""
    try:
        path = resolve_path(filename)
        if mode == "append":
            if not path.exists():
                return "error", f"Файл не найден: {path}"
            with path.open("a", encoding="utf-8") as f:
                f.write(("\n" if path.stat().st_size > 0 else "") + content)
            return "done", f"Дописал в файл: {path}"
        else:  # overwrite
            path.write_text(content, encoding="utf-8")
            return "done", f"Файл перезаписан: {path}"
    except Exception as e:
        return "error", f"Не удалось изменить файл «{filename}»: {e}"


def copy_file(src: str, dst: str) -> tuple[str, str]:
    try:
        src_path = resolve_path(src)
        if not src_path.exists():
            return "error", f"Исходный файл не найден: {src_path}"
        dst_path = resolve_path(dst)
        if dst_path.is_dir() or (not dst_path.suffix and not dst_path.exists()):
            dst_path.mkdir(parents=True, exist_ok=True)
            dst_path = dst_path / src_path.name
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
        return "done", f"Скопировал: {src_path} → {dst_path}"
    except Exception as e:
        return "error", f"Не удалось скопировать «{src}» в «{dst}»: {e}"


def move_file(src: str, dst: str) -> tuple[str, str]:
    try:
        src_path = resolve_path(src)
        if not src_path.exists():
            return "error", f"Исходный файл не найден: {src_path}"
        dst_path = resolve_path(dst)
        if dst_path.is_dir() or (not dst_path.suffix and not dst_path.exists()):
            dst_path.mkdir(parents=True, exist_ok=True)
            dst_path = dst_path / src_path.name
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
        return "done", f"Перенёс: {src_path} → {dst_path}"
    except Exception as e:
        return "error", f"Не удалось перенести «{src}» в «{dst}»: {e}"


def list_files(folder_hint: str) -> tuple[str, str]:
    folder = _resolve_folder(folder_hint)
    if folder is None:
        known = ", ".join(sorted({"рабочий стол", "загрузки", "документы", "картинки", "eva"}))
        return "error", f"Не знаю такую папку. Известные: {known} (или полный путь)."
    if not folder.exists():
        return "error", f"Папка не найдена: {folder}"
    try:
        items = sorted(folder.iterdir(), key=lambda p: p.name.lower())[:50]
        if not items:
            return "done", f"Папка «{folder}» пуста."
        listing = "\n".join(f"{'📁' if p.is_dir() else '📄'} {p.name}" for p in items)
        return "done", f"Содержимое «{folder}»:\n{listing}"
    except Exception as e:
        return "error", f"Не удалось прочитать папку «{folder}»: {e}"


# Папки/паттерны, которые пропускаем при поиске по всему диску — либо
# системные (трогать не нужно), либо огромные и бесполезные для обычного
# поиска пользовательских файлов (замедлили бы поиск в разы).
_EXCLUDED_DIR_NAMES = {
    "windows", "programdata", "$recycle.bin", "system volume information",
    "node_modules", "appdata", "$windows.~bt", "$windows.~ws",
}


def _walk_with_budget(root: Path, query_lower: str, matches: list, deadline: float, limit: int = 20) -> bool:
    """Ищет query_lower среди имён папок И файлов внутри root. Возвращает
    True, если наткнулись на дедлайн по времени раньше, чем закончили (то
    есть результат может быть неполным)."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Отсекаем "тяжёлые"/системные ветки ДО того, как os.walk в них зайдёт
        dirnames[:] = [d for d in dirnames if d.lower() not in _EXCLUDED_DIR_NAMES and not d.startswith(".")]

        for d in dirnames:
            if query_lower in d.lower():
                matches.append(f"📁 {Path(dirpath) / d}")
                if len(matches) >= limit:
                    return False
        for name in filenames:
            if query_lower in name.lower():
                matches.append(f"📄 {Path(dirpath) / name}")
                if len(matches) >= limit:
                    return False

        if time.time() > deadline:
            return True
    return False


def search_file(query: str) -> tuple[str, str]:
    query_lower = query.strip().lower()
    if not query_lower:
        return "error", "Не указано, что искать."

    matches: list = []

    # 1. Сначала быстрые приоритетные папки (EvaFiles, Рабочий стол,
    # Загрузки, Документы, Видео, Музыка) — почти всегда там и находится
    # то, что реально ищут.
    fast_deadline = time.time() + 8
    for root in SEARCH_ROOTS:
        if root.exists():
            _walk_with_budget(root, query_lower, matches, fast_deadline)
        if len(matches) >= 20:
            break

    # 2. Не нашли — расширяем поиск на весь диск (с исключениями и своим
    # бюджетом времени, чтобы не зависнуть на огромном диске навсегда).
    timed_out = False
    if not matches:
        system_drive = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
        full_deadline = time.time() + 25
        timed_out = _walk_with_budget(Path(system_drive), query_lower, matches, full_deadline)

    if not matches:
        return "done", f"Ничего не нашёл по запросу «{query}» — ни в обычных папках, ни на всём диске."

    result = "Найдено:\n" + "\n".join(matches)
    if timed_out:
        result += "\n\n⏱ Поиск по всему диску остановлен по времени — возможно, есть ещё совпадения."
    return "done", result


# ----- Скриншот -----
def take_screenshot() -> tuple[str, str, Optional[bytes]]:
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "done", "Скриншот сделан.", buf.getvalue()
    except Exception as e:
        return "error", f"Не удалось сделать скриншот: {e}"


# ----- Внутренний взгляд (look_at_screen) -----
def _ask_model_about_image(question: str, image_b64: str) -> str:
    """Посылает скриншот vision-модели (Ollama), возвращает текстовый ответ.
    Выделена отдельной функцией, чтобы тесты могли подменить вызов, не запуская
    Ollama и не захватывая экран."""
    import ollama
    from config import config as agent_config

    resp = ollama.chat(
        model=agent_config.ollama_model,
        messages=[{"role": "user", "content": question, "images": [image_b64]}],
    )
    return resp["message"]["content"].strip() or "Ничего не видно на экране."


def look_at_screen(question: str = "Что открыто на экране?") -> tuple[str, str]:
    """Внутренний взгляд: скриншот + вопрос vision-модели, что видно на экране.

    Возвращает ТЕКСТ-описание того, что видно — это вход для следующего шага
    планирования, а не картинка. Картинка:
    - живёт только в памяти этой функции (не сохраняется на диск);
    - не уходит в Telegram (это не шаг screenshot, image_bytes здесь не
      формируется — см. execute_action).
    """
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        # Сжимаем до ширины 1280 — полный 1080p модели ни к чему, это дольше
        # и дороже; модель отлично различает окна/вкладки и на меньшем размере.
        max_w = 1280
        if img.width > max_w:
            h = int(img.height * max_w / img.width)
            img = img.resize((max_w, h))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "done", _ask_model_about_image(question, b64)
    except Exception as e:
        return "error", f"Не удалось посмотреть на экран: {e}"


def find_element_on_screen(description: str, screen_width: int = 1920, screen_height: int = 1080) -> tuple[str, Optional[dict]]:
    """Находит элемент на экране по визуальному описанию через VLM.

    Возвращает (status, coords_dict | None):
    - status: 'found' | 'not_found' | 'error'
    - coords_dict: {'x': int, 'y': int, 'width': int, 'height': int} в экранных координатах

    Скриншот живёт только в памяти, не сохраняется на диск.
    Модель возвращает координаты в процентах от размера изображения,
    функция конвертирует в экранные координаты.
    """
    try:
        from PIL import ImageGrab
        import json
        import re

        img = ImageGrab.grab()
        orig_w, orig_h = img.size

        # Сжимаем для VLM (быстрее + дешевле)
        max_w = 1280
        if img.width > max_w:
            scale = max_w / img.width
            h = int(img.height * scale)
            img = img.resize((max_w, h))
        else:
            scale = 1.0

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        # Промпт для VLM — просим JSON с координатами
        prompt = f"""Найди на скриншоте элемент: "{description}".
Если элемент найден, верни JSON с координатами в формате:
{{"found": true, "x": 0.5, "y": 0.3, "width": 0.1, "height": 0.05}}
где x, y — центр элемента (0.0–1.0 от ширины/высоты), width/height — размер (0.0–1.0).
Если элемент не найден, верни: {{"found": false}}
Возвращай ТОЛЬКО JSON, без объяснений."""

        response = _ask_model_about_image(prompt, b64)

        # Парсим JSON из ответа
        json_match = re.search(r'\{[^}]+\}', response)
        if not json_match:
            return "not_found", None

        data = json.loads(json_match.group())
        if not data.get("found"):
            return "not_found", None

        # Конвертируем проценты в экранные координаты
        coords = {
            "x": int(data["x"] * orig_w),
            "y": int(data["y"] * orig_h),
            "width": int(data.get("width", 0.1) * orig_w),
            "height": int(data.get("height", 0.05) * orig_h),
        }
        return "found", coords

    except Exception as e:
        return "error", None


# ----- Осознание экрана: состояние + наблюдатели -----
def get_screen_state() -> tuple[str, str]:
    """Мгновенная картина экрана (без скриншота и VLM)."""
    from screen_watcher import get_screen_state as _gs
    st = _gs()
    aw = st.get("active_window") or {}
    title = aw.get("title") or "—"
    mode = st.get("mode", "normal")
    if mode == "locked":
        return "done", "Экран заблокирован или выключен."
    recent = st.get("recent_events") or []
    changes = [e for e in recent if e.get("t") == "change"]
    line = f"Активно на экране: {title}."
    if changes:
        line += f" Недавно менялось (последнее {len(changes)} изменений за {len(recent)} событий)."
    return "done", line


def start_watch(params: dict) -> tuple[str, str]:
    from watchers import WatchStore
    store = WatchStore()
    rules = store.load()
    target = (params.get("target") or "").strip()
    if not target:
        return "error", "Не указано, за чем следить (target)."
    when = params.get("when") or "change"
    if when not in ("change", "appear", "disappear"):
        return "error", f"Неизвестный режим when={when}. Нужен change|appear|disappear."
    what = params.get("what") or f"[{when}] {target}"
    r = store.add(rules, target, when, what)
    # Фоновая слежка отключена: правила складываются на диск, но активного
    # WatchManager нет, поэтому уведомления не идут. Агент отвечает по запросу.
    return "done", f"Правило записано: «{target}» ({when}). Сообщу: {what}"


def stop_watch(params: dict) -> tuple[str, str]:
    from watchers import WatchStore
    store = WatchStore()
    target = (params.get("target") or "").strip()
    if store.remove(store.load(), target):
        return "done", f"Наблюдение за «{target}» остановлено."
    return "done", f"Наблюдение за «{target}» не найдено."


def list_watches(params: dict) -> tuple[str, str]:
    from watchers import WatchStore
    rules = WatchStore().load()
    if not rules:
        return "done", "Активных наблюдений нет."
    lines = [f"- {r.target} ({r.when}): {r.what}" for r in rules]
    return "done", "Наблюдаю за:\n" + "\n".join(lines)


# ----- Громкость -----
def set_volume(direction: Optional[str], level: Optional[int]) -> tuple[str, str]:
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))

        if level is not None:
            volume.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100, None)
            return "done", f"Громкость выставлена на {level}%."
        if direction == "mute":
            volume.SetMute(1, None)
            return "done", "Звук выключен."
        if direction == "unmute":
            volume.SetMute(0, None)
            return "done", "Звук включён."
        current = volume.GetMasterVolumeLevelScalar()
        step = 0.1 if direction == "up" else -0.1
        new_level = max(0.0, min(1.0, current + step))
        volume.SetMasterVolumeLevelScalar(new_level, None)
        return "done", f"Громкость теперь {round(new_level * 100)}%."
    except Exception as e:
        return "error", f"Не удалось изменить громкость (нужны pycaw/comtypes): {e}"


# ----- Питание ПК -----
def power_action(action: str) -> tuple[str, str]:
    try:
        if action == "shutdown":
            subprocess.run(["shutdown", "/s", "/t", "5"], check=True)
            return "done", "Выключаю компьютер через 5 секунд."
        if action == "restart":
            subprocess.run(["shutdown", "/r", "/t", "5"], check=True)
            return "done", "Перезагружаю компьютер через 5 секунд."
        if action == "lock":
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            return "done", "Компьютер заблокирован."
        return "error", f"Неизвестное действие питания: {action}"
    except Exception as e:
        return "error", f"Не удалось выполнить действие с питанием: {e}"


# ----- Базовое управление внутри программ -----
# Печатает/нажимает клавиши в ТО ОКНО, КОТОРОЕ СЕЙЧАС В ФОКУСЕ — команду
# логично слать следом за "открой <программа>", дав окну секунду-другую
# на открытие, а не одним "магическим" действием, которое само гадает,
# в какое окно печатать.
#
# Используем библиотеку keyboard, а не pyautogui — она полноценно
# поддерживает кириллицу через SendInput(KEYEVENTF_UNICODE), тогда как
# pyautogui.typewrite умеет только базовую латиницу/ASCII.
#
# Ограничение ОС: если окно в фокусе запущено с правами администратора,
# а сам агент — нет (или наоборот), Windows заблокирует ввод между
# процессами с разным уровнем прав (UAC) — это защита самой системы,
# обойти её нельзя и не нужно.
def type_text(text: str) -> tuple[str, str]:
    try:
        import keyboard
        time.sleep(0.3)  # небольшая пауза — на случай, если окно ещё не в фокусе
        keyboard.write(text, delay=0.01)
        return "done", f"Напечатал: {text[:80]}{'...' if len(text) > 80 else ''}"
    except Exception as e:
        return "error", f"Не удалось напечатать текст (нужна библиотека keyboard): {e}"


def press_key(keys: str) -> tuple[str, str]:
    try:
        import keyboard
        combo = keys.strip().lower().replace(" ", "")
        time.sleep(0.3)
        keyboard.press_and_release(combo)
        return "done", f"Нажал: {keys}"
    except Exception as e:
        return "error", f"Не удалось нажать «{keys}» (нужна библиотека keyboard): {e}"


# ----- Управление мышью (pyautogui) -----
# Координаты — в пикселях экрана, (0,0) — левый верхний угол. Противоположный
# угол = разрешение экрана (например 1920x1080). pyautogui имеет защитный
# "убийственный" предохранитель: резкое движение курсора в угол (по умолчанию
# 10 сек наезда) прерывает скрипт — для аварийного останова, тут не мешает.
def _pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = False  # отключаем, чтобы случайные движения мыши не рвали команду
    pyautogui.PAUSE = 0.2
    return pyautogui


def mouse_move(x: int, y: int) -> tuple[str, str]:
    try:
        pg = _pyautogui()
        pg.moveTo(x, y, duration=0.3)
        return "done", f"Курсор перемещён в ({x}, {y})."
    except Exception as e:
        return "error", f"Не удалось переместить курсор (нужна библиотека pyautogui): {e}"


def mouse_click(x: int, y: int, button: str = "left") -> tuple[str, str]:
    try:
        pg = _pyautogui()
        pg.moveTo(x, y, duration=0.2)
        pg.click(x, y, button=button)
        label = {"left": "левой", "right": "правой", "middle": "средней"}.get(button, button)
        return "done", f"Кликнул {label} кнопкой мыши в ({x}, {y})."
    except Exception as e:
        return "error", f"Не удалось кликнуть мышью (нужна библиотека pyautogui): {e}"


def mouse_scroll(direction: str, amount: int = 3) -> tuple[str, str]:
    try:
        pg = _pyautogui()
        clicks = amount if direction == "up" else -amount
        pg.scroll(clicks)
        return "done", f"Прокрутил {direction} на {amount}."
    except Exception as e:
        return "error", f"Не удалось прокрутить (нужна библиотека pyautogui): {e}"


def mouse_drag(x_start: int, y_start: int, x_end: int, y_end: int) -> tuple[str, str]:
    try:
        pg = _pyautogui()
        pg.moveTo(x_start, y_start, duration=0.2)
        pg.mouseDown()
        pg.moveTo(x_end, y_end, duration=0.5)
        pg.mouseUp()
        return "done", f"Перетащил из ({x_start}, {y_start}) в ({x_end}, {y_end})."
    except Exception as e:
        return "error", f"Не удалось перетащить (нужна библиотека pyautogui): {e}"


# ----- Продвинутое управление внутри программ: поиск кнопок/меню по названию -----
# В отличие от type_text/press_key (которые "вслепую" шлют ввод в активное
# окно), pywinauto реально находит окно по названию и элемент управления
# (кнопку, пункт меню) внутри него по имени — надёжнее для классических
# Windows-программ, менее предсказуемо для некоторых современных
# (Electron/UWP) приложений, но обычно работает и там через UI Automation.
_BROWSER_MARKERS = ("chrome", "msedge", "edge", "opera", "firefox", "yandex")
_BROWSER_HINTS = ("браузер", "хром", "chrome", "browser", "edge", "opera", "firefox", "яндекс", "yandex")


def _words(text: str) -> set[str]:
    import re as _re
    return set(_re.findall(r"[a-zа-яё0-9]+", (text or "").lower()))


def _score_window(title_hint: str, candidate: str) -> int:
    """Сколько слов подсказки встречается в реальном заголовке окна.

    Русские словоформы («вкладка»/«вкладку») не сводятся по лемме, поэтому
    слово считается совпавшим, если у него и у кандидата общий корень длиной
    не меньше MIN_STEM — это и есть «близость по словам» для fuzzy-подбора.
    """
    MIN_STEM = 4
    a = sorted(_words(title_hint))
    b = sorted(_words(candidate))
    score = 0
    used = set()
    for wa in a:
        for wb in b:
            if wb in used:
                continue
            common = 0
            for k in range(min(len(wa), len(wb)), MIN_STEM - 1, -1):
                if wa[:k] == wb[:k]:
                    common = k
                    break
            if common >= MIN_STEM:
                score += 1
                used.add(wb)
                break
    return score


def _pick_window(candidates, title_hint: str):
    """Выбирает лучшее окно среди (заголовок, класс) по подсказке модели.

    Модель переплана не всегда знает точный заголовок — она может назвать
    окно «браузер» или переврать имя. Поэтому порядок:
    1) точная подстрока — как было раньше;
    2) браузер-подсказка («браузер», «хром»...) — берём любое окно браузерного
       класса (Chrome/Edge/Opera/Firefox/Яндекс);
    3) максимальное пересечение слов с заголовком — страховка от лёгких
       переделок имени.
    Возвращает заголовок выбранного окна или None, если совпадений нет.
    """
    hint = (title_hint or "").strip().lower()
    if hint:
        for title, _cls in candidates:
            if hint in (title or "").lower():
                return title
        if any(w in hint for w in _BROWSER_HINTS):
            for title, cls in candidates:
                if any(m in (cls or "").lower() for m in _BROWSER_MARKERS):
                    return title
    best_title, best_score = None, 0
    for title, _cls in candidates:
        sc = _score_window(hint, title)
        if sc > best_score:
            best_title, best_score = title, sc
    return best_title


def list_window_titles() -> list[str]:
    """Реальные заголовки открытых окон — подмешиваются в переплан цикла
    взгляда, чтобы модель кликала по настоящему имени окна, а не по тому,
    что она придумала по скриншоту."""
    from pywinauto import Desktop
    out = []
    for w in Desktop(backend="uia").windows():
        try:
            t = (w.window_text() or "").strip()
            if t and t not in out:
                out.append(t)
        except Exception:
            continue
    return out


def _find_window(title_hint: str):
    from pywinauto import Desktop
    windows = list(Desktop(backend="uia").windows())
    candidates = []
    for w in windows:
        try:
            candidates.append((w.window_text() or "", w.class_name() or ""))
        except Exception:
            continue
    best_title = _pick_window(candidates, title_hint)
    if best_title is None:
        return None
    for w in windows:
        try:
            if (w.window_text() or "") == best_title:
                return w
        except Exception:
            continue
    return None


def _find_control(win, control_hint: str, control_type: str):
    """Ищет контроль в окне по типу (Button/TabItem/...) и по подстроке имени.

    Перебирает `win.descendants()` — как ui_list_controls: на UIAWrapper нет
    метода child_window(), искать надо по дереву элементов."""
    import re as _re
    pat = _re.compile(_re.escape(control_hint.strip()), _re.I)
    for c in win.descendants():
        try:
            if c.friendly_class_name() != control_type:
                continue
            if pat.search(c.window_text() or ""):
                return c
        except Exception:
            continue
    return None


def ui_click(window_hint: str, control_hint: str) -> tuple[str, str]:
    try:
        win = _find_window(window_hint)
        if win is None:
            return "error", f"Не нашёл окно «{window_hint}» — убедись, что программа открыта."
        win.set_focus()

        control = _find_control(win, control_hint, "Button")
        if control is None:
            # Вкладки (Chrome, Edge, Opera) — это TabItem, а не кнопка.
            control = _find_control(win, control_hint, "TabItem")
        if control is None:
            return "error", f"Не нашёл элемент «{control_hint}» в окне «{window_hint}»."
        control.click_input()
        return "done", f"Нажал «{control_hint}» в окне «{window_hint}»."
    except Exception as e:
        return "error", f"Не удалось кликнуть «{control_hint}» в «{window_hint}» (нужна библиотека pywinauto): {e}"


def ui_list_controls(window_hint: str) -> tuple[str, str]:
    """Показывает названия элементов в окне — помогает понять, как именно
    называется нужная кнопка/пункт меню для команды ui_click."""
    try:
        win = _find_window(window_hint)
        if win is None:
            return "error", f"Не нашёл окно «{window_hint}» — убедись, что программа открыта."
        items = []
        for c in win.descendants():
            try:
                text = c.window_text()
                if text:
                    items.append(f"{c.friendly_class_name()}: {text}")
            except Exception:
                continue
            if len(items) >= 40:
                break
        if not items:
            return "done", f"В окне «{window_hint}» не нашёл именованных элементов."
        return "done", f"Элементы окна «{window_hint}»:\n" + "\n".join(items)
    except Exception as e:
        return "error", f"Не удалось получить элементы окна (нужна библиотека pywinauto): {e}"


def close_app(window_hint: str) -> tuple[str, str]:
    """Закрывает окно программы (посылает штатный сигнал закрытия — как
    нажатие крестика, а не принудительное убийство процесса), чтобы у
    программы был шанс спросить про сохранение перед закрытием."""
    try:
        win = _find_window(window_hint)
        if win is None:
            return "error", f"Не нашёл окно «{window_hint}» — убедись, что программа открыта."
        win.close()
        return "done", f"Закрыл окно «{window_hint}»."
    except Exception as e:
        return "error", f"Не удалось закрыть «{window_hint}»: {e}"


def minimize_window(window_hint: str) -> tuple[str, str]:
    try:
        win = _find_window(window_hint)
        if win is None:
            return "error", f"Не нашёл окно «{window_hint}» — убедись, что программа открыта."
        win.set_focus()
        try:
            win.minimize()
        except Exception:
            # Не все окна поддерживают программное сворачивание через UIA —
            # запасной вариант: горячая клавиша Win+↓ для окна в фокусе.
            import keyboard
            time.sleep(0.2)
            keyboard.send("win+down")
        return "done", f"Свернул окно «{window_hint}»."
    except Exception as e:
        return "error", f"Не удалось свернуть «{window_hint}»: {e}"


def maximize_window(window_hint: str) -> tuple[str, str]:
    try:
        win = _find_window(window_hint)
        if win is None:
            return "error", f"Не нашёл окно «{window_hint}» — убедись, что программа открыта."
        win.set_focus()
        try:
            win.maximize()
        except Exception:
            import keyboard
            time.sleep(0.2)
            keyboard.send("win+up")
        return "done", f"Развернул окно «{window_hint}»."
    except Exception as e:
        return "error", f"Не удалось развернуть «{window_hint}»: {e}"


def ui_type_text(window_hint: str, control_hint: str, text: str) -> tuple[str, str]:
    """Вводит текст НАПРЯМУЮ в конкретное найденное поле — надёжнее, чем
    вслепую печатать в то окно, которое сейчас в фокусе (type_text).

    Ищет поле через win.descendants() — UIAWrapper (от Desktop.windows())
    НЕ имеет метода child_window()."""
    try:
        win = _find_window(window_hint)
        if win is None:
            return "error", f"Не нашёл окно «{window_hint}» — убедись, что программа открыта."
        win.set_focus()

        import re as _re
        pat = _re.compile(_re.escape(control_hint.strip()), _re.I)
        control = None
        for c in win.descendants():
            try:
                if pat.search(c.window_text() or ""):
                    control = c
                    break
            except Exception:
                continue
        if control is None:
            return "error", f"Не нашёл поле «{control_hint}» в окне «{window_hint}»."

        try:
            control.set_text(text)  # мгновенно и надёжно для обычных полей ввода
        except Exception:
            control.click_input()  # запасной вариант — как будто напечатали руками
            control.type_keys(text, with_spaces=True)
        return "done", f"Ввёл текст в «{control_hint}» (окно «{window_hint}»)."
    except Exception as e:
        return "error", f"Не удалось ввести текст в «{control_hint}» (нужна библиотека pywinauto): {e}"


def ui_click_by_text_impl(params: dict) -> bool:
    import ui_automation as ui
    return ui.click_by_text(params.get("window", "") or ui.get_active_window(),
                            params.get("text", ""))


def ui_set_text_impl(params: dict) -> bool:
    import ui_automation as ui
    return ui.set_text(params.get("window", "") or ui.get_active_window(),
                       params.get("text", ""))


def ui_select_item_impl(params: dict) -> bool:
    import ui_automation as ui
    return ui.select_item(params.get("window", "") or ui.get_active_window(),
                          params.get("text", ""))


def execute_action(action: str, params: dict) -> tuple[str, str, Optional[bytes]]:
    """Единая точка входа — принимает разобранное действие от сервера,
    возвращает (status, result_text, image_bytes). status: 'done' | 'error'.
    image_bytes заполнен только для screenshot, иначе None."""
    if action == "open_app":
        status, text = open_app(params.get("app", ""))
    elif action == "open_website":
        status, text = open_website(params.get("url", ""))
    elif action == "open_file":
        status, text = open_file(params.get("path", ""))
    elif action == "open_folder":
        status, text = open_folder(params.get("name", ""))
    elif action == "web_search":
        status, text = web_search(params.get("query", ""), params.get("engine"), params.get("browser"))
    elif action == "set_default_browser":
        status, text = set_default_browser(params.get("browser", ""))
    elif action == "create_file":
        status, text = create_file(params.get("path", ""), params.get("content", ""))
    elif action == "read_file":
        status, text = read_file(params.get("path", ""))
    elif action == "delete_file":
        status, text = delete_file(params.get("path", ""))
    elif action == "edit_file":
        status, text = edit_file(params.get("path", ""), params.get("mode", "append"), params.get("content", ""))
    elif action == "copy_file":
        status, text = copy_file(params.get("src", ""), params.get("dst", ""))
    elif action == "move_file":
        status, text = move_file(params.get("src", ""), params.get("dst", ""))
    elif action == "list_files":
        status, text = list_files(params.get("folder", ""))
    elif action == "search_file":
        status, text = search_file(params.get("query", ""))
    elif action == "set_volume":
        status, text = set_volume(params.get("direction"), params.get("level"))
    elif action == "power_action":
        status, text = power_action(params.get("action", ""))
    elif action == "type_text":
        status, text = type_text(params.get("text", ""))
    elif action == "press_key":
        status, text = press_key(params.get("keys", ""))
    elif action == "mouse_move":
        status, text = mouse_move(int(params.get("x", 0)), int(params.get("y", 0)))
    elif action == "mouse_click":
        status, text = mouse_click(int(params.get("x", 0)), int(params.get("y", 0)), params.get("button", "left"))
    elif action == "mouse_scroll":
        status, text = mouse_scroll(params.get("direction", "down"), int(params.get("amount", 3)))
    elif action == "mouse_drag":
        status, text = mouse_drag(
            int(params.get("x_start", 0)), int(params.get("y_start", 0)),
            int(params.get("x_end", 0)), int(params.get("y_end", 0)),
        )
    elif action == "ui_click":
        status, text = ui_click(params.get("window", ""), params.get("control", ""))
    elif action == "ui_list_controls":
        status, text = ui_list_controls(params.get("window", ""))
    elif action == "close_app":
        status, text = close_app(params.get("window", ""))
    elif action == "minimize_window":
        status, text = minimize_window(params.get("window", ""))
    elif action == "maximize_window":
        status, text = maximize_window(params.get("window", ""))
    elif action == "ui_type_text":
        status, text = ui_type_text(params.get("window", ""), params.get("control", ""), params.get("text", ""))
    elif action == "screenshot":
        status, text, image_bytes = take_screenshot()
        return status, text, image_bytes
    elif action == "look_at_screen":
        # Внутренний взгляд: возвращаем ТОЛЬКО текст о том, что видно.
        # image_bytes намеренно None — картинка не уедет в Telegram.
        status, text = look_at_screen(params.get("question", "Что открыто на экране?"))
        return status, text, None
    elif action == "get_screen_state":
        status, text = get_screen_state()
    elif action == "start_watch":
        status, text = start_watch(params)
    elif action == "stop_watch":
        status, text = stop_watch(params)
    elif action == "list_watches":
        status, text = list_watches(params)
    elif action == "ui_do_task":
        from ui_agent import run_ui_task  # ленивый импорт: uia приезжает только тут
        status, text = run_ui_task(params.get("goal", ""), params.get("window"))
    elif action == "ui_click_by_text":
        status, text = (("done", "Кликнула по элементу.")
                        if ui_click_by_text_impl(params)
                        else ("error", "Не нашла элемент с таким текстом."))
    elif action == "ui_set_text":
        status, text = (("done", "Ввела текст в поле.")
                        if ui_set_text_impl(params)
                        else ("error", "Не удалось ввести текст."))
    elif action == "ui_select_item":
        status, text = (("done", "Выбрала элемент из списка.")
                        if ui_select_item_impl(params)
                        else ("error", "Не нашла элемент в списке."))
    elif action == "vlm_click":
        from vlm_actions import vlm_click  # ленивый импорт, как у ui_do_task
        res = vlm_click(
            params.get("description", ""),
            uia_text=params.get("text"),
            window_title=params.get("window"),
            expect=params.get("expect"),
        )
        status = "done" if res["ok"] else "error"
        text = res["message"]
    else:
        status, text = "error", f"Неизвестное действие: {action}"
    return status, text, None