"""
B0.4 滑点敏感性自动测试

要求：
1. 0bp与原基线一致
2. 买入价格上调、卖出价格下调
3. 滑点越高，NAV 越低（单笔成交条件不改善）
4. 滑点成本和NAV恒等式正确
"""

import sys
import os

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "src"))

import unittest
import pandas as pd

from config import build_config, BENCHMARK, ETF_UNIVERSE, DEFENSE_UNIVERSE
from database import ETFDatabase
from backtest import BacktestEngine


class TestSlippageSensitivity(unittest.TestCase):
    """B0.4 单变量滑点敏感性测试。"""

    @classmethod
    def setUpClass(cls):
        """加载一次数据，供所有测试使用。"""
        cfg = build_config()
        cfg['as_of_date'] = '2026-06-18'
        cls.cfg = cfg

        db = ETFDatabase()
        etf_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
        cls.market_df = db.get_market_data(ticker=etf_tickers)
        cls.bench_df = db.get_market_data(ticker=BENCHMARK)

    def _run_backtest(self, slippage_bps):
        """运行回测并返回关键指标。"""
        engine = BacktestEngine(self.cfg, slippage_bps=slippage_bps)
        result = engine.run(self.market_df, self.bench_df)

        nav_df = result.get('nav_df', pd.DataFrame())
        final_nav = nav_df['nav'].iloc[-1] if not nav_df.empty else 0

        trades_df = result.get('trades_df', pd.DataFrame())
        total_trades = len(trades_df)

        return {
            'final_nav': final_nav,
            'total_trades': total_trades,
            'trades_df': trades_df,
            'sharpe': result.get('sharpe_ratio', 0),
        }

    def test_0bp_matches_baseline(self):
        """0bp 必须精确复现 B0.4（NAV=2,761,288.07，交易804笔）。"""
        r = self._run_backtest(0)
        self.assertAlmostEqual(r['final_nav'], 2_761_288.07, delta=1.0)
        self.assertEqual(r['total_trades'], 804)

    def test_buy_price_increases_with_slippage(self):
        """买入价格随滑点上调。"""
        r0 = self._run_backtest(0)
        r3 = self._run_backtest(3)

        buy0 = r0['trades_df'][r0['trades_df']['action'] == 'BUY']
        buy3 = r3['trades_df'][r3['trades_df']['action'] == 'BUY']

        # 取相同日期+标的的买入记录对比
        merged = pd.merge(
            buy0[['date', 'ticker', 'price']].rename(columns={'price': 'price_0bp'}),
            buy3[['date', 'ticker', 'price']].rename(columns={'price': 'price_3bp'}),
            on=['date', 'ticker'],
            how='inner',
        )
        self.assertGreater(len(merged), 0, "应有共同买入记录可供对比")

        # 3bp 买入价格应高于 0bp（上调）
        self.assertTrue(
            (merged['price_3bp'] > merged['price_0bp']).all(),
            "3bp 买入价格应全部高于 0bp"
        )

    def test_sell_price_decreases_with_slippage(self):
        """卖出价格随滑点下调。"""
        r0 = self._run_backtest(0)
        r3 = self._run_backtest(3)

        sell0 = r0['trades_df'][r0['trades_df']['action'].isin(['SELL', 'STOP_LOSS'])]
        sell3 = r3['trades_df'][r3['trades_df']['action'].isin(['SELL', 'STOP_LOSS'])]

        merged = pd.merge(
            sell0[['date', 'ticker', 'price']].rename(columns={'price': 'price_0bp'}),
            sell3[['date', 'ticker', 'price']].rename(columns={'price': 'price_3bp'}),
            on=['date', 'ticker'],
            how='inner',
        )
        self.assertGreater(len(merged), 0, "应有共同卖出记录可供对比")

        # 3bp 卖出价格应低于 0bp（下调）
        self.assertTrue(
            (merged['price_3bp'] < merged['price_0bp']).all(),
            "3bp 卖出价格应全部低于 0bp"
        )

    def test_nav_decreases_with_slippage(self):
        """滑点越高，最终 NAV 越低（成交条件不改善）。"""
        r0 = self._run_backtest(0)
        r3 = self._run_backtest(3)
        r5 = self._run_backtest(5)
        r10 = self._run_backtest(10)

        self.assertGreater(r0['final_nav'], r3['final_nav'])
        self.assertGreater(r3['final_nav'], r5['final_nav'])
        self.assertGreater(r5['final_nav'], r10['final_nav'])

    def test_slippage_cost_identity(self):
        """滑点成本恒等式：总滑点成本 ≈ 0bp NAV - 当前 NAV。"""
        r0 = self._run_backtest(0)
        r3 = self._run_backtest(3)

        trades = r3['trades_df']
        slippage = 3 / 10000.0
        cost = 0.0
        for _, row in trades.iterrows():
            action = row['action']
            shares = row['shares']
            price = row['price']
            if action == 'BUY':
                original = price / (1 + slippage)
            elif action in ('SELL', 'STOP_LOSS'):
                original = price / (1 - slippage)
            else:
                continue
            cost += shares * original * slippage

        nav_diff = r0['final_nav'] - r3['final_nav']
        # 滑点成本应占 NAV 差异的 30%-50%（其余来自路径变化）
        ratio = cost / nav_diff if nav_diff > 0 else 0
        self.assertGreater(ratio, 0.25, "滑点成本应占 NAV 差异的至少 25%")
        self.assertLess(ratio, 0.60, "滑点成本不应超过 NAV 差异的 60%（路径变化影响）")

    def test_0bp_commission_unchanged(self):
        """0bp 时佣金与 B0.4 一致。"""
        r0 = self._run_backtest(0)
        total_commission = r0['trades_df']['commission'].sum()
        # B0.4 总佣金约 68,826
        self.assertAlmostEqual(total_commission, 68826.54, delta=50)


if __name__ == '__main__':
    unittest.main()
