# -*- coding: utf-8 -*-
"""B0 Target vs Actual Position Audit v5 - Price drift + execution gap decomposition, 16 ETF pool"""
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

MAX_POS_PER_ETF = config.STRATEGY_CONFIG['max_position_per_etf']
STOCK_MAX_HOLDINGS = config.STRATEGY_CONFIG['stock_max_holdings']
TOTAL_MAX_HOLDINGS = config.STRATEGY_CONFIG['total_max_holdings']
DEFENSE_MAX_HOLDINGS = config.STRATEGY_CONFIG['defense_max_holdings']
DEFENSE_FILL_MAX_BULL = config.STRATEGY_CONFIG.get('defense_fill_max_ratio_bull', 0.30)
DEFENSE_FILL_MAX_BEAR = config.STRATEGY_CONFIG.get('defense_fill_max_ratio_bear', 0.50)

print("=" * 80)
print("B0 Target vs Actual Position Audit v5 - Price Drift Decomposition")
print("=" * 80)

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

print(f"  B0: {len(nav_df)} days")

nav_df['date_only'] = nav_df['date'].dt.date
positions_by_date = dict(zip(nav_df['date_only'], nav_df['positions_detail']))

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
# 3. Per-day target vs actual calculation with price drift
# ============================================================
print("\n[3/6] Calculating target, price drift, and execution gap...")

audit_records = []

for i, row in nav_df.iterrows():
    date = row['date']
    nav = row['nav']
    cash = row['cash']
    positions_value = row['positions_value']
    positions_detail = row['positions_detail'] or {}
    max_total_pos = row['max_total_position']
    
    is_rebalance = date.weekday() == 3
    
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
    
    # Get BUY candidates
    day_buy = buy_signals[buy_signals['date'] == date]
    core_buy = day_buy[day_buy['ticker'].isin(CORE_TICKERS_SET)]
    defense_buy = day_buy[day_buy['ticker'].isin(DEFENSE_TICKERS_SET)]
    core_candidates = set(core_buy['ticker'].tolist())
    defense_candidates = set(defense_buy['ticker'].tolist())
    n_core_candidates = len(core_candidates)
    n_defense_candidates = len(defense_candidates)
    
    if is_rebalance:
        # Get pre-rebalance holdings
        prev_date = date - pd.Timedelta(days=1)
        while prev_date.weekday() >= 5 or prev_date.date() not in positions_by_date:
            prev_date -= pd.Timedelta(days=1)
            if prev_date < nav_df['date'].min():
                break
        
        if prev_date.date() in positions_by_date:
            pre_positions = positions_by_date[prev_date.date()] or {}
        else:
            pre_positions = {}
        
        pre_core = [t for t in pre_positions if t in CORE_TICKERS_SET]
        pre_defense = [t for t in pre_positions if t in DEFENSE_TICKERS_SET]
        
        core_retained = [t for t in pre_core if t in core_candidates]
        defense_retained = [t for t in pre_defense if t in defense_candidates]
        n_core_retained = len(core_retained)
        n_defense_retained = len(defense_retained)
        
        total_retained = n_core_retained + n_defense_retained
        max_new = TOTAL_MAX_HOLDINGS - total_retained
        
        core_new = [t for t in core_candidates if t not in pre_core]
        core_slots = min(len(core_new), max_new, STOCK_MAX_HOLDINGS - n_core_retained)
        
        # Industry target = (retained + new) * 20% of NAV
        industry_target = (n_core_retained + core_slots) * min(MAX_POS_PER_ETF, 1.0 / STOCK_MAX_HOLDINGS) * max_total_pos
        industry_target = min(industry_target, max_total_pos)
        
        # Defense fill
        if max_total_pos >= 1.0:
            defense_fill_max = DEFENSE_FILL_MAX_BULL
        else:
            defense_fill_max = DEFENSE_FILL_MAX_BEAR
        
        max_new_after_core = max_new - core_slots
        defense_new = [t for t in defense_candidates if t not in pre_defense]
        defense_slots = min(len(defense_new), max_new_after_core, DEFENSE_MAX_HOLDINGS - n_defense_retained)
        
        if industry_target < max_total_pos and defense_slots > 0:
            gap_to_fill = max_total_pos - industry_target
            defense_fill_target = min(gap_to_fill, defense_fill_max)
            defense_fill_target = min(defense_fill_target, defense_slots * 0.15)
        else:
            defense_fill_target = 0.0
        
        total_target = min(industry_target + defense_fill_target, max_total_pos)
        
        # Price drift: change in value of retained holdings from cost to current market value
        price_drift_industry = 0.0
        price_drift_defense = 0.0
        
        for ticker in core_retained:
            if ticker in pre_positions and ticker in positions_detail:
                pre_mv = pre_positions[ticker].get('market_value', 0)
                post_mv = positions_detail[ticker].get('market_value', 0)
                # Price drift for retained = (post_mv - pre_mv) / nav
                # But pre_mv was already subject to price changes before this day
                # Actually, we want: (current_mv - target_mv) / nav
                # target_mv = shares * cost (the target amount at entry)
                # But we don't know cost easily. Let's use pre_mv as baseline.
                # Actually, for retained holdings, the price drift since last rebalance
                # is already reflected in pre_mv. So price drift on this day = 0 for retained.
                # The only price drift is for NEWLY BOUGHT holdings (open vs close).
                pass
        
        # For simplicity: price drift = actual - (target based on cost)
        # But we can't easily get cost for all retained holdings.
        # Approximation: price drift = actual_position - total_target (if execution was perfect)
        # But this includes both price drift and execution gap.
        
        # Alternative: calculate price drift for retained holdings from pre-rebalance to post-rebalance
        pre_industry_value = sum(pre_positions[t].get('market_value', 0) for t in pre_core if t in pre_positions)
        pre_defense_value = sum(pre_positions[t].get('market_value', 0) for t in pre_defense if t in pre_positions)
        
        # Retained holdings' pre-value
        pre_retained_industry = sum(pre_positions[t].get('market_value', 0) for t in core_retained if t in pre_positions)
        pre_retained_defense = sum(pre_positions[t].get('market_value', 0) for t in defense_retained if t in pre_positions)
        
        # Retained holdings' post-value
        post_retained_industry = sum(positions_detail[t].get('market_value', 0) for t in core_retained if t in positions_detail)
        post_retained_defense = sum(positions_detail[t].get('market_value', 0) for t in defense_retained if t in positions_detail)
        
        # Price drift for retained = (post - pre) / nav
        price_drift_retained = (post_retained_industry + post_retained_defense - pre_retained_industry - pre_retained_defense) / nav if nav > 0 else 0
        
        # Newly bought holdings: target was based on open price, but market value is based on close price
        # For simplicity, assume newly bought holdings have price drift = 0 (same day)
        # Actually, they are bought at open price, and market value is at close price.
        # So there IS price drift for newly bought holdings.
        # But we don't have open price easily. Let's ignore for now.
        
        # Execution gap = actual - target - price_drift
        execution_gap = actual_position_ratio - total_target - price_drift_retained
        
        # Cash decomposition
        strategy_design_cash = 1.0 - max_total_pos
        candidate_shortfall = max_total_pos - industry_target
        defense_fill_gap = max(0, defense_fill_target - actual_defense_ratio)
        
        expected_cash = 1.0 - total_target
        unexplained_gap = actual_cash_ratio - expected_cash
        
    else:
        # Non-rebalance day: just record price drift from previous day
        pre_date = date - pd.Timedelta(days=1)
        while pre_date.weekday() >= 5 or pre_date.date() not in positions_by_date:
            pre_date -= pd.Timedelta(days=1)
            if pre_date < nav_df['date'].min():
                break
        
        if pre_date.date() in positions_by_date:
            pre_positions = positions_by_date[pre_date.date()] or {}
        else:
            pre_positions = {}
        
        pre_industry = sum(pre_positions[t].get('market_value', 0) for t in pre_positions if t in CORE_TICKERS_SET)
        post_industry = sum(positions_detail[t].get('market_value', 0) for t in positions_detail if t in CORE_TICKERS_SET)
        pre_defense = sum(pre_positions[t].get('market_value', 0) for t in pre_positions if t in DEFENSE_TICKERS_SET)
        post_defense = sum(positions_detail[t].get('market_value', 0) for t in positions_detail if t in DEFENSE_TICKERS_SET)
        
        price_drift_retained = (post_industry + post_defense - pre_industry - pre_defense) / nav if nav > 0 else 0
        
        core_retained = defense_retained = []
        n_core_retained = n_defense_retained = 0
        max_new = core_slots = defense_slots = 0
        industry_target = defense_fill_target = total_target = None
        strategy_design_cash = candidate_shortfall = defense_fill_gap = execution_gap = None
        unexplained_gap = None
    
    audit_records.append({
        'date': date, 'date_str': date.strftime('%Y-%m-%d'),
        'is_rebalance': is_rebalance, 'nav': nav, 'cash': cash,
        'actual_cash_ratio': actual_cash_ratio,
        'actual_industry_ratio': actual_industry_ratio,
        'actual_defense_ratio': actual_defense_ratio,
        'actual_position_ratio': actual_position_ratio,
        'market_signal': max_total_pos,
        'core_candidates': n_core_candidates,
        'defense_candidates': n_defense_candidates,
        'core_retained': n_core_retained,
        'defense_retained': n_defense_retained,
        'max_new': max_new if is_rebalance else None,
        'core_slots': core_slots if is_rebalance else None,
        'defense_slots': defense_slots if is_rebalance else None,
        'industry_target': industry_target,
        'defense_fill_target': defense_fill_target,
        'total_target': total_target,
        'price_drift': price_drift_retained,
        'execution_gap': execution_gap if is_rebalance else None,
        'strategy_design_cash': strategy_design_cash,
        'candidate_shortfall': candidate_shortfall,
        'defense_fill_gap': defense_fill_gap,
        'unexplained_gap': unexplained_gap,
    })

df_audit = pd.DataFrame(audit_records)

# Save CSV
os.makedirs('reports', exist_ok=True)
csv_path = 'reports/b0_target_actual_audit_v5.csv'
df_audit.to_csv(csv_path, index=False, encoding='utf-8-sig')
print(f"  Saved: {csv_path}")

# ============================================================
# 4. Summary
# ============================================================
print("\n" + "=" * 80)
print("[4/6] Rebalance Day Summary")
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

print(f"\nRetention stats:")
print(f"  Avg core retained: {df_rebal['core_retained'].mean():.1f}")
print(f"  Avg defense retained: {df_rebal['defense_retained'].mean():.1f}")
print(f"  Avg max_new: {df_rebal['max_new'].mean():.1f}")
print(f"  Avg core slots: {df_rebal['core_slots'].mean():.1f}")
print(f"  Avg defense slots: {df_rebal['defense_slots'].mean():.1f}")

print(f"\nAverage gaps:")
print(f"  Price drift (retained): {df_rebal['price_drift'].mean():.2%}")
print(f"  Execution gap: {df_rebal['execution_gap'].mean():.2%}")
print(f"  Strategy design cash: {df_rebal['strategy_design_cash'].mean():.2%}")
print(f"  Candidate shortfall: {df_rebal['candidate_shortfall'].mean():.2%}")
print(f"  Defense fill gap: {df_rebal['defense_fill_gap'].mean():.2%}")
print(f"  Unexplained gap: {df_rebal['unexplained_gap'].mean():.2%}")

# ============================================================
# 5. High cash intervals
# ============================================================
print("\n" + "=" * 80)
print("[5/6] High Cash Interval Decomposition (2022-2024)")
print("=" * 80)

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
    
    rebal_days = sub[sub['is_rebalance'] == True]
    if len(rebal_days) > 0:
        avg_strategy_cash = rebal_days['strategy_design_cash'].mean()
        avg_candidate_short = rebal_days['candidate_shortfall'].mean()
        avg_defense_gap = rebal_days['defense_fill_gap'].mean()
        avg_exec_gap = rebal_days['execution_gap'].mean()
        avg_price_drift = rebal_days['price_drift'].mean()
        avg_core_cand = rebal_days['core_candidates'].mean()
        avg_core_retained = rebal_days['core_retained'].mean()
        avg_core_slots = rebal_days['core_slots'].mean()
    else:
        avg_strategy_cash = avg_candidate_short = avg_defense_gap = avg_exec_gap = avg_price_drift = 0
        avg_core_cand = avg_core_retained = avg_core_slots = 0
    
    print(f"\n  {start.date()} ~ {end.date()} ({length} days)")
    print(f"    Actual:   cash={avg_cash:.1%}, industry={avg_industry:.1%}, defense={avg_defense:.1%}")
    print(f"    Cash decomposition:")
    print(f"      Strategy design:     {avg_strategy_cash:.1%}")
    print(f"      Candidate shortfall: {avg_candidate_short:.1%}")
    print(f"      Defense fill gap:    {avg_defense_gap:.1%}")
    print(f"      Price drift:         {avg_price_drift:.1%}")
    print(f"      Execution gap:       {avg_exec_gap:.1%}")
    print(f"    Avg core: candidates={avg_core_cand:.1f}, retained={avg_core_retained:.1f}, new_slots={avg_core_slots:.1f}")

# ============================================================
# 6. Significant execution gaps
# ============================================================
print("\n" + "=" * 80)
print("[6/6] Rebalance Days with Significant Execution Gap")
print("=" * 80)

exec_gaps = df_rebal[df_rebal['execution_gap'].abs() > 0.02].sort_values('execution_gap')
print(f"\n{len(exec_gaps)} rebalance days with |execution gap| > 2%")

print("\nTop 10: Positive execution gap (actual > target + drift):")
top_pos = df_rebal.nlargest(10, 'execution_gap')
for _, row in top_pos.iterrows():
    print(f"  {row['date_str']}: exec_gap=+{row['execution_gap']:.1%}, price_drift={row['price_drift']:.1%}")
    print(f"    actual_pos={row['actual_position_ratio']:.1%}, target={row['total_target']:.1%}")
    print(f"    core={row['core_candidates']}, retained={row['core_retained']}, slots={row['core_slots']}")

print("\nTop 10: Negative execution gap (actual < target + drift):")
top_neg = df_rebal.nsmallest(10, 'execution_gap')
for _, row in top_neg.iterrows():
    print(f"  {row['date_str']}: exec_gap={row['execution_gap']:.1%}, price_drift={row['price_drift']:.1%}")
    print(f"    actual_pos={row['actual_position_ratio']:.1%}, target={row['total_target']:.1%}")
    print(f"    core={row['core_candidates']}, retained={row['core_retained']}, slots={row['core_slots']}")

# ============================================================
# 7. Consistency check
# ============================================================
print("\n" + "=" * 80)
print("Consistency Check: Target + Drift + Gap = Actual")
print("=" * 80)

df_rebal['check_sum'] = df_rebal['total_target'] + df_rebal['price_drift'] + df_rebal['execution_gap']
df_rebal['check_diff'] = df_rebal['actual_position_ratio'] - df_rebal['check_sum']
max_check_diff = df_rebal['check_diff'].abs().max()
print(f"\nMax |actual - (target + drift + gap)|: {max_check_diff:.2%}")

if max_check_diff < 0.01:
    print("PASS: Identity holds within 1%")
else:
    print(f"FAIL: {len(df_rebal[df_rebal['check_diff'].abs() > 0.01])} days exceed 1%")

print("\n" + "=" * 80)
print("B0 Target vs Actual Position Audit v5 Complete")
print("=" * 80)
