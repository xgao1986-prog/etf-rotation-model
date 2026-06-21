"""
Phase 6.8 覆盖分析完整性测试。

测试 has_complete_data() 和 get_bench_trading_dates() 的基准交易日对齐逻辑。
"""

import sys
sys.path.insert(0, r'D:\etf_rotation_model')
sys.path.insert(0, r'D:\etf_rotation_model\src')

import pandas as pd
import numpy as np
import pytest

from scripts.phase6_8_structural_bull_attribution import (
    get_bench_trading_dates,
    has_complete_data,
)


class TestGetBenchTradingDates:
    """测试 get_bench_trading_dates：必须与基准交易日对齐"""

    def test_normal_range(self):
        """正常区间：返回基准的首个和末个真实交易日"""
        bench_df = pd.DataFrame({
            'date': pd.to_datetime(['2020-10-09', '2020-10-12', '2020-10-13', '2020-10-14']),
            'close': [1.0, 1.01, 1.02, 1.03],
        })
        first, last = get_bench_trading_dates(bench_df, '2020-10-09', '2020-10-14')
        assert first == pd.Timestamp('2020-10-09')
        assert last == pd.Timestamp('2020-10-14')

    def test_weekend_end(self):
        """区间结束日为周末：应返回最后一个周五"""
        bench_df = pd.DataFrame({
            'date': pd.to_datetime(['2020-10-09', '2020-10-12', '2020-10-13']),
            'close': [1.0, 1.01, 1.02],
        })
        # 2020-10-18 是周日，但基准数据只到 10-13（周二）
        first, last = get_bench_trading_dates(bench_df, '2020-10-09', '2020-10-18')
        assert first == pd.Timestamp('2020-10-09')
        assert last == pd.Timestamp('2020-10-13')

    def test_insufficient_data(self):
        """基准数据不足（少于2条）：返回 None, None"""
        bench_df = pd.DataFrame({
            'date': pd.to_datetime(['2020-10-09']),
            'close': [1.0],
        })
        first, last = get_bench_trading_dates(bench_df, '2020-10-09', '2020-10-14')
        assert first is None
        assert last is None


class TestHasCompleteData:
    """测试 has_complete_data：必须与基准交易日对齐，不能检查ETF自身窗口"""

    def test_complete_data(self):
        """首尾完整：ETF在基准首个和末个交易日都有有效价格"""
        market_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 3,
            'date': pd.to_datetime(['2020-10-09', '2020-10-12', '2020-10-13']),
            'close': [1.0, 1.05, 1.10],
        })
        result = has_complete_data(market_df, 'ETF_A', pd.Timestamp('2020-10-09'), pd.Timestamp('2020-10-13'))
        assert result is True

    def test_mid_listed(self):
        """中途上市：首个数据日晚于基准首个交易日 → False"""
        market_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 2,
            'date': pd.to_datetime(['2020-10-12', '2020-10-13']),
            'close': [1.0, 1.05],
        })
        # ETF 10-12 才上市，基准首个交易日是 10-09
        result = has_complete_data(market_df, 'ETF_A', pd.Timestamp('2020-10-09'), pd.Timestamp('2020-10-13'))
        assert result is False

    def test_missing_start_price(self):
        """缺基准起点价格：ETF在基准首个交易日无数据 → False"""
        market_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 2,
            'date': pd.to_datetime(['2020-10-12', '2020-10-13']),
            'close': [1.0, 1.05],
        })
        # 基准首个交易日是 10-09，但 ETF 10-09 无数据（虽然 10-12 有数据）
        # 注意：这里 first_date = 10-12，如果 first_bench = 10-09，那么 first_date > first_bench → False
        # 同时也测试 ETF 10-09 缺数据但 first_date <= first_bench 的情况
        result = has_complete_data(market_df, 'ETF_A', pd.Timestamp('2020-10-09'), pd.Timestamp('2020-10-13'))
        assert result is False

    def test_missing_end_price(self):
        """缺基准终点价格：ETF在基准末个交易日无数据 → False"""
        market_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 2,
            'date': pd.to_datetime(['2020-10-09', '2020-10-12']),
            'close': [1.0, 1.05],
        })
        # 基准末个交易日是 10-13，但 ETF 10-13 无数据
        result = has_complete_data(market_df, 'ETF_A', pd.Timestamp('2020-10-09'), pd.Timestamp('2020-10-13'))
        assert result is False

    def test_na_start_price(self):
        """基准起点价格为NaN → False"""
        market_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 3,
            'date': pd.to_datetime(['2020-10-09', '2020-10-12', '2020-10-13']),
            'close': [np.nan, 1.05, 1.10],
        })
        result = has_complete_data(market_df, 'ETF_A', pd.Timestamp('2020-10-09'), pd.Timestamp('2020-10-13'))
        assert result is False

    def test_na_end_price(self):
        """基准终点价格为NaN → False"""
        market_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 3,
            'date': pd.to_datetime(['2020-10-09', '2020-10-12', '2020-10-13']),
            'close': [1.0, 1.05, np.nan],
        })
        result = has_complete_data(market_df, 'ETF_A', pd.Timestamp('2020-10-09'), pd.Timestamp('2020-10-13'))
        assert result is False

    def test_not_aligned_with_bench(self):
        """
        关键测试：ETF自身窗口的第一条/最后一条记录与基准交易日不同。
        
        场景：基准交易日是 10-09 和 10-13，ETF 在 10-09 无数据（休市），
        但 ETF 自身窗口（>=10-09 且 <=10-13）的第一条是 10-12。
        如果按 ETF 自身窗口检查，会认为 10-12 是起点，这是错误的。
        必须与基准交易日对齐：10-09 必须有数据，否则排除。
        """
        market_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 2,
            'date': pd.to_datetime(['2020-10-12', '2020-10-13']),
            'close': [1.0, 1.05],
        })
        # 基准首个交易日 10-09，ETF 10-09 无数据
        result = has_complete_data(market_df, 'ETF_A', pd.Timestamp('2020-10-09'), pd.Timestamp('2020-10-13'))
        assert result is False

    def test_etf_first_before_bench(self):
        """ETF首个数据早于基准首个交易日：只要基准交易日有数据即可 → True"""
        market_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 4,
            'date': pd.to_datetime(['2020-10-08', '2020-10-09', '2020-10-12', '2020-10-13']),
            'close': [0.99, 1.0, 1.05, 1.10],
        })
        result = has_complete_data(market_df, 'ETF_A', pd.Timestamp('2020-10-09'), pd.Timestamp('2020-10-13'))
        assert result is True


class TestCoverageIntegration:
    """集成测试：覆盖分析中完整性与基准交易日的对齐"""

    def test_coverage_excludes_mid_listed(self):
        """中途上市ETF即使涨幅最高也必须被排除"""
        # 模拟：中途上市的ETF_A涨幅100%，但基准交易日是10-09和10-13
        # ETF_A 10-12才上市，不应参与排名
        coverage_df = pd.DataFrame({
            'ticker': ['ETF_A'] * 2 + ['ETF_B'] * 4,
            'date': pd.to_datetime(['2020-10-12', '2020-10-13'] * 1 + ['2020-10-09', '2020-10-12', '2020-10-13', '2020-10-14'] * 1),
            'close': [1.0, 2.0, 1.0, 1.01, 1.02, 1.03],  # ETF_A +100%
        })
        # 修正上面的DataFrame构造
        coverage_df = pd.DataFrame([
            {'ticker': 'ETF_A', 'date': '2020-10-12', 'close': 1.0},
            {'ticker': 'ETF_A', 'date': '2020-10-13', 'close': 2.0},  # +100%
            {'ticker': 'ETF_B', 'date': '2020-10-09', 'close': 1.0},
            {'ticker': 'ETF_B', 'date': '2020-10-12', 'close': 1.01},
            {'ticker': 'ETF_B', 'date': '2020-10-13', 'close': 1.02},
            {'ticker': 'ETF_B', 'date': '2020-10-14', 'close': 1.03},
        ])
        coverage_df['date'] = pd.to_datetime(coverage_df['date'])
        
        bench_df = pd.DataFrame({
            'date': pd.to_datetime(['2020-10-09', '2020-10-12', '2020-10-13', '2020-10-14']),
            'close': [1.0, 1.01, 1.02, 1.03],
        })
        
        first_bench, last_bench = get_bench_trading_dates(bench_df, '2020-10-09', '2020-10-14')
        
        # ETF_A 中途上市，应被排除
        assert has_complete_data(coverage_df, 'ETF_A', first_bench, last_bench) is False
        # ETF_B 完整参与
        assert has_complete_data(coverage_df, 'ETF_B', first_bench, last_bench) is True

    def test_coverage_excludes_missing_end(self):
        """缺少基准终点价格的ETF必须被排除"""
        coverage_df = pd.DataFrame([
            {'ticker': 'ETF_A', 'date': '2020-10-09', 'close': 1.0},
            {'ticker': 'ETF_A', 'date': '2020-10-12', 'close': 1.05},
            # ETF_A 缺少 2020-10-13（基准末个交易日）的数据
            {'ticker': 'ETF_B', 'date': '2020-10-09', 'close': 1.0},
            {'ticker': 'ETF_B', 'date': '2020-10-12', 'close': 1.01},
            {'ticker': 'ETF_B', 'date': '2020-10-13', 'close': 1.02},
        ])
        coverage_df['date'] = pd.to_datetime(coverage_df['date'])
        
        bench_df = pd.DataFrame({
            'date': pd.to_datetime(['2020-10-09', '2020-10-12', '2020-10-13']),
            'close': [1.0, 1.01, 1.02],
        })
        
        first_bench, last_bench = get_bench_trading_dates(bench_df, '2020-10-09', '2020-10-13')
        
        assert has_complete_data(coverage_df, 'ETF_A', first_bench, last_bench) is False
        assert has_complete_data(coverage_df, 'ETF_B', first_bench, last_bench) is True
