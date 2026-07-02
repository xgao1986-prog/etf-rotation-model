#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_paper_trading_service.py — account lifecycle and reconciliation tests"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading.models import (
    AccountCreate,
    AccountType,
    ManualFill,
    OpeningPosition,
    OrderStatus,
    StartMode,
)
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore


@pytest.fixture
def service():
    with tempfile.TemporaryDirectory() as d:
        yield PaperTradingService(
            PaperTradingStore(os.path.join(d, "paper.db"))
        )


def test_create_cash_account_writes_opening_nav(service):
    created = service.create_account(
        AccountCreate(
            account_id="acct-cash",
            name="B0.4",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    assert created["status"] == "READY"
    nav = service.get_nav("acct-cash", "2026-06-29")
    assert nav["cash"] == 1_000_000
    assert nav["positions_value"] == 0
    assert nav["nav"] == 1_000_000


def test_imported_account_requires_nav_identity(service):
    with pytest.raises(ValueError, match="opening NAV mismatch"):
        service.create_account(
            AccountCreate(
                account_id="acct-bad",
                name="Bad import",
                account_type=AccountType.SHADOW,
                strategy_name="B0.4",
                strategy_config={"max_holdings": 5},
                initial_capital=1_000_000,
                start_mode=StartMode.IMPORTED,
                start_date="2026-06-29",
                opening_cash=900_000,
                opening_positions=(
                    OpeningPosition("512400.SH", 100, 10.0, 10.0),
                ),
            )
        )


def test_imported_account_writes_opening_values(service):
    service.create_account(
        AccountCreate(
            account_id="acct-imported",
            name="Imported",
            account_type=AccountType.SHADOW,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.IMPORTED,
            start_date="2026-06-29",
            opening_cash=900_000,
            opening_positions=(
                OpeningPosition("512400.SH", 10_000, 10.0, 10.0),
            ),
        )
    )
    nav = service.get_nav("acct-imported", "2026-06-29")
    assert nav["cash"] == 900_000
    assert nav["positions_value"] == 100_000
    assert nav["nav"] == 1_000_000


def test_account_config_is_immutable(service):
    service.create_account(
        AccountCreate(
            account_id="acct-fixed",
            name="Fixed",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    with pytest.raises(RuntimeError, match="immutable"):
        service.replace_config("acct-fixed", {"max_holdings": 4})


def test_append_same_order_twice_is_rejected(service):
    service.create_account(
        AccountCreate(
            account_id="acct-order",
            name="Order account",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    order = {
        "order_id": "order-1",
        "dedupe_key": "acct-order:2026-07-02:512400.SH:BUY",
        "account_id": "acct-order",
        "signal_date": "2026-07-02",
        "trade_date": "2026-07-03",
        "ticker": "512400.SH",
        "action": "BUY",
        "current_shares": 0,
        "target_shares": 100,
        "delta_shares": 100,
        "reference_price": 1.0,
        "reason": "B0.4 selected",
        "status": "PENDING",
    }
    service.append_order(order)
    with pytest.raises(RuntimeError, match="duplicate ledger event"):
        service.append_order(order)


def test_reconcile_detects_bad_nav(service):
    service.create_account(
        AccountCreate(
            account_id="acct-reconcile",
            name="Reconcile",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    assert service.reconcile("acct-reconcile", "2026-06-29")["ok"]
    service.store.insert_nav(
        {
            "account_id": "acct-reconcile",
            "nav_date": "2026-06-30",
            "cash": 900_000,
            "positions_value": 50_000,
            "nav": 1_000_000,
            "data_date": "2026-06-30",
            "created_at": "2026-06-30T16:00:00",
        }
    )
    result = service.reconcile("acct-reconcile", "2026-06-30")
    assert not result["ok"]
    assert result["difference"] == -50_000


# ============== Shadow-Order Service Lifecycle ==============


def _seed_shadow_order(service):
    service.create_account(
        AccountCreate(
            account_id="shadow-1",
            name="Shadow",
            account_type=AccountType.SHADOW,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.IMPORTED,
            start_date="2026-06-29",
            opening_cash=900_000,
            opening_positions=(OpeningPosition("512400.SH", 10_000, 10.0, 10.0),),
        )
    )
    order = {
        "order_id": "order-1",
        "dedupe_key": "shadow-1:2026-07-03:512400.SH:BUY",
        "account_id": "shadow-1",
        "signal_date": "2026-07-02",
        "trade_date": "2026-07-03",
        "ticker": "512400.SH",
        "action": "BUY",
        "current_shares": 0,
        "target_shares": 100,
        "delta_shares": 100,
        "reference_price": 1.0,
        "reason": "B0.4 selected",
        "status": OrderStatus.PENDING.value,
        "created_at": "2026-07-02T16:00:00",
    }
    service.append_order(order)
    return order


def test_confirm_shadow_order_fills_trade_updates_state(service):
    _seed_shadow_order(service)
    fill = ManualFill(
        account_id="shadow-1",
        order_id="order-1",
        trade_date="2026-07-03",
        actual_price=1.05,
        actual_shares=100,
    )
    service.confirm_shadow_order(fill)
    order = service.store.get_order("order-1")
    assert order["status"] == OrderStatus.FILLED.value
    nav = service.store.get_nav("shadow-1", "2026-07-03")
    assert nav is not None
    positions = service.store.list_positions("shadow-1", "2026-07-03")
    assert len(positions) == 1
    assert positions[0]["shares"] == 10_100


def test_confirm_shadow_order_requires_shadow_account(service):
    service.create_account(
        AccountCreate(
            account_id="compare-1",
            name="Compare",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    with pytest.raises(ValueError, match="shadow"):
        service.confirm_shadow_order(
            ManualFill("compare-1", "order-1", "2026-07-03", 1.0, 100)
        )


def test_confirm_requires_pending_order(service):
    _seed_shadow_order(service)
    service.store.update_order_status("order-1", OrderStatus.CANCELLED.value)
    with pytest.raises(ValueError, match="PENDING"):
        service.confirm_shadow_order(
            ManualFill("shadow-1", "order-1", "2026-07-03", 1.0, 100)
        )


def test_confirm_rejects_wrong_account(service):
    _seed_shadow_order(service)
    service.create_account(
        AccountCreate(
            account_id="shadow-2",
            name="Shadow 2",
            account_type=AccountType.SHADOW,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    with pytest.raises(ValueError, match="account"):
        service.confirm_shadow_order(
            ManualFill("shadow-2", "order-1", "2026-07-03", 1.0, 100)
        )


def test_confirm_rejects_non_100_share_quantity(service):
    _seed_shadow_order(service)
    with pytest.raises(ValueError, match="100"):
        service.confirm_shadow_order(
            ManualFill("shadow-1", "order-1", "2026-07-03", 1.0, 150)
        )


def test_confirm_rejects_insufficient_cash(service):
    _seed_shadow_order(service)
    with pytest.raises(ValueError, match="cash"):
        service.confirm_shadow_order(
            ManualFill("shadow-1", "order-1", "2026-07-03", 1.0, 1_000_000_000)
        )


def test_confirm_rejects_oversell(service):
    _seed_shadow_order(service)
    service.store.update_order_status("order-1", OrderStatus.PENDING.value)
    # Replace order with SELL for 20_000 shares (more than available 10_000)
    with service.store.connect() as conn:
        conn.execute(
            "DELETE FROM paper_orders WHERE order_id = ?",
            ("order-1",),
        )
        conn.execute(
            """
            INSERT INTO paper_orders (
                order_id, dedupe_key, account_id, signal_date, trade_date, ticker,
                action, current_shares, target_shares, delta_shares, reference_price,
                reason, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "order-1", "shadow-1:2026-07-03:512400.SH:SELL", "shadow-1",
                "2026-07-02", "2026-07-03", "512400.SH", "SELL", 10_000, 0,
                -20_000, 1.0, "B0.4 exit", OrderStatus.PENDING.value,
                "2026-07-02T16:00:00",
            ),
        )
    with pytest.raises(ValueError, match="oversell"):
        service.confirm_shadow_order(
            ManualFill("shadow-1", "order-1", "2026-07-03", 1.0, 20_000)
        )


def test_reject_shadow_order(service):
    _seed_shadow_order(service)
    service.reject_shadow_order("shadow-1", "order-1", "manual skip")
    order = service.store.get_order("order-1")
    assert order["status"] == OrderStatus.REJECTED.value
    assert "manual skip" in order.get("reason", "")


def test_cancel_shadow_order(service):
    _seed_shadow_order(service)
    service.cancel_shadow_order("shadow-1", "order-1", "user cancelled")
    order = service.store.get_order("order-1")
    assert order["status"] == OrderStatus.CANCELLED.value


def test_expired_shadow_orders(service):
    _seed_shadow_order(service)
    service.expire_shadow_orders("shadow-1", "2026-07-04")
    order = service.store.get_order("order-1")
    assert order["status"] == OrderStatus.EXPIRED.value


def test_repeated_confirmation_is_rejected(service):
    _seed_shadow_order(service)
    fill = ManualFill("shadow-1", "order-1", "2026-07-03", 1.05, 100)
    service.confirm_shadow_order(fill)
    with pytest.raises(ValueError, match="PENDING"):
        service.confirm_shadow_order(fill)


def test_confirm_two_shadow_orders_same_day_accumulates(service):
    """同一天确认两笔影子买入，现金、持仓、总资产和成本必须累计。"""
    service.create_account(
        AccountCreate(
            account_id="shadow-1",
            name="Shadow",
            account_type=AccountType.SHADOW,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    for i, ticker in enumerate(("512400.SH", "515230.SH")):
        order = {
            "order_id": f"order-{i}",
            "dedupe_key": f"shadow-1:2026-07-03:{ticker}:BUY:{i}",
            "account_id": "shadow-1",
            "signal_date": "2026-07-02",
            "trade_date": "2026-07-03",
            "ticker": ticker,
            "action": "BUY",
            "current_shares": 0,
            "target_shares": 100,
            "delta_shares": 100,
            "reference_price": 1.0,
            "reason": "B0.4 selected",
            "status": OrderStatus.PENDING.value,
            "created_at": "2026-07-02T16:00:00",
        }
        service.append_order(order)

    service.confirm_shadow_order(ManualFill("shadow-1", "order-0", "2026-07-03", 1.0, 100))
    service.confirm_shadow_order(ManualFill("shadow-1", "order-1", "2026-07-03", 2.0, 100))

    nav = service.store.get_nav("shadow-1", "2026-07-03")
    commission = max(100 * 1.0 * 0.0003, 5) + max(100 * 2.0 * 0.0003, 5)
    assert nav["cash"] == pytest.approx(1_000_000 - 100 * 1.0 - 100 * 2.0 - commission, abs=0.01)
    assert nav["positions_value"] == pytest.approx(100 * 1.0 + 100 * 2.0, abs=0.01)
    positions = {p["ticker"]: p for p in service.store.list_positions("shadow-1", "2026-07-03")}
    assert positions["512400.SH"]["shares"] == 100
    assert positions["515230.SH"]["shares"] == 100


def test_confirm_buy_updates_average_cost(service):
    """对已有 ETF 继续买入后，平均持仓成本必须重新计算。"""
    service.create_account(
        AccountCreate(
            account_id="shadow-1",
            name="Shadow",
            account_type=AccountType.SHADOW,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.IMPORTED,
            start_date="2026-06-29",
            opening_cash=900_000,
            opening_positions=(OpeningPosition("512400.SH", 10_000, 10.0, 10.0),),
        )
    )
    order = {
        "order_id": "order-1",
        "dedupe_key": "shadow-1:2026-07-03:512400.SH:BUY",
        "account_id": "shadow-1",
        "signal_date": "2026-07-02",
        "trade_date": "2026-07-03",
        "ticker": "512400.SH",
        "action": "BUY",
        "current_shares": 10_000,
        "target_shares": 10_100,
        "delta_shares": 100,
        "reference_price": 1.0,
        "reason": "B0.4 selected",
        "status": OrderStatus.PENDING.value,
        "created_at": "2026-07-02T16:00:00",
    }
    service.append_order(order)
    service.confirm_shadow_order(ManualFill("shadow-1", "order-1", "2026-07-03", 1.0, 100))
    positions = {p["ticker"]: p for p in service.store.list_positions("shadow-1", "2026-07-03")}
    pos = positions["512400.SH"]
    expected_cost = (10_000 * 10.0 + 100 * 1.0) / 10_100
    assert pos["cost_price"] == pytest.approx(expected_cost, abs=0.0001)


def test_reject_requires_reason(service):
    _seed_shadow_order(service)
    with pytest.raises(ValueError, match="reason"):
        service.reject_shadow_order("shadow-1", "order-1", "")


def test_cancel_requires_reason(service):
    _seed_shadow_order(service)
    with pytest.raises(ValueError, match="reason"):
        service.cancel_shadow_order("shadow-1", "order-1", "   ")
