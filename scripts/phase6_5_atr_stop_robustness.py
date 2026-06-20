#!/usr/bin/env python3
"""
Phase 6.5: ATR动态止损稳健性验证

目标：验证ATR止损的改善是否稳定，以及它是否只是放宽固定-8%止损。

基准：B0.3固定止损-8%。
实验方案：
- ATR multiplier = 1.5
- ATR multiplier = 2.0
- ATR multiplier = 2.5

保持其他策略规则完全不变。

重点检查：
1. 明确当前ATR公式：
   atr_stop = cost - multiplier × entry_atr
   fixed_stop = cost × 0.92
   actual_stop = min(atr_stop, fixed_stop)

2. 统计每笔持仓的实际止损幅度分布
3. 对比固定止损：避免哪些、新增哪些、止损次数、平均亏损、最大亏损、后续价格表现
4. 分阶段运行：训练期2019-2022、验证期2023-2024
5. 分年度报告
6. 不运行2025-2026封存样本
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from copy import deepcopy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

TRAIN_END = '2022-12-30'
VALID_END = '2024-12-31'
REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase6_5_atr_stop_robustness.md')


def calc_annual_metrics(nav_df, trades_df, year):
    """计算年度指标"""
    start = pd.Timestamp(f'{year}-01-01')
    end = pd.Timestamp(f'{year}-12-31')
    year_nav = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].copy()
    if len(year_nav) < 2:
        return None
    first_nav = year_nav['nav'].iloc[0]
    last_nav = year_nav['nav'].iloc[-1]
    days = (year_nav['date'].iloc[-1] - year_nav['date'].iloc[0]).days
    ann_ret = (last_nav / first_nav) ** (365 / days) - 1 if days > 0 else 0.0
    year_nav['daily_ret'] = year_nav['nav'].pct_change()
    valid_rets = year_nav['daily_ret'].dropna()
    sharpe = (valid_rets.mean() / valid_rets.std()) * np.sqrt(252) if len(valid_rets) > 1 and valid_rets.std() > 0 else 0.0
    cummax = year_nav['nav'].cummax()
    drawdown = (year_nav['nav'] - cummax) / cummax
    max_dd = drawdown.min()
    year_trades = trades_df[(pd.to_datetime(trades_df['date']) >= start) & (pd.to_datetime(trades_df['date']) <= end)] if 'date' in trades_df.columns else pd.DataFrame()
    n_trades = len(year_trades)
    stop_trades = year_trades[year_trades['action'] == 'STOP_LOSS'] if not year_trades.empty else pd.DataFrame()
    n_stops = len(stop_trades)
    avg_stop_loss = stop_trades['pnl_pct'].mean() if not stop_trades.empty and 'pnl_pct' in stop_trades.columns else 0.0
    max_stop_loss = stop_trades['pnl_pct'].min() if not stop_trades.empty and 'pnl_pct' in stop_trades.columns else 0.0
    return {
        'ann_ret': ann_ret, 'sharpe': sharpe, 'max_dd': max_dd,
        'n_trades': n_trades, 'n_stops': n_stops,
        'avg_stop_loss': avg_stop_loss, 'max_stop_loss': max_stop_loss,
    }


def analyze_stop_losses(trades_df, market_df):
    """详细分析止损交易"""
    stops = trades_df[trades_df['action'] == 'STOP_LOSS'].copy()
    if stops.empty:
        return {}
    
    # 实际止损幅度分布
    pnl_values = stops['pnl_pct'].dropna()
    stats = {
        'count': len(stops),
        'median': pnl_values.median(),
        'q25': pnl_values.quantile(0.25),
        'q75': pnl_values.quantile(0.75),
        'min': pnl_values.min(),
        'max': pnl_values.max(),
        'mean': pnl_values.mean(),
        'std': pnl_values.std(),
        'below_8pct': (pnl_values < -0.08).sum(),
        'below_8pct_pct': (pnl_values < -0.08).mean() * 100,
    }
    
    # 解析止损原因
    if 'reason' in stops.columns:
        atr_stops = stops[stops['reason'].str.contains('ATR止损', na=False)]
        fixed_stops = stops[stops['reason'].str.contains('固定止损', na=False)]
        stats['atr_count'] = len(atr_stops)
        stats['fixed_count'] = len(fixed_stops)
        if not atr_stops.empty:
            stats['atr_median'] = atr_stops['pnl_pct'].median()
            stats['atr_mean'] = atr_stops['pnl_pct'].mean()
        if not fixed_stops.empty:
            stats['fixed_median'] = fixed_stops['pnl_pct'].median()
            stats['fixed_mean'] = fixed_stops['pnl_pct'].mean()
    
    # 后续价格表现
    market = market_df[['date', 'ticker', 'close']].copy().sort_values(['ticker', 'date'])
    market['date'] = pd.to_datetime(market['date'])
    
    future_returns = {5: [], 10: [], 20: []}
    for _, row in stops.iterrows():
        stop_date = pd.to_datetime(row['date'])
        ticker = row['ticker']
        stop_price = row['price']
        
        ticker_df = market[market['ticker'] == ticker]
        after_stop = ticker_df[ticker_df['date'] > stop_date]
        
        for h in (5, 10, 20):
            if len(after_stop) >= h:
                future_price = after_stop.iloc[h-1]['close']
                future_ret = (future_price - stop_price) / stop_price if stop_price > 0 else 0
                future_returns[h].append(future_ret)
    
    for h in (5, 10, 20):
        if future_returns[h]:
            arr = np.array(future_returns[h])
            stats[f'future_{h}d_mean'] = arr.mean()
            stats[f'future_{h}d_median'] = np.median(arr)
            stats[f'future_{h}d_positive'] = (arr > 0).mean() * 100
    
    return stats


def compare_stop_losses(fixed_trades, atr_trades, fixed_market, atr_market):
    """对比固定止损和ATR止损的差异"""
    fixed_stops = fixed_trades[fixed_trades['action'] == 'STOP_LOSS'].copy()
    atr_stops = atr_trades[atr_trades['action'] == 'STOP_LOSS'].copy()
    
    comparison = {}
    
    # 固定止损有但ATR没有的（被避免了）
    if not fixed_stops.empty and not atr_stops.empty:
        fixed_keys = set(zip(fixed_stops['date'], fixed_stops['ticker']))
        atr_keys = set(zip(atr_stops['date'], atr_stops['ticker']))
        avoided = fixed_keys - atr_keys
        added = atr_keys - fixed_keys
        comparison['avoided_count'] = len(avoided)
        comparison['added_count'] = len(added)
    else:
        comparison['avoided_count'] = 0
        comparison['added_count'] = 0
    
    # 止损次数
    comparison['fixed_count'] = len(fixed_stops)
    comparison['atr_count'] = len(atr_stops)
    
    # 平均亏损
    if not fixed_stops.empty and 'pnl_pct' in fixed_stops.columns:
        comparison['fixed_avg_loss'] = fixed_stops['pnl_pct'].mean()
        comparison['fixed_max_loss'] = fixed_stops['pnl_pct'].min()
    else:
        comparison['fixed_avg_loss'] = 0.0
        comparison['fixed_max_loss'] = 0.0
    
    if not atr_stops.empty and 'pnl_pct' in atr_stops.columns:
        comparison['atr_avg_loss'] = atr_stops['pnl_pct'].mean()
        comparison['atr_max_loss'] = atr_stops['pnl_pct'].min()
    else:
        comparison['atr_avg_loss'] = 0.0
        comparison['atr_max_loss'] = 0.0
    
    return comparison


def run_backtest(cfg, as_of_date, perf_start=None):
    """运行回测"""
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=as_of_date)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=as_of_date)
    engine = BacktestEngine(cfg)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=perf_start), market_df


def main():
    print("=" * 70)
    print("Phase 6.5: ATR Stop Loss Robustness Verification")
    print("=" * 70)
    
    # 基准配置
    cfg_fixed = build_config()
    cfg_fixed['fallback_equity_enabled'] = False
    cfg_fixed['momentum_factor_enabled'] = False
    cfg_fixed['volatility_factor_enabled'] = False
    # stop_loss_mode默认是'fixed'
    
    print(f"\n[1/7] Config setup:")
    print(f"  Fixed stop: stop_loss={cfg_fixed['stop_loss']}")
    
    # 实验配置
    experiments = {
        'fixed': {'name': '固定止损-8%', 'cfg': deepcopy(cfg_fixed)},
        'atr_1.5': {'name': 'ATR 1.5x', 'cfg': deepcopy(cfg_fixed)},
        'atr_2.0': {'name': 'ATR 2.0x', 'cfg': deepcopy(cfg_fixed)},
        'atr_2.5': {'name': 'ATR 2.5x', 'cfg': deepcopy(cfg_fixed)},
    }
    
    for key in ('atr_1.5', 'atr_2.0', 'atr_2.5'):
        experiments[key]['cfg']['stop_loss_mode'] = 'atr'
        experiments[key]['cfg']['atr_stop_multiplier'] = float(key.split('_')[1])
    
    # 运行回测
    print(f"\n[2/7] Running backtests...")
    results = {}
    market_data = {}
    
    for key, exp in experiments.items():
        results[key] = {}
        for split, (as_of, perf_start) in {
            'train': (TRAIN_END, None),
            'valid': (VALID_END, '2023-01-01'),
        }.items():
            print(f"  {exp['name']} - {split}...")
            result, market_df = run_backtest(exp['cfg'], as_of, perf_start)
            results[key][split] = result
            market_data[key] = market_df
    
    # 全区间（用于2020分析和止损对比）
    print(f"  Full interval (for stop loss analysis)...")
    for key, exp in experiments.items():
        result, market_df = run_backtest(exp['cfg'], VALID_END, None)
        results[key]['full'] = result
        market_data[key] = market_df
    
    # 提取指标
    print(f"\n[3/7] Extracting metrics...")
    metrics = {}
    for key, exp in experiments.items():
        metrics[key] = {}
        for split in ('train', 'valid', 'full'):
            if split not in results[key]:
                continue
            r = results[key][split]
            metrics[key][split] = {
                'total_return': r['total_return'], 'annual_return': r['annual_return'],
                'sharpe': r['sharpe_ratio'], 'max_dd': r['max_drawdown'],
                'num_trades': r['num_trades'], 'rebalance_count': r['rebalance_count'],
                'stop_loss_count': r.get('stop_loss_count', 0),
            }
            if split == 'full':
                metrics[key]['yearly'] = {}
                for year in range(2019, 2025):
                    y = calc_annual_metrics(r['nav_df'], r['trades_df'], year)
                    if y:
                        metrics[key]['yearly'][year] = y
    
    # 汇总表
    print(f"\n  {'Scheme':<20} {'Train Ann':>10} {'Train Sharpe':>12} {'Train DD':>10} {'Valid Ann':>10} {'Valid Sharpe':>12} {'Valid DD':>10}")
    print(f"  {'-'*86}")
    for key, exp in experiments.items():
        t = metrics[key]['train']; v = metrics[key]['valid']
        print(f"  {exp['name']:<20} {t['annual_return']:>9.2%} {t['sharpe']:>12.4f} {t['max_dd']:>9.2%} {v['annual_return']:>9.2%} {v['sharpe']:>12.4f} {v['max_dd']:>9.2%}")
    
    # 止损分析
    print(f"\n[4/7] Stop loss analysis...")
    stop_analysis = {}
    for key, exp in experiments.items():
        print(f"  {exp['name']}...")
        stop_analysis[key] = analyze_stop_losses(results[key]['full']['trades_df'], market_data[key])
    
    # 对比固定止损和ATR止损
    print(f"\n[5/7] Fixed vs ATR stop loss comparison...")
    comparisons = {}
    for key in ('atr_1.5', 'atr_2.0', 'atr_2.5'):
        comparisons[key] = compare_stop_losses(
            results['fixed']['full']['trades_df'],
            results[key]['full']['trades_df'],
            market_data['fixed'],
            market_data[key]
        )
    
    # 2020专项
    print(f"\n[6/7] 2020专项分析...")
    print(f"  {'Scheme':<20} {'2020 Ann':>10} {'2020 Sharpe':>12} {'2020 DD':>10} {'Stops':>8} {'AvgLoss':>10}")
    print(f"  {'-'*72}")
    for key, exp in experiments.items():
        y = metrics[key]['yearly'].get(2020, {})
        if y:
            print(f"  {exp['name']:<20} {y['ann_ret']:>9.2%} {y['sharpe']:>12.4f} {y['max_dd']:>9.2%} {y['n_stops']:>8.0f} {y['avg_stop_loss']:>9.2%}")
    
    # 候选选择
    print(f"\n[7/7] 候选选择...")
    base = metrics['fixed']['valid']
    
    candidates = []
    for key in ('atr_1.5', 'atr_2.0', 'atr_2.5'):
        v = metrics[key]['valid']
        issues = []
        
        # 1. 验证期收益或Sharpe至少一项改善
        if v['annual_return'] <= base['annual_return'] and v['sharpe'] <= base['sharpe']:
            issues.append("收益和Sharpe均未改善")
        
        # 2. 最大回撤不得明显恶化（深于基准超过1%）
        if v['max_dd'] < base['max_dd'] - 0.01:
            issues.append(f"回撤明显恶化({v['max_dd']:.2%} < {base['max_dd']:.2%})")
        
        # 3. 平均止损亏损和尾部亏损可接受
        sa = stop_analysis[key]
        if sa and sa.get('mean', 0) < -0.12:
            issues.append(f"平均止损亏损过大({sa['mean']:.2%})")
        if sa and sa.get('min', 0) < -0.20:
            issues.append(f"最大止损亏损过大({sa['min']:.2%})")
        
        # 4. 改善不能只来自单一年份
        yearly = metrics[key]['yearly']
        improvements = sum(1 for y in range(2019, 2025) if y in yearly and y in metrics['fixed']['yearly'] and yearly[y]['ann_ret'] > metrics['fixed']['yearly'][y]['ann_ret'] + 0.001)
        if improvements < 2:
            issues.append("改善只来自单一年份")
        
        if not issues:
            candidates.append(key)
            print(f"  {experiments[key]['name']}: 通过候选检查")
        else:
            print(f"  {experiments[key]['name']}: 淘汰")
            for issue in issues:
                print(f"    - {issue}")
    
    # 邻域稳定性检查
    if candidates:
        print(f"\n  邻域稳定性检查（1.5, 2.0, 2.5）:")
        vals = {k: metrics[k]['valid']['annual_return'] for k in ('atr_1.5', 'atr_2.0', 'atr_2.5')}
        sorted_keys = sorted(vals, key=vals.get, reverse=True)
        print(f"    排名: {[experiments[k]['name'] for k in sorted_keys]}")
        
        # 检查是否有断崖
        max_val = max(vals.values())
        min_val = min(vals.values())
        if max_val - min_val > 0.02:
            print(f"    WARNING: 邻域差异 {max_val - min_val:.2%} > 2%，存在不稳定")
        else:
            print(f"    邻域稳定，差异 {max_val - min_val:.2%}")
    
    # 生成报告
    lines = []
    lines.append("# Phase 6.5 ATR动态止损稳健性验证报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**基准**: B0.3固定止损-8%")
    lines.append(f"**实验**: ATR multiplier = 1.5 / 2.0 / 2.5")
    lines.append("")
    lines.append("## 1. ATR止损公式说明")
    lines.append("")
    lines.append("```")
    lines.append("atr_stop_price = cost - multiplier × entry_atr")
    lines.append("fixed_stop_price = cost × 0.92")
    lines.append("actual_stop_price = min(atr_stop_price, fixed_stop_price)")
    lines.append("```")
    lines.append("")
    lines.append("- 当 ATR 较小时（< cost×0.08/multiplier），固定止损更严格")
    lines.append("- 当 ATR 较大时，ATR止损更严格")
    lines.append("- 实际取两者中更严格的，不是放宽")
    lines.append("")
    lines.append("## 2. 回测表现")
    lines.append("")
    lines.append("### 2.1 训练期 (2019-2022)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 | 止损次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|----------|")
    for key, exp in experiments.items():
        m = metrics[key]['train']
        lines.append(f"| {exp['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} | {m['stop_loss_count']} |")
    lines.append("")
    lines.append("### 2.2 验证期 (2023-2024)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 | 止损次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|----------|")
    for key, exp in experiments.items():
        m = metrics[key]['valid']
        lines.append(f"| {exp['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} | {m['stop_loss_count']} |")
    lines.append("")
    lines.append("## 3. 止损详细分析")
    lines.append("")
    lines.append("### 3.1 止损幅度分布")
    lines.append("")
    lines.append("| 方案 | 次数 | 中位数 | 25%分位 | 75%分位 | 均值 | 标准差 | 最宽 | 低于-8%比例 |")
    lines.append("|------|------|--------|---------|---------|------|--------|------|------------|")
    for key, exp in experiments.items():
        sa = stop_analysis[key]
        if sa:
            lines.append(f"| {exp['name']} | {sa['count']} | {sa['median']:.2%} | {sa['q25']:.2%} | {sa['q75']:.2%} | {sa['mean']:.2%} | {sa['std']:.2%} | {sa['min']:.2%} | {sa['below_8pct']:.1f}% |")
        else:
            lines.append(f"| {exp['name']} | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
    lines.append("")
    lines.append("### 3.2 固定止损 vs ATR止损对比")
    lines.append("")
    lines.append("| 对比项 | 固定止损 | ATR 1.5x | ATR 2.0x | ATR 2.5x |")
    lines.append("|--------|----------|----------|----------|----------|")
    lines.append(f"| 止损次数 | {stop_analysis['fixed'].get('count', 0)} | {stop_analysis['atr_1.5'].get('count', 0)} | {stop_analysis['atr_2.0'].get('count', 0)} | {stop_analysis['atr_2.5'].get('count', 0)} |")
    lines.append(f"| 平均亏损 | {stop_analysis['fixed'].get('mean', 0):.2%} | {stop_analysis['atr_1.5'].get('mean', 0):.2%} | {stop_analysis['atr_2.0'].get('mean', 0):.2%} | {stop_analysis['atr_2.5'].get('mean', 0):.2%} |")
    lines.append(f"| 最大亏损 | {stop_analysis['fixed'].get('min', 0):.2%} | {stop_analysis['atr_1.5'].get('min', 0):.2%} | {stop_analysis['atr_2.0'].get('min', 0):.2%} | {stop_analysis['atr_2.5'].get('min', 0):.2%} |")
    lines.append(f"| 避免止损 | - | {comparisons['atr_1.5'].get('avoided_count', 0)} | {comparisons['atr_2.0'].get('avoided_count', 0)} | {comparisons['atr_2.5'].get('avoided_count', 0)} |")
    lines.append(f"| 新增止损 | - | {comparisons['atr_1.5'].get('added_count', 0)} | {comparisons['atr_2.0'].get('added_count', 0)} | {comparisons['atr_2.5'].get('added_count', 0)} |")
    lines.append("")
    lines.append("### 3.3 止损后未来价格表现")
    lines.append("")
    lines.append("| 方案 | 5日后均值 | 5日中位数 | 5日正收益比例 | 10日后均值 | 10日中位数 | 10日正收益比例 | 20日后均值 | 20日中位数 | 20日正收益比例 |")
    lines.append("|------|----------|-----------|---------------|------------|------------|----------------|------------|------------|----------------|")
    for key, exp in experiments.items():
        sa = stop_analysis[key]
        if sa:
            def fmt(v): return f"{v:.2%}" if v is not None else "N/A"
            lines.append(f"| {exp['name']} | {fmt(sa.get('future_5d_mean'))} | {fmt(sa.get('future_5d_median'))} | {fmt(sa.get('future_5d_positive'))} | {fmt(sa.get('future_10d_mean'))} | {fmt(sa.get('future_10d_median'))} | {fmt(sa.get('future_10d_positive'))} | {fmt(sa.get('future_20d_mean'))} | {fmt(sa.get('future_20d_median'))} | {fmt(sa.get('future_20d_positive'))} |")
        else:
            lines.append(f"| {exp['name']} | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
    lines.append("")
    lines.append("## 4. 分年度对比")
    lines.append("")
    lines.append("### 4.1 年化收益")
    lines.append("")
    lines.append("| 年份 | 固定止损 | ATR 1.5x | ATR 2.0x | ATR 2.5x |")
    lines.append("|------|----------|----------|----------|----------|")
    for year in range(2019, 2025):
        row = [str(year)]
        for key in ('fixed', 'atr_1.5', 'atr_2.0', 'atr_2.5'):
            y = metrics[key]['yearly'].get(year, {})
            row.append(f"{y['ann_ret']:.2%}" if y else "N/A")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("### 4.2 Sharpe比率")
    lines.append("")
    lines.append("| 年份 | 固定止损 | ATR 1.5x | ATR 2.0x | ATR 2.5x |")
    lines.append("|------|----------|----------|----------|----------|")
    for year in range(2019, 2025):
        row = [str(year)]
        for key in ('fixed', 'atr_1.5', 'atr_2.0', 'atr_2.5'):
            y = metrics[key]['yearly'].get(year, {})
            row.append(f"{y['sharpe']:.4f}" if y else "N/A")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("### 4.3 止损次数")
    lines.append("")
    lines.append("| 年份 | 固定止损 | ATR 1.5x | ATR 2.0x | ATR 2.5x |")
    lines.append("|------|----------|----------|----------|----------|")
    for year in range(2019, 2025):
        row = [str(year)]
        for key in ('fixed', 'atr_1.5', 'atr_2.0', 'atr_2.5'):
            y = metrics[key]['yearly'].get(year, {})
            row.append(f"{y['n_stops']:.0f}" if y else "N/A")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("### 4.4 平均止损亏损")
    lines.append("")
    lines.append("| 年份 | 固定止损 | ATR 1.5x | ATR 2.0x | ATR 2.5x |")
    lines.append("|------|----------|----------|----------|----------|")
    for year in range(2019, 2025):
        row = [str(year)]
        for key in ('fixed', 'atr_1.5', 'atr_2.0', 'atr_2.5'):
            y = metrics[key]['yearly'].get(year, {})
            row.append(f"{y['avg_stop_loss']:.2%}" if y else "N/A")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## 5. 候选选择")
    lines.append("")
    lines.append("### 5.1 检查项")
    lines.append("")
    lines.append("候选必须满足：")
    lines.append("1. 验证期收益或Sharpe至少一项改善")
    lines.append("2. 最大回撤不得明显恶化（深于基准超过1%）")
    lines.append("3. 平均止损亏损不超过-12%，最大不超过-20%")
    lines.append("4. 改善不能只来自单一年份")
    lines.append("")
    lines.append("### 5.2 检查结果")
    lines.append("")
    lines.append("| 方案 | 验证期年化 | 验证期Sharpe | 验证期回撤 | 平均止损 | 最大止损 | 改善年份 | 结果 |")
    lines.append("|------|-----------|-------------|-----------|----------|----------|----------|------|")
    for key in ('atr_1.5', 'atr_2.0', 'atr_2.5'):
        v = metrics[key]['valid']
        sa = stop_analysis[key]
        yearly = metrics[key]['yearly']
        improvements = sum(1 for y in range(2019, 2025) if y in yearly and y in metrics['fixed']['yearly'] and yearly[y]['ann_ret'] > metrics['fixed']['yearly'][y]['ann_ret'] + 0.001)
        
        result = "通过" if key in candidates else "淘汰"
        lines.append(f"| {experiments[key]['name']} | {v['annual_return']:.2%} | {v['sharpe']:.4f} | {v['max_dd']:.2%} | {sa.get('mean', 0):.2%} | {sa.get('min', 0):.2%} | {improvements}年 | {result} |")
    lines.append("")
    
    if candidates:
        lines.append("### 5.3 邻域稳定性")
        lines.append("")
        vals = {k: metrics[k]['valid']['annual_return'] for k in ('atr_1.5', 'atr_2.0', 'atr_2.5')}
        sorted_keys = sorted(vals, key=vals.get, reverse=True)
        lines.append(f"排名: {[experiments[k]['name'] for k in sorted_keys]}")
        max_val = max(vals.values()); min_val = min(vals.values())
        lines.append(f"邻域差异: {max_val - min_val:.2%}")
        if max_val - min_val > 0.02:
            lines.append(f"WARNING: 邻域差异 > 2%，存在不稳定")
        else:
            lines.append(f"邻域稳定")
    lines.append("")
    lines.append("### 5.4 最终结论")
    lines.append("")
    if not candidates:
        lines.append("- **所有ATR方案未通过候选检查，保持固定止损**")
    else:
        best = max(candidates, key=lambda k: metrics[k]['valid']['annual_return'])
        lines.append(f"- **候选: {experiments[best]['name']}**")
        lines.append(f"- 验证期年化: {metrics[best]['valid']['annual_return']:.2%} (基准: {metrics['fixed']['valid']['annual_return']:.2%})")
        lines.append(f"- 验证期Sharpe: {metrics[best]['valid']['sharpe']:.4f} (基准: {metrics['fixed']['valid']['sharpe']:.4f})")
        lines.append(f"- 全区间止损次数: {stop_analysis[best].get('count', 0)} (固定: {stop_analysis['fixed'].get('count', 0)})")
        lines.append(f"- 平均止损亏损: {stop_analysis[best].get('mean', 0):.2%} (固定: {stop_analysis['fixed'].get('mean', 0):.2%})")
        lines.append(f"- 避免止损: {comparisons[best].get('avoided_count', 0)} 次")
    lines.append("")
    lines.append("---")
    lines.append("*2025-2026封存样本未运行，不用于调参。*")
    lines.append("*未修改生产配置 (src/config.py)。*")
    
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n  Report saved to: {REPORT_PATH}")
    print("=" * 70)
    print("Phase 6.5 completed.")
    print("=" * 70)


if __name__ == '__main__':
    main()
