#!/usr/bin/env python3
"""Seed a deterministic, fully fictional SaaS database for the QueryLens demo.

All people, companies, and data are invented. The RNG is seeded so every
run produces the identical database — delete data/querylens.db and re-run
to rebuild from scratch.

Usage:
    python scripts/seed_db.py [--db data/querylens.db]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SEED = 42
# Fixed "today" so relative dates (and the eval set) are reproducible.
ANCHOR = datetime(2026, 10, 1)

FIRST_NAMES = [
    "Ava", "Liam", "Maya", "Noah", "Zoe", "Ethan", "Isla", "Lucas",
    "Mila", "Owen", "Aria", "Henry", "Ella", "Leo", "Nora", "Jack",
    "Ivy", "Oscar", "Ruby", "Felix", "Clara", "Miles", "June", "Silas",
    "Wren", "Hugo", "Ada", "Theo", "Lena", "Otis", "Nina", "Ezra",
    "Sasha", "Jude", "Tessa", "Rory", "Alba", "Kit", "Noor", "Emmett",
]

LAST_NAMES = [
    "Stone", "Reyes", "Kowalski", "Bennett", "Okafor", "Lindqvist",
    "Marsh", "Delacroix", "Petrov", "Nguyen", "Callahan", "Iversen",
    "Moreau", "Ashford", "Quintero", "Holloway", "Vance", "Sorensen",
    "Whitfield", "Abernathy", "Larkin", "Beaumont", "Cross", "Ellery",
    "Frost", "Granger", "Hale", "Irons", "Jensen", "Kessler",
    "Lockhart", "Merritt", "Nolan", "Osborne", "Pryce", "Radcliffe",
    "Sutton", "Thackeray", "Underwood", "Vaughan",
]

COMPANIES = [
    "Brightline Analytics", "Northloop", "Vantage Ops", "Corelink Systems",
    "Bluepeak Labs", "Frameworkly", "Quantia", "Hexad",
    "Loom & Ledger", "Parsec IO", "Driftwell", "Kestrelworks",
    "Modalshift", "Foundryline", "Signalcraft", "Opalstack",
    "Relaypoint", "Cloudmarble", "Fernway", "Updraftly",
    "Stonebridge Co", "Halcyon Data", "Metric & Main", "Polyrhythm",
    "Sidechannel", "Trueframe", "Voltline", "Waymarkr",
    "Yellowbrick AI", "Zeroth Systems", "Alto Mesa", "Brackenfield",
    "Cinderloop", "Dovetail Works", "Emberline", "Foxglove Labs",
    "Granary", "Harborlight", "Inkwell Ops", "Juniper Row",
    "Koda & Pine", "Larkspur Labs", "Maplefinch", "Nimbus Nine",
    "Overlook Data", "Palisade IO", "Quarryline", "Ridgeline Ops",
    "Sablewood", "Tidewater Labs", "Umbra Systems", "Vale & Vine",
    "Wrenbury", "Xenon Works", "Yonderly", "Zephyrine",
    "Aster & Oak", "Birchline", "Copperfinch", "Dunemarch",
]

COUNTRIES = [
    ("United States", 40), ("United Kingdom", 12), ("Germany", 8),
    ("France", 7), ("Canada", 9), ("Australia", 6), ("Netherlands", 5),
    ("Spain", 4), ("Ireland", 4), ("Sweden", 3), ("India", 2),
]

PLANS = ["starter", "growth", "scale", "enterprise"]
PLAN_WEIGHTS = [45, 30, 15, 10]
BASE_MRR = {"starter": 29.0, "growth": 99.0, "scale": 299.0, "enterprise": 1200.0}

SUB_STATUSES = ["active", "trialing", "past_due", "canceled"]
SUB_WEIGHTS = [68, 10, 6, 16]

INV_STATUSES = ["paid", "open", "overdue"]
INV_WEIGHTS = [80, 12, 8]

TICKET_PRIORITIES = ["low", "medium", "high", "urgent"]
TICKET_PRI_WEIGHTS = [40, 35, 18, 7]
TICKET_STATUSES = ["open", "pending", "resolved", "closed"]
TICKET_ST_WEIGHTS = [20, 15, 35, 30]

TICKET_SUBJECTS = [
    "SSO login failing for new hires", "Invoice total looks wrong",
    "How do I add a teammate?", "API rate limit hit during sync",
    "Dashboard loading very slowly", "Webhook deliveries failing",
    "Need to change billing email", "Export to CSV is truncated",
    "Two-factor reset request", "Custom domain SSL error",
    "Data discrepancy in monthly report", "Slack integration disconnected",
    "Cannot delete old workspace", "Feature request: audit log export",
    "Mobile app crashes on launch", "Password reset email not arriving",
    "How do I set up SAML?", "Duplicate charges on card",
    "Timezone wrong in scheduled reports", "Request data deletion (GDPR)",
    "Slow query on large board", "Invite link expired",
    "Billing: upgrade from growth to scale", "Permissions not applying",
    "Search returns stale results",
]

SCHEMA_SQL = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    plan TEXT NOT NULL,
    country TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    plan TEXT NOT NULL,
    mrr REAL NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    canceled_at TEXT
);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount REAL NOT NULL,
    status TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    paid_at TEXT
);
CREATE TABLE support_tickets (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    subject TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


def rand_datetime(rng: random.Random, start: datetime, end: datetime) -> datetime:
    delta = (end - start).total_seconds()
    return start + timedelta(seconds=rng.random() * delta)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def main(db_path: Path) -> None:
    rng = random.Random(SEED)
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)

    three_years_ago = ANCHOR - timedelta(days=3 * 365)

    # ---- customers ----
    combos = [(f, l) for f in FIRST_NAMES for l in LAST_NAMES]
    rng.shuffle(combos)
    customers = []
    used_emails = set()
    for i in range(500):
        first, last = combos[i]
        company = rng.choice(COMPANIES)
        slug = "".join(c for c in company.lower() if c.isalnum())
        base = f"{first}.{last}@{slug}.com".lower()
        email, n = base, 2
        while email in used_emails:
            email = f"{first}.{last}{n}@{slug}.com".lower()
            n += 1
        used_emails.add(email)
        plan = rng.choices(PLANS, weights=PLAN_WEIGHTS)[0]
        country = rng.choices(
            [c for c, _ in COUNTRIES], weights=[w for _, w in COUNTRIES]
        )[0]
        created = rand_datetime(rng, three_years_ago, ANCHOR)
        customers.append((i + 1, f"{first} {last}", email, plan, country, iso(created)))
    conn.executemany(
        "INSERT INTO customers VALUES (?,?,?,?,?,?)", customers
    )

    # ---- subscriptions ----
    subs = []
    for i in range(600):
        cid = rng.randint(1, 500)
        plan = rng.choices(PLANS, weights=PLAN_WEIGHTS)[0]
        mrr = round(BASE_MRR[plan] * rng.uniform(0.9, 1.1), 2)
        if plan == "enterprise":
            mrr = round(rng.uniform(800, 2500), 2)
        status = rng.choices(SUB_STATUSES, weights=SUB_WEIGHTS)[0]
        started = rand_datetime(rng, three_years_ago, ANCHOR)
        canceled = (
            iso(rand_datetime(rng, started, ANCHOR)) if status == "canceled" else None
        )
        subs.append((i + 1, cid, plan, mrr, status, iso(started), canceled))
    conn.executemany(
        "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?)", subs
    )

    # ---- invoices ----
    invs = []
    for i in range(2000):
        cid = rng.randint(1, 500)
        plan = customers[cid - 1][3]
        amount = round(BASE_MRR[plan] * rng.uniform(1, 6), 2)
        status = rng.choices(INV_STATUSES, weights=INV_WEIGHTS)[0]
        issued = rand_datetime(rng, three_years_ago, ANCHOR)
        paid = (
            iso(rand_datetime(rng, issued, min(issued + timedelta(days=20), ANCHOR)))
            if status == "paid"
            else None
        )
        invs.append((i + 1, cid, amount, status, iso(issued), paid))
    conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?)", invs)

    # ---- support tickets ----
    tickets = []
    for i in range(800):
        cid = rng.randint(1, 500)
        subject = rng.choice(TICKET_SUBJECTS)
        priority = rng.choices(TICKET_PRIORITIES, weights=TICKET_PRI_WEIGHTS)[0]
        status = rng.choices(TICKET_STATUSES, weights=TICKET_ST_WEIGHTS)[0]
        created = rand_datetime(rng, three_years_ago, ANCHOR)
        resolved = (
            iso(rand_datetime(rng, created, min(created + timedelta(days=14), ANCHOR)))
            if status in ("resolved", "closed")
            else None
        )
        tickets.append(
            (i + 1, cid, subject, priority, status, iso(created), resolved)
        )
    conn.executemany(
        "INSERT INTO support_tickets VALUES (?,?,?,?,?,?,?)", tickets
    )

    conn.commit()
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("customers", "subscriptions", "invoices", "support_tickets")
    }
    conn.close()
    print(f"Seeded {db_path}: " + ", ".join(f"{t}={n}" for t, n in counts.items()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parent.parent / "data" / "querylens.db"),
    )
    args = parser.parse_args()
    main(Path(args.db))
