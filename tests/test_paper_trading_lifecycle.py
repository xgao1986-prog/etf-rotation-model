#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_paper_trading_lifecycle.py — account lifecycle (v0.3.1)

Covers:
- Schema v3 migration columns on paper_accounts.
- close / reopen accounts.
- hide / unhide accounts.
- soft delete / restore accounts.
- permanent deletion cascading to child tables.
- list_accounts filtering by hidden/deleted flags.
- runner rejects ended/deleted accounts.
- UI service run_accounts skips ended/deleted accounts.
- Pending orders cancelled on close/soft-delete.
- Permanent deletion requires matching account name + explicit confirmation.
- v2 -> v3 schema migration preserves historical data.
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading.models import AccountCreate, AccountStatus, AccountType, StartMode
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore
from paper_trading.ui_service import PaperTradingUIService


@pytest.fixture
def service():
    with tempfile.TemporaryDirectory() as d:
        yield PaperTradingService(PaperTradingStore(os.path.join(d, "paper.db")))


@pytest.fixture
def ui_service(service):
    runner = _FakeRunner()
    return PaperTradingUIService(service, runner)


class _FakeRunner:
    def run_daily(self, account_id, trade_date, open_prices, close_prices, scores_df):
        return {"cash": 0, "positions": {}, "nav": 0, "trades": [], "skipped": []}


def _create_account(service, account_id="acct-1", name="Test"):
    return service.create_account(
        AccountCreate(
            account_id=account_id,
            name=name,
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )


def _seed_order(store, account_id, order_id, status="PENDING", trade_date="2026-06-29"):
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_orders (order_id, dedupe_key, account_id, signal_date, trade_date, ticker,
                action, current_shares, target_shares, delta_shares, reference_price, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, f"{account_id}:{trade_date}:{order_id}:512400.SH:BUY", account_id,
                "2026-06-28", trade_date, "512400.SH", "BUY", 0, 100, 100,
                1.0, "test", status, "2026-06-28T16:00:00",
            ),
        )


def _seed_trade(store, account_id, trade_id, order_id=None, trade_date="2026-06-29"):
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (trade_id, dedupe_key, order_id, account_id, trade_date, ticker,
                action, shares, price, commission, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id, f"{account_id}:{trade_date}:{trade_id}:512400.SH:BUY", order_id, account_id,
                trade_date, "512400.SH", "BUY", 100, 1.0, 5.0, "SIMULATED", "2026-06-29T16:00:00",
            ),
        )


def test_schema_v3_columns_exist(service):
    """Fresh database must include lifecycle columns."""
    account = _create_account(service)
    assert "is_hidden" in account
    assert "is_deleted" in account
    assert "closed_at" in account
    assert "deleted_at" in account
    assert "lifecycle_reason" in account
    assert account["is_hidden"] == 0
    assert account["is_deleted"] == 0


def test_close_account(service):
    account = _create_account(service)
    closed = service.close_account(account["account_id"], reason="test close")
    assert closed["status"] == AccountStatus.ENDED.value
    assert closed["closed_at"] is not None
    assert closed["lifecycle_reason"] == "test close"


def test_close_already_ended_account_fails(service):
    account = _create_account(service)
    service.close_account(account["account_id"])
    with pytest.raises(ValueError, match="already ended"):
        service.close_account(account["account_id"])


def test_close_account_cancels_pending_orders(service):
    """结束账户必须将同一账户的所有 PENDING 订单改为 CANCELLED，原因 ACCOUNT_CLOSED。"""
    account = _create_account(service)
    _seed_order(service.store, account["account_id"], "order-1")
    _seed_order(service.store, account["account_id"], "order-2")
    _seed_order(service.store, account["account_id"], "order-filled", status="FILLED")

    service.close_account(account["account_id"])

    orders = service.store.list_orders(account["account_id"])
    by_id = {o["order_id"]: o for o in orders}
    assert by_id["order-1"]["status"] == "CANCELLED"
    assert by_id["order-1"]["reason"] == "ACCOUNT_CLOSED"
    assert by_id["order-2"]["status"] == "CANCELLED"
    assert by_id["order-2"]["reason"] == "ACCOUNT_CLOSED"
    assert by_id["order-filled"]["status"] == "FILLED"


def test_reopen_account(service):
    account = _create_account(service)
    service.close_account(account["account_id"])
    reopened = service.reopen_account(account["account_id"], reason="test reopen")
    assert reopened["status"] == AccountStatus.READY.value
    assert reopened["closed_at"] is None
    assert reopened["lifecycle_reason"] == "test reopen"


def test_reopen_non_ended_account_fails(service):
    account = _create_account(service)
    with pytest.raises(ValueError, match="not ended"):
        service.reopen_account(account["account_id"])


def test_hide_and_unhide_account(service):
    account = _create_account(service)
    hidden = service.hide_account(account["account_id"], reason="test hide")
    assert hidden["is_hidden"] == 1
    assert hidden["lifecycle_reason"] == "test hide"

    visible = service.unhide_account(account["account_id"], reason="test unhide")
    assert visible["is_hidden"] == 0
    assert visible["lifecycle_reason"] == "test unhide"


def test_soft_delete_and_restore_account(service):
    account = _create_account(service)
    deleted = service.soft_delete_account(account["account_id"], reason="test delete")
    assert deleted["is_deleted"] == 1
    assert deleted["status"] == AccountStatus.ENDED.value
    assert deleted["deleted_at"] is not None
    assert deleted["lifecycle_reason"] == "test delete"

    restored = service.restore_account(account["account_id"], reason="test restore")
    assert restored["is_deleted"] == 0
    assert restored["status"] == AccountStatus.READY.value
    assert restored["deleted_at"] is None


def test_soft_delete_cancels_pending_orders(service):
    """软删除账户必须将同一账户的所有 PENDING 订单改为 CANCELLED，原因 ACCOUNT_DELETED。"""
    account = _create_account(service)
    _seed_order(service.store, account["account_id"], "order-1")
    _seed_order(service.store, account["account_id"], "order-2")
    _seed_order(service.store, account["account_id"], "order-filled", status="FILLED")

    service.soft_delete_account(account["account_id"])

    orders = service.store.list_orders(account["account_id"])
    by_id = {o["order_id"]: o for o in orders}
    assert by_id["order-1"]["status"] == "CANCELLED"
    assert by_id["order-1"]["reason"] == "ACCOUNT_DELETED"
    assert by_id["order-2"]["status"] == "CANCELLED"
    assert by_id["order-2"]["reason"] == "ACCOUNT_DELETED"
    assert by_id["order-filled"]["status"] == "FILLED"


def test_restore_non_deleted_account_fails(service):
    account = _create_account(service)
    with pytest.raises(ValueError, match="not deleted"):
        service.restore_account(account["account_id"])


def test_permanent_delete_requires_soft_delete(service):
    account = _create_account(service)
    with pytest.raises(ValueError, match="soft deleted"):
        service.permanently_delete_account(account["account_id"], account_name=account["name"], confirmed=True)


def test_permanent_delete_requires_matching_name(service):
    account = _create_account(service)
    service.soft_delete_account(account["account_id"])
    with pytest.raises(ValueError, match="account name does not match"):
        service.permanently_delete_account(
            account["account_id"], account_name="Wrong Name", confirmed=True
        )


def test_permanent_delete_requires_confirmation(service):
    account = _create_account(service)
    service.soft_delete_account(account["account_id"])
    with pytest.raises(ValueError, match="must be explicitly confirmed"):
        service.permanently_delete_account(
            account["account_id"], account_name=account["name"], confirmed=False
        )


def test_permanent_delete_cascades_to_child_tables(service):
    """永久删除必须先删 trades（依赖 orders），再删 orders，最后删账户和其他子表。"""
    account = _create_account(service)
    store = service.store
    account_id = account["account_id"]

    # Seed child records, including a trade that references an order
    _seed_order(store, account_id, "order-1")
    _seed_trade(store, account_id, "trade-1", order_id="order-1")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_positions (account_id, as_of_date, ticker, shares, cost_price, last_price, market_value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, "2026-06-29", "512400.SH", 100, 1.0, 1.0, 100),
        )
        conn.execute(
            """
            INSERT INTO paper_daily_nav (account_id, nav_date, cash, positions_value, nav, data_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, "2026-06-30", 900_000, 100_000, 1_000_000, "2026-06-30", "2026-06-30T16:00:00"),
        )
        conn.execute(
            """
            INSERT INTO paper_runs (run_id, dedupe_key, account_id, run_date, task_type, status, data_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-1", f"{account_id}:2026-06-29:DAILY", account_id,
             "2026-06-29", "DAILY", "SUCCESS", "2026-06-29", "2026-06-29T16:00:00"),
        )
        conn.execute(
            """
            INSERT INTO paper_signals (signal_id, account_id, signal_date, trade_date, scores_json, config_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("signal-1", account_id, "2026-06-29", "2026-06-29", "[]", "{}", "2026-06-29T16:00:00"),
        )
        conn.execute(
            """
            INSERT INTO paper_skipped (skipped_id, account_id, trade_date, order_id, ticker, action, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("skip-1", account_id, "2026-06-29", None, "512400.SH", "BUY", "test", "2026-06-29T16:00:00"),
        )

    service.soft_delete_account(account_id)
    service.permanently_delete_account(account_id, account_name=account["name"], confirmed=True)

    assert store.get_account(account_id) is None
    assert store.list_positions(account_id, "2026-06-29") == []
    assert store.list_orders(account_id) == []
    assert store.list_trades(account_id) == []
    assert store.list_nav_history(account_id) == []
    assert store.list_skipped(account_id, "2026-06-29") == []
    with store.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM paper_runs WHERE account_id = ?", (account_id,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM paper_signals WHERE account_id = ?", (account_id,)
        ).fetchone() is None


def test_permanent_delete_rolls_back_on_failure(service):
    """永久删除失败时，所有变更必须回滚，账户和子表数据保持完整。"""
    account = _create_account(service)
    store = service.store
    account_id = account["account_id"]
    _seed_order(store, account_id, "order-1")
    _seed_trade(store, account_id, "trade-1", order_id="order-1")
    service.soft_delete_account(account_id)

    # 通过 monkeypatch 让第二次删除在中间步骤失败
    original_delete = store.permanently_delete_account

    def failing_delete(account_id):
        with store.connect() as conn:
            conn.execute("DELETE FROM paper_trades WHERE account_id = ?", (account_id,))
            raise RuntimeError("simulated failure")

    store.permanently_delete_account = failing_delete
    with pytest.raises(RuntimeError, match="simulated failure"):
        service.permanently_delete_account(account_id, account_name=account["name"], confirmed=True)
    store.permanently_delete_account = original_delete

    # 数据必须完整保留
    assert store.get_account(account_id) is not None
    assert len(store.list_trades(account_id)) == 1
    assert len(store.list_orders(account_id)) == 1


def test_v2_to_v3_migration_preserves_data(service):
    """模拟旧 v2 数据库，迁移到 v3 后历史数据完整且新增生命周期字段可用。"""
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "paper_v2.db")
        # 手工创建 v2 schema（无生命周期字段）
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE paper_accounts (
                account_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                group_id TEXT,
                strategy_name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                initial_capital REAL NOT NULL,
                start_mode TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE paper_positions (
                account_id TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                shares INTEGER NOT NULL,
                cost_price REAL NOT NULL,
                last_price REAL NOT NULL,
                market_value REAL NOT NULL,
                PRIMARY KEY (account_id, as_of_date, ticker)
            );
            CREATE TABLE paper_orders (
                order_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                current_shares INTEGER NOT NULL,
                target_shares INTEGER NOT NULL,
                delta_shares INTEGER NOT NULL,
                reference_price REAL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE paper_trades (
                trade_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                order_id TEXT,
                account_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                shares INTEGER NOT NULL,
                price REAL NOT NULL,
                commission REAL NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE paper_daily_nav (
                account_id TEXT NOT NULL,
                nav_date TEXT NOT NULL,
                cash REAL NOT NULL,
                positions_value REAL NOT NULL,
                nav REAL NOT NULL,
                data_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, nav_date)
            );
            CREATE TABLE paper_runs (
                run_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                run_date TEXT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                data_date TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE paper_schema_version (
                version INTEGER PRIMARY KEY,
                updated_at TEXT NOT NULL
            );
            INSERT INTO paper_schema_version (version, updated_at) VALUES (2, '2026-06-29T16:00:00');
            INSERT INTO paper_accounts (account_id, name, account_type, strategy_name, config_json, config_hash,
                initial_capital, start_mode, start_date, status, created_at, updated_at)
            VALUES ('old-acct', 'Old Account', 'COMPARISON', 'B0.4', '{}', 'hash', 1000000, 'CASH',
                    '2026-06-29', 'READY', '2026-06-29T16:00:00', '2026-06-29T16:00:00');
            INSERT INTO paper_daily_nav (account_id, nav_date, cash, positions_value, nav, data_date, created_at)
            VALUES ('old-acct', '2026-06-29', 1000000, 0, 1000000, '2026-06-29', '2026-06-29T16:00:00');
            """
        )
        conn.commit()
        conn.close()

        store = PaperTradingStore(db_path)
        account = store.get_account("old-acct")
        assert account is not None
        assert "is_hidden" in account
        assert "is_deleted" in account
        assert account["is_hidden"] == 0
        assert account["is_deleted"] == 0
        history = store.list_nav_history("old-acct")
        assert len(history) == 1
        assert history[0]["nav"] == 1_000_000


def test_list_accounts_filters_hidden_and_deleted(service):
    ready = _create_account(service, account_id="ready")
    hidden = _create_account(service, account_id="hidden")
    deleted = _create_account(service, account_id="deleted")

    service.hide_account(hidden["account_id"])
    service.soft_delete_account(deleted["account_id"])

    default = service.list_accounts()
    assert len(default) == 1
    assert default[0]["account_id"] == "ready"

    with_hidden = service.list_accounts(include_hidden=True)
    assert {a["account_id"] for a in with_hidden} == {"ready", "hidden"}

    with_deleted = service.list_accounts(include_deleted=True)
    assert {a["account_id"] for a in with_deleted} == {"ready", "deleted"}

    all_accounts = service.list_accounts(include_hidden=True, include_deleted=True)
    assert {a["account_id"] for a in all_accounts} == {"ready", "hidden", "deleted"}


def test_runner_rejects_ended_and_deleted_accounts(service):
    from paper_trading.runner import PaperTradingRunner

    runner = PaperTradingRunner(service)
    ended = _create_account(service, account_id="ended")
    deleted = _create_account(service, account_id="deleted")
    service.close_account(ended["account_id"])
    service.soft_delete_account(deleted["account_id"])

    with pytest.raises(ValueError, match="account is ended"):
        runner.run_daily(ended["account_id"], "2026-06-30", {}, {})

    with pytest.raises(ValueError, match="account is deleted"):
        runner.run_daily(deleted["account_id"], "2026-06-30", {}, {})


def test_ended_account_rejected_even_if_day_processed(service):
    """即使账户在当天已有运行记录，结束后再次运行也必须被拒绝（生命周期检查先于幂等检查）。"""
    from paper_trading.runner import PaperTradingRunner

    runner = PaperTradingRunner(service)
    account = _create_account(service, account_id="ended-processed")
    trade_date = "2026-06-30"

    # 先正常完成一天运行，使 is_day_processed 返回 True
    runner.run_daily(account["account_id"], trade_date, {}, {})
    assert service.store.is_day_processed(account["account_id"], trade_date)

    # 关闭账户
    service.close_account(account["account_id"])

    # 即使当天已有记录，结束后再次运行也必须拒绝
    with pytest.raises(ValueError, match="account is ended"):
        runner.run_daily(account["account_id"], trade_date, {}, {})


def test_restore_after_close_and_soft_delete_clears_timestamps(service):
    """账户先 close 再 soft delete，恢复后必须同时清空 closed_at 和 deleted_at。"""
    account = _create_account(service)
    service.close_account(account["account_id"], reason="close first")
    service.soft_delete_account(account["account_id"], reason="then delete")

    before = service.get_account(account["account_id"])
    assert before["status"] == AccountStatus.ENDED.value
    assert before["is_deleted"] == 1
    assert before["closed_at"] is not None
    assert before["deleted_at"] is not None

    restored = service.restore_account(account["account_id"], reason="full restore")
    assert restored["status"] == AccountStatus.READY.value
    assert restored["is_deleted"] == 0
    assert restored["closed_at"] is None
    assert restored["deleted_at"] is None


def test_ui_service_run_accounts_skips_ended_and_deleted(ui_service, service):
    active = _create_account(service, account_id="active")
    ended = _create_account(service, account_id="ended")
    deleted = _create_account(service, account_id="deleted")
    service.close_account(ended["account_id"])
    service.soft_delete_account(deleted["account_id"])

    import pandas as pd
    results = ui_service.run_accounts(
        [active["account_id"], ended["account_id"], deleted["account_id"], "missing"],
        trade_date="2026-06-29",
        open_prices={},
        close_prices={},
        scores_df=pd.DataFrame(),
    )

    assert active["account_id"] in results["success"]
    assert ended["account_id"] in results["skipped"]
    assert deleted["account_id"] in results["skipped"]
    assert "missing" in results["failure"]
