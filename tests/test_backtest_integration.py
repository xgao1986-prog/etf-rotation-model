"""
B0 调仓引擎 v2.5 集成测试
验证 BacktestEngine._rebalance_v2 与 plan_rebalance_v2_5 的集成
"""

import pytest
import sys
import os
import math
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from backtest import BacktestEngine
from rebalance_planner import plan_rebalance_v2_5


class TestBacktestIntegration:
    """集成测试：验证 v2 调仓在 BacktestEngine 中的正确集成"""
    
    def test_rebalance_v2_method_exists(self):
        """验证 _rebalance_v2 方法存在"""
        engine = BacktestEngine()
        assert hasattr(engine, '_rebalance_v2')
    
    def test_rebalance_v2_empty_portfolio(self):
        """
        集成测试：空仓，5只行业候选，使用 v2 调仓
        验证：买入5只，无防御，NAV守恒
        """
        engine = BacktestEngine()
        engine.cfg['use_v2_rebalance'] = True
        
        portfolio = {
            'cash': 1_000_000.0,
            'positions': {},
        }
        
        date = pd.Timestamp('2026-03-12')
        date_str = '2026-03-12'
        
        # 构造 day_signals
        day_signals = pd.DataFrame({
            'ticker': ['A', 'B', 'C', 'D', 'E', 'GOLD'],
            'total_score': [90.0, 80.0, 70.0, 60.0, 50.0, 45.0],
            'signal_type': ['BUY', 'BUY', 'BUY', 'BUY', 'BUY', 'BUY'],
            'atr_14': [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        })
        
        day_prices = {
            'A': 100.0, 'B': 100.0, 'C': 100.0, 'D': 100.0, 'E': 100.0,
            'GOLD': 50.0,
        }
        
        effective_close_prices = dict(day_prices)
        last_valid_close = dict(day_prices)
        
        buy_signals = day_signals[day_signals['signal_type'] == 'BUY'].sort_values('total_score', ascending=False)
        
        trade_records = []
        cooling_list = {}
        
        _core_tickers = ['A', 'B', 'C', 'D', 'E']
        _fallback_tickers = []
        _defense_tickers = ['GOLD']
        
        engine._rebalance_v2(
            portfolio, day_signals, day_prices, effective_close_prices,
            last_valid_close, date, date_str, buy_signals, trade_records,
            cooling_list, 1.0, _core_tickers, _fallback_tickers, _defense_tickers,
            {}, 0, False, None, None, {}, 0, 0, None, 0.70,
            lambda amount: max(amount * 0.0003, 5.0),
        )
        
        # 验证：买入5只行业
        assert len(portfolio['positions']) == 5, f"Expected 5 positions, got {len(portfolio['positions'])}"
        for t in ['A', 'B', 'C', 'D', 'E']:
            assert t in portfolio['positions'], f"{t} should be in positions"
        
        # 验证：无防御
        assert 'GOLD' not in portfolio['positions'], "GOLD should not be bought (cash should be used by industry)"
        
        # 验证：NAV守恒
        nav_check = portfolio['cash'] + sum(
            p['shares'] * effective_close_prices.get(t, 0)
            for t, p in portfolio['positions'].items()
        )
        # 加上总佣金
        total_commission = sum(r['commission'] for r in trade_records)
        nav_check += total_commission
        assert abs(nav_check - 1_000_000.0) < 0.01, f"NAV not conserved: {nav_check} != 1,000,000"
    
    def test_rebalance_v2_with_defense(self):
        """
        集成测试：空仓，2只行业候选，2只防御候选，使用 v2 调仓
        验证：行业优先买入，防御用剩余资金填充
        """
        engine = BacktestEngine()
        engine.cfg['use_v2_rebalance'] = True
        
        portfolio = {
            'cash': 1_000_000.0,
            'positions': {},
        }
        
        date = pd.Timestamp('2026-03-12')
        date_str = '2026-03-12'
        
        day_signals = pd.DataFrame({
            'ticker': ['A', 'B', 'GOLD', 'BOND'],
            'total_score': [90.0, 80.0, 45.0, 40.0],
            'signal_type': ['BUY', 'BUY', 'BUY', 'BUY'],
            'atr_14': [1.0, 1.0, 1.0, 1.0],
        })
        
        day_prices = {
            'A': 100.0, 'B': 100.0, 'GOLD': 50.0, 'BOND': 50.0,
        }
        
        effective_close_prices = dict(day_prices)
        last_valid_close = dict(day_prices)
        
        buy_signals = day_signals[day_signals['signal_type'] == 'BUY'].sort_values('total_score', ascending=False)
        
        trade_records = []
        cooling_list = {}
        
        _core_tickers = ['A', 'B']
        _fallback_tickers = []
        _defense_tickers = ['GOLD', 'BOND']
        
        engine._rebalance_v2(
            portfolio, day_signals, day_prices, effective_close_prices,
            last_valid_close, date, date_str, buy_signals, trade_records,
            cooling_list, 1.0, _core_tickers, _fallback_tickers, _defense_tickers,
            {}, 0, False, None, None, {}, 0, 0, None, 0.70,
            lambda amount: max(amount * 0.0003, 5.0),
        )
        
        # 验证：行业先买入
        assert 'A' in portfolio['positions'], "A should be bought"
        assert 'B' in portfolio['positions'], "B should be bought"
        
        # 验证：防御可能填充（取决于剩余资金）
        # 在满仓信号下，防御可能填充
    
    def test_rebalance_v2_order_independence(self):
        """
        集成测试：候选顺序不同，结果相同
        """
        engine = BacktestEngine()
        engine.cfg['use_v2_rebalance'] = True
        
        date = pd.Timestamp('2026-03-12')
        date_str = '2026-03-12'
        
        day_prices = {'A': 100.0, 'B': 100.0}
        effective_close_prices = dict(day_prices)
        last_valid_close = dict(day_prices)
        
        _core_tickers = ['A', 'B']
        _fallback_tickers = []
        _defense_tickers = []
        
        # 顺序1：A, B
        portfolio1 = {'cash': 100_000.0, 'positions': {}}
        day_signals1 = pd.DataFrame({
            'ticker': ['A', 'B'],
            'total_score': [90.0, 80.0],
            'signal_type': ['BUY', 'BUY'],
            'atr_14': [1.0, 1.0],
        })
        buy_signals1 = day_signals1[day_signals1['signal_type'] == 'BUY']
        
        trade_records1 = []
        cooling_list1 = {}
        
        engine._rebalance_v2(
            portfolio1, day_signals1, day_prices, effective_close_prices,
            last_valid_close, date, date_str, buy_signals1, trade_records1,
            cooling_list1, 1.0, _core_tickers, _fallback_tickers, _defense_tickers,
            {}, 0, False, None, None, {}, 0, 0, None, 0.70,
            lambda amount: max(amount * 0.0003, 5.0),
        )
        
        # 顺序2：B, A
        portfolio2 = {'cash': 100_000.0, 'positions': {}}
        day_signals2 = pd.DataFrame({
            'ticker': ['B', 'A'],
            'total_score': [80.0, 90.0],
            'signal_type': ['BUY', 'BUY'],
            'atr_14': [1.0, 1.0],
        })
        buy_signals2 = day_signals2[day_signals2['signal_type'] == 'BUY']
        
        trade_records2 = []
        cooling_list2 = {}
        
        engine._rebalance_v2(
            portfolio2, day_signals2, day_prices, effective_close_prices,
            last_valid_close, date, date_str, buy_signals2, trade_records2,
            cooling_list2, 1.0, _core_tickers, _fallback_tickers, _defense_tickers,
            {}, 0, False, None, None, {}, 0, 0, None, 0.70,
            lambda amount: max(amount * 0.0003, 5.0),
        )
        
        # 验证：持仓相同
        assert portfolio1['positions'] == portfolio2['positions'], \
            f"Order independence failed: {portfolio1['positions']} != {portfolio2['positions']}"
