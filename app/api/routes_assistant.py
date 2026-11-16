"""
ИИ-помощник владельца — этап A1 «только смотреть» (документ 16): текстовый чат по данным каталога, ничего не меняет.

  GET  /api/v1/assistant/status   работает ли помощник на этом компьютере          — assistant:use (пока только владелец)
  POST /api/v1/assistant/chat     {messages: [{role, content}], lang} -> {reply, tools, seconds}

Переписку хранит страница; сервер её не сохраняет (app/services/assistant.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_permission
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.security import CurrentUser, Permission
from app.db.session import get_db
from app.services import assistant

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

_UNAVAILABLE = {
    "disabled": "Помощник выключен на этом компьютере: здесь нет модели ИИ (нужна видеокарта NVIDIA от 6 ГБ).",
    "model_unreachable": "Модель ИИ не отвечает. Возможно, она ещё загружается — попробуйте через минуту.",
    "bad_answer": "Помощник не смог ответить на этот вопрос. Попробуйте спросить по-другому.",
}


class ChatMessage(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(max_length=40)
    lang: str = Field(default="ru", max_length=16)


@router.get("/status")
def assistant_status(_user: CurrentUser = Depends(require_permission(Permission.ASSISTANT_USE))) -> dict:
    available = bool(assistant.model_address(get_settings()))
    return {"available": available, "mode": "read", "reason": None if available else "disabled"}


@router.post("/chat")
async def assistant_chat(
    body: ChatIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.ASSISTANT_USE)),
) -> dict:
    messages = [m.model_dump() for m in body.messages]
    try:
        reply = await run_in_threadpool(assistant.chat, db, user, messages, body.lang, get_settings())
    except ValueError as exc:
        raise ApiError(422, "assistant_empty", str(exc)) from exc
    except assistant.AssistantBusy as exc:
        raise ApiError(503, "assistant_busy", "Помощник занят: сейчас модель узнаёт товары для покупателей. Основные функции "
                                              "работают — спросите ещё раз через несколько секунд.") from exc
    except assistant.AssistantUnavailable as exc:
        raise ApiError(503, f"assistant_{exc.reason}", _UNAVAILABLE.get(exc.reason, _UNAVAILABLE["bad_answer"])) from exc
    return {"reply": reply.text, "tools": reply.tools, "seconds": reply.seconds}
