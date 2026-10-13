"""Natural language -> SQL, behind an env-selected provider.

PROVIDER=openai  -> real NL-to-SQL via the OpenAI API (needs OPENAI_API_KEY).
PROVIDER=local   -> no-key template engine. It is deliberately narrow: it
                    handles the demo/eval question shapes via ordered regex
                    rules and refuses anything else. Real answer quality
                    needs the OpenAI provider; the local one exists so the
                    pipeline, guardrails, and eval are verifiable offline.
"""
from __future__ import annotations

import os
import re

from .schema import Schema


class RefusalError(Exception):
    """Raised when the provider will not formulate SQL for a question."""


SYSTEM_PROMPT = """You are a text-to-SQL assistant for a READ-ONLY SQLite database.
Given the schema below, write ONE SQLite query that answers the question.

Hard rules:
- SELECT or WITH ... SELECT only. Never INSERT/UPDATE/DELETE/DDL.
- Query exactly one statement. Always add a LIMIT (default 100) unless the
  question asks for a single aggregate (COUNT/SUM/AVG...).
- Datetimes are ISO-8601 TEXT. Use strftime('%Y', col) for years and
  julianday(b) - julianday(a) for day differences.
- Prefer explicit JOINs using the documented relationships.

Schema:
{schema}

Return ONLY the SQL. No explanation, no markdown fences."""


class OpenAIProvider:
    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def generate(self, question: str, schema: Schema) -> str:
        from openai import OpenAI

        client = OpenAI()  # reads OPENAI_API_KEY from the environment
        resp = client.chat.completions.create(
            model=self.model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(schema=schema.describe()),
                },
                {"role": "user", "content": question},
            ],
        )
        sql = resp.choices[0].message.content.strip()
        # Strip accidental markdown fences.
        sql = re.sub(r"^```sql\s*|\s*```$", "", sql, flags=re.IGNORECASE).strip()
        return sql


# Any question matching these is refused before template matching.
_DANGEROUS = re.compile(
    r"ignore\s+(all\s+)?previous\s+instructions"
    r"|\b(drop|delete|insert|update|alter|truncate|create|grant|revoke|attach|pragma)\b"
    r"|sqlite_master"
    r"|;\s*\w"  # stacked statements smuggled into "natural language"
    r"--|/\*",
    re.IGNORECASE,
)


class LocalProvider:
    """Deterministic template engine covering the demo's question shapes."""

    def _top_customers(self, m: re.Match) -> str:
        n = m.group(1) or "5"
        return (
            "SELECT c.name, c.email, SUM(i.amount) AS lifetime_total "
            "FROM customers c JOIN invoices i ON i.customer_id = c.id "
            "WHERE i.status = 'paid' "
            f"GROUP BY c.id, c.name, c.email ORDER BY lifetime_total DESC LIMIT {n}"
        )

    RULES: list[tuple[re.Pattern, object]] = []

    def generate(self, question: str, schema: Schema) -> str:
        q = question.strip().lower()
        if _DANGEROUS.search(q):
            raise RefusalError(
                "I can't help with that — I only answer read-only questions "
                "about the database."
            )
        for pattern, builder in self._rules():
            m = pattern.search(q)
            if m:
                return builder(m)
        raise RefusalError(
            "I couldn't formulate a safe query for that question. "
            "Try asking about customers, subscriptions, invoices, or tickets."
        )

    @classmethod
    def _rules(cls):
        if cls.RULES:
            return cls.RULES
        prov = cls  # for readability below
        cls.RULES = [
            (re.compile(r"joined in (20\d\d)"),
             lambda m: "SELECT COUNT(*) AS customer_count FROM customers "
                       f"WHERE strftime('%Y', created_at) = '{m.group(1)}'"),
            (re.compile(r"how many customers"),
             lambda m: "SELECT COUNT(*) AS customer_count FROM customers"),
            (re.compile(r"customers? (by|per) country"),
             lambda m: "SELECT country, COUNT(*) AS n FROM customers "
                       "GROUP BY country ORDER BY n DESC"),
            (re.compile(r"total mrr|mrr.*\bactive\b"),
             lambda m: "SELECT SUM(mrr) AS total_mrr FROM subscriptions "
                       "WHERE status = 'active'"),
            (re.compile(r"average mrr|avg mrr"),
             lambda m: "SELECT AVG(mrr) AS avg_mrr FROM subscriptions "
                       "WHERE status = 'active'"),
            (re.compile(r"enterprise plan"),
             lambda m: "SELECT COUNT(*) AS enterprise_subscriptions "
                       "FROM subscriptions WHERE plan = 'enterprise'"),
            (re.compile(r"which plan has the most|most subscriptions"),
             lambda m: "SELECT plan, COUNT(*) AS n FROM subscriptions "
                       "GROUP BY plan ORDER BY n DESC LIMIT 1"),
            (re.compile(r"top\s*(\d+)?\s*customers by"),
             lambda m: prov()._top_customers(m)),
            (re.compile(r"average invoice|avg invoice"),
             lambda m: "SELECT AVG(amount) AS avg_invoice_amount FROM invoices"),
            (re.compile(r"total revenue|revenue.*\bpaid\b"),
             lambda m: "SELECT SUM(amount) AS paid_revenue FROM invoices "
                       "WHERE status = 'paid'"),
            (re.compile(r"largest.*invoice|biggest.*invoice|max.*invoice"),
             lambda m: "SELECT MAX(amount) AS max_invoice_amount FROM invoices"),
            (re.compile(r"overdue"),
             lambda m: "SELECT COUNT(*) AS overdue_invoices FROM invoices "
                       "WHERE status = 'overdue'"),
            (re.compile(r"\bopen\b.*tickets?|tickets?.*\bopen\b"),
             lambda m: "SELECT COUNT(*) AS open_tickets FROM support_tickets "
                       "WHERE status = 'open'"),
            (re.compile(r"high priority"),
             lambda m: "SELECT id, customer_id, subject, priority, status, created_at "
                       "FROM support_tickets WHERE priority = 'high' "
                       "ORDER BY created_at DESC LIMIT 100"),
            (re.compile(r"\burgent\b"),
             lambda m: "SELECT id, customer_id, subject, priority, status, created_at "
                       "FROM support_tickets WHERE priority = 'urgent' "
                       "ORDER BY created_at DESC LIMIT 100"),
            (re.compile(r"most recent|latest.*tickets?"),
             lambda m: "SELECT id, customer_id, subject, priority, status, created_at "
                       "FROM support_tickets ORDER BY created_at DESC LIMIT 10"),
            (re.compile(r"percent.*resolv|resolv.*percent|% of tickets"),
             lambda m: "SELECT 100.0 * SUM(CASE WHEN status IN ('resolved','closed') "
                       "THEN 1 ELSE 0 END) / COUNT(*) AS resolved_pct "
                       "FROM support_tickets"),
            (re.compile(r"cancel(?:ed|led)"),
             lambda m: "SELECT id, customer_id, plan, mrr, started_at, canceled_at "
                       "FROM subscriptions WHERE status = 'canceled' "
                       "ORDER BY canceled_at DESC LIMIT 100"),
            (re.compile(r"resolution time|resolv.*average|average.*resolv"),
             lambda m: "SELECT AVG(julianday(resolved_at) - julianday(created_at)) "
                       "AS avg_resolution_days FROM support_tickets "
                       "WHERE resolved_at IS NOT NULL"),
        ]
        return cls.RULES


def get_provider(name: str | None = None):
    name = (name or os.environ.get("PROVIDER", "local")).lower()
    if name == "openai":
        return OpenAIProvider()
    return LocalProvider()


def generate_sql(question: str, schema: Schema, provider: str | None = None) -> str:
    return get_provider(provider).generate(question, schema)
