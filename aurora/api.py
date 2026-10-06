from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from google.adk.apps.app import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel, ConfigDict

from . import storage
from .agents import root_agent
from .config import (
    APP_NAME,
    CONFIRMATION_FUNCTION_NAME,
    DATABASE_PATH,
    USER_ID,
)


adk_app = App(
    name=APP_NAME,
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)


class CreateSessionBody(BaseModel):
    apartamento: str


class MessageBody(BaseModel):
    texto: str


class ConfirmationBody(BaseModel):
    id: str
    confirmado: bool
    model_config = ConfigDict(extra="forbid")


class _SessionLocks:
    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    async def for_session(self, session_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(session_id, asyncio.Lock())


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.initialize()
    session_service = SqliteSessionService(db_path=str(DATABASE_PATH))
    app.state.session_service = session_service
    app.state.runner = Runner(app=adk_app, session_service=session_service)
    app.state.session_locks = _SessionLocks()
    try:
        yield
    finally:
        close = getattr(session_service, "close", None)
        if close:
            result = close()
            if asyncio.iscoroutine(result):
                await result


app = FastAPI(title="Residencial Aurora", lifespan=lifespan)


def _event_as_json(event: Any) -> dict[str, Any]:
    return event.model_dump(mode="json", by_alias=True, exclude_none=True)


def _event_text(events: list[Any]) -> str:
    for event in reversed(events):
        content = getattr(event, "content", None)
        if not content:
            continue
        text_parts = [part.text for part in (content.parts or []) if getattr(part, "text", None)]
        if text_parts:
            return "\n".join(text_parts)
    return ""


def _safe_confirmation(
    event: Any,
    confirmation_call: Any,
    session_id: str,
) -> None:
    args = confirmation_call.args or {}
    original = args.get("originalFunctionCall") or {}
    confirmation = args.get("toolConfirmation") or {}
    function_name = original.get("name", "")
    function_args = original.get("args") or {}
    call_id = original.get("id", "")
    payload = confirmation.get("payload") or {}

    if function_name == "reserve_common_area":
        area = storage.resolve_area(str(function_args.get("area", "")))
        data = function_args.get("data")
        if not area or not isinstance(data, str):
            return
        action = "reservar_area_com_taxa"
        details = {"area": area["id"], "data": data}
    elif function_name == "authorize_visitor":
        name = function_args.get("nome")
        data = function_args.get("data")
        if not isinstance(name, str) or not isinstance(data, str):
            return
        action = "autorizar_visitante"
        details = {"nome": name, "data": data}
    else:
        # Only the two domain tools above are allowed to pause for confirmation.
        return

    storage.save_pending_confirmation(
        confirmation_id=str(confirmation_call.id),
        session_id=session_id,
        invocation_id=str(event.invocation_id),
        function_name=function_name,
        function_call_id=str(call_id),
        function_args=function_args,
        confirmation_payload=payload,
        action=action,
        details=details,
    )


def _capture_confirmations(session_id: str, events: list[Any]) -> None:
    for event in events:
        long_running_ids = set(getattr(event, "long_running_tool_ids", None) or [])
        if not long_running_ids:
            continue
        for function_call in event.get_function_calls():
            if (
                function_call.id in long_running_ids
                and function_call.name == CONFIRMATION_FUNCTION_NAME
            ):
                _safe_confirmation(event, function_call, session_id)


def _api_response(events: list[Any], session_id: str) -> dict[str, Any]:
    return {
        "resposta": _event_text(events),
        "confirmacoes_pendentes": storage.pending_confirmations(session_id),
    }


async def _run(
    request: Request,
    session_id: str,
    message: types.Content,
    *,
    invocation_id: str | None = None,
) -> list[Any]:
    session = storage.session_record(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")

    lock = await request.app.state.session_locks.for_session(session_id)
    async with lock:
        events: list[Any] = []
        async for event in request.app.state.runner.run_async(
            user_id=session["user_id"],
            session_id=session_id,
            invocation_id=invocation_id,
            new_message=message,
        ):
            events.append(event)
        _capture_confirmations(session_id, events)
        return events


@app.post("/sessoes", status_code=status.HTTP_201_CREATED)
async def create_session(body: CreateSessionBody, request: Request) -> dict[str, str]:
    apartment = str(body.apartamento).strip()
    if not storage.apartment_exists(apartment):
        raise HTTPException(status_code=404, detail="Apartamento não encontrado.")
    session = await request.app.state.session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        state={"apartment": apartment},
    )
    storage.save_session(session.id, apartment, USER_ID)
    return {"session_id": session.id}


@app.post("/sessoes/{session_id}/mensagens")
async def send_message(
    session_id: str,
    body: MessageBody,
    request: Request,
) -> dict[str, Any]:
    if not body.texto.strip():
        raise HTTPException(status_code=422, detail="A mensagem não pode ficar vazia.")
    events = await _run(
        request,
        session_id,
        types.Content(role="user", parts=[types.Part(text=body.texto)]),
    )
    return _api_response(events, session_id)


@app.post("/sessoes/{session_id}/confirmacoes")
async def answer_confirmation(
    session_id: str,
    body: ConfirmationBody,
    request: Request,
) -> dict[str, Any]:
    if not storage.session_record(session_id):
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    confirmation = storage.claim_confirmation(session_id, body.id)
    if not confirmation:
        raise HTTPException(status_code=409, detail="Confirmação pendente não encontrada.")

    response = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=body.id,
                    name=CONFIRMATION_FUNCTION_NAME,
                    response={
                        "confirmed": body.confirmado,
                        "payload": confirmation["confirmation_payload"],
                    },
                )
            )
        ],
    )
    try:
        events = await _run(
            request,
            session_id,
            response,
            invocation_id=confirmation["invocation_id"],
        )
    except Exception:
        # Keep the ID consumed: replaying a possibly executed action is unsafe.
        storage.finish_confirmation(body.id, body.confirmado)
        raise
    storage.finish_confirmation(body.id, body.confirmado)
    return _api_response(events, session_id)


@app.get("/sessoes/{session_id}/eventos")
async def get_events(session_id: str, request: Request) -> list[dict[str, Any]]:
    record = storage.session_record(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    session = await request.app.state.session_service.get_session(
        app_name=APP_NAME,
        user_id=record["user_id"],
        session_id=session_id,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return [_event_as_json(event) for event in session.events]


@app.get("/apartamentos/{apartamento}/reservas")
async def get_apartment_reservations(apartamento: str) -> list[dict[str, str]]:
    return storage.list_reservations(apartamento)


@app.get("/apartamentos/{apartamento}/visitantes")
async def get_apartment_visitors(apartamento: str) -> list[dict[str, str]]:
    return storage.list_visitors(apartamento)
