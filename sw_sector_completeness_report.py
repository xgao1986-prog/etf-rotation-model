# -*- coding: utf-8 -*-
"""
sw_sector_completeness_report.py - 申万行业数据完整性报告

v1.3 Step 1 验收报告：
- 31个申万2021版一级行业数据完整性
- 研究池 vs 交易映射池分离
- 不改交易规则、不跑组合回测
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime
from config import SECTOR_INDEX_UNIVERSE

DB_PATH = 'database/etf_model.db'


def generate_completeness_report():
    """生成30个申万行业数据完整性报告"""
    
    print("="*80)
    print("申万一级行业数据完整性报告 (v1.3 Step 1)")
    print("="*80)
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据库: {DB_PATH}")
    print()
    
    conn = sqlite3.connect(DB_PATH)
    
    # 1. 获取所有板块数据概览
    print("[1/5] 数据概览")
    print("-"*80)
    
    cursor = conn.execute("""
        SELECT ticker, COUNT(*) as cnt, MIN(date) as start, MAX(date) as end
        FROM market_data
        WHERE ticker LIKE 'SECTOR_%'
        GROUP BY ticker
        ORDER BY ticker
    """)
    
    rows = cursor.fetchall()
    total_sectors = len(rows)
    print(f"数据库中板块总数: {total_sectors}")
    
    # 2. 详细统计每个行业
    print("\n[2/5] 行业数据明细")
    print("-"*80)
    
    report_data = []
    
    for ticker, cnt, start, end in rows:
        code = ticker.replace('SECTOR_', '')  # e.g., '801010'
        code_with_suffix = code + '.SI'
        
        # 从config获取名称和ETF映射
        sector_info = SECTOR_INDEX_UNIVERSE.get(code_with_suffix, ('未知', []))
        if isinstance(sector_info, tuple):
            name = sector_info[0]
            etfs = sector_info[1] if len(sector_info) > 1 else []
        else:
            name = '未知'
            etfs = []
        
        start_dt = pd.to_datetime(start)
        end_dt = pd.to_datetime(end)
        
        # 检查缺失日期
        cursor = conn.execute("SELECT date FROM market_data WHERE ticker = ? ORDER BY date", (ticker,))
        dates = [pd.to_datetime(r[0]) for r in cursor.fetchall()]
        
        date_range = pd.date_range(start=start_dt, end=end_dt, freq='B')
        actual_dates = set(dates)
        expected_dates = set(date_range)
        missing_dates = expected_dates - actual_dates
        
        missing_rate = len(missing_dates) / len(expected_dates) if expected_dates else 0
        
        # 数据覆盖类型
        if start_dt <= pd.Timestamp('2019-01-05'):
            coverage = '完整'
        elif start_dt <= pd.Timestamp('2021-12-20'):
            coverage = '中期'
        else:
            coverage = '近期'
        
        has_mapping = len(etfs) > 0
        
        report_data.append({
            'code': code,
            'name': name,
            'ticker': ticker,
            'rows': cnt,
            'start': start_dt.strftime('%Y-%m-%d'),
            'end': end_dt.strftime('%Y-%m-%d'),
            'missing_rate': missing_rate,
            'missing_days': len(missing_dates),
            'coverage': coverage,
            'has_mapping': has_mapping,
            'etfs': etfs,
        })
    
    # 打印表格
    print(f"\n{'代码':<8} {'名称':<10} {'行数':>6} {'起始':<12} {'结束':<12} {'缺失率':>8} {'覆盖':<6} {'映射':<6}")
    print("-"*80)
    
    for d in report_data:
        mapping = f"{len(d['etfs'])}ETF" if d['has_mapping'] else '无'
        print(f"{d['code']:<8} {d['name']:<10} {d['rows']:>6} {d['start']:<12} {d['end']:<12} {d['missing_rate']:>7.2%} {d['coverage']:<6} {mapping:<6}")
    
    # 3. 分类统计
    print("\n[3/5] 分类统计")
    print("-"*80)
    
    df_report = pd.DataFrame(report_data)
    
    print("\n按数据覆盖分类:")
    for cov in ['完整', '中期', '近期']:
        count = len(df_report[df_report['coverage'] == cov])
        print(f"  {cov}: {count} 个")
    
    mapped = df_report[df_report['has_mapping'] == True]
    unmapped = df_report[df_report['has_mapping'] == False]
    
    print(f"\n按交易映射分类:")
    print(f"  有ETF映射: {len(mapped)} 个")
    for _, row in mapped.iterrows():
        print(f"    {row['code']} {row['name']} -> {', '.join(row['etfs'])}")
    
    print(f"\n  无ETF映射: {len(unmapped)} 个 (仅研究/观察用)")
    for _, row in unmapped.iterrows():
        print(f"    {row['code']} {row['name']}")
    
    # 4. 数据质量评估
    print("\n[4/5] 数据质量评估")
    print("-"*80)
    
    total_missing = df_report['missing_days'].sum()
    avg_missing_rate = df_report['missing_rate'].mean()
    max_missing = df_report['missing_rate'].max()
    
    print(f"总缺失交易日: {total_missing}")
    print(f"平均缺失率: {avg_missing_rate:.2%}")
    print(f"最大缺失率: {max_missing:.2%}")
    
    high_missing = df_report[df_report['missing_rate'] > 0.01]
    if len(high_missing) > 0:
        print(f"\n缺失率>1%的板块 (需关注):")
        for _, row in high_missing.iterrows():
            print(f"  {row['code']} {row['name']}: {row['missing_rate']:.2%} ({row['missing_days']}天)")
    else:
        print(f"\n所有板块缺失率<1%，数据质量良好")
    
    # 5. 研究池 vs 交易映射池分离说明
    print("\n[5/5] 研究池 vs 交易映射池分离")
    print("-"*80)
    
    print(f"""
研究池 (Research Pool):
  - 用途: 市场风格判断、行业轮动观察、成交额排名
  - 数量: {len(df_report)} 个行业
  - 包含: 所有有数据和无数据映射的行业

交易映射池 (Trading Mapping Pool):
  - 用途: 实际ETF交易信号映射
  - 数量: {len(mapped)} 个行业
  - 包含: 只有有ETF对应行业才进入交易映射
  - 映射关系: 配置在 SECTOR_INDEX_UNIVERSE 中

分离原则:
  1. 30个行业全部进入研究池，用于观察市场结构
  2. 只有{len(mapped)}个行业有ETF映射，用于实际交易
  3. 无映射行业不用于交易，但可用于:
     - 市场风格判断（成长/价值/周期）
     - 行业轮动热力图
     - 大盘状态辅助判断
  4. 当前不改交易规则，不跑组合回测
""")
    
    # 保存报告
    report_path = 'reports/sw_sector_completeness_report.md'
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# 申万一级行业数据完整性报告 (v1.3 Step 1)\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**数据库**: {DB_PATH}\n\n")
        
        f.write("## 1. 数据概览\n\n")
        f.write(f"数据库中板块总数: {total_sectors}\n\n")
        
        f.write("## 2. 行业数据明细\n\n")
        f.write(f"| 代码 | 名称 | 行数 | 起始日期 | 结束日期 | 缺失率 | 覆盖 | 映射 |\n")
        f.write(f"|------|------|------|----------|----------|--------|------|------|\n")
        for d in report_data:
            mapping = f"{len(d['etfs'])}ETF" if d['has_mapping'] else '无'
            f.write(f"| {d['code']} | {d['name']} | {d['rows']} | {d['start']} | {d['end']} | {d['missing_rate']:.2%} | {d['coverage']} | {mapping} |\n")
        
        f.write("\n## 3. 分类统计\n\n")
        f.write("### 按数据覆盖分类\n\n")
        for cov in ['完整', '中期', '近期']:
            count = len(df_report[df_report['coverage'] == cov])
            f.write(f"- {cov}: {count} 个\n")
        
        f.write("\n### 按交易映射分类\n\n")
        f.write(f"**有ETF映射 ({len(mapped)} 个):**\n\n")
        for _, row in mapped.iterrows():
            f.write(f"- {row['code']} {row['name']} -> {', '.join(row['etfs'])}\n")
        
        f.write(f"\n**无ETF映射 ({len(unmapped)} 个):**\n\n")
        for _, row in unmapped.iterrows():
            f.write(f"- {row['code']} {row['name']}\n")
        
        f.write("\n## 4. 数据质量评估\n\n")
        f.write(f"- 总缺失交易日: {total_missing}\n")
        f.write(f"- 平均缺失率: {avg_missing_rate:.2%}\n")
        f.write(f"- 最大缺失率: {max_missing:.2%}\n")
        
        if len(high_missing) > 0:
            f.write(f"\n**缺失率>1%的板块 (需关注):**\n\n")
            for _, row in high_missing.iterrows():
                f.write(f"- {row['code']} {row['name']}: {row['missing_rate']:.2%} ({row['missing_days']}天)\n")
        else:
            f.write(f"\n所有板块缺失率<1%，数据质量良好\n")
        
        f.write("\n## 5. 研究池 vs 交易映射池分离\n\n")
        f.write(f"""
**研究池 (Research Pool):**
- 用途: 市场风格判断、行业轮动观察、成交额排名
- 数量: {len(df_report)} 个行业
- 包含: 所有有数据和无数据映射的行业

**交易映射池 (Trading Mapping Pool):**
- 用途: 实际ETF交易信号映射
- 数量: {len(mapped)} 个行业
- 包含: 只有有ETF对应行业才进入交易映射

**分离原则:**
1. 30个行业全部进入研究池，用于观察市场结构
2. {len(mapped)}个行业有ETF映射，用于实际交易
3. 无映射行业不用于交易，但可用于市场风格判断
4. 当前不改交易规则，不跑组合回测
""")
        
        f.write("\n## 6. 版本边界\n\n")
        f.write("- v1.2.2 已收口 (验收点: d9fd9e7)\n")
        f.write("- 当前为 v1.3 研究阶段 Step 1\n")
        f.write("- 不改交易规则\n")
        f.write("- 不跑组合回测\n")
        f.write("- 下一步: Step 2 成分股试点行业选择 (待验收后)\n")
    
    print(f"\n报告已保存: {report_path}")
    
    conn.close()
    
    return df_report


if __name__ == '__main__':
    df = generate_completeness_report()
    print(f"\n[OK] 数据完整性报告生成完成")
