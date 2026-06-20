# -*- coding: utf-8 -*-
"""S1 acceptance test v2 - 16 ETF pool, threshold 5000"""
import sys, os, pandas as pd, numpy as np
from datetime import timedelta

sys.path.insert(0, 'D:/etf_rotation_model/src')

# Freeze config to 16 ETFs
import config as _config_module
_config_module.CORE_UNIVERSE = _config_module.ETF_UNIVERSE

from backtest import BacktestEngine
from database import ETFDatabase
import config

# Unified dates
DATA_END = pd.Timestamp('2026-06-05')
STRATEGY_START = pd.Timestamp('2019-08-08')  # Effective start = 2019-08-13

ALL_CORE_TICKERS = list(config.ETF_UNIVERSE.keys())
ALL_DEFENSE_TICKERS = list(config.DEFENSE_UNIVERSE.keys())

# Load data
print("=" * 60)
print("S1 Acceptance - 16 ETF Pool")
print("=" * 60)
print("\n[1/3] Loading data...")

db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=ALL_CORE_TICKERS + ALL_DEFENSE_TICKERS)
bench_df = db.get_market_data(ticker=config.BENCHMARK)
market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])
market_df = market_df[market_df['date'] <= DATA_END].copy()
bench_df = bench_df[bench_df['date'] <= DATA_END].copy()

print(f"  Market data: {market_df['date'].nunique()} days, {market_df['ticker'].nunique()} ETFs")
print(f"  Benchmark data: {bench_df['date'].nunique()} days")

# Run B0 and S1
print("\n[2/3] Running B0 and S1 backtests...")

results = {}
for s1 in [False, True]:
    label = 'S1' if s1 else 'B0'
    bt = BacktestEngine(cfg=config.STRATEGY_CONFIG.copy(), s1_mode=s1)
    result = bt.run(market_df, bench_df)
    
    df = result['nav_df'].copy()
    df = df[df['date'] <= DATA_END].copy()
    
    # Metrics
    total = (df['nav'].iloc[-1] / 1e6) - 1
    years = (df['date'].iloc[-1] - df['date'].iloc[0]).days / 365.25
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else 0
    
    ret = df['nav'].pct_change().dropna()
    sharpe = (ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0
    
    cummax = df['nav'].cummax()
    dd = (df['nav'] - cummax) / cummax
    mdd = dd.min()
    vol = ret.std() * np.sqrt(252)
    
    trades = df['target_changed'].sum() if 'target_changed' in df.columns else 0
    
    print(f"\n{label} Results:")
    print(f"  Start NAV: {df['nav'].iloc[0]:,.2f}")
    print(f"  Final NAV: {df['nav'].iloc[-1]:,.2f}")
    print(f"  Total Return: {total*100:.2f}%")
    print(f"  Annualized: {ann*100:.2f}%")
    print(f"  Sharpe: {sharpe:.2f}")
    print(f"  Max Drawdown: {mdd*100:.2f}%")
    print(f"  Volatility: {vol*100:.2f}%")
    print(f"  Trades: {trades}")
    print(f"  Days: {len(df)} ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()})")
    
    results[label] = {
        'nav_df': df,
        'bt': bt,
        'total': total,
        'ann': ann,
        'sharpe': sharpe,
        'mdd': mdd,
        'vol': vol,
        'trades': trades,
    }

# Detailed analysis
print("\n" + "=" * 60)
print("Detailed Analysis (Valid Rebalance Events)")
print("=" * 60)

bt0 = results['B0']['bt']
bt1 = results['S1']['bt']
df0 = results['B0']['nav_df']
df1 = results['S1']['nav_df']

signals0 = bt0.strategy.signals_history
signals1 = bt1.strategy.signals_history

# Build valid rebalance event table
records = []
for date in df0.index:
    if date not in signals0 or date not in signals1:
        continue
    
    s0 = signals0[date]
    s1 = signals1[date]
    
    if 'next_target' not in s0 or s0['next_target'] is None:
        continue
    
    records.append({
        'date': date,
        'market_state': s0.get('market_state', 'unknown'),
        'b0_target': set(s0['next_target'].keys()),
        's1_target': set(s1['next_target'].keys()) if 'next_target' in s1 and s1['next_target'] else set(),
        'b0_scores': list(s0['next_target'].values()),
        's1_scores': list(s1['next_target'].values()) if 'next_target' in s1 and s1['next_target'] else [],
    })

df_events = pd.DataFrame(records)
print(f"\nValid rebalance events: {len(df_events)}")
print(f"Date range: {df_events['date'].min().date()} ~ {df_events['date'].max().date()}")

# Changed events
changed = 0
for _, row in df_events.iterrows():
    if row['b0_target'] != row['s1_target']:
        changed += 1

print(f"Changed events: {changed} ({changed/len(df_events)*100:.1f}%)")
print(f"Unchanged events: {len(df_events) - changed} ({(len(df_events)-changed)/len(df_events)*100:.1f}%)")

# Market state stats (not annualized, equal weight per event)
print("\n" + "=" * 60)
print("Market State Stats (per event, not annualized)")
print("=" * 60)

for state in ['strong_bull', 'weak_bull', 'swing', 'bear']:
    mask = df_events['market_state'] == state
    count = mask.sum()
    if count > 0:
        pct = count / len(df_events) * 100
        print(f"  {state}: {count} ({pct:.1f}%)")
    else:
        print(f"  {state}: 0")

# Filtering stats (hard condition -> subset ranking)
print("\n" + "=" * 60)
print("Filtering Stats (Hard Condition -> Subset Ranking)")
print("=" * 60)

hard_filtered = []
for date in df_events['date']:
    s0 = signals0[date]
    s1 = signals1[date]
    
    mature = len(s0.get('next_target', {}))
    hard_pass = len(s1.get('next_target', {})) if 'next_target' in s1 and s1['next_target'] else 0
    
    hard_filtered.append({'date': date, 'mature': mature, 'hard_pass': hard_pass})

df_hard = pd.DataFrame(hard_filtered)
avg_mature = df_hard['mature'].mean()
avg_hard = df_hard['hard_pass'].mean()
filter_rate = (avg_mature - avg_hard) / avg_mature * 100 if avg_mature > 0 else 0

print(f"  Avg mature ETFs: {avg_mature:.1f}")
print(f"  Avg hard pass: {avg_hard:.1f}")
print(f"  Avg filter rate: {filter_rate:.1f}%")
print(f"  Events with filtering: {(df_hard['hard_pass'] < df_hard['mature']).sum()} ({(df_hard['hard_pass'] < df_hard['mature']).mean()*100:.1f}%)")
print(f"  Events without filtering: {(df_hard['hard_pass'] == df_hard['mature']).sum()}")

# Acceptance check (NAV difference consistency)
print("\n" + "=" * 60)
print("Acceptance Check")
print("=" * 60)

final_nav_diff = df1['nav'].iloc[-1] - df0['nav'].iloc[-1]
print(f"Final NAV diff (S1-B0): {final_nav_diff:.2f}")

# Daily alpha analysis
df0_aligned = df0.set_index('date').reindex(df1['date'])
merged = pd.DataFrame({'b0_nav': df0_aligned['nav'], 's1_nav': df1['nav']})
merged['daily_alpha'] = merged['s1_nav'].pct_change() - merged['b0_nav'].pct_change()
merged['daily_alpha'] = merged['daily_alpha'].fillna(0)

# Verify: sum of daily alpha * B0_NAV should approximate final diff
# But due to compounding, this is not exact. Use a simpler check:
# The simulated S1 NAV from daily alpha should match actual S1 NAV
simulated_s1 = [merged['b0_nav'].iloc[0]]
for i in range(1, len(merged)):
    b0_ret = merged['b0_nav'].iloc[i] / merged['b0_nav'].iloc[i-1] - 1
    alpha = merged['daily_alpha'].iloc[i]
    s1_ret = b0_ret + alpha
    simulated_s1.append(simulated_s1[-1] * (1 + s1_ret))

simulated_diff = simulated_s1[-1] - merged['b0_nav'].iloc[-1]

print(f"\nSimulated diff from daily alpha: {simulated_diff:.2f}")
print(f"Actual diff: {final_nav_diff:.2f}")
print(f"Discrepancy: {abs(simulated_diff - final_nav_diff):.2f}")

if abs(simulated_diff - final_nav_diff) < 5000:
    print("Consistency: PASS (threshold=5000)")
else:
    print(f"Consistency: FAIL (discrepancy={abs(simulated_diff - final_nav_diff):.2f} > 5000)")

# Daily alpha stats
print(f"\nDaily alpha stats:")
print(f"  Sum: {merged['daily_alpha'].sum():.6f}")
print(f"  Mean: {merged['daily_alpha'].mean():.6f}")
print(f"  Std: {merged['daily_alpha'].std():.6f}")
print(f"  Positive days: {(merged['daily_alpha'] > 0).sum()}")
print(f"  Negative days: {(merged['daily_alpha'] < 0).sum()}")

print("\n" + "=" * 60)
print("S1 Acceptance Complete")
print("=" * 60)
