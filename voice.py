"""
Голосовой модуль desktop-агента: "Ева" всегда слушает, реагирует на своё
имя и озвучивает ответы (TTS через edge-tts).

Как это работает по шагам:
1. Vosk (офлайн распознавание речи, без ключей/регистраций) постоянно
   слушает микрофон через sounddevice в отдельном потоке (блокирующее
   чтение аудио — поэтому именно отдельный OS-поток, не корутина) и
   проверяет, не прозвучало ли слово "ева"/"эва" в распознанном тексте.
2. Как только услышала своё имя — на несколько секунд переключается на
   запись через отдельный поток (Groq даёт заметно более точное
   распознавание самой команды, чем маленькая offline-модель Vosk).
3. Записанный фрагмент уходит в Groq (Whisper) на распознавание.
4. Распознанный текст передаётся в ws_client.submit_voice_command() —
   оттуда он уходит на сервер и проходит ТОТ ЖЕ конвейер безопасности,
   что и текстовые команды в Telegram (см. eva_engine.py на сервере).
5. Ответы (через ws_client._speak_if_possible) озвучиваются функцией
   speak() ниже — синтез через edge-tts, воспроизведение через playsound.

Голос — необязательная часть агента: если VOSK_MODEL_PATH / GROQ_API_KEY
не заданы, start_listening() просто не запускает поток и агент
продолжает работать как обычно (только текстовые команды через
Telegram, без голоса).
"""
import asyncio
import json
import tempfile
import threading
import wave
from pathlib import Path

from config import config

_listening = False
_stop_flag = False

WAKE_WORDS = ("ева", "эва", "eva")


def is_listening() -> bool:
    return _listening


def _log(message: str, level: str = "info") -> None:
    # Переиспользуем тот же лог-буфер, что и у сетевого модуля, чтобы в UI
    # была единая лента событий (подключение к серверу + голосовые события).
    import ws_client
    ws_client._log(f"🎙 {message}", level)


# ----- Запись короткого фрагмента речи после срабатывания слова "Ева" -----
def _record_utterance(seconds: float = 5.0, sample_rate: int = 16000) -> Path:
    """Записывает `seconds` секунд с микрофона в временный .wav файл."""
    import sounddevice as sd

    recording = sd.rec(int(seconds * sample_rate), samplerate=sample_rate, channels=1, dtype="int16")
    sd.wait()  # блокируемся, пока запись не закончится

    tmp_path = Path(tempfile.mktemp(suffix=".wav"))
    with wave.open(str(tmp_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16 -> 2 байта
        wf.setframerate(sample_rate)
        wf.writeframes(recording.tobytes())
    return tmp_path


# ----- Распознавание записанного фрагмента через Groq Whisper -----
def _transcribe(wav_path: Path) -> str:
    from groq import Groq

    client = Groq(api_key=config.groq_api_key)
    with open(wav_path, "rb") as f:
        result = client.audio.transcriptions.create(
            file=f, model="whisper-large-v3-turbo", language="ru",
        )
    return (result.text or "").strip()


def _open_stream(sample_rate: int):
    import sounddevice as sd
    # RawInputStream отдаёт сырые байты через .read(), как и нужно Vosk —
    # такой же интерфейс, что был у pyaudio, но без проблем со сборкой на
    # Windows (у sounddevice есть готовые wheels под все версии Python).
    stream = sd.RawInputStream(samplerate=sample_rate, blocksize=4000, dtype="int16", channels=1)
    stream.start()
    return stream


# ----- Основной цикл прослушивания (работает в отдельном потоке) -----
def _listen_loop() -> None:
    global _listening
    try:
        from vosk import Model, KaldiRecognizer
    except Exception as e:
        _log(f"Библиотека vosk не установлена: {e}", "error")
        return

    try:
        model = Model(config.vosk_model_path)
    except Exception as e:
        _log(f"Не удалось загрузить модель Vosk (проверь путь в настройках): {e}", "error")
        return

    sample_rate = 16000
    recognizer = KaldiRecognizer(model, sample_rate)
    stream = _open_stream(sample_rate)
    _listening = True
    _log("Слушаю — скажи «Ева», чтобы отдать команду.", "success")

    try:
        while not _stop_flag:
            data, _overflowed = stream.read(4000)
            data = bytes(data)

            if recognizer.AcceptWaveform(data):
                heard = json.loads(recognizer.Result()).get("text", "").lower()
            else:
                heard = json.loads(recognizer.PartialResult()).get("partial", "").lower()

            if any(w in heard for w in WAKE_WORDS):
                _log("Услышала своё имя — говори команду...")
                recognizer.Reset()

                # Освобождаем устройство записи для отдельной записи
                # через sd.rec() (проще, чем делить один и тот же поток).
                stream.stop()
                stream.close()

                try:
                    wav_path = _record_utterance()
                    command_text = _transcribe(wav_path)
                    wav_path.unlink(missing_ok=True)
                except Exception as e:
                    _log(f"Ошибка распознавания команды: {e}", "error")
                    command_text = ""

                stream = _open_stream(sample_rate)

                if command_text:
                    _log(f"Распознала: «{command_text}»")
                    import ws_client
                    ws_client.submit_voice_command(command_text)
                else:
                    _log("Не расслышала команду.", "error")
    finally:
        stream.stop()
        stream.close()
        _listening = False
        _log("Остановила прослушивание.")


def start_listening() -> bool:
    """Запускает поток прослушивания, если голос настроен. Возвращает
    True, если реально запустился (или уже был запущен)."""
    global _stop_flag
    if _listening:
        return True
    if not config.is_voice_configured():
        _log("Голос не настроен (нужны путь к модели Vosk и ключ Groq).", "error")
        return False
    _stop_flag = False
    threading.Thread(target=_listen_loop, daemon=True).start()
    return True


def stop_listening() -> None:
    global _stop_flag
    _stop_flag = True


# ----- Синтез и воспроизведение речи (TTS) -----
def speak(text: str) -> None:
    """Синтезирует и проигрывает text вслух. Синхронная функция (вызывается
    из ws_client в отдельном потоке через asyncio.to_thread или напрямую
    из потока прослушивания) — сама внутри поднимает свой короткий
    asyncio-цикл для edge-tts, не мешая основным циклам агента."""
    if not text.strip():
        return
    try:
        import edge_tts
        from playsound import playsound

        tmp_path = Path(tempfile.mktemp(suffix=".mp3"))

        async def _synthesize() -> None:
            communicate = edge_tts.Communicate(text, config.tts_voice)
            await communicate.save(str(tmp_path))

        asyncio.run(_synthesize())
        playsound(str(tmp_path))
        tmp_path.unlink(missing_ok=True)
    except Exception as e:
        _log(f"Не удалось озвучить ответ: {e}", "error")
