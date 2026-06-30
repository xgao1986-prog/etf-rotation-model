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
    OpeningPosition,
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
