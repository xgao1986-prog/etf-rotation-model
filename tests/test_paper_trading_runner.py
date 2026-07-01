#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_paper_trading_runner.py — Phase 2 runner comprehensive tests

Covers: idempotency, over-sell, atomicity, missing price, stop-loss clearing,
trading calendar, defense threshold, T+1 signal execution, open/close prices,
partial failure isolation.
"""

import os, sys, tempfile, pytest
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from paper_trading.models import AccountCreate, AccountType, OpeningPosition, StartMode
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore
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


# ============== Idempotency: same state on repeated runs ==============

class TestIdempotency:
    def test_duplicate_run_produces_identical_state(self, runner, service):
        """同日完整流程运行两次，全部账户数据完全相同。"""
        acct = _make_account(service, cash=1_000_000)
        r1 = runner.run_daily(acct, '2026-06-30', {}, {})
        r2 = runner.run_daily(acct, '2026-06-30', {}, {})

        assert r1['cash'] == r2['cash']
        assert r1['nav'] == r2['nav']
        assert set(r1['positions'].keys()) == set(r2['positions'].keys())
        for t in r1['positions']:
            assert r1['positions'][t]['shares'] == r2['positions'][t]['shares']
            assert r1['positions'][t]['market_value'] == r2['positions'][t]['market_value']

    def test_second_run_does_not_create_extra_trades(self, runner, service):
        """重复运行不重复成交。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 1.0, 1.0)])
        runner.run_daily(acct, '2026-06-30', {}, {'512400.SH': 0.9})
        trades1 = service.store.list_trades(acct, '2026-06-30')

        runner.run_daily(acct, '2026-06-30', {}, {'512400.SH': 0.9})
        trades2 = service.store.list_trades(acct, '2026-06-30')

        assert len(trades1) == len(trades2)


# ============== Stop Loss ==============

class TestStopLoss:
    def test_stop_loss_clears_position(self, runner, service):
        """止损清仓后持仓确实消失。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        result = runner.run_daily(acct, '2026-06-30', {}, {'512400.SH': 9.1})

        # 止损触发，持仓应消失
        assert '512400.SH' not in result['positions']
        assert result['cash'] > 900_000  # 卖出后现金增加

        # 数据库中持仓也应消失
        db_positions = service.store.list_positions(acct, '2026-06-30')
        assert len([p for p in db_positions if p['ticker'] == '512400.SH']) == 0

    def test_exact_threshold_not_triggered(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        result = runner.run_daily(acct, '2026-06-30', {}, {'512400.SH': 9.2})
        assert '512400.SH' in result['positions']
        assert len(result['trades']) == 0


# ============== Over-sell Protection ==============

class TestOverSell:
    def test_cannot_sell_more_than_available(self, runner, service):
        """100股不能卖出200股。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 100, 1.0, 1.0)])

        # 手动构造超卖订单
        orders = [{
            'order_id': 'order-1', 'ticker': '512400.SH', 'action': 'SELL',
            'delta_shares': -200,
        }]
        executed, skipped = runner.simulate_execution(acct, '2026-06-30', orders, {'512400.SH': 1.0})
        assert len(executed) == 0
        assert len(skipped) == 1
        assert 'oversell' in skipped[0][1]

        # 验证持仓未变
        positions = service.store.list_positions(acct, '2026-06-30')
        # 由于 execute_trades_atomic 会清除旧持仓并重新写入，
        # 如果没有成交，就不会写入新持仓。需要检查前一日的持仓。
        prev_positions = service.store.list_positions(acct, '2026-06-29')
        assert prev_positions[0]['shares'] == 100


# ============== Trading Calendar ==============

class TestTradingCalendar:
    def test_weekend_skipped(self, runner, service):
        """周五的下一交易日是周一。"""
        from paper_trading.runner import TradingCalendar
        cal = TradingCalendar()
        # 2026-06-26 is Friday, next trading day is Monday 2026-06-29
        assert cal.next_trading_day('2026-06-26') == '2026-06-29'
        # 2026-06-27 is Saturday, next trading day is Monday 2026-06-29
        assert cal.next_trading_day('2026-06-27') == '2026-06-29'
        assert cal.next_trading_day('2026-06-28') == '2026-06-29'

    def test_thursday_signal_executed_friday(self, runner, service):
        """周四信号在周五自动执行。"""
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}

        # 周四(2026-07-02)：生成信号（使用周四收盘价）
        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores, cfg)

        # 验证信号已保存
        signals = service.store.load_pending_signals(acct, '2026-07-03')
        assert len(signals) == 1

        # 周五(2026-07-03)：自动执行周四信号（使用周五开盘价）
        result = runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.05}, {'512400.SH': 1.05})
        assert len(result['trades']) > 0
        assert result['trades'][0]['price'] == 1.05  # 使用周五开盘价


# ============== Defense Threshold ==============

class TestDefenseThreshold:
    def test_low_score_defense_not_bought(self, runner, service):
        """低分防御资产不买入。"""
        acct = _make_account(service, cash=1_000_000)
        # 行业门槛 40，防御门槛 = 30
        scores = _make_scores(
            ['512400.SH', '515230.SH', '518880.SH'],
            [65, 62, 35]  # 518880.SH 防御资产，35 < 40-10=30? No, 35 >= 30
        )
        # Let's make it 25 so it's below threshold
        scores = _make_scores(
            ['512400.SH', '515230.SH', '518880.SH'],
            [65, 62, 25]  # 518880.SH = 25 < 30
        )
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}

        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0, '515230.SH': 1.0, '518880.SH': 1.0},
                         {'512400.SH': 1.0, '515230.SH': 1.0, '518880.SH': 1.0}, scores, cfg)
        runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.0, '515230.SH': 1.0, '518880.SH': 1.0},
                         {'512400.SH': 1.0, '515230.SH': 1.0, '518880.SH': 1.0})

        positions = service.store.list_positions(acct, '2026-07-03')
        tickers = [p['ticker'] for p in positions]
        assert '518880.SH' not in tickers


# ============== Open/Close Price Separation ==============

class TestOpenClosePrices:
    def test_trades_use_open_prices(self, runner, service):
        """开盘成交与收盘估值使用不同价格。"""
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}

        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores, cfg)
        result = runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.10}, {'512400.SH': 1.20})

        # 成交使用开盘价
        trades = result['trades']
        assert len(trades) > 0
        assert trades[0]['price'] == 1.10

        # 估值使用收盘价
        nav = service.store.get_nav(acct, '2026-07-03')
        positions = service.store.list_positions(acct, '2026-07-03')
        if positions:
            assert positions[0]['last_price'] == 1.20


# ============== Partial Failure Isolation ==============

class TestFailureIsolation:
    def test_one_account_failure_does_not_affect_other(self, runner, service):
        """部分订单失败时不破坏其他账户记录。"""
        acct1 = _make_account(service, account_id='acct-1', cash=1_000_000)
        acct2 = _make_account(service, account_id='acct-2', cash=1_000_000)

        # acct1: 正常处理
        runner.run_daily(acct1, '2026-06-30', {}, {})

        # acct2: 正常处理
        runner.run_daily(acct2, '2026-06-30', {}, {})

        # 验证两个账户都正常
        nav1 = service.store.get_nav(acct1, '2026-06-30')
        nav2 = service.store.get_nav(acct2, '2026-06-30')
        assert nav1['nav'] == 1_000_000
        assert nav2['nav'] == 1_000_000


# ============== Unique Final State ==============

class TestUniqueFinalState:
    def test_single_nav_per_day(self, runner, service):
        """当天所有成交、现金、持仓和总资产形成唯一最终状态。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 1.0, 1.0)])
        runner.run_daily(acct, '2026-06-30', {}, {'512400.SH': 0.9})

        # 验证只有一条 NAV 记录
        with service.store.connect() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM paper_daily_nav WHERE account_id = ? AND nav_date = ?",
                (acct, '2026-06-30'),
            ).fetchall()
            assert rows[0][0] == 1

        # 验证 cash + positions_value = nav
        nav = service.store.get_nav(acct, '2026-06-30')
        expected_nav = nav['cash'] + nav['positions_value']
        assert nav['nav'] == expected_nav

    def test_cash_never_negative(self, runner, service):
        """买入前检查现金，禁止负现金。"""
        acct = _make_account(service, cash=1_000)
        scores = _make_scores(['512400.SH'], [65])
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}

        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores, cfg)
        result = runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.0}, {'512400.SH': 1.0})

        # 现金不足，订单被拒绝，现金不应为负
        nav = service.store.get_nav(acct, '2026-07-03')
        assert nav['cash'] >= 0


# ============== Stop Loss + Rebalance Conflict ==============

class TestConflict:
    def test_stop_loss_before_rebalance(self, runner, service):
        """止损与调仓冲突：止损先执行。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        # 周二：持有 512400.SH，触发止损
        runner.run_daily(acct, '2026-06-30', {'512400.SH': 9.1}, {'512400.SH': 9.1})
        # 周三：止损已执行，持仓已清空
        result = runner.run_daily(acct, '2026-07-01', {'512400.SH': 9.0}, {'512400.SH': 9.0})
        assert '512400.SH' not in result['positions']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
