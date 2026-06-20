#!/usr/bin/env python3
"""
Phase 6.1: 降低入场门槛 min_total_score 40→35

目标：改善2020年结构性牛市中"入场慢"的问题，同时验证不劣化其他年份。

方法：
- 基准：B0.3 (min_total_score=40)
- 实验：min_total_score=35（其他参数不变）
- 三阶段回测：训练2019-2022、验证2023-2024（不用于调参）、全区间2020专项分析
- 分年度拆解：重点看2020年改善，同时检查其他年份是否退化
- 候选淘汰：验证期必须不劣于B0.3（支配规则）

不修改生产配置，不运行2025-2026封存样本。
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
from copy import deepcopy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

TRAIN_END = '2022-12-30'
VALID_END = '2024-12-31'
REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase6_1_lower_threshold.md')


def calc_annual_metrics(nav_df, trades_df, year):
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
    return {'ann_ret': ann_ret, 'sharpe': sharpe, 'max_dd': max_dd, 'n_trades': n_trades}


def run_single(cfg, as_of_date, perf_start=None):
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=as_of_date)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=as_of_date)
    engine = BacktestEngine(cfg)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=perf_start)


def main():
    print("=" * 70)
    print("Phase 6.1: Lower min_total_score 40->35")
    print("=" * 70)
    
    # 配置
    cfg_40 = build_config()
    cfg_40['fallback_equity_enabled'] = False
    cfg_40['momentum_factor_enabled'] = False
    cfg_40['volatility_factor_enabled'] = False
    # min_total_score默认是40
    
    cfg_35 = deepcopy(cfg_40)
    cfg_35['min_total_score'] = 35
    
    print(f"\n[1/5] Config check:")
    print(f"  基准 min_total_score: {cfg_40.get('min_total_score', 40)}")
    print(f"  实验 min_total_score: {cfg_35.get('min_total_score', 40)}")
    
    # 运行三阶段
    print(f"\n[2/5] Running backtests...")
    results = {}
    
    for name, cfg in [('40', cfg_40), ('35', cfg_35)]:
        results[name] = {}
        for split, (as_of, perf_start) in {
            'train': (TRAIN_END, None),
            'valid': (VALID_END, '2023-01-01'),
        }.items():
            print(f"  {name} - {split}...")
            results[name][split] = run_single(cfg, as_of, perf_start)
    
    # 全区间（用于2020专项分析）
    print(f"  40 - full (for 2020 analysis)...")
    results['40']['full'] = run_single(cfg_40, VALID_END, None)
    print(f"  35 - full (for 2020 analysis)...")
    results['35']['full'] = run_single(cfg_35, VALID_END, None)
    
    # 提取指标
    print(f"\n[3/5] Extracting metrics...")
    metrics = {}
    for name in ('40', '35'):
        metrics[name] = {}
        for split in ('train', 'valid', 'full'):
            if split not in results[name]:
                continue
            r = results[name][split]
            metrics[name][split] = {
                'total_return': r['total_return'], 'annual_return': r['annual_return'],
                'sharpe': r['sharpe_ratio'], 'max_dd': r['max_drawdown'],
                'num_trades': r['num_trades'], 'rebalance_count': r['rebalance_count'],
            }
            if split == 'full':
                metrics[name]['yearly'] = {}
                for year in range(2019, 2025):
                    y = calc_annual_metrics(r['nav_df'], r['trades_df'], year)
                    if y:
                        metrics[name]['yearly'][year] = y
    
    print(f"\n  Full Results Summary:")
    print(f"  {'Scheme':<12} {'Train Ann':>10} {'Train Sharpe':>12} {'Train DD':>10} {'Valid Ann':>10} {'Valid Sharpe':>12} {'Valid DD':>10}")
    print(f"  {'-'*88}")
    for name in ('40', '35'):
        t = metrics[name]['train']; v = metrics[name]['valid']
        print(f"  {'min='+name:<12} {t['annual_return']:>9.2%} {t['sharpe']:>12.4f} {t['max_dd']:>9.2%} {v['annual_return']:>9.2%} {v['sharpe']:>12.4f} {v['max_dd']:>9.2%}")
    
    # 2020专项分析
    print(f"\n[4/5] 2020专项分析...")
    print(f"  {'Scheme':<12} {'2020 Ann':>10} {'2020 Sharpe':>12} {'2020 DD':>10} {'2020 Trades':>10}")
    print(f"  {'-'*60}")
    for name in ('40', '35'):
        y = metrics[name]['yearly'].get(2020, {})
        if y:
            print(f"  {'min='+name:<12} {y['ann_ret']:>9.2%} {y['sharpe']:>12.4f} {y['max_dd']:>9.2%} {y['n_trades']:>10.0f}")
    
    # 分年度对比
    print(f"\n  Year-by-year comparison:")
    print(f"  {'Year':<8} {'min=40':>10} {'min=35':>10} {'Delta':>10} {'Status':>10}")
    print(f"  {'-'*52}")
    for year in range(2019, 2025):
        y40 = metrics['40']['yearly'].get(year, {})
        y35 = metrics['35']['yearly'].get(year, {})
        if y40 and y35:
            delta = y35['ann_ret'] - y40['ann_ret']
            status = "改善" if delta > 0.005 else "退化" if delta < -0.005 else "持平"
            print(f"  {year:<8} {y40['ann_ret']:>9.2%} {y35['ann_ret']:>9.2%} {delta:>+9.2%} {status:>10}")
    
    # 支配检查（验证期 vs B0.3）
    print(f"\n[5/5] 候选选择（验证期 vs B0.3 支配规则）...")
    b = metrics['40']['valid']
    v = metrics['35']['valid']
    issues = []
    if v['annual_return'] < b['annual_return']:
        issues.append(f"年化劣化({v['annual_return']:.2%} < {b['annual_return']:.2%})")
    if v['sharpe'] < b['sharpe']:
        issues.append(f"Sharpe劣化({v['sharpe']:.4f} < {b['sharpe']:.4f})")
    if v['max_dd'] < b['max_dd']:
        issues.append(f"回撤劣化({v['max_dd']:.2%} < {b['max_dd']:.2%})")
    
    if issues:
        print(f"  min=35: 验证期被B0.3支配，淘汰")
        for issue in issues:
            print(f"    - {issue}")
        final_choice = '40'
    else:
        print(f"  min=35: 验证期通过支配检查")
        if v['annual_return'] > b['annual_return'] or v['sharpe'] > b['sharpe']:
            print(f"  min=35: 至少改善收益或Sharpe，允许进入")
            final_choice = '35'
        else:
            print(f"  min=35: 收益和Sharpe均未改善，不带来增量价值")
            final_choice = '40'
    
    print(f"  最终选择: min_total_score={final_choice}")
    
    # 生成报告
    lines = []
    lines.append("# Phase 6.1 降低入场门槛实验报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**目标**: 改善2020年结构性牛市中'入场慢'的问题")
    lines.append(f"**方法**: min_total_score 40→35（其他参数不变）")
    lines.append("")
    lines.append("## 1. 实验方案")
    lines.append("")
    lines.append("| 方案 | min_total_score | 其他参数 |")
    lines.append("|------|----------------|----------|")
    lines.append("| A | 40 (B0.3基准) | 不变 |")
    lines.append("| B | 35 | 不变 |")
    lines.append("")
    lines.append("## 2. 回测表现")
    lines.append("")
    lines.append("### 2.1 训练期 (2019-2022)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for name in ('40', '35'):
        m = metrics[name]['train']
        lines.append(f"| min={name} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 2.2 验证期 (2023-2024)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for name in ('40', '35'):
        m = metrics[name]['valid']
        lines.append(f"| min={name} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("## 3. 2020年专项分析")
    lines.append("")
    lines.append("| 方案 | 2020年化 | 2020 Sharpe | 2020最大回撤 | 2020交易次数 |")
    lines.append("|------|----------|-------------|-------------|-------------|")
    for name in ('40', '35'):
        y = metrics[name]['yearly'].get(2020, {})
        if y:
            lines.append(f"| min={name} | {y['ann_ret']:.2%} | {y['sharpe']:.4f} | {y['max_dd']:.2%} | {y['n_trades']:.0f} |")
    lines.append("")
    lines.append("## 4. 分年度对比")
    lines.append("")
    lines.append("| 年份 | min=40 | min=35 | Delta | 状态 |")
    lines.append("|------|--------|--------|-------|------|")
    for year in range(2019, 2025):
        y40 = metrics['40']['yearly'].get(year, {})
        y35 = metrics['35']['yearly'].get(year, {})
        if y40 and y35:
            delta = y35['ann_ret'] - y40['ann_ret']
            status = "改善" if delta > 0.005 else "退化" if delta < -0.005 else "持平"
            lines.append(f"| {year} | {y40['ann_ret']:.2%} | {y35['ann_ret']:.2%} | {delta:+.2%} | {status} |")
    lines.append("")
    lines.append("## 5. 候选选择（验证期 vs B0.3 支配规则）")
    lines.append("")
    lines.append("| 检查项 | min=35 | B0.3(min=40) | 结果 |")
    lines.append("|--------|--------|-------------|------|")
    v35 = metrics['35']['valid']; b40 = metrics['40']['valid']
    lines.append(f"| 验证期年化 | {v35['annual_return']:.2%} | {b40['annual_return']:.2%} | {'通过' if v35['annual_return'] >= b40['annual_return'] else 'FAIL'} |")
    lines.append(f"| 验证期Sharpe | {v35['sharpe']:.4f} | {b40['sharpe']:.4f} | {'通过' if v35['sharpe'] >= b40['sharpe'] else 'FAIL'} |")
    lines.append(f"| 验证期回撤 | {v35['max_dd']:.2%} | {b40['max_dd']:.2%} | {'通过' if v35['max_dd'] >= b40['max_dd'] else 'FAIL'} |")
    lines.append("")
    if issues:
        lines.append(f"**min=35 被B0.3支配，淘汰。**")
        lines.append("")
        lines.append("淘汰原因：")
        for issue in issues:
            lines.append(f"- {issue}")
    else:
        if v35['annual_return'] > b40['annual_return'] or v35['sharpe'] > b40['sharpe']:
            lines.append(f"**min=35 通过支配检查，且至少改善收益或Sharpe。**")
        else:
            lines.append(f"**min=35 通过支配检查，但收益和Sharpe均未改善，无增量价值。**")
    lines.append("")
    lines.append(f"### 最终结论")
    lines.append("")
    if final_choice == '35':
        lines.append(f"- **采纳 min_total_score=35**（降低入场门槛）")
        lines.append(f"- 2020年改善：{metrics['35']['yearly'][2020]['ann_ret']:.2%} vs {metrics['40']['yearly'][2020]['ann_ret']:.2%} ({metrics['35']['yearly'][2020]['ann_ret'] - metrics['40']['yearly'][2020]['ann_ret']:+.2%})")
        lines.append(f"- 验证期通过支配检查")
    else:
        lines.append(f"- **保持 min_total_score=40（B0.3基准）**")
        if issues:
            lines.append(f"- 原因：验证期被B0.3支配")
        else:
            lines.append(f"- 原因：min=35 无增量价值")
    lines.append("")
    lines.append("---")
    lines.append("*2025-2026封存样本未运行，不用于调参。*")
    lines.append("*未修改生产配置 (src/config.py)。*")
    
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n  Report saved to: {REPORT_PATH}")
    print("=" * 70)
    print("Phase 6.1 completed.")
    print("=" * 70)


if __name__ == '__main__':
    main()
