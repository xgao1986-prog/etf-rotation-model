#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_paper_trading_models.py — domain model validation tests"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading.models import (
    AccountCreate,
    AccountStatus,
    AccountType,
    OpeningPosition,
    StartMode,
    canonical_config,
    config_hash,
)


def test_config_hash_is_order_independent():
    left = {"weights": {"trend": 1, "confirm": 2}, "stop_loss": -0.08}
    right = {"stop_loss": -0.08, "weights": {"confirm": 2, "trend": 1}}
    assert canonical_config(left) == canonical_config(right)
    assert config_hash(left) == config_hash(right)


def test_cash_start_rejects_opening_positions():
    with pytest.raises(ValueError, match="cash start"):
        AccountCreate(
            account_id="acct-cash",
            name="B0.4 cash",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
            opening_positions=(
                OpeningPosition("512400.SH", 100, 1.0, 1.0),
            ),
        )


def test_import_start_requires_cash_and_valid_lots():
    with pytest.raises(ValueError, match="multiple of 100"):
        OpeningPosition("512400.SH", 150, 1.0, 1.0)

    with pytest.raises(ValueError, match="opening cash"):
        AccountCreate(
            account_id="acct-import",
            name="Imported",
            account_type=AccountType.SHADOW,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.IMPORTED,
            start_date="2026-06-29",
            opening_cash=-1,
        )


def test_account_status_values_are_stable():
    assert AccountStatus.READY.value == "READY"
    assert AccountStatus.RUNNING.value == "RUNNING"
    assert AccountStatus.PAUSED.value == "PAUSED"
    assert AccountStatus.ENDED.value == "ENDED"
    assert AccountStatus.ERROR.value == "ERROR"
