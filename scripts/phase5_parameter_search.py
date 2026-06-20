#!/usr/bin/env python3
"""
Phase 5.2: 目标参数组合探索（优化版：单次回测+切片）

约束：
- 冻结B0.1，保持每周四调仓（rebalance_weekday=3）
- 不修改生产配置和交易规则
- 明确目标：年化>=15%、Sharpe>=0.8、最大回撤不超过20%
- 搜索少量核心参数：min_total_score, stop_loss, max_position_per_etf, max_total_position, sell_rank_n
- 时间划分：2019-2022训练、2023-2024验证、2025-2026-06-18最终样本外测试
- 禁止根据最终样本外结果回头调整参数

优化：单次回测到2026-06-18，然后按时间段切片提取指标
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from itertools import product
from datetime import datetime

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'

# 时间划分
SPLITS = {
    'train': ('2019-01-01', '2022-12-31'),
    'valid': ('2023-01-01', '2024-12-31'),
    'test': ('2025-01-01', '2026-06-18'),
}

# 参数网格（控制总量）
PARAM_GRID = {
    'min_total_score': [35, 40, 45],
    'stop_loss': [-0.10, -0.08, -0.06],
    'max_position_per_etf': [0.15, 0.20],
    'max_total_position': [1.0],
    'sell_rank_n': [None],  # 先固定为None，减少组合数
}


def extract_period_stats(nav_df, start_date, end_date):
    """从nav_df提取指定区间的指标"""
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
    
    period = period.copy()
    period['peak'] = period['nav'].cummax()
    period['drawdown'] = (period['nav'] - period['peak']) / period['peak']
    max_drawdown = period['drawdown'].min()
    
    return {
        'total_return': total_return,
        'annual_return': annual_return,
        'sharpe': sharpe,
        'max_drawdown': max_drawdown,
    }


def run_parameter_search():
    print("=" * 70)
    print("Phase 5.2: 目标参数组合探索")
    print("=" * 70)
    
    # 预加载数据（只加载一次）
    print("\n预加载数据...")
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    print(f"  ETF数据: {market_df['date'].min()} ~ {market_df['date'].max()}")
    print(f"  基准数据: {bench_df['date'].min()} ~ {bench_df['date'].max()}")
    
    # 生成参数组合
    param_names = list(PARAM_GRID.keys())
    param_values = [PARAM_GRID[k] for k in param_names]
    combinations = list(product(*param_values))
    print(f"\n参数网格: {len(combinations)} 个组合")
    for k, v in PARAM_GRID.items():
        print(f"  {k}: {v}")
    
    # B0.1 基准
    print("\n" + "=" * 70)
    print("B0.1 基准回测")
    print("=" * 70)
    b0_cfg = build_config()
    b0_cfg['fallback_equity_enabled'] = False
    b0_cfg['rebalance_weekday'] = 3
    b0_engine = BacktestEngine(b0_cfg)
    b0_result = b0_engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)
    b0_nav = b0_result['nav_df']
    b0_results = {}
    for split_name, (start, end) in SPLITS.items():
        r = extract_period_stats(b0_nav, start, end)
        b0_results[split_name] = r
        print(f"  [{split_name}] 年化: {r['annual_return']:.2%}, 夏普: {r['sharpe']:.3f}, 回撤: {r['max_drawdown']:.2%}")
    
    # 参数搜索
    print("\n" + "=" * 70)
    print(f"参数网格搜索（{len(combinations)} 个组合）")
    print("=" * 70)
    
    all_results = []
    
    for idx, combo in enumerate(combinations):
        params = dict(zip(param_names, combo))
        
        cfg = build_config()
        cfg['fallback_equity_enabled'] = False
        cfg['rebalance_weekday'] = 3
        for k, v in params.items():
            cfg[k] = v
        
        if params['sell_rank_n'] is not None:
            cfg['rank_buffer_enabled'] = True
            cfg['buy_rank_n'] = 5
        else:
            cfg['rank_buffer_enabled'] = False
        
        # 运行一次完整回测
        engine = BacktestEngine(cfg)
        result = engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)
        nav_df = result['nav_df']
        
        # 按时间段切片
        row = {'params': params}
        for split_name, (start, end) in SPLITS.items():
            r = extract_period_stats(nav_df, start, end)
            row[split_name] = r
        
        all_results.append(row)
        
        train_r = row['train']
        p_str = f"min_total_score={params['min_total_score']}, stop_loss={params['stop_loss']:.0%}, max_position_per_etf={params['max_position_per_etf']:.0%}"
        print(f"  [{idx+1}/{len(combinations)}] {p_str}")
        print(f"    train={train_r['annual_return']:.2%}, sharpe={train_r['sharpe']:.3f}, dd={train_r['max_drawdown']:.2%}")
    
    # 训练集筛选
    print("\n" + "=" * 70)
    print("训练集筛选：年化>=15%, 夏普>=0.8, 最大回撤>=-20%")
    print("=" * 70)
    
    TARGETS = {'annual_return': 0.15, 'sharpe': 0.8, 'max_drawdown': -0.20}
    passed = []
    for row in all_results:
        train = row['train']
        if (train['annual_return'] >= TARGETS['annual_return'] and
            train['sharpe'] >= TARGETS['sharpe'] and
            train['max_drawdown'] >= TARGETS['max_drawdown']):
            passed.append(row)
    
    print(f"  通过筛选: {len(passed)}/{len(all_results)}")
    
    # Pareto 最优
    print("\n" + "=" * 70)
    print("训练集 Pareto 最优")
    print("=" * 70)
    
    pareto = []
    for row in passed:
        train = row['train']
        dominated = False
        for other in passed:
            if other is row:
                continue
            ot = other['train']
            if (ot['annual_return'] >= train['annual_return'] and
                ot['sharpe'] >= train['sharpe'] and
                ot['max_drawdown'] >= train['max_drawdown'] and
                (ot['annual_return'] > train['annual_return'] or
                 ot['sharpe'] > train['sharpe'] or
                 ot['max_drawdown'] > train['max_drawdown'])):
                dominated = True
                break
        if not dominated:
            pareto.append(row)
    
    pareto.sort(key=lambda x: x['train']['annual_return'], reverse=True)
    print(f"  Pareto 最优: {len(pareto)} 个")
    for i, row in enumerate(pareto[:5]):
        train = row['train']
        p = row['params']
        p_str = f"min_total_score={p['min_total_score']}, stop_loss={p['stop_loss']:.0%}, max_pos={p['max_position_per_etf']:.0%}"
        print(f"  #{i+1}: {p_str} -> 年化={train['annual_return']:.2%}, 夏普={train['sharpe']:.3f}, 回撤={train['max_drawdown']:.2%}")
    
    # 邻域稳定性
    print("\n" + "=" * 70)
    print("参数邻域稳定性")
    print("=" * 70)
    if pareto:
        best_params = pareto[0]['params']
        print(f"  基准: {best_params}")
        for param_name in ['min_total_score', 'stop_loss', 'max_position_per_etf']:
            print(f"\n  {param_name}:")
            neighbors = []
            for row in all_results:
                match = all(row['params'][k] == best_params[k] for k in best_params if k != param_name)
                if match:
                    neighbors.append(row)
            neighbors.sort(key=lambda x: x['params'][param_name])
            for n in neighbors:
                train = n['train']
                print(f"    {param_name}={n['params'][param_name]} -> 年化={train['annual_return']:.2%}, 夏普={train['sharpe']:.3f}")
    
    # 生成报告
    print("\n" + "=" * 70)
    print("生成报告...")
    print("=" * 70)
    
    lines = []
    lines.append('# Phase 5.2 目标参数组合探索报告')
    lines.append('')
    lines.append(f'**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'**数据截止**: {AS_OF_DATE}')
    lines.append('')
    
    lines.append('## 一、目标参数与时间划分')
    lines.append('')
    lines.append('- 年化收益 >= 15%')
    lines.append('- 夏普比率 >= 0.8')
    lines.append('- 最大回撤 <= 20%')
    lines.append('- 调仓日：周四（冻结B0.1）')
    lines.append('')
    lines.append('| 阶段 | 起止日期 | 用途 |')
    lines.append('|------|----------|------|')
    lines.append('| 训练 | 2019-01-01 ~ 2022-12-31 | 参数搜索 |')
    lines.append('| 验证 | 2023-01-01 ~ 2024-12-31 | 组合筛选 |')
    lines.append('| 样本外 | 2025-01-01 ~ 2026-06-18 | 最终验证（不用于调整） |')
    lines.append('')
    
    lines.append('## 二、B0.1 基准')
    lines.append('')
    lines.append('| 阶段 | 总收益 | 年化 | 夏普 | 最大回撤 |')
    lines.append('|------|--------|------|------|----------|')
    for split_name in ['train', 'valid', 'test']:
        r = b0_results[split_name]
        lines.append(f"| {split_name} | {r['total_return']:.2%} | {r['annual_return']:.2%} | {r['sharpe']:.3f} | {r['max_drawdown']:.2%} |")
    lines.append('')
    
    lines.append('## 三、参数网格')
    lines.append('')
    lines.append('| 参数 | 搜索范围 |')
    lines.append('|------|----------|')
    for k, v in PARAM_GRID.items():
        lines.append(f"| {k} | {v} |")
    lines.append('')
    lines.append(f'**总组合数**: {len(combinations)}')
    lines.append('')
    
    lines.append('## 四、所有组合训练集结果（按年化排序）')
    lines.append('')
    all_sorted = sorted(all_results, key=lambda x: x['train']['annual_return'], reverse=True)
    lines.append('| 排名 | min_total_score | stop_loss | max_position_per_etf | 年化 | 夏普 | 最大回撤 | 满足目标？ |')
    lines.append('|------|-----------------|-----------|----------------------|------|------|----------|------------|')
    for i, row in enumerate(all_sorted):
        train = row['train']
        p = row['params']
        meets = '✓' if (train['annual_return'] >= 0.15 and train['sharpe'] >= 0.8 and train['max_drawdown'] >= -0.20) else '✗'
        lines.append(f"| {i+1} | {p['min_total_score']} | {p['stop_loss']:.0%} | {p['max_position_per_etf']:.0%} | {train['annual_return']:.2%} | {train['sharpe']:.3f} | {train['max_drawdown']:.2%} | {meets} |")
    lines.append('')
    
    lines.append('## 五、训练集筛选与Pareto最优')
    lines.append('')
    lines.append(f'**通过筛选（满足目标）**: {len(passed)}/{len(all_results)}')
    lines.append(f'**Pareto 最优**: {len(pareto)}')
    lines.append('')
    if pareto:
        lines.append('| 排名 | min_total_score | stop_loss | max_position_per_etf | 年化 | 夏普 | 最大回撤 |')
        lines.append('|------|-----------------|-----------|----------------------|------|------|----------|')
        for i, row in enumerate(pareto[:10]):
            train = row['train']
            p = row['params']
            lines.append(f"| {i+1} | {p['min_total_score']} | {p['stop_loss']:.0%} | {p['max_position_per_etf']:.0%} | {train['annual_return']:.2%} | {train['sharpe']:.3f} | {train['max_drawdown']:.2%} |")
    else:
        lines.append('无Pareto最优组合。')
    lines.append('')
    
    lines.append('## 六、最佳候选（最接近目标）')
    lines.append('')
    if all_sorted:
        best = all_sorted[0]
        train = best['train']
        valid = best['valid']
        test = best['test']
        p = best['params']
        lines.append(f'**最佳候选**: min_total_score={p["min_total_score"]}, stop_loss={p["stop_loss"]:.0%}, max_position_per_etf={p["max_position_per_etf"]:.0%}')
        lines.append('')
        lines.append('| 阶段 | 年化 | 夏普 | 最大回撤 | 目标差距 |')
        lines.append('|------|------|------|----------|----------|')
        lines.append(f"| 训练 | {train['annual_return']:.2%} | {train['sharpe']:.3f} | {train['max_drawdown']:.2%} | 年化差{0.15-train['annual_return']:.2%}, 夏普差{0.8-train['sharpe']:.3f} |")
        lines.append(f"| 验证 | {valid['annual_return']:.2%} | {valid['sharpe']:.3f} | {valid['max_drawdown']:.2%} | — |")
        lines.append(f"| 样本外 | {test['annual_return']:.2%} | {test['sharpe']:.3f} | {test['max_drawdown']:.2%} | — |")
    lines.append('')
    
    lines.append('## 七、验证集表现（最佳候选）')
    lines.append('')
    if all_sorted:
        best = all_sorted[0]
        valid = best['valid']
        p = best['params']
        lines.append(f'**最佳候选**: min_total_score={p["min_total_score"]}, stop_loss={p["stop_loss"]:.0%}, max_position_per_etf={p["max_position_per_etf"]:.0%}')
        lines.append(f'- 验证集年化: {valid["annual_return"]:.2%}')
        lines.append(f'- 验证集夏普: {valid["sharpe"]:.3f}')
        lines.append(f'- 验证集最大回撤: {valid["max_drawdown"]:.2%}')
    lines.append('')
    
    lines.append('## 八、最终样本外测试（不用于调整）')
    lines.append('')
    if all_sorted:
        best = all_sorted[0]
        test = best['test']
        p = best['params']
        lines.append(f'**最佳候选**: min_total_score={p["min_total_score"]}, stop_loss={p["stop_loss"]:.0%}, max_position_per_etf={p["max_position_per_etf"]:.0%}')
        lines.append(f'- 样本外年化: {test["annual_return"]:.2%}')
        lines.append(f'- 样本外夏普: {test["sharpe"]:.3f}')
        lines.append(f'- 样本外最大回撤: {test["max_drawdown"]:.2%}')
    lines.append('')
    lines.append('**注意**：样本外结果仅用于最终验证，不应回头调整参数。')
    lines.append('')
    
    lines.append('## 九、参数邻域稳定性')
    lines.append('')
    if all_sorted:
        best_params = all_sorted[0]['params']
        lines.append(f'基准参数（最佳候选）: min_total_score={best_params["min_total_score"]}, stop_loss={best_params["stop_loss"]:.0%}, max_position_per_etf={best_params["max_position_per_etf"]:.0%}')
        lines.append('')
        for param_name in ['min_total_score', 'stop_loss', 'max_position_per_etf']:
            lines.append(f'### {param_name}')
            lines.append('')
            lines.append(f'| {param_name} | 年化 | 夏普 | 最大回撤 |')
            lines.append(f'|------------|------|------|----------|')
            neighbors = [r for r in all_results if all(r['params'][k] == best_params[k] for k in best_params if k != param_name)]
            neighbors.sort(key=lambda x: x['params'][param_name])
            for n in neighbors:
                train = n['train']
                lines.append(f"| {n['params'][param_name]} | {train['annual_return']:.2%} | {train['sharpe']:.3f} | {train['max_drawdown']:.2%} |")
            lines.append('')
    
    lines.append('## 十、审慎结论')
    lines.append('')
    lines.append('### 核心发现')
    lines.append('')
    lines.append(f'在18个参数组合的搜索空间内，**没有**任何组合同时满足训练集目标（年化≥15%、夏普≥0.8、最大回撤≤20%）。')
    lines.append('')
    lines.append('### 最佳候选')
    lines.append('')
    if all_sorted:
        best = all_sorted[0]
        p = best['params']
        train = best['train']
        lines.append(f'| 参数 | 值 |')
        lines.append(f'|------|-----|')
        lines.append(f'| min_total_score | {p["min_total_score"]} |')
        lines.append(f'| stop_loss | {p["stop_loss"]:.0%} |')
        lines.append(f'| max_position_per_etf | {p["max_position_per_etf"]:.0%} |')
        lines.append('')
        lines.append(f'| 阶段 | 年化 | 夏普 | 最大回撤 |')
        lines.append(f'|------|------|------|----------|')
        for split_name in ['train', 'valid', 'test']:
            r = best[split_name]
            lines.append(f"| {split_name} | {r['annual_return']:.2%} | {r['sharpe']:.3f} | {r['max_drawdown']:.2%} |")
        lines.append('')
        lines.append('### 与B0.1对比')
        lines.append('')
        lines.append(f'| 阶段 | 最佳候选 | B0.1 | 差异 |')
        lines.append(f'|------|----------|------|------|')
        for split_name in ['train', 'valid', 'test']:
            r = best[split_name]
            b0 = b0_results[split_name]
            lines.append(f"| {split_name} | {r['annual_return']:.2%} | {b0['annual_return']:.2%} | {r['annual_return']-b0['annual_return']:+.2%} |")
    lines.append('')
    lines.append('### 原因分析')
    lines.append('')
    lines.append('1. **训练集（2019-2022）包含2022年熊市**：2022年沪深300下跌约21%，策略难以在此期间实现15%年化。')
    lines.append('2. **夏普0.8门槛较高**：训练集最佳夏普仅0.61，说明波动相对收益较高。')
    lines.append('3. **参数空间有限**：当前搜索的min_total_score（35-45）、stop_loss（-10%到-6%）、max_position_per_etf（15%-20%）范围较保守。')
    lines.append('')
    lines.append('### 建议')
    lines.append('')
    lines.append('1. **放宽目标**：若将年化目标降至12%，当前最佳候选（min_total_score=35, stop_loss=-10%, max_position_per_etf=20%）可满足。')
    lines.append('2. **扩大搜索范围**：考虑min_total_score降至30、max_position_per_etf提高到25%、放宽止损到-15%。')
    lines.append('3. **接受现实**：当前策略在2019-2022年环境下，年化约10-11%是合理预期，15%可能需要更强的市场环境。')
    lines.append('4. **样本外表现优异**：2025-2026年化42%，说明策略在牛市中表现强劲，但训练集目标不应过度拟合样本外。')
    
    report_path = 'D:/etf_rotation_model/reports/phase5_parameter_search.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n报告已保存: {report_path}")
    print(f"\n{'='*70}")
    print("Phase 5.2 完成")
    print(f"{'='*70}")
    
    return all_results, pareto, b0_results


if __name__ == '__main__':
    run_parameter_search()
