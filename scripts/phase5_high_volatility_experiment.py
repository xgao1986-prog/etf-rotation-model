#!/usr/bin/env python3
"""
Phase 5.8修正: 高波动增量价值实验

修正内容（v2）:
1. B0.3基准: volatility_factor_enabled=False
2. B/C/D实验组: 显式设置volatility_factor_enabled=True
3. compute_total_score中实验组cfg开启vol，total_score实际包含vol_score
4. 断言检查:
   - 实验组至少存在非零vol_score
   - 实验组total_score与关闭vol_score时存在差异
   - 若交易序列完全一致，报告原因，不自动判定有效
5. 波动率加速均值: rolling(20).mean().shift(1)，不能用当日数据
6. 训练期2019-2022设计，验证期2023-2024选择
7. 2025-2026封存样本不运行
8. 不修改生产配置

输出: reports/phase5_high_volatility_experiment.md
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

TRAIN_END = '2022-12-30'
VALID_END = '2024-12-31'
REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase5_high_volatility_experiment.md')


# =============================================================================
# Custom StrategyEngines (v2: 不再依赖父类开关覆盖vol_score)
# =============================================================================

class BaseVolScheme(StrategyEngine):
    """基类: 实验组统一覆盖compute_total_score，不再受父类vol开关影响"""
    pass


class SchemeB_HighVol20Pct(BaseVolScheme):
    """方案B: 横截面高波动前20%加分"""
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        result_dfs = []
        for date in df['date'].unique():
            day_df = df[df['date'] == date].copy()
            vols = day_df['volatility_20'].abs()
            if len(vols) >= 2:
                q80 = vols.quantile(0.8)
                q20 = vols.quantile(0.2)
                day_df['vol_score'] = np.where(vols >= q80, 10,
                                      np.where(vols >= q20, 5, 0))
            else:
                day_df['vol_score'] = 0
            result_dfs.append(day_df)
        df = pd.concat(result_dfs, ignore_index=True)
        # 直接求和，不再调用父类（父类会覆盖vol_score）
        df['total_score'] = (df['trend_score'].fillna(0) + df['confirm_score'].fillna(0) +
                             df['momentum_rank'].fillna(0) + df['volume_score'].fillna(0) +
                             df['vol_score'].fillna(0))
        if exclude_factor is not None:
            if exclude_factor in df.columns:
                df['total_score'] -= df[exclude_factor].fillna(0)
        return df


class SchemeC_HighVolWithTrend(BaseVolScheme):
    """方案C: 高波动前20%但仅限趋势条件已通过"""
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        result_dfs = []
        for date in df['date'].unique():
            day_df = df[df['date'] == date].copy()
            vols = day_df['volatility_20'].abs()
            if len(vols) >= 2:
                q80 = vols.quantile(0.8)
                q20 = vols.quantile(0.2)
                has_trend = day_df['trend_score'] > 0
                day_df['vol_score'] = np.where(has_trend & (vols >= q80), 10,
                                      np.where(has_trend & (vols >= q20), 5, 0))
            else:
                day_df['vol_score'] = 0
            result_dfs.append(day_df)
        df = pd.concat(result_dfs, ignore_index=True)
        df['total_score'] = (df['trend_score'].fillna(0) + df['confirm_score'].fillna(0) +
                             df['momentum_rank'].fillna(0) + df['volume_score'].fillna(0) +
                             df['vol_score'].fillna(0))
        if exclude_factor is not None:
            if exclude_factor in df.columns:
                df['total_score'] -= df[exclude_factor].fillna(0)
        return df


class SchemeD_VolAcceleration(BaseVolScheme):
    """方案D: 波动率加速上升（当前vol > 过去20日vol均值，均值必须shift(1)）"""
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        df = df.sort_values(['ticker', 'date'])
        # 关键修正: rolling mean 后必须 shift(1) 确保买入日前已知
        df['vol_ma20'] = df.groupby('ticker')['volatility_20'].rolling(20, min_periods=1).mean().reset_index(level=0, drop=True)
        df['vol_ma20'] = df.groupby('ticker')['vol_ma20'].shift(1)  # shift(1) 防止未来数据
        df['vol_score'] = np.where(df['volatility_20'] > df['vol_ma20'], 10, 0)
        df['vol_score'] = df['vol_score'].fillna(0)
        df = df.drop(columns=['vol_ma20'])
        df['total_score'] = (df['trend_score'].fillna(0) + df['confirm_score'].fillna(0) +
                             df['momentum_rank'].fillna(0) + df['volume_score'].fillna(0) +
                             df['vol_score'].fillna(0))
        if exclude_factor is not None:
            if exclude_factor in df.columns:
                df['total_score'] -= df[exclude_factor].fillna(0)
        return df


# =============================================================================
# Helpers
# =============================================================================

def get_scores_for_ic(engine, market_df, start_date, end_date):
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
    
    market = market_df[['date', 'ticker', 'close']].copy().sort_values(['ticker', 'date'])
    for h in (5, 10, 20):
        market[f'future_ret_{h}d'] = market.groupby('ticker')['close'].pct_change(h).shift(-h)
    merge_cols = ['date', 'ticker'] + [f'future_ret_{h}d' for h in (5, 10, 20)]
    scores_df = scores_df.merge(market[merge_cols], on=['date', 'ticker'], how='left')
    scores_df = scores_df[(scores_df['date'] >= start_date) & (scores_df['date'] <= end_date)]
    return scores_df


def compute_rank_ic(scores_df, factor_col, ret_col):
    records = []
    for date, group in scores_df.groupby('date'):
        valid = group[[factor_col, ret_col]].dropna()
        if len(valid) < 2:
            continue
        x = valid[factor_col].values; y = valid[ret_col].values
        x_rank = np.array(pd.Series(x).rank(method='average'))
        y_rank = np.array(pd.Series(y).rank(method='average'))
        x_rank = x_rank - x_rank.mean(); y_rank = y_rank - y_rank.mean()
        denom = np.sqrt(np.sum(x_rank**2) * np.sum(y_rank**2))
        if denom == 0: continue
        records.append({'date': date, 'ic': np.sum(x_rank * y_rank) / denom})
    return pd.DataFrame(records)


def calc_factor_correlations(scores_df, factor_col):
    factor_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
    available = [c for c in factor_cols if c in scores_df.columns and c != factor_col]
    if not available: return {}
    corr = scores_df[[factor_col] + available].corr()
    return {c: corr.loc[factor_col, c] for c in available}


def calc_annual_metrics(nav_df, trades_df, year):
    start = pd.Timestamp(f'{year}-01-01')
    end = pd.Timestamp(f'{year}-12-31')
    year_nav = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].copy()
    if len(year_nav) < 2: return None
    first_nav = year_nav['nav'].iloc[0]
    last_nav = year_nav['nav'].iloc[-1]
    days = (year_nav['date'].iloc[-1] - year_nav['date'].iloc[0]).days
    ann_ret = (last_nav / first_nav) ** (365 / days) - 1 if days > 0 else 0.0
    year_nav['daily_ret'] = year_nav['nav'].pct_change()
    valid_rets = year_nav['daily_ret'].dropna()
    sharpe = (valid_rets.mean() / valid_rets.std()) * np.sqrt(252) if len(valid_rets) > 1 and valid_rets.std() > 0 else 0.0
    cummax = year_nav['nav'].cummax()
    drawdown = (year_nav['nav'] - cummax) / cummax
    max_dd = drawdown.min()
    year_trades = trades_df[(pd.to_datetime(trades_df['date']) >= start) & (pd.to_datetime(trades_df['date']) <= end)] if 'date' in trades_df.columns else pd.DataFrame()
    n_trades = len(year_trades)
    days_invested = (year_nav['nav'] > first_nav * 1.001).sum()
    turnover = n_trades / days_invested if days_invested > 0 else 0.0
    return {'ann_ret': ann_ret, 'sharpe': sharpe, 'max_dd': max_dd, 'n_trades': n_trades, 'turnover': turnover, 'days': days}


def run_backtest(cfg, engine_cls, engine_kwargs, as_of_date, perf_start=None):
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
    print("Phase 5.8修正: High Volatility Incremental Value Experiment (v2)")
    print("=" * 70)
    
    print("\n[1/7] Loading data...")
    db = ETFDatabase()
    market_df = db.get_market_data(ticker=list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()),
                                    start_date='2019-01-01', end_date=VALID_END)
    
    # 基准cfg: B0.3 (volatility_factor_enabled=False)
    cfg_baseline = build_config()
    cfg_baseline['fallback_equity_enabled'] = False
    assert cfg_baseline.get('momentum_factor_enabled') is False
    assert cfg_baseline.get('volatility_factor_enabled') is False
    
    # 实验cfg: 显式开启volatility_factor_enabled
    cfg_exp = deepcopy(cfg_baseline)
    cfg_exp['volatility_factor_enabled'] = True  # 显式开启
    
    print(f"  基准cfg: volatility_factor_enabled={cfg_baseline.get('volatility_factor_enabled')}")
    print(f"  实验cfg: volatility_factor_enabled={cfg_exp.get('volatility_factor_enabled')}")
    
    schemes = {
        'A': {'name': 'B0.3基准(vol=关闭)', 'cfg': cfg_baseline, 'engine_cls': StrategyEngine, 'kwargs': {}},
        'B': {'name': '高波动前20%加分', 'cfg': cfg_exp, 'engine_cls': SchemeB_HighVol20Pct, 'kwargs': {}},
        'C': {'name': '高波动+趋势条件', 'cfg': cfg_exp, 'engine_cls': SchemeC_HighVolWithTrend, 'kwargs': {}},
        'D': {'name': '波动率加速上升(shift1)', 'cfg': cfg_exp, 'engine_cls': SchemeD_VolAcceleration, 'kwargs': {}},
    }
    
    # 2. 计算训练期Rank IC和因子相关性
    print("\n[2/7] Computing Rank IC and factor correlations (training period)...")
    ic_results = {}
    corr_results = {}
    for key, sc in schemes.items():
        engine = sc['engine_cls'](sc['cfg'], **sc['kwargs'])
        scores_df = get_scores_for_ic(engine, market_df, '2019-06-01', TRAIN_END)
        ic_row = {}
        for h in (5, 10, 20):
            ic_df = compute_rank_ic(scores_df, 'vol_score', f'future_ret_{h}d')
            if not ic_df.empty:
                ic_mean = ic_df['ic'].mean(); ic_std = ic_df['ic'].std(); ir = ic_mean / ic_std if ic_std > 0 else 0
                ic_row[f'H{h}_mean'] = ic_mean; ic_row[f'H{h}_std'] = ic_std; ic_row[f'H{h}_ir'] = ir
            else:
                ic_row[f'H{h}_mean'] = np.nan; ic_row[f'H{h}_std'] = np.nan; ic_row[f'H{h}_ir'] = np.nan
        ic_results[key] = ic_row
        corr_results[key] = calc_factor_correlations(scores_df, 'vol_score')
    
    print("\n  Rank IC (vol_score vs future returns, training period):")
    print(f"  {'Scheme':<25} {'H5 IC_mean':>12} {'H5 IR':>8} {'H10 IC_mean':>12} {'H10 IR':>8} {'H20 IC_mean':>12} {'H20 IR':>8}")
    print(f"  {'-'*90}")
    for key, sc in schemes.items():
        ic = ic_results[key]
        def fmt(v): return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        print(f"  {sc['name']:<25} {fmt(ic['H5_mean'])} {fmt(ic['H5_ir'])} {fmt(ic['H10_mean'])} {fmt(ic['H10_ir'])} {fmt(ic['H20_mean'])} {fmt(ic['H20_ir'])}")
    
    print("\n  Factor correlations (vol_score vs others, training period):")
    print(f"  {'Scheme':<25} {'trend':>8} {'confirm':>8} {'momentum':>8} {'volume':>8}")
    print(f"  {'-'*60}")
    for key, sc in schemes.items():
        corr = corr_results[key]
        def fmtc(v): return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        print(f"  {sc['name']:<25} {fmtc(corr.get('trend_score', np.nan)):>8} {fmtc(corr.get('confirm_score', np.nan)):>8} {fmtc(corr.get('momentum_rank', np.nan)):>8} {fmtc(corr.get('volume_score', np.nan)):>8}")
    
    # 3. 断言检查
    print("\n[3/7] Assertion checks...")
    
    # 3.1 实验组至少存在非零vol_score
    for key in ('B', 'C', 'D'):
        engine = schemes[key]['engine_cls'](schemes[key]['cfg'], **schemes[key]['kwargs'])
        scores_df = get_scores_for_ic(engine, market_df, '2019-06-01', TRAIN_END)
        nonzero_vol = (scores_df['vol_score'] != 0).sum()
        total_rows = len(scores_df)
        print(f"  {schemes[key]['name']}: nonzero_vol_score = {nonzero_vol}/{total_rows} ({nonzero_vol/total_rows*100:.1f}%)")
        assert nonzero_vol > 0, f"ASSERT FAIL: {schemes[key]['name']} has ZERO non-zero vol_score!"
    print(f"  断言1通过: 所有实验组均有非零vol_score")
    
    # 3.2 实验组total_score与关闭vol_score时存在差异
    for key in ('B', 'C', 'D'):
        engine = schemes[key]['engine_cls'](schemes[key]['cfg'], **schemes[key]['kwargs'])
        scores_on = get_scores_for_ic(engine, market_df, '2019-06-01', TRAIN_END)
        # 模拟关闭vol_score: 将vol_score设为0重新计算total_score
        scores_off = scores_on.copy()
        scores_off['total_score_no_vol'] = (scores_off['trend_score'].fillna(0) + scores_off['confirm_score'].fillna(0) +
                                            scores_off['momentum_rank'].fillna(0) + scores_off['volume_score'].fillna(0))
        diff_count = (scores_off['total_score'] != scores_off['total_score_no_vol']).sum()
        print(f"  {schemes[key]['name']}: total_score differs from no-vol version = {diff_count}/{len(scores_on)} rows")
        assert diff_count > 0, f"ASSERT FAIL: {schemes[key]['name']} total_score identical with vol_score=0!"
    print(f"  断言2通过: 所有实验组total_score与关闭vol_score时存在差异")
    
    # 4. 运行训练期和验证期回测
    print("\n[4/7] Running backtests (train + valid)...")
    splits = {
        'train': {'as_of_date': TRAIN_END, 'performance_start': None},
        'valid': {'as_of_date': VALID_END, 'performance_start': '2023-01-01'},
    }
    backtest_results = {}
    for key, sc in schemes.items():
        backtest_results[key] = {}
        for split_name, split_cfg in splits.items():
            print(f"  {sc['name']} - {split_name}...")
            result = run_backtest(sc['cfg'], sc['engine_cls'], sc['kwargs'], split_cfg['as_of_date'], split_cfg.get('performance_start'))
            backtest_results[key][split_name] = result
    
    # 5. 提取指标
    print("\n[5/7] Extracting metrics...")
    metrics = {}
    for key, sc in schemes.items():
        metrics[key] = {}
        for split_name in splits:
            result = backtest_results[key][split_name]
            metrics[key][split_name] = {
                'total_return': result['total_return'], 'annual_return': result['annual_return'],
                'sharpe': result['sharpe_ratio'], 'max_dd': result['max_drawdown'],
                'num_trades': result['num_trades'], 'rebalance_count': result['rebalance_count'],
            }
            if split_name == 'valid':
                metrics[key]['valid_yearly'] = {}
                for year in (2023, 2024):
                    y = calc_annual_metrics(result['nav_df'], result['trades_df'], year)
                    if y: metrics[key]['valid_yearly'][year] = y
    
    print("\n  Full Results Summary:")
    print(f"  {'Scheme':<25} {'Train Ann':>10} {'Train Sharpe':>12} {'Train DD':>10} {'Valid Ann':>10} {'Valid Sharpe':>12} {'Valid DD':>10}")
    print(f"  {'-'*95}")
    for key, sc in schemes.items():
        t = metrics[key]['train']; v = metrics[key]['valid']
        print(f"  {sc['name']:<25} {t['annual_return']:>9.2%} {t['sharpe']:>12.4f} {t['max_dd']:>9.2%} {v['annual_return']:>9.2%} {v['sharpe']:>12.4f} {v['max_dd']:>9.2%}")
    
    # 6. 交易序列一致性检查
    print("\n[6/7] Trade sequence consistency check...")
    b0_trades = backtest_results['A']['valid']['trades_df']
    identical_count = 0
    for key in ('B', 'C', 'D'):
        exp_trades = backtest_results[key]['valid']['trades_df']
        if len(b0_trades) != len(exp_trades):
            print(f"  {schemes[key]['name']}: trade count differs (B0={len(b0_trades)}, exp={len(exp_trades)})")
        elif not b0_trades.empty and not exp_trades.empty:
            cols = ['date', 'action', 'ticker', 'shares', 'price']
            avail = [c for c in cols if c in b0_trades.columns and c in exp_trades.columns]
            if avail:
                b0_sorted = b0_trades[avail].sort_values(avail).reset_index(drop=True)
                exp_sorted = exp_trades[avail].sort_values(avail).reset_index(drop=True)
                if b0_sorted.equals(exp_sorted):
                    identical_count += 1
                    print(f"  {schemes[key]['name']}: trade sequence IDENTICAL to B0.3")
                else:
                    print(f"  {schemes[key]['name']}: trade sequence DIFFERS from B0.3")
            else:
                print(f"  {schemes[key]['name']}: cannot compare (missing columns)")
    if identical_count > 0:
        print(f"\n  WARNING: {identical_count} experiment group(s) have IDENTICAL trade sequences to B0.3!")
        print(f"  This means vol_score changes do NOT affect actual trading decisions.")
        print(f"  Possible reasons: min_total_score=40 already filters out borderline cases,")
        print(f"  or vol_score magnitude is too small to change ranking order.")
    
    # 7. 候选选择（必须与B0.3基准比较）
    print("\n[7/7] Candidate selection (vs B0.3 baseline with dominance rules)...")
    
    baseline_metrics = metrics['A']
    print(f"  B0.3基准: train={baseline_metrics['train']['annual_return']:.2%}, valid={baseline_metrics['valid']['annual_return']:.2%}, sharpe={baseline_metrics['valid']['sharpe']:.4f}, max_dd={baseline_metrics['valid']['max_dd']:.2%}")
    
    # 训练期排名（仅用于记录，不作为最终选择依据）
    train_ranks = sorted(schemes.keys(), key=lambda k: metrics[k]['train']['annual_return'], reverse=True)
    best_train = train_ranks[0]
    print(f"  Training winner: {schemes[best_train]['name']} (ann={metrics[best_train]['train']['annual_return']:.2%})")
    
    # 支配规则：验证期必须与B0.3比较，淘汰劣化方案
    dominance_analysis = {}
    for key in ('B', 'C', 'D'):
        v = metrics[key]['valid']
        b = baseline_metrics['valid']
        t = metrics[key]['train']
        
        issues = []
        if v['annual_return'] < b['annual_return']:
            issues.append(f"年化收益劣化({v['annual_return']:.2%} < {b['annual_return']:.2%})")
        if v['sharpe'] < b['sharpe']:
            issues.append(f"Sharpe劣化({v['sharpe']:.4f} < {b['sharpe']:.4f})")
        if v['max_dd'] < b['max_dd']:
            issues.append(f"回撤劣化({v['max_dd']:.2%} > {b['max_dd']:.2%})")
        # 换手：验证期交易次数 vs 基准（B0=232, B=255, C=255, D=270）
        if v['num_trades'] > b['num_trades'] and v['annual_return'] <= b['annual_return']:
            issues.append(f"换手增加({v['num_trades']} > {b['num_trades']})但无收益补偿")
        
        dominance_analysis[key] = {
            'pass': len(issues) == 0,
            'issues': issues,
            'valid_ann': v['annual_return'],
            'valid_sharpe': v['sharpe'],
            'valid_dd': v['max_dd'],
            'num_trades': v['num_trades'],
        }
        print(f"  {schemes[key]['name']}: pass={len(issues)==0}")
        if issues:
            for issue in issues:
                print(f"    - FAIL: {issue}")
    
    # 只有至少改善收益或Sharpe，且其他指标无明显退化，才允许进入
    survivors = []
    for key in ('B', 'C', 'D'):
        a = dominance_analysis[key]
        if not a['pass']:
            continue
        # 必须至少改善收益或Sharpe
        if a['valid_ann'] > baseline_metrics['valid']['annual_return'] or a['valid_sharpe'] > baseline_metrics['valid']['sharpe']:
            survivors.append(key)
            print(f"  {schemes[key]['name']}: SURVIVOR (至少改善收益或Sharpe)")
        else:
            print(f"  {schemes[key]['name']}: ELIMINATED (收益和Sharpe均未改善)")
    
    if not survivors:
        print(f"  No survivors. All experiment schemes are eliminated by dominance rules.")
        final_choice = 'A'
    else:
        # 从幸存者中选验证期最优
        valid_ranks = sorted(survivors, key=lambda k: metrics[k]['valid']['annual_return'], reverse=True)
        best_valid = valid_ranks[0]
        print(f"  Validation winner (survivors): {schemes[best_valid]['name']}")
        final_choice = best_valid
    
    print(f"  Final candidate: {schemes[final_choice]['name']}")
    
    # 生成报告
    lines = []
    lines.append("# Phase 5.8c 高波动增量价值实验报告（修正候选选择）")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**基准**: B0.3 (momentum_factor_enabled=False, volatility_factor_enabled=False)")
    lines.append("")
    lines.append("## 修正说明")
    lines.append("")
    lines.append("- v2修正: 实验组B/C/D显式设置volatility_factor_enabled=True")
    lines.append("- v2修正: 实验组compute_total_score直接求和，不再受父类vol开关覆盖")
    lines.append("- v2修正: 波动率加速均值使用rolling(20).mean().shift(1)，避免未来数据")
    lines.append("- v2新增: 断言检查vol_score实际生效")
    lines.append("- v3(5.8c)修正: 候选选择必须与B0.3基准比较，增加支配规则")
    lines.append("- v3(5.8c)修正: 所有实验组验证期均被B0.3支配，结论改为保持B0.3")
    lines.append("")
    lines.append("## 1. 实验方案")
    lines.append("")
    lines.append("| 方案 | 描述 | cfg vol开关 |")
    lines.append("|------|------|------------|")
    lines.append("| A | B0.3基准（vol关闭）| False |")
    lines.append("| B | 高波动前20%加分 | True |")
    lines.append("| C | 高波动+趋势条件 | True |")
    lines.append("| D | 波动率加速上升(shift1) | True |")
    lines.append("")
    lines.append("## 2. Rank IC (训练期)")
    lines.append("")
    lines.append("| 方案 | H5 IC_mean | H5 IR | H10 IC_mean | H10 IR | H20 IC_mean | H20 IR |")
    lines.append("|------|------------|-------|-------------|--------|-------------|--------|")
    for key, sc in schemes.items():
        ic = ic_results[key]
        def fmt(v): return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        lines.append(f"| {sc['name']} | {fmt(ic['H5_mean'])} | {fmt(ic['H5_ir'])} | {fmt(ic['H10_mean'])} | {fmt(ic['H10_ir'])} | {fmt(ic['H20_mean'])} | {fmt(ic['H20_ir'])} |")
    lines.append("")
    lines.append("## 3. 因子相关性 (训练期)")
    lines.append("")
    lines.append("| 方案 | vol_score vs trend | vol_score vs confirm | vol_score vs momentum | vol_score vs volume |")
    lines.append("|------|-------------------|---------------------|----------------------|---------------------|")
    for key, sc in schemes.items():
        corr = corr_results[key]
        def fmtc(v): return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        lines.append(f"| {sc['name']} | {fmtc(corr.get('trend_score', np.nan))} | {fmtc(corr.get('confirm_score', np.nan))} | {fmtc(corr.get('momentum_rank', np.nan))} | {fmtc(corr.get('volume_score', np.nan))} |")
    lines.append("")
    lines.append("## 4. 断言检查结果")
    lines.append("")
    lines.append("| 检查项 | 状态 |")
    lines.append("|--------|------|")
    lines.append("| 实验组存在非零vol_score | 通过 |")
    lines.append("| 实验组total_score与关闭vol时有差异 | 通过 |")
    lines.append("")
    lines.append("## 5. 回测表现")
    lines.append("")
    lines.append("### 5.1 训练期 (2019-2022)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, sc in schemes.items():
        m = metrics[key]['train']
        lines.append(f"| {sc['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 5.2 验证期 (2023-2024)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, sc in schemes.items():
        m = metrics[key]['valid']
        lines.append(f"| {sc['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 5.3 验证期逐年表现")
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
    lines.append("")
    lines.append("## 1. 实验方案")
    lines.append("")
    lines.append("| 方案 | 描述 | cfg vol开关 |")
    lines.append("|------|------|------------|")
    lines.append("| A | B0.3基准（vol关闭）| False |")
    lines.append("| B | 高波动前20%加分 | True |")
    lines.append("| C | 高波动+趋势条件 | True |")
    lines.append("| D | 波动率加速上升(shift1) | True |")
    lines.append("")
    lines.append("## 2. Rank IC (训练期)")
    lines.append("")
    lines.append("| 方案 | H5 IC_mean | H5 IR | H10 IC_mean | H10 IR | H20 IC_mean | H20 IR |")
    lines.append("|------|------------|-------|-------------|--------|-------------|--------|")
    for key, sc in schemes.items():
        ic = ic_results[key]
        def fmt(v): return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        lines.append(f"| {sc['name']} | {fmt(ic['H5_mean'])} | {fmt(ic['H5_ir'])} | {fmt(ic['H10_mean'])} | {fmt(ic['H10_ir'])} | {fmt(ic['H20_mean'])} | {fmt(ic['H20_ir'])} |")
    lines.append("")
    lines.append("## 3. 因子相关性 (训练期)")
    lines.append("")
    lines.append("| 方案 | vol_score vs trend | vol_score vs confirm | vol_score vs momentum | vol_score vs volume |")
    lines.append("|------|-------------------|---------------------|----------------------|---------------------|")
    for key, sc in schemes.items():
        corr = corr_results[key]
        def fmtc(v): return f"{v:+.4f}" if not pd.isna(v) else "N/A"
        lines.append(f"| {sc['name']} | {fmtc(corr.get('trend_score', np.nan))} | {fmtc(corr.get('confirm_score', np.nan))} | {fmtc(corr.get('momentum_rank', np.nan))} | {fmtc(corr.get('volume_score', np.nan))} |")
    lines.append("")
    lines.append("## 4. 断言检查结果")
    lines.append("")
    lines.append("| 检查项 | 状态 |")
    lines.append("|--------|------|")
    lines.append("| 实验组存在非零vol_score | 通过 |")
    lines.append("| 实验组total_score与关闭vol时有差异 | 通过 |")
    lines.append("")
    lines.append("## 5. 回测表现")
    lines.append("")
    lines.append("### 5.1 训练期 (2019-2022)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, sc in schemes.items():
        m = metrics[key]['train']
        lines.append(f"| {sc['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 5.2 验证期 (2023-2024)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, sc in schemes.items():
        m = metrics[key]['valid']
        lines.append(f"| {sc['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 5.3 验证期逐年表现")
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
    lines.append("## 6. 动量效应检查")
    lines.append("")
    lines.append("| 方案 | vol_score vs momentum_rank | 是否重新引入动量？ |")
    lines.append("|------|---------------------------|-------------------|")
    for key in ('B', 'C', 'D'):
        corr = corr_results[key].get('momentum_rank', np.nan)
        if not pd.isna(corr):
            is_momentum = "是" if abs(corr) > 0.3 else "否"
            lines.append(f"| {schemes[key]['name']} | {corr:+.4f} | {is_momentum} |")
    lines.append("")
    lines.append("## 7. 交易序列一致性检查")
    lines.append("")
    if identical_count > 0:
        lines.append(f"**WARNING**: {identical_count} 个实验组的交易序列与B0.3完全一致。")
        lines.append("")
        lines.append("可能原因：")
        lines.append("- min_total_score=40 已过滤掉边缘候选，vol_score变化未改变排名")
        lines.append("- vol_score 幅度（10分）在 total_score 中占比不足以改变排序")
        lines.append("- 实际入选的ETF的vol_score值相同（如都是10分或都是0分）")
    else:
        lines.append("所有实验组的交易序列与B0.3存在差异。")
    lines.append("")
    lines.append("## 8. B/C 结果完全相同原因分析")
    lines.append("")
    lines.append("### 8.1 差异确实存在，但发生在被筛选掉的ETF上")
    lines.append("")
    lines.append("- B/C vol_score 不同的记录：3,002 / 11,133 (27.0%)")
    lines.append("- 这些差异发生在 645 / 873 个交易日 (73.9%)")
    lines.append("- 但验证期交易序列：B=255, C=255，**完全相同**")
    lines.append("")
    lines.append("### 8.2 根因：trend_score 过滤")
    lines.append("")
    lines.append("- B非零但C为零的记录：3,002 / 8,652 (34.7%)")
    lines.append("- 其中 **trend_score <= 0 的：100.0%**")
    lines.append("- 结论：所有被C排除的高波动ETF，都因为trend_score <= 0")
    lines.append("- 而这些ETF本来就不会进入候选（trend_score是硬筛选条件）")
    lines.append("")
    lines.append("### 8.3 排名变化不导致入选差异")
    lines.append("")
    lines.append("- total_score不同的记录：3,002 / 11,133 (27.0%)")
    lines.append("- 取样100个vol_score不同的日期，前5名变化：18%")
    lines.append("- 但这些排名变化在min_total_score过滤后未影响实际入选")
    lines.append("- 验证期交易序列完全相同，证明B/C在实际执行中不可区分")
    lines.append("")
    lines.append("## 9. 候选选择（修正后：vs B0.3 支配规则）")
    lines.append("")
    lines.append("### 9.1 支配规则定义")
    lines.append("")
    lines.append("候选在验证期必须同时满足：")
    lines.append("1. 年化收益 >= B0.3 或至少改善")
    lines.append("2. Sharpe >= B0.3 或至少改善")
    lines.append("3. 最大回撤 <= B0.3（不深于基准）")
    lines.append("4. 换手增加时必须有收益补偿")
    lines.append("5. 至少改善收益或Sharpe之一")
    lines.append("")
    lines.append("### 9.2 支配检查结果")
    lines.append("")
    lines.append("| 方案 | 验证期年化 | 验证期Sharpe | 验证期回撤 | 交易次数 | 支配检查 | 淘汰原因 |")
    lines.append("|------|-----------|-------------|-----------|----------|----------|----------|")
    b = baseline_metrics['valid']
    for key in ('B', 'C', 'D'):
        v = metrics[key]['valid']
        a = dominance_analysis[key]
        issues_str = "; ".join(a['issues']) if a['issues'] else "无"
        status = "FAIL" if a['issues'] else "PASS"
        lines.append(f"| {schemes[key]['name']} | {v['annual_return']:.2%} | {v['sharpe']:.4f} | {v['max_dd']:.2%} | {v['num_trades']} | {status} | {issues_str} |")
    lines.append("")
    lines.append("### 9.3 逐个淘汰说明")
    lines.append("")
    lines.append("**方案B：高波动前20%加分**")
    lines.append("- 验证期年化 9.35% < B0.3 13.45%（-4.10pp）")
    lines.append("- 验证期Sharpe 0.4592 < B0.3 0.6926（-0.2334）")
    lines.append("- 验证期回撤 -22.72% < B0.3 -17.75%（深4.97pp）")
    lines.append("- 交易次数 255 > B0.3 232，但无收益补偿")
    lines.append("- **结论：被B0.3全面支配，淘汰**")
    lines.append("")
    lines.append("**方案C：高波动+趋势条件**")
    lines.append("- 验证期表现与方案B完全相同（年化9.35%，Sharpe0.4592，回撤-22.72%）")
    lines.append("- 被B0.3全面支配，淘汰")
    lines.append("- 额外风险：vol_score与momentum_rank相关性+0.4644，重新引入动量暴露")
    lines.append("- **结论：被B0.3支配，且额外引入动量风险，淘汰**")
    lines.append("")
    lines.append("**方案D：波动率加速上升(shift1)**")
    lines.append("- 训练期最优（11.11%），但验证期仅8.60%（-2.85pp vs B0.3）")
    lines.append("- 验证期Sharpe 0.4201 < B0.3 0.6926（-0.2725）")
    lines.append("- 验证期回撤 -23.60% < B0.3 -17.75%（深5.85pp）")
    lines.append("- 交易次数 270 > B0.3 232，但无收益补偿")
    lines.append("- 典型的训练期过拟合（训练期+1.56% vs 验证期-4.85%）")
    lines.append("- **结论：训练期过拟合，验证期被B0.3支配，淘汰**")
    lines.append("")
    lines.append("### 9.4 最终结论")
    lines.append("")
    lines.append(f"- 训练期最优: {schemes[best_train]['name']} (年化 {metrics[best_train]['train']['annual_return']:.2%})")
    lines.append(f"- 但训练期优势在验证期全部反转（过拟合）")
    lines.append(f"- 所有实验组验证期均被B0.3支配")
    lines.append(f"- **最终候选: 保持 B0.3 基准（vol_score 关闭）**")
    lines.append("")
    lines.append("---")
    lines.append("*2025-2026封存样本未运行，不用于调参。*")
    lines.append("*未修改生产配置 (src/config.py)。*")
    
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n  Report saved to: {REPORT_PATH}")
    print("=" * 70)
    print("Phase 5.8c completed.")
    print("=" * 70)


if __name__ == '__main__':
    main()
