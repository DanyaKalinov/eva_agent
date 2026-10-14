"""
Настройки desktop-агента Евы. Хранятся в .env рядом с приложением.

Обязательные для подключения к серверу:
- EVA_SERVER_WS_URL — адрес WebSocket-эндпоинта сервера, например
  wss://mnemonic-yqyf.onrender.com/ws/eva
- EVA_AGENT_SECRET — тот же секрет, что и в переменных окружения сервера
  на Render (EVA_AGENT_SECRET) — иначе сервер отклонит подключение (403).
- EVA_OWNER_TELEGRAM_ID — твой Telegram ID (тот же, что ADMIN_ID на
  сервере, или ID пользователя с granted-доступом к Еве) — сервер
  проверяет его при подключении и использует, чтобы понять, кому
  принадлежат голосовые команды с этого ПК.
- EVA_AGENT_ID — произвольное имя этого агента (на будущее, если
  появится несколько ПК/агентов одновременно).

Для голоса (необязательно, пока не настроено — просто не будет работать
прослушивание/озвучка, всё остальное продолжит работать как раньше):
- VOSK_MODEL_PATH — путь к распакованной папке модели Vosk (например,
  vosk-model-small-ru-0.22 с alphacephei.com/vosk/models) — используется
  для постоянного офлайн-прослушивания слова "Ева", без каких-либо
  ключей и регистраций.
- GROQ_API_KEY — свой ключ Groq (console.groq.com) для точного
  распознавания САМОЙ КОМАНДЫ после срабатывания слова "Ева" (используется
  тот же Whisper, что и в самом Telegram-боте, но здесь агенту нужен
  собственный ключ)
- TTS_VOICE — голос для edge-tts, например ru-RU-SvetlanaNeural
"""
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)


class AgentConfig:
    """Простой изменяемый конфиг в памяти — обновляется из UI (вкладка
    «Настройки») без перезапуска приложения; ws_client переподключается
    с новыми значениями при следующей попытке соединения."""

    @staticmethod
    def _clean(value: str) -> str:
        # Windows "Copy as path" оборачивает путь в кавычки — снимаем их
        # и при загрузке тоже, на случай если в .env уже попало сломанное
        # значение с прошлого раза.
        return (value or "").strip().strip('"').strip("'")

    def __init__(self) -> None:
        self.server_ws_url: str = self._clean(os.getenv("EVA_SERVER_WS_URL", ""))
        self.agent_secret: str = self._clean(os.getenv("EVA_AGENT_SECRET", ""))
        self.agent_id: str = self._clean(os.getenv("EVA_AGENT_ID", "default")) or "default"
        self.owner_telegram_id: str = self._clean(os.getenv("EVA_OWNER_TELEGRAM_ID", ""))

        # Голос — необязательно
        self.vosk_model_path: str = self._clean(os.getenv("VOSK_MODEL_PATH", ""))
        self.groq_api_key: str = self._clean(os.getenv("GROQ_API_KEY", ""))
        self.tts_voice: str = self._clean(os.getenv("TTS_VOICE", "ru-RU-SvetlanaNeural")) or "ru-RU-SvetlanaNeural"

        # Браузер по умолчанию для открытия сайтов/поиска, если команда
        # не указала конкретный. "default" = системный браузер по умолчанию.
        self.default_browser: str = self._clean(os.getenv("EVA_DEFAULT_BROWSER", "default")) or "default"

        # Локальный мозг (Ollama)
        self.ollama_model: str = self._clean(os.getenv("EVA_OLLAMA_MODEL", "qwen2.5vl:7b")) or "qwen2.5vl:7b"
        self.ollama_url: str = self._clean(os.getenv("EVA_OLLAMA_URL", "http://localhost:11434")) or "http://localhost:11434"

        # Непрерывное осознание экрана (двухъярусная картина)
        self.screen_frame_interval: float = float(os.getenv("EVA_SCREEN_FRAME_INTERVAL", "1.0"))
        self.caption_ttl: float = float(os.getenv("EVA_CAPTION_TTL", "12.0"))
        self.caption_refresh_s: float = float(os.getenv("EVA_CAPTION_REFRESH_S", "45.0"))
        self.watch_cooldown_s: float = float(os.getenv("EVA_WATCH_COOLDOWN_S", "300.0"))

    def get_browser(self) -> str:
        """Какой браузер использовать, когда команда не назвала конкретный."""
        return self.default_browser or "default"

    def is_configured(self) -> bool:
        return bool(self.server_ws_url and self.agent_secret and self.owner_telegram_id)

    def is_voice_configured(self) -> bool:
        return bool(self.vosk_model_path and self.groq_api_key)

    def as_dict(self) -> dict:
        return {
            "server_ws_url": self.server_ws_url,
            "agent_secret": self.agent_secret,
            "agent_id": self.agent_id,
            "owner_telegram_id": self.owner_telegram_id,
            "vosk_model_path": self.vosk_model_path,
            "groq_api_key": self.groq_api_key,
            "tts_voice": self.tts_voice,
            "default_browser": self.default_browser,
            "ollama_model": self.ollama_model,
            "ollama_url": self.ollama_url,
            "screen_frame_interval": self.screen_frame_interval,
            "caption_ttl": self.caption_ttl,
            "caption_refresh_s": self.caption_refresh_s,
            "watch_cooldown_s": self.watch_cooldown_s,
        }

    def update(self, **kwargs) -> None:
        """Принимает любые из полей выше (только переданные — обновляются),
        сохраняет всё на диск в .env."""
        mapping = {
            "server_ws_url": "EVA_SERVER_WS_URL",
            "agent_secret": "EVA_AGENT_SECRET",
            "agent_id": "EVA_AGENT_ID",
            "owner_telegram_id": "EVA_OWNER_TELEGRAM_ID",
            "vosk_model_path": "VOSK_MODEL_PATH",
            "groq_api_key": "GROQ_API_KEY",
            "tts_voice": "TTS_VOICE",
            "default_browser": "EVA_DEFAULT_BROWSER",
            "ollama_model": "EVA_OLLAMA_MODEL",
            "ollama_url": "EVA_OLLAMA_URL",
            "screen_frame_interval": "EVA_SCREEN_FRAME_INTERVAL",
            "caption_ttl": "EVA_CAPTION_TTL",
            "caption_refresh_s": "EVA_CAPTION_REFRESH_S",
            "watch_cooldown_s": "EVA_WATCH_COOLDOWN_S",
        }
        ENV_PATH.touch(exist_ok=True)
        for field, env_key in mapping.items():
            if field in kwargs:
                value = self._clean(kwargs[field])
                if field == "agent_id" and not value:
                    value = "default"
                if field == "tts_voice" and not value:
                    value = "ru-RU-SvetlanaNeural"
                setattr(self, field, value)
                set_key(str(ENV_PATH), env_key, value)


config = AgentConfig()
