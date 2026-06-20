#!/usr/bin/env python3
"""2021年逐月表现分析 - 找到真正跑输的子区间"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

def main():
    db = ETFDatabase()
    b0_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=b0_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    
    nav_df = result['nav_df'].copy()
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    nav_df = nav_df[(nav_df['date'] >= '2021-01-01') & (nav_df['date'] <= '2021-12-31')]
    
    print("=" * 80)
    print("2021年逐月表现分析")
    print("=" * 80)
    
    # 按月统计
    months = ['2021-01', '2021-02', '2021-03', '2021-04', '2021-05', '2021-06', 
              '2021-07', '2021-08', '2021-09', '2021-10', '2021-11', '2021-12']
    
    print(f"\n{'月份':<10} {'策略收益':>10} {'基准收益':>10} {'超额':>10} {'持仓天数':>8} {'空仓天数':>8}")
    print("-" * 60)
    
    worst_month = None
    worst_excess = float('inf')
    
    for month in months:
        month_data = nav_df[nav_df['date'].dt.strftime('%Y-%m') == month]
        if len(month_data) < 2:
            continue
        
        nav_start = month_data['nav'].iloc[0]
        nav_end = month_data['nav'].iloc[-1]
        strat_ret = (nav_end / nav_start) - 1
        
        bench_start = month_data['bench_price'].iloc[0]
        bench_end = month_data['bench_price'].iloc[-1]
        bench_ret = (bench_end / bench_start) - 1
        
        excess = strat_ret - bench_ret
        pos_days = (month_data['num_positions'] > 0).sum()
        empty_days = (month_data['num_positions'] == 0).sum()
        
        marker = " <<< 跑输" if excess < 0 else ""
        print(f"{month:<10} {strat_ret:>+10.2%} {bench_ret:>+10.2%} {excess:>+10.2%} {pos_days:>8} {empty_days:>8}{marker}")
        
        if excess < worst_excess:
            worst_excess = excess
            worst_month = month
    
    # 全2021年
    if len(nav_df) >= 2:
        nav_start = nav_df['nav'].iloc[0]
        nav_end = nav_df['nav'].iloc[-1]
        strat_ret = (nav_end / nav_start) - 1
        
        bench_start = nav_df['bench_price'].iloc[0]
        bench_end = nav_df['bench_price'].iloc[-1]
        bench_ret = (bench_end / bench_start) - 1
        
        print("-" * 60)
        print(f"{'2021全年':<10} {strat_ret:>+10.2%} {bench_ret:>+10.2%} {strat_ret-bench_ret:>+10.2%}")
    
    print(f"\n最差月份: {worst_month} (超额 {worst_excess:.2%})")
    
    # 找到连续跑输的最长区间
    print("\n" + "=" * 80)
    print("连续跑输区间分析")
    print("=" * 80)
    
    nav_df['strat_ret'] = nav_df['nav'].pct_change()
    nav_df['bench_ret'] = nav_df['bench_price'].pct_change()
    nav_df['excess'] = nav_df['strat_ret'] - nav_df['bench_ret']
    nav_df['underperform'] = nav_df['excess'] < 0
    
    # 找连续跑输的区间
    consecutive = []
    current_start = None
    current_count = 0
    
    for i, row in nav_df.iterrows():
        if row['underperform']:
            if current_start is None:
                current_start = row['date']
            current_count += 1
        else:
            if current_count > 0:
                consecutive.append((current_start, row['date'], current_count))
            current_start = None
            current_count = 0
    
    if current_count > 0:
        consecutive.append((current_start, nav_df['date'].iloc[-1], current_count))
    
    consecutive.sort(key=lambda x: x[2], reverse=True)
    
    print(f"\n{'开始日期':<12} {'结束日期':<12} {'连续天数':>8} {'区间超额':>10}")
    print("-" * 50)
    for start, end, count in consecutive[:5]:
        mask = (nav_df['date'] >= start) & (nav_df['date'] <= end)
        period = nav_df[mask]
        if len(period) >= 2:
            nav_s = period['nav'].iloc[0]
            nav_e = period['nav'].iloc[-1]
            bench_s = period['bench_price'].iloc[0]
            bench_e = period['bench_price'].iloc[-1]
            period_excess = (nav_e/nav_s - 1) - (bench_e/bench_s - 1)
            print(f"{start.strftime('%Y-%m-%d'):<12} {end.strftime('%Y-%m-%d'):<12} {count:>8} {period_excess:>+10.2%}")

if __name__ == '__main__':
    main()
