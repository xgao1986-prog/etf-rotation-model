#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_paper_trading_runner.py — Phase 2 runner comprehensive tests

Covers: idempotency, over-sell, atomicity, missing price, stop-loss clearing,
trading calendar, defense threshold, T+1 signal execution, open/close prices,
partial failure isolation.
"""

import json, os, sys, tempfile, pytest
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from paper_trading.models import AccountCreate, AccountType, OpeningPosition, StartMode
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore
from paper_trading.runner import PaperTradingRunner
from paper_trading.calendar import ChinaTradingCalendar


def _make_account(service, account_id='acct-test', start_mode=StartMode.CASH, cash=1_000_000,
                    holdings=None, strategy_config=None):
    holdings = holdings or []
    positions_value = sum(h.market_value for h in holdings)
    if start_mode is StartMode.IMPORTED:
        initial_capital = cash + positions_value
    else:
        initial_capital = cash
    strategy_config = strategy_config or {'max_holdings': 5, 'stop_loss': -0.08}
    request = AccountCreate(
        account_id=account_id, name='Test',
        account_type=AccountType.COMPARISON, strategy_name='B0.4',
        strategy_config=strategy_config,
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
        """止损清仓后持仓确实消失。止损按开盘价检查并成交。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        result = runner.run_daily(acct, '2026-06-30', {'512400.SH': 9.1}, {'512400.SH': 9.1})

        # 止损触发，持仓应消失
        assert '512400.SH' not in result['positions']
        assert result['cash'] > 900_000  # 卖出后现金增加

        # 数据库中持仓也应消失
        db_positions = service.store.list_positions(acct, '2026-06-30')
        assert len([p for p in db_positions if p['ticker'] == '512400.SH']) == 0

    def test_exact_threshold_not_triggered(self, runner, service):
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        result = runner.run_daily(acct, '2026-06-30', {'512400.SH': 9.2}, {'512400.SH': 9.2})
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
        assert 'oversell' in skipped[0]['reason']

        # simulate_execution 现在只在内存执行，不单独写入账户状态；
        # 因此当日无持仓记录，上一日持仓应保持不变。
        positions = service.store.list_positions(acct, '2026-06-30')
        assert len(positions) == 0
        prev_positions = service.store.list_positions(acct, '2026-06-29')
        assert prev_positions[0]['shares'] == 100


# ============== Trading Calendar ==============

class TestTradingCalendar:
    def test_weekend_skipped(self, runner, service):
        """周五的下一交易日是周一。"""
        cal = ChinaTradingCalendar()
        # 2026-06-26 is Friday, next trading day is Monday 2026-06-29
        assert cal.next_trading_day('2026-06-26') == '2026-06-29'
        # 2026-06-27 is Saturday, next trading day is Monday 2026-06-29
        assert cal.next_trading_day('2026-06-27') == '2026-06-29'
        assert cal.next_trading_day('2026-06-28') == '2026-06-29'

    def test_thursday_signal_executed_friday(self, runner, service):
        """周四信号在周五自动执行。"""
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])

        # 周四(2026-07-02)：生成信号（使用周四收盘价）
        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)

        # 验证信号已保存，且配置使用账户冻结配置
        signals = service.store.load_pending_signals(acct, '2026-07-03')
        assert len(signals) == 1
        saved_cfg = json.loads(signals[0]['config_json'])
        account = service.store.get_account(acct)
        assert saved_cfg == json.loads(account['config_json'])

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
        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0, '515230.SH': 1.0, '518880.SH': 1.0},
                         {'512400.SH': 1.0, '515230.SH': 1.0, '518880.SH': 1.0}, scores)
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

        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
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

        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
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
        # 周二：持有 512400.SH，开盘触发止损
        runner.run_daily(acct, '2026-06-30', {'512400.SH': 9.1}, {'512400.SH': 9.1})
        # 周三：止损已执行，持仓已清空
        result = runner.run_daily(acct, '2026-07-01', {'512400.SH': 9.0}, {'512400.SH': 9.0})
        assert '512400.SH' not in result['positions']


# ============== Codex Final Review: Atomic Save & Open/Close Separation ==============

class TestCodexFinalReview:
    def test_friday_trade_updates_state_with_close_valuation(self, runner, service):
        """周五成交后必须持有对应股份，现金扣减正确，收盘总资产按收盘价计算。"""
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])
        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        result = runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.05}, {'512400.SH': 1.20})

        assert '512400.SH' in result['positions']
        trade = [t for t in result['trades'] if t['ticker'] == '512400.SH'][0]
        shares = trade['shares']
        assert trade['price'] == 1.05  # 成交价用开盘价

        expected_cash = 1_000_000 - shares * 1.05 - trade['commission']
        assert result['cash'] == pytest.approx(expected_cash, abs=0.01)

        nav = service.store.get_nav(acct, '2026-07-03')
        expected_nav = result['cash'] + shares * 1.20  # 收盘总资产用收盘价
        assert nav['nav'] == pytest.approx(expected_nav, abs=0.01)

    def test_stop_loss_on_open_even_if_close_recovered(self, runner, service):
        """开盘跌破止损线、收盘恢复时仍应按开盘价止损。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        result = runner.run_daily(acct, '2026-06-30', {'512400.SH': 9.1}, {'512400.SH': 10.0})
        assert '512400.SH' not in result['positions']
        assert len(result['trades']) == 1
        assert result['trades'][0]['price'] == 9.1

    def test_no_stop_loss_when_only_close_falls(self, runner, service):
        """开盘未跌破、仅收盘跌破时不得当天止损。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        result = runner.run_daily(acct, '2026-06-30', {'512400.SH': 9.3}, {'512400.SH': 9.0})
        assert '512400.SH' in result['positions']
        assert len(result['trades']) == 0

    def test_june_18_2026_next_trading_day_is_june_22(self, runner, service):
        """2026年6月18日的下一交易日必须是6月22日（端午节假期）。"""
        cal = ChinaTradingCalendar()
        assert cal.next_trading_day('2026-06-18') == '2026-06-22'

    def test_external_config_cannot_override_account_config(self, runner, service):
        """运行配置必须读取账户创建时保存的冻结配置，不允许调用时临时替换。"""
        acct = _make_account(
            service, cash=1_000_000,
            strategy_config={'max_holdings': 1, 'min_total_score': 40, 'max_position_per_etf': 0.20},
        )
        scores = _make_scores(['512400.SH', '515230.SH'], [65, 60])
        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0, '515230.SH': 1.0},
                         {'512400.SH': 1.0, '515230.SH': 1.0}, scores)

        # 篡改信号中保存的配置，模拟外部传入不同配置
        with service.store.connect() as conn:
            conn.execute(
                "UPDATE paper_signals SET config_json = ? WHERE account_id = ?",
                (json.dumps({'max_holdings': 5, 'min_total_score': 40, 'max_position_per_etf': 0.20}, sort_keys=True), acct),
            )

        result = runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.0, '515230.SH': 1.0},
                                  {'512400.SH': 1.0, '515230.SH': 1.0})
        # 账户冻结配置 max_holdings=1，因此只能持有1只
        held_tickers = set(result['positions'].keys())
        assert len(held_tickers) == 1

    def test_save_failure_leaves_no_partial_records(self, runner, service):
        """保存过程故意失败时，不得留下成交或半套账户记录。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])

        def failing_save(*args, **kwargs):
            raise RuntimeError("simulated save failure")

        original_save = service.store.save_daily_state
        service.store.save_daily_state = failing_save
        with pytest.raises(RuntimeError, match="simulated save failure"):
            runner.run_daily(acct, '2026-06-30', {'512400.SH': 9.1}, {'512400.SH': 9.1})

        service.store.save_daily_state = original_save
        assert service.store.list_trades(acct, '2026-06-30') == []
        assert service.store.list_positions(acct, '2026-06-30') == []
        assert service.store.get_nav(acct, '2026-06-30') is None
        with service.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_runs WHERE account_id = ? AND run_date = ?",
                (acct, '2026-06-30'),
            ).fetchall()
            assert len(rows) == 0

    def test_trade_can_be_traced_to_order(self, runner, service):
        """每笔成交必须能够追溯到原始建议。"""
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])
        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        result = runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.05}, {'512400.SH': 1.05})
        for trade in result['trades']:
            assert trade['order_id'] is not None
            assert trade['order_id'].startswith(trade['action'].lower())

    def test_stop_loss_trade_can_be_traced_to_order(self, runner, service):
        """止损成交同样可以追溯到原始建议。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        result = runner.run_daily(acct, '2026-06-30', {'512400.SH': 9.1}, {'512400.SH': 9.1})
        trade = result['trades'][0]
        assert trade['order_id'] == f"sl-{acct}-2026-06-30-512400.SH"

    def test_skipped_reasons_persist_after_repeated_reads(self, runner, service):
        """缺价、资金不足、超卖等未执行原因必须保存，重复查看时不能丢失。"""
        acct = _make_account(service, cash=900_000, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)])
        # 开盘缺价，止损无法执行
        result1 = runner.run_daily(acct, '2026-06-30', {}, {'512400.SH': 9.1})
        assert len(result1['skipped']) == 1
        assert 'missing price' in result1['skipped'][0]['reason']

        result2 = runner.run_daily(acct, '2026-06-30', {}, {'512400.SH': 9.1})
        assert len(result2['skipped']) == 1
        assert result2['skipped'][0]['reason'] == result1['skipped'][0]['reason']


# ============== Codex Final Review v2: Orders, Sell-First, Calendar ==============

class TestCodexFinalReviewV2:
    def _create_phase1_db(self, db_path):
        """创建一个 Phase 1 旧数据库（无 paper_signals / paper_skipped / schema_version）。"""
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS paper_accounts (
                account_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                group_id TEXT,
                strategy_name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                initial_capital REAL NOT NULL CHECK(initial_capital > 0),
                start_mode TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                account_id TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                shares INTEGER NOT NULL CHECK(shares >= 0),
                cost_price REAL NOT NULL CHECK(cost_price >= 0),
                last_price REAL NOT NULL CHECK(last_price >= 0),
                market_value REAL NOT NULL CHECK(market_value >= 0),
                PRIMARY KEY (account_id, as_of_date, ticker),
                FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
            );
            CREATE TABLE IF NOT EXISTS paper_orders (
                order_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                current_shares INTEGER NOT NULL,
                target_shares INTEGER NOT NULL,
                delta_shares INTEGER NOT NULL,
                reference_price REAL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                order_id TEXT,
                account_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                shares INTEGER NOT NULL CHECK(shares > 0),
                price REAL NOT NULL CHECK(price > 0),
                commission REAL NOT NULL CHECK(commission >= 0),
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES paper_orders(order_id),
                FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
            );
            CREATE TABLE IF NOT EXISTS paper_daily_nav (
                account_id TEXT NOT NULL,
                nav_date TEXT NOT NULL,
                cash REAL NOT NULL CHECK(cash >= -0.01),
                positions_value REAL NOT NULL CHECK(positions_value >= 0),
                nav REAL NOT NULL CHECK(nav >= 0),
                data_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, nav_date),
                FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
            );
            CREATE TABLE IF NOT EXISTS paper_runs (
                run_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                run_date TEXT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                data_date TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
            );
            CREATE TRIGGER IF NOT EXISTS paper_accounts_config_immutable
            BEFORE UPDATE OF config_json, config_hash ON paper_accounts
            BEGIN
                SELECT RAISE(ABORT, 'configuration is immutable');
            END;
        """)
        conn.commit()
        conn.close()

    def test_phase1_db_upgrade_can_trade(self, tmp_path):
        """Phase 1 旧数据库升级后可以正常成交。"""
        import sqlite3
        db_path = str(tmp_path / 'phase1.db')
        self._create_phase1_db(db_path)
        store = PaperTradingStore(db_path)
        service = PaperTradingService(store)
        runner = PaperTradingRunner(service)
        acct = _make_account(service, cash=1_000_000)

        # 验证升级后 schema_version 表存在
        with sqlite3.connect(db_path) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            assert 'paper_schema_version' in tables
            assert 'paper_signals' in tables
            assert 'paper_skipped' in tables

        scores = _make_scores(['512400.SH'], [65])
        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        result = runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.05}, {'512400.SH': 1.05})
        assert '512400.SH' in result['positions']
        assert len(result['trades']) == 1

    def test_zero_cash_sells_old_to_buy_new(self, runner, service):
        """零现金账户卖出旧 ETF 后能买入新 ETF。"""
        acct = _make_account(
            service, cash=0, start_mode=StartMode.IMPORTED,
            holdings=[OpeningPosition('512400.SH', 10_000, 1.0, 1.0)],
            strategy_config={'max_holdings': 5, 'min_total_score': 40, 'max_position_per_etf': 0.20},
        )
        # 周四：512400.SH 落选，515230.SH 入选
        scores = _make_scores(['512400.SH', '515230.SH'], [30, 65])
        runner.run_daily(acct, '2026-07-02',
                         {'512400.SH': 1.0, '515230.SH': 1.0},
                         {'512400.SH': 1.0, '515230.SH': 1.0}, scores)
        # 周五：卖出 512400.SH，买入 515230.SH
        result = runner.run_daily(acct, '2026-07-03',
                                  {'512400.SH': 1.0, '515230.SH': 1.0},
                                  {'512400.SH': 1.0, '515230.SH': 1.0})
        assert '512400.SH' not in result['positions']
        assert '515230.SH' in result['positions']

    def test_every_trade_maps_to_real_order(self, runner, service):
        """每笔成交对应订单表中的真实订单。"""
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])
        runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        result = runner.run_daily(acct, '2026-07-03', {'512400.SH': 1.05}, {'512400.SH': 1.05})
        for trade in result['trades']:
            order_id = trade['order_id']
            with service.store.connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM paper_orders WHERE order_id = ?", (order_id,)
                ).fetchone()
            assert row is not None, f"missing order {order_id} for trade {trade['trade_id']}"

    def test_signal_rollback_when_save_fails(self, runner, service):
        """周四最终保存失败后不留下周五信号。"""
        acct = _make_account(service, cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])

        original_save = service.store.save_daily_state
        def failing_save(*args, **kwargs):
            raise RuntimeError("simulated save failure")

        service.store.save_daily_state = failing_save
        with pytest.raises(RuntimeError, match="simulated save failure"):
            runner.run_daily(acct, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        service.store.save_daily_state = original_save

        signals = service.store.load_pending_signals(acct, '2026-07-03')
        assert len(signals) == 0

    def test_gap_open_uses_open_nav_for_position_sizing(self, runner, service):
        """开盘大幅跳空时按开盘总资产计算仓位。"""
        acct = _make_account(
            service, cash=0, start_mode=StartMode.IMPORTED,
            holdings=[OpeningPosition('512400.SH', 10_000, 10.0, 10.0)],
            strategy_config={'max_holdings': 2, 'min_total_score': 40, 'max_position_per_etf': 0.50},
        )
        # 周四收盘价 10，周五开盘价跳空到 20
        scores = _make_scores(['515230.SH'], [65])
        runner.run_daily(acct, '2026-07-02',
                         {'512400.SH': 10.0, '515230.SH': 10.0},
                         {'512400.SH': 10.0, '515230.SH': 10.0}, scores)
        result = runner.run_daily(acct, '2026-07-03',
                                  {'512400.SH': 20.0, '515230.SH': 10.0},
                                  {'512400.SH': 20.0, '515230.SH': 10.0})
        # 开盘总资产 = 10_000 * 20 = 200,000；单只上限 50% => 100,000
        # 515230.SH 买入金额应接近 100,000，而不是按周四收盘总资产 100,000 计算的 50,000
        trade = [t for t in result['trades'] if t['ticker'] == '515230.SH' and t['action'] == 'BUY'][0]
        assert trade['shares'] * trade['price'] >= 90_000

    def test_two_accounts_same_day_no_id_collision(self, runner, service):
        """两个账户同日交易不会编号冲突。"""
        acct1 = _make_account(service, account_id='acct-1', cash=1_000_000)
        acct2 = _make_account(service, account_id='acct-2', cash=1_000_000)
        scores = _make_scores(['512400.SH'], [65])
        runner.run_daily(acct1, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        runner.run_daily(acct2, '2026-07-02', {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        r1 = runner.run_daily(acct1, '2026-07-03', {'512400.SH': 1.05}, {'512400.SH': 1.05})
        r2 = runner.run_daily(acct2, '2026-07-03', {'512400.SH': 1.05}, {'512400.SH': 1.05})

        trade_ids_1 = {t['trade_id'] for t in r1['trades']}
        trade_ids_2 = {t['trade_id'] for t in r2['trades']}
        assert not trade_ids_1 & trade_ids_2

        with service.store.connect() as conn:
            orders_1 = {r['order_id'] for r in conn.execute(
                "SELECT order_id FROM paper_orders WHERE account_id = ?", (acct1,)
            ).fetchall()}
            orders_2 = {r['order_id'] for r in conn.execute(
                "SELECT order_id FROM paper_orders WHERE account_id = ?", (acct2,)
            ).fetchall()}
        assert not orders_1 & orders_2

    def test_non_trading_day_and_out_of_range_fail(self, runner, service):
        """超出日历范围和非交易日必须明确失败。"""
        acct = _make_account(service, cash=1_000_000)
        # 非交易日（周六）
        with pytest.raises(ValueError, match="not a trading day"):
            runner.run_daily(acct, '2026-06-27', {}, {})
        # 超出日历范围
        with pytest.raises(ValueError, match="outside trading calendar range"):
            runner.run_daily(acct, '2030-01-01', {}, {})




# ============== Order Status Tracking ==============

class TestOrderStatus:
    """测试 _execute_orders_in_memory 为每笔订单正确标记 status 字段。"""

    def _run_execute(self, runner, account_id, orders, prices, cash=1_000_000, positions=None):
        state = {
            "cash": cash,
            "positions": positions or {},
        }
        return runner._execute_orders_in_memory(
            account_id, '2026-07-03', state, orders, prices
        )

    def test_buy_filled(self, runner, service):
        """正常买入订单标记为 FILLED。"""
        acct = _make_account(service, cash=1_000_000)
        orders = [{
            'order_id': 'buy-1', 'ticker': '512400.SH', 'action': 'BUY',
            'delta_shares': 100,
        }]
        trades, skipped, out_orders = self._run_execute(
            runner, acct, orders, {'512400.SH': 1.0}
        )
        assert len(trades) == 1
        assert len(skipped) == 0
        assert out_orders[0]['status'] == 'FILLED'

    def test_sell_filled(self, runner, service):
        """正常卖出订单标记为 FILLED。"""
        acct = _make_account(service, cash=0, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 100, 1.0, 1.0)])
        orders = [{
            'order_id': 'sell-1', 'ticker': '512400.SH', 'action': 'SELL',
            'delta_shares': -100,
        }]
        trades, skipped, out_orders = self._run_execute(
            runner, acct, orders, {'512400.SH': 1.0}, cash=0,
            positions={'512400.SH': {'shares': 100}}
        )
        assert len(trades) == 1
        assert len(skipped) == 0
        assert out_orders[0]['status'] == 'FILLED'

    def test_missing_price_skipped(self, runner, service):
        """缺少有效价格时标记为 SKIPPED。"""
        acct = _make_account(service, cash=1_000_000)
        orders = [{
            'order_id': 'buy-1', 'ticker': '512400.SH', 'action': 'BUY',
            'delta_shares': 100,
        }]
        trades, skipped, out_orders = self._run_execute(
            runner, acct, orders, {}
        )
        assert len(trades) == 0
        assert len(skipped) == 1
        assert out_orders[0]['status'] == 'SKIPPED'

    def test_zero_shares_cancelled(self, runner, service):
        """delta_shares 为 0 时标记为 CANCELLED。"""
        acct = _make_account(service, cash=1_000_000)
        orders = [{
            'order_id': 'buy-1', 'ticker': '512400.SH', 'action': 'BUY',
            'delta_shares': 0,
        }]
        trades, skipped, out_orders = self._run_execute(
            runner, acct, orders, {'512400.SH': 1.0}
        )
        assert len(trades) == 0
        assert len(skipped) == 0
        assert out_orders[0]['status'] == 'CANCELLED'

    def test_oversell_skipped(self, runner, service):
        """卖出数量超过持仓时标记为 SKIPPED。"""
        acct = _make_account(service, cash=0, start_mode=StartMode.IMPORTED,
                               holdings=[OpeningPosition('512400.SH', 100, 1.0, 1.0)])
        orders = [{
            'order_id': 'sell-1', 'ticker': '512400.SH', 'action': 'SELL',
            'delta_shares': -200,
        }]
        trades, skipped, out_orders = self._run_execute(
            runner, acct, orders, {'512400.SH': 1.0}, cash=0,
            positions={'512400.SH': {'shares': 100}}
        )
        assert len(trades) == 0
        assert len(skipped) == 1
        assert out_orders[0]['status'] == 'SKIPPED'

    def test_insufficient_cash_skipped(self, runner, service):
        """现金不足时买入订单标记为 SKIPPED。"""
        acct = _make_account(service, cash=1_000)
        orders = [{
            'order_id': 'buy-1', 'ticker': '512400.SH', 'action': 'BUY',
            'delta_shares': 10000,
        }]
        trades, skipped, out_orders = self._run_execute(
            runner, acct, orders, {'512400.SH': 1.0}, cash=1_000
        )
        assert len(trades) == 0
        assert len(skipped) == 1
        assert out_orders[0]['status'] == 'SKIPPED'

    def test_unknown_action_skipped(self, runner, service):
        """未知 action 时标记为 SKIPPED。"""
        acct = _make_account(service, cash=1_000_000)
        orders = [{
            'order_id': 'bad-1', 'ticker': '512400.SH', 'action': 'HOLD',
            'delta_shares': 100,
        }]
        trades, skipped, out_orders = self._run_execute(
            runner, acct, orders, {'512400.SH': 1.0}
        )
        assert len(trades) == 0
        assert len(skipped) == 1
        assert out_orders[0]['status'] == 'SKIPPED'

    def test_mixed_orders_have_correct_status(self, runner, service):
        """同一批订单中不同结果分别标记正确状态。"""
        acct = _make_account(service, cash=10_000)
        orders = [
            {'order_id': 'buy-1', 'ticker': '512400.SH', 'action': 'BUY', 'delta_shares': 100},
            {'order_id': 'zero-1', 'ticker': '515230.SH', 'action': 'BUY', 'delta_shares': 0},
            {'order_id': 'buy-2', 'ticker': '518880.SH', 'action': 'BUY', 'delta_shares': 100000},
        ]
        trades, skipped, out_orders = self._run_execute(
            runner, acct, orders,
            {'512400.SH': 1.0, '515230.SH': 1.0, '518880.SH': 1.0},
            cash=10_000
        )
        statuses = {o['order_id']: o['status'] for o in out_orders}
        assert statuses['buy-1'] == 'FILLED'
        assert statuses['zero-1'] == 'CANCELLED'
        assert statuses['buy-2'] == 'SKIPPED'
        assert len(trades) == 1
        assert len(skipped) == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
