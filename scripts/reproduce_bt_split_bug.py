# -*- coding: utf-8 -*-
"""复现：BacktestEngine 未同步拆分持仓，512480 在 2026-07-03 错误止损。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import pandas as pd
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, build_config
from database import ETFDatabase
from backtest import BacktestEngine

db = ETFDatabase()
tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
START, END = '2025-11-01', '2026-07-20'
market_df = db.get_market_data(ticker=tickers, start_date=START, end_date=END)
bench_df = db.get_market_data(ticker=BENCHMARK, start_date=START, end_date=END)
print(f"market rows: {len(market_df)}, range {market_df['date'].min()} ~ {market_df['date'].max()}")

cfg = build_config()
cfg['market_timing'] = False
cfg['fallback_equity_enabled'] = False
cfg['momentum_factor_enabled'] = False
cfg['volatility_factor_enabled'] = False

engine = BacktestEngine(cfg)
result = engine.run(market_df, bench_df)

nav = result['nav_df'].copy()
nav['date'] = pd.to_datetime(nav['date']).dt.strftime('%Y-%m-%d')
nav['daily_ret'] = nav['nav'].pct_change()

trades = result['trades_df'].copy()
trades['date'] = pd.to_datetime(trades['date']).dt.strftime('%Y-%m-%d')

# 512480 价格序列（真实价）
px = market_df[market_df['ticker'] == '512480.SH'].copy()
px['date'] = pd.to_datetime(px['date']).dt.strftime('%Y-%m-%d')
print("\n512480 真实价 6/29~7/6:")
print(px[px['date'].between('2026-06-29', '2026-07-06')][['date', 'open', 'close']].to_string(index=False))

print("\n2026-07 上旬止损交易：")
sl = trades[(trades['date'] >= '2026-07-01') & (trades['date'] <= '2026-07-10')
            & (trades['action'] == 'STOP_LOSS')]
print(sl[['date', 'ticker', 'action', 'price', 'reason']].to_string(index=False) if len(sl) else "（无）")

print("\n2026-07-03 前后 NAV：")
w = nav[nav['date'].between('2026-07-01', '2026-07-07')][['date', 'nav', 'daily_ret']]
print(w.to_string(index=False))

big_drop = nav[nav['daily_ret'] < -0.05][['date', 'nav', 'daily_ret']]
print("\n单日跌幅超过 -5% 的日期：")
print(big_drop.to_string(index=False))
