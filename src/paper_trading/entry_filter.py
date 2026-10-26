# src/paper_trading/entry_filter.py — B0.4 完整入场条件（虚拟盘 runner / 实盘脚本共用）
"""
与正式 B0.4 生产信号路径逐条对齐（src/strategy.py StrategyEngine.generate_signals
+ src/backtest.py _rebalance_v2 的候选二次过滤），不得另行发明规则：

行业（核心池）入场，全部同时满足：
  1. 数据成熟期：history_count >= 51（第 51 个交易日才是第一个完整指标集）
  2. trend_score >= min_trend_score（默认 15）
  3. confirm_score >= min_confirm_score（默认 4）
  4. total_score >= effective_min_total：
     - 弱市（核心池 momentum_20 横截面中位数 < 0）→ 55（生产硬编码，
       strategy.py:407-413 "差市场：提高 total_score 门槛到 55"）
     - 否则 → 账户 min_total_score（默认 40）
  5. prev_close > ma20（前一日收盘价在 20 日均线之上）
  6. ma20_slope > 0

防御资产入场（generate_signals defense_mask 硬编码宽松档 + _rebalance_v2
min_total-10 二次过滤，两者同时满足）：
  1. history_count >= 51
  2. trend_score >= 10、confirm_score >= 2
  3. total_score >= 30 且 total_score >= min_total_score - 10
  4. prev_close > ma20 * 0.98
  5. ma20_slope > -0.001

字段纪律（2026-10-22 复审补修 ①）：必要评分字段缺失或字段值无效（NaN/inf）
时抛出 ValueError 并停止生成交易——不允许"缺字段退回仅看总分"的宽松路径。
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

#: 入场判定必需的评分列（daily_scores 之外，prev_close/history_count 由数据层富化）
REQUIRED_SCORE_COLUMNS = frozenset({
    "trend_score", "confirm_score", "total_score",
    "momentum_20", "ma20", "ma20_slope",
    "prev_close", "history_count",
})

#: 值必须为有限数的列（momentum_20 允许 NaN：生产路径横截面中位数 skipna 跳过）
_STRICT_VALUE_COLUMNS = (
    "trend_score", "confirm_score", "total_score",
    "ma20", "ma20_slope", "prev_close", "history_count",
)

#: 弱市总分门槛（strategy.py:411 生产硬编码）
WEAK_MARKET_MIN_TOTAL = 55.0

#: 数据成熟期阈值（strategy.py:417 生产硬编码）
MATURE_HISTORY_COUNT = 51

#: 防御资产硬编码宽松档（strategy.py:487-493）
DEFENSE_MIN_TREND = 10.0
DEFENSE_MIN_CONFIRM = 2.0
DEFENSE_MIN_TOTAL = 30.0
DEFENSE_PREV_CLOSE_BUFFER = 0.98
DEFENSE_MIN_SLOPE = -0.001


def validate_score_data(scores_df: pd.DataFrame,
                        universe_tickers: Iterable[str],
                        context: str = "") -> None:
    """校验评分数据完整性：缺列或值无效即抛出 ValueError（停止生成交易）。

    universe_tickers：参与决策的标的池（行业 ∪ 防御）。池外标的（如仅用于
    市场质量判定的其他核心池标的）不做值校验，与生产路径一致。
    """
    where = f"（{context}）" if context else ""
    if scores_df is None or scores_df.empty:
        raise ValueError(f"评分数据为空{where}，已停止生成交易。")
    missing = sorted(REQUIRED_SCORE_COLUMNS - set(scores_df.columns))
    if missing:
        raise ValueError(
            f"评分字段缺失{where}：{', '.join(missing)}。"
            f"入场判定需要完整 B0.4 字段（含 prev_close/history_count，由数据层富化），"
            f"已停止生成交易（不允许退回仅看总分的宽松路径）。"
        )
    pool = set(universe_tickers)
    rows = scores_df[scores_df["ticker"].isin(pool)]
    for col in _STRICT_VALUE_COLUMNS:
        values = pd.to_numeric(rows[col], errors="coerce")
        bad = rows[~np.isfinite(values)]
        if not bad.empty:
            tickers = sorted(bad["ticker"].unique())
            raise ValueError(
                f"评分字段无效{where}：{col} 存在空值/非数值，涉及标的 "
                f"{', '.join(tickers)}，已停止生成交易。"
            )


def market_quality_poor(scores_df: pd.DataFrame,
                        core_tickers: Optional[Sequence[str]] = None) -> bool:
    """弱市判定：核心池 momentum_20 横截面中位数 < 0（strategy.py:398-405）。

    与生产一致：只基于 CORE_UNIVERSE 股票型 ETF；median 跳过 NaN；
    无核心池数据时返回 False（生产 merge 后 NaN < 0 为 False）。
    """
    if core_tickers is None:
        import config as config_module
        core_tickers = list(getattr(config_module, "CORE_UNIVERSE", {}).keys())
    core = scores_df[scores_df["ticker"].isin(set(core_tickers))]
    if core.empty:
        return False
    median = pd.to_numeric(core["momentum_20"], errors="coerce").median()
    if pd.isna(median):
        return False
    return bool(median < 0)


def select_candidates(
    scores_df: pd.DataFrame,
    *,
    industry_tickers: Iterable[str],
    defense_tickers: Iterable[str],
    min_total_score: float = 40,
    min_trend_score: float = 15,
    min_confirm_score: float = 4,
    market_quality_tickers: Optional[Sequence[str]] = None,
    context: str = "",
) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]], dict]:
    """按 B0.4 完整入场条件筛选行业/防御候选。

    返回 (industry_candidates, defense_candidates, meta)；候选按 total_score
    降序。meta 含 market_quality_poor / effective_min_total 供页面与日志展示。
    """
    industry_tickers = set(industry_tickers)
    defense_tickers = set(defense_tickers)
    validate_score_data(scores_df, industry_tickers | defense_tickers, context)

    poor = market_quality_poor(scores_df, market_quality_tickers)
    # 与生产 np.where(poor, 55, min_total) 完全一致（不取 max，沿用实际规则）
    effective_min_total = WEAK_MARKET_MIN_TOTAL if poor else float(min_total_score)

    mature = scores_df["history_count"] >= MATURE_HISTORY_COUNT

    ind = scores_df[scores_df["ticker"].isin(industry_tickers) & mature]
    ind = ind[
        (ind["trend_score"] >= min_trend_score)
        & (ind["confirm_score"] >= min_confirm_score)
        & (ind["total_score"] >= effective_min_total)
        & (ind["prev_close"] > ind["ma20"])
        & (ind["ma20_slope"] > 0)
    ].sort_values("total_score", ascending=False)
    industry_candidates = [
        (row["ticker"], float(row["total_score"])) for _, row in ind.iterrows()
    ]

    # 防御：硬编码宽松档 + min_total-10 二次过滤（两个门槛同时满足）
    defense_floor = max(DEFENSE_MIN_TOTAL, float(min_total_score) - 10.0)
    dfn = scores_df[scores_df["ticker"].isin(defense_tickers) & mature]
    dfn = dfn[
        (dfn["trend_score"] >= DEFENSE_MIN_TREND)
        & (dfn["confirm_score"] >= DEFENSE_MIN_CONFIRM)
        & (dfn["total_score"] >= defense_floor)
        & (dfn["prev_close"] > dfn["ma20"] * DEFENSE_PREV_CLOSE_BUFFER)
        & (dfn["ma20_slope"] > DEFENSE_MIN_SLOPE)
    ].sort_values("total_score", ascending=False)
    defense_candidates = [
        (row["ticker"], float(row["total_score"])) for _, row in dfn.iterrows()
    ]

    meta = {
        "market_quality_poor": poor,
        "effective_min_total": effective_min_total,
        "defense_floor": defense_floor,
    }
    return industry_candidates, defense_candidates, meta
