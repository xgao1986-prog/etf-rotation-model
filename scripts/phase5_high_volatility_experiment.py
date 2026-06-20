#!/usr/bin/env python3
"""
Phase 5.8: 高波动增量价值实验

以B0.3为冻结基准，仅改变volatility的使用方式，比较4个方案：
- 方案A: B0.3基准（vol_score=0）
- 方案B: 横截面高波动前20%加分（高波动=高分）
- 方案C: 高波动前20%但仅限趋势条件已通过（trend_score>0）
- 方案D: 波动率加速上升（当前vol > 过去20日均值）

训练期2019-2022设计，验证期2023-2024选择。
不运行2025-2026封存样本。

检查：Rank IC、年化、Sharpe、回撤、换手、因子相关性、是否重新引入动量。
不修改生产配置。

输出：reports/phase5_high_volatility_experiment.md
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

REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase5_high_volatility_experiment.md')


# =============================================================================
# Custom StrategyEngine subclasses for each scheme
# =============================================================================

class SchemeB_HighVol20Pct(StrategyEngine):
    """方案B: 横截面高波动前20%加分
    每天按volatility_20排序，前20%分位（最高波动）给10分，20%-80%给5分，后20%给0分
    """
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        result_dfs = []
        for date in df['date'].unique():
            day_df = df[df['date'] == date].copy()
            vols = day_df['volatility_20'].abs()
            if len(vols) >= 2:
                q80 = vols.quantile(0.8)  # 前20%高波动
                q20 = vols.quantile(0.2)  # 后20%低波动
                # 高波动=高分（与常规低波动异象相反）
                day_df['vol_score'] = np.where(vols >= q80, 10,
                                      np.where(vols >= q20, 5, 0))
            else:
                day_df['vol_score'] = 0
            result_dfs.append(day_df)
        df = pd.concat(result_dfs, ignore_index=True)
        return super().compute_total_score(df, exclude_factor=exclude_factor)


class SchemeC_HighVolWithTrend(StrategyEngine):
    """方案C: 高波动前20%但仅限趋势条件已通过
    只有trend_score > 0的ETF，如果vol在前20%则加10分，20%-80%加5分
    """
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        result_dfs = []
        for date in df['date'].unique():
            day_df = df[df['date'] == date].copy()
            vols = day_df['volatility_20'].abs()
            if len(vols) >= 2:
                q80 = vols.quantile(0.8)
                q20 = vols.quantile(0.2)
                # 只有trend_score > 0的ETF才给vol_score
                has_trend = day_df['trend_score'] > 0
                day_df['vol_score'] = np.where(has_trend & (vols >= q80), 10,
                                      np.where(has_trend & (vols >= q20), 5, 0))
            else:
                day_df['vol_score'] = 0
            result_dfs.append(day_df)
        df = pd.concat(result_dfs, ignore_index=True)
        return super().compute_total_score(df, exclude_factor=exclude_factor)


class SchemeD_VolAcceleration(StrategyEngine):
    """方案D: 波动率加速上升（当前vol > 过去20日vol均值）
    需要计算vol的20日移动平均，然后比较当前值
    """
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        # 计算volatility_20的20日移动平均（注意：这里用的是过去20日vol均值）
        df = df.sort_values(['ticker', 'date'])
        df['vol_ma20'] = df.groupby('ticker')['volatility_20'].rolling(20, min_periods=1).mean().reset_index(level=0, drop=True)
        # 加速上升：当前vol > 过去20日vol均值
        df['vol_score'] = np.where(df['volatility_20'] > df['vol_ma20'], 10, 0)
        df['vol_score'] = df['vol_score'].fillna(0)
        df = df.drop(columns=['vol_ma20'])
        return super().compute_total_score(df, exclude_factor=exclude_factor)


# =============================================================================
# Helpers
# =============================================================================

def get_scores_with_future(engine, market_df, start_date, end_date):
    """获取scores_df并添加未来收益"""
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
    
    # 添加未来收益
    market = market_df[['date', 'ticker', 'close']].copy().sort_values(['ticker', 'date'])
    for h in (5, 10, 20):
        market[f'future_ret_{h}d'] = market.groupby('ticker')['close'].pct_change(h).shift(-h)
    merge_cols = ['date', 'ticker'] + [f'future_ret_{h}d' for h in (5, 10, 20)]
    scores_df = scores_df.merge(market[merge_cols], on=['date', 'ticker'], how='left')
    
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


def calc_factor_correlations(scores_df, factor_col):
    """计算指定因子与其他因子的相关性"""
    factor_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
    available = [c for c in factor_cols if c in scores_df.columns and c != factor_col]
    if not available:
        return {}
    
    corr = scores_df[[factor_col] + available].corr()
    return {c: corr.loc[factor_col, c] for c in available}


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


def run_backtest_with_strategy(cfg, engine_cls, engine_kwargs, as_of_date, perf_start=None):
    """使用自定义StrategyEngine运行回测"""
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=as_of_date)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=as_of_date)
    
    engine = BacktestEngine(cfg)
    engine.strategy = engine_cls(cfg, **engine_kwargs)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=perf_start)


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 70)
    print("Phase 5.8: High Volatility Incremental Value Experiment")
    print("=" * 70)
    
    # 1. 准备数据
    print("\n[1/6] Loading data...")
    db = ETFDatabase()
    market_df = db.get_market_data(ticker=list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()),
                                    start_date='2019-01-01', end_date=VALID_END)
    
    # 2. 定义4个方案
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    assert cfg.get('momentum_factor_enabled') is False
    assert cfg.get('volatility_factor_enabled') is False
    
    schemes = {
        'A': {'name': 'B0.3基准(无波动率)', 'engine_cls': StrategyEngine, 'kwargs': {}},
        'B': {'name': '高波动前20%加分', 'engine_cls': SchemeB_HighVol20Pct, 'kwargs': {}},
        'C': {'name': '高波动+趋势条件', 'engine_cls': SchemeC_HighVolWithTrend, 'kwargs': {}},
        'D': {'name': '波动率加速上升', 'engine_cls': SchemeD_VolAcceleration, 'kwargs': {}},
    }
    
    # 3. 计算训练期Rank IC和因子相关性
    print("\n[2/6] Computing Rank IC and factor correlations on training period...")
    ic_results = {}
    corr_results = {}
    for key, sc in schemes.items():
        engine = sc['engine_cls'](cfg, **sc['kwargs'])
        scores_df = get_scores_with_future(engine, market_df, '2019-06-01', TRAIN_END)
        
        # Rank IC for vol_score
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
        
        # 因子相关性
        corr_results[key] = calc_factor_correlations(scores_df, 'vol_score')
    
    # 打印Rank IC
    print("\n  Rank IC (vol_score vs future returns, training period):")
    print(f"  {'Scheme':<25} {'H5 IC_mean':>12} {'H5 IR':>8} {'H10 IC_mean':>12} {'H10 IR':>8} {'H20 IC_mean':>12} {'H20 IR':>8}")
    print(f"  {'-'*90}")
    for key, sc in schemes.items():
        ic = ic_results[key]
        def fmt(v):
            return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        print(f"  {sc['name']:<25} {fmt(ic['H5_mean'])} {fmt(ic['H5_ir'])} {fmt(ic['H10_mean'])} {fmt(ic['H10_ir'])} {fmt(ic['H20_mean'])} {fmt(ic['H20_ir'])}")
    
    # 打印因子相关性
    print("\n  Factor correlations (vol_score vs others, training period):")
    print(f"  {'Scheme':<25} {'trend':>8} {'confirm':>8} {'momentum':>8} {'volume':>8}")
    print(f"  {'-'*60}")
    for key, sc in schemes.items():
        corr = corr_results[key]
        def fmtc(v):
            return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        print(f"  {sc['name']:<25} {fmtc(corr.get('trend_score', np.nan)):>8} {fmtc(corr.get('confirm_score', np.nan)):>8} {fmtc(corr.get('momentum_rank', np.nan)):>8} {fmtc(corr.get('volume_score', np.nan)):>8}")
    
    # 4. 运行训练期和验证期回测
    print("\n[3/6] Running backtests (train + valid)...")
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
    
    # 5. 提取指标
    print("\n[4/6] Extracting metrics...")
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
    
    # 6. 选择候选
    print("\n[5/6] Candidate selection...")
    train_ranks = sorted(schemes.keys(), key=lambda k: metrics[k]['train']['annual_return'], reverse=True)
    best_train = train_ranks[0]
    print(f"  Training winner: {schemes[best_train]['name']} (ann={metrics[best_train]['train']['annual_return']:.2%})")
    
    top2 = train_ranks[:2]
    valid_ranks = sorted(top2, key=lambda k: metrics[k]['valid']['annual_return'], reverse=True)
    best_valid = valid_ranks[0]
    print(f"  Validation winner (top2 from train): {schemes[best_valid]['name']} (ann={metrics[best_valid]['valid']['annual_return']:.2%})")
    
    train_gap = metrics[best_train]['train']['annual_return'] - metrics['A']['train']['annual_return']
    print(f"  Training gap vs baseline: {train_gap:+.2%}")
    if train_gap < -0.02:
        print(f"  WARNING: Best candidate is DEGRADED in training (>2pp). Keeping baseline.")
        final_choice = 'A'
    else:
        final_choice = best_valid
    
    print(f"  Final candidate: {schemes[final_choice]['name']}")
    
    # 7. 检查是否重新引入动量效应
    print("\n[6/6] Checking for momentum re-introduction...")
    print(f"  Correlation between vol_score and momentum_rank (training):")
    for key, sc in schemes.items():
        corr = corr_results[key].get('momentum_rank', np.nan)
        if not pd.isna(corr):
            print(f"    {sc['name']:<25}: {corr:+.4f}")
    
    # 判断
    print(f"\n  Momentum check:")
    for key in ('B', 'C', 'D'):
        corr = corr_results[key].get('momentum_rank', 0)
        if abs(corr) > 0.3:
            print(f"    WARNING: {schemes[key]['name']} has high vol_score-momentum correlation ({corr:+.4f}) - may re-introduce momentum effect!")
        else:
            print(f"    {schemes[key]['name']}: vol_score-momentum correlation = {corr:+.4f} (low risk)")
    
    # 生成报告
    lines = []
    lines.append("# Phase 5.8 高波动增量价值实验报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**基准**: B0.3 (momentum_factor_enabled=False, volatility_factor_enabled=False)")
    lines.append("")
    lines.append("## 1. 实验方案")
    lines.append("")
    lines.append("| 方案 | 描述 |")
    lines.append("|------|------|")
    lines.append("| A | B0.3基准（无波动率）|")
    lines.append("| B | 横截面高波动前20%加分（高波动=高分）|")
    lines.append("| C | 高波动前20%但仅限趋势条件已通过（trend_score>0）|")
    lines.append("| D | 波动率加速上升（当前vol > 过去20日vol均值）|")
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
    lines.append("## 3. 因子相关性 (训练期)")
    lines.append("")
    lines.append("| 方案 | vol_score vs trend | vol_score vs confirm | vol_score vs momentum | vol_score vs volume |")
    lines.append("|------|-------------------|---------------------|----------------------|---------------------|")
    for key, sc in schemes.items():
        corr = corr_results[key]
        def fmtc(v):
            return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        lines.append(f"| {sc['name']} | {fmtc(corr.get('trend_score', np.nan))} | {fmtc(corr.get('confirm_score', np.nan))} | {fmtc(corr.get('momentum_rank', np.nan))} | {fmtc(corr.get('volume_score', np.nan))} |")
    lines.append("")
    lines.append("## 4. 回测表现")
    lines.append("")
    lines.append("### 4.1 训练期 (2019-2022)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, sc in schemes.items():
        m = metrics[key]['train']
        lines.append(f"| {sc['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 4.2 验证期 (2023-2024)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, sc in schemes.items():
        m = metrics[key]['valid']
        lines.append(f"| {sc['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 4.3 验证期逐年表现")
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
    lines.append("## 5. 动量效应检查")
    lines.append("")
    lines.append("| 方案 | vol_score vs momentum_rank | 是否重新引入动量？ |")
    lines.append("|------|---------------------------|-------------------|")
    for key in ('B', 'C', 'D'):
        corr = corr_results[key].get('momentum_rank', np.nan)
        if not pd.isna(corr):
            is_momentum = "是" if abs(corr) > 0.3 else "否"
            lines.append(f"| {schemes[key]['name']} | {corr:+.4f} | {is_momentum} |")
    lines.append("")
    lines.append("## 6. 候选选择")
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
    print("Phase 5.8 completed.")
    print("=" * 70)


if __name__ == '__main__':
    main()
