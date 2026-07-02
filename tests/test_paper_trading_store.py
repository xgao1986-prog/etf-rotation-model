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


def test_duplicate_trade_is_rejected(store):
    seed_account(store, "acct-trade")
    trade = {
        "trade_id": "trade-1",
        "dedupe_key": "acct-trade:2026-07-03:512400.SH:BUY",
        "order_id": None,
        "account_id": "acct-trade",
        "trade_date": "2026-07-03",
        "ticker": "512400.SH",
        "action": "BUY",
        "shares": 100,
        "price": 1.0,
        "commission": 5.0,
        "source": "SIMULATED",
        "created_at": "2026-07-03T09:30:00",
    }
    store.append_trade(trade)
    duplicate = dict(trade, trade_id="trade-2")
    with pytest.raises(DuplicateLedgerEvent):
        store.append_trade(duplicate)


# ============== Shadow-Order Lifecycle ==============


def _seed_shadow_account(store, account_id="shadow-1"):
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
                account_id, "Shadow", "SHADOW", None, "B0.4",
                "{}", "seed-hash", 1_000_000, "IMPORTED",
                "2026-06-29", None, "READY",
                "2026-06-29T16:00:00", "2026-06-29T16:00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_daily_nav (account_id, nav_date, cash, positions_value, nav, data_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, "2026-06-29", 900_000, 100_000, 1_000_000, "2026-06-29", "2026-06-29T16:00:00"),
        )
        conn.execute(
            """
            INSERT INTO paper_positions (account_id, as_of_date, ticker, shares, cost_price, last_price, market_value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, "2026-06-29", "512400.SH", 10_000, 10.0, 10.0, 100_000),
        )


def _seed_order(store, account_id="shadow-1", order_id="order-1", status="PENDING", trade_date="2026-07-03"):
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_orders (
                order_id, dedupe_key, account_id, signal_date, trade_date, ticker,
                action, current_shares, target_shares, delta_shares, reference_price,
                reason, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, f"{account_id}:{trade_date}:{order_id}:512400.SH:BUY", account_id,
                "2026-07-02", trade_date, "512400.SH", "BUY", 0, 100, 100,
                1.0, "B0.4 selected", status, "2026-07-02T16:00:00",
            ),
        )


def test_order_status_updates(store):
    _seed_shadow_account(store)
    _seed_order(store)
    store.update_order_status("order-1", "FILLED")
    order = store.get_order("order-1")
    assert order["status"] == "FILLED"


def test_order_status_rejected_cancelled_expired(store):
    _seed_shadow_account(store)
    for status in ("REJECTED", "CANCELLED", "EXPIRED"):
        order_id = f"order-{status.lower()}"
        _seed_order(store, order_id=order_id, status="PENDING")
        store.update_order_status(order_id, status, reason=f"test {status}")
        order = store.get_order(order_id)
        assert order["status"] == status


def test_unconfirmed_order_does_not_change_cash_or_positions(store):
    _seed_shadow_account(store)
    _seed_order(store)
    nav = store.get_nav("shadow-1", "2026-06-29")
    positions = store.list_positions("shadow-1", "2026-06-29")
    assert nav["cash"] == 900_000
    assert len(positions) == 1
    assert positions[0]["shares"] == 10_000


def test_list_orders_by_status(store):
    _seed_shadow_account(store)
    _seed_order(store, order_id="order-pending")
    _seed_order(store, order_id="order-filled", status="FILLED")
    pending = store.list_orders("shadow-1", status="PENDING")
    assert len(pending) == 1
    assert pending[0]["order_id"] == "order-pending"


def test_apply_manual_fill_updates_order_trade_position_nav(store):
    _seed_shadow_account(store)
    _seed_order(store)
    store.apply_manual_fill(
        fill_row={
            "order_id": "order-1",
            "status": "FILLED",
            "filled_at": "2026-07-03T10:00:00",
        },
        trade_row={
            "trade_id": "trade-1",
            "dedupe_key": "shadow-1:2026-07-03:512400.SH:BUY",
            "order_id": "order-1",
            "account_id": "shadow-1",
            "trade_date": "2026-07-03",
            "ticker": "512400.SH",
            "action": "BUY",
            "shares": 100,
            "price": 1.05,
            "commission": 5.0,
            "source": "MANUAL",
            "created_at": "2026-07-03T10:00:00",
        },
        position_rows=[{
            "account_id": "shadow-1",
            "as_of_date": "2026-07-03",
            "ticker": "512400.SH",
            "shares": 10_100,
            "cost_price": 10.0,
            "last_price": 1.05,
            "market_value": 10_605,
        }],
        nav_row={
            "account_id": "shadow-1",
            "nav_date": "2026-07-03",
            "cash": 899_890,
            "positions_value": 110_605,
            "nav": 1_010_495,
            "data_date": "2026-07-03",
            "created_at": "2026-07-03T16:00:00",
        },
    )
    order = store.get_order("order-1")
    assert order["status"] == "FILLED"
    trades = store.list_trades("shadow-1", "2026-07-03")
    assert len(trades) == 1
    nav = store.get_nav("shadow-1", "2026-07-03")
    assert nav["cash"] == 899_890


def test_expire_pending_orders_before_date(store):
    _seed_shadow_account(store)
    _seed_order(store, order_id="order-old", trade_date="2026-07-02")
    _seed_order(store, order_id="order-today", trade_date="2026-07-03")
    store.expire_pending_orders("shadow-1", before_trade_date="2026-07-03")
    assert store.get_order("order-old")["status"] == "EXPIRED"
    assert store.get_order("order-today")["status"] == "PENDING"


def test_list_nav_history(store):
    _seed_shadow_account(store)
    history = store.list_nav_history("shadow-1")
    assert len(history) == 1
    assert history[0]["nav_date"] == "2026-06-29"
