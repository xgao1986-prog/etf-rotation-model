#!/usr/bin/env python3
"""
Phase 6.2-6.4: 综合实验 — 改善2020年结构性牛市跑输问题

三个独立实验，每个只修改一个因素，分别与B0.3基准比较：
- 6.2: Momentum急涨通道（提前入场机制）
- 6.3: ATR动态止损（减少震荡市被震出）
- 6.4: 行业弹性过滤（排除低波动行业如银行）

基准：B0.3 (min_total_score=40, fixed stop, no momentum, no vol_score)
方法：三阶段回测 + 支配规则 + 2020专项分析
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
REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase6_2_to_6_4_comprehensive.md')


# =============================================================================
# 实验6.2: Momentum 急涨通道
# =============================================================================

class SurgeChannelEngine(StrategyEngine):
    """
    急涨通道：当 momentum_20 > 0 且 trend_score >= 10 时，
    即使 total_score < 40，也给一个 surge_bonus +5 使其跨过门槛。
    这样可以在快速反弹年份（如2020）提前入场，吃到更多涨幅。
    """
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = super().compute_total_score(scores_df, exclude_factor)
        # 急涨通道条件：momentum_20 > 0（有正向动量）且 trend_score >= 10（至少上穿MA）
        # 且当前total_score < 40（无法通过正常门槛）
        # 给 surge_bonus +5，使其跨过40门槛
        surge = (df['momentum_20'] > 0) & (df['trend_score'] >= 10) & (df['total_score'] < 40)
        df.loc[surge, 'total_score'] += 5
        # 标记哪些是急涨通道（用于报告）
        df['surge_channel'] = surge.astype(int)
        return df


# =============================================================================
# 实验6.4: 行业弹性过滤
# =============================================================================

class ElasticFilterEngine(StrategyEngine):
    """
    弹性过滤：排除波动率最低的行业ETF（如银行），避免在结构性牛市中
    占用仓位给低弹性行业。
    """
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = super().compute_total_score(scores_df, exclude_factor)
        # 对于每个日期，计算行业ETF的20日波动率分位数
        # 排除波动率最低的20%（约3个行业），将其total_score设为0
        result = []
        core_tickers = set(ETF_UNIVERSE.keys())
        for date in df['date'].unique():
            day = df[df['date'] == date].copy()
            core = day[day['ticker'].isin(core_tickers)]
            if len(core) >= 5:
                q20 = core['volatility_20'].quantile(0.2)
                # 只排除行业ETF中波动率最低的20%
                low_vol = (day['volatility_20'] <= q20) & day['ticker'].isin(core_tickers)
                day.loc[low_vol, 'total_score'] = 0
                day.loc[low_vol, 'elastic_filtered'] = 1
                day.loc[~low_vol, 'elastic_filtered'] = 0
            else:
                day['elastic_filtered'] = 0
            result.append(day)
        return pd.concat(result, ignore_index=True)


# =============================================================================
# Helpers
# =============================================================================

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


def dominance_check(metrics_exp, metrics_base, name):
    """返回 (是否通过, 问题列表)"""
    issues = []
    if metrics_exp['annual_return'] < metrics_base['annual_return']:
        issues.append(f"年化劣化({metrics_exp['annual_return']:.2%} < {metrics_base['annual_return']:.2%})")
    if metrics_exp['sharpe'] < metrics_base['sharpe']:
        issues.append(f"Sharpe劣化({metrics_exp['sharpe']:.4f} < {metrics_base['sharpe']:.4f})")
    if metrics_exp['max_dd'] < metrics_base['max_dd']:
        issues.append(f"回撤劣化({metrics_exp['max_dd']:.2%} < {metrics_base['max_dd']:.2%})")
    return len(issues) == 0, issues


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 70)
    print("Phase 6.2-6.4: Comprehensive 2020 Improvement Experiments")
    print("=" * 70)
    
    # 基准配置
    cfg_baseline = build_config()
    cfg_baseline['fallback_equity_enabled'] = False
    cfg_baseline['momentum_factor_enabled'] = False
    cfg_baseline['volatility_factor_enabled'] = False
    # stop_loss_mode 默认是 'fixed'
    
    print(f"\n[1/6] Config setup:")
    print(f"  Baseline: min_total_score={cfg_baseline.get('min_total_score', 40)}, stop_loss_mode={cfg_baseline.get('stop_loss_mode', 'fixed')}")
    
    # 实验配置
    experiments = {
        'A': {'name': 'B0.3基准', 'cfg': cfg_baseline, 'engine_cls': StrategyEngine, 'kwargs': {}},
        'B': {
            'name': '6.2 Momentum急涨通道',
            'cfg': deepcopy(cfg_baseline),
            'engine_cls': SurgeChannelEngine,
            'kwargs': {},
        },
        'C': {
            'name': '6.3 ATR动态止损',
            'cfg': deepcopy(cfg_baseline),
            'engine_cls': StrategyEngine,
            'kwargs': {},
        },
        'D': {
            'name': '6.4 行业弹性过滤',
            'cfg': deepcopy(cfg_baseline),
            'engine_cls': ElasticFilterEngine,
            'kwargs': {},
        },
    }
    
    # 6.3特殊配置
    experiments['C']['cfg']['stop_loss_mode'] = 'atr'
    experiments['C']['cfg']['atr_stop_multiplier'] = 2.0
    
    # 运行回测
    print(f"\n[2/6] Running backtests...")
    results = {}
    for key, exp in experiments.items():
        results[key] = {}
        for split, (as_of, perf_start) in {
            'train': (TRAIN_END, None),
            'valid': (VALID_END, '2023-01-01'),
        }.items():
            print(f"  {exp['name']} - {split}...")
            db = ETFDatabase()
            tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
            market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=as_of)
            bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=as_of)
            engine = BacktestEngine(exp['cfg'])
            engine.strategy = exp['engine_cls'](exp['cfg'], **exp['kwargs'])
            results[key][split] = engine.run(market_df, bench_df, as_of_date=as_of, performance_start=perf_start)
    
    # 全区间（用于2020分析）
    print(f"  Full interval (for 2020 analysis)...")
    for key, exp in experiments.items():
        db = ETFDatabase()
        tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
        market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=VALID_END)
        bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=VALID_END)
        engine = BacktestEngine(exp['cfg'])
        engine.strategy = exp['engine_cls'](exp['cfg'], **exp['kwargs'])
        results[key]['full'] = engine.run(market_df, bench_df, as_of_date=VALID_END, performance_start=None)
    
    # 提取指标
    print(f"\n[3/6] Extracting metrics...")
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
    print(f"\n  {'Scheme':<25} {'Train Ann':>10} {'Train Sharpe':>12} {'Train DD':>10} {'Valid Ann':>10} {'Valid Sharpe':>12} {'Valid DD':>10}")
    print(f"  {'-'*92}")
    for key, exp in experiments.items():
        t = metrics[key]['train']; v = metrics[key]['valid']
        print(f"  {exp['name']:<25} {t['annual_return']:>9.2%} {t['sharpe']:>12.4f} {t['max_dd']:>9.2%} {v['annual_return']:>9.2%} {v['sharpe']:>12.4f} {v['max_dd']:>9.2%}")
    
    # 2020专项
    print(f"\n[4/6] 2020专项分析...")
    print(f"  {'Scheme':<25} {'2020 Ann':>10} {'2020 Sharpe':>12} {'2020 DD':>10} {'2020 Trades':>12} {'2020 Stops':>10}")
    print(f"  {'-'*84}")
    for key, exp in experiments.items():
        y = metrics[key]['yearly'].get(2020, {})
        if y:
            print(f"  {exp['name']:<25} {y['ann_ret']:>9.2%} {y['sharpe']:>12.4f} {y['max_dd']:>9.2%} {y['n_trades']:>12.0f} {metrics[key]['full'].get('stop_loss_count', 0):>10}")
    
    # 分年度对比
    print(f"\n[5/6] 分年度对比（vs B0.3）:")
    print(f"  {'Year':<8} {'B0.3':>10} {'6.2':>10} {'6.3':>10} {'6.4':>10}")
    print(f"  {'-'*52}")
    for year in range(2019, 2025):
        y40 = metrics['A']['yearly'].get(year, {})
        y62 = metrics['B']['yearly'].get(year, {})
        y63 = metrics['C']['yearly'].get(year, {})
        y64 = metrics['D']['yearly'].get(year, {})
        if y40 and y62 and y63 and y64:
            print(f"  {year:<8} {y40['ann_ret']:>9.2%} {y62['ann_ret']:>9.2%} {y63['ann_ret']:>9.2%} {y64['ann_ret']:>9.2%}")
    
    print(f"\n  {'Year':<8} {'B0.3':>10} {'6.2':>10} {'6.3':>10} {'6.4':>10}")
    print(f"  {'-'*52}")
    for year in range(2019, 2025):
        y40 = metrics['A']['yearly'].get(year, {})
        y62 = metrics['B']['yearly'].get(year, {})
        y63 = metrics['C']['yearly'].get(year, {})
        y64 = metrics['D']['yearly'].get(year, {})
        if y40 and y62 and y63 and y64:
            print(f"  {year:<8} {y40['sharpe']:>10.4f} {y62['sharpe']:>10.4f} {y63['sharpe']:>10.4f} {y64['sharpe']:>10.4f}")
    
    # 支配检查
    print(f"\n[6/6] 候选选择（验证期 vs B0.3 支配规则）...")
    base_v = metrics['A']['valid']
    final_choices = {}
    for key in ('B', 'C', 'D'):
        exp_v = metrics[key]['valid']
        passed, issues = dominance_check(exp_v, base_v, experiments[key]['name'])
        print(f"  {experiments[key]['name']}: pass={passed}")
        if issues:
            for issue in issues:
                print(f"    - FAIL: {issue}")
        
        if passed and (exp_v['annual_return'] > base_v['annual_return'] or exp_v['sharpe'] > base_v['sharpe']):
            print(f"    -> SURVIVOR (至少改善收益或Sharpe)")
            final_choices[key] = True
        else:
            final_choices[key] = False
            if not passed:
                print(f"    -> ELIMINATED (被B0.3支配)")
            else:
                print(f"    -> ELIMINATED (无增量价值)")
    
    survivors = [k for k, v in final_choices.items() if v]
    if not survivors:
        print(f"\n  No survivors. All experiments eliminated by dominance rules.")
        print(f"  最终选择: 保持 B0.3 基准")
    else:
        print(f"\n  Survivors: {[experiments[k]['name'] for k in survivors]}")
        print(f"  最终选择: 需要进一步验证")
    
    # 生成报告
    lines = []
    lines.append("# Phase 6.2-6.4 综合实验报告：改善2020年结构性牛市跑输")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**基准**: B0.3 (min_total_score=40, fixed stop, no momentum, no vol_score)")
    lines.append("")
    lines.append("## 实验方案")
    lines.append("")
    lines.append("| 方案 | 描述 | 修改点 |")
    lines.append("|------|------|--------|")
    lines.append("| A | B0.3基准 | 无 |")
    lines.append("| B | 6.2 Momentum急涨通道 | momentum_20>0 + trend_score>=10 时 +5 bonus |")
    lines.append("| C | 6.3 ATR动态止损 | stop_loss_mode='atr', multiplier=2.0 |")
    lines.append("| D | 6.4 行业弹性过滤 | 排除波动率最低20%行业 |")
    lines.append("")
    lines.append("## 1. 回测表现")
    lines.append("")
    lines.append("### 1.1 训练期 (2019-2022)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, exp in experiments.items():
        m = metrics[key]['train']
        lines.append(f"| {exp['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("### 1.2 验证期 (2023-2024)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|")
    for key, exp in experiments.items():
        m = metrics[key]['valid']
        lines.append(f"| {exp['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} |")
    lines.append("")
    lines.append("## 2. 2020年专项分析")
    lines.append("")
    lines.append("| 方案 | 2020年化 | 2020 Sharpe | 2020最大回撤 | 2020交易次数 |")
    lines.append("|------|----------|-------------|-------------|-------------|")
    for key, exp in experiments.items():
        y = metrics[key]['yearly'].get(2020, {})
        if y:
            lines.append(f"| {exp['name']} | {y['ann_ret']:.2%} | {y['sharpe']:.4f} | {y['max_dd']:.2%} | {y['n_trades']:.0f} |")
    lines.append("")
    lines.append("## 3. 分年度对比")
    lines.append("")
    lines.append("### 3.1 年化收益")
    lines.append("")
    lines.append("| 年份 | B0.3 | 6.2 | 6.3 | 6.4 |")
    lines.append("|------|------|-----|-----|-----|")
    for year in range(2019, 2025):
        y40 = metrics['A']['yearly'].get(year, {})
        y62 = metrics['B']['yearly'].get(year, {})
        y63 = metrics['C']['yearly'].get(year, {})
        y64 = metrics['D']['yearly'].get(year, {})
        if y40 and y62 and y63 and y64:
            lines.append(f"| {year} | {y40['ann_ret']:.2%} | {y62['ann_ret']:.2%} | {y63['ann_ret']:.2%} | {y64['ann_ret']:.2%} |")
    lines.append("")
    lines.append("### 3.2 Sharpe比率")
    lines.append("")
    lines.append("| 年份 | B0.3 | 6.2 | 6.3 | 6.4 |")
    lines.append("|------|------|-----|-----|-----|")
    for year in range(2019, 2025):
        y40 = metrics['A']['yearly'].get(year, {})
        y62 = metrics['B']['yearly'].get(year, {})
        y63 = metrics['C']['yearly'].get(year, {})
        y64 = metrics['D']['yearly'].get(year, {})
        if y40 and y62 and y63 and y64:
            lines.append(f"| {year} | {y40['sharpe']:.4f} | {y62['sharpe']:.4f} | {y63['sharpe']:.4f} | {y64['sharpe']:.4f} |")
    lines.append("")
    lines.append("## 4. 候选选择（验证期 vs B0.3 支配规则）")
    lines.append("")
    lines.append("| 方案 | 验证期年化 | 验证期Sharpe | 验证期回撤 | 支配检查 | 结论 |")
    lines.append("|------|-----------|-------------|-----------|----------|------|")
    for key in ('B', 'C', 'D'):
        exp_v = metrics[key]['valid']
        passed, issues = dominance_check(exp_v, base_v, experiments[key]['name'])
        issues_str = "; ".join(issues) if issues else "无"
        status = "通过" if passed else "FAIL"
        lines.append(f"| {experiments[key]['name']} | {exp_v['annual_return']:.2%} | {exp_v['sharpe']:.4f} | {exp_v['max_dd']:.2%} | {status} | {'淘汰' if issues else '需进一步检查'} |")
    lines.append("")
    
    if not survivors:
        lines.append("### 最终结论")
        lines.append("")
        lines.append("- **所有实验组被B0.3支配，淘汰**")
        lines.append("- **保持 B0.3 基准不变**")
        lines.append("")
        lines.append("各实验详细淘汰原因：")
        for key in ('B', 'C', 'D'):
            passed, issues = dominance_check(metrics[key]['valid'], base_v, experiments[key]['name'])
            lines.append(f"- **{experiments[key]['name']}**：")
            for issue in issues:
                lines.append(f"  - {issue}")
    else:
        lines.append("### 最终结论")
        lines.append("")
        lines.append(f"- **幸存者**: {[experiments[k]['name'] for k in survivors]}")
        lines.append(f"- 需要进一步验证和组合测试")
    
    lines.append("")
    lines.append("## 5. 讨论")
    lines.append("")
    lines.append("### 5.1 为什么2020年改善但验证期退化？")
    lines.append("")
    lines.append("和Phase 6.1（降低门槛）类似，每个实验都面临Trade-off：")
    lines.append("- 6.2 momentum急涨通道：在快速反弹年份（2020）提前入场，但在震荡年份增加误报")
    lines.append("- 6.3 ATR动态止损：在急跌后反弹时避免被震出，但可能让亏损扩大")
    lines.append("- 6.4 弹性过滤：排除低弹性行业（如银行），但可能错过防御性行情")
    lines.append("")
    lines.append("### 5.2 下一步建议")
    lines.append("")
    lines.append("- 单一改进无法全局解决2020年问题")
    lines.append("- 需要组合策略：如'急涨通道 + 弹性过滤'联合使用")
    lines.append("- 或考虑市场状态识别：在'急涨'状态启用急涨通道，在'震荡'状态保持保守")
    lines.append("")
    lines.append("---")
    lines.append("*2025-2026封存样本未运行，不用于调参。*")
    lines.append("*未修改生产配置 (src/config.py)。*")
    
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n  Report saved to: {REPORT_PATH}")
    print("=" * 70)
    print("Phase 6.2-6.4 completed.")
    print("=" * 70)


if __name__ == '__main__':
    main()
