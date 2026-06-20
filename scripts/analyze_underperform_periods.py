#!/usr/bin/env python3
"""
全历史连续跑输区间分析
找到所有类似2021年1-2月的连续跑输事件，寻找共性规律
"""
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
    
    # 计算日超额
    nav_df['strat_ret'] = nav_df['nav'].pct_change()
    nav_df['bench_ret'] = nav_df['bench_price'].pct_change()
    nav_df['excess'] = nav_df['strat_ret'] - nav_df['bench_ret']
    nav_df['underperform'] = nav_df['excess'] < 0
    
    # 找所有连续跑输区间（>=3天）
    consecutive = []
    current_start = None
    current_count = 0
    
    for i, row in nav_df.iterrows():
        if row['underperform']:
            if current_start is None:
                current_start = row['date']
            current_count += 1
        else:
            if current_count >= 3:
                consecutive.append((current_start, row['date'], current_count))
            current_start = None
            current_count = 0
    
    # 处理尾部
    if current_count >= 3:
        consecutive.append((current_start, nav_df['date'].iloc[-1], current_count))
    
    # 合并相邻的短区间（间隔<3天）
    merged = []
    for start, end, count in consecutive:
        if merged:
            last_start, last_end, last_count = merged[-1]
            gap = (start - last_end).days
            if gap <= 3:
                merged[-1] = (last_start, end, last_count + count)
                continue
        merged.append((start, end, count))
    
    print("=" * 90)
    print("全历史连续跑输区间分析")
    print("=" * 90)
    
    print(f"\n{'排名':<4} {'开始日期':<12} {'结束日期':<12} {'天数':>5} {'区间超额':>10} {'区间策略':>10} {'区间基准':>10} {'最大回撤':>10} {'空仓占比':>8}")
    print("-" * 95)
    
    interval_stats = []
    for start, end, count in merged:
        mask = (nav_df['date'] >= start) & (nav_df['date'] <= end)
        period = nav_df[mask]
        if len(period) < 2:
            continue
        
        nav_s = period['nav'].iloc[0]
        nav_e = period['nav'].iloc[-1]
        bench_s = period['bench_price'].iloc[0]
        bench_e = period['bench_price'].iloc[-1]
        
        strat_ret = (nav_e / nav_s) - 1
        bench_ret = (bench_e / bench_s) - 1
        excess = strat_ret - bench_ret
        
        # 最大回撤
        nav_vals = period['nav'].values
        max_dd = 0
        peak = nav_vals[0]
        for v in nav_vals:
            if v > peak:
                peak = v
            dd = (v - peak) / peak
            if dd < max_dd:
                max_dd = dd
        
        # 空仓占比
        empty_ratio = (period['num_positions'] == 0).mean()
        
        interval_stats.append({
            'start': start, 'end': end, 'days': count,
            'strat_ret': strat_ret, 'bench_ret': bench_ret,
            'excess': excess, 'max_dd': max_dd,
            'empty_ratio': empty_ratio,
        })
    
    # 按区间超额排序（最差的在前面）
    interval_stats.sort(key=lambda x: x['excess'])
    
    for i, s in enumerate(interval_stats[:15]):
        print(f"{i+1:<4} {s['start'].strftime('%Y-%m-%d'):<12} {s['end'].strftime('%Y-%m-%d'):<12} "
              f"{s['days']:>5} {s['excess']:>+10.2%} {s['strat_ret']:>+10.2%} {s['bench_ret']:>+10.2%} "
              f"{s['max_dd']:>+10.2%} {s['empty_ratio']:>8.1%}")
    
    # 按年份统计跑输天数和累计超额
    print("\n" + "=" * 90)
    print("按年度跑输统计")
    print("=" * 90)
    
    nav_df['year'] = nav_df['date'].dt.year
    years = sorted(nav_df['year'].unique())
    
    print(f"\n{'年份':<8} {'总交易日':>8} {'跑输天数':>8} {'跑输占比':>10} {'累计超额':>10} {'空仓占比':>10}")
    print("-" * 60)
    
    for year in years:
        year_data = nav_df[nav_df['year'] == year]
        total_days = len(year_data)
        under_days = year_data['underperform'].sum()
        empty_ratio = (year_data['num_positions'] == 0).mean()
        
        nav_s = year_data['nav'].iloc[0]
        nav_e = year_data['nav'].iloc[-1]
        bench_s = year_data['bench_price'].iloc[0]
        bench_e = year_data['bench_price'].iloc[-1]
        excess = (nav_e/nav_s - 1) - (bench_e/bench_s - 1)
        
        print(f"{year:<8} {total_days:>8} {under_days:>8} {under_days/total_days:>10.1%} {excess:>+10.2%} {empty_ratio:>10.1%}")
    
    # 找大节日前后的跑输模式（春节、国庆等）
    print("\n" + "=" * 90)
    print("特殊时段分析（春节/国庆前后）")
    print("=" * 90)
    
    # 春节假期前后（通常1月下旬到2月中旬）
    spring_years = [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    for year in spring_years:
        # 春节前10天到春节后20天（简化：1月20日到2月28日）
        mask = (nav_df['date'] >= f'{year}-01-20') & (nav_df['date'] <= f'{year}-02-28')
        period = nav_df[mask]
        if len(period) < 2:
            continue
        nav_s = period['nav'].iloc[0]
        nav_e = period['nav'].iloc[-1]
        bench_s = period['bench_price'].iloc[0]
        bench_e = period['bench_price'].iloc[-1]
        excess = (nav_e/nav_s - 1) - (bench_e/bench_s - 1)
        under_days = period['underperform'].sum()
        total_days = len(period)
        print(f"{year}春节前后(1.20-2.28): 超额{excess:+.2%}, 跑输{under_days}/{total_days}天")
    
    # 国庆假期前后
    print()
    for year in spring_years:
        mask = (nav_df['date'] >= f'{year}-09-25') & (nav_df['date'] <= f'{year}-10-31')
        period = nav_df[mask]
        if len(period) < 2:
            continue
        nav_s = period['nav'].iloc[0]
        nav_e = period['nav'].iloc[-1]
        bench_s = period['bench_price'].iloc[0]
        bench_e = period['bench_price'].iloc[-1]
        excess = (nav_e/nav_s - 1) - (bench_e/bench_s - 1)
        under_days = period['underperform'].sum()
        total_days = len(period)
        print(f"{year}国庆前后(9.25-10.31): 超额{excess:+.2%}, 跑输{under_days}/{total_days}天")

if __name__ == '__main__':
    main()
