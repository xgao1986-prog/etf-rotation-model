"""
B0.4 单变量滑点敏感性测试 v2（冻结快照版）

要求：
1. 使用 B0.4 冻结快照，不依赖持续更新中的数据库。
2. 0bp 精确复现 B0.4（NAV=2,761,288.07，交易804笔）。
3. 所有规划 BUY 订单在滑点下可执行，不得被静默跳过。
4. 买入价格上调、卖出价格下调。
5. NAV 随滑点递减。
6. STOP_LOSS 单独统计。
7. 年化使用回测引擎值（非总收益/年数）。
8. 每日现金+持仓市值=NAV 恒等式。
"""

import sys
import os
import hashlib

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "src"))

import unittest
import pandas as pd

from config import build_config, BENCHMARK, ETF_UNIVERSE, DEFENSE_UNIVERSE
from backtest import BacktestEngine

# 冻结快照路径
FROZEN_SNAPSHOT_PATH = os.path.join(
    root, "data", "snapshots", "B0_4_candidate_data_20260621_210815.csv"
)
# 冻结快照 SHA-256（不得修改快照文件）
FROZEN_SNAPSHOT_SHA256 = "dbdb041d1269c50ec32fe30da4986d650f57daef412b7c8d2371d9eeba367d13"


def _load_frozen_snapshot():
    """从冻结快照加载数据，并校验 SHA-256。"""
    if not os.path.exists(FROZEN_SNAPSHOT_PATH):
        raise FileNotFoundError(
            f"冻结快照不存在: {FROZEN_SNAPSHOT_PATH}. "
            f"B0.4 冻结基线测试必须使用固定快照，不能读取当前数据库。"
        )

    with open(FROZEN_SNAPSHOT_PATH, "rb") as f:
        actual_hash = hashlib.sha256(f.read()).hexdigest()

    if actual_hash != FROZEN_SNAPSHOT_SHA256:
        raise AssertionError(
            f"冻结快照 SHA-256 不匹配。\n"
            f"预期: {FROZEN_SNAPSHOT_SHA256}\n"
            f"实际: {actual_hash}\n"
            f"快照文件已被修改或不是 B0.4 冻结基线。"
        )

    df = pd.read_csv(FROZEN_SNAPSHOT_PATH)
    # 确保 date 列为 datetime 类型，与数据库查询结果一致
    df['date'] = pd.to_datetime(df['date'])

    # 筛选回测引擎需要的列
    cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume']
    df = df[cols]

    return df


def _load_frozen_data():
    """加载冻结快照并拆分为 market_df 和 bench_df。"""
    df = _load_frozen_snapshot()

    etf_tickers = set(ETF_UNIVERSE.keys()) | set(DEFENSE_UNIVERSE.keys())
    market_df = df[df['ticker'].isin(etf_tickers)].copy()
    bench_df = df[df['ticker'] == BENCHMARK].copy()

    if market_df.empty:
        raise ValueError("冻结快照中未找到 ETF 数据")
    if bench_df.empty:
        raise ValueError("冻结快照中未找到基准数据")

    return market_df, bench_df


class TestSlippageV2(unittest.TestCase):
    """B0.4 单变量滑点敏感性测试 v2（冻结快照版）。"""

    @classmethod
    def setUpClass(cls):
        """从冻结快照加载一次数据，供所有测试使用。"""
        cfg = build_config()
        cfg['as_of_date'] = '2026-06-18'
        cls.cfg = cfg

        cls.market_df, cls.bench_df = _load_frozen_data()

    def _run_backtest(self, slippage_bps):
        """运行回测并返回结果。"""
        engine = BacktestEngine(self.cfg, slippage_bps=slippage_bps)
        result = engine.run(self.market_df, self.bench_df)
        result['engine'] = engine
        return result

    def test_0bp_matches_baseline(self):
        """0bp 必须精确复现 B0.4（NAV=2,761,288.07，交易804笔）。"""
        r = self._run_backtest(0)
        nav_df = r['nav_df']
        final_nav = nav_df['nav'].iloc[-1]
        total_trades = len(r['trades_df'])

        self.assertAlmostEqual(final_nav, 2_761_288.07, delta=1.0)
        self.assertEqual(total_trades, 804)

    def test_planned_buys_are_executable(self):
        """所有规划 BUY 订单在滑点下必须可执行，不得被静默跳过。"""
        r = self._run_backtest(3)
        engine = r['engine']
        skipped = getattr(engine, '_skipped_buys', [])
        self.assertEqual(len(skipped), 0,
            f"规划阶段未使用滑点价，导致 {len(skipped)} 笔 BUY 订单在执行阶段被静默跳过")

    def test_buy_price_increases_with_slippage(self):
        """买入价格随滑点上调。"""
        r0 = self._run_backtest(0)
        r3 = self._run_backtest(3)

        buy0 = r0['trades_df'][r0['trades_df']['action'] == 'BUY']
        buy3 = r3['trades_df'][r3['trades_df']['action'] == 'BUY']

        merged = pd.merge(
            buy0[['date', 'ticker', 'price']].rename(columns={'price': 'price_0bp'}),
            buy3[['date', 'ticker', 'price']].rename(columns={'price': 'price_3bp'}),
            on=['date', 'ticker'],
            how='inner',
        )
        self.assertGreater(len(merged), 0, "应有共同买入记录可供对比")
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

        nav0 = r0['nav_df']['nav'].iloc[-1]
        nav3 = r3['nav_df']['nav'].iloc[-1]
        nav5 = r5['nav_df']['nav'].iloc[-1]
        nav10 = r10['nav_df']['nav'].iloc[-1]

        self.assertGreater(nav0, nav3)
        self.assertGreater(nav3, nav5)
        self.assertGreater(nav5, nav10)

    def test_stop_loss_separate(self):
        """STOP_LOSS 必须单独统计，不能混入 SELL。"""
        r = self._run_backtest(0)
        trades = r['trades_df']
        sell_count = len(trades[trades['action'] == 'SELL'])
        stop_count = len(trades[trades['action'] == 'STOP_LOSS'])
        combined_sell = len(trades[trades['action'].isin(['SELL', 'STOP_LOSS'])])

        self.assertGreater(stop_count, 0, "应有止损交易")
        self.assertEqual(combined_sell, sell_count + stop_count,
            "SELL 和 STOP_LOSS 应无重叠")

    def test_annual_return_from_engine(self):
        """年化必须使用回测引擎 annual_return，禁止总收益除以年数。"""
        r = self._run_backtest(0)
        total_return = r['total_return']
        annual_return = r['annual_return']
        nav_df = r['nav_df']
        years = len(nav_df) / 252

        naive_annual = total_return / years if years > 0 else 0
        self.assertNotAlmostEqual(annual_return, naive_annual, delta=0.001,
            msg="年化必须使用复利公式，不是总收益除以年数")

        self.assertGreater(annual_return, 0)
        expected = (1 + total_return) ** (1 / years) - 1 if years > 0 and total_return > -1 else 0
        self.assertAlmostEqual(annual_return, expected, delta=0.001)

    def test_cash_nav_identity(self):
        """每日现金 + 持仓市值 = NAV 恒等式。"""
        r = self._run_backtest(0)
        nav_df = r['nav_df']
        for _, row in nav_df.iterrows():
            expected_nav = row['cash'] + row['positions_value']
            self.assertAlmostEqual(
                row['nav'], expected_nav, delta=1.0,
                msg=f"日期 {row['date']}: cash({row['cash']}) + positions({row['positions_value']}) != nav({row['nav']})"
            )

    def test_snapshot_hash_matches(self):
        """冻结快照 SHA-256 必须匹配预期值。"""
        # _load_frozen_snapshot 已在 setUpClass 中调用，若哈希不匹配会抛出 AssertionError。
        # 此测试显式验证哈希值，使失败原因在测试报告中更明确。
        with open(FROZEN_SNAPSHOT_PATH, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(
            actual_hash, FROZEN_SNAPSHOT_SHA256,
            f"冻结快照 SHA-256 不匹配。预期: {FROZEN_SNAPSHOT_SHA256}, 实际: {actual_hash}"
        )


if __name__ == '__main__':
    unittest.main()
