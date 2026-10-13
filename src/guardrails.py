"""Guardrails: static validation of generated SQL BEFORE anything executes.

Policy (fail closed):
  - Exactly one statement; it must be SELECT or WITH ... SELECT.
  - No write/DDL/DCL keywords anywhere, including inside subqueries.
  - No stacked statements, no comments (comment smuggling is a classic).
  - Every referenced table must be in the schema allowlist (plus CTE names).
  - No access to sqlite internal tables (sqlite_master, sqlite_sequence...).
  - Dotted column references (table.column) are checked against the schema;
    bare column names must exist in at least one table. SELECT * is allowed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlparse
from sqlparse.sql import Identifier, IdentifierList, Parenthesis
from sqlparse.tokens import Comment, Keyword, Name, Wildcard

from .schema import Schema

FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "GRANT", "REVOKE", "ATTACH", "DETACH", "VACUUM", "PRAGMA", "REINDEX",
    "REPLACE", "MERGE", "CALL", "EXECUTE", "COPY",
}
FORBIDDEN_TABLES = {"sqlite_master", "sqlite_temp_master", "sqlite_sequence"}


@dataclass
class ValidationResult:
    ok: bool
    sql: str = ""
    errors: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)


def _first_keyword(stmt) -> str | None:
    for tok in stmt.tokens:
        if tok.is_whitespace:
            continue
        if tok.ttype in (Comment.Single, Comment.Multiline):
            continue
        return tok.value.upper()
    return None


def _tables_in(stmt, cte_names: set[str]) -> set[str]:
    """Best-effort table extraction from FROM/JOIN clauses (incl. CTEs)."""
    tables: set[str] = set()
    tokens = [t for t in stmt.tokens if not t.is_whitespace]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.ttype in Keyword and tok.value.upper() in ("FROM", "JOIN"):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if isinstance(nxt, IdentifierList):
                for ident in nxt.get_identifiers():
                    tables.add(_ident_table(ident))
            elif isinstance(nxt, Identifier):
                tables.add(_ident_table(nxt))
            elif nxt is not None and nxt.ttype is Name:
                tables.add(nxt.value)
            i += 1
        elif isinstance(tok, Parenthesis):
            tables.update(_tables_in(tok, cte_names))
        i += 1
    return {t for t in tables if t}


def _ident_table(ident: Identifier) -> str:
    name = ident.get_real_name()
    parent = ident.get_parent_name()
    return parent or name or ""


def _cte_names(stmt) -> set[str]:
    names: set[str] = set()
    tokens = [t for t in stmt.tokens if not t.is_whitespace]
    # NB: `WITH` lexes as Token.Keyword.CTE (a Keyword *subtype*), so use
    # `in` rather than `is` for the type check.
    if tokens and tokens[0].ttype in Keyword and tokens[0].value.upper() == "WITH":
        for tok in tokens[1:]:
            if isinstance(tok, Identifier):
                names.add(tok.get_real_name())
            elif isinstance(tok, IdentifierList):
                names.update(i.get_real_name() for i in tok.get_identifiers())
            elif tok.ttype in Keyword and tok.value.upper() == "SELECT":
                break
    return {n for n in names if n}


def _dotted_columns(stmt) -> set[tuple[str, str]]:
    """Collect table.column references."""
    found: set[tuple[str, str]] = set()
    for tok in stmt.flatten():
        if tok.ttype is Name and "." in (tok.value or ""):
            parts = tok.value.split(".")
            if len(parts) == 2:
                found.add((parts[0], parts[1]))
    for sub in getattr(stmt, "tokens", []):
        if isinstance(sub, Parenthesis):
            found.update(_dotted_columns(sub))
    return found


def _bare_columns(stmt) -> set[str]:
    found: set[str] = set()
    for tok in stmt.flatten():
        if tok.ttype is Name and "." not in (tok.value or ""):
            found.add(tok.value)
    return found


def validate_sql(sql: str, schema: Schema) -> ValidationResult:
    errors: list[str] = []
    checks: list[str] = []

    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        return ValidationResult(ok=False, errors=["empty SQL"])

    statements = [s for s in sqlparse.parse(cleaned) if str(s).strip()]
    if len(statements) != 1:
        return ValidationResult(
            ok=False, errors=[f"expected exactly 1 statement, found {len(statements)}"]
        )
    checks.append("single statement")
    stmt = statements[0]

    # No comments anywhere (smuggling vector).
    if any(t.ttype in (Comment.Single, Comment.Multiline) for t in stmt.flatten()):
        return ValidationResult(ok=False, errors=["SQL comments are not allowed"])
    checks.append("no comments")

    # Statement type: SELECT or WITH...SELECT only.
    first = _first_keyword(stmt)
    if first not in ("SELECT", "WITH"):
        return ValidationResult(
            ok=False, errors=[f"only SELECT/WITH queries allowed, got {first}"]
        )
    if first == "WITH":
        tail = cleaned.upper()
        # WITH must resolve to a SELECT (no INSERT/UPDATE/DELETE after CTEs).
        body = re_tail_after_ctes(tail)
        if not body.startswith("SELECT"):
            return ValidationResult(
                ok=False, errors=["WITH clause must end in a SELECT"]
            )
    checks.append("read-only statement type (SELECT/WITH)")

    # Forbidden keywords anywhere in the token stream.
    hit = {
        t.value.upper()
        for t in stmt.flatten()
        if t.ttype is Keyword and t.value.upper() in FORBIDDEN_KEYWORDS
    }
    if hit:
        return ValidationResult(
            ok=False, errors=[f"forbidden keywords: {sorted(hit)}"]
        )
    checks.append("no write/DDL/DCL keywords")

    # Table allowlist (+ CTE names defined in the query itself).
    allowed = schema.allowed_tables() | _cte_names(stmt)
    referenced = _tables_in(stmt, _cte_names(stmt))
    unknown = {t for t in referenced if t.lower() not in {a.lower() for a in allowed}}
    internal = {t for t in referenced if t.lower() in FORBIDDEN_TABLES}
    if internal:
        return ValidationResult(
            ok=False, errors=[f"internal tables are off-limits: {sorted(internal)}"]
        )
    if unknown:
        return ValidationResult(
            ok=False, errors=[f"unknown tables: {sorted(unknown)}"]
        )
    checks.append(f"tables in allowlist ({len(referenced)} referenced)")

    # Column checks: dotted refs must resolve; bare names must exist somewhere.
    # Names the query itself defines (CTE names, AS aliases, table aliases)
    # and SQL function names are exempt from the bare-name check.
    # Table names also surface as bare Name tokens, so allowlist them too.
    defined = _query_defined_names(cleaned, _cte_names(stmt)) | allowed
    col_index = schema.column_index()
    for table, col in _dotted_columns(stmt):
        real = next((t for t in allowed if t.lower() == table.lower()), None)
        if real is None:
            continue  # already flagged as unknown table / alias
        if col not in col_index.get(real, set()) and col != "*":
            return ValidationResult(
                ok=False, errors=[f"unknown column {table}.{col}"]
            )
    all_cols = schema.all_columns()
    for col in _bare_columns(stmt):
        if col in defined or col.upper() in _SQL_KEYWORDS or col == "*":
            continue
        if col.upper() in _SQL_FUNCTIONS:
            continue
        if col not in all_cols:
            return ValidationResult(ok=False, errors=[f"unknown column {col}"])
    checks.append("columns resolve against schema")

    return ValidationResult(ok=True, sql=cleaned, checks=checks)


def re_tail_after_ctes(up: str) -> str:
    """Crude: find the last ')' at depth 0 in a WITH query, return what follows."""
    depth = 0
    last_close = -1
    in_str = False
    for i, ch in enumerate(up):
        if ch == "'" and not in_str:
            in_str = True
        elif ch == "'" and in_str:
            in_str = False
        elif in_str:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                last_close = i
    return up[last_close + 1 :].strip() if last_close >= 0 else up


def _query_defined_names(sql: str, cte_names: set[str]) -> set[str]:
    """Names the query defines itself: CTE names, AS aliases, table aliases."""
    names = set(cte_names)
    names.update(
        m.group(1)
        for m in re.finditer(r"\bAS\s+([A-Za-z_]\w*)", sql, re.IGNORECASE)
    )
    for m in re.finditer(
        r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?",
        sql,
        re.IGNORECASE,
    ):
        alias = m.group(2)
        if alias and alias.upper() not in _SQL_KEYWORDS and alias.upper() not in (
            "ON", "WHERE", "GROUP", "ORDER", "LIMIT", "LEFT", "RIGHT",
            "INNER", "OUTER", "FULL", "CROSS",
        ):
            names.add(alias)
    return names


_SQL_FUNCTIONS = {
    "STRFTIME", "JULIANDAY", "DATE", "DATETIME", "TIME", "COALESCE", "IFNULL",
    "NULLIF", "LOWER", "UPPER", "LENGTH", "SUBSTR", "SUBSTRING", "TRIM",
    "LTRIM", "RTRIM", "ROUND", "ABS", "CAST", "PRINT", "TYPEOF", "RANDOM",
}


_SQL_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "LIMIT",
    "OFFSET", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "ON",
    "AS", "AND", "OR", "NOT", "IN", "IS", "NULL", "DISTINCT", "UNION",
    "ALL", "CASE", "WHEN", "THEN", "ELSE", "END", "BETWEEN", "LIKE",
    "EXISTS", "WITH", "ASC", "DESC", "COUNT", "SUM", "AVG", "MIN", "MAX",
}
