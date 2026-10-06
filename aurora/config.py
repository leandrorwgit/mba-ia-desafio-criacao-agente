from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "dados"
STATE_DIR = ROOT / ".aurora"
DATABASE_PATH = STATE_DIR / "aurora.sqlite3"
APP_NAME = "residencial_aurora"
USER_ID = "morador"
CONFIRMATION_FUNCTION_NAME = "adk_request_confirmation"

load_dotenv(ROOT / ".env")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")


def model_name() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
