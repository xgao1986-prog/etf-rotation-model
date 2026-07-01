#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_paper_trading_runner.py — Phase 2: daily valuation, stop-loss, weekly rebalance, simulation
"""

import os, sys, tempfile, pytest
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from paper_trading.models import AccountCreate, AccountType, OpeningPosition, StartMode
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore
from paper_trading.runner import PaperTradingRunner


@pytest.fixture
def service():
    with tempfile.TemporaryDirectory() as d:
        yield PaperTradingService(PaperTradingStore(os.path.join(d, 'paper.db')))


@pytest.fixture
def runner(service):
    return PaperTradingRunner(service)


def _make_account(service, account_id='acct-runner', start_mode=StartMode.CASH, cash=1_000_000, holdings=None):
    holdings = holdings or []
    positions_value = sum(h.market_value for h in holdings)
    if start_mode is StartMode.IMPORTED:
        initial_capital = cash + positions_value
    else:
        initial_capital = cash
    request = AccountCreate(
        account_id=account_id, name='Runner',
        account_type=AccountType.COMPARISON, strategy_name='B0.4',
        strategy_config={'max_holdings': 5, 'stop_loss': -0.08},
        initial_capital=initial_capital, start_mode=start_mode,
        start_date='2026-06-29',
        opening_cash=cash if start_mode is StartMode.IMPORTED else None,
        opening_positions=tuple(holdings),
    )
    service.create_account(request)
    return account_id


def _make_prices(tickers, price, date='2026-06-30'):
    return {t: price for t in tickers}


class TestDailyValuation:
    """每日估值：更新持仓市值，记录 NAV。"""

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
        prices = _make_prices(['512400.SH'], 11.0)
        runner.run_daily_valuation(acct, '2026-06-30', prices)
        nav = service.get_nav(acct, '2026-06-30')
        assert nav['cash'] == 900_000
        assert nav['positions_value'] == 110_000
        assert nav['nav'] == 1_010_000

    def test_missing_price_leaves_last_price(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        runner.run_daily_valuation(acct, '2026-06-30', {})
        nav = service.get_nav(acct, '2026-06-30')
        assert nav['positions_value'] == 100_000


class TestStopLoss:
    """止损检查：触发止损的持仓生成 STOP_LOSS 订单。"""

    def test_no_stop_loss_when_above_threshold(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        prices = _make_prices(['512400.SH'], 10.0)
        orders = runner.check_stop_loss(acct, '2026-06-30', prices, stop_loss=-0.08)
        assert len(orders) == 0

    def test_stop_loss_triggered_at_threshold(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        prices = _make_prices(['512400.SH'], 9.1)  # -9% < -8%
        orders = runner.check_stop_loss(acct, '2026-06-30', prices, stop_loss=-0.08)
        assert len(orders) == 1
        assert orders[0]['action'] == 'STOP_LOSS'
        assert orders[0]['ticker'] == '512400.SH'
        assert orders[0]['delta_shares'] == -10_000

    def test_stop_loss_exactly_at_threshold_not_triggered(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        prices = _make_prices(['512400.SH'], 9.2)  # -8% exactly
        orders = runner.check_stop_loss(acct, '2026-06-30', prices, stop_loss=-0.08)
        assert len(orders) == 0


class TestWeeklyRebalance:
    """每周调仓：生成 B0.4 信号订单。"""

    def test_rebalance_creates_orders_for_top_candidates(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        scores = pd.DataFrame({
            'ticker': ['512400.SH', '515230.SH', '512480.SH', '516110.SH', '159928.SZ'],
            'total_score': [65, 62, 60, 58, 55],
        })
        prices = _make_prices(scores['ticker'].tolist(), 1.0)
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        orders = runner.run_weekly_rebalance(acct, '2026-06-30', scores, prices, cfg)
        assert len(orders) == 5
        assert all(o['action'] == 'BUY' for o in orders)

    def test_rebalance_skips_below_threshold(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        scores = pd.DataFrame({
            'ticker': ['512400.SH', '515230.SH'],
            'total_score': [65, 35],
        })
        prices = _make_prices(['512400.SH', '515230.SH'], 1.0)
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        orders = runner.run_weekly_rebalance(acct, '2026-06-30', scores, prices, cfg)
        tickers = [o['ticker'] for o in orders]
        assert '512400.SH' in tickers
        assert '515230.SH' not in tickers


class TestSimulateExecution:
    """模拟成交：T+1 开盘价成交，更新持仓和现金。"""

    def test_buy_execution_updates_cash_and_positions(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        order = {
            'order_id': 'order-1',
            'dedupe_key': f'{acct}:2026-07-01:512400.SH:BUY',
            'account_id': acct,
            'signal_date': '2026-06-30',
            'trade_date': '2026-07-01',
            'ticker': '512400.SH',
            'action': 'BUY',
            'current_shares': 0,
            'target_shares': 10_000,
            'delta_shares': 10_000,
            'reference_price': 1.0,
            'reason': 'B0.4 selected',
            'status': 'PENDING',
        }
        service.append_order(order)
        prices = _make_prices(['512400.SH'], 1.0)
        trades = runner.simulate_execution(acct, '2026-07-01', [order], prices, commission_rate=0.0003)
        assert len(trades) == 1
        assert trades[0]['action'] == 'BUY'
        assert trades[0]['shares'] == 10_000
        # 佣金: 10,000 * 1.0 * 0.0003 = 3.0, min 5.0
        assert trades[0]['commission'] == 5.0
        # 持仓已更新
        positions = service.store.list_positions(acct, '2026-07-01')
        assert len(positions) == 1
        assert positions[0]['ticker'] == '512400.SH'
        assert positions[0]['shares'] == 10_000
        # NAV 已更新
        nav = service.get_nav(acct, '2026-07-01')
        assert nav['cash'] == 1_000_000 - 10_000 - 5.0
        assert nav['positions_value'] == 10_000

    def test_sell_execution_updates_cash_and_removes_position(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 1.0, 1.0)])
        order = {
            'order_id': 'order-2',
            'dedupe_key': f'{acct}:2026-07-01:512400.SH:SELL',
            'account_id': acct,
            'signal_date': '2026-06-30',
            'trade_date': '2026-07-01',
            'ticker': '512400.SH',
            'action': 'SELL',
            'current_shares': 10_000,
            'target_shares': 0,
            'delta_shares': -10_000,
            'reference_price': 1.0,
            'reason': 'Rebalance',
            'status': 'PENDING',
        }
        service.append_order(order)
        prices = _make_prices(['512400.SH'], 1.0)
        trades = runner.simulate_execution(acct, '2026-07-01', [order], prices, commission_rate=0.0003)
        assert len(trades) == 1
        assert trades[0]['action'] == 'SELL'
        # 持仓已清空
        positions = service.store.list_positions(acct, '2026-07-01')
        assert len([p for p in positions if p['ticker'] == '512400.SH']) == 0
        # 现金增加
        nav = service.get_nav(acct, '2026-07-01')
        assert nav['cash'] == 900_000 + 10_000 - 5.0

    def test_idempotent_execution_same_day(self, runner, service):
        acct = _make_account(service, cash=1_000_000)
        order = {
            'order_id': 'order-3',
            'dedupe_key': f'{acct}:2026-07-01:512400.SH:BUY',
            'account_id': acct,
            'signal_date': '2026-06-30',
            'trade_date': '2026-07-01',
            'ticker': '512400.SH',
            'action': 'BUY',
            'current_shares': 0,
            'target_shares': 10_000,
            'delta_shares': 10_000,
            'reference_price': 1.0,
            'reason': 'B0.4 selected',
            'status': 'PENDING',
        }
        service.append_order(order)
        prices = _make_prices(['512400.SH'], 1.0)
        # 第一次执行
        trades1 = runner.simulate_execution(acct, '2026-07-01', [order], prices)
        assert len(trades1) == 1
        # 第二次执行（同日）应被阻止
        trades2 = runner.simulate_execution(acct, '2026-07-01', [order], prices)
        assert len(trades2) == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
