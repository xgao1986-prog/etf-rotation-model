# -*- coding: utf-8 -*-
"""B0 Target vs Actual Position Audit v2 - Correct defense fill logic, 16 ETF pool"""
import sys, os, pandas as pd, numpy as np
from datetime import timedelta

sys.path.insert(0, 'D:/etf_rotation_model/src')

import config as _config_module
_config_module.CORE_UNIVERSE = _config_module.ETF_UNIVERSE

from backtest import BacktestEngine
from strategy import StrategyEngine
from database import ETFDatabase
import config

DATA_END = pd.Timestamp('2026-06-05')
WARMUP_END = pd.Timestamp('2019-08-13')

ALL_CORE_TICKERS = list(config.ETF_UNIVERSE.keys())
ALL_DEFENSE_TICKERS = list(config.DEFENSE_UNIVERSE.keys())
DEFENSE_TICKERS_SET = set(ALL_DEFENSE_TICKERS)
CORE_TICKERS_SET = set(ALL_CORE_TICKERS)

ETF_NAME_MAP = {**config.ETF_UNIVERSE, **config.DEFENSE_UNIVERSE}

# Configuration Parameters
MAX_POS_PER_ETF = config.STRATEGY_CONFIG['max_position_per_etf']       # 0.20
STOCK_MAX_HOLDINGS = config.STRATEGY_CONFIG['stock_max_holdings']     # 5
TOTAL_MAX_HOLDINGS = config.STRATEGY_CONFIG['total_max_holdings']     # 5
DEFENSE_MAX_HOLDINGS = config.STRATEGY_CONFIG['defense_max_holdings'] # 2
DEFENSE_FILL_MAX_BULL = config.STRATEGY_CONFIG.get('defense_fill_max_ratio_bull', 0.30)
DEFENSE_FILL_MAX_BEAR = config.STRATEGY_CONFIG.get('defense_fill_max_ratio_bear', 0.50)

print("=" * 80)
print("B0 Target vs Actual Position Audit v2")
print("=" * 80)
print(f"\nFrozen Configuration:")
print(f"  Single core ETF max: {MAX_POS_PER_ETF:.0%}")
print(f"  Industry max holdings: {STOCK_MAX_HOLDINGS}")
print(f"  Total max holdings: {TOTAL_MAX_HOLDINGS}")
print(f"  Defense max holdings: {DEFENSE_MAX_HOLDINGS}")
print(f"  Defense fill max (bull): {DEFENSE_FILL_MAX_BULL:.0%}")
print(f"  Defense fill max (bear): {DEFENSE_FILL_MAX_BEAR:.0%}")
print(f"  Cash: allowed as active allocation when candidates insufficient")
print(f"  515230.SH = {ETF_NAME_MAP.get('515230.SH', 'UNKNOWN')}")
print(f"  Core universe: {len(ALL_CORE_TICKERS)} ETFs (16 industry)")
print(f"  Defense universe: {ALL_DEFENSE_TICKERS}")

# ============================================================
# 1. Load data and run B0
# ============================================================
print("\n[1/6] Running B0 backtest...")

db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=ALL_CORE_TICKERS + ALL_DEFENSE_TICKERS)
bench_df = db.get_market_data(ticker=config.BENCHMARK)
market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])
market_df = market_df[market_df['date'] <= DATA_END].copy()
bench_df = bench_df[bench_df['date'] <= DATA_END].copy()

bt_b0 = BacktestEngine(cfg=config.STRATEGY_CONFIG.copy(), s1_mode=False)
result_b0 = bt_b0.run(market_df, bench_df)
nav_df = result_b0['nav_df'].copy()
nav_df = nav_df[nav_df['date'] <= DATA_END].reset_index(drop=True)
nav_df = nav_df[nav_df['date'] >= WARMUP_END].sort_values('date').reset_index(drop=True)

trades_df = result_b0['trades_df'].copy()
if not trades_df.empty:
    trades_df['date'] = pd.to_datetime(trades_df['date'])
else:
    trades_df = pd.DataFrame(columns=['date', 'ticker', 'action', 'price', 'shares', 'amount', 'commission', 'pnl_pct', 'reason'])

print(f"  B0: {len(nav_df)} days, {len(trades_df)} total trades")

# ============================================================
# 2. Recompute daily signals
# ============================================================
print("\n[2/6] Recomputing daily signals...")

strategy = StrategyEngine(config.STRATEGY_CONFIG.copy(), s1_mode=False)
all_scores = []
for ticker in ALL_CORE_TICKERS + ALL_DEFENSE_TICKERS:
    tdf = market_df[market_df['ticker'] == ticker].copy()
    if len(tdf) < 51:
        continue
    scored = strategy.calculate_total_score(tdf)
    all_scores.append(scored)

scores_df = pd.concat(all_scores, ignore_index=True)
scores_df = strategy.rank_all_momentum(scores_df)
scores_df = strategy.compute_total_score(scores_df)
signals = strategy.generate_signals(scores_df, bench_df)

buy_signals = signals[(signals['signal_type'] == 'BUY') & (signals['momentum_valid'] == True)].copy()

# ============================================================
# 3. Per-day target vs actual calculation
# ============================================================
print("\n[3/6] Calculating target vs actual per day...")

audit_records = []

for _, row in nav_df.iterrows():
    date = row['date']
    nav = row['nav']
    cash = row['cash']
    positions_value = row['positions_value']
    positions_detail = row['positions_detail'] or {}
    max_total_pos = row['max_total_position']
    num_positions = row['num_positions']
    
    is_rebalance = date.weekday() == 3  # Thursday
    
    # Actual allocation
    actual_cash_ratio = cash / nav if nav > 0 else 0
    actual_position_ratio = positions_value / nav if nav > 0 else 0
    
    industry_value = 0
    defense_value = 0
    for ticker, detail in positions_detail.items():
        mv = detail.get('market_value', 0)
        if ticker in DEFENSE_TICKERS_SET:
            defense_value += mv
        elif ticker in CORE_TICKERS_SET:
            industry_value += mv
    
    actual_industry_ratio = industry_value / nav if nav > 0 else 0
    actual_defense_ratio = defense_value / nav if nav > 0 else 0
    
    # Get BUY candidates for this day
    day_buy = buy_signals[buy_signals['date'] == date]
    core_buy = day_buy[day_buy['ticker'].isin(CORE_TICKERS_SET)]
    defense_buy = day_buy[day_buy['ticker'].isin(DEFENSE_TICKERS_SET)]
    
    core_candidates = len(core_buy)
    defense_candidates = len(defense_buy)
    
    # --- TARGET CALCULATION (only meaningful on rebalance days) ---
    if is_rebalance:
        # Industry target: min(core_candidates, 5) * 20% * market_signal
        # But also limited by market_signal overall
        max_industry_slots = min(core_candidates, STOCK_MAX_HOLDINGS)
        industry_target = max_industry_slots * min(MAX_POS_PER_ETF, 1.0 / STOCK_MAX_HOLDINGS) * max_total_pos
        industry_target = min(industry_target, max_total_pos)
        
        # Defense fill target: when industry_target < max_total_pos and defense candidates exist
        # Defense fills up to defense_fill_max (30% in bull, 50% in bear)
        if max_total_pos >= 1.0:
            defense_fill_max = DEFENSE_FILL_MAX_BULL
        else:
            defense_fill_max = DEFENSE_FILL_MAX_BEAR
        
        # Defense fill is only triggered if industry doesn't fill all slots
        if industry_target < max_total_pos and defense_candidates > 0:
            # Defense can fill the gap, up to defense_fill_max
            gap_to_fill = max_total_pos - industry_target
            defense_fill_target = min(gap_to_fill, defense_fill_max)
            # But also limited by defense_max_holdings and available candidates
            max_defense_slots = min(DEFENSE_MAX_HOLDINGS, defense_candidates)
            # Each defense ETF: no single max, just fill the gap
            # But practically, each defense ETF gets gap / slots
            defense_fill_target = min(defense_fill_target, max_defense_slots * MAX_POS_PER_ETF)
            defense_fill_target = min(defense_fill_target, defense_fill_max)
        else:
            defense_fill_target = 0.0
        
        # Total target = industry + defense fill
        total_target = min(industry_target + defense_fill_target, max_total_pos)
        
        # Cash components
        strategy_design_cash = 1.0 - max_total_pos  # Cash from market timing limit
        candidate_shortfall = max_total_pos - industry_target  # Gap from insufficient core candidates
        defense_fill_gap = max(0, defense_fill_target - actual_defense_ratio)  # Defense not filled
        
        # Unexplained gap = actual cash - expected cash
        # Expected cash = 1.0 - total_target
        expected_cash = 1.0 - total_target
        unexplained_gap = actual_cash_ratio - expected_cash
        
    else:
        # Non-rebalance day: target = previous day's target (drift only)
        industry_target = None
        defense_fill_target = None
        total_target = None
        strategy_design_cash = None
        candidate_shortfall = None
        defense_fill_gap = None
        unexplained_gap = None
    
    audit_records.append({
        'date': date, 'date_str': date.strftime('%Y-%m-%d'),
        'is_rebalance': is_rebalance, 'nav': nav, 'cash': cash,
        'actual_cash_ratio': actual_cash_ratio,
        'actual_industry_ratio': actual_industry_ratio,
        'actual_defense_ratio': actual_defense_ratio,
        'actual_position_ratio': actual_position_ratio,
        'market_signal': max_total_pos,
        'core_candidates': core_candidates,
        'defense_candidates': defense_candidates,
        'industry_target': industry_target,
        'defense_fill_target': defense_fill_target,
        'total_target': total_target,
        'strategy_design_cash': strategy_design_cash,
        'candidate_shortfall': candidate_shortfall,
        'defense_fill_gap': defense_fill_gap,
        'unexplained_gap': unexplained_gap,
    })

df_audit = pd.DataFrame(audit_records)

# Save CSV
os.makedirs('reports', exist_ok=True)
csv_path = 'reports/b0_target_actual_audit_v2.csv'
df_audit.to_csv(csv_path, index=False, encoding='utf-8-sig')
print(f"  Saved: {csv_path}")

# ============================================================
# 4. Rebalance day summary
# ============================================================
print("\n" + "=" * 80)
print("[4/6] Rebalance Day Target vs Actual Summary")
print("=" * 80)

df_rebal = df_audit[df_audit['is_rebalance'] == True].copy()
print(f"\nTotal rebalance days: {len(df_rebal)}")

print(f"\nAverage actual allocation:")
print(f"  Industry: {df_rebal['actual_industry_ratio'].mean():.2%}")
print(f"  Defense:  {df_rebal['actual_defense_ratio'].mean():.2%}")
print(f"  Cash:     {df_rebal['actual_cash_ratio'].mean():.2%}")
print(f"  Total position: {df_rebal['actual_position_ratio'].mean():.2%}")

print(f"\nAverage target allocation:")
print(f"  Industry target: {df_rebal['industry_target'].mean():.2%}")
print(f"  Defense fill target: {df_rebal['defense_fill_target'].mean():.2%}")
print(f"  Total target: {df_rebal['total_target'].mean():.2%}")

print(f"\nAverage gaps:")
print(f"  Strategy design cash: {df_rebal['strategy_design_cash'].mean():.2%}")
print(f"  Candidate shortfall: {df_rebal['candidate_shortfall'].mean():.2%}")
print(f"  Defense fill gap: {df_rebal['defense_fill_gap'].mean():.2%}")
print(f"  Unexplained gap: {df_rebal['unexplained_gap'].mean():.2%}")

# ============================================================
# 5. High cash interval decomposition
# ============================================================
print("\n" + "=" * 80)
print("[5/6] High Cash Interval Decomposition (2022-2024)")
print("=" * 80)

# Identify high cash intervals (cash >= 50%)
df_audit['high_cash'] = df_audit['actual_cash_ratio'] >= 0.50

intervals = []
if df_audit['high_cash'].any():
    in_interval = False
    start_date = None
    for _, row in df_audit.iterrows():
        if row['high_cash'] and not in_interval:
            in_interval = True
            start_date = row['date']
        elif not row['high_cash'] and in_interval:
            in_interval = False
            end_date = df_audit[df_audit['date'] < row['date']]['date'].max()
            intervals.append((start_date, end_date))
    if in_interval:
        intervals.append((start_date, df_audit['date'].max()))

# Only show major intervals (>= 5 days in 2022-2024)
major_intervals = []
for start, end in intervals:
    if start.year >= 2022 and start.year <= 2024 or end.year >= 2022 and end.year <= 2024:
        length = (end - start).days + 1
        if length >= 5:
            major_intervals.append((start, end, length))

print(f"\nMajor high-cash intervals (cash >= 50%, 2022-2024, >= 5 days):")
for start, end, length in major_intervals:
    mask = (df_audit['date'] >= start) & (df_audit['date'] <= end)
    sub = df_audit[mask]
    
    avg_cash = sub['actual_cash_ratio'].mean()
    avg_industry = sub['actual_industry_ratio'].mean()
    avg_defense = sub['actual_defense_ratio'].mean()
    
    # Rebalance days in this interval
    rebal_days = sub[sub['is_rebalance'] == True]
    if len(rebal_days) > 0:
        avg_target_cash = (1.0 - rebal_days['total_target']).mean()
        avg_candidate_short = rebal_days['candidate_shortfall'].mean()
        avg_defense_gap = rebal_days['defense_fill_gap'].mean()
        avg_unexplained = rebal_days['unexplained_gap'].mean()
        avg_core_cand = rebal_days['core_candidates'].mean()
    else:
        avg_target_cash = avg_candidate_short = avg_defense_gap = avg_unexplained = avg_core_cand = 0
    
    print(f"\n  {start.date()} ~ {end.date()} ({length} days)")
    print(f"    Actual:   cash={avg_cash:.1%}, industry={avg_industry:.1%}, defense={avg_defense:.1%}")
    print(f"    Target:   cash={avg_target_cash:.1%}")
    print(f"    Decomposition:")
    print(f"      Strategy design cash: {1.0 - rebal_days['market_signal'].mean() if len(rebal_days) > 0 else 0:.1%}")
    print(f"      Candidate shortfall:  {avg_candidate_short:.1%}")
    print(f"      Defense fill gap:     {avg_defense_gap:.1%}")
    print(f"      Unexplained gap:      {avg_unexplained:.1%}")
    print(f"    Avg core candidates: {avg_core_cand:.1f}")

# ============================================================
# 6. Rebalance days with significant unexplained gap
# ============================================================
print("\n" + "=" * 80)
print("[6/6] Rebalance Days with Significant Unexplained Gap")
print("=" * 80)

# Filter: |unexplained_gap| > 2% (meaningful)
significant_gap = df_rebal[df_rebal['unexplained_gap'].abs() > 0.02].sort_values('unexplained_gap')

print(f"\n{len(significant_gap)} rebalance days with |unexplained gap| > 2%")

# Top 10 positive gaps (actual cash > target cash, under-invested)
print("\nTop 10: Under-invested (actual cash > target):")
top_under = df_rebal.nlargest(10, 'unexplained_gap')
for _, row in top_under.iterrows():
    print(f"  {row['date_str']}: cash={row['actual_cash_ratio']:.1%}, target_cash={1.0 - row['total_target']:.1%}, gap=+{row['unexplained_gap']:.1%}")
    print(f"    core={row['core_candidates']}, defense={row['defense_candidates']}")
    print(f"    actual: industry={row['actual_industry_ratio']:.1%}, defense={row['actual_defense_ratio']:.1%}")
    print(f"    target: industry={row['industry_target']:.1%}, defense_fill={row['defense_fill_target']:.1%}")

# Top 10 negative gaps (actual cash < target cash, over-invested)
print("\nTop 10: Over-invested (actual cash < target):")
top_over = df_rebal.nsmallest(10, 'unexplained_gap')
for _, row in top_over.iterrows():
    print(f"  {row['date_str']}: cash={row['actual_cash_ratio']:.1%}, target_cash={1.0 - row['total_target']:.1%}, gap={row['unexplained_gap']:.1%}")
    print(f"    core={row['core_candidates']}, defense={row['defense_candidates']}")
    print(f"    actual: industry={row['actual_industry_ratio']:.1%}, defense={row['actual_defense_ratio']:.1%}")
    print(f"    target: industry={row['industry_target']:.1%}, defense_fill={row['defense_fill_target']:.1%}")

# ============================================================
# 7. Consistency check
# ============================================================
print("\n" + "=" * 80)
print("Consistency Check: Target vs Actual on Rebalance Days")
print("=" * 80)

df_rebal['target_vs_actual'] = df_rebal['actual_position_ratio'] - df_rebal['total_target']
max_diff = df_rebal['target_vs_actual'].abs().max()
print(f"\nMax |actual - target| on rebalance days: {max_diff:.2%}")

if max_diff < 0.02:
    print("PASS: All rebalance days within 2% tolerance")
else:
    n_fail = len(df_rebal[df_rebal['target_vs_actual'].abs() > 0.02])
    print(f"FAIL: {n_fail} days exceed 2% tolerance")
    
    # Show breakdown
    over_target = df_rebal[df_rebal['target_vs_actual'] > 0.02]
    under_target = df_rebal[df_rebal['target_vs_actual'] < -0.02]
    print(f"  Over-target (actual > target): {len(over_target)} days")
    print(f"  Under-target (actual < target): {len(under_target)} days")

print("\n" + "=" * 80)
print("B0 Target vs Actual Position Audit v2 Complete")
print("=" * 80)
