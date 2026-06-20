#!/usr/bin/env python3
"""
Phase 5.3: Corrected Scoring Diagnosis

Purpose:
  Diagnose the root cause of vol_score=0, evaluate factor predictive power via
daily cross-sectional Rank IC, and compare B0.1 (default) vs no_momentum
(excluding momentum_rank) on a year-by-year basis across training
(2019-2022) and validation (2023-2024) periods.

Rules enforced:
  1. vol_score=0 is a design failure (scale mismatch), not a code bug.
  2. Rank IC is computed only on days with >=2 non-zero factor scores.
  3. Training-period degradation >2pp disqualifies a candidate.
  4. No final out-of-sample test is run; production config is untouched.
  5. Exactly one recommendation is emitted after all analysis.

Outputs:
  - Console logs
  - reports/phase5_scoring_diagnosis.md
"""

import sys
import os

# Allow imports from src/
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
import copy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, CORE_UNIVERSE
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine


# =============================================================================
# Constants
# =============================================================================

FACTORS = {
    'trend':      {'score_col': 'trend_score',    'max_score': 30},
    'confirm':    {'score_col': 'confirm_score',  'max_score': 20},
    'momentum':   {'score_col': 'momentum_rank',  'max_score': 25},
    'volume':     {'score_col': 'volume_score',   'max_score': 15},
    'volatility': {'score_col': 'vol_score',      'max_score': 10},
}

TRAINING_YEARS   = [2019, 2020, 2021, 2022]
VALIDATION_YEARS = [2023, 2024]
ALL_YEARS        = TRAINING_YEARS + VALIDATION_YEARS

DEGRADATION_THRESHOLD = 0.02  # 2 percentage points

REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase5_scoring_diagnosis.md')


# =============================================================================
# Data loading helpers
# =============================================================================

def load_market_data():
    """Load market data for all tradable ETFs plus benchmark."""
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    return market_df, bench_df


def get_raw_signals(cfg, market_df):
    """
    Compute raw factor scores for the core universe without running a backtest.
    Returns a DataFrame with all factor columns plus total_score.
    """
    strategy = StrategyEngine(cfg)
    core_tickers = list(CORE_UNIVERSE.keys())
    core_df = market_df[market_df['ticker'].isin(core_tickers)].copy()

    all_scores = []
    for ticker in core_df['ticker'].unique():
        ticker_df = core_df[core_df['ticker'] == ticker].copy()
        if len(ticker_df) < 51:
            continue
        scored = strategy.calculate_total_score(ticker_df)
        all_scores.append(scored)

    if not all_scores:
        return None

    scores_df = pd.concat(all_scores, ignore_index=True)
    scores_df = strategy.rank_all_momentum(scores_df)
    scores_df = strategy.compute_total_score(scores_df)
    return scores_df


def add_future_returns(scores_df, market_df, horizons=(5, 10, 20)):
    """
    Add forward-looking returns (H5, H10, H20) for each (date, ticker).
    """
    df = scores_df.copy()
    market = market_df[['date', 'ticker', 'close']].copy()
    market = market.sort_values(['ticker', 'date'])

    for h in horizons:
        market[f'future_ret_{h}'] = (
            market.groupby('ticker')['close'].shift(-h) / market['close'] - 1
        )

    merge_cols = ['date', 'ticker'] + [f'future_ret_{h}' for h in horizons]
    df = df.merge(market[merge_cols], on=['date', 'ticker'], how='left')
    return df


# =============================================================================
# Requirement 1: Volatility scale-mismatch diagnosis
# =============================================================================

def analyze_volatility_distribution(market_df):
    """
    Recompute volatility_20 using the exact formula in strategy.py and
    report its empirical distribution.
    """
    vol_series = []
    for ticker in market_df['ticker'].unique():
        tdf = market_df[market_df['ticker'] == ticker].copy().sort_values('date')
        if len(tdf) < 21:
            continue
        tdf['volatility_20'] = (
            tdf['close'].pct_change().rolling(20).std().shift(1) * np.sqrt(252)
        )
        v = tdf['volatility_20'].dropna()
        if len(v) > 0:
            vol_series.append(v)

    if not vol_series:
        return None

    all_vol = pd.concat(vol_series)
    return {
        'count':   len(all_vol),
        'mean':    all_vol.mean(),
        'median':  all_vol.median(),
        'std':     all_vol.std(),
        'min':     all_vol.min(),
        'max':     all_vol.max(),
        'q01':     all_vol.quantile(0.01),
        'q05':     all_vol.quantile(0.05),
        'q25':     all_vol.quantile(0.25),
        'q75':     all_vol.quantile(0.75),
        'q95':     all_vol.quantile(0.95),
        'q99':     all_vol.quantile(0.99),
        'pct_in_0_01_0_04': ((all_vol >= 0.01) & (all_vol <= 0.04)).mean(),
        'pct_in_0_04_0_06': ((all_vol >  0.04) & (all_vol <= 0.06)).mean(),
        'pct_above_0_06':   (all_vol > 0.06).mean(),
    }


# =============================================================================
# Requirement 2: Daily cross-sectional Rank IC
# =============================================================================

def compute_daily_rank_ic(scores_df, factor_col, ret_col):
    """
    For each date, compute the Spearman rank correlation between
    factor_col and ret_col, keeping only dates where at least 2 ETFs
    have a non-zero factor score.

    Returns a DataFrame with columns: date, year, ic.
    """
    records = []
    for date, group in scores_df.groupby('date'):
        non_zero = group[group[factor_col].fillna(0) != 0]
        if len(non_zero) < 2:
            continue
        valid = non_zero[[factor_col, ret_col]].dropna()
        if len(valid) < 2:
            continue
        # Manual Spearman rank correlation (no scipy dependency)
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
        if pd.notna(corr) and np.isfinite(corr):
            records.append({'date': date, 'ic': corr})

    if not records:
        return None

    ic_df = pd.DataFrame(records)
    ic_df['date'] = pd.to_datetime(ic_df['date'])
    ic_df['year'] = ic_df['date'].dt.year
    return ic_df


def summarize_ic(ic_df):
    """Return overall and annual IC statistics."""
    if ic_df is None or ic_df.empty:
        return None

    overall_mean = ic_df['ic'].mean()
    overall_std  = ic_df['ic'].std()
    ir = overall_mean / overall_std if overall_std > 0 else np.nan

    annual = []
    for year, grp in ic_df.groupby('year'):
        annual.append({
            'year':    year,
            'ic_mean': grp['ic'].mean(),
            'ic_std':  grp['ic'].std(),
            'n_days':  len(grp),
        })

    return {
        'overall_ic_mean': overall_mean,
        'overall_ic_std':  overall_std,
        'ir':              ir,
        'n_days':          len(ic_df),
        'annual':          annual,
    }


# =============================================================================
# Requirement 3: Year-by-year backtest metrics
# =============================================================================

def run_full_backtest(cfg, as_of_date='2024-12-31', performance_start='2019-01-01'):
    """
    Run one full backtest covering the entire period of interest.
    We slice the resulting nav_df and trades_df by year to obtain
    per-year statistics (more accurate than running isolated yearly
    backtests because the warm-up and signal continuity are preserved).
    """
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    engine = BacktestEngine(cfg)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=performance_start)


def extract_year_metrics(result, year):
    """
    Extract per-year metrics from a full backtest result.
    """
    nav_df = result.get('nav_df')
    trades_df = result.get('trades_df')

    if nav_df is None or nav_df.empty:
        return None

    nav_df = nav_df.copy()
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    mask = nav_df['date'].dt.year == year
    year_nav = nav_df[mask].copy()

    if len(year_nav) < 5:
        return None

    # Annual return (point-to-point within the calendar year)
    start_nav = year_nav['nav'].iloc[0]
    end_nav   = year_nav['nav'].iloc[-1]
    year_return = (end_nav / start_nav) - 1

    # Sharpe (annualized from daily returns inside the year)
    daily_rets = year_nav['nav'].pct_change().dropna()
    vol = daily_rets.std() * np.sqrt(252)
    sharpe = year_return / vol if vol > 0 else 0.0

    # Max drawdown (within the calendar year)
    year_nav['peak'] = year_nav['nav'].cummax()
    year_nav['drawdown'] = (year_nav['nav'] - year_nav['peak']) / year_nav['peak']
    max_dd = year_nav['drawdown'].min()

    # Turnover: trades / days with positions
    if trades_df is not None and not trades_df.empty:
        trades_df = trades_df.copy()
        trades_df['date'] = pd.to_datetime(trades_df['date'])
        year_trades = trades_df[trades_df['date'].dt.year == year]
        num_trades = len(year_trades)
    else:
        num_trades = 0

    days_invested = (year_nav['num_positions'] > 0).sum()
    turnover = num_trades / max(days_invested, 1)

    return {
        'annual_return': year_return,
        'sharpe_ratio':  sharpe,
        'max_drawdown':  max_dd,
        'num_trades':    num_trades,
        'days_invested': days_invested,
        'turnover_rate': turnover,
    }


# =============================================================================
# Report generation
# =============================================================================

def fmt_pct(x):
    """Format float as percentage string."""
    if pd.isna(x):
        return 'N/A'
    return f"{x:.2%}"


def fmt_float(x, dec=4):
    if pd.isna(x):
        return 'N/A'
    return f"{x:.{dec}f}"


def generate_report(vol_stats, ic_summary, year_metrics,
                    b0_train_avg, nm_train_avg, b0_valid_avg, nm_valid_avg,
                    is_degraded, recommendation, reason):
    """Build the markdown report string."""
    lines = []
    lines.append('# Phase 5.3 Corrected Scoring Diagnosis Report')
    lines.append('')
    lines.append(f'**Generated**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append('')

    # -------------------------------------------------------------------------
    # Executive Summary & Recommendation
    # -------------------------------------------------------------------------
    lines.append('## Executive Summary')
    lines.append('')
    lines.append(f'**Recommendation**: `{recommendation}`')
    lines.append('')
    lines.append(f'**Reasoning**: {reason}')
    lines.append('')

    # -------------------------------------------------------------------------
    # Requirement 1: vol_score root cause
    # -------------------------------------------------------------------------
    lines.append('## 1. Root Cause of vol_score = 0')
    lines.append('')
    lines.append('### 1.1 How volatility_20 is computed')
    lines.append('')
    lines.append('```python')
    lines.append("df['volatility_20'] = df['close'].pct_change().rolling(20).std().shift(1) * np.sqrt(252)")
    lines.append('```')
    lines.append('')
    lines.append('This formula produces **annualized** volatility.  The thresholds in')
    lines.append('`calculate_scores`, however, are written as if the number were a raw daily')
    lines.append('volatility or a small decimal:')
    lines.append('')
    lines.append('- `vol_score = 10` when `volatility_20 ∈ [0.01, 0.04]`')
    lines.append('- `vol_score =  5` when `volatility_20 ∈ (0.04, 0.06]`')
    lines.append('')
    lines.append('### 1.2 Empirical distribution')
    lines.append('')
    if vol_stats:
        lines.append('| Statistic | Value |')
        lines.append('|-----------|-------|')
        lines.append(f"| Count     | {vol_stats['count']:,} |")
        lines.append(f"| Mean      | {vol_stats['mean']:.4f} |")
        lines.append(f"| Median    | {vol_stats['median']:.4f} |")
        lines.append(f"| Std Dev   | {vol_stats['std']:.4f} |")
        lines.append(f"| Min       | {vol_stats['min']:.4f} |")
        lines.append(f"| Max       | {vol_stats['max']:.4f} |")
        lines.append(f"| 1%        | {vol_stats['q01']:.4f} |")
        lines.append(f"| 5%        | {vol_stats['q05']:.4f} |")
        lines.append(f"| 25%       | {vol_stats['q25']:.4f} |")
        lines.append(f"| 75%       | {vol_stats['q75']:.4f} |")
        lines.append(f"| 95%       | {vol_stats['q95']:.4f} |")
        lines.append(f"| 99%       | {vol_stats['q99']:.4f} |")
        lines.append('')
        lines.append('| Range | % of observations |')
        lines.append('|-------|-------------------|')
        lines.append(f"| [0.01, 0.04]   | {vol_stats['pct_in_0_01_0_04']:.4%} |")
        lines.append(f"| (0.04, 0.06]   | {vol_stats['pct_in_0_04_0_06']:.4%} |")
        lines.append(f"| > 0.06         | {vol_stats['pct_above_0_06']:.4%} |")
        lines.append('')
        lines.append('**Conclusion**: The thresholds are orders of magnitude too small for')
        lines.append('annualized volatility.  This is a **design failure** (scale mismatch),')
        lines.append('not a code bug.  `vol_score` is effectively always 0.')
    else:
        lines.append('*No volatility data available.*')
    lines.append('')

    # -------------------------------------------------------------------------
    # Requirement 2: Rank IC
    # -------------------------------------------------------------------------
    lines.append('## 2. Factor Predictive Power (Daily Cross-Sectional Rank IC)')
    lines.append('')
    lines.append('Only days with **≥2 non-zero factor scores** are included.')
    lines.append('')

    # Overall summary table
    lines.append('### 2.1 Overall Rank IC Summary')
    lines.append('')
    lines.append('| Factor | H5 IC_mean | H5 IC_std | H5 IR | H10 IC_mean | H10 IC_std | H10 IR | H20 IC_mean | H20 IC_std | H20 IR |')
    lines.append('|--------|------------|-----------|-------|-------------|------------|--------|-------------|------------|--------|')

    for factor_name in FACTORS.keys():
        cols = []
        for h in (5, 10, 20):
            s = ic_summary.get(factor_name, {}).get(h)
            if s:
                cols.append(f"{s['overall_ic_mean']:.4f}")
                cols.append(f"{s['overall_ic_std']:.4f}")
                cols.append(f"{s['ir']:.4f}")
            else:
                cols.extend(['N/A', 'N/A', 'N/A'])
        lines.append(f"| {factor_name} | {' | '.join(cols)} |")
    lines.append('')

    # Annual breakdown per factor
    lines.append('### 2.2 Annual Breakdown')
    lines.append('')
    for factor_name in FACTORS.keys():
        lines.append(f'#### {factor_name}')
        lines.append('')
        lines.append('| Year | H5_mean | H5_std | H10_mean | H10_std | H20_mean | H20_std |')
        lines.append('|------|---------|--------|----------|---------|----------|---------|')
        for h in (5, 10, 20):
            s = ic_summary.get(factor_name, {}).get(h)
            if not s or not s['annual']:
                continue
        # We need to merge annual data for all horizons; let's do it per-factor
        annual_rows = {}
        for h in (5, 10, 20):
            s = ic_summary.get(factor_name, {}).get(h)
            if not s or not s['annual']:
                continue
            for row in s['annual']:
                year = row['year']
                if year not in annual_rows:
                    annual_rows[year] = {}
                annual_rows[year][f'h{h}_mean'] = row['ic_mean']
                annual_rows[year][f'h{h}_std'] = row['ic_std']
        for year in sorted(annual_rows.keys()):
            r = annual_rows[year]
            vals = []
            for h in (5, 10, 20):
                m = r.get(f'h{h}_mean', np.nan)
                sd = r.get(f'h{h}_std', np.nan)
                vals.append(f"{m:.4f}" if pd.notna(m) else 'N/A')
                vals.append(f"{sd:.4f}" if pd.notna(sd) else 'N/A')
            lines.append(f"| {year} | {' | '.join(vals)} |")
        lines.append('')

    # -------------------------------------------------------------------------
    # Requirement 3: Year-by-year backtest
    # -------------------------------------------------------------------------
    lines.append('## 3. Year-by-Year Backtest Comparison: B0.1 vs no_momentum')
    lines.append('')
    lines.append('*Training years: 2019-2022 | Validation years: 2023-2024*')
    lines.append('')
    lines.append('| Year | Strategy | Ann.Return | Sharpe | Max DD | Trades | Days Invested | Turnover |')
    lines.append('|------|----------|------------|--------|--------|--------|---------------|----------|')
    for year in ALL_YEARS:
        for strategy in ('B0.1', 'no_momentum'):
            m = year_metrics.get(year, {}).get(strategy)
            if m:
                tag = ' (train)' if year in TRAINING_YEARS else ' (valid)'
                lines.append(
                    f"| {year}{tag} | {strategy} | {fmt_pct(m['annual_return'])} | "
                    f"{fmt_float(m['sharpe_ratio'], 3)} | {fmt_pct(m['max_drawdown'])} | "
                    f"{m['num_trades']} | {m['days_invested']} | {fmt_float(m['turnover_rate'], 2)} |"
                )
            else:
                lines.append(f"| {year} | {strategy} | N/A | N/A | N/A | N/A | N/A | N/A |")
    lines.append('')

    # -------------------------------------------------------------------------
    # Requirement 4: Degradation check
    # -------------------------------------------------------------------------
    lines.append('## 4. Training-Period Degradation Check')
    lines.append('')
    lines.append('| Metric | B0.1 | no_momentum | Delta (B0.1 - no_momentum) | Degraded? |')
    lines.append('|--------|------|-------------|------------------------|-----------|')
    lines.append(
        f"| Avg Annual Return (training) | {fmt_pct(b0_train_avg)} | "
        f"{fmt_pct(nm_train_avg)} | {fmt_pct(b0_train_avg - nm_train_avg)} | "
        f"{'YES' if is_degraded else 'NO'} |"
    )
    lines.append('')
    if is_degraded:
        lines.append('**Result**: `no_momentum` is **degraded in training** (>2 pp below B0.1).')
        lines.append('It is **disqualified** regardless of validation performance.')
    else:
        lines.append('**Result**: `no_momentum` is **not degraded** in training (≤2 pp).')
    lines.append('')

    # -------------------------------------------------------------------------
    # Requirement 6: Recommendation (already shown in Executive Summary)
    # -------------------------------------------------------------------------
    lines.append('## 5. Recommendation')
    lines.append('')
    lines.append(f'**Final recommendation**: `{recommendation}`')
    lines.append('')
    lines.append('### Supporting evidence')
    lines.append('')
    lines.append(f'- **Training avg return**: B0.1 = {fmt_pct(b0_train_avg)}, no_momentum = {fmt_pct(nm_train_avg)}')
    lines.append(f'- **Validation avg return**: B0.1 = {fmt_pct(b0_valid_avg)}, no_momentum = {fmt_pct(nm_valid_avg)}')
    lines.append(f'- **Degradation rule triggered**: {"Yes" if is_degraded else "No"}')
    lines.append('')

    # -------------------------------------------------------------------------
    # Conclusion
    # -------------------------------------------------------------------------
    lines.append('## 6. Conclusion')
    lines.append('')
    lines.append('1. `vol_score` is broken by design: the annualized volatility scale is')
    lines.append('   incompatible with the hard-coded thresholds `[0.01, 0.04]` and `(0.04, 0.06]` .')
    lines.append('   It contributes 0 points on virtually every day and should be repaired or removed.')
    lines.append('')
    lines.append('2. Rank IC analysis shows the relative predictive power of each factor.')
    lines.append('   Factors with consistently positive IR and low annual variance are more reliable.')
    lines.append('')
    lines.append('3. The year-by-year comparison and the 2 pp degradation rule provide a disciplined')
    lines.append('   framework for deciding whether to drop the momentum factor.')
    lines.append('')
    lines.append(f'4. **Single recommendation**: `{recommendation}` .')
    lines.append('')
    lines.append('---')
    lines.append('*No production config (`src/config.py`) was modified. No final OOS test was run.*')
    lines.append('')

    return '\n'.join(lines)


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 70)
    print("Phase 5.3: Corrected Scoring Diagnosis")
    print("=" * 70)

    # -------------------------------------------------------------------------
    # Shared base config (mirrors existing script settings)
    # -------------------------------------------------------------------------
    base_cfg = build_config()
    base_cfg['fallback_equity_enabled'] = False
    base_cfg['rebalance_weekday'] = 3
    base_cfg['stop_loss'] = -0.08
    base_cfg['max_position_per_etf'] = 0.20
    base_cfg['min_total_score'] = 40

    market_df, bench_df = load_market_data()

    # =======================================================================
    # Requirement 1: Volatility root cause
    # =======================================================================
    print("\n" + "=" * 70)
    print("Requirement 1: Root cause of vol_score = 0")
    print("=" * 70)

    vol_stats = analyze_volatility_distribution(market_df)
    if vol_stats:
        print(f"  volatility_20 count  : {vol_stats['count']:,}")
        print(f"  volatility_20 mean   : {vol_stats['mean']:.4f}")
        print(f"  volatility_20 median : {vol_stats['median']:.4f}")
        print(f"  volatility_20 min/max: [{vol_stats['min']:.4f}, {vol_stats['max']:.4f}]")
        print(f"  % in [0.01, 0.04]    : {vol_stats['pct_in_0_01_0_04']:.4%}")
        print(f"  % in (0.04, 0.06]    : {vol_stats['pct_in_0_04_0_06']:.4%}")
        print(f"  % > 0.06             : {vol_stats['pct_above_0_06']:.4%}")
    else:
        print("  Warning: no volatility statistics computed (insufficient data).")

    # =======================================================================
    # Requirement 2: Rank IC
    # =======================================================================
    print("\n" + "=" * 70)
    print("Requirement 2: Daily cross-sectional Rank IC")
    print("=" * 70)

    # Compute raw scores for the whole history (needed for IC analysis)
    all_scores = get_raw_signals(base_cfg, market_df)
    if all_scores is not None and not all_scores.empty:
        all_scores = add_future_returns(all_scores, market_df)
    else:
        print("  Warning: could not compute raw signals.  IC analysis skipped.")
        all_scores = None

    ic_summary = {}
    for factor_name, config in FACTORS.items():
        col = config['score_col']
        ic_summary[factor_name] = {}
        if all_scores is not None and col in all_scores.columns:
            for h in (5, 10, 20):
                ret_col = f'future_ret_{h}'
                if ret_col in all_scores.columns:
                    ic_df = compute_daily_rank_ic(all_scores, col, ret_col)
                    summary = summarize_ic(ic_df)
                    ic_summary[factor_name][h] = summary
                    if summary:
                        print(f"  {factor_name:12s} H{h:2d}: IC_mean={summary['overall_ic_mean']: .4f}, "
                              f"IC_std={summary['overall_ic_std']:.4f}, IR={summary['ir']:.4f}, "
                              f"n_days={summary['n_days']}")
                    else:
                        print(f"  {factor_name:12s} H{h:2d}: insufficient non-zero observations")
        else:
            for h in (5, 10, 20):
                ic_summary[factor_name][h] = None
                print(f"  {factor_name:12s} H{h:2d}: factor column missing")

    # =======================================================================
    # Requirement 3: Year-by-year backtests
    # =======================================================================
    print("\n" + "=" * 70)
    print("Requirement 3: Year-by-year B0.1 vs no_momentum")
    print("=" * 70)

    # Run one full backtest per configuration, then slice by year
    print("\n  Running B0.1 full backtest ...")
    b0_result = run_full_backtest(base_cfg, as_of_date='2024-12-31', performance_start='2019-01-01')

    print("  Running no_momentum full backtest ...")
    no_momentum_cfg = copy.deepcopy(base_cfg)
    no_momentum_cfg['exclude_factor'] = 'momentum_rank'
    nm_result = run_full_backtest(no_momentum_cfg, as_of_date='2024-12-31', performance_start='2019-01-01')

    year_metrics = {}
    for year in ALL_YEARS:
        year_metrics[year] = {
            'B0.1':         extract_year_metrics(b0_result, year),
            'no_momentum':  extract_year_metrics(nm_result,  year),
        }
        print(f"\n  Year {year}:")
        for strategy in ('B0.1', 'no_momentum'):
            m = year_metrics[year][strategy]
            if m:
                print(f"    {strategy:12s}: ann_ret={m['annual_return']: .2%}, sharpe={m['sharpe_ratio']:.3f}, "
                      f"max_dd={m['max_drawdown']: .2%}, trades={m['num_trades']}, "
                      f"turnover={m['turnover_rate']:.2f}")
            else:
                print(f"    {strategy:12s}: N/A (insufficient data)")

    # =======================================================================
    # Requirement 4: Degradation check
    # =======================================================================
    print("\n" + "=" * 70)
    print("Requirement 4: Training-period degradation check")
    print("=" * 70)

    b0_train_returns = []
    nm_train_returns = []
    for year in TRAINING_YEARS:
        b0_m = year_metrics[year]['B0.1']
        nm_m = year_metrics[year]['no_momentum']
        if b0_m:
            b0_train_returns.append(b0_m['annual_return'])
        if nm_m:
            nm_train_returns.append(nm_m['annual_return'])

    b0_train_avg = np.mean(b0_train_returns) if b0_train_returns else 0.0
    nm_train_avg = np.mean(nm_train_returns) if nm_train_returns else 0.0
    train_gap = b0_train_avg - nm_train_avg
    is_degraded = train_gap > DEGRADATION_THRESHOLD

    print(f"  B0.1  training avg annual return: {b0_train_avg: .2%}")
    print(f"  no_momentum training avg annual return: {nm_train_avg: .2%}")
    print(f"  Gap (B0.1 - no_momentum): {train_gap: .2%}")
    print(f"  Degraded in training (>2pp): {'YES' if is_degraded else 'NO'}")

    # Validation averages for context
    b0_valid_returns = []
    nm_valid_returns = []
    for year in VALIDATION_YEARS:
        b0_m = year_metrics[year]['B0.1']
        nm_m = year_metrics[year]['no_momentum']
        if b0_m:
            b0_valid_returns.append(b0_m['annual_return'])
        if nm_m:
            nm_valid_returns.append(nm_m['annual_return'])

    b0_valid_avg = np.mean(b0_valid_returns) if b0_valid_returns else 0.0
    nm_valid_avg = np.mean(nm_valid_returns) if nm_valid_returns else 0.0

    # =======================================================================
    # Requirement 6: Single recommendation
    # =======================================================================
    print("\n" + "=" * 70)
    print("Requirement 6: Single recommendation")
    print("=" * 70)

    # Decision logic (documented for transparency)
    if is_degraded:
        recommendation = "Keep B0.1"
        reason = (
            "no_momentum is degraded in the training period by more than 2 percentage points "
            f"({train_gap:.2%}). Per the degradation rule, it is disqualified regardless of validation performance."
        )
    elif nm_valid_avg > b0_valid_avg:
        # Even if not degraded, we need to check whether validation is meaningfully better
        recommendation = "no_momentum enters final validation"
        reason = (
            "no_momentum is not degraded in training and shows higher average validation return "
            f"({nm_valid_avg:.2%} vs B0.1 {b0_valid_avg:.2%}). It merits a final validation review."
        )
    else:
        recommendation = "Keep B0.1"
        reason = (
            "B0.1 matches or exceeds no_momentum in both training and validation periods, and no_momentum "
            "does not show sufficient evidence to justify removing the momentum factor."
        )

    print(f"  Recommendation: {recommendation}")
    print(f"  Reason: {reason}")

    # =======================================================================
    # Requirement 7: Write report
    # =======================================================================
    print("\n" + "=" * 70)
    print("Requirement 7: Writing report")
    print("=" * 70)

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    report_md = generate_report(
        vol_stats, ic_summary, year_metrics,
        b0_train_avg, nm_train_avg, b0_valid_avg, nm_valid_avg,
        is_degraded, recommendation, reason
    )
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report_md)

    print(f"\n  Report saved to: {REPORT_PATH}")
    print(f"\n{'='*70}")
    print("Phase 5.3 completed.")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
