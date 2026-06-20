# -*- coding: utf-8 -*-
"""S1 acceptance test v3 - 16 ETF pool, threshold 5000, using StrategyEngine directly"""
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

# Load data
print("=" * 60)
print("S1 Acceptance - 16 ETF Pool")
print("=" * 60)
print("\n[1/4] Loading data...")

db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=ALL_CORE_TICKERS + ALL_DEFENSE_TICKERS)
bench_df = db.get_market_data(ticker=config.BENCHMARK)
market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])
market_df = market_df[market_df['date'] <= DATA_END].copy()
bench_df = bench_df[bench_df['date'] <= DATA_END].copy()

all_dates = sorted(market_df['date'].unique())
all_weekdays = [d for d in all_dates if d.weekday() == 3]

print(f"  Market data: {market_df['date'].nunique()} days, {market_df['ticker'].nunique()} ETFs")
print(f"  Benchmark data: {bench_df['date'].nunique()} days")
print(f"  All Thursdays: {len(all_weekdays)}")

# Run B0 and S1 backtests
print("\n[2/4] Running B0 and S1 backtests...")

cfg = config.STRATEGY_CONFIG.copy()

bt_b0 = BacktestEngine(cfg, s1_mode=False)
bt_s1 = BacktestEngine(cfg, s1_mode=True)
result_b0 = bt_b0.run(market_df, bench_df)
result_s1 = bt_s1.run(market_df, bench_df)

nav_b0 = result_b0['nav_df'].copy()
nav_s1 = result_s1['nav_df'].copy()
nav_b0 = nav_b0[nav_b0['date'] <= DATA_END]
nav_s1 = nav_s1[nav_s1['date'] <= DATA_END]

for label, nav in [('B0', nav_b0), ('S1', nav_s1)]:
    sub = nav[nav['date'] >= WARMUP_END].sort_values('date').reset_index(drop=True)
    total = sub['nav'].iloc[-1] / sub['nav'].iloc[0] - 1
    years = (sub['date'].iloc[-1] - sub['date'].iloc[0]).days / 365.25
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else 0
    ret = sub['nav'].pct_change().dropna()
    vol = ret.std() * np.sqrt(252)
    sharpe = ann / vol if vol > 0 else 0
    mdd = ((sub['nav'] - sub['nav'].cummax()) / sub['nav'].cummax()).min()
    
    print(f"\n{label} Results:")
    print(f"  Start NAV: {sub['nav'].iloc[0]:,.2f}")
    print(f"  Final NAV: {sub['nav'].iloc[-1]:,.2f}")
    print(f"  Total Return: {total:.2%}")
    print(f"  Annualized: {ann:.2%}")
    print(f"  Sharpe: {sharpe:.2f}")
    print(f"  Max Drawdown: {mdd:.2%}")
    print(f"  Days: {len(sub)}")

# Calculate signals using StrategyEngine directly
print("\n[3/4] Calculating signals...")

strategy_b0 = StrategyEngine(cfg, s1_mode=False)
strategy_s1 = StrategyEngine(cfg, s1_mode=True)

all_scores_b0 = []
all_scores_s1 = []
for ticker in ALL_CORE_TICKERS:
    tdf = market_df[market_df['ticker'] == ticker].copy()
    if len(tdf) < 51:
        continue
    scored_b0 = strategy_b0.calculate_total_score(tdf)
    scored_s1 = strategy_s1.calculate_total_score(tdf)
    all_scores_b0.append(scored_b0)
    all_scores_s1.append(scored_s1)

scores_b0 = pd.concat(all_scores_b0, ignore_index=True)
scores_s1 = pd.concat(all_scores_s1, ignore_index=True)

scores_b0 = strategy_b0.rank_all_momentum(scores_b0)
scores_b0 = strategy_b0.compute_total_score(scores_b0)
scores_s1_raw = strategy_s1.compute_total_score(scores_s1)

signals_b0 = strategy_b0.generate_signals(scores_b0, bench_df)
signals_s1 = strategy_s1.generate_signals(scores_s1, bench_df)

# Extract BUY signals for core ETFs
buy_b0 = signals_b0[
    (signals_b0['ticker'].isin(ALL_CORE_TICKERS)) & 
    (signals_b0['signal_type'] == 'BUY') &
    (signals_b0['momentum_valid'] == True)
].copy()

buy_s1 = signals_s1[
    (signals_s1['ticker'].isin(ALL_CORE_TICKERS)) & 
    (signals_s1['signal_type'] == 'BUY') &
    (signals_s1['momentum_valid'] == True)
].copy()

# Classify regime
def classify_regime(date, bench_df):
    bsub = bench_df[bench_df['date'] <= date].tail(60).sort_values('date')
    if len(bsub) < 50:
        return 'unknown'
    bsub['ma20'] = bsub['close'].rolling(20).mean()
    bsub['ma50'] = bsub['close'].rolling(50).mean()
    bsub['ma20_slope'] = bsub['ma20'].diff()
    bsub['ma50_slope'] = bsub['ma50'].diff()
    row = bsub.iloc[-1]
    close, ma20, ma50, s20, s50 = row['close'], row['ma20'], row['ma50'], row['ma20_slope'], row['ma50_slope']
    if pd.isna(ma50):
        return 'unknown'
    if close > ma20 and ma20 > ma50 and s20 > 0 and s50 > 0:
        return 'strong_bull'
    if close > ma50:
        return 'weak_bull'
    if close < ma50 and s50 < 0:
        return 'bear'
    return 'swing'

valid_rebalance_dates = []
for d in all_weekdays:
    if d < WARMUP_END:
        continue
    regime = classify_regime(d, bench_df)
    if regime == 'unknown':
        continue
    valid_rebalance_dates.append(d)

rebalance_event_dates = valid_rebalance_dates[:-1]

print(f"  All Thursdays: {len(all_weekdays)}")
print(f"  Post-warmup Thursdays: {len([d for d in all_weekdays if d >= WARMUP_END])}")
print(f"  Valid rebalance dates: {len(valid_rebalance_dates)}")
print(f"  Valid rebalance events: {len(rebalance_event_dates)}")

# Build valid rebalance event table
print("\n[4/4] Building event table...")

event_records = []
for date in rebalance_event_dates:
    regime = classify_regime(date, bench_df)
    
    b0_targets = buy_b0[buy_b0['date'] == date]['ticker'].tolist()
    s1_targets = buy_s1[buy_s1['date'] == date]['ticker'].tolist()
    
    b0_scores = buy_b0[buy_b0['date'] == date]['total_score'].tolist()
    s1_scores = buy_s1[buy_s1['date'] == date]['total_score'].tolist()
    
    event_records.append({
        'date': date,
        'market_state': regime,
        'b0_target': set(b0_targets),
        's1_target': set(s1_targets),
        'b0_scores': b0_scores,
        's1_scores': s1_scores,
    })

df_events = pd.DataFrame(event_records)
print(f"\nValid rebalance events: {len(df_events)}")

# Changed events
changed = 0
for _, row in df_events.iterrows():
    if row['b0_target'] != row['s1_target']:
        changed += 1

print(f"Changed events: {changed} ({changed/len(df_events)*100:.1f}%)")
print(f"Unchanged events: {len(df_events) - changed} ({(len(df_events)-changed)/len(df_events)*100:.1f}%)")

# Market state stats
print("\nMarket State Stats (per event, not annualized):")
for state in ['strong_bull', 'weak_bull', 'swing', 'bear']:
    count = (df_events['market_state'] == state).sum()
    if count > 0:
        print(f"  {state}: {count} ({count/len(df_events)*100:.1f}%)")
    else:
        print(f"  {state}: 0")

# Filtering stats: count mature vs hard-pass
print("\nFiltering Stats (hard condition -> subset ranking):")

# For each event, count how many ETFs passed B0 vs S1
filter_records = []
for date in rebalance_event_dates:
    # B0: all core ETFs with BUY signal and momentum_valid
    b0_count = len(buy_b0[buy_b0['date'] == date])
    # S1: hard condition passed (same as buy_s1 count)
    s1_count = len(buy_s1[buy_s1['date'] == date])
    filter_records.append({'date': date, 'mature': b0_count, 'hard_pass': s1_count})

df_filter = pd.DataFrame(filter_records)
avg_mature = df_filter['mature'].mean()
avg_hard = df_filter['hard_pass'].mean()
filter_rate = (avg_mature - avg_hard) / avg_mature * 100 if avg_mature > 0 else 0

print(f"  Avg mature ETFs: {avg_mature:.1f}")
print(f"  Avg hard pass: {avg_hard:.1f}")
print(f"  Avg filter rate: {filter_rate:.1f}%")
print(f"  Events with filtering: {(df_filter['hard_pass'] < df_filter['mature']).sum()}")
print(f"  Events without filtering: {(df_filter['hard_pass'] == df_filter['mature']).sum()}")

# Acceptance check: NAV difference consistency
print("\nAcceptance Check:")
final_nav_diff = nav_s1['nav'].iloc[-1] - nav_b0['nav'].iloc[-1]
print(f"Final NAV diff (S1-B0): {final_nav_diff:.2f}")

# Align by date and compute daily alpha
merged = nav_b0[['date', 'nav']].merge(nav_s1[['date', 'nav']], on='date', suffixes=('_b0', '_s1'))
merged = merged[merged['date'] >= WARMUP_END].sort_values('date').reset_index(drop=True)
merged['b0_ret'] = merged['nav_b0'].pct_change()
merged['s1_ret'] = merged['nav_s1'].pct_change()
merged['daily_alpha'] = merged['s1_ret'] - merged['b0_ret']
merged['daily_alpha'] = merged['daily_alpha'].fillna(0)

# Simulate S1 NAV from daily alpha
simulated = [merged['nav_b0'].iloc[0]]
for i in range(1, len(merged)):
    b0_ret = merged['b0_ret'].iloc[i]
    alpha = merged['daily_alpha'].iloc[i]
    simulated.append(simulated[-1] * (1 + b0_ret + alpha))

simulated_diff = simulated[-1] - merged['nav_b0'].iloc[-1]
print(f"Simulated diff from daily alpha: {simulated_diff:.2f}")
print(f"Actual diff: {final_nav_diff:.2f}")
print(f"Discrepancy: {abs(simulated_diff - final_nav_diff):.2f}")

if abs(simulated_diff - final_nav_diff) < 5000:
    print("Consistency: PASS (threshold=5000)")
else:
    print(f"Consistency: FAIL (discrepancy={abs(simulated_diff - final_nav_diff):.2f} > 5000)")

print(f"\nDaily alpha stats:")
print(f"  Sum: {merged['daily_alpha'].sum():.6f}")
print(f"  Mean: {merged['daily_alpha'].mean():.6f}")
print(f"  Std: {merged['daily_alpha'].std():.6f}")
print(f"  Positive days: {(merged['daily_alpha'] > 0).sum()}")
print(f"  Negative days: {(merged['daily_alpha'] < 0).sum()}")

print("\n" + "=" * 60)
print("S1 Acceptance Complete")
print("=" * 60)
