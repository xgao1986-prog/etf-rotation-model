#!/usr/bin/env python3
"""
Phase 5.1：调仓星期稳健性诊断（修正版）
- 分年度及区间分析周一至周五调仓效果
- 各星期排名统计：平均排名、中位排名、前二占比、年度胜率
- 区分"绝对收益优势"与"排名稳定性"
- 调仓日期差异分析
- 节假日顺延检查
- 审慎判断星期四优势

约束：不修改生产代码，纯分析脚本
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from datetime import datetime

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'
WEEKDAY_NAMES = {0: '周一', 1: '周二', 2: '周三', 3: '周四', 4: '周五'}


def run_weekday_backtest(weekday):
    """运行指定调仓日的回测"""
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['rebalance_weekday'] = weekday
    
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)
    return result


def extract_period_stats(nav_df, start_date, end_date):
    """提取指定区间的绩效统计"""
    mask = (nav_df['date'] >= start_date) & (nav_df['date'] <= end_date)
    period = nav_df[mask]
    if len(period) < 2:
        return None
    
    nav_start = period['nav'].iloc[0]
    nav_end = period['nav'].iloc[-1]
    total_return = (nav_end / nav_start) - 1
    
    days = len(period)
    years = days / 252
    annual_return = (nav_end / nav_start) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0
    
    daily_ret = period['nav'].pct_change().dropna()
    volatility = daily_ret.std() * np.sqrt(252)
    sharpe = annual_return / volatility if volatility > 0 else 0
    
    period['peak'] = period['nav'].cummax()
    period['drawdown'] = (period['nav'] - period['peak']) / period['peak']
    max_drawdown = period['drawdown'].min()
    
    return {
        'total_return': total_return,
        'annual_return': annual_return,
        'sharpe': sharpe,
        'max_drawdown': max_drawdown,
    }


def compute_rankings(df, group_col, value_col='total_return'):
    """计算每个星期在各组中的排名统计"""
    rankings = {wd_name: [] for wd_name in WEEKDAY_NAMES.values()}
    
    for group in df[group_col].unique():
        group_data = df[df[group_col] == group]
        if len(group_data) < 5:
            continue
        sorted_data = group_data.sort_values(value_col, ascending=False).reset_index(drop=True)
        for rank, row in sorted_data.iterrows():
            wd = row['weekday']
            rankings[wd].append(rank + 1)
    
    stats = {}
    for wd_name, ranks in rankings.items():
        if not ranks:
            continue
        arr = np.array(ranks)
        # 年度胜率：正收益年份占比
        pos_years = df[(df['weekday'] == wd_name) & (df[value_col] > 0)]
        total_years = df[df['weekday'] == wd_name]
        win_rate = len(pos_years) / len(total_years) if len(total_years) > 0 else 0
        
        stats[wd_name] = {
            'avg_rank': arr.mean(),
            'median_rank': np.median(arr),
            'top2_rate': (arr <= 2).mean(),
            'win_rate': win_rate,
            'count': len(arr),
        }
    
    return stats


def check_holiday_shift(rebalance_dates):
    """检查节假日导致的调仓顺延"""
    holidays = [
        '2020-01-24', '2020-01-27', '2020-01-28', '2020-01-29', '2020-01-30',
        '2021-02-11', '2021-02-12', '2021-02-15', '2021-02-16', '2021-02-17',
        '2022-01-31', '2022-02-01', '2022-02-02', '2022-02-03', '2022-02-04',
        '2023-01-23', '2023-01-24', '2023-01-25', '2023-01-26', '2023-01-27',
        '2024-02-09', '2024-02-12', '2024-02-13', '2024-02-14', '2024-02-15',
        '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31', '2025-02-03',
        '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
        '2020-10-01', '2020-10-02', '2020-10-05', '2020-10-06', '2020-10-07', '2020-10-08',
        '2021-10-01', '2021-10-04', '2021-10-05', '2021-10-06', '2021-10-07',
        '2022-10-03', '2022-10-04', '2022-10-05', '2022-10-06', '2022-10-07',
        '2023-10-02', '2023-10-03', '2023-10-04', '2023-10-05', '2023-10-06',
        '2024-10-01', '2024-10-02', '2024-10-03', '2024-10-04', '2024-10-07',
        '2025-10-01', '2025-10-02', '2025-10-03', '2025-10-06', '2025-10-07', '2025-10-08',
        '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08',
        '2020-05-01', '2020-05-04', '2020-05-05',
        '2021-05-01', '2021-05-03', '2021-05-04', '2021-05-05',
        '2022-05-02', '2022-05-03', '2022-05-04',
        '2023-05-01', '2023-05-02', '2023-05-03',
        '2024-05-01', '2024-05-02', '2024-05-03',
        '2025-05-01', '2025-05-02', '2025-05-05',
        '2026-05-01', '2026-05-04', '2026-05-05',
    ]
    
    holiday_shifts = []
    for rd in rebalance_dates:
        rd_dt = pd.to_datetime(rd)
        rd_str = rd_dt.strftime('%Y-%m-%d')
        for h in holidays:
            h_dt = pd.to_datetime(h)
            diff = (rd_dt - h_dt).days
            if diff in [0, 1, -1]:
                holiday_shifts.append({
                    'rebalance_date': rd_str,
                    'holiday': h,
                    'diff_days': diff,
                })
    
    return holiday_shifts


def main():
    print("=" * 70)
    print("Phase 5.1: 调仓星期稳健性诊断（修正版）")
    print("=" * 70)
    
    # 收集全区间结果
    all_results = {}
    
    for wd in range(5):
        print(f"\n[{WEEKDAY_NAMES[wd]}] 回测中...")
        result = run_weekday_backtest(wd)
        all_results[wd] = result
        
        print(f"  全区间总收益: {result['total_return']:.2%}")
        print(f"  年化: {result['annual_return']:.2%}")
        print(f"  夏普: {result['sharpe_ratio']:.3f}")
        print(f"  最大回撤: {result['max_drawdown']:.2%}")
        print(f"  交易次数: {result['num_trades']}")
        print(f"  总佣金: {result['total_commission']:,.2f}")
        print(f"  调仓次数: {result['rebalance_count']}")
    
    # 分年度统计
    print("\n" + "=" * 70)
    print("分年度统计（各调仓日）")
    print("=" * 70)
    
    years = [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
    
    annual_rows = []
    for year in years:
        for wd in range(5):
            result = all_results[wd]
            nav_df = result['nav_df']
            start = f'{year}-01-01'
            end = f'{year}-12-31'
            stats = extract_period_stats(nav_df, start, end)
            if stats:
                annual_rows.append({
                    'year': year,
                    'weekday': WEEKDAY_NAMES[wd],
                    **stats,
                })
    
    annual_df = pd.DataFrame(annual_rows)
    
    # 打印年度收益表
    if not annual_df.empty:
        print(f"\n{'年份':<6} {'周一':>10} {'周二':>10} {'周三':>10} {'周四':>10} {'周五':>10} {'周四排名':>8}")
        print("-" * 70)
        for year in years:
            year_data = annual_df[annual_df['year'] == year]
            if len(year_data) < 5:
                continue
            returns = {r['weekday']: r['total_return'] for _, r in year_data.iterrows()}
            thu_ret = returns.get('周四', 0)
            sorted_rets = sorted(returns.values(), reverse=True)
            rank = sorted_rets.index(thu_ret) + 1 if thu_ret in sorted_rets else '-'
            print(f"{year:<6} {returns.get('周一',0):>+10.2%} {returns.get('周二',0):>+10.2%} "
                  f"{returns.get('周三',0):>+10.2%} {returns.get('周四',0):>+10.2%} "
                  f"{returns.get('周五',0):>+10.2%} {rank:>8}")
    
    # 年度排名统计
    print("\n" + "=" * 70)
    print("年度排名统计")
    print("=" * 70)
    
    annual_rank_stats = compute_rankings(annual_df, 'year')
    
    print(f"\n{'星期':<6} {'平均排名':>8} {'中位排名':>8} {'前二占比':>10} {'年度胜率':>8} {'样本数':>6}")
    print("-" * 55)
    for wd_name in ['周一', '周二', '周三', '周四', '周五']:
        if wd_name in annual_rank_stats:
            s = annual_rank_stats[wd_name]
            print(f"{wd_name:<6} {s['avg_rank']:>8.2f} {s['median_rank']:>8.1f} "
                  f"{s['top2_rate']:>10.1%} {s['win_rate']:>8.1%} {s['count']:>6}")
    
    # 分区间统计
    print("\n" + "=" * 70)
    print("分区间统计（各调仓日）")
    print("=" * 70)
    
    periods = [
        ('2020-2021', '2020-01-01', '2021-12-31'),
        ('2022-2023', '2022-01-01', '2023-12-31'),
        ('2024-2026', '2024-01-01', '2026-06-18'),
    ]
    
    period_rows = []
    for name, start, end in periods:
        for wd in range(5):
            result = all_results[wd]
            nav_df = result['nav_df']
            stats = extract_period_stats(nav_df, start, end)
            if stats:
                period_rows.append({
                    'period': name,
                    'weekday': WEEKDAY_NAMES[wd],
                    **stats,
                })
    
    period_df = pd.DataFrame(period_rows)
    
    if not period_df.empty:
        print(f"\n{'区间':<10} {'周一':>10} {'周二':>10} {'周三':>10} {'周四':>10} {'周五':>10} {'周四排名':>8}")
        print("-" * 70)
        for name, _, _ in periods:
            p_data = period_df[period_df['period'] == name]
            if len(p_data) < 5:
                continue
            returns = {r['weekday']: r['total_return'] for _, r in p_data.iterrows()}
            thu_ret = returns.get('周四', 0)
            sorted_rets = sorted(returns.values(), reverse=True)
            rank = sorted_rets.index(thu_ret) + 1 if thu_ret in sorted_rets else '-'
            print(f"{name:<10} {returns.get('周一',0):>+10.2%} {returns.get('周二',0):>+10.2%} "
                  f"{returns.get('周三',0):>+10.2%} {returns.get('周四',0):>+10.2%} "
                  f"{returns.get('周五',0):>+10.2%} {rank:>8}")
    
    # 区间排名统计
    print("\n" + "=" * 70)
    print("区间排名统计")
    print("=" * 70)
    
    period_rank_stats = compute_rankings(period_df, 'period')
    
    print(f"\n{'星期':<6} {'平均排名':>8} {'中位排名':>8} {'前二占比':>10} {'区间胜率':>8} {'样本数':>6}")
    print("-" * 55)
    for wd_name in ['周一', '周二', '周三', '周四', '周五']:
        if wd_name in period_rank_stats:
            s = period_rank_stats[wd_name]
            print(f"{wd_name:<6} {s['avg_rank']:>8.2f} {s['median_rank']:>8.1f} "
                  f"{s['top2_rate']:>10.1%} {s['win_rate']:>8.1%} {s['count']:>6}")
    
    # 调仓日期差异分析
    print("\n" + "=" * 70)
    print("调仓日期差异分析")
    print("=" * 70)
    
    rebalance_dates_map = {}
    for wd in range(5):
        rebalance_dates_map[WEEKDAY_NAMES[wd]] = all_results[wd].get('rebalance_dates', [])
    
    thu_dates = set(rebalance_dates_map.get('周四', []))
    for wd_name in ['周一', '周二', '周三', '周五']:
        other_dates = set(rebalance_dates_map.get(wd_name, []))
        common = thu_dates & other_dates
        thu_only = thu_dates - other_dates
        other_only = other_dates - thu_dates
        print(f"\n周四 vs {wd_name}:")
        print(f"  共同调仓日: {len(common)} 次")
        print(f"  周四独有: {len(thu_only)} 次")
        print(f"  {wd_name}独有: {len(other_only)} 次")
    
    # 节假日顺延检查
    print("\n" + "=" * 70)
    print("节假日顺延检查")
    print("=" * 70)
    
    for wd in range(5):
        wd_name = WEEKDAY_NAMES[wd]
        dates = rebalance_dates_map.get(wd_name, [])
        shifts = check_holiday_shift(dates)
        if shifts:
            print(f"\n{wd_name}: 发现 {len(shifts)} 次节假日附近调仓")
            for s in shifts[:5]:
                print(f"  调仓日 {s['rebalance_date']} 紧邻节假日 {s['holiday']} (差{s['diff_days']}天)")
        else:
            print(f"\n{wd_name}: 未发现节假日附近调仓")
    
    # 审慎判断
    print("\n" + "=" * 70)
    print("审慎判断：星期四优势分析")
    print("=" * 70)
    
    # 绝对收益优势
    thu_full_return = all_results[3]['total_return']  # 周四全区间
    fri_full_return = all_results[4]['total_return']  # 周五全区间
    
    # 排名稳定性
    thu_avg_rank_annual = annual_rank_stats.get('周四', {}).get('avg_rank', 0)
    thu_median_rank_annual = annual_rank_stats.get('周四', {}).get('median_rank', 0)
    thu_top2_rate = annual_rank_stats.get('周四', {}).get('top2_rate', 0)
    thu_win_rate = annual_rank_stats.get('周四', {}).get('win_rate', 0)
    
    print(f"\n一、绝对收益优势（全区间）")
    print(f"  周四: {thu_full_return:.2%}")
    print(f"  周五: {fri_full_return:.2%}")
    print(f"  周三: {all_results[2]['total_return']:.2%}")
    print(f"  周二: {all_results[1]['total_return']:.2%}")
    print(f"  周一: {all_results[0]['total_return']:.2%}")
    print(f"  周四领先第二名: {thu_full_return - fri_full_return:.2%}")
    
    print(f"\n二、排名稳定性（年度）")
    print(f"  平均排名: {thu_avg_rank_annual:.2f} (1=最优, 5=最差)")
    print(f"  中位排名: {thu_median_rank_annual:.1f}")
    print(f"  前二占比: {thu_top2_rate:.1%}")
    print(f"  年度胜率: {thu_win_rate:.1%} (正收益年份占比)")
    
    # 审慎结论
    print(f"\n三、审慎结论")
    if thu_avg_rank_annual <= 2.5 and thu_top2_rate >= 0.5:
        print(f"  排名稳定性：周四平均排名 {thu_avg_rank_annual:.2f}，前二占比 {thu_top2_rate:.1%}，")
        print(f"  说明周四在多数年份中表现靠前，但非绝对最优。")
    else:
        print(f"  排名稳定性：周四平均排名 {thu_avg_rank_annual:.2f}，前二占比 {thu_top2_rate:.1%}，")
        print(f"  说明周四排名波动较大，优势不稳定。")
    
    print(f"\n  综合判断：")
    print(f"  1. 全区间收益：周四显著领先（{thu_full_return:.2%} vs 次优 {fri_full_return:.2%}）")
    print(f"  2. 年度排名：平均 {thu_avg_rank_annual:.2f}，前二占比 {thu_top2_rate:.1%}")
    print(f"  3. 年度胜率：{thu_win_rate:.1%}（8年中有 {int(thu_win_rate*8)} 年正收益）")
    if thu_avg_rank_annual <= 2.0 and thu_top2_rate >= 0.5:
        print(f'  4. 审慎结论：周四具有「绝对收益优势 + 排名稳定性」，但样本仅8年，仍需持续观察。')
    elif thu_avg_rank_annual <= 2.5 and thu_top2_rate >= 0.4:
        print(f'  4. 审慎结论：周四具有「绝对收益优势」，但「排名稳定性」中等，可能受偶然因素影响。')
    else:
        print(f'  4. 审慎结论：周四绝对收益优势明显，但排名稳定性不足，不宜过度解读。')
    
    # 生成报告
    print("\n" + "=" * 70)
    print("生成报告...")
    print("=" * 70)
    
    lines = []
    lines.append('# Phase 5.1 调仓星期稳健性诊断报告（修正版）')
    lines.append('')
    lines.append(f'**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'**数据截止**: {AS_OF_DATE}')
    lines.append('')
    
    lines.append('## 一、全区间回测结果')
    lines.append('')
    lines.append('| 调仓日 | 总收益 | 年化 | 夏普 | 最大回撤 | 交易次数 | 总佣金 |')
    lines.append('|--------|--------|------|------|----------|----------|--------|')
    for wd in range(5):
        r = all_results[wd]
        lines.append(f"| {WEEKDAY_NAMES[wd]} | {r['total_return']:.2%} | {r['annual_return']:.2%} | "
                     f"{r['sharpe_ratio']:.3f} | {r['max_drawdown']:.2%} | {r['num_trades']} | "
                     f"{r['total_commission']:,.0f} |")
    lines.append('')
    
    lines.append('## 二、分年度统计')
    lines.append('')
    lines.append('| 年份 | 周一 | 周二 | 周三 | 周四 | 周五 | 周四排名 |')
    lines.append('|------|------|------|------|------|------|----------|')
    for year in years:
        year_data = annual_df[annual_df['year'] == year]
        if len(year_data) < 5:
            continue
        returns = {r['weekday']: r['total_return'] for _, r in year_data.iterrows()}
        thu_ret = returns.get('周四', 0)
        sorted_rets = sorted(returns.values(), reverse=True)
        rank = sorted_rets.index(thu_ret) + 1 if thu_ret in sorted_rets else '-'
        lines.append(f"| {year} | {returns.get('周一',0):+.2%} | {returns.get('周二',0):+.2%} | "
                     f"{returns.get('周三',0):+.2%} | {returns.get('周四',0):+.2%} | "
                     f"{returns.get('周五',0):+.2%} | {rank} |")
    lines.append('')
    
    lines.append('## 三、年度排名统计')
    lines.append('')
    lines.append('| 星期 | 平均排名 | 中位排名 | 前二占比 | 年度胜率 | 样本数 |')
    lines.append('|------|----------|----------|----------|----------|--------|')
    for wd_name in ['周一', '周二', '周三', '周四', '周五']:
        if wd_name in annual_rank_stats:
            s = annual_rank_stats[wd_name]
            lines.append(f"| {wd_name} | {s['avg_rank']:.2f} | {s['median_rank']:.1f} | "
                         f"{s['top2_rate']:.1%} | {s['win_rate']:.1%} | {s['count']} |")
    lines.append('')
    
    lines.append('## 四、分区间统计')
    lines.append('')
    lines.append('| 区间 | 周一 | 周二 | 周三 | 周四 | 周五 | 周四排名 |')
    lines.append('|------|------|------|------|------|------|----------|')
    for name, _, _ in periods:
        p_data = period_df[period_df['period'] == name]
        if len(p_data) < 5:
            continue
        returns = {r['weekday']: r['total_return'] for _, r in p_data.iterrows()}
        thu_ret = returns.get('周四', 0)
        sorted_rets = sorted(returns.values(), reverse=True)
        rank = sorted_rets.index(thu_ret) + 1 if thu_ret in sorted_rets else '-'
        lines.append(f"| {name} | {returns.get('周一',0):+.2%} | {returns.get('周二',0):+.2%} | "
                     f"{returns.get('周三',0):+.2%} | {returns.get('周四',0):+.2%} | "
                     f"{returns.get('周五',0):+.2%} | {rank} |")
    lines.append('')
    
    lines.append('## 五、区间排名统计')
    lines.append('')
    lines.append('| 星期 | 平均排名 | 中位排名 | 前二占比 | 区间胜率 | 样本数 |')
    lines.append('|------|----------|----------|----------|----------|--------|')
    for wd_name in ['周一', '周二', '周三', '周四', '周五']:
        if wd_name in period_rank_stats:
            s = period_rank_stats[wd_name]
            lines.append(f"| {wd_name} | {s['avg_rank']:.2f} | {s['median_rank']:.1f} | "
                         f"{s['top2_rate']:.1%} | {s['win_rate']:.1%} | {s['count']} |")
    lines.append('')
    
    lines.append('## 六、调仓日期差异分析')
    lines.append('')
    lines.append('| 对比 | 共同调仓日 | 周四独有 | 对比日独有 |')
    lines.append('|------|------------|----------|------------|')
    for wd_name in ['周一', '周二', '周三', '周五']:
        thu_dates = set(rebalance_dates_map.get('周四', []))
        other_dates = set(rebalance_dates_map.get(wd_name, []))
        common = thu_dates & other_dates
        thu_only = thu_dates - other_dates
        other_only = other_dates - thu_dates
        lines.append(f"| 周四 vs {wd_name} | {len(common)} | {len(thu_only)} | {len(other_only)} |")
    lines.append('')
    
    lines.append('## 七、审慎结论')
    lines.append('')
    lines.append('### 一、绝对收益优势（全区间）')
    lines.append('')
    lines.append(f"| 调仓日 | 总收益 | 领先第二名 |")
    lines.append(f"|--------|--------|------------|")
    lines.append(f"| 周四 | {thu_full_return:.2%} | — |")
    lines.append(f"| 周五 | {fri_full_return:.2%} | {thu_full_return - fri_full_return:.2%} |")
    lines.append(f"| 周三 | {all_results[2]['total_return']:.2%} | {thu_full_return - all_results[2]['total_return']:.2%} |")
    lines.append(f"| 周二 | {all_results[1]['total_return']:.2%} | {thu_full_return - all_results[1]['total_return']:.2%} |")
    lines.append(f"| 周一 | {all_results[0]['total_return']:.2%} | {thu_full_return - all_results[0]['total_return']:.2%} |")
    lines.append('')
    
    lines.append('### 二、排名稳定性（年度）')
    lines.append('')
    lines.append(f"| 指标 | 数值 | 解释 |")
    lines.append(f"|------|------|------|")
    lines.append(f"| 平均排名 | {thu_avg_rank_annual:.2f} | 1=最优, 5=最差 |")
    lines.append(f"| 中位排名 | {thu_median_rank_annual:.1f} | 半数年份排名在此之上/之下 |")
    lines.append(f"| 前二占比 | {thu_top2_rate:.1%} | 排名进入前二的年份占比 |")
    lines.append(f"| 年度胜率 | {thu_win_rate:.1%} | 正收益年份占比 |")
    lines.append('')
    
    lines.append('### 三、综合判断')
    lines.append('')
    lines.append(f"1. **绝对收益优势**：周四全区间收益 {thu_full_return:.2%}，显著领先第二名（周五 {fri_full_return:.2%}，差距 {thu_full_return - fri_full_return:.2%}）。")
    lines.append(f"2. **排名稳定性**：平均排名 {thu_avg_rank_annual:.2f}，前二占比 {thu_top2_rate:.1%}，年度胜率 {thu_win_rate:.1%}。")
    if thu_avg_rank_annual <= 2.0 and thu_top2_rate >= 0.5:
        lines.append(f'3. **审慎结论**：周四同时具备「绝对收益优势」和「排名稳定性」（平均排名≤2.0且前二占比≥50%）。但样本仅8年，仍需持续观察。')
    elif thu_avg_rank_annual <= 2.5 and thu_top2_rate >= 0.4:
        lines.append(f'3. **审慎结论**：周四具有「绝对收益优势」，但「排名稳定性」中等（平均排名{thu_avg_rank_annual:.2f}，前二占比{thu_top2_rate:.1%}）。可能受偶然因素影响，不宜过度解读。')
    else:
        lines.append(f'3. **审慎结论**：周四绝对收益优势明显，但排名稳定性不足。不宜将周四作为「最优调仓日」进行策略化。')
    lines.append('')
    
    report_path = 'D:/etf_rotation_model/reports/phase5_weekday_robustness.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n报告已保存: {report_path}")
    print(f"\n{'='*70}")
    print("Phase 5.1 修正版完成")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
