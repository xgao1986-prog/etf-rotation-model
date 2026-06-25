#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_live_trading.py

实盘交互模块 v0.1 测试
覆盖：持仓读取、校验、价格更新、止损检查、订单生成、成交记录
"""

import os, sys, tempfile, pytest
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from live_trading_assistant import (
    LiveTradingAssistant, ActualTrade, ValidationReport, CASH_TICKER
)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def assistant(temp_dir):
    return LiveTradingAssistant(
        positions_path=os.path.join(temp_dir, "actual_positions.csv"),
        trades_path=os.path.join(temp_dir, "actual_trades.csv"),
        plan_path=os.path.join(temp_dir, "latest_trade_plan.csv"),
        config={"max_total_position": 1.0, "stop_loss": 0.08},
    )


@pytest.fixture
def sample_positions(assistant):
    """创建示例持仓（使用 B0-18 池内真实 ticker）"""
    rows = [
        {"ticker": "512400.SH", "name": "信息技术", "shares": 200,
         "cost_price": 0.636, "current_price": 0.650, "market_value": 130.0,
         "available_cash": 0, "update_time": "2026-06-26"},
        {"ticker": "515230.SH", "name": "软件ETF", "shares": 300,
         "cost_price": 1.200, "current_price": 1.150, "market_value": 345.0,
         "available_cash": 0, "update_time": "2026-06-26"},
        {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
         "cost_price": 0, "current_price": 0, "market_value": 150000.0,
         "available_cash": 150475.0, "update_time": "2026-06-26"},
    ]
    df = pd.DataFrame(rows)
    assistant.save_positions(df)
    return df


class TestLoadPositions:
    """持仓读取测试"""

    def test_load_empty(self, assistant):
        df = assistant.load_positions()
        assert df.empty

    def test_load_sample(self, assistant, sample_positions):
        df = assistant.load_positions()
        assert len(df) == 3
        assert "512400.SH" in df["ticker"].values
        assert CASH_TICKER in df["ticker"].values


class TestValidation:
    """持仓校验测试"""

    def test_nav_identity(self, assistant, sample_positions):
        """NAV恒等式：现金 + 市值 = 总资产"""
        report = assistant.validate_positions()
        # 130 + 345 + 150000 = 150475
        assert report.ok, f"NAV校验失败: {report.errors}"

    def test_invalid_ticker(self, assistant):
        """模型外ETF应报错"""
        rows = [
            {"ticker": "999999.SH", "name": "假ETF", "shares": 100,
             "cost_price": 1.0, "current_price": 1.0, "market_value": 100.0,
             "available_cash": 0, "update_time": "2026-06-26"},
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 1000.0,
             "available_cash": 1100.0, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))
        report = assistant.validate_positions()
        assert not report.ok
        assert any("模型外持仓" in e for e in report.errors)

    def test_non_100_shares(self, assistant):
        """非100股整数应警告"""
        rows = [
            {"ticker": "512400.SH", "name": "信息技术", "shares": 150,
             "cost_price": 0.636, "current_price": 0.650, "market_value": 97.5,
             "available_cash": 0, "update_time": "2026-06-26"},
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 1000.0,
             "available_cash": 1097.5, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))
        report = assistant.validate_positions()
        assert any("非100股整数" in w for w in report.warnings)

    def test_missing_price(self, assistant):
        """缺价应报错"""
        rows = [
            {"ticker": "512400.SH", "name": "信息技术", "shares": 200,
             "cost_price": 0.636, "current_price": 0, "market_value": 0,
             "available_cash": 0, "update_time": "2026-06-26"},
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 1000.0,
             "available_cash": 1000.0, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))
        report = assistant.validate_positions()
        assert not report.ok
        assert any("缺价" in e for e in report.errors)

    def test_nav_mismatch(self, assistant):
        """NAV不恒等应报错"""
        rows = [
            {"ticker": "512400.SH", "name": "信息技术", "shares": 200,
             "cost_price": 0.636, "current_price": 0.650, "market_value": 130.0,
             "available_cash": 0, "update_time": "2026-06-26"},
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 150000.0,
             "available_cash": 200000.0, "update_time": "2026-06-26"},  # 不匹配
        ]
        assistant.save_positions(pd.DataFrame(rows))
        report = assistant.validate_positions()
        assert not report.ok
        assert any("NAV恒等式不成立" in e for e in report.errors)


class TestPriceUpdate:
    """价格更新测试"""

    def test_update_prices(self, assistant, sample_positions):
        price_map = {"512400.SH": 0.700, "512760.SH": 1.100}
        df = assistant.update_prices(price_map, "2026-06-27")
        row_400 = df[df["ticker"] == "512400.SH"].iloc[0]
        assert row_400["current_price"] == 0.700
        assert row_400["market_value"] == 200 * 0.700
        assert row_400["update_time"] == "2026-06-27"


class TestStopLoss:
    """止损检查测试"""

    def test_no_alert(self, assistant, sample_positions):
        """未触发止损"""
        alerts = assistant.check_stop_loss()
        assert alerts.empty

    def test_alert_triggered(self, assistant):
        """触发止损"""
        rows = [
            {"ticker": "512400.SH", "name": "信息技术", "shares": 200,
             "cost_price": 1.000, "current_price": 0.900, "market_value": 180.0,
             "available_cash": 0, "update_time": "2026-06-26"},
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 1000.0,
             "available_cash": 1180.0, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))
        alerts = assistant.check_stop_loss()
        assert len(alerts) == 1
        assert alerts.iloc[0]["ticker"] == "512400.SH"
        assert alerts.iloc[0]["loss_pct"] == pytest.approx(-0.10, abs=0.001)  # 跌破 8% 止损线


class TestTradePlan:
    """订单生成测试"""

    def test_generate_buy(self, assistant, sample_positions):
        """目标持仓 > 实际持仓，生成 BUY"""
        target = {"512400.SH": 300, "515230.SH": 300}
        price_map = {"512400.SH": 0.650, "515230.SH": 1.150}
        plan = assistant.generate_trade_plan(target, price_map, "2026-06-26")
        buy_df = plan[plan["action"] == "BUY"]
        assert len(buy_df) == 1
        assert buy_df.iloc[0]["ticker"] == "512400.SH"
        assert buy_df.iloc[0]["delta_shares"] == 100

    def test_generate_sell(self, assistant, sample_positions):
        """目标持仓 < 实际持仓，生成 SELL"""
        target = {"512400.SH": 200, "515230.SH": 100}
        price_map = {"512400.SH": 0.650, "515230.SH": 1.150}
        plan = assistant.generate_trade_plan(target, price_map, "2026-06-26")
        sell_df = plan[plan["action"] == "SELL"]
        assert len(sell_df) == 1
        assert sell_df.iloc[0]["ticker"] == "515230.SH"
        assert sell_df.iloc[0]["delta_shares"] == -200

    def test_generate_hold(self, assistant, sample_positions):
        """目标持仓 = 实际持仓，生成 HOLD"""
        target = {"512400.SH": 200, "515230.SH": 300}
        price_map = {"512400.SH": 0.650, "515230.SH": 1.150}
        plan = assistant.generate_trade_plan(target, price_map, "2026-06-26")
        hold_df = plan[plan["action"] == "HOLD"]
        assert len(hold_df) == 2

    def test_cash_insufficient(self, assistant):
        """现金不足时应标记"""
        rows = [
            {"ticker": "512400.SH", "name": "信息技术", "shares": 0,
             "cost_price": 0.636, "current_price": 0.650, "market_value": 0,
             "available_cash": 0, "update_time": "2026-06-26"},
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 10.0,
             "available_cash": 10.0, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))
        target = {"512400.SH": 1000}
        price_map = {"512400.SH": 0.650}
        plan = assistant.generate_trade_plan(target, price_map, "2026-06-26")
        buy_df = plan[plan["action"] == "BUY"]
        assert len(buy_df) == 1
        assert "现金不足" in buy_df.iloc[0]["reason"]


class TestRecordTrade:
    """成交记录测试"""

    def test_record_trade(self, assistant, sample_positions):
        trade = ActualTrade(
            date="2026-06-26", ticker="512400.SH", action="BUY",
            shares=100, actual_price=0.650, commission=0.2, note="测试"
        )
        assistant.record_trade(trade)
        trades_df = pd.read_csv(assistant.trades_path)
        assert len(trades_df) == 1
        assert trades_df.iloc[0]["ticker"] == "512400.SH"

    def test_apply_trade_buy(self, assistant, sample_positions):
        """买入后持仓更新"""
        trade = ActualTrade(
            date="2026-06-26", ticker="512400.SH", action="BUY",
            shares=100, actual_price=0.650, commission=0.2, note="测试"
        )
        df = assistant.apply_trade(trade)
        row = df[df["ticker"] == "512400.SH"].iloc[0]
        assert row["shares"] == 300  # 200 + 100
        cash = df[df["ticker"] == CASH_TICKER].iloc[0]["market_value"]
        assert cash == 150000.0 - 100 * 0.650 - 0.2

    def test_apply_trade_sell(self, assistant, sample_positions):
        """卖出后持仓更新"""
        trade = ActualTrade(
            date="2026-06-26", ticker="512400.SH", action="SELL",
            shares=100, actual_price=0.650, commission=0.2, note="测试"
        )
        df = assistant.apply_trade(trade)
        row = df[df["ticker"] == "512400.SH"].iloc[0]
        assert row["shares"] == 100  # 200 - 100
        cash = df[df["ticker"] == CASH_TICKER].iloc[0]["market_value"]
        assert cash == 150000.0 + 100 * 0.650 - 0.2


class TestReportGeneration:
    """报告生成测试"""

    def test_daily_alert_no_trigger(self, assistant, sample_positions):
        content = assistant.generate_daily_alert("2026-06-26", output_path=os.path.join(tempfile.gettempdir(), "alert_test.md"))
        assert "无触发止损" in content

    def test_daily_alert_triggered(self, assistant):
        rows = [
            {"ticker": "512400.SH", "name": "信息技术", "shares": 200,
             "cost_price": 1.000, "current_price": 0.900, "market_value": 180.0,
             "available_cash": 0, "update_time": "2026-06-26"},
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 1000.0,
             "available_cash": 1180.0, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))
        content = assistant.generate_daily_alert("2026-06-26", output_path=os.path.join(tempfile.gettempdir(), "alert_test2.md"))
        assert "触发" in content
        assert "512400.SH" in content

    def test_weekly_plan_empty(self, assistant):
        content = assistant.generate_weekly_plan(pd.DataFrame(), "2026-06-26", output_path=os.path.join(tempfile.gettempdir(), "plan_test.md"))
        assert "无调仓建议" in content

    def test_weekly_plan_with_orders(self, assistant, sample_positions):
        target = {"512400.SH": 300, "512760.SH": 100}
        price_map = {"512400.SH": 0.650, "512760.SH": 1.150}
        plan = assistant.generate_trade_plan(target, price_map, "2026-06-26")
        content = assistant.generate_weekly_plan(plan, "2026-06-26", output_path=os.path.join(tempfile.gettempdir(), "plan_test2.md"))
        assert "买入订单" in content
        assert "卖出订单" in content


class TestMissingPrice:
    """缺价格场景测试"""

    def test_buy_with_missing_price_not_valid(self, assistant):
        """只有现金、目标买入新ETF、price_map缺失时，不能生成有效 BUY"""
        # 只保留现金，清空持仓
        rows = [
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 150000.0,
             "available_cash": 150000.0, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))

        target = {"512400.SH": 1000}
        price_map = {}  # 缺价格
        plan = assistant.generate_trade_plan(target, price_map, "2026-06-26")

        buy_df = plan[plan["action"] == "BUY"]
        assert len(buy_df) == 1
        assert buy_df.iloc[0]["estimated_price"] == 0
        assert buy_df.iloc[0]["estimated_amount"] == 0
        assert buy_df.iloc[0]["commission"] == 0
        assert "缺价格" in buy_df.iloc[0]["reason"]
        assert buy_df.iloc[0]["post_cash"] == 150000.0  # 现金未扣减

    def test_buy_with_valid_price_correct(self, assistant):
        """只有现金、目标买入新ETF、price_map包含价格时，BUY金额和股数正确"""
        rows = [
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 150000.0,
             "available_cash": 150000.0, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))

        target = {"512400.SH": 1000}
        price_map = {"512400.SH": 0.650}
        plan = assistant.generate_trade_plan(target, price_map, "2026-06-26")

        buy_df = plan[plan["action"] == "BUY"]
        assert len(buy_df) == 1
        assert buy_df.iloc[0]["estimated_price"] == 0.650
        assert buy_df.iloc[0]["estimated_amount"] == 1000 * 0.650
        assert buy_df.iloc[0]["commission"] > 0  # 有佣金
        assert buy_df.iloc[0]["post_cash"] == 150000.0 - 1000 * 0.650 - buy_df.iloc[0]["commission"]

    def test_report_warns_missing_price(self, assistant):
        """价格缺失时报告有明确警告"""
        rows = [
            {"ticker": CASH_TICKER, "name": "现金", "shares": 0,
             "cost_price": 0, "current_price": 0, "market_value": 150000.0,
             "available_cash": 150000.0, "update_time": "2026-06-26"},
        ]
        assistant.save_positions(pd.DataFrame(rows))

        target = {"512400.SH": 1000}
        price_map = {}  # 缺价格
        plan = assistant.generate_trade_plan(target, price_map, "2026-06-26")

        content = assistant.generate_weekly_plan(plan, "2026-06-26", output_path=os.path.join(tempfile.gettempdir(), "plan_missing_price.md"))
        assert "缺价格警告" in content
        assert "无法生成有效预估" in content
