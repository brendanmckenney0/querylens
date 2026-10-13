# Guarded Execution at Enterprise Scale

QueryLens's demo runs text-to-SQL against a local SQLite file with a
handful of guardrails. This sketch describes how the same
*guarded-execution* pattern extends to production databases — and why
"the AI runs queries" is only sellable **with** these guardrails. Lead
with the safety story, exactly like permissions in the RAG design.

## The core thesis

Nobody sane lets an LLM hold database credentials. The enterprise
version inverts the relationship: **the AI never touches the
database.** It produces *candidate SQL*, and a deterministic,
auditable execution layer decides whether that SQL may run, where,
and with what limits. The LLM is untrusted input to the executor —
the same way user form input is untrusted input to a web app.

## 1. Identity and least-privilege access

- **Read-only service accounts, one per tenant** (at minimum). The
  executor connects as a database role that physically *cannot*
  write: `GRANT SELECT` only, no DDL, no DCL. Even if every software
  guardrail failed, the database itself refuses writes.
- **Per-role schema exposure.** Different users see different slices:
  support leads see tickets but not `salary`-grade tables; finance
  sees invoices but masked customer emails. Implement as database
  views per role, or as executor-side table/column allowlists derived
  from the caller's identity (the demo's allowlist, promoted to
  per-user).
- **Credentials in a secrets manager** (Vault, AWS Secrets Manager),
  rotated on a schedule, never in application config. Short-lived
  credentials via dynamic secrets where the platform supports it.

## 2. The validation pipeline (deterministic, testable)

Every candidate query passes through, in order:

1. **Parse** (sqlparse / sqlglot): exactly one statement.
2. **Statement-type gate**: `SELECT` / `WITH ... SELECT` only.
   Everything else — including `SELECT ... INTO`, `FOR UPDATE`,
   stacked statements — is rejected.
3. **Denylist scan**: write/DDL/DCL keywords, comments, stacked
   statements, access to system catalogs (`pg_catalog`,
   `information_schema`, `sqlite_master`...).
4. **Allowlist check**: tables and columns against the caller's
   permitted schema. Unknown identifiers fail closed.
5. **Cost estimation**: `EXPLAIN` the query; reject on full-table
   scans above a size threshold or estimated cost budget.
6. **Rewrite**: append/merge a `LIMIT` (or `TOP`/`FETCH FIRST`), cap
   it at the role's maximum. Force the role's row-level predicates
   (e.g. `tenant_id = $current_tenant`) — never trust the model to
   include them.

Steps 1–4 are pure functions over the SQL string: unit-testable,
fuzzable, and shippable without any model in the loop. That is the
point — the safety story must not depend on the LLM behaving.

## 3. Execution sandbox

- **Timeouts and resource governors**: statement timeout (e.g.
  30s), per-role concurrency limits, and — on Postgres — a
  `statement_timeout` plus a separate connection pool for AI queries
  so a runaway can't starve the application.
- **Read replicas**: AI queries run against replicas, never the
  primary. Analytical questions don't belong on the write path.
- **Result caps**: hard row cap (paginated in the UI), byte cap on
  result payloads.
- **Dry-run mode**: `EXPLAIN ANALYZE` without execution for the
  approval flow below.

## 4. PII and data masking

- **Column policies per role**: `email` → `j***@example.com`;
  full masking (`***`) for SSNs, card numbers; tokenization where
  join keys must stay consistent.
- **Detection**: declared policies beat regexes, but run a PII
  scanner over new columns at onboarding so undeclared PII doesn't
  slip through.
- **Masking happens in the executor**, after the database returns
  rows and before the caller sees them — the model never sees raw
  PII either (important: generated SQL may *filter* on PII, but
  result *projection* is masked).

## 5. Human approval for the non-trivial

Not everything should auto-execute. Tiered policy:

- **Auto-run**: single-table SELECTs / simple joins under the row
  cap, against the replica, with masked PII. This covers ~80% of
  questions and is where the demo lives.
- **Preview + one-click approve**: multi-table aggregations,
  expensive-looking plans, or queries touching sensitive tables.
  The UI shows the SQL, the EXPLAIN cost, and the masked schema
  slice — a human approves, then it runs.
- **Never**: anything the validator rejects, anything against the
  primary, anything outside the role's schema.

The approval queue itself becomes a training signal: approved
queries join the eval set.

## 6. Audit trail

Every execution — allowed or blocked — logs: who asked, the natural
language question, the candidate SQL, the validator's verdict with
reasons, row count, timing, and result hash. Ship these logs to the
customer's SIEM (Splunk/Datadog/Sentinel). Blocked attempts get
alerted on: a spike in rejected queries is either an attack or a
schema the validator doesn't understand — both worth knowing about.

## 7. Per-database connector notes

- **Postgres**: `SET ROLE readonly_role; SET statement_timeout`;
  `EXPLAIN (COSTS)` for the cost gate; RLS policies as a second
  enforcement layer behind the executor's own checks (defense in
  depth, not the primary control).
- **BigQuery / Snowflake**: service accounts with dataset-level
  grants; cost controls are *money* controls — set per-query byte
  budgets and kill switches, because one bad query can cost real
  dollars. Dry-run (`dryRun: true`) before execution is cheap and
  should be mandatory in the approval tier.
- **MySQL**: no CTE-based tricks needed; watch for `INTO OUTFILE`
  and `LOAD_FILE` in the denylist.

## 8. Why this is the pitch

"The AI writes SQL" is a demo. "The AI writes SQL and here is the
six-stage deterministic pipeline that decides what may execute, the
per-role schema exposure, the masking policy, the approval flow, and
the SIEM-integrated audit trail" is a product a data team will let
near their warehouse. Same lesson as the RAG work: **the safety
story is the product story.**
