"""Память намерения: одно «незакрытое» намерение «напиши …».

Когда пользователь просит написать сообщение, не сказав куда, агент
сохраняет заготовку (prefix + текст) и спрашивает «куда написать?».
Следующее сообщение трактуем как ОТВЕТ-адресат (merge в полную цель)
«напиши в <адресат>: <текст>»; новую команду с глаголом — как перезапись;
«отмена» — как отмену. Одна заготовка на процесс: перезапуск агента её
теряет — это ожидаемо (пользователь просто повторит).
"""
import re
from dataclasses import dataclass
from typing import Optional

# Новая команда (перезапись заготовки), если сообщение начинается с одного из этих.
_VERB_RE = re.compile(r"^(напиш|отправ|сообщи|введи|открой|запусти|найди|сделай)", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^(отмен|не надо|cancel)", re.IGNORECASE)


@dataclass
class PendingIntent:
    prefix: str
    text: str


_pending: Optional[PendingIntent] = None


def store(prefix: str, text: str) -> None:
    global _pending
    _pending = PendingIntent(prefix=prefix, text=text)


def get() -> Optional[PendingIntent]:
    return _pending


def clear() -> None:
    global _pending
    _pending = None


def interpret_response(msg: str) -> str:
    """Ответ на вопрос «куда написать?»: 'cancel' | 'replace' | 'answer'.

    - отмена («отмена»/«не надо»/«cancel»)   -> 'cancel'
    - новая команда с глаголом-действием      -> 'replace' (заготовку выкидываем)
    - иначе — это адресат                      -> 'answer'
    """
    m = (msg or "").strip()
    if _CANCEL_RE.search(m):
        return "cancel"
    if _VERB_RE.match(m):
        return "replace"
    return "answer"


def merge(pending: Optional[PendingIntent], answer: str) -> str:
    """Склейка «напиши <адресат>: <текст>» из заготовки и ответа."""
    if pending is None:
        return answer
    return f"{pending.prefix} {answer.strip()}: {pending.text}"