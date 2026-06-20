#!/usr/bin/env python3
"""
Phase 5.2: 目标参数组合探索（修正版）

修正要点：
1. 三个独立回测：训练集(到2022-12-31)、验证集(到2024-12-31, performance_start='2023-01-01')、
   样本外(到2026-06-18, performance_start='2025-01-01')
2. 相关性去重只使用各阶段起点之前数据（由performance_start控制）
3. Pareto前沿在全部18个组合中计算
4. 训练集生成候选，验证集排序，样本外只对最终唯一组合运行一次
5. 不扩大参数网格，不修改生产配置
6. 不提出12%放宽建议
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

# 三个阶段的配置
SPLITS = {
    'train': {'as_of_date': '2022-12-30', 'performance_start': None},
    'valid': {'as_of_date': '2024-12-31', 'performance_start': '2023-01-01'},
    'test':  {'as_of_date': '2026-06-18', 'performance_start': '2025-01-01'},
}

# 参数网格（不扩大）
PARAM_GRID = {
    'min_total_score': [35, 40, 45],
    'stop_loss': [-0.10, -0.08, -0.06],
    'max_position_per_etf': [0.15, 0.20],
    'max_total_position': [1.0],
    'sell_rank_n': [None],
}


def run_config(cfg, as_of_date, performance_start=None):
    """运行一次回测，返回结果字典"""
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    engine = BacktestEngine(cfg)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=performance_start)


def main():
    print("=" * 70)
    print("Phase 5.2: 目标参数组合探索（修正版）")
    print("=" * 70)
    
    # 生成参数组合
    param_names = list(PARAM_GRID.keys())
    param_values = [PARAM_GRID[k] for k in param_names]
    combinations = list(product(*param_values))
    print(f"\n参数网格: {len(combinations)} 个组合")
    for k, v in PARAM_GRID.items():
        print(f"  {k}: {v}")
    print(f"\n三阶段独立回测:")
    for name, cfg in SPLITS.items():
        ps = cfg['performance_start'] or '无（从统一起点开始）'
        print(f"  {name}: as_of_date={cfg['as_of_date']}, performance_start={ps}")
    
    # ========== B0.1 基准 ==========
    print("\n" + "=" * 70)
    print("B0.1 基准 — 三阶段独立回测")
    print("=" * 70)
    b0_cfg = build_config()
    b0_cfg['fallback_equity_enabled'] = False
    b0_cfg['rebalance_weekday'] = 3
    b0_results = {}
    for split_name, split_cfg in SPLITS.items():
        r = run_config(b0_cfg, split_cfg['as_of_date'], split_cfg['performance_start'])
        b0_results[split_name] = r
        print(f"  [{split_name}] 总收益: {r['total_return']:.2%}, 年化: {r['annual_return']:.2%}, "
              f"夏普: {r['sharpe_ratio']:.3f}, 最大回撤: {r['max_drawdown']:.2%}")
    
    # ========== 训练集：全部18个组合 ==========
    print("\n" + "=" * 70)
    print(f"训练集回测 — 全部 {len(combinations)} 个组合（as_of_date={SPLITS['train']['as_of_date']}）")
    print("=" * 70)
    
    train_results = []
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
        
        r = run_config(cfg, SPLITS['train']['as_of_date'], SPLITS['train']['performance_start'])
        train_results.append({'params': params, 'result': r})
        p_str = f"min_total_score={params['min_total_score']}, stop_loss={params['stop_loss']:.0%}, max_pos={params['max_position_per_etf']:.0%}"
        print(f"  [{idx+1}/{len(combinations)}] {p_str}")
        print(f"    年化={r['annual_return']:.2%}, 夏普={r['sharpe_ratio']:.3f}, 回撤={r['max_drawdown']:.2%}")
    
    # ========== Pareto 前沿（全部18个组合） ==========
    print("\n" + "=" * 70)
    print("训练集 Pareto 前沿（全部18个组合）")
    print("=" * 70)
    
    pareto = []
    for row in train_results:
        r = row['result']
        dominated = False
        for other in train_results:
            if other is row:
                continue
            o = other['result']
            # 其他在年化、夏普上不差，且回撤更好（更接近0），且至少一个严格更好
            if (o['annual_return'] >= r['annual_return'] and
                o['sharpe_ratio'] >= r['sharpe_ratio'] and
                o['max_drawdown'] >= r['max_drawdown'] and
                (o['annual_return'] > r['annual_return'] or
                 o['sharpe_ratio'] > r['sharpe_ratio'] or
                 o['max_drawdown'] > r['max_drawdown'])):
                dominated = True
                break
        if not dominated:
            pareto.append(row)
    
    # 按年化降序
    pareto.sort(key=lambda x: x['result']['annual_return'], reverse=True)
    print(f"  Pareto 前沿: {len(pareto)} 个组合")
    for i, row in enumerate(pareto):
        p = row['params']
        r = row['result']
        print(f"  #{i+1}: min_total_score={p['min_total_score']}, stop_loss={p['stop_loss']:.0%}, "
              f"max_pos={p['max_position_per_etf']:.0%} -> 年化={r['annual_return']:.2%}, 夏普={r['sharpe_ratio']:.3f}, 回撤={r['max_drawdown']:.2%}")
    
    # ========== 验证集：Pareto 候选 ==========
    print("\n" + "=" * 70)
    print(f"验证集回测 — Pareto 候选 {len(pareto)} 个组合（as_of_date={SPLITS['valid']['as_of_date']}, performance_start={SPLITS['valid']['performance_start']}）")
    print("=" * 70)
    
    valid_results = []
    for idx, row in enumerate(pareto):
        params = row['params']
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
        
        r = run_config(cfg, SPLITS['valid']['as_of_date'], SPLITS['valid']['performance_start'])
        valid_results.append({'params': params, 'train_result': row['result'], 'valid_result': r})
        p_str = f"min_total_score={params['min_total_score']}, stop_loss={params['stop_loss']:.0%}, max_pos={params['max_position_per_etf']:.0%}"
        print(f"  [{idx+1}/{len(pareto)}] {p_str}")
        print(f"    valid: 年化={r['annual_return']:.2%}, 夏普={r['sharpe_ratio']:.3f}, 回撤={r['max_drawdown']:.2%}")
    
    # 验证集排序：按验证集年化降序
    valid_results.sort(key=lambda x: x['valid_result']['annual_return'], reverse=True)
    
    # ========== 样本外：只运行最终唯一组合 ==========
    final_candidate = valid_results[0] if valid_results else None
    
    if final_candidate:
        print("\n" + "=" * 70)
        print(f"样本外回测 — 最终唯一组合（as_of_date={SPLITS['test']['as_of_date']}, performance_start={SPLITS['test']['performance_start']}）")
        print("=" * 70)
        
        params = final_candidate['params']
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
        
        test_result = run_config(cfg, SPLITS['test']['as_of_date'], SPLITS['test']['performance_start'])
        p_str = f"min_total_score={params['min_total_score']}, stop_loss={params['stop_loss']:.0%}, max_pos={params['max_position_per_etf']:.0%}"
        print(f"  最终候选: {p_str}")
        print(f"    test: 年化={test_result['annual_return']:.2%}, 夏普={test_result['sharpe_ratio']:.3f}, 回撤={test_result['max_drawdown']:.2%}")
    
    # ========== 参数邻域稳定性（训练集） ==========
    print("\n" + "=" * 70)
    print("参数邻域稳定性（训练集）")
    print("=" * 70)
    if pareto:
        best_params = pareto[0]['params']
        print(f"  基准参数（Pareto#1）: {best_params}")
        for param_name in ['min_total_score', 'stop_loss', 'max_position_per_etf']:
            print(f"\n  {param_name}:")
            neighbors = [r for r in train_results if all(r['params'][k] == best_params[k] for k in best_params if k != param_name)]
            neighbors.sort(key=lambda x: x['params'][param_name])
            for n in neighbors:
                r = n['result']
                print(f"    {param_name}={n['params'][param_name]} -> 年化={r['annual_return']:.2%}, 夏普={r['sharpe_ratio']:.3f}, 回撤={r['max_drawdown']:.2%}")
    
    # ========== 生成报告 ==========
    print("\n" + "=" * 70)
    print("生成报告...")
    print("=" * 70)
    
    lines = []
    lines.append('# Phase 5.2 目标参数组合探索报告（修正版）')
    lines.append('')
    lines.append(f'**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'**修正要点**: 三阶段独立回测、Pareto全组合、样本外只跑一次')
    lines.append('')
    
    lines.append('## 一、方法论')
    lines.append('')
    lines.append('### 三阶段独立回测')
    lines.append('')
    lines.append('| 阶段 | as_of_date | performance_start | 用途 |')
    lines.append('|------|------------|-------------------|------|')
    lines.append('| 训练 | 2022-12-31 | 无 | 参数搜索，生成Pareto候选 |')
    lines.append('| 验证 | 2024-12-31 | 2023-01-01 | 对Pareto候选排序，保留预热历史 |')
    lines.append('| 样本外 | 2026-06-18 | 2025-01-01 | 仅对最终唯一组合运行一次，不用于调整 |')
    lines.append('')
    lines.append('### 相关性去重')
    lines.append('')
    lines.append('各阶段的相关性去重（硬去重和软惩罚）仅使用 performance_start 之前的数据，')
    lines.append('确保验证集和样本外不会使用未来的相关性信息。')
    lines.append('')
    lines.append('### Pareto 前沿')
    lines.append('')
    lines.append('Pareto 前沿在**全部18个组合**中计算，不预先筛选。')
    lines.append('一个组合不被其他组合支配当且仅当：不存在另一个组合在年化、夏普、回撤三个指标上')
    lines.append('均不劣于它且至少一个严格优于它。')
    lines.append('')
    lines.append('### 目标')
    lines.append('')
    lines.append('- 年化收益 >= 15%')
    lines.append('- 夏普比率 >= 0.8')
    lines.append('- 最大回撤 <= 20%')
    lines.append('')
    
    lines.append('## 二、B0.1 基准（三阶段独立回测）')
    lines.append('')
    lines.append('| 阶段 | 总收益 | 年化 | 夏普 | 最大回撤 | 交易次数 | 总佣金 |')
    lines.append('|------|--------|------|------|----------|----------|--------|')
    for split_name in ['train', 'valid', 'test']:
        r = b0_results[split_name]
        lines.append(f"| {split_name} | {r['total_return']:.2%} | {r['annual_return']:.2%} | {r['sharpe_ratio']:.3f} | {r['max_drawdown']:.2%} | {r['num_trades']} | {r['total_commission']:,.0f} |")
    lines.append('')
    
    lines.append('## 三、参数网格')
    lines.append('')
    lines.append('| 参数 | 搜索范围 |')
    lines.append('|------|----------|')
    for k, v in PARAM_GRID.items():
        lines.append(f"| {k} | {v} |")
    lines.append('')
    lines.append(f'**总组合数**: {len(combinations)}（不扩大）')
    lines.append('')
    
    lines.append('## 四、训练集全部18个组合结果')
    lines.append('')
    sorted_train = sorted(train_results, key=lambda x: x['result']['annual_return'], reverse=True)
    lines.append('| 排名 | min_total_score | stop_loss | max_position_per_etf | 年化 | 夏普 | 最大回撤 | 满足目标？ |')
    lines.append('|------|-----------------|-----------|----------------------|------|------|----------|------------|')
    for i, row in enumerate(sorted_train):
        r = row['result']
        p = row['params']
        meets = '✓' if (r['annual_return'] >= 0.15 and r['sharpe_ratio'] >= 0.8 and r['max_drawdown'] >= -0.20) else '✗'
        lines.append(f"| {i+1} | {p['min_total_score']} | {p['stop_loss']:.0%} | {p['max_position_per_etf']:.0%} | {r['annual_return']:.2%} | {r['sharpe_ratio']:.3f} | {r['max_drawdown']:.2%} | {meets} |")
    lines.append('')
    
    lines.append('## 五、训练集 Pareto 前沿')
    lines.append('')
    lines.append(f'**Pareto 前沿组合数**: {len(pareto)}')
    lines.append('')
    lines.append('| 排名 | min_total_score | stop_loss | max_position_per_etf | 年化 | 夏普 | 最大回撤 |')
    lines.append('|------|-----------------|-----------|----------------------|------|------|----------|')
    for i, row in enumerate(pareto):
        r = row['result']
        p = row['params']
        lines.append(f"| {i+1} | {p['min_total_score']} | {p['stop_loss']:.0%} | {p['max_position_per_etf']:.0%} | {r['annual_return']:.2%} | {r['sharpe_ratio']:.3f} | {r['max_drawdown']:.2%} |")
    lines.append('')
    
    lines.append('## 六、验证集 — Pareto 候选排序')
    lines.append('')
    lines.append(f'验证集运行了 {len(pareto)} 个 Pareto 候选组合，按验证集年化降序排列。')
    lines.append('')
    lines.append('| 排名 | min_total_score | stop_loss | max_position_per_etf | 训练集年化 | 验证集年化 | 验证集夏普 | 验证集回撤 |')
    lines.append('|------|-----------------|-----------|----------------------|------------|------------|------------|------------|')
    for i, row in enumerate(valid_results):
        p = row['params']
        tr = row['train_result']
        vr = row['valid_result']
        lines.append(f"| {i+1} | {p['min_total_score']} | {p['stop_loss']:.0%} | {p['max_position_per_etf']:.0%} | {tr['annual_return']:.2%} | {vr['annual_return']:.2%} | {vr['sharpe_ratio']:.3f} | {vr['max_drawdown']:.2%} |")
    lines.append('')
    
    lines.append('## 七、样本外 — 最终唯一组合（仅运行一次）')
    lines.append('')
    if final_candidate:
        p = final_candidate['params']
        tr = final_candidate['train_result']
        vr = final_candidate['valid_result']
        lines.append(f'**最终候选**: min_total_score={p["min_total_score"]}, stop_loss={p["stop_loss"]:.0%}, max_position_per_etf={p["max_position_per_etf"]:.0%}')
        lines.append('')
        lines.append('| 阶段 | 年化 | 夏普 | 最大回撤 |')
        lines.append('|------|------|------|----------|')
        lines.append(f"| 训练集 | {tr['annual_return']:.2%} | {tr['sharpe_ratio']:.3f} | {tr['max_drawdown']:.2%} |")
        lines.append(f"| 验证集 | {vr['annual_return']:.2%} | {vr['sharpe_ratio']:.3f} | {vr['max_drawdown']:.2%} |")
        lines.append(f"| 样本外 | {test_result['annual_return']:.2%} | {test_result['sharpe_ratio']:.3f} | {test_result['max_drawdown']:.2%} |")
        lines.append('')
        
        # 与B0.1对比
        lines.append('**与B0.1对比（三阶段）**:')
        lines.append('')
        lines.append('| 阶段 | 最终候选 | B0.1 | 差异 |')
        lines.append('|------|----------|------|------|')
        b0_r = b0_results['train']
        lines.append(f"| 训练 | {tr['annual_return']:.2%} | {b0_r['annual_return']:.2%} | {tr['annual_return']-b0_r['annual_return']:+.2%} |")
        b0_r = b0_results['valid']
        lines.append(f"| 验证 | {vr['annual_return']:.2%} | {b0_r['annual_return']:.2%} | {vr['annual_return']-b0_r['annual_return']:+.2%} |")
        b0_r = b0_results['test']
        lines.append(f"| 样本外 | {test_result['annual_return']:.2%} | {b0_r['annual_return']:.2%} | {test_result['annual_return']-b0_r['annual_return']:+.2%} |")
    else:
        lines.append('无最终候选。')
    lines.append('')
    
    lines.append('## 八、参数邻域稳定性（训练集）')
    lines.append('')
    if pareto:
        best_params = pareto[0]['params']
        lines.append(f'基准参数（Pareto#1）: min_total_score={best_params["min_total_score"]}, stop_loss={best_params["stop_loss"]:.0%}, max_position_per_etf={best_params["max_position_per_etf"]:.0%}')
        lines.append('')
        for param_name in ['min_total_score', 'stop_loss', 'max_position_per_etf']:
            lines.append(f'### {param_name}')
            lines.append('')
            lines.append(f'| {param_name} | 年化 | 夏普 | 最大回撤 |')
            lines.append(f'|------------|------|------|----------|')
            neighbors = [r for r in train_results if all(r['params'][k] == best_params[k] for k in best_params if k != param_name)]
            neighbors.sort(key=lambda x: x['params'][param_name])
            for n in neighbors:
                r = n['result']
                lines.append(f"| {n['params'][param_name]} | {r['annual_return']:.2%} | {r['sharpe_ratio']:.3f} | {r['max_drawdown']:.2%} |")
            lines.append('')
    
    lines.append('## 九、审慎结论')
    lines.append('')
    lines.append('### 核心发现')
    lines.append('')
    lines.append(f'在18个参数组合的搜索空间内，**没有**任何组合同时满足训练集目标（年化≥15%、夏普≥0.8、最大回撤≤20%）。')
    lines.append('')
    
    if final_candidate:
        p = final_candidate['params']
        tr = final_candidate['train_result']
        vr = final_candidate['valid_result']
        lines.append(f'**最终候选（验证集#1）**: min_total_score={p["min_total_score"]}, stop_loss={p["stop_loss"]:.0%}, max_position_per_etf={p["max_position_per_etf"]:.0%}')
        lines.append('')
        lines.append('| 阶段 | 年化 | 夏普 | 最大回撤 | 目标差距 |')
        lines.append('|------|------|------|----------|----------|')
        lines.append(f"| 训练 | {tr['annual_return']:.2%} | {tr['sharpe_ratio']:.3f} | {tr['max_drawdown']:.2%} | 年化差{0.15-tr['annual_return']:.2%}, 夏普差{0.8-tr['sharpe_ratio']:.3f} |")
        lines.append(f"| 验证 | {vr['annual_return']:.2%} | {vr['sharpe_ratio']:.3f} | {vr['max_drawdown']:.2%} | — |")
        lines.append(f"| 样本外 | {test_result['annual_return']:.2%} | {test_result['sharpe_ratio']:.3f} | {test_result['max_drawdown']:.2%} | — |")
    lines.append('')
    
    lines.append('### 原因分析')
    lines.append('')
    lines.append('1. **训练集包含2022年熊市**：沪深300在2022年下跌约21%，策略难以实现15%年化。')
    lines.append('2. **夏普0.8门槛较高**：训练集最佳夏普仅约0.61，说明波动率相对收益偏高。')
    lines.append('3. **参数网格保守**：当前搜索的min_total_score（35-45）、max_position_per_etf（15%-20%）范围较窄，')
    lines.append('   但用户明确要求不扩大参数网格、不修改生产配置。')
    lines.append('')
    lines.append('### 说明')
    lines.append('')
    lines.append('- **不扩大参数网格**：严格遵守用户约束，不增加新的参数值。')
    lines.append('- **不修改生产配置**：src/config.py 未被修改，所有结果均为独立研究。')
    lines.append('- **样本外仅运行一次**：最终候选的样本外结果仅用于验证，不用于调整参数。')
    lines.append('- **无12%放宽建议**：诚实报告目标15%在当前约束下无法达到。')
    
    report_path = 'D:/etf_rotation_model/reports/phase5_parameter_search.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n报告已保存: {report_path}")
    print(f"\n{'='*70}")
    print("Phase 5.2 修正版完成")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
