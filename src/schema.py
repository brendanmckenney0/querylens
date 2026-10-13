"""Schema introspection: turn the live SQLite schema into a prompt-friendly
description for the text-to-SQL model, plus allowlists for the guardrails."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class Column:
    name: str
    type: str
    samples: list[str] = field(default_factory=list)
    pii: bool = False


@dataclass
class Table:
    name: str
    columns: list[Column]
    row_count: int


@dataclass
class Schema:
    tables: list[Table]
    joins: list[str]
    pii_columns: set[str]

    def allowed_tables(self) -> set[str]:
        return {t.name for t in self.tables}

    def column_index(self) -> dict[str, set[str]]:
        """table -> set of column names."""
        return {t.name: {c.name for c in t.columns} for t in self.tables}

    def all_columns(self) -> set[str]:
        cols: set[str] = set()
        for t in self.tables:
            cols.update(c.name for c in t.columns)
        return cols

    def describe(self) -> str:
        """Prompt-friendly schema description with samples and join hints."""
        lines = ["Database schema (SQLite, read-only):", ""]
        for t in self.tables:
            col_bits = []
            for c in t.columns:
                bit = f"{c.name} {c.type}"
                if c.pii:
                    bit += " [PII - will be masked in results]"
                if c.samples:
                    shown = ", ".join(f"'{s}'" for s in c.samples[:4])
                    bit += f" (e.g. {shown})"
                col_bits.append(bit)
            lines.append(f"- {t.name} ({t.row_count} rows): " + "; ".join(col_bits))
        lines += ["", "Join relationships:"]
        lines += [f"- {j}" for j in self.joins] or ["- (none declared)"]
        lines += [
            "",
            "Conventions: datetimes are ISO-8601 TEXT ('2025-03-14T09:22:10').",
            "Use strftime('%Y', col) / strftime('%Y-%m', col) for date parts,",
            "and julianday(b) - julianday(a) for day differences.",
        ]
        return "\n".join(lines)


# Columns treated as PII and masked in every result set.
PII_COLUMNS = {"email"}


def introspect(db_path: str) -> Schema:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables: list[Table] = []
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall():
            cols: list[Column] = []
            for _cid, cname, ctype, _nn, _dflt, _pk in conn.execute(
                f"PRAGMA table_info({name})"
            ).fetchall():
                samples = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT DISTINCT {cname} FROM {name} "
                        f"WHERE {cname} IS NOT NULL LIMIT 4"
                    ).fetchall()
                ]
                cols.append(
                    Column(
                        name=cname,
                        type=ctype or "TEXT",
                        samples=[str(s) for s in samples],
                        pii=cname in PII_COLUMNS,
                    )
                )
            row_count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            tables.append(Table(name=name, columns=cols, row_count=row_count))

        joins: list[str] = []
        for t in tables:
            for _id, _seq, ref_table, frm, to, *_rest in conn.execute(
                f"PRAGMA foreign_key_list({t.name})"
            ).fetchall():
                joins.append(f"{t.name}.{frm} -> {ref_table}.{to}")
        return Schema(tables=tables, joins=joins, pii_columns=set(PII_COLUMNS))
    finally:
        conn.close()
