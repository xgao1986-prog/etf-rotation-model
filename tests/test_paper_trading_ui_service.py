#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_paper_trading_ui_service.py — batch account creation and UI workflows."""

import os
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from paper_trading.models import AccountType, OpeningPosition, StartMode
from paper_trading.runner import PaperTradingRunner
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore
from paper_trading.ui_service import PaperTradingUIService


B0_4_PRESET = {
    "weights": {
        "trend": 0.30, "confirm": 0.20, "momentum": 0.25,
        "volume": 0.15, "volatility": 0.10,
    },
    "min_trend_score": 15,
    "min_confirm_score": 4,
    "min_total_score": 40,
    "max_holdings": 5,
    "max_position_per_etf": 0.20,
    "stop_loss": -0.08,
}

CONSERVATIVE_PRESET = {
    "weights": {
        "trend": 0.40, "confirm": 0.30, "momentum": 0.15,
        "volume": 0.10, "volatility": 0.05,
    },
    "min_trend_score": 20,
    "min_confirm_score": 8,
    "min_total_score": 50,
    "max_holdings": 3,
    "max_position_per_etf": 0.10,
    "stop_loss": -0.05,
}


def _make_presets():
    return {
        "B0.4": B0_4_PRESET,
        "保守型": CONSERVATIVE_PRESET,
    }


@pytest.fixture
def ui_service():
    with tempfile.TemporaryDirectory() as d:
        store = PaperTradingStore(os.path.join(d, 'paper.db'))
        service = PaperTradingService(store)
        runner = PaperTradingRunner(service)
        yield PaperTradingUIService(service, runner)


def test_create_two_comparison_accounts_from_presets(ui_service):
    presets = _make_presets()
    ids = ui_service.create_comparison_accounts(
        preset_names=["B0.4", "保守型"],
        presets=presets,
        initial_capital=1_000_000,
        start_date="2026-06-29",
    )
    assert len(ids) == 2
    accounts = {a["account_id"]: a for a in ui_service.service.list_accounts()}
    assert all(i in accounts for i in ids)
    assert accounts[ids[0]]["account_type"] == AccountType.COMPARISON.value
    assert accounts[ids[1]]["account_type"] == AccountType.COMPARISON.value
    assert accounts[ids[0]]["initial_capital"] == 1_000_000
    assert accounts[ids[1]]["initial_capital"] == 1_000_000


def test_comparison_accounts_share_group_id(ui_service):
    presets = _make_presets()
    ids = ui_service.create_comparison_accounts(
        preset_names=["B0.4", "保守型"],
        presets=presets,
        initial_capital=1_000_000,
        start_date="2026-06-29",
    )
    accounts = {a["account_id"]: a for a in ui_service.service.list_accounts()}
    group_ids = {accounts[i]["group_id"] for i in ids}
    assert len(group_ids) == 1
    assert group_ids.pop() is not None


def test_empty_preset_selection_rejected(ui_service):
    with pytest.raises(ValueError, match="preset"):
        ui_service.create_comparison_accounts(
            preset_names=[],
            presets=_make_presets(),
            initial_capital=1_000_000,
            start_date="2026-06-29",
        )


def test_non_positive_initial_capital_rejected(ui_service):
    with pytest.raises(ValueError, match="capital"):
        ui_service.create_comparison_accounts(
            preset_names=["B0.4"],
            presets=_make_presets(),
            initial_capital=0,
            start_date="2026-06-29",
        )


def test_duplicate_account_prevented(ui_service):
    presets = _make_presets()
    ui_service.create_comparison_accounts(
        preset_names=["B0.4"],
        presets=presets,
        initial_capital=1_000_000,
        start_date="2026-06-29",
    )
    with pytest.raises(ValueError, match="exists"):
        ui_service.create_comparison_accounts(
            preset_names=["B0.4"],
            presets=presets,
            initial_capital=1_000_000,
            start_date="2026-06-29",
        )


def test_create_shadow_account_with_imported_positions(ui_service):
    presets = _make_presets()
    account_id = ui_service.create_shadow_account(
        name="我的实盘",
        preset_name="B0.4",
        preset=B0_4_PRESET,
        initial_capital=1_000_000,
        opening_cash=900_000,
        opening_positions=(OpeningPosition("512400.SH", 10_000, 10.0, 10.0),),
        start_date="2026-06-29",
    )
    account = ui_service.service.get_account(account_id)
    assert account["account_type"] == AccountType.SHADOW.value
    nav = ui_service.service.get_nav(account_id, "2026-06-29")
    assert nav["nav"] == 1_000_000


def test_shadow_account_nav_mismatch_rejected(ui_service):
    presets = _make_presets()
    with pytest.raises(ValueError, match="NAV"):
        ui_service.create_shadow_account(
            name="我的实盘",
            preset_name="B0.4",
            preset=B0_4_PRESET,
            initial_capital=1_000_000,
            opening_cash=900_000,
            opening_positions=(OpeningPosition("512400.SH", 5_000, 10.0, 10.0),),
            start_date="2026-06-29",
        )


def test_account_config_hash_unchanged_after_preset_edit(ui_service):
    presets = _make_presets()
    ids = ui_service.create_comparison_accounts(
        preset_names=["B0.4"],
        presets=presets,
        initial_capital=1_000_000,
        start_date="2026-06-29",
    )
    original_hash = ui_service.service.get_account(ids[0])["config_hash"]
    # Simulate editing the source preset
    presets["B0.4"]["max_holdings"] = 99
    # Account hash must stay the same
    assert ui_service.service.get_account(ids[0])["config_hash"] == original_hash


def test_list_account_summaries(ui_service):
    presets = _make_presets()
    ui_service.create_comparison_accounts(
        preset_names=["B0.4"],
        presets=presets,
        initial_capital=1_000_000,
        start_date="2026-06-29",
    )
    summaries = ui_service.list_account_summaries()
    assert len(summaries) == 1
    assert summaries[0]["account_type"] == AccountType.COMPARISON.value
    assert summaries[0]["initial_capital"] == 1_000_000


def test_run_accounts_isolates_failures(ui_service):
    presets = _make_presets()
    ids = ui_service.create_comparison_accounts(
        preset_names=["B0.4"],
        presets=presets,
        initial_capital=1_000_000,
        start_date="2026-06-29",
    )
    # Add a non-existent account id to the run list
    results = ui_service.run_accounts(
        account_ids=ids + ["does-not-exist"],
        trade_date="2026-06-30",
        open_prices={},
        close_prices={},
        scores_df=pd.DataFrame({"ticker": [], "total_score": []}),
    )
    assert len(results["success"]) == 1
    assert len(results["failure"]) == 1
    assert ids[0] in results["success"]
    assert "does-not-exist" in results["failure"]


def test_list_pending_shadow_orders(ui_service):
    presets = _make_presets()
    ui_service.create_shadow_account(
        name="Shadow",
        preset_name="B0.4",
        preset=B0_4_PRESET,
        initial_capital=1_000_000,
        opening_cash=1_000_000,
        opening_positions=(),
        start_date="2026-06-29",
    )
    # No orders yet
    pending = ui_service.list_pending_shadow_orders()
    assert pending == []


def test_batch_creation_rolls_back_on_failure(ui_service):
    """批量创建任一项失败时不得留下任何账户。"""
    presets = _make_presets()
    # 先创建第一个预设对应的账户
    ui_service.create_comparison_accounts(
        preset_names=["B0.4"],
        presets=presets,
        initial_capital=1_000_000,
        start_date="2026-06-29",
    )
    # 再次批量创建（包含已存在的 B0.4 和一个新预设）应整体失败并回滚
    with pytest.raises(ValueError, match="exists"):
        ui_service.create_comparison_accounts(
            preset_names=["B0.4", "保守型"],
            presets=presets,
            initial_capital=1_000_000,
            start_date="2026-06-29",
        )
    accounts = ui_service.service.list_accounts()
    # 只保留第一次创建的账户，第二次不应留下保守型
    assert len(accounts) == 1
    assert accounts[0]["strategy_name"] == "B0.4"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
