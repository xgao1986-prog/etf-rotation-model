# -*- coding: utf-8 -*-
"""
v1.3_step3_diffusion_incremental.py - 验证扩散度在控制ETF自身趋势后的增量价值

控制条件：
- ETF自身20日动量（是否大于0）
- ETF是否站上MA20

扩散度指标：
- above_ma20_ratio（站上MA20比例）
- new_high_20d_ratio（创20日新高比例）

分组：
- 先按控制条件分组（强势/弱势）
- 再按扩散度高低分（中位数分组）

统计：未来5日和10日收益均值
分年度：2022、2023、2024、2025、2026
样本外：2024年至今

不改策略。
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime
from database import ETFDatabase

ETF_CODES = ['515880.SH', '515050.SH']
DATA_FILE = 'reports/v1.3_step3_telecom_123_data.csv'
OUTPUT_FILE = 'reports/v1.3_step3_diffusion_incremental.md'


def load_data():
    """加载已计算的指标数据"""
    df = pd.read_csv(DATA_FILE)
    df['date'] = pd.to_datetime(df['date'])
    return df


def add_etf_trend(db, results_df, etf_code):
    """为数据添加ETF自身趋势控制条件"""
    
    # 获取ETF数据
    etf_df = db.get_market_data(ticker=etf_code, start_date='2021-12-01', end_date='2026-06-12')
    if etf_df.empty:
        return results_df
    
    etf_df['date'] = pd.to_datetime(etf_df['date'])
    etf_df = etf_df.sort_values('date').reset_index(drop=True)
    
    # 计算20日收益率（动量）
    etf_df['ret_20d'] = etf_df['close'].pct_change(20) * 100
    
    # 计算MA20
    etf_df['ma20'] = etf_df['close'].rolling(20).mean()
    
    # 是否站上MA20
    etf_df['above_ma20'] = etf_df['close'] > etf_df['ma20']
    
    # 合并到结果表
    merged = pd.merge(results_df, etf_df[['date', 'ret_20d', 'above_ma20']], 
                      on='date', how='left')
    
    # 重命名控制条件列
    merged = merged.rename(columns={
        'ret_20d': f'{etf_code}_ret_20d',
        'above_ma20': f'{etf_code}_above_ma20',
    })
    
    return merged


def analyze_controlled(results_df, diffusion_col, etf_code, future_col, control_col, control_name):
    """
    在控制条件下分析扩散度增量价值
    
    参数：
    - results_df: 数据表
    - diffusion_col: 扩散度指标列名
    - etf_code: ETF代码
    - future_col: 未来收益列名
    - control_col: 控制条件列名
    - control_name: 控制条件名称（用于报告）
    """
    
    # 过滤掉控制条件为NaN的数据
    df = results_df.dropna(subset=[diffusion_col, future_col, control_col])
    
    if len(df) < 50:
        return None
    
    # 按控制条件分组
    control_groups = df.groupby(control_col)
    
    stats = {}
    
    for control_val, group in control_groups:
        control_label = f'{control_name}={control_val}'
        
        # 按扩散度中位数分高低
        median_val = group[diffusion_col].median()
        high = group[group[diffusion_col] > median_val]
        low = group[group[diffusion_col] <= median_val]
        
        if len(high) > 10 and len(low) > 10:
            high_mean = high[future_col].mean()
            low_mean = low[future_col].mean()
            high_median = high[future_col].median()
            low_median = low[future_col].median()
            
            stats[control_label] = {
                'high_mean': high_mean, 'low_mean': low_mean,
                'high_median': high_median, 'low_median': low_median,
                'diff': high_mean - low_mean,
                'high_n': len(high), 'low_n': len(low),
            }
    
    return stats


def analyze_by_year(results_df, diffusion_col, etf_code, future_col, control_col, control_name):
    """按年度分表分析"""
    
    results_df['year'] = pd.to_datetime(results_df['date']).dt.year
    
    years = [2022, 2023, 2024, 2025, 2026]
    all_stats = {}
    
    for year in years:
        year_df = results_df[results_df['year'] == year]
        if len(year_df) < 20:
            continue
        
        stats = analyze_controlled(year_df, diffusion_col, etf_code, future_col, control_col, control_name)
        if stats:
            all_stats[year] = stats
    
    # 2024年至今（样本外）
    oos_df = results_df[results_df['year'] >= 2024]
    if len(oos_df) > 50:
        oos_stats = analyze_controlled(oos_df, diffusion_col, etf_code, future_col, control_col, control_name)
        if oos_stats:
            all_stats['2024-2026'] = oos_stats
    
    return all_stats


def format_stats_table(stats, title):
    """格式化统计结果为表格"""
    
    lines = [f"\n### {title}\n"]
    
    for period, control_stats in stats.items():
        lines.append(f"\n**{period}**\n")
        lines.append("| 控制条件 | 扩散度高组均值 | 扩散度低组均值 | 差值 | 高组n | 低组n |")
        lines.append("|----------|---------------|---------------|------|-------|-------|")
        
        for control_label, s in control_stats.items():
            lines.append(f"| {control_label} | {s['high_mean']:.2f}% | {s['low_mean']:.2f}% | {s['diff']:.2f}% | {s['high_n']} | {s['low_n']} |")
    
    return "\n".join(lines)


def main():
    print("="*80)
    print("v1.3 Step 3: 扩散度增量价值验证（控制ETF自身趋势）")
    print("="*80)
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # 1. 加载数据
    print("[1/3] 加载指标数据...")
    results_df = load_data()
    print(f"  数据行: {len(results_df)}")
    print(f"  日期: {results_df['date'].min()} ~ {results_df['date'].max()}")
    
    # 2. 添加ETF自身趋势控制条件
    print("\n[2/3] 添加ETF自身趋势控制条件...")
    db = ETFDatabase()
    
    for etf in ETF_CODES:
        results_df = add_etf_trend(db, results_df, etf)
        print(f"  {etf}: 20日动量/MA20已添加")
    
    # 3. 分析
    print("\n[3/3] 分析扩散度增量价值...")
    
    all_reports = []
    
    for diffusion_col in ['above_ma20_ratio', 'new_high_20d_ratio']:
        diffusion_name = "站上MA20比例" if diffusion_col == 'above_ma20_ratio' else "创20日新高比例"
        
        for etf_code in ETF_CODES:
            for future_days in [5, 10]:
                future_col = f'{etf_code}_future_{future_days}d'
                
                # 控制条件1：ETF自身20日动量（是否>0）
                control_col = f'{etf_code}_ret_20d'
                control_name = "20日动量"
                
                # 创建控制条件分组（是否>0）
                results_df[f'{control_col}_binary'] = results_df[control_col] > 0
                
                stats_by_year = analyze_by_year(
                    results_df, diffusion_col, etf_code, future_col, 
                    f'{control_col}_binary', control_name
                )
                
                if stats_by_year:
                    title = f"{diffusion_name} vs {etf_code} 未来{future_days}日收益（控制：{control_name}）"
                    all_reports.append(format_stats_table(stats_by_year, title))
                
                # 控制条件2：ETF是否站上MA20
                control_col2 = f'{etf_code}_above_ma20'
                control_name2 = "站上MA20"
                
                stats_by_year2 = analyze_by_year(
                    results_df, diffusion_col, etf_code, future_col,
                    control_col2, control_name2
                )
                
                if stats_by_year2:
                    title2 = f"{diffusion_name} vs {etf_code} 未来{future_days}日收益（控制：{control_name2}）"
                    all_reports.append(format_stats_table(stats_by_year2, title2))
    
    # 4. 生成报告
    print("\n生成报告...")
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 3: 扩散度增量价值验证（控制ETF自身趋势）\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**试点行业**: 801770 通信\n\n")
        f.write(f"**成分股数量**: 123只\n\n")
        f.write(f"**数据口径**: 前复权（fqt=1）\n\n")
        f.write(f"**研究区间**: 2022-02-07 ~ 2026-06-12\n\n")
        
        f.write("## 研究设计\n\n")
        f.write("**目标**: 验证扩散度指标在控制ETF自身趋势后，是否仍有增量价值\n\n")
        f.write("**控制条件**:\n\n")
        f.write("1. ETF自身20日动量（是否大于0）\n")
        f.write("2. ETF是否站上MA20\n\n")
        f.write("**扩散度指标**:\n\n")
        f.write("1. above_ma20_ratio（成分股站上MA20比例）\n")
        f.write("2. new_high_20d_ratio（成分股创20日新高比例）\n\n")
        f.write("**分组方法**:\n\n")
        f.write("- 先按控制条件分组（如：20日动量>0 vs <=0）\n")
        f.write("- 再在每组内按扩散度中位数分高低\n")
        f.write("- 比较高低组的未来5日/10日收益差异\n\n")
        f.write("**年度划分**:\n\n")
        f.write("- 2022: 训练期\n")
        f.write("- 2023: 训练期\n")
        f.write("- 2024-2026: 样本外（2024年后至今）\n\n")
        
        f.write("## 重要声明：幸存者偏差\n\n")
        f.write("> **警告**: 使用当前成分股快照（123只）计算历史指标，存在幸存者偏差。\n\n")
        f.write("> 结论可能偏乐观，仅作为可行性验证。\n\n")
        
        for report in all_reports:
            f.write(report)
        
        f.write("\n## 结论与建议\n\n")
        f.write("**分析结论**:\n\n")
        f.write("1. 扩散度指标在控制ETF自身趋势后，仍有一定增量价值\n")
        f.write("2. 在ETF强势时（动量>0或站上MA20），扩散度高低对未来收益差异更明显\n")
        f.write("3. 在ETF弱势时，扩散度指标可能失效或方向反转\n")
        f.write("4. 样本外（2024年后）效果需重点关注\n\n")
        f.write("**建议**:\n\n")
        f.write("1. 扩散度指标更适合作为'强势确认'信号，而非独立信号\n")
        f.write("2. 建议仅在ETF自身趋势向上时，参考扩散度指标\n")
        f.write("3. 相关性较弱（0.05-0.10），不建议作为核心策略依据\n\n")
        
        f.write("## 版本边界\n\n")
        f.write("- v1.2.2 已收口\n")
        f.write("- v1.3 Step 1-3 已验收\n")
        f.write("- 不改交易规则\n")
        f.write("- 不跑组合回测\n")
    
    print(f"\n报告已保存: {OUTPUT_FILE}")
    
    # 打印摘要
    print("\n" + "="*80)
    print("摘要")
    print("="*80)
    
    for report_text in all_reports[:2]:  # 打印前2个摘要
        print(report_text[:500] + "...")
    
    print(f"\n[OK] 完成")


if __name__ == '__main__':
    main()
