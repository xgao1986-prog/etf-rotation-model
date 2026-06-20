# -*- coding: utf-8 -*-
"""B0 逐订单诊断脚本 - 2026-03-12 重点追踪"""
import sys, os, pandas as pd, numpy as np
from datetime import timedelta

sys.path.insert(0, 'D:/etf_rotation_model/src')

import config as _config_module
_config_module.CORE_UNIVERSE = _config_module.ETF_UNIVERSE

from backtest import BacktestEngine
from strategy import StrategyEngine
from database import ETFDatabase
import config

# 修改 backtest.py 以在 2026-03-12 打印详细日志
# 我们通过 monkey-patch 来实现

original_execute = BacktestEngine._execute_backtest

def patched_execute(self, signals_df, market_df, bench_df, corr_matrix=None, 
                     corr_threshold=0.70, excluded_tickers=None, enhanced_tickers=None,
                     unified_start=None, min_mature_count=5, performance_start=None,
                     early_exit_days=None):
    
    # 在调用原始方法前，设置日志标志
    self._diag_date = '2026-03-12'
    self._diag_active = True
    
    return original_execute(self, signals_df, market_df, bench_df, corr_matrix,
                           corr_threshold, excluded_tickers, enhanced_tickers,
                           unified_start, min_mature_count, performance_start, early_exit_days)

BacktestEngine._execute_backtest = patched_execute

# 再 patch 买入逻辑的核心部分
# 找到 backtest.py 中买入逻辑的位置，在那里插入诊断打印

import backtest as bt_module
import inspect
source = inspect.getsource(bt_module.BacktestEngine._execute_backtest)

# 由于无法直接修改源码，我们使用另一种方法：
# 在运行后，从交易记录和 NAV 数据反推

print("=" * 80)
print("B0 逐订单诊断 - 2026-03-12")
print("=" * 80)

# ============================================================
# 1. 运行 B0 回测
# ============================================================
DATA_END = pd.Timestamp('2026-06-05')
WARMUP_END = pd.Timestamp('2019-08-13')

ALL_CORE_TICKERS = list(config.ETF_UNIVERSE.keys())
ALL_DEFENSE_TICKERS = list(config.DEFENSE_UNIVERSE.keys())
DEFENSE_TICKERS_SET = set(ALL_DEFENSE_TICKERS)
CORE_TICKERS_SET = set(ALL_CORE_TICKERS)

ETF_NAME_MAP = {**config.ETF_UNIVERSE, **config.DEFENSE_UNIVERSE}

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

print(f"B0: {len(nav_df)} days, {len(trades_df)} trades")

# ============================================================
# 2. 诊断 2026-03-12
# ============================================================
target_date = '2026-03-12'

day_nav = nav_df[nav_df['date'] == target_date]
if len(day_nav) == 0:
    print(f"ERROR: {target_date} not found in nav_df")
    sys.exit(1)

row = day_nav.iloc[0]
nav = row['nav']
cash = row['cash']
pos_value = row['positions_value']
positions_detail = row['positions_detail'] or {}
max_total_pos = row['max_total_position']
regime_id = row.get('regime_id', 'N/A')

print(f"\n{'='*80}")
print(f"诊断日期: {target_date}")
print(f"{'='*80}")
print(f"NAV: {nav:,.2f}")
print(f"Cash: {cash:,.2f} ({cash/nav:.2%})")
print(f"Positions Value: {pos_value:,.2f} ({pos_value/nav:.2%})")
print(f"Max Total Position: {max_total_pos}")
print(f"Regime ID: {regime_id}")
print(f"Number of Positions: {len(positions_detail)}")
print()

# 打印持仓明细
print("--- 当日持仓明细 ---")
industry_value = 0
defense_value = 0
for ticker, detail in positions_detail.items():
    mv = detail.get('market_value', 0)
    shares = detail.get('shares', 0)
    price = detail.get('price', 0)
    cost = detail.get('cost', 0)
    name = ETF_NAME_MAP.get(ticker, ticker)
    is_defense = ticker in DEFENSE_TICKERS_SET
    cat = 'DEFENSE' if is_defense else 'CORE'
    print(f"  [{cat}] {ticker} ({name}): shares={shares}, price={price:.3f}, cost={cost:.3f}, mv={mv:,.2f} ({mv/nav:.2%})")
    if is_defense:
        defense_value += mv
    else:
        industry_value += mv

print(f"\n  Industry Total: {industry_value:,.2f} ({industry_value/nav:.2%})")
print(f"  Defense Total: {defense_value:,.2f} ({defense_value/nav:.2%})")
print()

# ============================================================
# 3. 当日交易明细
# ============================================================
day_trades = trades_df[trades_df['date'] == target_date]
print(f"--- 当日交易 ({len(day_trades)} 笔) ---")
for _, t in day_trades.iterrows():
    ticker = t['ticker']
    action = t['action']
    price = t['price']
    shares = t['shares']
    amount = t['amount']
    commission = t['commission']
    reason = t['reason']
    name = ETF_NAME_MAP.get(ticker, ticker)
    print(f"  [{action}] {ticker} ({name}): {shares} shares @ {price:.3f} = {amount:,.2f} (comm={commission:.2f}), reason={reason}")

print()

# ============================================================
# 4. 前一日持仓与价格变化
# ============================================================
prev_date = nav_df[nav_df['date'] < target_date]['date'].max()
prev_row = nav_df[nav_df['date'] == prev_date].iloc[0]
prev_positions = prev_row['positions_detail'] or {}

print(f"--- 前一日 ({prev_date.strftime('%Y-%m-%d')}) 持仓 ---")
prev_industry = 0
prev_defense = 0
for ticker, detail in prev_positions.items():
    mv = detail.get('market_value', 0)
    name = ETF_NAME_MAP.get(ticker, ticker)
    is_defense = ticker in DEFENSE_TICKERS_SET
    cat = 'DEFENSE' if is_defense else 'CORE'
    print(f"  [{cat}] {ticker} ({name}): mv={mv:,.2f} ({mv/prev_row['nav']:.2%})")
    if is_defense:
        prev_defense += mv
    else:
        prev_industry += mv

print(f"\n  Prev Industry: {prev_industry:,.2f} ({prev_industry/prev_row['nav']:.2%})")
print(f"  Prev Defense: {prev_defense:,.2f} ({prev_defense/prev_row['nav']:.2%})")
print()

# ============================================================
# 5. 计算买入逻辑的关键变量
# ============================================================
print(f"{'='*80}")
print("买入逻辑诊断")
print(f"{'='*80}")

# 从交易记录反推买入逻辑
buy_trades = day_trades[day_trades['action'] == 'BUY']

# 获取当日价格
market_day = market_df[market_df['date'] == target_date]

for _, t in buy_trades.iterrows():
    ticker = t['ticker']
    price = t['price']
    shares = t['shares']
    amount = t['amount']
    commission = t['commission']
    
    # 查找当日该ETF的close价格
    m = market_day[market_day['ticker'] == ticker]
    if len(m) > 0:
        close_price = m.iloc[0]['close']
    else:
        close_price = price
    
    # 一手金额（A股ETF通常100股=1手）
    lot_size = 100
    lot_value = lot_size * close_price
    
    # 目标金额（假设为20% NAV）
    target_amount = nav * 0.20
    
    # 计划股数（不限制现金）
    planned_shares_unlimited = int(target_amount / close_price)
    planned_shares_unlimited = (planned_shares_unlimited // lot_size) * lot_size
    
    # 计划股数（限制现金后）
    # 这里需要知道当时的 available_cash，但我们只能从交易记录反推
    
    name = ETF_NAME_MAP.get(ticker, ticker)
    print(f"\n--- {ticker} ({name}) ---")
    print(f"  Close Price: {close_price:.3f}")
    print(f"  1 Lot ({lot_size} shares) = {lot_value:,.2f}")
    print(f"  20% Target Amount = {target_amount:,.2f}")
    print(f"  Planned Shares (unlimited cash) = {planned_shares_unlimited}")
    print(f"  Actual Shares = {shares}")
    print(f"  Actual Amount = {amount:,.2f}")
    print(f"  Commission = {commission:.2f}")
    print(f"  Actual / Target = {amount/target_amount:.2%}")
    
    if shares < planned_shares_unlimited:
        print(f"  >>> UNDER-BOUGHT: planned={planned_shares_unlimited}, actual={shares}")
    
    if lot_value < 1000:
        print(f"  >>> WARNING: lot value < 1000, but min trade is 1000")

# ============================================================
# 6. 检查 max_position_per_etf 动态调整
# ============================================================
print(f"\n{'='*80}")
print("单只上限确认")
print(f"{'='*80}")
print(f"基础配置 max_position_per_etf: {config.STRATEGY_CONFIG['max_position_per_etf']}")
print(f"当日 regime_id: {regime_id}")
if regime_id and str(int(regime_id)) in config.MARKET_REGIME_CONFIG.get('states', {}):
    state_cfg = config.MARKET_REGIME_CONFIG['states'][str(int(regime_id))]
    print(f"State {int(regime_id)} max_position_per_etf: {state_cfg.get('max_position_per_etf')}")
else:
    print(f"No state-specific override for regime {regime_id}")

print(f"\n{'='*80}")
print("诊断完成")
print(f"{'='*80}")
