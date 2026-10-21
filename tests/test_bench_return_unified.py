#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_bench_return_unified.py — 统一基准收益口径（ROUND5 修正）。

覆盖：
1. 全期区间：bench_return_in_range == bench_price 首末比 - 1
2. 非全期区间（选定区间切片）：同样按切片内首末比，不复用全期基期
3. 非完整年度切片（跨年/年内中段）：年度分组口径各自独立
4. NaN 处理：区间内基准价不足 2 个有效值返回 None
5. 回归锁定：旧 bench_return 列差分口径在子区间上与本口径不等价（防回退）
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # app.py 在项目根
sys.path.insert(0, os.path.join(ROOT, "src"))

from app import bench_return_in_range  # noqa: E402


def _make_nav_df(start="2024-01-02", periods=600, bench_start=4000.0, drift=0.0002):
    """构造带 bench_price 的 nav_df（交易日近似用 bdate_range）。"""
    dates = pd.bdate_range(start, periods=periods)
    bench = bench_start * np.exp(drift * np.arange(periods))
    return pd.DataFrame({
        "date": dates,
        "nav": 1_000_000 * np.exp(0.0003 * np.arange(periods)),
        "bench_price": bench,
        "bench_return": bench / bench[0] - 1,  # 全期累计列（旧口径）
    })


class TestBenchReturnInRange:
    def test_full_period(self):
        df = _make_nav_df()
        ret = bench_return_in_range(df)
        expected = df["bench_price"].iloc[-1] / df["bench_price"].iloc[0] - 1
        assert ret == pytest.approx(expected, rel=1e-12)
        assert ret > 0  # drift 为正，常识校验

    def test_selected_sub_range_not_full_period(self):
        """选定区间（如 2024-06 ~ 2024-12）必须按切片内首末比，不用全期基期。"""
        df = _make_nav_df()
        sub = df[(df["date"] >= "2024-06-01") & (df["date"] <= "2024-12-31")].reset_index(drop=True)
        assert len(sub) < len(df)  # 确实非全期
        ret = bench_return_in_range(sub)
        expected = sub["bench_price"].iloc[-1] / sub["bench_price"].iloc[0] - 1
        assert ret == pytest.approx(expected, rel=1e-12)

        # 回归锁定：旧口径（全期累计 bench_return 列差分）在子区间上与本口径不同
        old_style = sub["bench_return"].iloc[-1] - sub["bench_return"].iloc[0]
        assert not np.isclose(ret, old_style, rtol=1e-6), (
            "子区间口径退回到全期基期差分"
        )

    def test_partial_first_and_last_year(self):
        """非完整年度（回测从年中开始、到年中结束）年度分组各自独立。"""
        df = _make_nav_df(start="2024-06-03", periods=400)  # 跨 2024下半年~2026
        df["year"] = df["date"].dt.year
        for year, group in df.groupby("year"):
            if len(group) < 5:
                continue
            ret = bench_return_in_range(group)
            expected = group["bench_price"].iloc[-1] / group["bench_price"].iloc[0] - 1
            assert ret == pytest.approx(expected, rel=1e-12), f"{year} 年口径错配"

    def test_year_boundary_segments_match_selected_range(self):
        """年度表与选定区间的口径一致性：完整年度切片 == 该年组。"""
        df = _make_nav_df()
        df["year"] = df["date"].dt.year
        full_year = 2025  # 中间完整年
        group = df[df["year"] == full_year]
        sliced = df[(df["date"] >= f"{full_year}-01-01") & (df["date"] <= f"{full_year}-12-31")]
        assert bench_return_in_range(group) == pytest.approx(
            bench_return_in_range(sliced), rel=1e-12)

    def test_nan_handling(self):
        df = _make_nav_df()
        # 全部 NaN
        df["bench_price"] = np.nan
        assert bench_return_in_range(df) is None
        # 只有一个有效值
        df2 = _make_nav_df()
        df2.loc[df2.index[1:], "bench_price"] = np.nan
        assert bench_return_in_range(df2) is None
        # 缺 bench_price 列 / 空表 / None
        assert bench_return_in_range(pd.DataFrame({"nav": [1, 2]})) is None
        assert bench_return_in_range(pd.DataFrame()) is None
        assert bench_return_in_range(None) is None
