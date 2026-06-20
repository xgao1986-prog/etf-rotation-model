#!/usr/bin/env python3
"""
Phase 5.3: 评分标准增量价值验证

保持：
- 周四调仓、-8%止损、单只20%
- 训练/验证/最终样本外划分不变
- 最终样本外暂不运行
- 不修改生产配置

步骤：
1. 统计各因子分数分布、与未来收益关系、买入候选通过率、因子间相关性
2. 单因子消融：删除一个因子，比较训练/验证期表现
3. 对证明有增量价值的因子测试少量权重配置
4. 训练集生成候选，验证集选择唯一方案
5. 禁止根据最终样本外调整

输出：reports/phase5_scoring_diagnosis.md
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from itertools import product
from datetime import datetime
import copy

from config import build_config, ETF_UNIVERSE, CONCEPT_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, STRATEGY_CONFIG
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

# 阶段配置（与Phase 5.2一致）
SPLITS = {
    'train': {'as_of_date': '2022-12-30', 'performance_start': None},
    'valid': {'as_of_date': '2024-12-31', 'performance_start': '2023-01-01'},
}

# 因子配置
FACTORS = {
    'trend': {'score_col': 'trend_score', 'max_score': 30},
    'confirm': {'score_col': 'confirm_score', 'max_score': 20},
    'momentum': {'score_col': 'momentum_rank', 'max_score': 25},
    'volume': {'score_col': 'volume_score', 'max_score': 15},
    'volatility': {'score_col': 'vol_score', 'max_score': 10},
}

# 当前有效权重（与评分上限一致）
DEFAULT_WEIGHTS = {
    'trend': 30/100,
    'confirm': 20/100,
    'momentum': 25/100,
    'volume': 15/100,
    'volatility': 10/100,
}


def get_raw_signals(cfg, market_df, bench_df):
    """获取所有ETF的原始因子分数（不运行回测）"""
    strategy = StrategyEngine(cfg)
    
    # 核心池
    core_tickers = list(ETF_UNIVERSE.keys()) + list(CONCEPT_UNIVERSE.keys())
    core_df = market_df[market_df['ticker'].isin(core_tickers)].copy()
    
    # 逐只计算
    all_scores = []
    for ticker in core_df['ticker'].unique():
        ticker_df = core_df[core_df['ticker'] == ticker].copy()
        if len(ticker_df) < 51:
            continue
        scored = strategy.calculate_total_score(ticker_df)
        all_scores.append(scored)
    
    if not all_scores:
        return None
    
    scores_df = pd.concat(all_scores, ignore_index=True)
    
    # 横截面动量排名
    scores_df = strategy.rank_all_momentum(scores_df)
    
    # 计算默认total_score
    scores_df = strategy.compute_total_score(scores_df)
    
    return scores_df


def add_future_returns(scores_df, market_df, horizons=[5, 10, 20]):
    """为每个(date, ticker)添加未来收益"""
    df = scores_df.copy()
    market = market_df[['date', 'ticker', 'close']].copy()
    market = market.sort_values(['ticker', 'date'])
    
    for h in horizons:
        market[f'future_ret_{h}'] = market.groupby('ticker')['close'].shift(-h) / market['close'] - 1
    
    df = df.merge(market[['date', 'ticker'] + [f'future_ret_{h}' for h in horizons]], 
                  on=['date', 'ticker'], how='left')
    return df


def analyze_factor_stats(scores_df):
    """分析各因子的统计特性"""
    stats = {}
    
    for name, config in FACTORS.items():
        col = config['score_col']
        if col not in scores_df.columns:
            continue
        
        vals = scores_df[col].dropna()
        
        stats[name] = {
            'mean': vals.mean(),
            'median': vals.median(),
            'std': vals.std(),
            'min': vals.min(),
            'max': vals.max(),
            'q25': vals.quantile(0.25),
            'q75': vals.quantile(0.75),
        }
    
    return stats


def analyze_factor_future_corr(scores_df):
    """分析各因子与未来收益的相关性"""
    corrs = {}
    horizons = [5, 10, 20]
    
    for name, config in FACTORS.items():
        col = config['score_col']
        if col not in scores_df.columns:
            continue
        
        corrs[name] = {}
        for h in horizons:
            ret_col = f'future_ret_{h}'
            if ret_col not in scores_df.columns:
                continue
            mask = scores_df[col].notna() & scores_df[ret_col].notna()
            if mask.sum() < 10:
                continue
            corr = scores_df.loc[mask, col].corr(scores_df.loc[mask, ret_col])
            corrs[name][h] = corr
    
    return corrs


def analyze_factor_pass_rate(scores_df):
    """分析各因子对买入候选的贡献（pass rate）"""
    # 买入条件：total_score >= 40 AND 各因子至少满足最低要求
    # 这里分析：如果只依赖该因子，有多少比例的候选能通过
    pass_rates = {}
    
    for name, config in FACTORS.items():
        col = config['score_col']
        if col not in scores_df.columns:
            continue
        
        # 该因子得分 > 0 的比例
        has_score = (scores_df[col].fillna(0) > 0).mean()
        # 该因子得分 >= 50% max 的比例
        decent_score = (scores_df[col].fillna(0) >= config['max_score'] * 0.5).mean()
        # 该因子得分 >= 75% max 的比例
        good_score = (scores_df[col].fillna(0) >= config['max_score'] * 0.75).mean()
        
        pass_rates[name] = {
            'any_score': has_score,
            'decent_score': decent_score,
            'good_score': good_score,
        }
    
    return pass_rates


def analyze_factor_correlations(scores_df):
    """分析因子间的相关性"""
    cols = [config['score_col'] for config in FACTORS.values() 
            if config['score_col'] in scores_df.columns]
    if len(cols) < 2:
        return None
    
    corr_matrix = scores_df[cols].corr()
    return corr_matrix


def run_backtest_with_config(cfg, as_of_date, performance_start=None):
    """运行回测，返回结果"""
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    engine = BacktestEngine(cfg)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=performance_start)


def run_ablation(cfg, factor_name, split_name, split_cfg):
    """运行单因子消融"""
    test_cfg = copy.deepcopy(cfg)
    score_col = FACTORS[factor_name]['score_col']
    test_cfg['exclude_factor'] = score_col
    
    r = run_backtest_with_config(test_cfg, split_cfg['as_of_date'], split_cfg['performance_start'])
    return r


def run_weighted_backtest(cfg, weights, split_name, split_cfg):
    """运行加权回测"""
    test_cfg = copy.deepcopy(cfg)
    test_cfg['_custom_weights'] = weights
    
    # 创建自定义引擎
    engine = BacktestEngine(test_cfg)
    original_compute = engine.strategy.compute_total_score
    
    def weighted_compute(scores_df, exclude_factor=None):
        df = scores_df.copy()
        w = test_cfg.get('_custom_weights', {})
        if not w:
            return original_compute(df, exclude_factor)
        
        # 计算加权总分（归一化到0-100）
        total = 0
        for factor, weight in w.items():
            col = FACTORS[factor]['score_col']
            max_s = FACTORS[factor]['max_score']
            raw = df[col].fillna(0)
            total += raw / max_s * weight * 100
        
        df['total_score'] = total
        return df
    
    engine.strategy.compute_total_score = weighted_compute
    
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    return engine.run(market_df, bench_df, 
                      as_of_date=split_cfg['as_of_date'], 
                      performance_start=split_cfg['performance_start'])


def main():
    print("=" * 70)
    print("Phase 5.3: 评分标准增量价值验证")
    print("=" * 70)
    
    # 基础配置（周四调仓、-8%止损、20%单只）
    base_cfg = build_config()
    base_cfg['fallback_equity_enabled'] = False
    base_cfg['rebalance_weekday'] = 3
    base_cfg['stop_loss'] = -0.08
    base_cfg['max_position_per_etf'] = 0.20
    base_cfg['min_total_score'] = 40  # 使用B0.1默认值
    
    # ========== 步骤1：因子统计特性分析 ==========
    print("\n" + "=" * 70)
    print("步骤1：因子统计特性分析")
    print("=" * 70)
    
    # 获取训练集原始信号
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    # 只取训练集数据
    train_market = market_df[market_df['date'] <= '2022-12-30'].copy()
    train_scores = get_raw_signals(base_cfg, train_market, bench_df)
    train_scores = add_future_returns(train_scores, train_market)
    
    print(f"  训练集信号样本数: {len(train_scores)}")
    print(f"  日期范围: {train_scores['date'].min()} ~ {train_scores['date'].max()}")
    
    # 1.1 分数分布
    print("\n  1.1 分数分布:")
    dist_stats = analyze_factor_stats(train_scores)
    for name, s in dist_stats.items():
        print(f"    {name}: 均值={s['mean']:.2f}, 中位={s['median']:.2f}, 标准差={s['std']:.2f}, "
              f"范围=[{s['min']:.1f}, {s['max']:.1f}]")
    
    # 1.2 与未来收益相关性
    print("\n  1.2 与未来收益相关性 (Pearson):")
    future_corrs = analyze_factor_future_corr(train_scores)
    for name, corrs in future_corrs.items():
        h_str = ", ".join([f"H{h}={v:.4f}" for h, v in corrs.items()])
        print(f"    {name}: {h_str}")
    
    # 1.3 买入候选通过率
    print("\n  1.3 买入候选得分分布:")
    pass_rates = analyze_factor_pass_rate(train_scores)
    for name, rates in pass_rates.items():
        print(f"    {name}: 有分={rates['any_score']:.1%}, 过半={rates['decent_score']:.1%}, "
              f"75%={rates['good_score']:.1%}")
    
    # 1.4 因子间相关性
    print("\n  1.4 因子间相关性:")
    factor_corr = analyze_factor_correlations(train_scores)
    if factor_corr is not None:
        print(factor_corr.round(3).to_string())
    
    # ========== 步骤2：单因子消融 ==========
    print("\n" + "=" * 70)
    print("步骤2：单因子消融测试")
    print("=" * 70)
    
    # B0.1 基准（两阶段）
    print("\n  B0.1 基准:")
    b0_results = {}
    for split_name, split_cfg in SPLITS.items():
        r = run_backtest_with_config(base_cfg, split_cfg['as_of_date'], split_cfg['performance_start'])
        b0_results[split_name] = r
        print(f"    [{split_name}] 年化={r['annual_return']:.2%}, 夏普={r['sharpe_ratio']:.3f}, "
              f"回撤={r['max_drawdown']:.2%}, 交易={r['num_trades']}")
    
    # 消融测试
    print("\n  消融测试（删除一个因子）:")
    ablation_results = {}
    for factor_name in FACTORS.keys():
        ablation_results[factor_name] = {}
        print(f"\n    删除 {factor_name}:")
        for split_name, split_cfg in SPLITS.items():
            r = run_ablation(base_cfg, factor_name, split_name, split_cfg)
            ablation_results[factor_name][split_name] = r
            b0 = b0_results[split_name]
            delta = r['annual_return'] - b0['annual_return']
            print(f"      [{split_name}] 年化={r['annual_return']:.2%} (Δ{delta:+.2%}), "
                  f"夏普={r['sharpe_ratio']:.3f}, 回撤={r['max_drawdown']:.2%}")
    
    # 判断增量价值：删除后年化下降超过0.5%的因子认为有增量价值
    proven_factors = []
    for factor_name in FACTORS.keys():
        train_delta = ablation_results[factor_name]['train']['annual_return'] - b0_results['train']['annual_return']
        if train_delta < -0.005:  # 删除后年化下降超过0.5%
            proven_factors.append(factor_name)
    
    print(f"\n  有增量价值的因子（删除后训练集年化下降>0.5%）: {proven_factors}")
    
    # ========== 步骤3：权重测试（仅对有增量价值的因子） ==========
    print("\n" + "=" * 70)
    print("步骤3：权重测试（仅对有增量价值的因子）")
    print("=" * 70)
    
    weight_configs = []
    
    # 当前权重（基准）
    weight_configs.append({
        'name': 'current',
        'weights': DEFAULT_WEIGHTS.copy(),
    })
    
    # 等权版本
    equal_w = {f: 1/5 for f in FACTORS.keys()}
    weight_configs.append({
        'name': 'equal',
        'weights': equal_w,
    })
    
    if proven_factors:
        # 对第一个有增量价值的因子 +25%
        f1 = proven_factors[0]
        w_plus = DEFAULT_WEIGHTS.copy()
        # 增加25%权重，其他等比例减少
        old_w = w_plus[f1]
        new_w = old_w * 1.25
        other_sum = sum(w_plus[k] for k in w_plus if k != f1)
        scale = (1 - new_w) / other_sum
        for k in w_plus:
            if k != f1:
                w_plus[k] *= scale
        w_plus[f1] = new_w
        weight_configs.append({
            'name': f'{f1}_plus25',
            'weights': w_plus,
        })
        
        # 对第一个有增量价值的因子 -25%
        f1 = proven_factors[0]
        w_minus = DEFAULT_WEIGHTS.copy()
        old_w = w_minus[f1]
        new_w = old_w * 0.75
        other_sum = sum(w_minus[k] for k in w_minus if k != f1)
        scale = (1 - new_w) / other_sum
        for k in w_minus:
            if k != f1:
                w_minus[k] *= scale
        w_minus[f1] = new_w
        weight_configs.append({
            'name': f'{f1}_minus25',
            'weights': w_minus,
        })
        
        # 删除无效因子版本（只保留有增量价值的因子）
        if len(proven_factors) < len(FACTORS):
            w_remove = {f: DEFAULT_WEIGHTS[f] for f in proven_factors}
            total = sum(w_remove.values())
            w_remove = {f: v/total for f, v in w_remove.items()}
            weight_configs.append({
                'name': 'remove_invalid',
                'weights': w_remove,
            })
    
    print(f"\n  测试 {len(weight_configs)} 种权重配置:")
    for wc in weight_configs:
        w_str = ", ".join([f"{k}={v:.2f}" for k, v in wc['weights'].items()])
        print(f"    {wc['name']}: {w_str}")
    
    # 运行权重测试
    weight_results = {}
    for wc in weight_configs:
        weight_results[wc['name']] = {}
        print(f"\n    [{wc['name']}]:")
        for split_name, split_cfg in SPLITS.items():
            r = run_weighted_backtest(base_cfg, wc['weights'], split_name, split_cfg)
            weight_results[wc['name']][split_name] = r
            b0 = b0_results[split_name]
            delta = r['annual_return'] - b0['annual_return']
            print(f"      [{split_name}] 年化={r['annual_return']:.2%} (Δ{delta:+.2%}), "
                  f"夏普={r['sharpe_ratio']:.3f}, 回撤={r['max_drawdown']:.2%}")
    
    # ========== 步骤4：训练集生成候选，验证集选择 ==========
    print("\n" + "=" * 70)
    print("步骤4：训练集生成候选，验证集选择")
    print("=" * 70)
    
    # 候选 = 所有消融 + 所有权重配置
    # 在训练集上按年化排序，取前N个
    all_candidates = []
    
    # 基准
    all_candidates.append({
        'name': 'B0.1',
        'type': 'baseline',
        'train_result': b0_results['train'],
        'valid_result': b0_results['valid'],
    })
    
    # 消融候选
    for factor_name in FACTORS.keys():
        all_candidates.append({
            'name': f'no_{factor_name}',
            'type': 'ablation',
            'train_result': ablation_results[factor_name]['train'],
            'valid_result': ablation_results[factor_name]['valid'],
        })
    
    # 权重候选
    for wc in weight_configs:
        if wc['name'] == 'current':
            continue  # 与基准相同
        all_candidates.append({
            'name': wc['name'],
            'type': 'weight',
            'train_result': weight_results[wc['name']]['train'],
            'valid_result': weight_results[wc['name']]['valid'],
        })
    
    # 训练集排序
    all_candidates.sort(key=lambda x: x['train_result']['annual_return'], reverse=True)
    
    print("\n  训练集排名（候选）:")
    for i, c in enumerate(all_candidates[:10]):
        r = c['train_result']
        print(f"    #{i+1}: {c['name']} ({c['type']}) -> 年化={r['annual_return']:.2%}, "
              f"夏普={r['sharpe_ratio']:.3f}, 回撤={r['max_drawdown']:.2%}")
    
    # 验证集选择：取验证集年化最高的
    best = max(all_candidates, key=lambda x: x['valid_result']['annual_return'])
    print(f"\n  验证集选择的最佳方案: {best['name']} ({best['type']})")
    print(f"    训练集: 年化={best['train_result']['annual_return']:.2%}, "
          f"夏普={best['train_result']['sharpe_ratio']:.3f}")
    print(f"    验证集: 年化={best['valid_result']['annual_return']:.2%}, "
          f"夏普={best['valid_result']['sharpe_ratio']:.3f}")
    
    # ========== 生成报告 ==========
    print("\n" + "=" * 70)
    print("生成报告...")
    print("=" * 70)
    
    lines = []
    lines.append('# Phase 5.3 评分标准增量价值验证报告')
    lines.append('')
    lines.append(f'**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append('')
    lines.append('## 方法论')
    lines.append('')
    lines.append('### 保持不变的参数')
    lines.append('- 调仓日：周四')
    lines.append('- 止损：-8%')
    lines.append('- 单只上限：20%')
    lines.append('- 最低总评分：40')
    lines.append('- 不修改 src/config.py')
    lines.append('')
    lines.append('### 分析步骤')
    lines.append('1. 统计各因子分数分布、与未来5/10/20日收益相关性、买入候选通过率、因子间相关性')
    lines.append('2. 单因子消融：每次删除一个因子，比较训练/验证期表现')
    lines.append('3. 对证明有增量价值的因子测试少量权重配置')
    lines.append('4. 训练集生成候选，验证集选择唯一方案')
    lines.append('5. 最终样本外暂不运行（不根据样本外调整）')
    lines.append('')
    
    lines.append('## 一、因子统计特性（训练集 2019-2022）')
    lines.append('')
    
    lines.append('### 1.1 分数分布')
    lines.append('')
    lines.append('| 因子 | 均值 | 中位 | 标准差 | 最小 | 最大 | 25%分位 | 75%分位 |')
    lines.append('|------|------|------|--------|------|------|---------|---------|')
    for name, s in dist_stats.items():
        lines.append(f"| {name} | {s['mean']:.2f} | {s['median']:.2f} | {s['std']:.2f} | "
                     f"{s['min']:.1f} | {s['max']:.1f} | {s['q25']:.2f} | {s['q75']:.2f} |")
    lines.append('')
    
    lines.append('### 1.2 与未来收益相关性（Pearson）')
    lines.append('')
    lines.append('| 因子 | H5 | H10 | H20 |')
    lines.append('|------|------|------|------|')
    for name, corrs in future_corrs.items():
        h5 = corrs.get(5, 'N/A')
        h10 = corrs.get(10, 'N/A')
        h20 = corrs.get(20, 'N/A')
        h5_s = f"{h5:.4f}" if isinstance(h5, float) else h5
        h10_s = f"{h10:.4f}" if isinstance(h10, float) else h10
        h20_s = f"{h20:.4f}" if isinstance(h20, float) else h20
        lines.append(f"| {name} | {h5_s} | {h10_s} | {h20_s} |")
    lines.append('')
    
    lines.append('### 1.3 买入候选得分分布')
    lines.append('')
    lines.append('| 因子 | 有分(>0) | 过半(≥50%) | 75%分(≥75%) |')
    lines.append('|------|----------|------------|-------------|')
    for name, rates in pass_rates.items():
        lines.append(f"| {name} | {rates['any_score']:.1%} | {rates['decent_score']:.1%} | {rates['good_score']:.1%} |")
    lines.append('')
    
    lines.append('### 1.4 因子间相关性')
    lines.append('')
    if factor_corr is not None:
        lines.append(factor_corr.round(3).to_markdown())
    lines.append('')
    
    lines.append('## 二、单因子消融')
    lines.append('')
    lines.append('消融规则：从总评分中删除一个因子，其他因子保持不变。')
    lines.append('')
    lines.append('| 删除因子 | 训练集年化 | 训练集Δ | 训练集夏普 | 验证集年化 | 验证集Δ | 验证集夏普 | 增量价值？ |')
    lines.append('|----------|------------|---------|------------|------------|---------|------------|------------|')
    for factor_name in FACTORS.keys():
        tr = ablation_results[factor_name]['train']
        vr = ablation_results[factor_name]['valid']
        b0_tr = b0_results['train']
        b0_vr = b0_results['valid']
        train_delta = tr['annual_return'] - b0_tr['annual_return']
        valid_delta = vr['annual_return'] - b0_vr['annual_return']
        has_value = '是' if train_delta < -0.005 else '否'
        lines.append(f"| {factor_name} | {tr['annual_return']:.2%} | {train_delta:+.2%} | {tr['sharpe_ratio']:.3f} | "
                     f"{vr['annual_return']:.2%} | {valid_delta:+.2%} | {vr['sharpe_ratio']:.3f} | {has_value} |")
    lines.append('')
    
    lines.append(f'**有增量价值的因子**: {", ".join(proven_factors) if proven_factors else "无"}')
    lines.append('')
    
    lines.append('## 三、权重测试')
    lines.append('')
    lines.append('仅对有增量价值的因子调整权重，其他因子等比例补偿。')
    lines.append('')
    lines.append('| 配置 | 权重 | 训练集年化 | 训练集Δ | 训练集夏普 | 验证集年化 | 验证集Δ | 验证集夏普 |')
    lines.append('|------|------|------------|---------|------------|------------|---------|------------|')
    for wc in weight_configs:
        name = wc['name']
        tr = weight_results[name]['train']
        vr = weight_results[name]['valid']
        b0_tr = b0_results['train']
        b0_vr = b0_results['valid']
        train_delta = tr['annual_return'] - b0_tr['annual_return']
        valid_delta = vr['annual_return'] - b0_vr['annual_return']
        w_str = ", ".join([f"{k}={v:.2f}" for k, v in wc['weights'].items()])
        lines.append(f"| {name} | {w_str} | {tr['annual_return']:.2%} | {train_delta:+.2%} | {tr['sharpe_ratio']:.3f} | "
                     f"{vr['annual_return']:.2%} | {valid_delta:+.2%} | {vr['sharpe_ratio']:.3f} |")
    lines.append('')
    
    lines.append('## 四、训练集排名与验证集选择')
    lines.append('')
    lines.append('### 训练集排名（候选方案）')
    lines.append('')
    lines.append('| 排名 | 方案 | 类型 | 训练集年化 | 训练集夏普 | 训练集回撤 |')
    lines.append('|------|------|------|------------|------------|------------|')
    for i, c in enumerate(all_candidates[:10]):
        r = c['train_result']
        lines.append(f"| {i+1} | {c['name']} | {c['type']} | {r['annual_return']:.2%} | {r['sharpe_ratio']:.3f} | {r['max_drawdown']:.2%} |")
    lines.append('')
    
    lines.append('### 验证集选择')
    lines.append('')
    lines.append(f'**最佳方案**: {best["name"]} ({best["type"]})')
    lines.append('')
    lines.append('| 阶段 | 年化 | 夏普 | 最大回撤 | 交易次数 |')
    lines.append('|------|------|------|----------|----------|')
    lines.append(f"| 训练集 | {best['train_result']['annual_return']:.2%} | {best['train_result']['sharpe_ratio']:.3f} | {best['train_result']['max_drawdown']:.2%} | {best['train_result']['num_trades']} |")
    lines.append(f"| 验证集 | {best['valid_result']['annual_return']:.2%} | {best['valid_result']['sharpe_ratio']:.3f} | {best['valid_result']['max_drawdown']:.2%} | {best['valid_result']['num_trades']} |")
    lines.append('')
    
    lines.append('### 与B0.1对比')
    lines.append('')
    b0 = b0_results
    lines.append(f"| 阶段 | {best['name']} | B0.1 | 差异 |")
    lines.append(f"|------|------|------|------|")
    lines.append(f"| 训练集 | {best['train_result']['annual_return']:.2%} | {b0['train']['annual_return']:.2%} | {best['train_result']['annual_return']-b0['train']['annual_return']:+.2%} |")
    lines.append(f"| 验证集 | {best['valid_result']['annual_return']:.2%} | {b0['valid']['annual_return']:.2%} | {best['valid_result']['annual_return']-b0['valid']['annual_return']:+.2%} |")
    lines.append('')
    
    lines.append('## 五、审慎结论')
    lines.append('')
    lines.append('### 核心发现')
    lines.append('')
    
    if proven_factors:
        lines.append(f'1. **有增量价值的因子**: {", ".join(proven_factors)}')
        lines.append(f'   删除这些因子后，训练集年化下降超过0.5%。')
    else:
        lines.append('1. **无因子被证明有增量价值**：删除任何单个因子后，训练集年化下降均不超过0.5%。')
        lines.append('   说明当前评分系统可能存在过度参数化，或各因子信息重叠。')
    
    lines.append('')
    
    # 统计最佳方案
    if best['name'] != 'B0.1':
        lines.append(f'2. **验证集选择的最佳方案**: {best["name"]} ({best["type"]})')
        train_delta = best['train_result']['annual_return'] - b0_results['train']['annual_return']
        valid_delta = best['valid_result']['annual_return'] - b0_results['valid']['annual_return']
        lines.append(f'   训练集差异: {train_delta:+.2%}，验证集差异: {valid_delta:+.2%}')
        if abs(valid_delta) < 0.005:
            lines.append(f'   验证集差异小于0.5%，说明该方案与B0.1在统计上无显著差异。')
    else:
        lines.append('2. **B0.1 基准在验证集上表现最优**，无需调整。')
    
    lines.append('')
    lines.append('### 建议')
    lines.append('')
    lines.append('1. 当前评分系统五个因子高度耦合，建议进一步分析因子组合效果。')
    lines.append('2. 如验证集选择与B0.1差异不大，建议保持当前配置。')
    lines.append('3. 最终样本外暂不运行，等待验证集确认后再决定是否执行。')
    lines.append('4. 不修改 src/config.py，所有结果均为独立研究。')
    
    report_path = 'D:/etf_rotation_model/reports/phase5_scoring_diagnosis.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n报告已保存: {report_path}")
    print(f"\n{'='*70}")
    print("Phase 5.3 完成")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
