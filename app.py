"""QueryLens demo server: FastAPI + single-page chat UI."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.executor import GuardrailError, ask  # noqa: E402
from src.schema import introspect  # noqa: E402
from src.text2sql import RefusalError  # noqa: E402

DB_PATH = os.environ.get("QUERYLENS_DB", str(ROOT / "data" / "querylens.db"))
AUDIT_PATH = os.environ.get("QUERYLENS_AUDIT", str(ROOT / "data" / "audit.jsonl"))
PROVIDER = os.environ.get("PROVIDER", "local")

app = FastAPI(title="QueryLens")
_schema = None


def get_schema():
    global _schema
    if _schema is None:
        if not Path(DB_PATH).exists():
            raise RuntimeError(
                f"Database not found at {DB_PATH}. Run: python scripts/seed_db.py"
            )
        _schema = introspect(DB_PATH)
    return _schema


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health():
    schema = get_schema()
    return {
        "status": "ok",
        "provider": PROVIDER,
        "tables": [t.name for t in schema.tables],
    }


@app.post("/ask")
def ask_question(req: AskRequest):
    question = req.question.strip()
    if not question:
        return JSONResponse({"ok": False, "error": "empty question"}, status_code=400)
    schema = get_schema()
    try:
        result = ask(question, DB_PATH, schema, provider=PROVIDER,
                     audit_path=AUDIT_PATH)
        return {
            "ok": True,
            "question": result["question"],
            "sql": result["sql"],
            "verdict_checks": result["verdict_checks"],
            "columns": result["columns"],
            "rows": result["rows"],
            "row_count": result["row_count"],
            "truncated": result["truncated"],
            "query_ms": result["query_ms"],
        }
    except (RefusalError, GuardrailError) as exc:
        return {"ok": False, "error": str(exc), "refused": True}
    except Exception as exc:  # never leak tracebacks to the demo UI
        return JSONResponse({"ok": False, "error": f"unexpected error: {exc}"},
                            status_code=500)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static" / "index.html")
