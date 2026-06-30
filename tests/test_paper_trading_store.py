#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_paper_trading_store.py — schema, persistence, and duplicate protection tests"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading.store import DuplicateLedgerEvent, PaperTradingStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield PaperTradingStore(os.path.join(d, "paper.db"))


def seed_account(store, account_id="acct-1"):
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_accounts (
                account_id, name, account_type, group_id, strategy_name,
                config_json, config_hash, initial_capital, start_mode,
                start_date, end_date, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, "Seed", "COMPARISON", None, "B0.4",
                "{}", "seed-hash", 1_000_000, "CASH",
                "2026-06-29", None, "READY",
                "2026-06-29T16:00:00", "2026-06-29T16:00:00",
            ),
        )


def test_schema_contains_all_ledger_tables(store):
    with store.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "paper_accounts",
        "paper_positions",
        "paper_orders",
        "paper_trades",
        "paper_daily_nav",
        "paper_runs",
    }.issubset(names)


def test_duplicate_dedupe_key_is_rejected(store):
    seed_account(store)
    store.append_run(
        run_id="run-1",
        dedupe_key="acct-1:2026-06-29:DAILY",
        account_id="acct-1",
        run_date="2026-06-29",
        task_type="DAILY",
        status="SUCCESS",
        data_date="2026-06-29",
        error_message=None,
    )
    with pytest.raises(DuplicateLedgerEvent):
        store.append_run(
            run_id="run-2",
            dedupe_key="acct-1:2026-06-29:DAILY",
            account_id="acct-1",
            run_date="2026-06-29",
            task_type="DAILY",
            status="SUCCESS",
            data_date="2026-06-29",
            error_message=None,
        )


def test_config_snapshot_cannot_be_updated(store):
    seed_account(store)
    with pytest.raises(sqlite3.IntegrityError, match="configuration is immutable"):
        with store.connect() as conn:
            conn.execute(
                """
                UPDATE paper_accounts
                SET config_json = ?, config_hash = ?
                WHERE account_id = ?
                """,
                ('{"max_holdings":4}', "changed", "acct-1"),
            )
