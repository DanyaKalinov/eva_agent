"""
WebSocket-клиент desktop-агента. Держит соединение с сервером Евы,
переподключается с задержкой при обрыве, выполняет входящие команды
через actions.execute_action и отправляет результат обратно.

Также отвечает за голосовую сторону:
- принимает {"type": "speak", "text": ...} от сервера (например, ответ
  ask_web) и озвучивает его через TTS (voice.py);
- если входящая команда помечена voice=true (пришла изначально с голоса),
  после выполнения не только шлёт результат на сервер как обычно, но и
  озвучивает его локально;
- предоставляет submit_voice_command() — вызывается из ОТДЕЛЬНОГО потока
  прослушивания wake word (voice.py), потокобезопасно передаёт
  распознанный текст в этот asyncio-цикл, который отправит его на сервер
  как {"type": "voice_command", "text": ...}.

Работает в фоновом asyncio-цикле в отдельном потоке — сам pywebview
должен жить в главном потоке (так требует Windows), поэтому UI (app.py)
и сеть (этот модуль) общаются только через потокобезопасный лог-буфер
и объект config, а не напрямую вызывают друг друга.
"""
import asyncio
import base64
import json
import uuid
from collections import deque
from datetime import datetime
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from local_brain import build_plan, classify_steps, ensure_ollama_ready

from actions import execute_action, list_window_titles
from config import config
from screen_watcher import describe_screen, get_screen_state
import notifications
import pending_intent

# Буферы для UI-чата (приложение читает через Api).
CHAT_BUFFER: deque = deque(maxlen=200)

# Последние N событий для отображения в UI (вкладка «Лог»).
LOG_BUFFER: deque[dict] = deque(maxlen=300)
_connected = False
_current_ws = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_voice_command_ids: set = set()


def is_connected() -> bool:
    return _connected


def _log(message: str, level: str = "info") -> None:
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,  # info | success | error
        "message": message,
    }
    LOG_BUFFER.append(entry)
    print(f"[{entry['time']}] {message}", flush=True)


def push_ui_message(role: str, text: str) -> None:
    CHAT_BUFFER.append({"role": role, "text": text, "time": datetime.now().strftime("%H:%M:%S")})


def get_ui_chat() -> list:
    return list(CHAT_BUFFER)


def prepare_chat_payload(message_id: int, plan: dict) -> dict:
    return {"type": "chat_result", "message_id": message_id, "kind": plan.get("kind", "answer"), "text": plan.get("text", "")}


def resolve_plan(plan: dict, message_id: int, origin: str):
    """Возвращает (msg_type, payload_to_send). msg_type: 'chat_result' | 'confirm' | 'cloud_action'."""
    kind = plan.get("kind")
    if kind == "answer":
        return "chat_result", prepare_chat_payload(message_id, plan)
    if kind == "need_destination":
        return "chat_result", prepare_chat_payload(message_id, plan)
    if kind == "cloud":
        return "cloud_action", {
            "type": "cloud_action",
            "request_id": uuid.uuid4().hex,
            "action": plan["cloud"]["action"],
            "params": plan["cloud"]["params"],
        }
    if kind == "actions":
        steps = plan.get("steps") or []
        if not steps:
            # Разрушительный случай: модель вернула actions без единого шага.
            # Отвечаем явной ошибкой, а не уходим как «выполнено».
            return "chat_result", prepare_chat_payload(
                message_id, {"kind": "error", "text": "Мозг вернул пустой план действий — попробуй ещё раз."}
            )
        if classify_steps(steps) == "confirm":
            local_steps = [
                {
                    "action": s.get("action"),
                    "params": s.get("params") or {},
                    "description": s.get("description") or f"Действие «{s.get('action')}»",
                }
                for s in steps
            ]
            payload = {"type": "confirm_request", "message_id": message_id, "local_steps": local_steps}
            if origin == "voice":
                payload["voice"] = True
            return "confirm", payload
        # безопасные локальные — агент исполняет сам и сразу отвечает
        return "execute_local", {"type": "chat_result", "message_id": message_id, "kind": "actions", **{"steps": steps}}
    return "chat_result", prepare_chat_payload(message_id, {"kind": "error", "text": plan.get("text", "Не понял.")})


def _speak_if_possible(text: str) -> None:
    """Озвучивает текст через TTS, если голосовой модуль настроен и
    установлен. Если нет — просто тихо пропускаем, остальной агент
    продолжает работать как обычно (голос — не обязательная часть)."""
    try:
        import voice
        voice.speak(text)
    except Exception as e:
        _log(f"(озвучка недоступна: {e})")


async def _handle_chat(ws, data: dict) -> None:
    message_id = data.get("message_id")
    text = data.get("text", "")
    origin = data.get("origin", "telegram")
    memory_context = "\n".join(f"- {m}" for m in (data.get("memory") or []))

    # Контекст для команд: активное окно из яруса 1 — без скриншота и VLM.
    try:
        _st = get_screen_state()
        _win = (_st.get("active_window") or {}).get("title")
        line = f"\n(на экране сейчас активно: {_win or 'ничего не видно'}; режим: {_st.get('mode', 'normal')})"
    except Exception:
        line = ""

    # Двухходовка «куда написать?»: если осталось незакрытое намерение — это
    # может быть ответ-адресат, новая команда или отмена. Занимаемся этим ДО
    # обращения к мозгу, чтобы отмена не тянула модель. Склейку в полную цель
    # («напиши в <адресат>: <текст>») уводим дальше как eff_text.
    pending = pending_intent.get()
    eff_text = text
    if pending is not None:
        resp = pending_intent.interpret_response(text)
        if resp == "cancel":
            pending_intent.clear()
            cancel_payload = {"type": "chat_result", "message_id": message_id,
                              "kind": "answer", "text": "Отменила."}
            await ws.send(json.dumps(cancel_payload))
            if origin == "voice":
                _speak_if_possible("Отменила.")
            return
        if resp == "replace":
            pending_intent.clear()
        else:
            eff_text = pending_intent.merge(pending, text)
            pending_intent.clear()
            _log(f"✍️ Продолжаю по заготовке: {eff_text}")

    _log(f"💬 Чат от {origin}: {text}")
    ok, brain_msg = await asyncio.to_thread(ensure_ollama_ready)
    if not ok:
        # Локальный мозг недоступен (запускается/качает модель) — отвечаем
        # понятной ошибкой, не трогая модель.
        _log(f"🧠 {brain_msg}", "error")
        err_payload = {
            "type": "chat_result",
            "message_id": message_id,
            "kind": "error",
            "text": brain_msg,
        }
        await ws.send(json.dumps(err_payload))
        if origin == "voice":
            _speak_if_possible(brain_msg)
        return
    plan = await asyncio.to_thread(build_plan, eff_text + line, memory_context)
    plan = await _resolve_local_plan(plan, eff_text, memory_context)
    # Мозг попросил уточнить адресата — сохраняем заготовку «напиши …», чтобы
    # следующее сообщение склеилось в полную цель. Сохраняем ИТОГОВЫЙ план
    # (после возможных look-перепланов).
    if plan.get("kind") == "need_destination":
        _p = plan.get("pending") or {}
        pending_intent.store(prefix=_p.get("prefix", "напиши"),
                             text=_p.get("text", ""))
    msg_type, payload = resolve_plan(plan, message_id, origin)

    if msg_type == "execute_local":
        _log("Выполняю локальные действия...")
        results, status, image_bytes = await _execute_local_steps(plan.get("steps") or [])
        payload = {
            "type": "chat_result",
            "message_id": message_id,
            "kind": "error" if status == "error" else "actions",
            "text": "\n".join(results),
        }
        if image_bytes is not None:
            payload["image_base64"] = base64.b64encode(image_bytes).decode("ascii")
    elif msg_type == "confirm":
        _log("⚠️ Нужно подтверждение разрушительного действия в Telegram.")
        push_ui_message("system", "Нужно подтверждение в Telegram, прежде чем я это сделаю.")

    await ws.send(json.dumps(payload))
    if origin == "voice" and msg_type == "chat_result":
        _speak_if_possible(payload.get("text", ""))


async def _resolve_local_plan(plan: dict, text: str, memory_context: str) -> dict:
    """Цикл взгляда: если мозг попросил look_at_screen — агент смотрит на
    экран и перепланирует с тем, что увидел. Взгляд идёт через кэш
    screen_watcher.describe_screen (VLM + TTL), а не через повторный
    скриншот. Не более 3 перепланирований, чтобы «упрямая» модель не
    зациклила чат."""
    seen_context: list[str] = []
    for _ in range(3):
        steps = plan.get("steps") or []
        looks = [s for s in steps if s.get("action") == "look_at_screen"]
        if not looks:
            return plan
        for s in looks:
            question = (s.get("params") or {}).get("question", "Что открыто на экране?")
            seen = await asyncio.to_thread(describe_screen, question)
            _log(f"👀 Смотрю на экран: {seen[:120]}")
            seen_context.append(seen)
        context = "; ".join(_compact_context(s) for s in seen_context)
        # Реальные заголовки окон — чтобы переплан кликал по настоящему имени,
        # а не по названию, которое модель выдумала по скриншоту.
        window_titles = ", ".join(await asyncio.to_thread(list_window_titles))
        plan = await asyncio.to_thread(
            build_plan,
            f"{text}\n(я посмотрела на экран: {context}; открыты окна: {window_titles})",
            memory_context,
        )
    return plan


def _compact_context(seen: str, limit: int = 400) -> str:
    """Страховка для переплана: qwen2.5vl сбивается с валидного JSON на длинных
    текстах с двойными кавычками. Ограничиваем длину и заменяем кавычки."""
    clean = (seen or "").replace('"', "‹").replace("\\", "")
    if len(clean) > limit:
        return clean[:limit] + "…"
    return clean


async def _execute_local_steps(steps: list[dict]):
    results, status, last_image = [], "done", None
    for i, step in enumerate(steps, start=1):
        action = step.get("action")
        params = step.get("params", {})
        st, result, img = await asyncio.to_thread(execute_action, action, params)
        results.append(f"{i}. {result}")
        if img is not None:
            last_image = img
        if st == "error":
            status = "error"
            break
        if i < len(steps):
            await asyncio.sleep(1.0)
    return results, status, last_image


def _handle_cloud_result(data: dict) -> None:
    status = data.get("status")
    text = data.get("result", "")
    _log(f"☁️ Облачное действие: {status} — {text}")
    push_ui_message("eva", f"☁️ {text}")


def _handle_reminder(data: dict) -> None:
    text = data.get("text", "")
    _log(f"⏰ Напоминание: {text}")
    # Системное уведомление Windows (всплывающий тост) + короткий бип.
    notifications.show_toast("⏰ Напоминание", text)
    notifications.play_notification_sound()
    push_ui_message("system", f"⏰ Напоминание: {text}")
    _speak_if_possible(f"Напоминаю: {text}")


def _handle_chat_echo(data: dict) -> None:
    role = data.get("role", "eva")
    text = data.get("text", "")
    push_ui_message(role, text)


async def _run_single_step(action: str, params: dict) -> tuple[str, str, Optional[bytes]]:
    """execute_action синхронная — уводим в отдельный поток, чтобы не
    подвешивать сетевой цикл на время выполнения."""
    return await asyncio.to_thread(execute_action, action, params)


async def _handle_message(ws, raw: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _log(f"Сервер прислал не-JSON: {raw[:200]}", "error")
        return

    msg_type = data.get("type")

    if msg_type == "speak":
        _speak_if_possible(data.get("text", ""))
        return

    if msg_type == "chat":
        await _handle_chat(ws, data)
        return
    if msg_type == "cloud_result":
        _handle_cloud_result(data)
        return
    if msg_type == "reminder":
        _handle_reminder(data)
        return
    if msg_type == "chat_echo":
        _handle_chat_echo(data)
        return

    if msg_type != "command":
        return

    command_id = data.get("id")
    if data.get("voice"):
        _voice_command_ids.add(command_id)

    steps = data.get("steps")

    if steps:
        # Составная команда из нескольких шагов — выполняем по порядку,
        # с небольшой паузой между ними (дать окну открыться/переключиться,
        # прежде чем следующий шаг, например type_text, начнёт печатать).
        _log(f"Команда №{command_id}: последовательность из {len(steps)} шагов")
        results = []
        overall_status = "done"
        last_image: Optional[bytes] = None

        for i, step in enumerate(steps, start=1):
            action = step.get("action")
            params = step.get("params", {})
            status, result, image_bytes = await _run_single_step(action, params)
            results.append(f"{i}. {result}")
            if image_bytes is not None:
                last_image = image_bytes
            if status == "error":
                overall_status = "error"
                _log(f"Шаг {i} ({action}) — ошибка: {result}", "error")
                break  # прерываем последовательность на первой ошибке
            else:
                _log(f"Шаг {i} ({action}) выполнен: {result}", "success")
            if i < len(steps):
                await asyncio.sleep(1.0)  # дать предыдущему действию "устояться"

        combined_result = "\n".join(results)
        await _finish_command(ws, command_id, overall_status, combined_result, last_image)
        return

    # Одиночное действие (обратная совместимость)
    action = data.get("action")
    params = data.get("params", {})
    _log(f"Команда №{command_id}: {action} {params}")
    status, result, image_bytes = await _run_single_step(action, params)
    _log(f"Результат №{command_id}: {status} — {result}", "success" if status == "done" else "error")
    await _finish_command(ws, command_id, status, result, image_bytes)


async def _finish_command(ws, command_id, status: str, result: str, image_bytes: Optional[bytes]) -> None:
    await _send_result(ws, command_id, status, result, image_bytes)
    if command_id in _voice_command_ids:
        _voice_command_ids.discard(command_id)
        _speak_if_possible(result)


async def _send_result(ws, command_id, status: str, result: str, image_bytes: Optional[bytes]) -> None:
    payload = {
        "type": "result",
        "command_id": command_id,
        "status": status,
        "result": result,
    }
    if image_bytes is not None:
        # Скриншот — кодируем в base64, чтобы передать как обычную JSON-строку.
        payload["image_base64"] = base64.b64encode(image_bytes).decode("ascii")

    await ws.send(json.dumps(payload))


async def send_voice_command(text: str) -> None:
    """Отправляет распознанную голосовую команду на сервер — тот же
    конвейер обработки, что и текстовые /eva команды в Telegram (см.
    eva_engine.py на сервере), включая обязательное подтверждение
    разрушительных действий."""
    global _current_ws
    if _current_ws is None:
        _log("⚠️ Нет соединения с сервером — не могу отправить голосовую команду.", "error")
        return
    try:
        await _current_ws.send(json.dumps({"type": "voice_command", "text": text}))
        _log(f"🎙 Голосовая команда отправлена: {text}")
    except Exception as e:
        _log(f"⚠️ Не удалось отправить голосовую команду: {e}", "error")


def submit_chat(text: str) -> None:
    """Вызывается из UI (поток pywebview) — переправляет текст в сетевой цикл."""
    if _loop is None:
        push_ui_message("system", "Сетевой цикл ещё не запущен.")
        return
    asyncio.run_coroutine_threadsafe(_send_chat_async(text), _loop)


async def _send_chat_async(text: str) -> None:
    global _current_ws
    if _current_ws is None:
        push_ui_message("system", "Нет соединения с сервером.")
        return
    push_ui_message("user", text)
    await _current_ws.send(json.dumps({"type": "chat", "text": text, "origin": "app"}))


def notify_owner(text: str) -> None:
    """Уведомление владельцу: в UI приложения + в Telegram через сервер
    (тип agent_notify, обрабатывает eva_ws на сервере)."""
    notifications.show_toast("Ева", text)
    notifications.play_notification_sound()
    push_ui_message("eva", f"🗔 {text}")
    if _loop is None or _current_ws is None:
        return

    async def _send():
        try:
            await _current_ws.send(json.dumps({"type": "agent_notify", "text": text}))
        except Exception as e:
            _log(f"⚠️ Не удалось отправить уведомление: {e}", "error")

    asyncio.run_coroutine_threadsafe(_send(), _loop)


def submit_voice_command(text: str) -> None:
    """Вызывается из ДРУГОГО потока (voice.py — поток прослушивания wake
    word) — потокобезопасно передаёт распознанный текст в asyncio-цикл
    этого (сетевого) потока."""
    if _loop is None:
        _log("⚠️ Сетевой цикл ещё не запущен.", "error")
        return
    asyncio.run_coroutine_threadsafe(send_voice_command(text), _loop)


async def _connect_once() -> None:
    global _connected, _current_ws
    headers = {
        "X-Eva-Agent-Secret": config.agent_secret,
        "X-Eva-Agent-Id": config.agent_id,
        "X-Eva-Owner-Id": config.owner_telegram_id,
    }
    async with websockets.connect(
        config.server_ws_url,
        additional_headers=headers,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        _connected = True
        _current_ws = ws
        _log("🔌 Подключён к серверу Евы.", "success")
        try:
            async for raw in ws:
                await _handle_message(ws, raw)
        finally:
            _connected = False
            _current_ws = None
            _log("🔌 Отключён от сервера.", "error")


async def run_forever() -> None:
    """Основной цикл: подключается, при обрыве ждёт 5 секунд и пробует снова.
    Работает всё время жизни приложения — фоновый поток, в котором он
    крутится, завершается сам вместе с процессом при закрытии приложения."""
    backoff = 5
    warned_not_configured = False
    while True:
        if not config.is_configured():
            if not warned_not_configured:
                _log("⚠️ Сервер/секрет/владелец не настроены — открой вкладку «Настройки».", "error")
                warned_not_configured = True
            await asyncio.sleep(backoff)
            continue
        warned_not_configured = False  # настройки появились — сбрасываем, чтобы предупредить снова, если пропадут
        try:
            await _connect_once()
        except InvalidStatusCode as e:
            _log(f"⚠️ Сервер отклонил подключение (проверь секрет и Telegram ID владельца): {e}", "error")
        except ConnectionClosed:
            pass  # уже залогировано в _connect_once
        except (OSError, Exception) as e:
            _log(f"⚠️ Не удалось подключиться: {e}", "error")
        await asyncio.sleep(backoff)


def start_brain_background() -> None:
    """Фоновый поток при старте приложения: запускает/проверяет Ollama.
    Не блокирует UI; результат пишется в лог (вкладка «Лог»)."""
    from local_brain import ensure_ollama_ready
    ok, msg = ensure_ollama_ready()
    _log(f"🧠 {msg}", "success" if ok else "error")


def start_background_loop() -> None:
    """Точка входа для фонового потока — создаёт свой event loop и
    запускает run_forever(). Цикл сохраняется в модульной переменной,
    чтобы submit_voice_command() из ДРУГОГО потока (voice.py) могло
    потокобезопасно отправлять в него корутины."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(run_forever())
