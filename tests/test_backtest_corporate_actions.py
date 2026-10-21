#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_backtest_corporate_actions.py — 回测引擎公司行动同步（Trae ROUND4）。

复现 Trae 第四轮验收问题：虚拟盘已处理拆分，但 BacktestEngine 未同步调整
持仓份额/成本，2026-07-03 512480（1:2 拆分）被误读为 -51% 亏损而错误止损，
组合当日虚假下跌约 9.9%。

本测试用正式库真实价格窗口复现该场景，断言修复后：
1. 拆分日 512480 不触发止损
2. 拆分日 NAV 无虚假跳跌（真实涨跌幅范围内）
"""

import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, build_config
from database import ETFDatabase
from backtest import BacktestEngine

WINDOW_START = "2025-11-01"
WINDOW_END = "2026-07-20"
SPLIT_DATE = "2026-07-03"
SPLIT_TICKER = "512480.SH"


def _load_window():
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(
        ticker=tickers, start_date=WINDOW_START, end_date=WINDOW_END)
    bench_df = db.get_market_data(
        ticker=BENCHMARK, start_date=WINDOW_START, end_date=WINDOW_END)
    return db, market_df, bench_df


def _b04_cfg():
    cfg = build_config()
    cfg['market_timing'] = False
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    return cfg


@pytest.fixture(scope="module")
def backtest_result():
    db, market_df, bench_df = _load_window()
    confirmed = db.get_corporate_actions(status='confirmed')
    actions = confirmed.to_dict('records') if confirmed is not None else None
    engine = BacktestEngine(_b04_cfg())
    result = engine.run(market_df, bench_df, corporate_actions=actions)
    result['_market_df'] = market_df
    return result


class TestBacktestSplitNoFalseStop:
    def test_no_stop_loss_on_split_day(self, backtest_result):
        """512480 拆分日（2026-07-03）不得出现止损交易。"""
        trades = backtest_result['trades_df'].copy()
        trades['date'] = pd.to_datetime(trades['date']).dt.strftime('%Y-%m-%d')
        sl = trades[(trades['date'] == SPLIT_DATE) & (trades['ticker'] == SPLIT_TICKER)
                    & (trades['action'] == 'STOP_LOSS')]
        assert len(sl) == 0, (
            f"拆分日 {SPLIT_TICKER} 出现错误止损:\n"
            f"{sl[['date', 'ticker', 'price', 'reason']].to_string(index=False)}"
        )

    def test_no_fake_nav_drop_on_split_day(self, backtest_result):
        """拆分日 NAV 日收益不得出现拆分价差造成的虚假大跌（<-5%）。"""
        nav = backtest_result['nav_df'].copy()
        nav['date'] = pd.to_datetime(nav['date']).dt.strftime('%Y-%m-%d')
        nav = nav.sort_values('date')
        nav['daily_ret'] = nav['nav'].pct_change()
        row = nav[nav['date'] == SPLIT_DATE]
        assert not row.empty, f"NAV 序列缺少 {SPLIT_DATE}"
        ret = row['daily_ret'].iloc[0]
        assert ret > -0.05, (
            f"{SPLIT_DATE} NAV 日收益 {ret:.2%}，疑似拆分价差未同步的虚假下跌"
        )


class TestBacktestSplitPositionChain:
    """加强验证（ROUND5 意见三）：拆分前持仓 → 拆分后股数/成本 → 资产金额全链条。

    512480 真实链条（本夹具窗口 2025-11-01 起）：
      06-25 买入 N 股 @2.700（拆前持仓，N 由仓位 sizing 决定）
      07-03 拆分 1:2 → 2N 股、成本 2.700→1.350（股数×2、成本÷2）
      07-03 NAV 连续无跳变（资产金额不变）
      07-16 真实下跌触发止损（正常涨跌仍可止损，股数=2N）
    断言一律以夹具内实际买入值为基准做倍数/比例校验，不硬编码股数。
    """

    def _last_pre_split_buy(self, backtest_result):
        trades = backtest_result['trades_df'].copy()
        trades['date'] = pd.to_datetime(trades['date']).dt.strftime('%Y-%m-%d')
        buy = trades[(trades['ticker'] == SPLIT_TICKER) & (trades['action'] == 'BUY')
                     & (trades['date'] < SPLIT_DATE)].sort_values('date')
        return trades, buy

    def test_pre_split_position_exists(self, backtest_result):
        """拆分前必须存在拆前持仓：06-25 最后一次买入 @2.700（真实价）。"""
        _, buy = self._last_pre_split_buy(backtest_result)
        assert not buy.empty, "拆分前无 512480 持仓，场景前提不成立"
        last_buy = buy.iloc[-1]
        assert last_buy['date'] == '2026-06-25'
        assert last_buy['shares'] > 0
        assert last_buy['price'] == pytest.approx(2.700, abs=1e-9), (
            f"拆前买入价 {last_buy['price']} != 2.700（拆分前真实价）"
        )

    def test_post_split_shares_doubled_and_cost_halved(self, backtest_result):
        """拆分后止损股数必须 = 夹具内拆前股数 × 2；止损价相对拆后成本
        (2.700/2=1.350) 的盈亏必须与止损理由记录值一致（成本÷2 生效）。"""
        trades, buy = self._last_pre_split_buy(backtest_result)
        pre_shares = float(buy.iloc[-1]['shares'])
        sl = trades[(trades['ticker'] == SPLIT_TICKER) & (trades['action'] == 'STOP_LOSS')
                    & (trades['date'] > SPLIT_DATE)].sort_values('date')
        assert not sl.empty, "拆分后应出现一次真实下跌止损（正常涨跌仍可止损）"
        first_sl = sl.iloc[0]
        assert first_sl['shares'] == pytest.approx(pre_shares * 2, rel=1e-6), (
            f"拆分后止损股数 {first_sl['shares']:.0f} != 拆前 {pre_shares:.0f} ×2，股数未同步"
        )
        # 成本 ÷2：止损价 1.191 / 拆后成本 1.350 - 1 ≈ -11.8%（拆前口径则 ≈ -56%）
        adj_cost = 2.700 / 2.0
        pnl = first_sl['price'] / adj_cost - 1
        assert -0.20 < pnl < -0.05, (
            f"止损价 {first_sl['price']:.3f} 相对拆后成本 {adj_cost:.3f} 为 {pnl:.2%}，"
            "不在 (-20%, -5%) 区间，成本可能未按拆后口径调整"
        )
        # 止损理由中的成本盈亏百分比必须解析得出且与计算一致
        import re
        m = re.search(r'成本盈亏(-?\d+\.\d+)%', str(first_sl['reason']))
        assert m, f"止损理由缺少成本盈亏: {first_sl['reason']}"
        reason_pnl = float(m.group(1)) / 100
        assert reason_pnl == pytest.approx(pnl, abs=0.01), (
            f"理由记录 {reason_pnl:.2%} 与按拆后成本计算 {pnl:.2%} 不一致"
        )

    def test_asset_amount_continuous_across_split(self, backtest_result):
        """拆分日 NAV 必须与前一交易日基本连续（资产金额不因拆分跳变）。

        容忍 ±1%：拆分日本身无交易，NAV 变化只反映当日真实涨跌。
        """
        nav = backtest_result['nav_df'].copy()
        nav['date'] = pd.to_datetime(nav['date']).dt.strftime('%Y-%m-%d')
        nav = nav.sort_values('date').reset_index(drop=True)
        i = nav.index[nav['date'] == SPLIT_DATE]
        assert len(i), f"NAV 序列缺少 {SPLIT_DATE}"
        i = i[0]
        before = nav.loc[i - 1, 'nav']
        after = nav.loc[i, 'nav']
        change = after / before - 1
        assert abs(change) < 0.01, (
            f"拆分日 NAV 变化 {change:+.2%} 超过 ±1%，资产金额疑似因拆分跳变"
        )


class TestPendingProtectionLive:
    """pending 保护在回测引擎真实生效（ROUND5 安全处理新语义）。

    pending 且涉及当前持仓：回测必须整体暂停（抛 PendingCorporateActionError），
    不得输出任何收益/交易——而不是旧语义的"跳过止损继续跑"。
    """

    @pytest.fixture(scope="module")
    def pending_actions(self):
        return [{
            "ticker": SPLIT_TICKER, "effective_date": SPLIT_DATE,
            "action_type": "split", "ratio": 2.0, "status": "pending",
        }]

    def test_pending_held_position_pauses_backtest(self, pending_actions):
        """pending 拆分涉及持仓 → 回测抛异常暂停，不输出结果。"""
        from backtest import PendingCorporateActionError
        db, market_df, bench_df = _load_window()
        engine = BacktestEngine(_b04_cfg())
        with pytest.raises(PendingCorporateActionError) as excinfo:
            engine.run(market_df, bench_df, corporate_actions=pending_actions)
        msg = str(excinfo.value)
        assert SPLIT_TICKER in msg and SPLIT_DATE in msg
        assert "待确认" in msg

    def test_pending_not_held_suspends_new_buys(self):
        """pending 拆分未涉及持仓 → 该标的新买入资格暂停直到确认。

        用一个窗口内本来会被买入的非持仓标的验证：屏蔽其买入后，
        后续日期不得出现该标的的 BUY 交易。
        """
        from backtest import PendingCorporateActionError
        db, market_df, bench_df = _load_window()
        # 找窗口内首次被买入的标的（此时必未持仓），把它的拆分行动置为 pending
        engine_probe = BacktestEngine(_b04_cfg())
        probe = engine_probe.run(market_df, bench_df, corporate_actions=None)
        buys = probe['trades_df'][probe['trades_df']['action'] == 'BUY'].copy()
        buys['date'] = pd.to_datetime(buys['date']).dt.strftime('%Y-%m-%d')
        first_buy = buys.sort_values('date').iloc[0]
        ticker, buy_date = first_buy['ticker'], first_buy['date']
        pending = [{
            "ticker": ticker, "effective_date": buy_date,
            "action_type": "split", "ratio": 2.0, "status": "pending",
        }]
        # 拆分日在买入日之前：pending 生效后该标的一直被禁买
        engine = BacktestEngine(_b04_cfg())
        result = engine.run(market_df, bench_df, corporate_actions=pending)
        trades = result['trades_df'].copy()
        trades['date'] = pd.to_datetime(trades['date']).dt.strftime('%Y-%m-%d')
        buys_after = trades[(trades['ticker'] == ticker) & (trades['action'] == 'BUY')
                            & (trades['date'] >= buy_date)]
        assert buys_after.empty, (
            f"pending 拆分标的 {ticker} 在 {buy_date} 之后仍被买入："
            f"新买入资格暂停未生效")

    def test_paused_run_produces_no_output(self, pending_actions):
        """暂停的回测不得产生任何可消费输出（防御性：异常即中断）。"""
        from backtest import PendingCorporateActionError
        db, market_df, bench_df = _load_window()
        engine = BacktestEngine(_b04_cfg())
        with pytest.raises(PendingCorporateActionError):
            engine.run(market_df, bench_df, corporate_actions=pending_actions)


class TestEntryPointsPassAllActions:
    """入口接线验证（ROUND5 意见二）：页面与 CLI 必须传全量公司行动记录
    （含 pending），而不是只传 confirmed——否则 pending 保护是死代码。"""

    def test_cli_passes_all_actions(self, monkeypatch):
        """拦截 cmd_backtest 的数据加载与引擎，验证传入的 corporate_actions
        是全量记录（与 db.get_corporate_actions() 无过滤结果一致）。"""
        import main as main_mod

        db = ETFDatabase()
        full_records = db.get_corporate_actions().to_dict('records')
        assert len(full_records) > 0

        captured = {}
        tickers = ['512400.SH']
        fake_market = db.get_market_data(
            ticker=tickers, start_date=WINDOW_START, end_date=WINDOW_END)
        fake_bench = db.get_market_data(
            ticker=BENCHMARK, start_date=WINDOW_START, end_date=WINDOW_END)

        monkeypatch.setattr(main_mod.ETFDatabase, 'get_market_data',
                            lambda self, **kw: fake_market if 'ticker' in kw and kw.get('ticker') != BENCHMARK else fake_bench)
        monkeypatch.setattr(main_mod.ETFDatabase, 'save_backtest_result',
                            lambda self, *a, **kw: None)

        def fake_run(self, market_df, bench_df, **kw):
            captured['ca_records'] = kw.get('corporate_actions')
            nav = fake_bench.copy()
            nav['date'] = pd.to_datetime(nav['date'])
            nav['nav'] = nav['close']
            return {
                'nav_df': nav,
                'trades_df': pd.DataFrame(),
                'total_return': 0, 'annual_return': 0, 'volatility': 0,
                'sharpe_ratio': 0, 'sortino_ratio': 0,
                'max_drawdown': 0, 'num_trades': 0, 'win_rate': 0,
                'avg_win': 0, 'avg_loss': 0, 'total_commission': 0,
                'stop_loss_count': 0, 'avg_holdings': 0, 'max_holdings': 0,
                'params': {},
            }
        monkeypatch.setattr(main_mod.BacktestEngine, 'run', fake_run)

        class A:
            sample = 'full'
            save = False
        main_mod.cmd_backtest(A())

        assert captured.get('ca_records') is not None, "cmd_backtest 未传公司行动"
        got = {(r['ticker'], str(r['effective_date'])[:10]) for r in captured['ca_records']}
        want = {(r['ticker'], str(r['effective_date'])[:10]) for r in full_records}
        assert got == want, (
            f"CLI 传入记录与全量不一致: 缺 {want - got} 多 {got - want}"
        )

    def test_page_passes_all_actions(self, monkeypatch):
        """拦截 run_weighted_backtest，验证页面路径同样传全量记录。"""
        sys.path.insert(0, ROOT)
        import app as app_mod

        db = ETFDatabase()
        full_records = db.get_corporate_actions().to_dict('records')

        captured = {}
        fake_market = db.get_market_data(
            ticker=['512400.SH'], start_date=WINDOW_START, end_date=WINDOW_END)
        fake_bench = db.get_market_data(
            ticker=BENCHMARK, start_date=WINDOW_START, end_date=WINDOW_END)
        monkeypatch.setattr(app_mod.ETFDatabase, 'get_market_data',
                            lambda self, **kw: fake_market if kw.get('ticker') != BENCHMARK else fake_bench)

        def fake_run(self, market_df, bench_df, **kw):
            captured['ca_records'] = kw.get('corporate_actions')
            nav = fake_bench.copy()
            nav['date'] = pd.to_datetime(nav['date'])
            nav['nav'] = nav['close']
            return {
                'nav_df': nav,
                'trades_df': pd.DataFrame(),
                'total_return': 0, 'annual_return': 0, 'volatility': 0,
                'sharpe_ratio': 0, 'sortino_ratio': 0,
                'max_drawdown': 0, 'num_trades': 0, 'win_rate': 0,
                'avg_win': 0, 'avg_loss': 0, 'total_commission': 0,
                'stop_loss_count': 0, 'avg_holdings': 0, 'max_holdings': 0,
                'params': {},
            }
        monkeypatch.setattr(app_mod.BacktestEngine, 'run', fake_run)

        app_mod.run_weighted_backtest(_b04_cfg(), "全区间")

        assert captured.get('ca_records') is not None, "页面回测未传公司行动"
        got = {(r['ticker'], str(r['effective_date'])[:10]) for r in captured['ca_records']}
        want = {(r['ticker'], str(r['effective_date'])[:10]) for r in full_records}
        assert got == want, (
            f"页面传入记录与全量不一致: 缺 {want - got} 多 {got - want}"
        )
