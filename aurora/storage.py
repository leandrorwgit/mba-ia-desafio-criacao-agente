from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DATA_DIR, DATABASE_PATH, STATE_DIR


def _read_json(filename: str) -> list[dict[str, Any]]:
    path = DATA_DIR / filename
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, list):
        raise ValueError(f"Esperava uma lista em {path}")
    return value


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
    finally:
        connection.close()


def initialize() -> None:
    """Create durable tables and import the immutable seed files once."""
    apartments = _read_json("apartamentos.json")
    areas = _read_json("areas.json")
    reservations = _read_json("reservas.json")
    visitors = _read_json("visitantes.json")

    with _connection() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS apartments (
                number TEXT PRIMARY KEY,
                resident TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS areas (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                fee REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reservation_codes (
                code TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS reservations (
                code TEXT PRIMARY KEY REFERENCES reservation_codes(code),
                apartment TEXT NOT NULL,
                area TEXT NOT NULL,
                date TEXT NOT NULL,
                cancelled INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_reservation_per_area_date
                ON reservations(area, date) WHERE cancelled = 0;
            CREATE TABLE IF NOT EXISTS visitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apartment TEXT NOT NULL,
                name TEXT NOT NULL,
                date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_sessions (
                session_id TEXT PRIMARY KEY,
                apartment TEXT NOT NULL,
                user_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS confirmations (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                invocation_id TEXT NOT NULL,
                function_name TEXT NOT NULL,
                function_call_id TEXT NOT NULL,
                function_args TEXT NOT NULL,
                confirmation_payload TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'approved', 'denied')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS confirmations_by_session
                ON confirmations(session_id, status);
            CREATE TABLE IF NOT EXISTS completed_actions (
                idempotency_key TEXT PRIMARY KEY,
                result TEXT NOT NULL
            );
            """
        )
        db.execute("BEGIN IMMEDIATE")
        seeded = db.execute(
            "SELECT value FROM metadata WHERE key = 'seed_loaded'"
        ).fetchone()
        if not seeded:
            db.executemany(
                "INSERT OR REPLACE INTO apartments(number, resident) VALUES (?, ?)",
                [(str(row["numero"]), str(row["morador"])) for row in apartments],
            )
            db.executemany(
                "INSERT OR REPLACE INTO areas(id, name, fee) VALUES (?, ?, ?)",
                [
                    (str(row["id"]), str(row["nome"]), float(row["taxa"]))
                    for row in areas
                ],
            )
            db.executemany(
                "INSERT OR IGNORE INTO reservation_codes(code) VALUES (?)",
                [(str(row["codigo"]),) for row in reservations],
            )
            db.executemany(
                """INSERT OR IGNORE INTO reservations
                   (code, apartment, area, date, cancelled) VALUES (?, ?, ?, ?, 0)""",
                [
                    (
                        str(row["codigo"]),
                        str(row["apartamento"]),
                        str(row["area"]),
                        str(row["data"]),
                    )
                    for row in reservations
                ],
            )
            db.executemany(
                "INSERT INTO visitors(apartment, name, date) VALUES (?, ?, ?)",
                [
                    (
                        str(row["apartamento"]),
                        str(row["nome"]),
                        str(row["data"]),
                    )
                    for row in visitors
                ],
            )
            db.execute(
                "INSERT INTO metadata(key, value) VALUES ('seed_loaded', '1')"
            )
        db.commit()


def restore_initial_data() -> None:
    """Restore mutable condo data while retaining session history and used codes."""
    reservations = _read_json("reservas.json")
    visitors = _read_json("visitantes.json")
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM reservations")
        db.execute("DELETE FROM visitors")
        db.execute("DELETE FROM confirmations")
        db.execute("DELETE FROM completed_actions")
        for row in reservations:
            code = str(row["codigo"])
            db.execute("INSERT OR IGNORE INTO reservation_codes(code) VALUES (?)", (code,))
            db.execute(
                """INSERT INTO reservations(code, apartment, area, date, cancelled)
                   VALUES (?, ?, ?, ?, 0)""",
                (code, str(row["apartamento"]), str(row["area"]), str(row["data"])),
            )
        db.executemany(
            "INSERT INTO visitors(apartment, name, date) VALUES (?, ?, ?)",
            [
                (str(row["apartamento"]), str(row["nome"]), str(row["data"]))
                for row in visitors
            ],
        )
        db.commit()


def apartment_exists(apartment: str) -> bool:
    with _connection() as db:
        return (
            db.execute(
                "SELECT 1 FROM apartments WHERE number = ?", (str(apartment),)
            ).fetchone()
            is not None
        )


def area_by_id(area_id: str) -> dict[str, Any] | None:
    with _connection() as db:
        row = db.execute(
            "SELECT id, name, fee FROM areas WHERE id = ?", (area_id,)
        ).fetchone()
    return dict(row) if row else None


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def resolve_area(value: str) -> dict[str, Any] | None:
    query = _fold(value)
    with _connection() as db:
        rows = db.execute("SELECT id, name, fee FROM areas ORDER BY id").fetchall()
    for row in rows:
        area = dict(row)
        candidates = {_fold(area["id"]), _fold(area["name"])}
        if query in candidates:
            return area
        if query and any(query in candidate or candidate in query for candidate in candidates):
            return area
    return None


def list_reservations(apartment: str) -> list[dict[str, str]]:
    with _connection() as db:
        rows = db.execute(
            """SELECT code, area, date FROM reservations
               WHERE apartment = ? AND cancelled = 0 ORDER BY date, code""",
            (str(apartment),),
        ).fetchall()
    return [dict(codigo=row["code"], area=row["area"], data=row["date"]) for row in rows]


def list_visitors(apartment: str) -> list[dict[str, str]]:
    with _connection() as db:
        rows = db.execute(
            """SELECT name, date FROM visitors
               WHERE apartment = ? ORDER BY date, id""",
            (str(apartment),),
        ).fetchall()
    return [dict(nome=row["name"], data=row["date"]) for row in rows]


def is_area_available(area_id: str, date: str) -> bool:
    with _connection() as db:
        return (
            db.execute(
                """SELECT 1 FROM reservations
                   WHERE area = ? AND date = ? AND cancelled = 0 LIMIT 1""",
                (area_id, date),
            ).fetchone()
            is None
        )


def reserve_atomically(
    apartment: str,
    area_id: str,
    date: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Insert a booking under the database lock and unique active-slot index."""
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT result FROM completed_actions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if previous:
            db.commit()
            return json.loads(previous["result"])

        area = db.execute(
            "SELECT id FROM areas WHERE id = ?", (area_id,)
        ).fetchone()
        if not area:
            result = {"status": "invalid_area"}
        else:
            try:
                occupied = db.execute(
                    """SELECT 1 FROM reservations
                       WHERE area = ? AND date = ? AND cancelled = 0 LIMIT 1""",
                    (area_id, date),
                ).fetchone()
                if occupied:
                    result = {"status": "occupied"}
                else:
                    code = "RSV-" + uuid.uuid4().hex.upper()
                    while db.execute(
                        "SELECT 1 FROM reservation_codes WHERE code = ?", (code,)
                    ).fetchone():
                        code = "RSV-" + uuid.uuid4().hex.upper()
                    db.execute("INSERT INTO reservation_codes(code) VALUES (?)", (code,))
                    db.execute(
                        """INSERT INTO reservations(code, apartment, area, date, cancelled)
                           VALUES (?, ?, ?, ?, 0)""",
                        (code, apartment, area_id, date),
                    )
                    result = {"status": "reserved", "codigo": code}
            except sqlite3.IntegrityError:
                # The partial unique index is the final arbiter of a race.
                result = {"status": "occupied"}
        db.execute(
            "INSERT INTO completed_actions(idempotency_key, result) VALUES (?, ?)",
            (idempotency_key, json.dumps(result, ensure_ascii=False)),
        )
        db.commit()
        return result


def cancel_own_reservation(apartment: str, area_id: str, date: str) -> bool:
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            """UPDATE reservations SET cancelled = 1
               WHERE apartment = ? AND area = ? AND date = ? AND cancelled = 0""",
            (apartment, area_id, date),
        )
        db.commit()
    return cursor.rowcount > 0


def authorize_visitor_atomically(
    apartment: str,
    name: str,
    date: str,
    idempotency_key: str,
) -> dict[str, str]:
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT result FROM completed_actions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if previous:
            db.commit()
            return json.loads(previous["result"])
        db.execute(
            "INSERT INTO visitors(apartment, name, date) VALUES (?, ?, ?)",
            (apartment, name.strip(), date),
        )
        result = {"status": "authorized", "nome": name.strip(), "data": date}
        db.execute(
            "INSERT INTO completed_actions(idempotency_key, result) VALUES (?, ?)",
            (idempotency_key, json.dumps(result, ensure_ascii=False)),
        )
        db.commit()
        return result


def save_session(session_id: str, apartment: str, user_id: str) -> None:
    with _connection() as db:
        db.execute(
            "INSERT INTO api_sessions(session_id, apartment, user_id) VALUES (?, ?, ?)",
            (session_id, apartment, user_id),
        )


def session_record(session_id: str) -> dict[str, str] | None:
    with _connection() as db:
        row = db.execute(
            "SELECT session_id, apartment, user_id FROM api_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def save_pending_confirmation(
    *,
    confirmation_id: str,
    session_id: str,
    invocation_id: str,
    function_name: str,
    function_call_id: str,
    function_args: dict[str, Any],
    confirmation_payload: dict[str, Any],
    action: str,
    details: dict[str, Any],
) -> None:
    with _connection() as db:
        db.execute(
            """INSERT OR IGNORE INTO confirmations
               (id, session_id, invocation_id, function_name, function_call_id,
                function_args, confirmation_payload, action, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                confirmation_id,
                session_id,
                invocation_id,
                function_name,
                function_call_id,
                json.dumps(function_args, ensure_ascii=False),
                json.dumps(confirmation_payload, ensure_ascii=False),
                action,
                json.dumps(details, ensure_ascii=False),
            ),
        )


def pending_confirmations(session_id: str) -> list[dict[str, Any]]:
    with _connection() as db:
        rows = db.execute(
            """SELECT id, action, details FROM confirmations
               WHERE session_id = ? AND status = 'pending' ORDER BY created_at, id""",
            (session_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "acao": row["action"],
            "detalhes": json.loads(row["details"]),
        }
        for row in rows
    ]


def claim_confirmation(session_id: str, confirmation_id: str) -> dict[str, Any] | None:
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT * FROM confirmations
               WHERE id = ? AND session_id = ? AND status = 'pending'""",
            (confirmation_id, session_id),
        ).fetchone()
        if not row:
            db.rollback()
            return None
        db.execute(
            "UPDATE confirmations SET status = 'processing' WHERE id = ?",
            (confirmation_id,),
        )
        db.commit()
    result = dict(row)
    result["function_args"] = json.loads(result["function_args"])
    result["confirmation_payload"] = json.loads(result["confirmation_payload"])
    return result


def finish_confirmation(confirmation_id: str, confirmed: bool) -> None:
    status = "approved" if confirmed else "denied"
    with _connection() as db:
        db.execute(
            "UPDATE confirmations SET status = ? WHERE id = ? AND status = 'processing'",
            (status, confirmation_id),
        )
