#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test: Live Daily Workflow

覆盖：
1. 每日数据更新入口
2. 止损检查
3. 纸面交易日志追加
4. 报告生成

不依赖外部数据，使用 mock。
"""

import pytest, os, sys, tempfile, pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, "src")

from live_trading_assistant import LiveTradingAssistant, CASH_TICKER


@pytest.fixture
def temp_dirs():
    """创建临时目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_positions_csv(temp_dirs):
    """创建示例持仓 CSV。"""
    path = os.path.join(temp_dirs, "actual_positions.csv")
    df = pd.DataFrame({
        "ticker": ["512400.SH", "512010.SH", CASH_TICKER],
        "name": ["有色金属", "医药", "现金"],
        "shares": [200, 100, 0],
        "cost_price": [0.650, 1.200, 1.0],
        "current_price": [0.600, 1.150, 1.0],
        "market_value": [120.0, 115.0, 50000.0],
        "available_cash": [0, 0, 50235.0],
        "update_time": ["2026-06-25", "2026-06-25", "2026-06-25"],
    })
    df.to_csv(path, index=False)
    return path


class TestLiveDailyWorkflow:
    """测试每日工作流。"""

    def test_load_positions(self, sample_positions_csv):
        """测试持仓加载。"""
        assistant = LiveTradingAssistant(positions_path=sample_positions_csv)
        df = assistant.load_positions()
        assert len(df) == 3
        assert CASH_TICKER in df["ticker"].values

    def test_validate_positions(self, sample_positions_csv):
        """测试持仓校验。"""
        assistant = LiveTradingAssistant(positions_path=sample_positions_csv)
        report = assistant.validate_positions()
        # 512400 和 512010 都是 B0.4 池内，应该通过
        assert report.ok

    def test_check_stop_loss(self, sample_positions_csv):
        """测试止损检查。"""
        assistant = LiveTradingAssistant(positions_path=sample_positions_csv)
        # 512400 成本 0.650，当前 0.600，跌幅约 7.69%
        # 512010 成本 1.200，当前 1.150，跌幅约 4.17%
        alerts = assistant.check_stop_loss(stop_loss_pct=0.05)
        # 512400 跌幅 7.69% > 5%，应该触发
        assert "512400.SH" in alerts["ticker"].values
        # 512010 跌幅 4.17% < 5%，不应触发
        assert "512010.SH" not in alerts["ticker"].values

    def test_generate_trade_plan(self, sample_positions_csv):
        """测试交易计划生成。"""
        assistant = LiveTradingAssistant(positions_path=sample_positions_csv)
        target = {"512400.SH": 300, "512010.SH": 0, "515230.SH": 200}
        price_map = {"512400.SH": 0.600, "512010.SH": 1.150, "515230.SH": 1.000}
        plan = assistant.generate_trade_plan(target, price_map)
        # 512400: 200 -> 300, BUY
        buy_rows = plan[plan["action"] == "BUY"]
        assert "512400.SH" in buy_rows["ticker"].values
        # 512010: 100 -> 0, SELL
        sell_rows = plan[plan["action"] == "SELL"]
        assert "512010.SH" in sell_rows["ticker"].values

    def test_paper_log_append(self, temp_dirs, sample_positions_csv):
        """测试纸面日志追加。"""
        import scripts.live_daily_update as ldu
        ldu.DATA_LIVE_DIR = temp_dirs

        ldu.append_paper_log(
            "2026-06-26", "512400.SH", "BUY", 100, 0.600,
            model_reason="调仓买入", stop_loss_triggered=False
        )

        log_path = os.path.join(temp_dirs, "paper_trading_log.csv")
        assert os.path.exists(log_path)
        df = pd.read_csv(log_path)
        assert len(df) == 1
        assert df.iloc[0]["ticker"] == "512400.SH"
        assert df.iloc[0]["action"] == "BUY"
        assert df.iloc[0]["model_reason"] == "调仓买入"
        assert df.iloc[0]["executed"] == False

    def test_daily_report_generation(self, temp_dirs, sample_positions_csv):
        """测试每日报告生成。"""
        import scripts.live_daily_update as ldu
        ldu.DATA_LIVE_DIR = temp_dirs
        ldu.REPORTS_LIVE_DIR = temp_dirs

        output_path = os.path.join(temp_dirs, "latest_daily_check.md")
        ldu.generate_daily_report(
            "2026-06-26", True, [], {}, pd.DataFrame(), {}, output_path
        )
        assert os.path.exists(output_path)
        with open(output_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "每日检查报告" in content
        assert "数据完整性" in content
