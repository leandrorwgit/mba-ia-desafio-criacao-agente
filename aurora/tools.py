from __future__ import annotations

import re
from datetime import date as date_type
from typing import Any

from google.adk.tools import ToolContext

from . import storage
from .config import DATA_DIR


def _apartment(tool_context: ToolContext) -> str:
    """Apartment identity is injected by the API and never comes from the model."""
    apartment = tool_context.state.get("apartment")
    if not isinstance(apartment, str) or not apartment:
        raise ValueError("A sessão não possui um apartamento associado.")
    return apartment


def _action_key(tool_context: ToolContext) -> str:
    """Scope ADK's call ID to its persistent session before using it as a key."""
    call_id = tool_context.function_call_id
    if not call_id:
        raise ValueError("A tool precisa de um identificador de chamada do ADK.")
    return f"{tool_context.session.id}:{call_id}"


def _valid_date(value: str) -> bool:
    try:
        date_type.fromisoformat(value)
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    except (TypeError, ValueError):
        return False


def list_my_reservations(tool_context: ToolContext) -> dict[str, Any]:
    """Lista as reservas ativas do apartamento autenticado nesta sessão."""
    apartment = _apartment(tool_context)
    return {"reservas": storage.list_reservations(apartment)}


def list_my_visitors(tool_context: ToolContext) -> dict[str, Any]:
    """Lista os visitantes autorizados pelo apartamento desta sessão."""
    apartment = _apartment(tool_context)
    return {"visitantes": storage.list_visitors(apartment)}


def reserve_common_area(
    area: str,
    data: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Reserva uma área livre; cobráveis ficam aguardando aprovação do sistema."""
    apartment = _apartment(tool_context)
    resolved_area = storage.resolve_area(area)
    if not resolved_area:
        return {"status": "invalid_area", "message": "Não reconheci essa área comum."}
    if not _valid_date(data):
        return {"status": "invalid_date", "message": "Informe a data no formato AAAA-MM-DD."}

    area_id = resolved_area["id"]
    if not storage.is_area_available(area_id, data):
        # Deliberately reveal only occupancy; no booking code or apartment is returned.
        return {"status": "occupied", "message": "Essa área já está reservada nessa data."}

    fee = float(resolved_area["fee"])
    confirmation = tool_context.tool_confirmation
    if fee > 0 and confirmation is None:
        tool_context.request_confirmation(
            hint=f"Confirme a reserva de {resolved_area['name']} em {data}; a taxa é R$ {fee:.2f}.",
            payload={
                "area_id": area_id,
                "data": data,
                "apartment": apartment,
                "idempotency_key": _action_key(tool_context),
            },
        )
        return {"status": "waiting_for_confirmation"}
    if fee > 0 and confirmation is not None and not confirmation.confirmed:
        return {"status": "denied", "message": "A reserva não foi criada."}

    result = storage.reserve_atomically(
        apartment=apartment,
        area_id=area_id,
        date=data,
        idempotency_key=_action_key(tool_context),
    )
    if result["status"] == "reserved":
        return {"status": "reserved", "codigo": result["codigo"], "area": area_id, "data": data}
    if result["status"] == "occupied":
        return {"status": "occupied", "message": "Essa área acabou de ser reservada por outro morador."}
    return {"status": result["status"]}


def cancel_my_reservation(
    area: str,
    data: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Cancela uma reserva apenas no apartamento associado à sessão."""
    apartment = _apartment(tool_context)
    resolved_area = storage.resolve_area(area)
    if not resolved_area:
        return {"status": "invalid_area", "message": "Não reconheci essa área comum."}
    if not _valid_date(data):
        return {"status": "invalid_date", "message": "Informe a data no formato AAAA-MM-DD."}
    canceled = storage.cancel_own_reservation(apartment, resolved_area["id"], data)
    if canceled:
        return {"status": "cancelled", "area": resolved_area["id"], "data": data}
    return {"status": "not_found", "message": "Não encontrei reserva sua para essa área e data."}


def authorize_visitor(
    nome: str,
    data: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Autoriza visitante somente após confirmação explícita pela rota da API."""
    apartment = _apartment(tool_context)
    name = nome.strip()
    if not name:
        return {"status": "invalid_name", "message": "Informe o nome do visitante."}
    if not _valid_date(data):
        return {"status": "invalid_date", "message": "Informe a data no formato AAAA-MM-DD."}

    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        tool_context.request_confirmation(
            hint=f"Confirme a autorização de entrada de {name} em {data}.",
            payload={
                "nome": name,
                "data": data,
                "apartment": apartment,
                "idempotency_key": _action_key(tool_context),
            },
        )
        return {"status": "waiting_for_confirmation"}
    if not confirmation.confirmed:
        return {"status": "denied", "message": "O visitante não foi autorizado."}

    return storage.authorize_visitor_atomically(
        apartment=apartment,
        name=name,
        date=data,
        idempotency_key=_action_key(tool_context),
    )


def search_regulation(question: str) -> dict[str, Any]:
    """Find only the most relevant short paragraphs in the regulation file."""
    path = DATA_DIR / "regulamento.md"
    text = path.read_text(encoding="utf-8")
    # Split tables and long paragraphs into focused lines/sentences so a hit does
    # not pull an entire chapter (or an adjacent topic) into the model history.
    candidates: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or re.fullmatch(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?", line):
            continue
        candidates.extend(
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", line)
            if sentence.strip()
        )
    words = {
        word
        for word in re.sub(r"[^a-z0-9 ]", " ", _fold_for_search(question)).split()
        if len(word) > 3 and word not in {"para", "pela", "pelo", "como", "qual", "quais", "horas", "funciona"}
    }
    ranked: list[tuple[int, int, str]] = []
    for index, candidate in enumerate(candidates):
        folded = _fold_for_search(candidate)
        overlap = sum(1 for word in words if word in folded)
        if overlap:
            ranked.append((overlap, -index, candidate[:800]))
    ranked.sort(reverse=True)
    excerpts: list[str] = []
    for _, _, candidate in ranked:
        if candidate not in excerpts:
            excerpts.append(candidate)
        if len(excerpts) == 2:
            break
    return {"trechos_relevantes": excerpts}


def _fold_for_search(value: str) -> str:
    import unicodedata

    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()
