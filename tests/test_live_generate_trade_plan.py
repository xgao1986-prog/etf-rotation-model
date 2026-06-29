#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_live_generate_trade_plan.py

实盘调仓建议生成测试
覆盖：行业候选充足、行业候选不足、防御资产补充、首次建仓、已有持仓调仓
"""

import os, sys, tempfile, pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from rebalance_planner import plan_rebalance_v2_5


class TestPlanRebalanceV25:
    """测试 plan_rebalance_v2_5 在实盘场景中的行为"""

    def test_five_qualified_industry(self):
        """5只合格行业ETF：全部选中，不配置防御"""
        industry = [
            ('512400.SH', 65.0), ('515230.SH', 62.0), ('512480.SH', 60.0),
            ('516110.SH', 58.0), ('512690.SH', 55.0),
        ]
        defense = [('518880.SH', 50.0), ('511010.SH', 45.0)]
        prices = {t: 1.0 for t, _ in industry + defense}

        trades, final = plan_rebalance_v2_5(
            nav=150000, cash=150000,
            current_positions={},
            industry_candidates=industry,
            defense_candidates=defense,
            prices=prices,
            industry_tickers=set(t for t, _ in industry),
            defense_tickers=set(t for t, _ in defense),
            max_industry_holdings=5, max_defense_holdings=2,
            max_total_holdings=5, max_position_per_etf=0.20,
            max_total_position=1.0, defense_enabled=True,
        )

        positions = final['positions']
        # 应选中5只行业ETF
        industry_selected = [t for t in positions if t in set(x for x, _ in industry)]
        assert len(industry_selected) == 5, f"Expected 5 industry, got {len(industry_selected)}: {industry_selected}"

        # 不应有防御资产
        defense_selected = [t for t in positions if t in set(x for x, _ in defense)]
        assert len(defense_selected) == 0, f"Expected 0 defense, got {len(defense_selected)}: {defense_selected}"

    def test_three_qualified_industry_fills_defense(self):
        """3只合格行业ETF：补充防御资产至5只"""
        industry = [
            ('512400.SH', 65.0), ('515230.SH', 62.0), ('512480.SH', 60.0),
        ]
        defense = [('518880.SH', 50.0), ('511010.SH', 45.0)]
        prices = {t: 1.0 for t, _ in industry + defense}

        trades, final = plan_rebalance_v2_5(
            nav=150000, cash=150000,
            current_positions={},
            industry_candidates=industry,
            defense_candidates=defense,
            prices=prices,
            industry_tickers=set(t for t, _ in industry),
            defense_tickers=set(t for t, _ in defense),
            max_industry_holdings=5, max_defense_holdings=2,
            max_total_holdings=5, max_position_per_etf=0.20,
            max_total_position=1.0, defense_enabled=True,
        )

        positions = final['positions']
        # 应选中3只行业 + 2只防御 = 5只
        industry_selected = [t for t in positions if t in set(x for x, _ in industry)]
        defense_selected = [t for t in positions if t in set(x for x, _ in defense)]
        assert len(industry_selected) == 3, f"Expected 3 industry, got {len(industry_selected)}"
        assert len(defense_selected) == 2, f"Expected 2 defense, got {len(defense_selected)}"

    def test_defense_does_not_rank(self):
        """防御ETF不参与排名，只在行业不足时填充"""
        industry = [
            ('512400.SH', 65.0), ('515230.SH', 62.0),
        ]
        # 防御评分很高，但不应抢占行业位置
        defense = [('518880.SH', 99.0), ('511010.SH', 98.0)]
        prices = {t: 1.0 for t, _ in industry + defense}

        trades, final = plan_rebalance_v2_5(
            nav=150000, cash=150000,
            current_positions={},
            industry_candidates=industry,
            defense_candidates=defense,
            prices=prices,
            industry_tickers=set(t for t, _ in industry),
            defense_tickers=set(t for t, _ in defense),
            max_industry_holdings=5, max_defense_holdings=2,
            max_total_holdings=5, max_position_per_etf=0.20,
            max_total_position=1.0, defense_enabled=True,
        )

        positions = final['positions']
        # 防御评分高，但只填充不足部分
        industry_selected = [t for t in positions if t in set(x for x, _ in industry)]
        defense_selected = [t for t in positions if t in set(x for x, _ in defense)]
        assert len(industry_selected) == 2, f"Expected 2 industry, got {len(industry_selected)}"
        assert len(defense_selected) == 2, f"Expected 2 defense, got {len(defense_selected)}"

    def test_first_buy_generates_orders(self):
        """首次建仓：生成BUY订单"""
        industry = [('512400.SH', 65.0), ('515230.SH', 62.0)]
        defense = [('518880.SH', 50.0)]
        prices = {'512400.SH': 1.0, '515230.SH': 1.0, '518880.SH': 1.0}

        trades, final = plan_rebalance_v2_5(
            nav=150000, cash=150000,
            current_positions={},
            industry_candidates=industry,
            defense_candidates=defense,
            prices=prices,
            industry_tickers={'512400.SH', '515230.SH'},
            defense_tickers={'518880.SH'},
            max_industry_holdings=5, max_defense_holdings=2,
            max_total_holdings=5, max_position_per_etf=0.20,
            max_total_position=1.0, defense_enabled=True,
        )

        buy_trades = [t for t in trades if t['action'] == 'BUY']
        assert len(buy_trades) > 0, f"Expected BUY trades, got: {trades}"

    def test_existing_position_rebalance(self):
        """已有持仓调仓：卖出不合格，买入新候选"""
        # 当前持有 A（不合格），新候选 C、D 合格
        industry = [
            ('512400.SH', 65.0),  # C - new, qualified
            ('515230.SH', 62.0),  # D - new, qualified
        ]
        current = {'512690.SH': 1000}  # A - existing, not in candidates
        prices = {'512400.SH': 1.0, '515230.SH': 1.0, '512690.SH': 1.0}
        # NAV must equal cash + valued_positions
        nav = 150000 + 1000  # cash + shares * price

        trades, final = plan_rebalance_v2_5(
            nav=nav, cash=150000,
            current_positions=current,
            industry_candidates=industry,
            defense_candidates=[],
            prices=prices,
            industry_tickers={'512400.SH', '515230.SH', '512690.SH'},
            defense_tickers=set(),
            max_industry_holdings=5, max_defense_holdings=2,
            max_total_holdings=5, max_position_per_etf=0.20,
            max_total_position=1.0, defense_enabled=True,
        )

        # 应该卖出 512690.SH
        sell_trades = [t for t in trades if t['action'] == 'SELL' and t['ticker'] == '512690.SH']
        assert len(sell_trades) > 0, f"Expected SELL 512690.SH, got trades: {trades}"

        # 应该买入新候选
        buy_tickers = [t['ticker'] for t in trades if t['action'] == 'BUY']
        assert '512400.SH' in buy_tickers or '515230.SH' in buy_tickers, \
            f"Expected BUY new candidates, got: {buy_tickers}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
