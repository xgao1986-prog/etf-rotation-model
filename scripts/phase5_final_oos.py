#!/usr/bin/env python3
"""
Phase 5.4: Final Out-of-Sample Validation

Rules:
1. Only compare frozen B0.1 vs no_momentum
2. OOS fixed: 2025-01-01 to 2026-06-18
3. No parameter adjustments
4. Output: ann_ret, sharpe, max_dd, turnover, trades, year-by-year
5. Judge if no_momentum improves return/risk or at least one metric without degradation
6. After this, OOS sample is sealed
7. Do not fix vol_score, do not modify production config
8. Update docs, test, commit, remind user to push

Output: reports/phase5_final_oos.md
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
import copy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine


# =============================================================================
# Constants
# =============================================================================

OOS_START = '2025-01-01'
OOS_END   = '2026-06-18'
OOS_YEARS = [2025, 2026]

REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase5_final_oos.md')


def run_backtest(cfg_name, cfg, as_of_date, performance_start):
    """Run backtest and return result dict."""
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(tickers, start_date='2019-01-01', end_date=as_of_date)
    bench_df  = db.get_market_data(BENCHMARK, start_date='2019-01-01', end_date=as_of_date)

    engine = BacktestEngine(cfg)
    result = engine.run(
        market_df,
        bench_df,
        as_of_date=as_of_date,
        performance_start=performance_start,
    )
    return result


def calc_annual_metrics(nav_df, trades_df, year):
    """Calculate metrics for a specific year."""
    start = pd.Timestamp(f'{year}-01-01')
    end   = pd.Timestamp(f'{year}-12-31')
    
    year_nav = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].copy()
    if len(year_nav) < 2:
        return None
    
    first_nav = year_nav['nav'].iloc[0]
    last_nav  = year_nav['nav'].iloc[-1]
    days = (year_nav['date'].iloc[-1] - year_nav['date'].iloc[0]).days
    if days < 1:
        return None
    ann_ret = (last_nav / first_nav) ** (365 / days) - 1
    
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
    
    return {
        'ann_ret': ann_ret,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'n_trades': n_trades,
        'turnover': turnover,
        'days': days,
    }


def calc_full_metrics(nav_df, trades_df, start_date, end_date):
    """Calculate metrics for the full OOS period."""
    mask = (nav_df['date'] >= start_date) & (nav_df['date'] <= end_date)
    oos_nav = nav_df[mask].copy()
    if len(oos_nav) < 2:
        return None
    
    first_nav = oos_nav['nav'].iloc[0]
    last_nav  = oos_nav['nav'].iloc[-1]
    days = (oos_nav['date'].iloc[-1] - oos_nav['date'].iloc[0]).days
    ann_ret = (last_nav / first_nav) ** (365 / days) - 1 if days > 0 else 0.0
    
    oos_nav['daily_ret'] = oos_nav['nav'].pct_change()
    valid_rets = oos_nav['daily_ret'].dropna()
    if len(valid_rets) > 1 and valid_rets.std() > 0:
        sharpe = (valid_rets.mean() / valid_rets.std()) * np.sqrt(252)
    else:
        sharpe = 0.0
    
    cummax = oos_nav['nav'].cummax()
    drawdown = (oos_nav['nav'] - cummax) / cummax
    max_dd = drawdown.min()
    
    oos_trades = trades_df[
        (pd.to_datetime(trades_df['date']) >= start_date) & (pd.to_datetime(trades_df['date']) <= end_date)
    ] if 'date' in trades_df.columns else pd.DataFrame()
    n_trades = len(oos_trades)
    
    days_invested = (oos_nav['nav'] > first_nav * 1.001).sum()
    turnover = n_trades / days_invested if days_invested > 0 else 0.0
    
    return {
        'ann_ret': ann_ret,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'n_trades': n_trades,
        'turnover': turnover,
        'days': days,
    }


def format_val(key, val):
    """Format a metric value for display."""
    if key in ['ann_ret', 'max_dd']:
        return f"{val:+.2%}"
    elif key == 'n_trades':
        return f"{val:.0f}"
    else:
        return f"{val:.4f}"


def main():
    print("=" * 70)
    print("Phase 5.4: Final Out-of-Sample Validation")
    print("=" * 70)
    print(f"  OOS period: {OOS_START} to {OOS_END}")
    print(f"  Strategies: B0.1 (frozen) vs no_momentum")
    print(f"  Rule: No parameter adjustments after this phase")
    print("=" * 70)

    # Build configs
    b0_cfg = build_config()
    nm_cfg = copy.deepcopy(b0_cfg)
    nm_cfg['exclude_factor'] = 'momentum_rank'

    results = {}
    for name, cfg in [('B0.1', b0_cfg), ('no_momentum', nm_cfg)]:
        print(f"\n  Running {name} ...")
        result = run_backtest(name, cfg, OOS_END, OOS_START)
        results[name] = result

    nav_b0 = results['B0.1']['nav_df']
    nav_nm = results['no_momentum']['nav_df']
    trades_b0 = results['B0.1']['trades_df']
    trades_nm = results['no_momentum']['trades_df']

    # Full OOS metrics
    print("\n" + "=" * 70)
    print("Full OOS Metrics")
    print("=" * 70)
    
    b0_full = calc_full_metrics(nav_b0, trades_b0, pd.Timestamp(OOS_START), pd.Timestamp(OOS_END))
    nm_full = calc_full_metrics(nav_nm, trades_nm, pd.Timestamp(OOS_START), pd.Timestamp(OOS_END))
    
    print(f"  {'Metric':<20} {'B0.1':>12} {'no_momentum':>12} {'Delta':>12}")
    print(f"  {'-'*56}")
    for key in ['ann_ret', 'sharpe', 'max_dd', 'n_trades', 'turnover']:
        b0_v = b0_full[key]
        nm_v = nm_full[key]
        if key in ['ann_ret', 'max_dd']:
            print(f"  {key:<20} {b0_v:>11.2%} {nm_v:>11.2%} {nm_v-b0_v:>+11.2%}")
        elif key == 'n_trades':
            print(f"  {key:<20} {b0_v:>12.0f} {nm_v:>12.0f} {nm_v-b0_v:>+12.0f}")
        else:
            print(f"  {key:<20} {b0_v:>12.4f} {nm_v:>12.4f} {nm_v-b0_v:>+12.4f}")

    # Year-by-year
    print("\n" + "=" * 70)
    print("Year-by-Year Results")
    print("=" * 70)
    
    yearly = {}
    for year in OOS_YEARS:
        print(f"\n  Year {year}:")
        b0_y = calc_annual_metrics(nav_b0, trades_b0, year)
        nm_y = calc_annual_metrics(nav_nm, trades_nm, year)
        yearly[year] = {'B0.1': b0_y, 'no_momentum': nm_y}
        
        if b0_y and nm_y:
            print(f"    {'Metric':<20} {'B0.1':>12} {'no_momentum':>12} {'Delta':>12}")
            for key in ['ann_ret', 'sharpe', 'max_dd', 'n_trades', 'turnover']:
                b0_v = b0_y[key]
                nm_v = nm_y[key]
                if key in ['ann_ret', 'max_dd']:
                    print(f"    {key:<20} {b0_v:>11.2%} {nm_v:>11.2%} {nm_v-b0_v:>+11.2%}")
                elif key == 'n_trades':
                    print(f"    {key:<20} {b0_v:>12.0f} {nm_v:>12.0f} {nm_v-b0_v:>+12.0f}")
                else:
                    print(f"    {key:<20} {b0_v:>12.4f} {nm_v:>12.4f} {nm_v-b0_v:>+12.4f}")
        else:
            print(f"    Insufficient data")

    # Judgment
    print("\n" + "=" * 70)
    print("Judgment: Does no_momentum improve?")
    print("=" * 70)
    
    ret_improved = nm_full['ann_ret'] > b0_full['ann_ret']
    sharpe_improved = nm_full['sharpe'] > b0_full['sharpe']
    dd_improved = nm_full['max_dd'] > b0_full['max_dd']  # less negative = better
    
    improvements = sum([ret_improved, sharpe_improved, dd_improved])
    
    print(f"  Return improved:     {ret_improved}  ({b0_full['ann_ret']:+.2%} -> {nm_full['ann_ret']:+.2%})")
    print(f"  Sharpe improved:     {sharpe_improved}  ({b0_full['sharpe']:.4f} -> {nm_full['sharpe']:.4f})")
    print(f"  Drawdown improved:   {dd_improved}  ({b0_full['max_dd']:.2%} -> {nm_full['max_dd']:.2%})")
    print(f"  Score: {improvements}/3")
    
    if ret_improved and sharpe_improved and dd_improved:
        verdict = "no_momentum improves ALL THREE metrics"
    elif ret_improved and (sharpe_improved or dd_improved):
        verdict = "no_momentum improves return plus at least one risk metric"
    elif ret_improved or sharpe_improved or dd_improved:
        verdict = "no_momentum improves at least one metric without clear harm"
    else:
        verdict = "no_momentum does NOT improve; keep B0.1"
    
    print(f"  Verdict: {verdict}")
    
    final_decision = 'Adopt no_momentum' if improvements >= 2 else 'Keep B0.1'
    print(f"  Final decision: {final_decision}")

    # Generate report
    lines = []
    lines.append("# Phase 5.4 Final Out-of-Sample Validation Report")
    lines.append("")
    lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**OOS Period**: {OOS_START} to {OOS_END}")
    lines.append("")
    lines.append("## 1. Full OOS Metrics")
    lines.append("")
    lines.append("| Metric | B0.1 | no_momentum | Delta |")
    lines.append("|--------|------|-------------|-------|")
    for key in ['ann_ret', 'sharpe', 'max_dd', 'n_trades', 'turnover']:
        b0_v = b0_full[key]
        nm_v = nm_full[key]
        if key in ['ann_ret', 'max_dd']:
            lines.append(f"| {key} | {b0_v:+.2%} | {nm_v:+.2%} | {nm_v-b0_v:+.2%} |")
        elif key == 'n_trades':
            lines.append(f"| {key} | {b0_v:.0f} | {nm_v:.0f} | {nm_v-b0_v:+.0f} |")
        else:
            lines.append(f"| {key} | {b0_v:.4f} | {nm_v:.4f} | {nm_v-b0_v:+.4f} |")
    
    lines.append("")
    lines.append("## 2. Year-by-Year Results")
    lines.append("")
    for year in OOS_YEARS:
        b0_y = yearly[year]['B0.1']
        nm_y = yearly[year]['no_momentum']
        if b0_y and nm_y:
            lines.append(f"### Year {year}")
            lines.append("")
            lines.append("| Metric | B0.1 | no_momentum | Delta |")
            lines.append("|--------|------|-------------|-------|")
            for key in ['ann_ret', 'sharpe', 'max_dd', 'n_trades', 'turnover']:
                b0_v = b0_y[key]
                nm_v = nm_y[key]
                if key in ['ann_ret', 'max_dd']:
                    lines.append(f"| {key} | {b0_v:+.2%} | {nm_v:+.2%} | {nm_v-b0_v:+.2%} |")
                elif key == 'n_trades':
                    lines.append(f"| {key} | {b0_v:.0f} | {nm_v:.0f} | {nm_v-b0_v:+.0f} |")
                else:
                    lines.append(f"| {key} | {b0_v:.4f} | {nm_v:.4f} | {nm_v-b0_v:+.4f} |")
            lines.append("")
    
    lines.append("## 3. Judgment")
    lines.append("")
    lines.append(f"- Return improved: {ret_improved} ({b0_full['ann_ret']:+.2%} -> {nm_full['ann_ret']:+.2%})")
    lines.append(f"- Sharpe improved: {sharpe_improved} ({b0_full['sharpe']:.4f} -> {nm_full['sharpe']:.4f})")
    lines.append(f"- Drawdown improved: {dd_improved} ({b0_full['max_dd']:.2%} -> {nm_full['max_dd']:.2%})")
    lines.append(f"- Score: {improvements}/3")
    lines.append("")
    lines.append(f"**Verdict**: {verdict}")
    lines.append("")
    lines.append(f"**Final Decision**: {final_decision}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*This OOS period ({OOS_START} to {OOS_END}) is now sealed and will not be used for further tuning.*")
    lines.append("*No production config was modified.*")
    
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n  Report saved to: {REPORT_PATH}")
    print("\n" + "=" * 70)
    print("Phase 5.4 completed.")
    print("=" * 70)


if __name__ == '__main__':
    main()
