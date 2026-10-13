"""Guarded execution: validated SQL runs against a READ-ONLY connection
with enforced limits, a statement timeout, PII masking, and audit logging.

Nothing reaches the database unless guardrails.validate_sql() passes first.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import sqlparse

from .guardrails import ValidationResult, validate_sql
from .schema import Schema
from .text2sql import RefusalError, generate_sql

ROW_CAP = 200            # max rows returned to the caller
STATEMENT_TIMEOUT_S = 5  # wall-clock budget per query
DEFAULT_LIMIT = 100


class GuardrailError(Exception):
    """Raised when generated SQL fails static validation."""


def mask_pii(value, column: str, pii_columns: set[str]):
    """Mask PII columns: j***@example.com style. Non-PII passes through."""
    if column not in pii_columns or value is None:
        return value
    text = str(value)
    if "@" in text:
        local, domain = text.split("@", 1)
        return f"{local[:1]}***@{domain}" if local else f"***@{domain}"
    return text[:1] + "***" if text else text


def _has_limit(sql: str) -> bool:
    return any(
        t.ttype is sqlparse.tokens.Keyword and t.value.upper() == "LIMIT"
        for t in sqlparse.parse(sql)[0].flatten()
    )


def _armed_connection(db_path: str) -> sqlite3.Connection:
    """Read-only connection with a statement timeout via progress handler."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    start = time.time()

    def _handler() -> int:
        return 1 if (time.time() - start) > STATEMENT_TIMEOUT_S else 0

    conn.set_progress_handler(_handler, 1000)
    return conn


def execute(
    sql: str,
    db_path: str,
    schema: Schema,
    question: str = "",
    audit_path: str | None = None,
) -> dict:
    """Validate -> execute (read-only) -> mask -> audit. Returns result dict."""
    started = time.time()
    verdict_checks: list[str] = []

    validation: ValidationResult = validate_sql(sql, schema)
    if not validation.ok:
        _audit(audit_path, question, sql, False, 0, 0.0, validation.errors)
        raise GuardrailError("; ".join(validation.errors))
    verdict_checks += validation.checks
    clean = validation.sql

    # Enforce a LIMIT so a missing one can't dump the table.
    if not _has_limit(clean):
        clean = f"{clean} LIMIT {ROW_CAP + 1}"
        verdict_checks.append(f"LIMIT enforced (was missing, set to {ROW_CAP + 1})")
    else:
        verdict_checks.append("LIMIT present in query")

    conn = _armed_connection(db_path)
    try:
        cur = conn.execute(clean)
        columns = [d[0] for d in cur.description] if cur.description else []
        raw_rows = cur.fetchall()
    except sqlite3.OperationalError as exc:
        # Includes timeouts aborted by the progress handler.
        _audit(audit_path, question, clean, False, 0, 0.0, [str(exc)])
        raise GuardrailError(f"execution blocked: {exc}") from exc
    finally:
        conn.close()
    elapsed_ms = round((time.time() - started) * 1000, 1)
    verdict_checks.append("read-only connection (mode=ro)")
    verdict_checks.append(f"statement timeout armed ({STATEMENT_TIMEOUT_S}s)")

    truncated = len(raw_rows) > ROW_CAP
    rows = [
        {
            col: mask_pii(val, col, schema.pii_columns)
            for col, val in zip(columns, row)
        }
        for row in raw_rows[:ROW_CAP]
    ]
    if schema.pii_columns & set(columns):
        verdict_checks.append(
            f"PII masked ({sorted(schema.pii_columns & set(columns))})"
        )

    _audit(audit_path, question, clean, True, len(raw_rows), elapsed_ms, [])
    return {
        "sql": clean,
        "columns": columns,
        "rows": rows,
        "row_count": len(raw_rows),
        "truncated": truncated,
        "query_ms": elapsed_ms,
        "verdict_checks": verdict_checks,
    }


def _audit(audit_path, question, sql, ok, row_count, ms, errors):
    if not audit_path:
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "sql": sql,
        "ok": ok,
        "row_count": row_count,
        "query_ms": ms,
        "errors": errors,
    }
    Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def ask(
    question: str,
    db_path: str,
    schema: Schema,
    provider: str | None = None,
    audit_path: str | None = None,
) -> dict:
    """Full pipeline: NL question -> SQL -> guardrails -> execution -> result."""
    sql = generate_sql(question, schema, provider)  # may raise RefusalError
    result = execute(sql, db_path, schema, question=question, audit_path=audit_path)
    result["question"] = question
    return result
