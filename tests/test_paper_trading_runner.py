#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_paper_trading_runner.py — Phase 2 runner: daily valuation, stop-loss, weekly rebalance, execution

Covers: 满仓手续费, 重复运行, 原子性, 缺价, 止损调仓冲突, 卖出后持仓清除, T+1, 防御资产填充
"""

import os, sys, tempfile, pytest
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from paper_trading.models import AccountCreate, AccountType, OpeningPosition, StartMode
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore, DuplicateLedgerEvent
from paper_trading.runner import PaperTradingRunner


def _make_account(service, account_id='acct-test', start_mode=StartMode.CASH, cash=1_000_000, holdings=None):
    holdings = holdings or []
    positions_value = sum(h.market_value for h in holdings)
    if start_mode is StartMode.IMPORTED:
        initial_capital = cash + positions_value
    else:
        initial_capital = cash
    request = AccountCreate(
        account_id=account_id, name='Test',
        account_type=AccountType.COMPARISON, strategy_name='B0.4',
        strategy_config={'max_holdings': 5, 'stop_loss': -0.08},
        initial_capital=initial_capital, start_mode=start_mode,
        start_date='2026-06-29',
        opening_cash=cash if start_mode is StartMode.IMPORTED else None,
        opening_positions=tuple(holdings),
    )
    service.create_account(request)
    return account_id


def _make_scores(tickers, scores):
    return pd.DataFrame({'ticker': tickers, 'total_score': scores})


@pytest.fixture
def service():
    with tempfile.TemporaryDirectory() as d:
        yield PaperTradingService(PaperTradingStore(os.path.join(d, 'paper.db')))


@pytest.fixture
def runner(service):
    return PaperTradingRunner(service)


# ============== Daily Valuation ==============

class TestDailyValuation:
    def test_cash_account_no_change(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        runner.run_daily_valuation(acct, '2026-06-30', {})
        nav = service.get_nav(acct, '2026-06-30')
        assert nav['cash'] == 1_000_000
        assert nav['positions_value'] == 0
        assert nav['nav'] == 1_000_000

    def test_position_value_updates(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        runner.run_daily_valuation(acct, '2026-06-30', {'512400.SH': 11.0})
        nav = service.get_nav(acct, '2026-06-30')
        assert nav['cash'] == 900_000
        assert nav['positions_value'] == 110_000
        assert nav['nav'] == 1_010_000

    def test_missing_price_keeps_last(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        runner.run_daily_valuation(acct, '2026-06-30', {})
        nav = service.get_nav(acct, '2026-06-30')
        assert nav['positions_value'] == 100_000

    def test_duplicate_run_same_day_returns_existing(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        nav1 = runner.run_daily_valuation(acct, '2026-06-30', {})
        nav2 = runner.run_daily_valuation(acct, '2026-06-30', {})
        assert nav1 == nav2  # 不报错，返回已有 NAV


# ============== Stop Loss ==============

class TestStopLoss:
    def test_no_trigger_when_above(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        orders = runner.check_stop_loss(acct, '2026-06-30', {'512400.SH': 10.0})
        assert len(orders) == 0

    def test_triggered_below_threshold(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        orders = runner.check_stop_loss(acct, '2026-06-30', {'512400.SH': 9.1})
        assert len(orders) == 1
        assert orders[0]['action'] == 'STOP_LOSS'
        assert orders[0]['delta_shares'] == -10_000

    def test_exactly_at_threshold_not_triggered(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        orders = runner.check_stop_loss(acct, '2026-06-30', {'512400.SH': 9.2})
        assert len(orders) == 0

    def test_missing_price_no_order(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        orders = runner.check_stop_loss(acct, '2026-06-30', {})
        assert len(orders) == 0


# ============== Weekly Rebalance ==============

class TestWeeklyRebalance:
    def test_five_industry_no_defense(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH', '515230.SH', '512480.SH', '516110.SH', '159928.SZ'],
                              [65, 62, 60, 58, 55])
        prices = {t: 1.0 for t in scores['ticker']}
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        orders = runner.run_weekly_rebalance(acct, '2026-06-30', scores, prices, cfg)
        buy_tickers = [o['ticker'] for o in orders if o['action'] == 'BUY']
        assert len(buy_tickers) == 5
        assert all(t in buy_tickers for t in ['512400.SH', '515230.SH', '512480.SH', '516110.SH', '159928.SZ'])

    def test_three_industry_fills_defense(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH', '515230.SH', '512480.SH', '518880.SH', '511010.SH'],
                              [65, 62, 60, 50, 45])
        prices = {t: 1.0 for t in scores['ticker']}
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        orders = runner.run_weekly_rebalance(acct, '2026-06-30', scores, prices, cfg)
        buy_tickers = [o['ticker'] for o in orders if o['action'] == 'BUY']
        assert len(buy_tickers) == 5
        industry = [t for t in buy_tickers if t in ['512400.SH', '515230.SH', '512480.SH']]
        defense = [t for t in buy_tickers if t in ['518880.SH', '511010.SH']]
        assert len(industry) == 3
        assert len(defense) == 2

    def test_signal_date_and_trade_date_separate(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])
        prices = {'512400.SH': 1.0}
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        orders = runner.run_weekly_rebalance(acct, '2026-06-26', scores, prices, cfg, trade_date='2026-06-27')
        assert orders[0]['signal_date'] == '2026-06-26'
        assert orders[0]['trade_date'] == '2026-06-27'

    def test_skips_below_threshold(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH', '515230.SH'], [65, 35])
        prices = {'512400.SH': 1.0, '515230.SH': 1.0}
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        orders = runner.run_weekly_rebalance(acct, '2026-06-30', scores, prices, cfg)
        buy_tickers = [o['ticker'] for o in orders if o['action'] == 'BUY']
        assert '512400.SH' in buy_tickers
        assert '515230.SH' not in buy_tickers


# ============== Simulated Execution ==============

class TestSimulateExecution:
    def test_buy_updates_cash_and_positions(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        order = {
            'order_id': 'order-1', 'dedupe_key': f'{acct}:2026-07-01:512400.SH:BUY',
            'account_id': acct, 'signal_date': '2026-06-30', 'trade_date': '2026-07-01',
            'ticker': '512400.SH', 'action': 'BUY',
            'current_shares': 0, 'target_shares': 10_000, 'delta_shares': 10_000,
            'reference_price': 1.0, 'reason': 'test', 'status': 'PENDING',
        }
        executed, skipped = runner.simulate_execution(acct, '2026-07-01', [order], {'512400.SH': 1.0})
        assert len(executed) == 1
        assert executed[0]['commission'] == 5.0
        nav = service.get_nav(acct, '2026-07-01')
        assert nav['cash'] == 1_000_000 - 10_000 - 5.0
        positions = service.store.list_positions(acct, '2026-07-01')
        assert positions[0]['shares'] == 10_000

    def test_sell_removes_position(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 1.0, 1.0)])
        order = {
            'order_id': 'order-2', 'dedupe_key': f'{acct}:2026-07-01:512400.SH:SELL',
            'account_id': acct, 'signal_date': '2026-06-30', 'trade_date': '2026-07-01',
            'ticker': '512400.SH', 'action': 'SELL',
            'current_shares': 10_000, 'target_shares': 0, 'delta_shares': -10_000,
            'reference_price': 1.0, 'reason': 'test', 'status': 'PENDING',
        }
        executed, skipped = runner.simulate_execution(acct, '2026-07-01', [order], {'512400.SH': 1.0})
        assert len(executed) == 1
        positions = service.store.list_positions(acct, '2026-07-01')
        assert len([p for p in positions if p['ticker'] == '512400.SH']) == 0

    def test_idempotent_same_day(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        order = {
            'order_id': 'order-3', 'dedupe_key': f'{acct}:2026-07-01:512400.SH:BUY',
            'account_id': acct, 'signal_date': '2026-06-30', 'trade_date': '2026-07-01',
            'ticker': '512400.SH', 'action': 'BUY',
            'current_shares': 0, 'target_shares': 10_000, 'delta_shares': 10_000,
            'reference_price': 1.0, 'reason': 'test', 'status': 'PENDING',
        }
        executed1, _ = runner.simulate_execution(acct, '2026-07-01', [order], {'512400.SH': 1.0})
        assert len(executed1) == 1
        executed2, _ = runner.simulate_execution(acct, '2026-07-01', [order], {'512400.SH': 1.0})
        assert len(executed2) == 0  # 重复，不报错，不重复成交

    def test_insufficient_cash_skipped(self, runner, service):
        """现金不足时跳过买入，不产生负现金。"""
        acct = _make_account(service, cash=1_000)
        order = {
            'order_id': 'order-4', 'dedupe_key': f'{acct}:2026-07-01:512400.SH:BUY',
            'account_id': acct, 'signal_date': '2026-06-30', 'trade_date': '2026-07-01',
            'ticker': '512400.SH', 'action': 'BUY',
            'current_shares': 0, 'target_shares': 10_000, 'delta_shares': 10_000,
            'reference_price': 1.0, 'reason': 'test', 'status': 'PENDING',
        }
        executed, skipped = runner.simulate_execution(acct, '2026-07-01', [order], {'512400.SH': 1.0})
        assert len(executed) == 0
        assert len(skipped) == 1
        assert 'insufficient cash' in skipped[0][1]
        # 验证没有负现金（cash 不变，持仓不变）
        nav = service.get_nav(acct, '2026-07-01')
        assert nav['cash'] == 1_000
        assert nav['positions_value'] == 0
        positions = service.store.list_positions(acct, '2026-07-01')
        assert len(positions) == 0

    def test_missing_price_skipped(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        order = {
            'order_id': 'order-5', 'dedupe_key': f'{acct}:2026-07-01:512400.SH:BUY',
            'account_id': acct, 'signal_date': '2026-06-30', 'trade_date': '2026-07-01',
            'ticker': '512400.SH', 'action': 'BUY',
            'current_shares': 0, 'target_shares': 10_000, 'delta_shares': 10_000,
            'reference_price': 1.0, 'reason': 'test', 'status': 'PENDING',
        }
        executed, skipped = runner.simulate_execution(acct, '2026-07-01', [order], {})
        assert len(executed) == 0
        assert len(skipped) == 1
        assert 'missing price' in skipped[0][1]


# ============== Full Daily Flow ==============

class TestDailyFlow:
    def test_complete_daily_no_rebalance(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        result = runner.run_daily(acct, '2026-06-30', {})
        assert result['valuation']['nav'] == 1_000_000
        assert len(result['stop_loss_orders']) == 0
        assert len(result['rebalance_orders']) == 0

    def test_stop_loss_and_rebalance_conflict(self, runner, service):
        """止损和调仓冲突：止损先执行，调仓后执行。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        prices = {'512400.SH': 9.1}  # 触发止损
        scores = _make_scores(['512400.SH'], [65])
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}

        # 先执行止损
        sl_orders = runner.check_stop_loss(acct, '2026-06-30', prices)
        assert len(sl_orders) == 1
        sl_executed, _ = runner.simulate_execution(acct, '2026-06-30', sl_orders, prices)
        assert len(sl_executed) == 1

        # 再估值（止损后持仓已清空）
        runner.run_daily_valuation(acct, '2026-06-30', prices)

        # 调仓（因为 512400.SH 已卖出，如果有买入订单， cash 应足够）
        rebalance_orders = runner.run_weekly_rebalance(acct, '2026-06-30', scores, prices, cfg)
        # 此时 cash 已增加（止损卖出后），可以重新买入
        nav = service.get_nav(acct, '2026-06-30')
        assert nav['cash'] > 900_000  # 止损卖出后 cash 增加

    def test_full_position_plus_commission(self, runner, service):
        """满仓加手续费：买入前现金检查。"""
        acct = _make_account(service, cash=10_000)
        order = {
            'order_id': 'order-6', 'dedupe_key': f'{acct}:2026-07-01:512400.SH:BUY',
            'account_id': acct, 'signal_date': '2026-06-30', 'trade_date': '2026-07-01',
            'ticker': '512400.SH', 'action': 'BUY',
            'current_shares': 0, 'target_shares': 10_000, 'delta_shares': 10_000,
            'reference_price': 1.0, 'reason': 'test', 'status': 'PENDING',
        }
        executed, skipped = runner.simulate_execution(acct, '2026-07-01', [order], {'512400.SH': 1.0})
        assert len(executed) == 0
        assert 'insufficient cash' in skipped[0][1]


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
