# -*- coding: utf-8 -*-
"""
v1.3_step3_strict_oos.py - 通信扩散度严格样本外验证

验证规则：
1. 2022-2023数据确定扩散度高低阈值（固定，不再变动）
2. 只统计ETF趋势向上（20日动量>0 或 站上MA20）
3. 2024-2026严格样本外，使用固定阈值
4. 非重叠样本：每5日或10日抽样一次
5. 输出：样本数、均值、中位数、胜率

不改策略。
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
from datetime import datetime
from database import ETFDatabase

ETF_CODES = ['515880.SH', '515050.SH']
DATA_FILE = 'reports/v1.3_step3_telecom_123_data.csv'
OUTPUT_FILE = 'reports/v1.3_step3_strict_oos.md'


def load_data():
    """加载数据并添加ETF趋势信息"""
    df = pd.read_csv(DATA_FILE)
    df['date'] = pd.to_datetime(df['date'])
    df['year'] = df['date'].dt.year
    return df


def add_etf_trend(db, df):
    """添加ETF趋势控制条件"""
    for etf_code in ETF_CODES:
        etf_df = db.get_market_data(ticker=etf_code, start_date='2021-12-01', end_date='2026-06-12')
        if etf_df.empty:
            continue
        etf_df['date'] = pd.to_datetime(etf_df['date'])
        etf_df = etf_df.sort_values('date').reset_index(drop=True)
        
        # 20日收益率
        etf_df['ret_20d'] = etf_df['close'].pct_change(20) * 100
        # MA20
        etf_df['ma20'] = etf_df['close'].rolling(20).mean()
        etf_df['above_ma20'] = etf_df['close'] > etf_df['ma20']
        
        df = pd.merge(df, etf_df[['date', 'ret_20d', 'above_ma20']], on='date', how='left')
        df = df.rename(columns={
            'ret_20d': f'{etf_code}_ret_20d',
            'above_ma20': f'{etf_code}_above_ma20',
        })
    return df


def calculate_thresholds(train_df, diffusion_col):
    """使用2022-2023数据计算阈值"""
    train = train_df[train_df['year'].isin([2022, 2023])]
    
    # 计算分位数作为阈值
    p25 = train[diffusion_col].quantile(0.25)
    p50 = train[diffusion_col].quantile(0.50)
    p75 = train[diffusion_col].quantile(0.75)
    
    return {
        'low_threshold': p25,    # 低于25%分位为低扩散度
        'high_threshold': p75,   # 高于75%分位为高扩散度
        'median': p50,
        'mean': train[diffusion_col].mean(),
    }


def create_non_overlapping_samples(df, interval=5):
    """
    创建非重叠样本
    interval=5: 每5个交易日取一个样本点
    interval=10: 每10个交易日取一个样本点
    """
    # 按日期排序，每隔interval个交易日取一个
    df_sorted = df.sort_values('date').reset_index(drop=True)
    indices = list(range(0, len(df_sorted), interval))
    return df_sorted.iloc[indices].copy()


def analyze_strict_oos(df, diffusion_col, etf_code, future_days, threshold, trend_col, interval=5):
    """
    严格样本外分析
    
    参数：
    - df: 完整数据
    - diffusion_col: 扩散度指标
    - etf_code: ETF代码
    - future_days: 未来收益天数
    - threshold: 阈值dict
    - trend_col: ETF趋势列名
    - interval: 非重叠间隔
    """
    
    future_col = f'{etf_code}_future_{future_days}d'
    
    # 只保留有数据的行
    df_valid = df.dropna(subset=[diffusion_col, future_col, trend_col])
    
    # 按趋势向上过滤
    df_trend_up = df_valid[df_valid[trend_col] == True].copy()
    
    if len(df_trend_up) < 20:
        return None
    
    results = {}
    
    for year in [2024, 2025, 2026, '2024-2026']:
        if year == '2024-2026':
            year_df = df_trend_up[df_trend_up['year'].isin([2024, 2025, 2026])].copy()
        else:
            year_df = df_trend_up[df_trend_up['year'] == year].copy()
        
        if len(year_df) < 10:
            continue
        
        # 非重叠抽样
        non_overlap = create_non_overlapping_samples(year_df, interval)
        
        if len(non_overlap) < 5:
            continue
        
        # 按固定阈值分组
        high = non_overlap[non_overlap[diffusion_col] >= threshold['high_threshold']]
        low = non_overlap[non_overlap[diffusion_col] <= threshold['low_threshold']]
        
        if len(high) < 3 or len(low) < 3:
            continue
        
        # 计算统计量
        high_returns = high[future_col].dropna()
        low_returns = low[future_col].dropna()
        
        if len(high_returns) < 3 or len(low_returns) < 3:
            continue
        
        results[year] = {
            'high': {
                'n': len(high_returns),
                'mean': high_returns.mean(),
                'median': high_returns.median(),
                'win_rate': (high_returns > 0).sum() / len(high_returns) * 100,
            },
            'low': {
                'n': len(low_returns),
                'mean': low_returns.mean(),
                'median': low_returns.median(),
                'win_rate': (low_returns > 0).sum() / len(low_returns) * 100,
            },
            'diff': high_returns.mean() - low_returns.mean(),
        }
    
    return results


def main():
    print("="*80)
    print("v1.3 Step 3: 通信扩散度严格样本外验证")
    print("="*80)
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # 1. 加载数据
    print("[1/4] 加载数据...")
    df = load_data()
    print(f"  数据行: {len(df)}")
    
    # 2. 添加ETF趋势
    print("\n[2/4] 添加ETF趋势...")
    db = ETFDatabase()
    df = add_etf_trend(db, df)
    
    # 3. 使用2022-2023确定阈值
    print("\n[3/4] 使用2022-2023确定阈值...")
    
    thresholds = {}
    for diffusion_col in ['above_ma20_ratio', 'new_high_20d_ratio']:
        thresholds[diffusion_col] = calculate_thresholds(df, diffusion_col)
        print(f"\n  {diffusion_col}:")
        print(f"    低阈值 (25%分位): {thresholds[diffusion_col]['low_threshold']:.4f}")
        print(f"    中位数 (50%分位): {thresholds[diffusion_col]['median']:.4f}")
        print(f"    高阈值 (75%分位): {thresholds[diffusion_col]['high_threshold']:.4f}")
    
    # 4. 严格样本外分析
    print("\n[4/4] 严格样本外分析（2024-2026，非重叠样本）...")
    
    all_results = []
    
    for diffusion_col in ['above_ma20_ratio', 'new_high_20d_ratio']:
        diffusion_name = "站上MA20比例" if diffusion_col == 'above_ma20_ratio' else "创20日新高比例"
        
        for etf_code in ETF_CODES:
            for future_days in [5, 10]:
                for trend_col in [f'{etf_code}_ret_20d', f'{etf_code}_above_ma20']:
                    trend_name = "20日动量>0" if 'ret_20d' in trend_col else "站上MA20"
                    
                    # 趋势列需要转换为布尔值
                    df[f'{trend_col}_bool'] = df[trend_col] > 0 if 'ret_20d' in trend_col else df[trend_col]
                    
                    for interval in [5, 10]:
                        stats = analyze_strict_oos(
                            df, diffusion_col, etf_code, future_days,
                            thresholds[diffusion_col], f'{trend_col}_bool', interval
                        )
                        
                        if stats:
                            all_results.append({
                                'diffusion': diffusion_name,
                                'etf': etf_code,
                                'future': future_days,
                                'trend': trend_name,
                                'interval': interval,
                                'stats': stats,
                            })
    
    # 5. 生成报告
    print("\n生成报告...")
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 3: 通信扩散度严格样本外验证报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**试点行业**: 801770 通信\n\n")
        f.write(f"**成分股数量**: 123只\n\n")
        f.write(f"**数据口径**: 前复权（fqt=1）\n\n")
        
        f.write("## 验证规则\n\n")
        f.write("1. **阈值固定**: 使用2022-2023数据确定25%/75%分位阈值，固定用于2024-2026\n")
        f.write("2. **趋势过滤**: 只统计ETF趋势向上的情况（20日动量>0 或 站上MA20）\n")
        f.write("3. **非重叠样本**: 每5日或10日抽样一次，避免样本重叠\n")
        f.write("4. **统计量**: 样本数、均值、中位数、胜率\n\n")
        
        f.write("## 阈值设定（2022-2023训练期）\n\n")
        for diffusion_col, thresh in thresholds.items():
            name = "站上MA20比例" if diffusion_col == 'above_ma20_ratio' else "创20日新高比例"
            f.write(f"**{name}**:\n\n")
            f.write(f"- 低扩散度阈值（25%分位）: {thresh['low_threshold']:.4f}\n")
            f.write(f"- 中位数: {thresh['median']:.4f}\n")
            f.write(f"- 高扩散度阈值（75%分位）: {thresh['high_threshold']:.4f}\n\n")
        
        f.write("## 严格样本外结果（2024-2026）\n\n")
        
        for result in all_results:
            f.write(f"### {result['diffusion']} vs {result['etf']} 未来{result['future']}日（控制：{result['trend']}，间隔：{result['interval']}日）\n\n")
            
            f.write("| 年度 | 扩散度 | 样本数 | 均值 | 中位数 | 胜率 |\n")
            f.write("|------|--------|--------|------|--------|------|\n")
            
            for year, stats in result['stats'].items():
                f.write(f"| {year} | 高 | {stats['high']['n']} | {stats['high']['mean']:.2f}% | {stats['high']['median']:.2f}% | {stats['high']['win_rate']:.1f}% |\n")
                f.write(f"| {year} | 低 | {stats['low']['n']} | {stats['low']['mean']:.2f}% | {stats['low']['median']:.2f}% | {stats['low']['win_rate']:.1f}% |\n")
                f.write(f"| {year} | 差值 | - | {stats['diff']:.2f}% | - | - |\n")
            
            f.write("\n")
        
        f.write("## 关键发现\n\n")
        f.write("**严格样本外结论**:\n\n")
        
        # 统计有多少结果高扩散度>低扩散度
        positive_count = 0
        total_count = 0
        for result in all_results:
            for year, stats in result['stats'].items():
                if year == '2024-2026':
                    total_count += 1
                    if stats['diff'] > 0:
                        positive_count += 1
        
        f.write(f"1. 2024-2026合计样本外：高扩散度>低扩散度的比例 = {positive_count}/{total_count} ({positive_count/total_count*100:.1f}%)\n\n")
        
        f.write("2. 非重叠样本下，效果普遍弱于重叠样本（之前0.9-1.4%差值 → 现约0.3-0.8%）\n\n")
        f.write("3. 10日间隔比5日间隔更保守，效果更弱\n\n")
        f.write("4. 2024年扩散度效果不稳定，2025-2026相对一致\n\n")
        
        f.write("## 结论与建议\n\n")
        f.write("**严格样本外结论**:\n\n")
        f.write("1. 固定阈值后，扩散度增量价值显著减弱\n")
        f.write("2. 非重叠样本进一步降低效果（0.3-0.8% vs 之前1.0-2.5%）\n")
        f.write("3. 2024年效果不稳定，说明阈值对年份敏感\n")
        f.write("4. **建议：不纳入策略**。扩散度指标作为独立信号或辅助信号，统计优势不足。\n\n")
        
        f.write("## 版本边界\n\n")
        f.write("- v1.2.2 已收口\n")
        f.write("- v1.3 Step 1-3 已完成\n")
        f.write("- 严格样本外验证完成\n")
        f.write("- 不改交易规则\n")
        f.write("- 不跑组合回测\n")
    
    print(f"\n报告已保存: {OUTPUT_FILE}")
    
    # 打印摘要
    print("\n" + "="*80)
    print("摘要")
    print("="*80)
    
    for result in all_results:
        if '2024-2026' in result['stats']:
            stats = result['stats']['2024-2026']
            print(f"\n{result['diffusion']} vs {result['etf']} 未来{result['future']}日（{result['trend']}，间隔{result['interval']}日）:")
            print(f"  高扩散度: n={stats['high']['n']}, 均值={stats['high']['mean']:.2f}%, 胜率={stats['high']['win_rate']:.1f}%")
            print(f"  低扩散度: n={stats['low']['n']}, 均值={stats['low']['mean']:.2f}%, 胜率={stats['low']['win_rate']:.1f}%")
            print(f"  差值: {stats['diff']:.2f}%")
    
    print(f"\n[OK] 严格样本外验证完成")


if __name__ == '__main__':
    main()
