#!/usr/bin/env python3
"""
Phase 5.6: 波动率评分修复实验

以B0.2为冻结基准，仅改变vol_score，比较4个方案：
- 当前失效版本（B0.2）
- 完全删除vol_score
- 年化波动率固定阈值评分（训练数据确定p20/p80）
- 每日横截面分位数评分（20%/80%分位）

训练期设计方案（2019-2022），验证期选择唯一候选（2023-2024）。
2025-2026封存样本暂不运行。

报告：Rank IC、年度表现、回撤、Sharpe、换手。
阈值由训练数据确定，禁止拍脑袋。
不修改生产配置。
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
from copy import deepcopy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

# =============================================================================
# Constants
# =============================================================================

TRAIN_END = '2022-12-30'
VALID_END = '2024-12-31'
OOS_START = '2025-01-01'

REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase5_volatility_repair.md')

# =============================================================================
# StrategyEngine subclasses for each vol_score scheme
# =============================================================================

class RemoveVolStrategyEngine(StrategyEngine):
    """方案C: 完全删除vol_score"""
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        df['vol_score'] = 0
        return super().compute_total_score(df, exclude_factor=exclude_factor)

class FixedThresholdVolStrategyEngine(StrategyEngine):
    """方案A: 固定阈值评分（训练数据确定p20/p80）"""
    def __init__(self, cfg, p20, p80):
        super().__init__(cfg)
        self.p20 = p20
        self.p80 = p80

    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        vol = df['volatility_20'].abs()
        df['vol_score'] = 0
        df.loc[vol < self.p20, 'vol_score'] = 10
        df.loc[(vol >= self.p20) & (vol < self.p80), 'vol_score'] = 5
        return super().compute_total_score(df, exclude_factor=exclude_factor)

class CrossSectionVolStrategyEngine(StrategyEngine):
    """方案B: 每日横截面分位数评分（20%/80%分位）"""
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        # 计算每天的20%/80%分位数，merge回df
        daily_q = df.groupby('date')['volatility_20'].agg(
            q20=lambda x: x.abs().quantile(0.2),
            q80=lambda x: x.abs().quantile(0.8)
        ).reset_index()
        df = df.merge(daily_q, on='date', how='left')
        df['vol_score'] = np.where(df['volatility_20'].abs() <= df['q20'], 10,
                           np.where(df['volatility_20'].abs() <= df['q80'], 5, 0))
        df = df.drop(columns=['q20', 'q80'])
        return super().compute_total_score(df, exclude_factor=exclude_factor)

# =============================================================================
# Helpers
# =============================================================================

def get_train_volatility_thresholds(market_df):
    """基于训练数据确定volatility阈值（p20/p80）"""
    train_df = market_df[(market_df['date'] >= '2019-01-01') & (market_df['date'] <= TRAIN_END)]
    vol_series = []
    for ticker in train_df['ticker'].unique():
        tdf = train_df[train_df['ticker'] == ticker].copy().sort_values('date')
        if len(tdf) < 21:
            continue
        tdf['volatility_20'] = tdf['close'].pct_change().rolling(20).std().shift(1) * np.sqrt(252)
        v = tdf['volatility_20'].dropna()
        if len(v) > 0:
            vol_series.append(v)
    all_vol = pd.concat(vol_series)
    return {
        'p20': all_vol.quantile(0.2),
        'p25': all_vol.quantile(0.25),
        'p50': all_vol.quantile(0.5),
        'p75': all_vol.quantile(0.75),
        'p80': all_vol.quantile(0.8),
        'mean': all_vol.mean(),
        'std': all_vol.std(),
    }


def get_scores_for_rank_ic(engine, market_df, start_date, end_date):
    """获取训练期的scores_df，用于计算Rank IC"""
    # 逐只ETF计算indicators和scores
    all_scores = []
    for ticker in market_df['ticker'].unique():
        ticker_df = market_df[market_df['ticker'] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        ticker_df = engine.calculate_indicators(ticker_df)
        scored = engine.calculate_scores(ticker_df)
        all_scores.append(scored)
    scores_df = pd.concat(all_scores, ignore_index=True)
    scores_df = engine.rank_all_momentum(scores_df)
    scores_df = engine.compute_total_score(scores_df)

    # 添加future returns
    market = market_df[['date', 'ticker', 'close']].copy().sort_values(['ticker', 'date'])
    for h in (5, 10, 20):
        market[f'future_ret_{h}d'] = market.groupby('ticker')['close'].pct_change(h).shift(-h)
    merge_cols = ['date', 'ticker'] + [f'future_ret_{h}d' for h in (5, 10, 20)]
    scores_df = scores_df.merge(market[merge_cols], on=['date', 'ticker'], how='left')

    # 限制到训练期
    scores_df = scores_df[(scores_df['date'] >= start_date) & (scores_df['date'] <= end_date)]
    return scores_df


def compute_rank_ic(scores_df, factor_col, ret_col):
    """计算每日横截面Spearman Rank IC"""
    records = []
    for date, group in scores_df.groupby('date'):
        valid = group[[factor_col, ret_col]].dropna()
        if len(valid) < 2:
            continue
        x = valid[factor_col].values
        y = valid[ret_col].values
        x_rank = pd.Series(x).rank(method='average').values
        y_rank = pd.Series(y).rank(method='average').values
        x_rank = x_rank - x_rank.mean()
        y_rank = y_rank - y_rank.mean()
        denom = np.sqrt(np.sum(x_rank**2) * np.sum(y_rank**2))
        if denom == 0:
            continue
        corr = np.sum(x_rank * y_rank) / denom
        records.append({'date': date, 'ic': corr})
    return pd.DataFrame(records)


def calc_annual_metrics(nav_df, trades_df, year):
    """计算某年的年度指标"""
    start = pd.Timestamp(f'{year}-01-01')
    end = pd.Timestamp(f'{year}-12-31')
    year_nav = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].copy()
    if len(year_nav) < 2:
        return None

    first_nav = year_nav['nav'].iloc[0]
    last_nav = year_nav['nav'].iloc[-1]
    days = (year_nav['date'].iloc[-1] - year_nav['date'].iloc[0]).days
    ann_ret = (last_nav / first_nav) ** (365 / days) - 1 if days > 0 else 0.0

    year_nav['daily_ret'] = year_nav['nav'].pct_change()
    valid_rets = year_nav['daily_ret'].dropna()
    if len(valid_rets) > 1 and valid_rets.std() > 0:
        sharpe = (valid_rets.mean() / valid_rets.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    cummax = year_nav['nav'].cummax()
    drawdown = (year_nav['nav'] - cummax) / cummax
    max_dd = drawdown.min()

    year_trades = trades_df[
        (pd.to_datetime(trades_df['date']) >= start) & (pd.to_datetime(trades_df['date']) <= end)
    ] if 'date' in trades_df.columns else pd.DataFrame()
    n_trades = len(year_trades)

    days_invested = (year_nav['nav'] > first_nav * 1.001).sum()
    turnover = n_trades / days_invested if days_invested > 0 else 0.0

    return {'ann_ret': ann_ret, 'sharpe': sharpe, 'max_dd': max_dd,
            'n_trades': n_trades, 'turnover': turnover, 'days': days}


def run_backtest_with_strategy(cfg, strategy_engine_cls, strategy_kwargs,
                                 as_of_date, performance_start=None):
    """使用自定义StrategyEngine运行回测"""
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=as_of_date)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=as_of_date)

    engine = BacktestEngine(cfg)
    engine.strategy = strategy_engine_cls(cfg, **strategy_kwargs)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=performance_start)


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 70)
    print("Phase 5.6: Volatility Score Repair Experiment")
    print("=" * 70)

    # 1. 获取数据，确定训练期阈值
    print("\n[1/5] Determining volatility thresholds from training data...")
    db = ETFDatabase()
    market_df = db.get_market_data(ticker=list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()),
                                    start_date='2019-01-01', end_date=VALID_END)
    thresholds = get_train_volatility_thresholds(market_df)
    print(f"  Training volatility distribution:")
    print(f"    p20={thresholds['p20']:.4f}, p25={thresholds['p25']:.4f}, p50={thresholds['p50']:.4f}")
    print(f"    p75={thresholds['p75']:.4f}, p80={thresholds['p80']:.4f}")
    print(f"    mean={thresholds['mean']:.4f}, std={thresholds['std']:.4f}")
    print(f"  Fixed thresholds: <{thresholds['p20']:.4f}=10, [{thresholds['p20']:.4f},{thresholds['p80']:.4f})=5, >={thresholds['p80']:.4f}=0")

    p20 = thresholds['p20']
    p80 = thresholds['p80']

    # 2. 定义4个方案
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    # B0.2 already has momentum disabled
    assert cfg.get('momentum_factor_enabled') is False, "Expected B0.2 with momentum disabled"

    schemes = {
        'current': {
            'name': '当前失效(B0.2)',
            'engine_cls': StrategyEngine,
            'kwargs': {},
        },
        'remove': {
            'name': '完全删除vol_score',
            'engine_cls': RemoveVolStrategyEngine,
            'kwargs': {},
        },
        'fixed': {
            'name': f'固定阈值(p20={p20:.3f},p80={p80:.3f})',
            'engine_cls': FixedThresholdVolStrategyEngine,
            'kwargs': {'p20': p20, 'p80': p80},
        },
        'cross_section': {
            'name': '横截面分位数(20%/80%)',
            'engine_cls': CrossSectionVolStrategyEngine,
            'kwargs': {},
        },
    }

    # 3. 计算训练期Rank IC
    print("\n[2/5] Computing Rank IC on training period...")
    train_scores = {}
    for key, sc in schemes.items():
        engine = sc['engine_cls'](cfg, **sc['kwargs'])
        scores_df = get_scores_for_rank_ic(engine, market_df, '2019-06-01', TRAIN_END)
        train_scores[key] = scores_df

    # Rank IC for vol_score
    print("\n  Rank IC (vol_score vs future returns, training period):")
    print(f"  {'Scheme':<25} {'H5 IC_mean':>12} {'H5 IR':>8} {'H10 IC_mean':>12} {'H10 IR':>8} {'H20 IC_mean':>12} {'H20 IR':>8}")
    print(f"  {'-'*90}")
    ic_results = {}
    for key, sc in schemes.items():
        scores_df = train_scores[key]
        ic_row = {}
        for h in (5, 10, 20):
            ic_df = compute_rank_ic(scores_df, 'vol_score', f'future_ret_{h}d')
            if not ic_df.empty:
                ic_mean = ic_df['ic'].mean()
                ic_std = ic_df['ic'].std()
                ir = ic_mean / ic_std if ic_std > 0 else 0
                ic_row[f'H{h}_mean'] = ic_mean
                ic_row[f'H{h}_std'] = ic_std
                ic_row[f'H{h}_ir'] = ir
            else:
                ic_row[f'H{h}_mean'] = np.nan
                ic_row[f'H{h}_std'] = np.nan
                ic_row[f'H{h}_ir'] = np.nan
        ic_results[key] = ic_row
        print(f"  {sc['name']:<25} {ic_row['H5_mean']:>+11.4f} {ic_row['H5_ir']:>8.4f} {ic_row['H10_mean']:>+11.4f} {ic_row['H10_ir']:>8.4f} {ic_row['H20_mean']:>+11.4f} {ic_row['H20_ir']:>8.4f}")

    # 4. 运行训练期和验证期回测
    print("\n[3/5] Running backtests (train + valid)...")
    splits = {
        'train': {'as_of_date': TRAIN_END, 'performance_start': None},
        'valid': {'as_of_date': VALID_END, 'performance_start': '2023-01-01'},
    }

    backtest_results = {}
    for key, sc in schemes.items():
        backtest_results[key] = {}
        for split_name, split_cfg in splits.items():
            print(f"  {sc['name']} - {split_name}...")
            result = run_backtest_with_strategy(
                cfg, sc['engine_cls'], sc['kwargs'],
                split_cfg['as_of_date'], split_cfg.get('performance_start')
            )
            backtest_results[key][split_name] = result

    # 5. 提取指标并比较
    print("\n[4/5] Extracting metrics...")
    metrics = {}
    for key, sc in schemes.items():
        metrics[key] = {}
        for split_name in splits:
            result = backtest_results[key][split_name]
            nav_df = result['nav_df']
            trades_df = result['trades_df']
            metrics[key][split_name] = {
                'total_return': result['total_return'],
                'annual_return': result['annual_return'],
                'sharpe': result['sharpe_ratio'],
                'max_dd': result['max_drawdown'],
                'num_trades': result['num_trades'],
                'rebalance_count': result['rebalance_count'],
            }
            # Year-by-year for valid period
            if split_name == 'valid':
                metrics[key]['valid_yearly'] = {}
                for year in (2023, 2024):
                    y = calc_annual_metrics(nav_df, trades_df, year)
                    if y:
                        metrics[key]['valid_yearly'][year] = y

    # 打印汇总
    print("\n  Full Results Summary:")
    print(f"  {'Scheme':<25} {'Train Ann':>10} {'Train Sharpe':>12} {'Train DD':>10} {'Valid Ann':>10} {'Valid Sharpe':>12} {'Valid DD':>10}")
    print(f"  {'-'*95}")
    for key, sc in schemes.items():
        t = metrics[key]['train']
        v = metrics[key]['valid']
        print(f"  {sc['name']:<25} {t['annual_return']:>9.2%} {t['sharpe']:>12.4f} {t['max_dd']:>9.2%} {v['annual_return']:>9.2%} {v['sharpe']:>12.4f} {v['max_dd']:>9.2%}")

    # 6. 选择候选：训练期最优，验证期确认
    print("\n[5/5] Candidate selection...")
    # 训练期排序：按年化收益
    train_ranks = sorted(schemes.keys(), key=lambda k: metrics[k]['train']['annual_return'], reverse=True)
    best_train = train_ranks[0]
    print(f"  Training winner: {schemes[best_train]['name']} (ann={metrics[best_train]['train']['annual_return']:.2%})")

    # 验证期确认：训练期前2名在验证期的表现
    top2 = train_ranks[:2]
    valid_ranks = sorted(top2, key=lambda k: metrics[k]['valid']['annual_return'], reverse=True)
    best_valid = valid_ranks[0]
    print(f"  Validation winner (top2 from train): {schemes[best_valid]['name']} (ann={metrics[best_valid]['valid']['annual_return']:.2%})")

    # 退化检查：训练期差距>2pp则淘汰
    train_gap = metrics[best_train]['train']['annual_return'] - metrics['current']['train']['annual_return']
    print(f"  Training gap vs current: {train_gap:+.2%}")
    if train_gap < -0.02:
        print(f"  WARNING: Best candidate is DEGRADED in training (>2pp). Keeping current.")
        final_choice = 'current'
    else:
        final_choice = best_valid

    print(f"  Final candidate: {schemes[final_choice]['name']}")

    # 7. 生成报告
    lines = []
    lines.append("# Phase 5.6 波动率评分修复实验报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**基准**: B0.2 (momentum_factor_enabled=False)")
    lines.append("")
    lines.append("## 1. 训练期波动率分布与阈值")
    lines.append("")
    lines.append(f"| 统计量 | 值 |")
    lines.append(f"|--------|-----|")
    lines.append(f"| p20 | {p20:.4f} |")
    lines.append(f"| p25 | {thresholds['p25']:.4f} |")
    lines.append(f"| p50 | {thresholds['p50']:.4f} |")
    lines.append(f"| p75 | {thresholds['p75']:.4f} |")
    lines.append(f"| p80 | {p80:.4f} |")
    lines.append(f"| mean | {thresholds['mean']:.4f} |")
    lines.append(f"| std | {thresholds['std']:.4f} |")
    lines.append("")
    lines.append(f"**固定阈值方案**: volatility_20 < {p20:.4f} → 10分, [{p20:.4f}, {p80:.4f}) → 5分, >= {p80:.4f} → 0分")
    lines.append(f"**横截面分位数方案**: 每天最低20%分位 → 10分, 20%-80%分位 → 5分, 最高20%分位 → 0分")
    lines.append("")
    lines.append("## 2. Rank IC (训练期)")
    lines.append("")
    lines.append("| 方案 | H5 IC_mean | H5 IR | H10 IC_mean | H10 IR | H20 IC_mean | H20 IR |")
    lines.append("|------|------------|-------|-------------|--------|-------------|--------|")
    for key, sc in schemes.items():
        ic = ic_results[key]
        def fmt(v):
            return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        lines.append(f"| {sc['name']} | {fmt(ic['H5_mean'])} | {fmt(ic['H5_ir'])} | {fmt(ic['H10_mean'])} | {fmt(ic['H10_ir'])} | {fmt(ic['H20_mean'])} | {fmt(ic['H20_ir'])} |")
    lines.append("")
    lines.append("## 3. 回测表现")
    lines.append("")
    lines.append("### 3.1 训练期 (2019-2022)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, sc in schemes.items():
        m = metrics[key]['train']
        lines.append(f"| {sc['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 3.2 验证期 (2023-2024)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, sc in schemes.items():
        m = metrics[key]['valid']
        lines.append(f"| {sc['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 3.3 验证期逐年表现")
    lines.append("")
    for year in (2023, 2024):
        lines.append(f"#### {year}年")
        lines.append("")
        lines.append("| 方案 | 年化 | Sharpe | 最大回撤 | 交易次数 | 换手 |")
        lines.append("|------|------|--------|----------|----------|------|")
        for key, sc in schemes.items():
            if 'valid_yearly' in metrics[key] and year in metrics[key]['valid_yearly']:
                y = metrics[key]['valid_yearly'][year]
                lines.append(f"| {sc['name']} | {y['ann_ret']:.2%} | {y['sharpe']:.4f} | {y['max_dd']:.2%} | {y['n_trades']:.0f} | {y['turnover']:.4f} |")
            else:
                lines.append(f"| {sc['name']} | N/A | N/A | N/A | N/A | N/A |")
        lines.append("")
    lines.append("## 4. 候选选择")
    lines.append("")
    lines.append(f"- 训练期最优: {schemes[best_train]['name']} (年化 {metrics[best_train]['train']['annual_return']:.2%})")
    lines.append(f"- 验证期前2名候选中最优: {schemes[best_valid]['name']} (年化 {metrics[best_valid]['valid']['annual_return']:.2%})")
    lines.append(f"- 训练期退化检查: {train_gap:+.2%} (阈值: -2%)")
    lines.append(f"- **最终候选**: {schemes[final_choice]['name']}")
    lines.append("")
    lines.append("---")
    lines.append("*2025-2026封存样本未运行，不用于调参。*")
    lines.append("*未修改生产配置 (src/config.py)。*")

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"\n  Report saved to: {REPORT_PATH}")
    print("=" * 70)
    print("Phase 5.6 completed.")
    print("=" * 70)


if __name__ == '__main__':
    main()
