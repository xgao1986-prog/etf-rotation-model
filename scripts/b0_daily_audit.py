# -*- coding: utf-8 -*-
"""B0 Daily Position and Cash Audit - without modifying strategy"""
import sys, os, pandas as pd, numpy as np
from datetime import timedelta

sys.path.insert(0, 'D:/etf_rotation_model/src')

# Freeze config to 16 ETFs
import config as _config_module
_config_module.CORE_UNIVERSE = _config_module.ETF_UNIVERSE

from backtest import BacktestEngine
from strategy import StrategyEngine
from database import ETFDatabase
import config

# Unified dates
DATA_END = pd.Timestamp('2026-06-05')
WARMUP_END = pd.Timestamp('2019-08-13')

ALL_CORE_TICKERS = list(config.ETF_UNIVERSE.keys())
ALL_DEFENSE_TICKERS = list(config.DEFENSE_UNIVERSE.keys())
DEFENSE_TICKERS_SET = set(ALL_DEFENSE_TICKERS)
CORE_TICKERS_SET = set(ALL_CORE_TICKERS)

print("=" * 60)
print("B0 Daily Position Audit - 16 ETF Pool")
print("=" * 60)

# ============================================================
# 1. Load data and run B0 backtest
# ============================================================
print("\n[1/5] Loading data and running B0 backtest...")

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

# Filter to post-warmup
nav_df = nav_df[nav_df['date'] >= WARMUP_END].sort_values('date').reset_index(drop=True)

print(f"  B0 backtest completed: {len(nav_df)} days")
print(f"  Date range: {nav_df['date'].iloc[0].date()} ~ {nav_df['date'].iloc[-1].date()}")

# ============================================================
# 2. Recompute daily signals for audit (mature count, candidates)
# ============================================================
print("\n[2/5] Recomputing daily signals for audit...")

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

# Extract BUY signals
buy_signals = signals[
   (signals['signal_type'] == 'BUY') &
   (signals['momentum_valid'] == True)
].copy()

# Mature signals: history_count >= 51 and momentum_valid
mature_signals = signals[
   (signals['history_count'] >= 51) &
   (signals['momentum_valid'] == True)
].copy()

print(f"  Total signals: {len(signals)}")
print(f"  BUY signals: {len(buy_signals)}")
print(f"  Mature signals: {len(mature_signals)}")

# ============================================================
# 3. Build daily audit records
# ============================================================
print("\n[3/5] Building daily audit records...")

audit_records = []

for _, row in nav_df.iterrows():
   date = row['date']
   date_str = date.strftime('%Y-%m-%d')
   
   # Basic info from nav_df
   nav = row['nav']
   cash = row['cash']
   positions_value = row['positions_value']
   num_positions = row['num_positions']
   max_total_pos = row['max_total_position']
   positions_detail = row['positions_detail'] or {}
   positions_pct = row['positions_pct'] or {}
   
   # Cash ratio
   cash_ratio = cash / nav if nav > 0 else 0
   
   # Total position ratio
   total_position_ratio = positions_value / nav if nav > 0 else 0
   
   # Separate industry and defense
   industry_value = 0
   defense_value = 0
   industry_positions = {}
   defense_positions = {}
   
   for ticker, detail in positions_detail.items():
       mv = detail.get('market_value', 0)
       if ticker in DEFENSE_TICKERS_SET:
           defense_value += mv
           defense_positions[ticker] = detail
       elif ticker in CORE_TICKERS_SET:
           industry_value += mv
           industry_positions[ticker] = detail
   
   industry_ratio = industry_value / nav if nav > 0 else 0
   defense_ratio = defense_value / nav if nav > 0 else 0
   
   # Signal counts for the day
   day_buy = buy_signals[buy_signals['date'] == date]
   day_mature = mature_signals[mature_signals['date'] == date]
   
   buy_candidates = len(day_buy)
   mature_count = len(day_mature)
   
   # Core vs defense candidates
   core_buy = day_buy[day_buy['ticker'].isin(CORE_TICKERS_SET)]
   defense_buy = day_buy[day_buy['ticker'].isin(DEFENSE_TICKERS_SET)]
   core_candidates = len(core_buy)
   defense_candidates = len(defense_buy)
   
   # Market signal
   market_signal = max_total_pos
   
   # Target position = allowed max
   target_position = market_signal
   
   # Gap to target
   gap = target_position - total_position_ratio
   
   # Is rebalance day? (Thursday after warmup)
   is_rebalance = date.weekday() == 3
   
   # Determine under-investment reason
   # We can only infer from available data since we don't modify backtest.py
   under_invest_reasons = []
   
   if total_position_ratio < target_position - 0.01:  # Significant gap
       if buy_candidates == 0:
           under_invest_reasons.append("no_candidates")
       elif core_candidates < 5:
           under_invest_reasons.append("few_candidates")
       
       if market_signal < 1.0:
           under_invest_reasons.append("market_timing")
       
       # Check if defense is configured but not enough core bought
       defense_alloc = config.DEFENSE_ALLOCATION.get(market_signal, 0.0)
       if defense_alloc > 0 and defense_value < nav * defense_alloc * 0.5:
           under_invest_reasons.append("defense_not_filled")
       
       # If cash is high but candidates exist, might be execution issues
       if cash_ratio > 0.3 and core_candidates >= 5:
           under_invest_reasons.append("execution_gap")
   
   # Individual ETF details
   etf_details = []
   for ticker in sorted(positions_detail.keys()):
       detail = positions_detail[ticker]
       shares = detail['shares']
       cost = detail['cost']
       mv = detail.get('market_value', 0)
       close_price = mv / shares if shares > 0 else 0
       pct = mv / nav if nav > 0 else 0
       etf_type = 'defense' if ticker in DEFENSE_TICKERS_SET else 'core'
       etf_details.append({
           'ticker': ticker,
           'type': etf_type,
           'shares': shares,
           'close_price': close_price,
           'market_value': mv,
           'nav_pct': pct,
       })
   
   audit_records.append({
       'date': date,
       'date_str': date_str,
       'nav': nav,
       'cash': cash,
       'cash_ratio': cash_ratio,
       'industry_value': industry_value,
       'industry_ratio': industry_ratio,
       'defense_value': defense_value,
       'defense_ratio': defense_ratio,
       'positions_value': positions_value,
       'total_position_ratio': total_position_ratio,
       'num_positions': num_positions,
       'industry_count': len(industry_positions),
       'defense_count': len(defense_positions),
       'mature_count': mature_count,
       'buy_candidates': buy_candidates,
       'core_candidates': core_candidates,
       'defense_candidates': defense_candidates,
       'market_signal': market_signal,
       'max_total_position': max_total_pos,
       'target_position': target_position,
       'gap': gap,
       'is_rebalance': is_rebalance,
       'under_invest_reasons': under_invest_reasons,
       'etf_details': etf_details,
       'positions_detail': positions_detail,
   })

df_audit = pd.DataFrame(audit_records)
print(f"  Audit records: {len(df_audit)}")

# ============================================================
# 4. Identity and constraint validation
# ============================================================
print("\n" + "=" * 60)
print("[4/5] Identity and Constraint Validation")
print("=" * 60)

# Check 1: Cash + positions_value = NAV
df_audit['computed_total'] = df_audit['cash'] + df_audit['positions_value']
df_audit['nav_diff'] = abs(df_audit['computed_total'] - df_audit['nav'])
max_nav_diff = df_audit['nav_diff'].max()
print(f"\nCheck 1: Cash + Positions = NAV")
print(f"  Max absolute difference: {max_nav_diff:.2f}")
if max_nav_diff < 1.0:
    print("  PASS (diff < 1.0)")
else:
    print(f"  FAIL (diff >= 1.0)")
    # Show worst days
    worst = df_audit.nlargest(5, 'nav_diff')[['date_str', 'nav', 'cash', 'positions_value', 'computed_total', 'nav_diff']]
    print("  Worst days:")
    print(worst.to_string(index=False))

# Check 2: Sum of ratios + cash_ratio = 100%
df_audit['sum_ratios'] = df_audit['cash_ratio'] + df_audit['industry_ratio'] + df_audit['defense_ratio']
max_ratio_diff = (df_audit['sum_ratios'] - 1.0).abs().max()
print(f"\nCheck 2: Cash + Industry + Defense ratios = 100%")
print(f"  Max absolute difference from 1.0: {max_ratio_diff:.6f}")
if max_ratio_diff < 0.001:
    print("  PASS (diff < 0.1%)")
else:
    print(f"  FAIL (diff >= 0.1%)")

# Check 3: Industry holdings <= 5
max_industry = df_audit['industry_count'].max()
print(f"\nCheck 3: Industry holdings <= 5")
print(f"  Max industry holdings: {max_industry}")
if max_industry <= 5:
    print("  PASS")
else:
    print(f"  FAIL (max = {max_industry} > 5)")

# Check 4: Total position <= market_signal allowed
df_audit['position_vs_allowed'] = df_audit['total_position_ratio'] - df_audit['market_signal']
max_excess = df_audit['position_vs_allowed'].max()
print(f"\nCheck 4: Total position <= market_signal allowed")
print(f"  Max excess: {max_excess:.4f}")
if max_excess < 0.01:
    print("  PASS (excess < 1%)")
else:
    print(f"  FAIL (excess >= 1%)")
    worst = df_audit.nlargest(5, 'position_vs_allowed')[['date_str', 'total_position_ratio', 'market_signal', 'position_vs_allowed']]
    print("  Worst days:")
    print(worst.to_string(index=False))

# Check 5: Single core ETF <= max_position_per_etf (0.15 or 0.075)
max_single_core = 0
max_single_core_date = None
max_single_core_ticker = None
for _, row in df_audit.iterrows():
   for etf in row['etf_details']:
       if etf['type'] == 'core' and etf['nav_pct'] > max_single_core:
           max_single_core = etf['nav_pct']
           max_single_core_date = row['date_str']
           max_single_core_ticker = etf['ticker']

print(f"\nCheck 5: Single core ETF <= max_position_per_etf")
print(f"  Max single core position: {max_single_core:.2%} ({max_single_core_ticker} on {max_single_core_date})")
max_allowed = config.STRATEGY_CONFIG['max_position_per_etf']
if max_single_core <= max_allowed + 0.001:
   print(f"  PASS (<= {max_allowed:.2%})")
else:
   print(f"  FAIL (> {max_allowed:.2%})")

# Check 6: Non-rebalance days with >20% position change must be explained by price
print(f"\nCheck 6: Non-rebalance days with >20% position change explained by price")
# This is complex - position changes on non-rebalance days are due to price changes
# We can check if the change in positions_value matches price changes
# Skip for now as it requires detailed price tracking
print("  Skipped (requires detailed price tracking)")

# ============================================================
# 5. High cash day classification
# ============================================================
print("\n" + "=" * 60)
print("[5/5] High Cash Day Classification")
print("=" * 60)

# Cash >= 50%
high_cash_50 = df_audit[df_audit['cash_ratio'] >= 0.50]
print(f"\nCash >= 50%: {len(high_cash_50)} days ({len(high_cash_50)/len(df_audit)*100:.1f}%)")

# Find continuous intervals
intervals_50 = []
if len(high_cash_50) > 0:
   start = high_cash_50['date'].iloc[0]
   prev = start
   for i in range(1, len(high_cash_50)):
       curr = high_cash_50['date'].iloc[i]
       if (curr - prev).days > 1:
           intervals_50.append((start, prev, (prev - start).days + 1))
           start = curr
       prev = curr
   intervals_50.append((start, prev, (prev - start).days + 1))

print("  Continuous intervals (cash >= 50%):")
for start, end, length in intervals_50:
   print(f"    {start.date()} ~ {end.date()} ({length} days)")

# Cash >= 80%
high_cash_80 = df_audit[df_audit['cash_ratio'] >= 0.80]
print(f"\nCash >= 80%: {len(high_cash_80)} days ({len(high_cash_80)/len(df_audit)*100:.1f}%)")

intervals_80 = []
if len(high_cash_80) > 0:
   start = high_cash_80['date'].iloc[0]
   prev = start
   for i in range(1, len(high_cash_80)):
       curr = high_cash_80['date'].iloc[i]
       if (curr - prev).days > 1:
           intervals_80.append((start, prev, (prev - start).days + 1))
           start = curr
       prev = curr
   intervals_80.append((start, prev, (prev - start).days + 1))

print("  Continuous intervals (cash >= 80%):")
for start, end, length in intervals_80:
   print(f"    {start.date()} ~ {end.date()} ({length} days)")

# Classify high cash days
print("\nHigh cash day classification (cash >= 50%):")
classification = {
   'no_candidates': 0,
   'few_candidates': 0,
   'market_timing': 0,
   'defense_not_filled': 0,
   'execution_gap': 0,
   'other': 0,
}

for _, row in high_cash_50.iterrows():
   reasons = row['under_invest_reasons']
   if 'no_candidates' in reasons:
       classification['no_candidates'] += 1
   elif 'few_candidates' in reasons:
       classification['few_candidates'] += 1
   elif 'market_timing' in reasons and 'execution_gap' not in reasons:
       classification['market_timing'] += 1
   elif 'defense_not_filled' in reasons:
       classification['defense_not_filled'] += 1
   elif 'execution_gap' in reasons:
       classification['execution_gap'] += 1
   else:
       classification['other'] += 1

for reason, count in classification.items():
   if count > 0:
       print(f"  {reason}: {count} days")

# ============================================================
# 6. Specific anomalies
# ============================================================
print("\n" + "=" * 60)
print("Specific Anomalies")
print("=" * 60)

# Single core ETF > 25%
print("\nSingle core ETF > 25%:")
over_25_records = []
for _, row in df_audit.iterrows():
   for etf in row['etf_details']:
       if etf['type'] == 'core' and etf['nav_pct'] > 0.25:
           over_25_records.append({
               'date': row['date_str'],
               'ticker': etf['ticker'],
               'nav_pct': etf['nav_pct'],
           })

if over_25_records:
   df_over_25 = pd.DataFrame(over_25_records)
   print(df_over_25.to_string(index=False))
else:
   print("  None")

# Total position significantly below allowed with abundant candidates
print("\nTotal position < allowed - 10% with core_candidates >= 5:")
under_invested = df_audit[
   (df_audit['gap'] > 0.10) & 
   (df_audit['core_candidates'] >= 5)
].sort_values('gap', ascending=False)

if len(under_invested) > 0:
   print(under_invested[['date_str', 'total_position_ratio', 'market_signal', 'gap', 'core_candidates', 'buy_candidates']].head(20).to_string(index=False))
else:
   print("  None")

# ============================================================
# 7. 2022-2024 major cash intervals explanation
# ============================================================
print("\n" + "=" * 60)
print("2022-2024 Major Cash Intervals Explanation")
print("=" * 60)

# Find intervals in 2022-2024 with cash >= 50%
for start, end, length in intervals_50:
   year_start = start.year
   year_end = end.year
   if year_start >= 2022 and year_start <= 2024 or year_end >= 2022 and year_end <= 2024:
       print(f"\n  {start.date()} ~ {end.date()} ({length} days)")
       # Get sample days from this interval
       sample = df_audit[(df_audit['date'] >= start) & (df_audit['date'] <= end)]
       if len(sample) > 0:
           avg_cash = sample['cash_ratio'].mean()
           avg_candidates = sample['core_candidates'].mean()
           avg_market_signal = sample['market_signal'].mean()
           print(f"    Avg cash: {avg_cash:.1%}")
           print(f"    Avg core candidates: {avg_candidates:.1f}")
           print(f"    Avg market signal: {avg_market_signal:.2f}")
           # Get unique reasons
           all_reasons = set()
           for reasons in sample['under_invest_reasons']:
               all_reasons.update(reasons)
           print(f"    Primary reasons: {', '.join(sorted(all_reasons)) if all_reasons else 'price-driven'}")

print("\n" + "=" * 60)
print("B0 Daily Position Audit Complete")
print("=" * 60)
