# -*- coding: utf-8 -*-
"""B0 Daily Position Audit with Plots - 16 ETF pool"""
import sys, os, pandas as pd, numpy as np
from datetime import timedelta

sys.path.insert(0, 'D:/etf_rotation_model/src')

import config as _config_module
_config_module.CORE_UNIVERSE = _config_module.ETF_UNIVERSE

from backtest import BacktestEngine
from strategy import StrategyEngine
from database import ETFDatabase
import config

import matplotlib
matplotlib.use("Agg")

DATA_END = pd.Timestamp('2026-06-05')
WARMUP_END = pd.Timestamp('2019-08-13')

ALL_CORE_TICKERS = list(config.ETF_UNIVERSE.keys())
ALL_DEFENSE_TICKERS = list(config.DEFENSE_UNIVERSE.keys())
DEFENSE_TICKERS_SET = set(ALL_DEFENSE_TICKERS)
CORE_TICKERS_SET = set(ALL_CORE_TICKERS)

print("=" * 60)
print("B0 Daily Position Audit + Plots - 16 ETF Pool")
print("=" * 60)

# ============================================================
# 1. Load data and run B0
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
nav_df = nav_df[nav_df['date'] >= WARMUP_END].sort_values('date').reset_index(drop=True)

trades_df = result_b0['trades_df'].copy()
if not trades_df.empty:
    trades_df['date'] = pd.to_datetime(trades_df['date'])
    buy_trades = trades_df[trades_df['action'] == 'BUY']
else:
    buy_trades = pd.DataFrame()

print(f"  B0 backtest: {len(nav_df)} days")
print(f"  Total trades: {len(trades_df)}")
print(f"  BUY trades: {len(buy_trades)}")

# ============================================================
# 2. Recompute daily signals
# ============================================================
print("\n[2/5] Recomputing daily signals...")

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
mature_signals = signals[(signals['history_count'] >= 51) & (signals['momentum_valid'] == True)].copy()

# ============================================================
# 3. Build daily audit records
# ============================================================
print("\n[3/5] Building daily audit records...")

audit_records = []
for _, row in nav_df.iterrows():
    date = row['date']
    date_str = date.strftime('%Y-%m-%d')
    
    nav = row['nav']
    cash = row['cash']
    positions_value = row['positions_value']
    num_positions = row['num_positions']
    max_total_pos = row['max_total_position']
    positions_detail = row['positions_detail'] or {}
    positions_pct = row['positions_pct'] or {}
    
    cash_ratio = cash / nav if nav > 0 else 0
    total_position_ratio = positions_value / nav if nav > 0 else 0
    
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
    
    day_buy = buy_signals[buy_signals['date'] == date]
    day_mature = mature_signals[mature_signals['date'] == date]
    
    buy_candidates = len(day_buy)
    mature_count = len(day_mature)
    core_buy = day_buy[day_buy['ticker'].isin(CORE_TICKERS_SET)]
    defense_buy = day_buy[day_buy['ticker'].isin(DEFENSE_TICKERS_SET)]
    core_candidates = len(core_buy)
    defense_candidates = len(defense_buy)
    
    gap = max_total_pos - total_position_ratio
    is_rebalance = date.weekday() == 3
    
    under_invest_reasons = []
    if total_position_ratio < max_total_pos - 0.01:
        if buy_candidates == 0:
            under_invest_reasons.append("no_candidates")
        elif core_candidates < 5:
            under_invest_reasons.append("few_candidates")
        if max_total_pos < 1.0:
            under_invest_reasons.append("market_timing")
        if cash_ratio > 0.3 and core_candidates >= 5:
            under_invest_reasons.append("execution_gap")
    
    etf_details = []
    for ticker in sorted(positions_detail.keys()):
        detail = positions_detail[ticker]
        shares = detail['shares']
        mv = detail.get('market_value', 0)
        close_price = mv / shares if shares > 0 else 0
        pct = mv / nav if nav > 0 else 0
        etf_type = 'defense' if ticker in DEFENSE_TICKERS_SET else 'core'
        etf_details.append({
            'ticker': ticker, 'type': etf_type, 'shares': shares,
            'close_price': close_price, 'market_value': mv, 'nav_pct': pct,
        })
    
    audit_records.append({
        'date': date, 'date_str': date_str, 'nav': nav, 'cash': cash,
        'cash_ratio': cash_ratio, 'industry_value': industry_value,
        'industry_ratio': industry_ratio, 'defense_value': defense_value,
        'defense_ratio': defense_ratio, 'positions_value': positions_value,
        'total_position_ratio': total_position_ratio, 'num_positions': num_positions,
        'industry_count': len(industry_positions), 'defense_count': len(defense_positions),
        'mature_count': mature_count, 'buy_candidates': buy_candidates,
        'core_candidates': core_candidates, 'defense_candidates': defense_candidates,
        'market_signal': max_total_pos, 'max_total_position': max_total_pos,
        'target_position': max_total_pos, 'gap': gap, 'is_rebalance': is_rebalance,
        'under_invest_reasons': under_invest_reasons, 'etf_details': etf_details,
        'positions_detail': positions_detail,
    })

df_audit = pd.DataFrame(audit_records)

# Save daily audit CSV
os.makedirs('reports', exist_ok=True)
csv_path = 'reports/b0_daily_audit.csv'
# Flatten for CSV: extract key fields
csv_df = df_audit[['date_str', 'nav', 'cash', 'cash_ratio', 'industry_ratio', 'defense_ratio', 'total_position_ratio', 'num_positions', 'industry_count', 'defense_count', 'mature_count', 'buy_candidates', 'core_candidates', 'defense_candidates', 'market_signal', 'gap', 'is_rebalance']].copy()
csv_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
print(f"  Saved: {csv_path}")

# ============================================================
# 4. Validation
# ============================================================
print("\n" + "=" * 60)
print("[4/5] Validation")
print("=" * 60)

# Check 1: Cash + positions = NAV
df_audit['computed_total'] = df_audit['cash'] + df_audit['positions_value']
df_audit['nav_diff'] = abs(df_audit['computed_total'] - df_audit['nav'])
max_nav_diff = df_audit['nav_diff'].max()
print(f"\n1. Cash + Positions = NAV: max_diff={max_nav_diff:.2f} {'PASS' if max_nav_diff < 1.0 else 'FAIL'}")

# Check 2: Ratios sum = 100%
df_audit['sum_ratios'] = df_audit['cash_ratio'] + df_audit['industry_ratio'] + df_audit['defense_ratio']
max_ratio_diff = (df_audit['sum_ratios'] - 1.0).abs().max()
print(f"2. Ratios = 100%: max_diff={max_ratio_diff:.6f} {'PASS' if max_ratio_diff < 0.001 else 'FAIL'}")

# Check 3: Industry <= 5
max_industry = df_audit['industry_count'].max()
print(f"3. Industry <= 5: max={max_industry} {'PASS' if max_industry <= 5 else 'FAIL'}")

# Check 4: Total position <= market_signal
df_audit['position_vs_allowed'] = df_audit['total_position_ratio'] - df_audit['market_signal']
max_excess = df_audit['position_vs_allowed'].max()
print(f"4. Position <= allowed: max_excess={max_excess:.4f} {'PASS' if max_excess < 0.01 else 'FAIL'}")

# Check 5: BUY-day single core ETF <= max_position_per_etf (buy price)
max_position_cfg = config.STRATEGY_CONFIG['max_position_per_etf']
if not buy_trades.empty:
    buy_day_violations = []
    for _, trade in buy_trades.iterrows():
        # Find the NAV on that day
        day_nav = df_audit[df_audit['date'] == trade['date']]
        if len(day_nav) > 0:
            nav_val = day_nav['nav'].iloc[0]
            # Find target amount from trade
            amount = trade['amount']
            commission = trade['commission']
            total_cost = amount + commission
            target_pct = total_cost / nav_val if nav_val > 0 else 0
            if target_pct > max_position_cfg + 0.001:
                buy_day_violations.append({
                    'date': trade['date'].strftime('%Y-%m-%d'),
                    'ticker': trade['ticker'],
                    'target_pct': target_pct,
                    'max_allowed': max_position_cfg,
                })
    
    if buy_day_violations:
        print(f"5. BUY-day single core <= {max_position_cfg:.2%}: {len(buy_day_violations)} violations FAIL")
        for v in buy_day_violations[:5]:
            print(f"   {v['date']} {v['ticker']}: {v['target_pct']:.2%} > {v['max_allowed']:.2%}")
    else:
        print(f"5. BUY-day single core <= {max_position_cfg:.2%}: PASS (0 violations)")
else:
    print(f"5. BUY-day single core <= {max_position_cfg:.2%}: No BUY trades found")

# Holding-day > 25% (informational, not a constraint violation)
over_25 = []
for _, row in df_audit.iterrows():
    for etf in row['etf_details']:
        if etf['type'] == 'core' and etf['nav_pct'] > 0.25:
            over_25.append({'date': row['date_str'], 'ticker': etf['ticker'], 'nav_pct': etf['nav_pct']})
print(f"6. Holding-day core > 25% (info): {len(over_25)} days (price appreciation, not constraint violation)")

# ============================================================
# 5. Plotting
# ============================================================
print("\n" + "=" * 60)
print("[5/5] Plotting")
print("=" * 60)

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

plt.rcParams["figure.figsize"] = (16, 12)

fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True)

# Plot 1: Industry total, Defense, Cash
ax1 = axes[0]
ax1.fill_between(df_audit['date'], 0, df_audit['industry_ratio'], alpha=0.7, label='Industry ETF', color='steelblue')
ax1.fill_between(df_audit['date'], df_audit['industry_ratio'], df_audit['industry_ratio'] + df_audit['defense_ratio'], alpha=0.7, label='Defense', color='forestgreen')
ax1.fill_between(df_audit['date'], df_audit['industry_ratio'] + df_audit['defense_ratio'], 1.0, alpha=0.7, label='Cash', color='gold')
ax1.set_ylabel('Portfolio Allocation')
ax1.set_ylim(0, 1.0)
ax1.legend(loc='upper left')
ax1.set_title('B0 Position Allocation: Industry + Defense + Cash')
ax1.axhline(y=0.5, color='r', linestyle='--', alpha=0.3, label='50% cash')

# Plot 2: Individual ETF positions within industry stack
ax2 = axes[1]
# Prepare industry-only ETF time series
industry_tickers = sorted(CORE_TICKERS_SET)
industry_data = {t: [] for t in industry_tickers}
industry_dates = []

for _, row in df_audit.iterrows():
    industry_dates.append(row['date'])
    nav_val = row['nav']
    for ticker in industry_tickers:
        mv = 0
        for etf in row['etf_details']:
            if etf['ticker'] == ticker:
                mv = etf['market_value']
                break
        industry_data[ticker].append(mv / nav_val if nav_val > 0 else 0)

industry_df = pd.DataFrame(industry_data, index=industry_dates)
# Only plot if there are non-zero values
plot_tickers = [t for t in industry_tickers if industry_df[t].max() > 0.01]
colors = plt.cm.tab20(np.linspace(0, 1, len(plot_tickers)))

ax2.stackplot(industry_dates, *[industry_df[t] for t in plot_tickers], labels=plot_tickers, colors=colors, alpha=0.8)
ax2.set_ylabel('Industry ETF Allocation')
ax2.legend(loc='upper left', ncol=4, fontsize=7)
ax2.set_title('Industry ETF Composition (within industry allocation)')
ax2.set_ylim(0, 1.0)

# Plot 3: BUY candidates + max allowed position
ax3 = axes[2]
ax3.plot(df_audit['date'], df_audit['core_candidates'], label='Core BUY candidates', color='blue', alpha=0.7)
ax3.plot(df_audit['date'], df_audit['market_signal'] * 5, label='Max allowed slots (5 * signal)', color='red', alpha=0.7)
ax3.fill_between(df_audit['date'], 0, df_audit['market_signal'] * 5, alpha=0.1, color='red')
ax3.set_ylabel('Count / Slots')
ax3.set_xlabel('Date')
ax3.legend(loc='upper left')
ax3.set_title('Daily Core BUY Candidates vs Max Allowed Slots')
ax3.set_ylim(0, 8)

# Format x-axis
for ax in axes:
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    ax.xaxis.set_minor_locator(mdates.MonthLocator(interval=3))
    ax.grid(True, alpha=0.3)

plt.tight_layout()
plot_path = 'reports/b0_position_audit.png'
plt.savefig(plot_path, dpi=150, bbox_inches="tight")
plt.close()
print(f"  Saved: {plot_path}")

print("\n" + "=" * 60)
print("B0 Daily Position Audit Complete")
print("=" * 60)
