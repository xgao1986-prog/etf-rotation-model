#!/usr/bin/env python3
"""
Phase 5.1：调仓星期稳健性诊断
- 分年度及区间分析周一至周五调仓效果
- 滑点敏感性（0, 5, 10, 20bp）
- 调仓日期差异分析
- 节假日顺延检查
- 判断星期四优势是否跨阶段稳定

约束：不修改生产代码，纯分析脚本
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from copy import deepcopy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'
WEEKDAY_NAMES = {0: '周一', 1: '周二', 2: '周三', 3: '周四', 4: '周五'}


def run_weekday_backtest(weekday, slippage_bp=0):
    """运行指定调仓日和滑点的回测"""
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['rebalance_weekday'] = weekday
    cfg['slippage'] = slippage_bp / 10000  # bp 转小数
    
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


def check_holiday_shift(nav_df, rebalance_dates):
    """检查节假日导致的调仓顺延"""
    # 中国主要节假日（2020-2026）
    holidays = [
        # 春节
        '2020-01-24', '2020-01-27', '2020-01-28', '2020-01-29', '2020-01-30',
        '2021-02-11', '2021-02-12', '2021-02-15', '2021-02-16', '2021-02-17',
        '2022-01-31', '2022-02-01', '2022-02-02', '2022-02-03', '2022-02-04',
        '2023-01-23', '2023-01-24', '2023-01-25', '2023-01-26', '2023-01-27',
        '2024-02-09', '2024-02-12', '2024-02-13', '2024-02-14', '2024-02-15',
        '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31', '2025-02-03',
        '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
        # 国庆
        '2020-10-01', '2020-10-02', '2020-10-05', '2020-10-06', '2020-10-07', '2020-10-08',
        '2021-10-01', '2021-10-04', '2021-10-05', '2021-10-06', '2021-10-07',
        '2022-10-03', '2022-10-04', '2022-10-05', '2022-10-06', '2022-10-07',
        '2023-10-02', '2023-10-03', '2023-10-04', '2023-10-05', '2023-10-06',
        '2024-10-01', '2024-10-02', '2024-10-03', '2024-10-04', '2024-10-07',
        '2025-10-01', '2025-10-02', '2025-10-03', '2025-10-06', '2025-10-07', '2025-10-08',
        '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08',
        # 劳动节
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
        # 查找该调仓日前后的节假日
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
    print("Phase 5.1: 调仓星期稳健性诊断")
    print("=" * 70)
    
    # 配置
    slippage_list = [0, 5, 10, 20]
    
    # 收集全区间结果
    all_results = {}
    
    for wd in range(5):
        print(f"\n[{WEEKDAY_NAMES[wd]}] 回测中...")
        result = run_weekday_backtest(wd, slippage_bp=0)
        all_results[wd] = result
        
        nav_df = result['nav_df']
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
                    'total_return': stats['total_return'],
                    'annual_return': stats['annual_return'],
                    'sharpe': stats['sharpe'],
                    'max_drawdown': stats['max_drawdown'],
                })
    
    annual_df = pd.DataFrame(annual_rows)
    
    if not annual_df.empty:
        # 每年周四排名
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
    
    # 滑点敏感性
    print("\n" + "=" * 70)
    print("滑点敏感性（周四调仓）")
    print("=" * 70)
    
    # 检查回测引擎是否支持滑点
    has_slippage_support = False
    try:
        import inspect
        from backtest import BacktestEngine
        source = inspect.getsource(BacktestEngine._execute_backtest)
        has_slippage_support = 'slippage' in source.lower()
    except:
        pass
    
    slippage_results = []
    if not has_slippage_support:
        print("\n  [NOTE] 当前回测引擎未实现滑点逻辑。")
        print("  config.py 中定义了 slippage_enabled/slippage_bps，但 backtest.py 未读取。")
        print("  所有滑点设置下结果相同。")
        
        result = run_weekday_backtest(3)  # 周四=3, 0滑点
        slippage_results = [{
            'slippage_bp': bp,
            'total_return': result['total_return'],
            'annual_return': result['annual_return'],
            'sharpe_ratio': result['sharpe_ratio'],
            'max_drawdown': result['max_drawdown'],
            'num_trades': result['num_trades'],
            'total_commission': result['total_commission'],
            'note': '引擎未实现滑点' if bp > 0 else '基准',
        } for bp in slippage_list]
    else:
        for bp in slippage_list:
            print(f"\n[滑点 {bp}bp] 回测中...")
            result = run_weekday_backtest(3, slippage_bp=bp)
            slippage_results.append({
                'slippage_bp': bp,
                'total_return': result['total_return'],
                'annual_return': result['annual_return'],
                'sharpe_ratio': result['sharpe_ratio'],
                'max_drawdown': result['max_drawdown'],
                'num_trades': result['num_trades'],
                'total_commission': result['total_commission'],
            })
            print(f"  总收益: {result['total_return']:.2%}")
    
    # 调仓日期差异分析
    print("\n" + "=" * 70)
    print("调仓日期差异分析")
    print("=" * 70)
    
    # 收集各调仓日的调仓日期列表
    rebalance_dates_map = {}
    for wd in range(5):
        rebalance_dates_map[WEEKDAY_NAMES[wd]] = all_results[wd].get('rebalance_dates', [])
    
    # 比较周四与其他日期的差异
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
        shifts = check_holiday_shift(None, dates)
        if shifts:
            print(f"\n{wd_name}: 发现 {len(shifts)} 次节假日附近调仓")
            for s in shifts[:5]:
                print(f"  调仓日 {s['rebalance_date']} 紧邻节假日 {s['holiday']} (差{s['diff_days']}天)")
        else:
            print(f"\n{wd_name}: 未发现节假日附近调仓")
    
    # 星期四优势跨阶段稳定性判断
    print("\n" + "=" * 70)
    print("星期四优势跨阶段稳定性判断")
    print("=" * 70)
    
    thu_stable = True
    thu_best_count = 0
    total_periods = 0
    
    # 检查年度
    for year in years:
        year_data = annual_df[annual_df['year'] == year]
        if len(year_data) < 5:
            continue
        returns = {r['weekday']: r['total_return'] for _, r in year_data.iterrows()}
        thu_ret = returns.get('周四', 0)
        best_ret = max(returns.values())
        if thu_ret == best_ret:
            thu_best_count += 1
        total_periods += 1
    
    # 检查区间
    for name, _, _ in periods:
        p_data = period_df[period_df['period'] == name]
        if len(p_data) < 5:
            continue
        returns = {r['weekday']: r['total_return'] for _, r in p_data.iterrows()}
        thu_ret = returns.get('周四', 0)
        best_ret = max(returns.values())
        if thu_ret == best_ret:
            thu_best_count += 1
        total_periods += 1
    
    print(f"\n周四在 {total_periods} 个阶段中，{thu_best_count} 次为最优调仓日")
    print(f"  最优占比: {thu_best_count/total_periods:.1%}" if total_periods > 0 else "")
    
    if thu_best_count / total_periods >= 0.5:
        print("  结论: 星期四优势在多数阶段中稳定存在")
    else:
        print("  结论: 星期四优势不稳定，可能受偶然因素影响")
    
    # 生成报告
    print("\n" + "=" * 70)
    print("生成报告...")
    print("=" * 70)
    
    lines = []
    lines.append('# Phase 5.1 调仓星期稳健性诊断报告')
    lines.append('')
    lines.append(f'**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'**数据截止**: {AS_OF_DATE}')
    lines.append('')
    
    lines.append('## 一、全区间回测结果（周一至周五）')
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
    
    lines.append('## 三、分区间统计')
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
    
    lines.append('## 四、滑点敏感性（周四调仓）')
    lines.append('')
    lines.append('| 滑点 | 总收益 | 年化 | 夏普 | 最大回撤 | 交易次数 | 总佣金 |')
    lines.append('|------|--------|------|------|----------|----------|--------|')
    for r in slippage_results:
        note = f" ({r.get('note', '')})" if 'note' in r and r['note'] else ''
        lines.append(f"| {r['slippage_bp']}bp{note} | {r['total_return']:.2%} | {r['annual_return']:.2%} | "
                     f"{r['sharpe_ratio']:.3f} | {r['max_drawdown']:.2%} | {r['num_trades']} | "
                     f"{r['total_commission']:,.0f} |")
    lines.append('')
    
    # 添加滑点未实现的说明
    lines.append('> **注意**：当前回测引擎（`backtest.py`）未实现滑点逻辑。')
    lines.append('> `config.py` 中定义了 `slippage_enabled`/`slippage_bps`，但回测引擎未读取。')
    lines.append('> 因此所有滑点设置下结果相同，滑点敏感性分析在本次诊断中无法进行。')
    lines.append('> 如需实现滑点，需在 `backtest.py` 的买入/卖出执行逻辑中增加价格偏移。')
    lines.append('')
    
    lines.append('## 五、调仓日期差异分析')
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
    
    lines.append('## 六、结论')
    lines.append('')
    lines.append(f"- 周四在 {total_periods} 个阶段中，{thu_best_count} 次为最优调仓日（{thu_best_count/total_periods:.1%}）")
    if thu_best_count / total_periods >= 0.5:
        lines.append('- **结论：星期四优势在多数阶段中稳定存在**')
    else:
        lines.append('- **结论：星期四优势不稳定，可能受偶然因素影响**')
    lines.append('')
    lines.append('- 滑点敏感性：每增加10bp滑点，收益下降约...（详见上方表格）')
    lines.append('')
    
    report_path = 'D:/etf_rotation_model/reports/phase5_weekday_robustness.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n报告已保存: {report_path}")
    print(f"\n{'='*70}")
    print("Phase 5.1 完成")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
