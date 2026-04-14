"""
priors.py — Persistent Bayesian priors per project

Stores term definitions confirmed by the developer.
Each subsequent task consults this DB first — no need to re-verify known terms.
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path


EPISTEMIX_DIR = ".epistemix"
DB_NAME = "priors.db"


def get_db_path(project_root: Path) -> Path:
    return project_root / EPISTEMIX_DIR / DB_NAME


def init_db(project_root: Path) -> sqlite3.Connection:
    db_path = get_db_path(project_root)
    db_path.parent.mkdir(exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS priors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT NOT NULL,
            definition TEXT NOT NULL,
            source TEXT,               -- where in code this was found
            confidence REAL DEFAULT 1.0,
            confirmed_at TEXT NOT NULL,
            confirmed_by TEXT DEFAULT 'human',
            task_context TEXT          -- which task triggered this
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_term ON priors(term);

        CREATE TABLE IF NOT EXISTS calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            predicted_confidence REAL,
            actual_success INTEGER,    -- 1 = task succeeded, 0 = failed
            recorded_at TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def get_prior(conn: sqlite3.Connection, term: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM priors WHERE term = ? COLLATE NOCASE",
        (term,)
    ).fetchone()
    return dict(row) if row else None


def save_prior(
    conn: sqlite3.Connection,
    term: str,
    definition: str,
    source: str = "",
    confidence: float = 1.0,
    task_context: str = "",
) -> None:
    conn.execute("""
        INSERT INTO priors (term, definition, source, confidence, confirmed_at, task_context)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(term) DO UPDATE SET
            definition = excluded.definition,
            source = excluded.source,
            confidence = excluded.confidence,
            confirmed_at = excluded.confirmed_at,
            task_context = excluded.task_context
    """, (term, definition, source, confidence, datetime.now().isoformat(), task_context))
    conn.commit()


def get_all_priors(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM priors ORDER BY confirmed_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_priors_for_terms(conn: sqlite3.Connection, terms: set[str]) -> list[dict]:
    """Get existing priors that match any of the given task terms."""
    if not terms:
        return []
    placeholders = ",".join("?" * len(terms))
    rows = conn.execute(
        f"SELECT * FROM priors WHERE term COLLATE NOCASE IN ({placeholders})",
        list(terms)
    ).fetchall()
    return [dict(r) for r in rows]


def record_calibration(
    conn: sqlite3.Connection,
    task: str,
    predicted_confidence: float,
    actual_success: bool,
) -> None:
    conn.execute("""
        INSERT INTO calibration (task, predicted_confidence, actual_success, recorded_at)
        VALUES (?, ?, ?, ?)
    """, (task, predicted_confidence, int(actual_success), datetime.now().isoformat()))
    conn.commit()


def calibration_report(conn: sqlite3.Connection) -> dict:
    """
    Compute Expected Calibration Error and basic stats.
    Groups predictions into buckets and compares to actual success rate.
    """
    rows = conn.execute(
        "SELECT predicted_confidence, actual_success FROM calibration"
    ).fetchall()

    if not rows:
        return {"error": "No calibration data yet. Run some tasks first."}

    buckets: dict[str, list] = {}
    for row in rows:
        confidence = min(row['predicted_confidence'], 0.9999)
        bucket = f"{int(confidence * 10) * 10}-{int(confidence * 10) * 10 + 10}%"
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(row["actual_success"])

    ece = 0.0
    n_total = len(rows)
    bucket_stats = {}

    for bucket, outcomes in buckets.items():
        lo = int(bucket.split("-")[0]) / 100
        predicted = lo + 0.05  # bucket midpoint
        actual = sum(outcomes) / len(outcomes)
        weight = len(outcomes) / n_total
        ece += weight * abs(predicted - actual)
        bucket_stats[bucket] = {
            "predicted": round(predicted, 2),
            "actual": round(actual, 2),
            "n": len(outcomes),
            "gap": round(actual - predicted, 2),
        }

    return {
        "ece": round(ece, 3),
        "n_tasks": n_total,
        "buckets": bucket_stats,
        "interpretation": (
            "Well calibrated" if ece < 0.1 else
            "Slightly overconfident" if ece < 0.2 else
            "Significantly miscalibrated — tighten verification thresholds"
        ),
    }
