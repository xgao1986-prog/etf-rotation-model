#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 8.1: 排名位置预测力诊断

目标：验证B0.3每期横截面排名是否能够预测后续收益，尤其回答Rank 1-5内部是否具有稳定区分度。

冻结基准：
- 18只ETF的B0.3基准（momentum_factor_enabled=False, volatility_factor_enabled=False）
- 排名范围仅包含16只行业ETF，排除黄金(518880.SH)和国债(511010.SH)
- 使用B0.3原始评分与排序逻辑
- 不修改策略、评分、交易规则或仓位

研究区间：2019-08-13 至 2024-12-31
训练期：2019-08-13 至 2022-12-31
验证期：2023-01-01 至 2024-12-31

交付物：
- scripts/phase8_1_rank_position_diagnostics.py
- reports/phase8_1_rank_position_diagnostics.md
- reports/phase8_1_rank_samples.csv
- reports/phase8_1_rank_summary.csv

只研究，不改策略。Phase 8.1完成后停止，不自行开展仓位实验。
"""

import sys
sys.path.insert(0, r'D:\etf_rotation_model\src')

import os
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

from config import (
    ETF_UNIVERSE, BENCHMARK, STRATEGY_CONFIG, BACKTEST_CONFIG,
    DEFENSE_UNIVERSE
)

# ============================================================================
# 一、配置
# ============================================================================

# 研究区间
BACKTEST_START = date(2019, 8, 13)
BACKTEST_END = date(2024, 12, 31)
TRAIN_END = date(2022, 12, 31)
VALID_START = date(2023, 1, 1)

# 行业ETF（16只，排除黄金和国债）
INDUSTRY_ETFS = list(ETF_UNIVERSE.keys())  # 16只
BENCHMARK_TICKER = BENCHMARK  # 000300.SH

# 未来收益期限
FORWARD_PERIODS = [5, 10, 20]  # 交易日

# B0.3 配置（momentum和volatility关闭）
B03_CONFIG = dict(STRATEGY_CONFIG)
B03_CONFIG['momentum_factor_enabled'] = False
B03_CONFIG['volatility_factor_enabled'] = False
B03_CONFIG['min_total_score'] = 40

# 数据库路径
DB_PATH = r'D:\etf_rotation_model\database\etf_model.db'

# 输出路径
OUTPUT_DIR = r'D:\etf_rotation_model\reports'
SCRIPT_DIR = r'D:\etf_rotation_model\scripts'


# ============================================================================
# 二、数据加载
# ============================================================================

def load_data(db_path):
    """
    从数据库加载行情数据和评分数据。
    
    Returns:
        (market_df, scores_df) 行情数据和评分数据DataFrame
    """
    conn = sqlite3.connect(db_path)
    
    # 加载行情数据（行业ETF + 基准）
    all_tickers = INDUSTRY_ETFS + [BENCHMARK_TICKER]
    placeholders = ','.join(['?' for _ in all_tickers])
    
    market_query = f"""
    SELECT ticker, date, open, close, volume
    FROM market_data
    WHERE ticker IN ({placeholders})
      AND date >= '2019-01-01'
      AND date <= '2025-01-01'
    ORDER BY ticker, date
    """
    market_df = pd.read_sql(market_query, conn, params=all_tickers)
    market_df['date'] = pd.to_datetime(market_df['date']).dt.date
    
    # 加载评分数据
    scores_query = f"""
    SELECT ticker, date, ma20, ma20_slope, trend_score, confirm_score,
           momentum_rank, volume_score, vol_score, total_score
    FROM daily_scores
    WHERE ticker IN ({placeholders})
      AND date >= '2019-01-01'
      AND date <= '2025-01-01'
    ORDER BY ticker, date
    """
    scores_df = pd.read_sql(scores_query, conn, params=all_tickers)
    scores_df['date'] = pd.to_datetime(scores_df['date']).dt.date
    
    conn.close()
    
    return market_df, scores_df


def merge_market_and_scores(market_df, scores_df):
    """
    合并行情和评分数据，计算prev_close等字段。
    """
    # 合并
    merged = scores_df.merge(market_df[['ticker', 'date', 'open', 'close']],
                              on=['ticker', 'date'], how='inner')
    
    # 按ticker排序后计算prev_close
    merged = merged.sort_values(['ticker', 'date'])
    merged['prev_close'] = merged.groupby('ticker')['close'].shift(1)
    
    # 大盘强势标志（简化：仅用于记录，不影响排名）
    merged['history_count'] = merged.groupby('ticker').cumcount() + 1
    
    return merged


# ============================================================================
# 三、生成调仓日和排名
# ============================================================================

def generate_rebalance_dates(start_date, end_date, weekday=3):
    """
    生成每周四调仓日。
    weekday: 3 = 周四
    """
    dates = []
    current = start_date
    while current <= end_date:
        if current.weekday() == weekday:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def extract_rankings_at_date(merged_df, rebalance_date, config):
    """
    在指定调仓日提取16只行业ETF的排名。
    
    Returns:
        DataFrame with columns: rank, ticker, total_score, trend_score, 
        confirm_score, is_buy, buy_reason
    """
    # 获取该日期的所有行业ETF数据
    day_df = merged_df[
        (merged_df['date'] == rebalance_date) & 
        (merged_df['ticker'].isin(INDUSTRY_ETFS))
    ].copy()
    
    if day_df.empty:
        return pd.DataFrame()
    
    # 只保留有足够历史的ETF（history_count >= 51）
    day_df = day_df[day_df['history_count'] >= 51]
    
    if day_df.empty:
        return pd.DataFrame()
    
    # 按total_score降序排名
    day_df = day_df.sort_values('total_score', ascending=False)
    day_df['rank'] = range(1, len(day_df) + 1)
    
    # 检查BUY条件（B0.3原始逻辑）
    # 入场条件：
    # 1. trend_score >= 15
    # 2. confirm_score >= 4
    # 3. total_score >= 40（或差市场55）
    # 4. prev_close > ma20
    # 5. ma20_slope > 0
    
    # 简化：使用原始min_total_score=40，不计算market_quality
    effective_min = config['min_total_score']
    
    buy_conditions = (
        (day_df['trend_score'] >= config['min_trend_score']) &
        (day_df['confirm_score'] >= config['min_confirm_score']) &
        (day_df['total_score'] >= effective_min) &
        (day_df['prev_close'] > day_df['ma20']) &
        (day_df['ma20_slope'] > 0)
    )
    
    day_df['is_buy'] = buy_conditions
    
    # BUY原因
    def get_buy_reason(row):
        if not row['is_buy']:
            reasons = []
            if row['trend_score'] < config['min_trend_score']:
                reasons.append(f"trend={row['trend_score']}<{config['min_trend_score']}")
            if row['confirm_score'] < config['min_confirm_score']:
                reasons.append(f"confirm={row['confirm_score']}<{config['min_confirm_score']}")
            if row['total_score'] < effective_min:
                reasons.append(f"total={row['total_score']:.1f}<{effective_min}")
            if pd.isna(row['prev_close']) or row['prev_close'] <= row['ma20']:
                reasons.append("prev_close<=ma20")
            if row['ma20_slope'] <= 0:
                reasons.append("ma20_slope<=0")
            return '; '.join(reasons) if reasons else 'N/A'
        return 'BUY'
    
    day_df['buy_reason'] = day_df.apply(get_buy_reason, axis=1)
    
    # 选择输出列
    result = day_df[['rank', 'ticker', 'total_score', 'trend_score', 
                     'confirm_score', 'momentum_rank', 'volume_score',
                     'is_buy', 'buy_reason', 'close', 'prev_close', 'ma20',
                     'ma20_slope']].copy()
    
    return result


# ============================================================================
# 四、计算未来收益
# ============================================================================

def calculate_forward_returns(market_df, rebalance_date, ticker, periods):
    """
    计算指定ETF在调仓日后的未来收益。
    
    信号在T日收盘后生成，从T+1开盘计算未来收益。
    
    Returns:
        dict: {period: return, ...}
    """
    ticker_data = market_df[market_df['ticker'] == ticker].sort_values('date')
    
    if ticker_data.empty:
        return {p: np.nan for p in periods}
    
    # 找到T+1的开盘价的索引
    t1_idx = ticker_data[ticker_data['date'] > rebalance_date].index
    if len(t1_idx) == 0:
        return {p: np.nan for p in periods}
    
    t1_row = ticker_data.loc[t1_idx[0]]
    t1_open = t1_row['open']
    t1_date = t1_row['date']
    
    if pd.isna(t1_open) or t1_open == 0:
        return {p: np.nan for p in periods}
    
    # 获取T+1之后的数据（排除T+1当天）
    future_data = ticker_data[ticker_data['date'] > t1_date].reset_index(drop=True)
    
    results = {}
    for p in periods:
        if len(future_data) >= p:
            future_close = future_data.iloc[p - 1]['close']
            if pd.isna(future_close):
                results[p] = np.nan
            else:
                results[p] = future_close / t1_open - 1
        else:
            results[p] = np.nan
    
    return results


def calculate_benchmark_forward_return(market_df, rebalance_date, periods):
    """
    计算基准在调仓日后的未来收益。
    """
    return calculate_forward_returns(market_df, rebalance_date, BENCHMARK_TICKER, periods)


# ============================================================================
# 五、主分析流程
# ============================================================================

def run_rank_position_diagnostics():
    """
    主分析函数：运行排名位置预测力诊断。
    """
    print("=" * 80)
    print("Phase 8.1: 排名位置预测力诊断")
    print("=" * 80)
    print()
    
    # 1. 加载数据
    print("[1/7] 加载数据...")
    market_df, scores_df = load_data(DB_PATH)
    merged_df = merge_market_and_scores(market_df, scores_df)
    
    print(f"  行情数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只ETF")
    print(f"  评分数据: {len(scores_df)} 行, {scores_df['ticker'].nunique()} 只ETF")
    print(f"  合并数据: {len(merged_df)} 行")
    
    # 2. 生成调仓日
    print("[2/7] 生成调仓日...")
    rebalance_dates = generate_rebalance_dates(BACKTEST_START, BACKTEST_END, weekday=3)
    print(f"  调仓日数量: {len(rebalance_dates)} 个")
    
    # 3. 提取每期排名并计算未来收益
    print("[3/7] 提取排名并计算未来收益...")
    
    all_samples = []  # 每个排名位置的样本
    
    for rd in rebalance_dates:
        rankings = extract_rankings_at_date(merged_df, rd, B03_CONFIG)
        
        if rankings.empty:
            continue
        
        # 计算未来收益
        for _, row in rankings.iterrows():
            ticker = row['ticker']
            
            # ETF未来收益
            etf_returns = calculate_forward_returns(market_df, rd, ticker, FORWARD_PERIODS)
            
            # 基准未来收益
            bench_returns = calculate_benchmark_forward_return(market_df, rd, FORWARD_PERIODS)
            
            # 超额收益
            for p in FORWARD_PERIODS:
                excess = etf_returns[p] - bench_returns[p] if not pd.isna(etf_returns[p]) and not pd.isna(bench_returns[p]) else np.nan
                
                all_samples.append({
                    'date': rd,
                    'period': p,
                    'rank': row['rank'],
                    'ticker': ticker,
                    'total_score': row['total_score'],
                    'trend_score': row['trend_score'],
                    'confirm_score': row['confirm_score'],
                    'is_buy': row['is_buy'],
                    'buy_reason': row['buy_reason'],
                    'etf_return': etf_returns[p],
                    'benchmark_return': bench_returns[p],
                    'excess_return': excess,
                })
    
    samples_df = pd.DataFrame(all_samples)
    
    # 划分训练期和验证期
    samples_df['period_label'] = samples_df['date'].apply(
        lambda d: '训练期' if d <= TRAIN_END else '验证期'
    )
    
    print(f"  总样本数: {len(samples_df)} (训练期: {len(samples_df[samples_df['period_label']=='训练期'])}, 验证期: {len(samples_df[samples_df['period_label']=='验证期'])})")
    
    # 保存样本数据
    samples_path = os.path.join(OUTPUT_DIR, 'phase8_1_rank_samples.csv')
    samples_df.to_csv(samples_path, index=False, encoding='utf-8-sig')
    print(f"  样本数据已保存: {samples_path}")
    
    # 4. 按排名聚合统计
    print("[4/7] 按排名聚合统计...")
    
    summary_list = []
    
    for period in FORWARD_PERIODS:
        for period_label in ['训练期', '验证期', '全部']:
            if period_label == '全部':
                subset = samples_df[samples_df['period'] == period]
            else:
                subset = samples_df[(samples_df['period'] == period) & (samples_df['period_label'] == period_label)]
            
            if subset.empty:
                continue
            
            for rank in range(1, 17):  # Rank 1-16
                rank_subset = subset[subset['rank'] == rank]
                
                if len(rank_subset) == 0:
                    continue
                
                etf_rets = rank_subset['etf_return'].dropna()
                excess_rets = rank_subset['excess_return'].dropna()
                
                if len(etf_rets) == 0:
                    continue
                
                summary_list.append({
                    'period': period,
                    'period_label': period_label,
                    'rank': rank,
                    'n_samples': len(rank_subset),
                    'n_valid': len(etf_rets),
                    'mean_return': etf_rets.mean(),
                    'median_return': etf_rets.median(),
                    'std_return': etf_rets.std(),
                    'p25_return': etf_rets.quantile(0.25),
                    'p75_return': etf_rets.quantile(0.75),
                    'min_return': etf_rets.min(),
                    'max_return': etf_rets.max(),
                    'positive_rate': (etf_rets > 0).mean(),
                    'mean_excess': excess_rets.mean() if len(excess_rets) > 0 else np.nan,
                    'median_excess': excess_rets.median() if len(excess_rets) > 0 else np.nan,
                    'excess_positive_rate': (excess_rets > 0).mean() if len(excess_rets) > 0 else np.nan,
                })
    
    summary_df = pd.DataFrame(summary_list)
    
    # 保存汇总数据
    summary_path = os.path.join(OUTPUT_DIR, 'phase8_1_rank_summary.csv')
    summary_df.to_csv(summary_path, index=False, encoding='utf-8-sig')
    print(f"  汇总数据已保存: {summary_path}")
    
    # 5. 排名间比较
    print("[5/7] 排名间比较...")
    
    comparisons = calculate_rank_comparisons(samples_df)
    
    # 6. Block Bootstrap置信区间
    print("[6/7] Block Bootstrap置信区间...")
    bootstrap_results = calculate_block_bootstrap_for_all(samples_df)
    
    # 7. 生成可视化
    print("[7/7] 生成可视化...")
    
    try:
        generate_plots(samples_df, summary_df, bootstrap_results)
        print("  图表已生成")
    except Exception as e:
        print(f"  图表生成失败: {e}")
    
    # 8. 生成报告
    print("[8/8] 生成报告...")
    
    report_path = os.path.join(OUTPUT_DIR, 'phase8_1_rank_position_diagnostics.md')
    generate_report(samples_df, summary_df, comparisons, bootstrap_results, report_path)
    print(f"  报告已保存: {report_path}")
    
    return samples_df, summary_df, comparisons, bootstrap_results


# ============================================================================
# 六、排名间比较
# ============================================================================

def calculate_rank_comparisons(samples_df):
    """
    计算不同排名组合之间的比较统计。
    
    Returns:
        dict of comparison results
    """
    comparisons = {}
    
    for period in FORWARD_PERIODS:
        subset = samples_df[samples_df['period'] == period]
        
        # 比较对定义
        compare_pairs = [
            ('Rank 1', 'Rank 5', 1, 5),
            ('Rank 1', 'Rank 2-5均值', 1, [2, 3, 4, 5]),
            ('Top 3', 'Rank 4-5', [1, 2, 3], [4, 5]),
            ('Top 5', 'Rank 6-10', [1, 2, 3, 4, 5], [6, 7, 8, 9, 10]),
            ('Top 5', 'Rank 11-16', [1, 2, 3, 4, 5], [11, 12, 13, 14, 15, 16]),
        ]
        
        for name_a, name_b, ranks_a, ranks_b in compare_pairs:
            key = f"{name_a} vs {name_b}"
            
            if isinstance(ranks_a, int):
                a_rets = subset[subset['rank'] == ranks_a]['etf_return'].dropna()
            else:
                a_rets = subset[subset['rank'].isin(ranks_a)]['etf_return'].dropna()
            
            if isinstance(ranks_b, int):
                b_rets = subset[subset['rank'] == ranks_b]['etf_return'].dropna()
            else:
                b_rets = subset[subset['rank'].isin(ranks_b)]['etf_return'].dropna()
            
            if len(a_rets) == 0 or len(b_rets) == 0:
                continue
            
            comparisons[f"{period}d_{key}"] = {
                'period': period,
                'comparison': key,
                'a_mean': a_rets.mean(),
                'b_mean': b_rets.mean(),
                'a_median': a_rets.median(),
                'b_median': b_rets.median(),
                'a_std': a_rets.std(),
                'b_std': b_rets.std(),
                'a_n': len(a_rets),
                'b_n': len(b_rets),
                'diff_mean': a_rets.mean() - b_rets.mean(),
                'diff_median': a_rets.median() - b_rets.median(),
            }
    
    return comparisons


# ============================================================================
# 六、Block Bootstrap 置信区间
# ============================================================================

def block_bootstrap_ci(samples_df, period, rank, n_bootstrap=1000, ci=0.95):
    """
    Block bootstrap：按调仓日期（block）重采样，计算置信区间。
    
    由于5/10/20日样本存在重叠，不能把每只ETF样本视为独立。
    以调仓日期为block单位重采样，保持同一日期内所有ETF的关联结构。
    
    Args:
        samples_df: 样本DataFrame
        period: 未来收益期限（5/10/20）
        rank: 排名位置（1-16）
        n_bootstrap: 重采样次数
        ci: 置信水平
    
    Returns:
        (mean, lower, upper) 均值和置信区间
    """
    subset = samples_df[(samples_df['period'] == period) & (samples_df['rank'] == rank)]
    
    if subset.empty:
        return np.nan, np.nan, np.nan
    
    # 获取所有唯一的调仓日期
    unique_dates = subset['date'].unique()
    n_dates = len(unique_dates)
    
    if n_dates == 0:
        return np.nan, np.nan, np.nan
    
    # 按日期聚合：每个block是一个日期的所有ETF收益
    date_returns = subset.groupby('date')['etf_return'].apply(list).to_dict()
    
    bootstrap_means = []
    np.random.seed(42)  # 可复现
    
    for _ in range(n_bootstrap):
        # 有放回地抽取日期
        sampled_dates = np.random.choice(unique_dates, size=n_dates, replace=True)
        
        # 收集被抽中日期的所有ETF收益（过滤NaN）
        sampled_returns = []
        for d in sampled_dates:
            valid_returns = [r for r in date_returns[d] if not pd.isna(r)]
            sampled_returns.extend(valid_returns)
        
        if len(sampled_returns) > 0:
            bootstrap_means.append(np.mean(sampled_returns))
    
    if len(bootstrap_means) == 0:
        return np.nan, np.nan, np.nan
    
    bootstrap_means = np.array(bootstrap_means)
    mean = bootstrap_means.mean()
    alpha = (1 - ci) / 2
    lower = np.percentile(bootstrap_means, alpha * 100)
    upper = np.percentile(bootstrap_means, (1 - alpha) * 100)
    
    return mean, lower, upper


def calculate_block_bootstrap_for_all(samples_df):
    """
    为所有rank和period计算block bootstrap置信区间。
    
    Returns:
        dict: {(period, rank): (mean, lower, upper), ...}
    """
    print("  [Block Bootstrap] 计算置信区间...")
    
    results = {}
    for period in FORWARD_PERIODS:
        for rank in range(1, 17):
            mean, lower, upper = block_bootstrap_ci(samples_df, period, rank)
            results[(period, rank)] = (mean, lower, upper)
    
    print("  [Block Bootstrap] 完成")
    return results


# ============================================================================
# 七、可视化
# ============================================================================

def generate_plots(samples_df, summary_df, bootstrap_results):
    """
    生成排名位置诊断图表（含bootstrap置信区间）。
    """
    import matplotlib
    matplotlib.use('Agg')  # 非交互式后端
    import matplotlib.pyplot as plt
    
    fig, axes = plt.subplots(3, 3, figsize=(18, 15))
    
    for i, period in enumerate(FORWARD_PERIODS):
        # 1. 平均收益按排名（含bootstrap CI）
        ax = axes[i, 0]
        for label in ['训练期', '验证期']:
            data = summary_df[(summary_df['period'] == period) & (summary_df['period_label'] == label)]
            if not data.empty:
                ax.plot(data['rank'], data['mean_return'] * 100, marker='o', label=label, alpha=0.7)
        
        # 添加全部区间的bootstrap CI
        ranks_all = []
        means_all = []
        lowers_all = []
        uppers_all = []
        for rank in range(1, 17):
            mean, lower, upper = bootstrap_results.get((period, rank), (np.nan, np.nan, np.nan))
            if not pd.isna(mean):
                ranks_all.append(rank)
                means_all.append(mean * 100)
                lowers_all.append(lower * 100)
                uppers_all.append(upper * 100)
        
        if ranks_all:
            ax.errorbar(ranks_all, means_all, 
                       yerr=[np.array(means_all) - np.array(lowers_all), 
                             np.array(uppers_all) - np.array(means_all)],
                       fmt='ko-', capsize=3, label='全部(95% CI)', linewidth=1.5, markersize=5)
        
        ax.set_xlabel('Rank')
        ax.set_ylabel('Mean Return (%)')
        ax.set_title(f'{period}D - Mean Return by Rank (with 95% CI)')
        ax.legend(loc='best', fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
        
        # 2. 正收益胜率
        ax = axes[i, 1]
        for label in ['训练期', '验证期']:
            data = summary_df[(summary_df['period'] == period) & (summary_df['period_label'] == label)]
            if not data.empty:
                ax.plot(data['rank'], data['positive_rate'] * 100, marker='o', label=label)
        ax.set_xlabel('Rank')
        ax.set_ylabel('Positive Rate (%)')
        ax.set_title(f'{period}D - Positive Rate by Rank')
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.axhline(y=50, color='gray', linestyle='--', alpha=0.5)
        
        # 3. 超额胜率
        ax = axes[i, 2]
        for label in ['训练期', '验证期']:
            data = summary_df[(summary_df['period'] == period) & (summary_df['period_label'] == label)]
            if not data.empty:
                ax.plot(data['rank'], data['excess_positive_rate'] * 100, marker='o', label=label)
        ax.set_xlabel('Rank')
        ax.set_ylabel('Excess Win Rate (%)')
        ax.set_title(f'{period}D - Excess Win Rate by Rank')
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.axhline(y=50, color='gray', linestyle='--', alpha=0.5)
    
    plt.tight_layout()
    plot_path = os.path.join(OUTPUT_DIR, 'phase8_1_rank_diagnostics.png')
    plt.savefig(plot_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    print(f"  图表已保存: {plot_path}")


# ============================================================================
# 八、报告生成
# ============================================================================

def generate_report(samples_df, summary_df, comparisons, bootstrap_results, output_path):
    """
    生成Markdown诊断报告。
    """
    lines = []
    
    lines.append("# Phase 8.1: 排名位置预测力诊断报告")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 研究区间：{BACKTEST_START} ~ {BACKTEST_END}")
    lines.append(f"> 训练期：{BACKTEST_START} ~ {TRAIN_END}")
    lines.append(f"> 验证期：{VALID_START} ~ {BACKTEST_END}")
    lines.append(f"> 排名范围：16只行业ETF（排除黄金、国债）")
    lines.append(f"> 基准：沪深300（000300.SH）")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # 一、方法论
    lines.append("## 一、研究方法论")
    lines.append("")
    lines.append("**研究对象**：排名位置（Rank 1-16），不是具体ETF。")
    lines.append("**信号时点**：T日收盘后生成，从T+1开盘价计算未来收益。")
    lines.append("**未来收益口径**：")
    lines.append("- ETF未来收益 = 期末收盘价 / T+1开盘价 - 1")
    lines.append("- 基准未来收益 = 沪深300同期末收盘价 / T+1开盘价 - 1")
    lines.append("- 超额收益 = ETF未来收益 - 基准未来收益")
    lines.append("")
    lines.append("**B0.3评分配置**：")
    lines.append("- momentum_factor_enabled = False")
    lines.append("- volatility_factor_enabled = False")
    lines.append("- min_total_score = 40")
    lines.append("- 权重：trend=30%, confirm=20%, momentum=25%, volume=15%, volatility=10%")
    lines.append("")
    
    # 二、Rank 1-16完整结果表
    lines.append("## 二、Rank 1-16 完整结果表（全部区间）")
    lines.append("")
    
    for period in FORWARD_PERIODS:
        lines.append(f"### {period}个交易日")
        lines.append("")
        lines.append("| Rank | 样本数 | 平均收益 | 中位数收益 | 正收益胜率 | 平均超额 | 超额胜率 | 收益标准差 | 25%分位 | 75%分位 | 最差收益 |")
        lines.append("|------|--------|----------|------------|------------|----------|----------|------------|----------|----------|----------|")
        
        data = summary_df[(summary_df['period'] == period) & (summary_df['period_label'] == '全部')]
        for _, row in data.iterrows():
            lines.append(f"| {int(row['rank'])} | {int(row['n_samples'])} | {row['mean_return']*100:.2f}% | {row['median_return']*100:.2f}% | {row['positive_rate']*100:.1f}% | {row['mean_excess']*100:.2f}% | {row['excess_positive_rate']*100:.1f}% | {row['std_return']*100:.2f}% | {row['p25_return']*100:.2f}% | {row['p75_return']*100:.2f}% | {row['min_return']*100:.2f}% |")
        
        lines.append("")
    
    # 三、Rank 1-5 详细结果
    lines.append("## 三、Rank 1-5 详细结果（训练期 vs 验证期）")
    lines.append("")
    
    for period in FORWARD_PERIODS:
        lines.append(f"### {period}个交易日")
        lines.append("")
        lines.append("| Rank | 区间 | 样本数 | 平均收益 | 中位数收益 | 正收益胜率 | 平均超额 | 超额胜率 |")
        lines.append("|------|------|--------|----------|------------|------------|----------|----------|")
        
        for rank in range(1, 6):
            for label in ['训练期', '验证期']:
                row = summary_df[(summary_df['period'] == period) & 
                                 (summary_df['period_label'] == label) & 
                                 (summary_df['rank'] == rank)]
                if not row.empty:
                    r = row.iloc[0]
                    lines.append(f"| {rank} | {label} | {int(r['n_samples'])} | {r['mean_return']*100:.2f}% | {r['median_return']*100:.2f}% | {r['positive_rate']*100:.1f}% | {r['mean_excess']*100:.2f}% | {r['excess_positive_rate']*100:.1f}% |")
        
        lines.append("")
    
    # 四、Block Bootstrap 置信区间（按调仓日期block重采样）
    lines.append("## 四、Block Bootstrap 置信区间")
    lines.append("")
    lines.append("由于5/10/20日样本存在重叠，按调仓日期进行block bootstrap，不能将每只ETF样本视为独立。")
    lines.append("")
    
    for period in FORWARD_PERIODS:
        lines.append(f"### {period}个交易日")
        lines.append("")
        lines.append("| Rank | 样本数 | 均值 | 95% CI下限 | 95% CI上限 | CI是否包含0 |")
        lines.append("|------|--------|------|------------|------------|-------------|")
        
        for rank in range(1, 17):
            mean, lower, upper = bootstrap_results.get((period, rank), (np.nan, np.nan, np.nan))
            n_samples = len(samples_df[(samples_df['period'] == period) & (samples_df['rank'] == rank)])
            
            if not pd.isna(mean):
                ci_contains_zero = '是' if lower <= 0 <= upper else '否'
                lines.append(f"| {rank} | {n_samples} | {mean*100:.2f}% | {lower*100:.2f}% | {upper*100:.2f}% | {ci_contains_zero} |")
        
        lines.append("")
    
    # 五、排名间比较
    lines.append("## 五、排名间比较")
    lines.append("")
    
    for period in FORWARD_PERIODS:
        lines.append(f"### {period}个交易日")
        lines.append("")
        lines.append("| 比较 | A均值 | B均值 | A中位数 | B中位数 | A-B均值 | A-B中位数 |")
        lines.append("|------|-------|-------|---------|---------|---------|------------|")
        
        for key, val in comparisons.items():
            if val['period'] == period:
                lines.append(f"| {val['comparison']} | {val['a_mean']*100:.2f}% | {val['b_mean']*100:.2f}% | {val['a_median']*100:.2f}% | {val['b_median']*100:.2f}% | {val['diff_mean']*100:.2f}% | {val['diff_median']*100:.2f}% |")
        
        lines.append("")
    
    # 六、区分度检验
    lines.append("## 六、区分度检验")
    lines.append("")
    
    for period in FORWARD_PERIODS:
        lines.append(f"### {period}个交易日")
        lines.append("")
        
        subset = samples_df[(samples_df['period'] == period) & (samples_df['period_label'] == '全部')]
        
        # Spearman相关
        valid = subset[['rank', 'etf_return']].dropna()
        if len(valid) > 10:
            from scipy import stats
            spearman_r, spearman_p = stats.spearmanr(valid['rank'], valid['etf_return'])
            lines.append(f"- **Rank与未来收益Spearman相关**: r={spearman_r:.4f}, p={spearman_p:.4f}")
        
        # Rank 1-5内部差异
        top5 = subset[subset['rank'] <= 5]
        if not top5.empty:
            rank1_mean = subset[subset['rank'] == 1]['etf_return'].mean()
            rank5_mean = subset[subset['rank'] == 5]['etf_return'].mean()
            lines.append(f"- **Rank 1 vs Rank 5均值差异**: {rank1_mean*100:.2f}% vs {rank5_mean*100:.2f}% (差={ (rank1_mean - rank5_mean)*100:.2f}%)")
        
        # 训练期与验证期方向一致性
        train_data = summary_df[(summary_df['period'] == period) & (summary_df['period_label'] == '训练期')]
        valid_data = summary_df[(summary_df['period'] == period) & (summary_df['period_label'] == '验证期')]
        
        if not train_data.empty and not valid_data.empty:
            # 计算训练期和验证期的Rank 1-5平均收益
            train_top5 = train_data[train_data['rank'] <= 5]['mean_return'].mean()
            valid_top5 = valid_data[valid_data['rank'] <= 5]['mean_return'].mean()
            train_bot5 = train_data[train_data['rank'] >= 12]['mean_return'].mean()
            valid_bot5 = valid_data[valid_data['rank'] >= 12]['mean_return'].mean()
            
            train_diff = train_top5 - train_bot5
            valid_diff = valid_top5 - valid_bot5
            
            lines.append(f"- **训练期Top5-Bottom5**: {train_diff*100:.2f}%")
            lines.append(f"- **验证期Top5-Bottom5**: {valid_diff*100:.2f}%")
            lines.append(f"- **方向一致性**: {'一致' if (train_diff > 0) == (valid_diff > 0) else '不一致'} (训练期{'+' if train_diff > 0 else ''}{train_diff*100:.2f}%, 验证期{'+' if valid_diff > 0 else ''}{valid_diff*100:.2f}%)")
        
        lines.append("")
    
    # 六、预先规定结论分支
    lines.append("## 六、结论")
    lines.append("")
    
    # 判断结论分支
    conclusion = determine_conclusion_branch(samples_df, summary_df)
    
    lines.append(f"**结论分支：{conclusion['branch']}**")
    lines.append("")
    lines.append(f"{conclusion['reason']}")
    lines.append("")
    
    if conclusion['branch'] == '情况A':
        lines.append("**建议**：进入Phase 8.2，测试按排名差异化仓位。")
    elif conclusion['branch'] == '情况B':
        lines.append("**建议**：继续Top 5等权，不测试Rank 1重仓。")
    else:
        lines.append("**建议**：暂停仓位优化，进一步诊断评分因子预测力。")
    
    lines.append("")
    
    # 七、附录
    lines.append("## 七、附录")
    lines.append("")
    lines.append("### 数据文件")
    lines.append(f"- 样本数据：reports/phase8_1_rank_samples.csv")
    lines.append(f"- 汇总数据：reports/phase8_1_rank_summary.csv")
    lines.append(f"- 图表：reports/phase8_1_rank_diagnostics.png")
    lines.append("")
    
    lines.append("### 工程说明")
    lines.append("- 不修改生产策略代码和B0.3配置")
    lines.append("- 仅加载16只行业ETF及沪深300")
    lines.append("- 输出每期排名样本，允许Codex复核任意日期")
    lines.append("- Phase 8.1完成后停止，不自行开展仓位实验")
    lines.append("")
    
    report = "\n".join(lines)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    return report


def determine_conclusion_branch(samples_df, summary_df):
    """
    根据统计结果判断结论分支（A/B/C）。
    
    Returns:
        dict with 'branch' and 'reason'
    """
    
    results = {'branch': '情况C', 'reason': ''}
    
    # 检查三个期限
    all_periods_consistent = True
    
    for period in FORWARD_PERIODS:
        subset = samples_df[(samples_df['period'] == period) & (samples_df['period_label'] == '全部')]
        
        # 1. Rank 1-5内部是否有稳定递减
        top5 = subset[subset['rank'] <= 5]
        if top5.empty:
            all_periods_consistent = False
            continue
        
        # 计算Rank 1-5的平均收益
        rank_means = []
        for r in range(1, 6):
            r_mean = subset[subset['rank'] == r]['etf_return'].mean()
            rank_means.append(r_mean)
        
        # 检查是否基本递减
        decreasing = all(rank_means[i] >= rank_means[i+1] for i in range(len(rank_means)-1))
        
        # 2. 训练期和验证期方向一致性
        train_data = summary_df[(summary_df['period'] == period) & (summary_df['period_label'] == '训练期')]
        valid_data = summary_df[(summary_df['period'] == period) & (summary_df['period_label'] == '验证期')]
        
        direction_consistent = True
        if not train_data.empty and not valid_data.empty:
            train_top5 = train_data[train_data['rank'] <= 5]['mean_return'].mean()
            valid_top5 = valid_data[valid_data['rank'] <= 5]['mean_return'].mean()
            train_bot = train_data[train_data['rank'] >= 12]['mean_return'].mean()
            valid_bot = valid_data[valid_data['rank'] >= 12]['mean_return'].mean()
            
            train_diff = train_top5 - train_bot
            valid_diff = valid_top5 - valid_bot
            
            direction_consistent = (train_diff > 0) == (valid_diff > 0)
        
        # 3. 不是单一年份贡献（简化：检查样本分布）
        samples_by_year = subset.groupby(subset['date'].apply(lambda d: d.year)).size()
        not_single_year = len(samples_by_year) >= 3 and samples_by_year.min() >= 5
        
        if not decreasing or not direction_consistent or not not_single_year:
            all_periods_consistent = False
    
    # 判断分支
    if all_periods_consistent:
        results['branch'] = '情况A'
        results['reason'] = '训练期和验证期均显示：高排名收益和超额胜率更高；Rank 1-5具有稳定区分度；不是单一年份贡献。'
    else:
        # 检查是否Top 5优于其他但内部无区分度
        top5_better = True
        for period in FORWARD_PERIODS:
            subset = samples_df[(samples_df['period'] == period) & (samples_df['period_label'] == '全部')]
            top5_mean = subset[subset['rank'] <= 5]['etf_return'].mean()
            bottom_mean = subset[subset['rank'] >= 12]['etf_return'].mean()
            if top5_mean <= bottom_mean:
                top5_better = False
                break
        
        if top5_better:
            results['branch'] = '情况B'
            results['reason'] = 'Top 5优于其他排名，但Rank 1-5内部无稳定区分度。说明评分适合筛选Top 5，但不适合决定内部权重。'
        else:
            results['branch'] = '情况C'
            results['reason'] = '排名整体缺乏预测力，或训练期与验证期方向不一致。'
    
    return results


# ============================================================================
# 九、主入口
# ============================================================================

if __name__ == '__main__':
    # 断言只加载16只行业ETF及沪深300
    assert len(INDUSTRY_ETFS) == 16, f"行业ETF数量应为16，实际为{len(INDUSTRY_ETFS)}"
    assert BENCHMARK_TICKER == '000300.SH', f"基准应为000300.SH，实际为{BENCHMARK_TICKER}"
    
    # 运行诊断
    samples_df, summary_df, comparisons, bootstrap_results = run_rank_position_diagnostics()
    
    print("\n" + "=" * 80)
    print("Phase 8.1 完成")
    print("=" * 80)
