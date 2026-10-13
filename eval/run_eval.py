#!/usr/bin/env python3
"""QueryLens eval runner.

Compares RESULT SETS (not SQL strings — many formulations are valid),
and verifies the guardrails reject dangerous inputs.

Usage:
    PROVIDER=local python eval/run_eval.py [--db data/querylens.db]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.executor import GuardrailError, ask  # noqa: E402
from src.guardrails import validate_sql  # noqa: E402
from src.schema import introspect  # noqa: E402
from src.text2sql import RefusalError  # noqa: E402


def check(expect: dict, result: dict) -> tuple[bool, str]:
    kind = expect["check"]
    if kind == "all":
        for sub in expect["checks"]:
            ok, msg = check(sub, result)
            if not ok:
                return False, msg
        return True, "all sub-checks passed"
    if kind == "rejected":
        return False, "expected rejection but query executed"
    rows, cols = result["rows"], result["columns"]
    if kind == "row_count_eq":
        return (len(rows) == expect["value"],
                f"rows={len(rows)} expected={expect['value']}")
    if kind == "min_rows":
        return (len(rows) >= expect["value"],
                f"rows={len(rows)} expected>={expect['value']}")
    if kind == "columns_contain":
        missing = [c for c in expect["value"] if c not in cols]
        return (not missing, f"missing columns: {missing}" if missing else "ok")
    if kind == "single_value_eq":
        val = rows[0][cols[0]] if rows and cols else None
        return (val == expect["value"], f"value={val} expected={expect['value']}")
    if kind == "single_value_gt":
        val = rows[0][cols[0]] if rows and cols else None
        ok = isinstance(val, (int, float)) and val > expect["value"]
        return (ok, f"value={val} expected>{expect['value']}")
    if kind == "all_equal":
        col, want = expect["column"], expect["value"]
        bad = [r for r in rows if r.get(col) != want]
        return (not bad and rows, f"{len(bad)} rows != {want}")
    return False, f"unknown check {kind}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "querylens.db"))
    args = ap.parse_args()

    spec = json.loads((ROOT / "eval" / "eval_set.json").read_text())
    schema = introspect(args.db)
    passed, failed = 0, []

    print("== NL -> SQL cases ==")
    for case in spec["cases"]:
        qid, question = case["id"], case["question"]
        try:
            result = ask(question, args.db, schema, audit_path=None)
            rejected = False
        except (RefusalError, GuardrailError) as exc:
            rejected, result = True, {"error": str(exc)}
        want_reject = case["expect"]["check"] == "rejected"
        if want_reject:
            ok, msg = (rejected, "correctly rejected" if rejected
                       else "NOT rejected (executed!)")
        elif rejected:
            ok, msg = False, f"unexpected refusal: {result['error']}"
        else:
            ok, msg = check(case["expect"], result)
        print(f"[{'PASS' if ok else 'FAIL'}] {qid}: {question[:60]} — {msg}")
        if not ok:
            failed.append(qid)
            if not rejected and not want_reject:
                print(f"       SQL was: {result.get('sql')}")
        else:
            passed += 1

    print("\n== raw guardrail tests ==")
    for t in spec["guardrail_tests"]:
        vr = validate_sql(t["sql"], schema)
        ok = vr.ok == t["expect_ok"]
        want = "allow" if t["expect_ok"] else "reject"
        print(f"[{'PASS' if ok else 'FAIL'}] {want}: {t['sql'][:70]}"
              + ("" if ok else f" — got ok={vr.ok}: {vr.errors}"))
        if ok:
            passed += 1
        else:
            failed.append(t["sql"][:40])

    total = len(spec["cases"]) + len(spec["guardrail_tests"])
    print(f"\n{passed}/{total} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
