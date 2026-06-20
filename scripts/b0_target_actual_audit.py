# -*- coding: utf-8 -*-
"""B0 Target vs Actual Position Audit - 16 ETF pool, no strategy changes"""
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

# ============================================================
# Configuration Parameters (B0 frozen baseline)
# ============================================================
MAX_POS_PER_ETF = config.STRATEGY_CONFIG['max_position_per_etf']       # 0.20
STOCK_MAX_HOLDINGS = config.STRATEGY_CONFIG['stock_max_holdings']     # 5
TOTAL_MAX_HOLDINGS = config.STRATEGY_CONFIG['total_max_holdings']     # 5
DEFENSE_MAX_HOLDINGS = config.STRATEGY_CONFIG['defense_max_holdings'] # 2
DEFENSE_ALLOCATION = config.DEFENSE_ALLOCATION
DEFENSE_ALLOCATION_MODE = config.DEFENSE_ALLOCATION_MODE

print("=" * 80)
print("B0 Target vs Actual Position Audit")
print("=" * 80)
print(f"\nFrozen Configuration:")
print(f"  Single core ETF max: {MAX_POS_PER_ETF:.0%}")
print(f"  Industry max holdings: {STOCK_MAX_HOLDINGS}")
print(f"  Total max holdings: {TOTAL_MAX_HOLDINGS}")
print(f"  Defense max holdings: {DEFENSE_MAX_HOLDINGS}")
print(f"  Defense allocation mode: {DEFENSE_ALLOCATION_MODE}")
print(f"  Defense allocation map: {DEFENSE_ALLOCATION}")
print(f"  Cash: allowed as active allocation when candidates insufficient")
print(f"  515230.SH = {ETF_NAME_MAP.get('515230.SH', 'UNKNOWN')}")

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
# 3. Per-rebalance-day target vs actual calculation
# ============================================================
print("\n[3/6] Calculating target vs actual per rebalance day...")

rebalance_records = []

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
    
    # --- Target calculation (only on rebalance days) ---
    if is_rebalance:
        # Get BUY candidates for this day
        day_buy = buy_signals[buy_signals['date'] == date]
        core_buy = day_buy[day_buy['ticker'].isin(CORE_TICKERS_SET)]
        defense_buy = day_buy[day_buy['ticker'].isin(DEFENSE_TICKERS_SET)]
        
        core_candidates = len(core_buy)
        defense_candidates = len(defense_buy)
        
        # Target: industry can form at most min(core_candidates, STOCK_MAX_HOLDINGS) slots
        # Each slot = min(MAX_POS_PER_ETF, 1.0 / STOCK_MAX_HOLDINGS) = min(0.20, 0.20) = 0.20
        # So max_industry_target = min(core_candidates, STOCK_MAX_HOLDINGS) * 0.20
        max_industry_slots = min(core_candidates, STOCK_MAX_HOLDINGS)
        max_industry_target = max_industry_slots * min(MAX_POS_PER_ETF, 1.0 / STOCK_MAX_HOLDINGS)
        
        # But also limited by total_max_holdings and market_signal
        max_industry_target = min(max_industry_target, TOTAL_MAX_HOLDINGS * min(MAX_POS_PER_ETF, 1.0 / STOCK_MAX_HOLDINGS))
        max_industry_target = min(max_industry_target, max_total_pos)
        
        # Defense target: based on DEFENSE_ALLOCATION and market_signal
        if DEFENSE_ALLOCATION_MODE == 'linear':
            sorted_signals = sorted(DEFENSE_ALLOCATION.keys())
            _defense_alloc = 0.0
            for i in range(len(sorted_signals) - 1):
                s_low, s_high = sorted_signals[i], sorted_signals[i + 1]
                if s_low <= max_total_pos <= s_high:
                    a_low = DEFENSE_ALLOCATION[s_low]
                    a_high = DEFENSE_ALLOCATION[s_high]
                    if s_high == s_low:
                        _defense_alloc = a_low
                    else:
                        _defense_alloc = a_low + (a_high - a_low) * (max_total_pos - s_low) / (s_high - s_low)
                    break
            else:
                if max_total_pos < sorted_signals[0]:
                    _defense_alloc = DEFENSE_ALLOCATION[sorted_signals[0]]
                else:
                    _defense_alloc = DEFENSE_ALLOCATION[sorted_signals[-1]]
        else:
            _defense_alloc = DEFENSE_ALLOCATION.get(max_total_pos, 0.0)
        
        defense_target = max_total_pos * _defense_alloc
        
        # Defense can also fill gaps when industry is insufficient
        # But defense_target is already the "mandatory" allocation based on market_signal
        # In practice, defense fills up to defense_target, but if industry is < max_total_pos - defense_target,
        # defense can fill more up to DEFENSE_FILL_MAX limits
        # For simplicity, we use the _defense_alloc as the primary target
        
        # Total target = min(max_industry_target + defense_target, max_total_pos)
        # But defense_target already accounts for market_signal, so:
        total_target = min(max_industry_target + defense_target, max_total_pos)
        
        # Theoretical target cash = 1.0 - total_target
        # But if market_signal < 1.0, the "strategy-design" cash = 1.0 - max_total_pos
        target_cash = 1.0 - total_target
        strategy_design_cash = 1.0 - max_total_pos
        
        # Cash from insufficient candidates
        if max_industry_target < max_total_pos - defense_target:
            candidate_shortfall = (max_total_pos - defense_target) - max_industry_target
        else:
            candidate_shortfall = 0.0
        
        # Unexplained cash gap = actual_cash - target_cash
        # But we need to be careful: if actual is close to target, that's good
        unexplained_gap = actual_cash_ratio - target_cash
        
        # Defense fill gap (defense not filled)
        defense_fill_gap = max(0, defense_target - actual_defense_ratio)
        
        rebalance_records.append({
            'date': date, 'date_str': date.strftime('%Y-%m-%d'),
            'is_rebalance': True, 'nav': nav, 'cash': cash,
            'actual_cash_ratio': actual_cash_ratio,
            'actual_industry_ratio': actual_industry_ratio,
            'actual_defense_ratio': actual_defense_ratio,
            'actual_position_ratio': actual_position_ratio,
            'market_signal': max_total_pos,
            'core_candidates': core_candidates,
            'defense_candidates': defense_candidates,
            'max_industry_target': max_industry_target,
            'defense_target': defense_target,
            'total_target': total_target,
            'target_cash': target_cash,
            'strategy_design_cash': strategy_design_cash,
            'candidate_shortfall': candidate_shortfall,
            'defense_fill_gap': defense_fill_gap,
            'unexplained_gap': unexplained_gap,
        })
    else:
        # Non-rebalance day: just record drift
        rebalance_records.append({
            'date': date, 'date_str': date.strftime('%Y-%m-%d'),
            'is_rebalance': False, 'nav': nav, 'cash': cash,
            'actual_cash_ratio': actual_cash_ratio,
            'actual_industry_ratio': actual_industry_ratio,
            'actual_defense_ratio': actual_defense_ratio,
            'actual_position_ratio': actual_position_ratio,
            'market_signal': max_total_pos,
            'core_candidates': None, 'defense_candidates': None,
            'max_industry_target': None, 'defense_target': None,
            'total_target': None, 'target_cash': None,
            'strategy_design_cash': None, 'candidate_shortfall': None,
            'defense_fill_gap': None, 'unexplained_gap': None,
        })

df_rebal = pd.DataFrame(rebalance_records)

# ============================================================
# 4. Rebalance day summary
# ============================================================
print("\n" + "=" * 80)
print("[4/6] Rebalance Day Target vs Actual Summary")
print("=" * 80)

df_rebal_days = df_rebal[df_rebal['is_rebalance'] == True].copy()
print(f"\nTotal rebalance days: {len(df_rebal_days)}")

# Summary stats
print(f"\nAverage actual allocation:")
print(f"  Industry: {df_rebal_days['actual_industry_ratio'].mean():.2%}")
print(f"  Defense:  {df_rebal_days['actual_defense_ratio'].mean():.2%}")
print(f"  Cash:     {df_rebal_days['actual_cash_ratio'].mean():.2%}")
print(f"  Total position: {df_rebal_days['actual_position_ratio'].mean():.2%}")

print(f"\nAverage target allocation:")
print(f"  Max industry target: {df_rebal_days['max_industry_target'].mean():.2%}")
print(f"  Defense target:      {df_rebal_days['defense_target'].mean():.2%}")
print(f"  Total target:        {df_rebal_days['total_target'].mean():.2%}")
print(f"  Target cash:         {df_rebal_days['target_cash'].mean():.2%}")

print(f"\nAverage gaps:")
print(f"  Candidate shortfall: {df_rebal_days['candidate_shortfall'].mean():.2%}")
print(f"  Defense fill gap:    {df_rebal_days['defense_fill_gap'].mean():.2%}")
print(f"  Unexplained gap:     {df_rebal_days['unexplained_gap'].mean():.2%}")

# ============================================================
# 5. High cash interval decomposition
# ============================================================
print("\n" + "=" * 80)
print("[5/6] High Cash Interval Decomposition")
print("=" * 80)

# Identify high cash intervals (cash >= 50%)
df_rebal['high_cash'] = df_rebal['actual_cash_ratio'] >= 0.50

# Find continuous high cash intervals
intervals = []
if df_rebal['high_cash'].any():
    in_interval = False
    start_date = None
    for _, row in df_rebal.iterrows():
        if row['high_cash'] and not in_interval:
            in_interval = True
            start_date = row['date']
        elif not row['high_cash'] and in_interval:
            in_interval = False
            # Find the end date
            end_date = df_rebal[df_rebal['date'] < row['date']]['date'].max()
            intervals.append((start_date, end_date))
    if in_interval:
        intervals.append((start_date, df_rebal['date'].max()))

# Only show intervals within 2022-2024 (major cash periods)
major_intervals = [(s, e) for s, e in intervals if s.year >= 2022 and s.year <= 2024 or e.year >= 2022 and e.year <= 2024]

print(f"\nMajor high-cash intervals (2022-2024, cash >= 50%):")
for start, end in major_intervals:
    mask = (df_rebal['date'] >= start) & (df_rebal['date'] <= end)
    sub = df_rebal[mask]
    
    avg_cash = sub['actual_cash_ratio'].mean()
    avg_strategy_cash = sub['strategy_design_cash'].mean() if sub['strategy_design_cash'].notna().any() else 0
    avg_candidate_short = sub['candidate_shortfall'].mean() if sub['candidate_shortfall'].notna().any() else 0
    avg_defense_gap = sub['defense_fill_gap'].mean() if sub['defense_fill_gap'].notna().any() else 0
    
    # Rebalance days in this interval
    rebal_days = sub[sub['is_rebalance'] == True]
    if len(rebal_days) > 0:
        avg_unexplained = rebal_days['unexplained_gap'].mean()
        avg_core_cand = rebal_days['core_candidates'].mean()
    else:
        avg_unexplained = 0
        avg_core_cand = 0
    
    print(f"\n  {start.date()} ~ {end.date()} ({len(sub)} days)")
    print(f"    Avg actual cash:     {avg_cash:.1%}")
    print(f"    Strategy design cash: {avg_strategy_cash:.1%}")
    print(f"    Candidate shortfall:  {avg_candidate_short:.1%}")
    print(f"    Defense fill gap:     {avg_defense_gap:.1%}")
    print(f"    Unexplained gap:      {avg_unexplained:.1%}")
    print(f"    Avg core candidates:  {avg_core_cand:.1f}")

# ============================================================
# 6. Rebalance days with significant unexplained gap
# ============================================================
print("\n" + "=" * 80)
print("[6/6] Rebalance Days with Significant Unexplained Gap")
print("=" * 80)

# Filter: |unexplained_gap| > 2% (meaningful)
significant_gap = df_rebal_days[df_rebal_days['unexplained_gap'].abs() > 0.02].sort_values('unexplained_gap')

print(f"\n{len(significant_gap)} rebalance days with |unexplained gap| > 2%")

# Show top 10 positive gaps (actual cash > target cash, meaning under-invested)
print("\nTop 10: Actual cash > target (under-invested):")
top_under = df_rebal_days.nlargest(10, 'unexplained_gap')
if len(top_under) > 0:
    for _, row in top_under.iterrows():
        print(f"  {row['date_str']}: cash={row['actual_cash_ratio']:.1%}, target={row['target_cash']:.1%}, gap=+{row['unexplained_gap']:.1%}")
        print(f"    core_cand={row['core_candidates']}, defense_cand={row['defense_candidates']}")
        print(f"    industry={row['actual_industry_ratio']:.1%}, defense={row['actual_defense_ratio']:.1%}")
        print(f"    target_industry={row['max_industry_target']:.1%}, target_defense={row['defense_target']:.1%}")

# Show top 10 negative gaps (actual cash < target cash, meaning over-invested)
print("\nTop 10: Actual cash < target (over-invested):")
top_over = df_rebal_days.nsmallest(10, 'unexplained_gap')
if len(top_over) > 0:
    for _, row in top_over.iterrows():
        print(f"  {row['date_str']}: cash={row['actual_cash_ratio']:.1%}, target={row['target_cash']:.1%}, gap={row['unexplained_gap']:.1%}")
        print(f"    core_cand={row['core_candidates']}, defense_cand={row['defense_candidates']}")
        print(f"    industry={row['actual_industry_ratio']:.1%}, defense={row['actual_defense_ratio']:.1%}")
        print(f"    target_industry={row['max_industry_target']:.1%}, target_defense={row['defense_target']:.1%}")

# ============================================================
# 7. Consistency check
# ============================================================
print("\n" + "=" * 80)
print("Consistency Check: Target vs Actual")
print("=" * 80)

# For each rebalance day, check if actual = target within tolerance
df_rebal_days['target_vs_actual'] = df_rebal_days['actual_position_ratio'] - df_rebal_days['total_target']
max_diff = df_rebal_days['target_vs_actual'].abs().max()
print(f"\nMax |actual - target| on rebalance days: {max_diff:.2%}")
 
if max_diff < 0.02:
    print("PASS: All rebalance days within 2% tolerance")
else:
    print(f"FAIL: {len(df_rebal_days[df_rebal_days['target_vs_actual'].abs() > 0.02])} days exceed 2%")

# Save CSV
os.makedirs('reports', exist_ok=True)
csv_path = 'reports/b0_target_actual_audit.csv'
df_rebal.to_csv(csv_path, index=False, encoding='utf-8-sig')
print(f"\nSaved: {csv_path}")

print("\n" + "=" * 80)
print("B0 Target vs Actual Position Audit Complete")
print("=" * 80)
