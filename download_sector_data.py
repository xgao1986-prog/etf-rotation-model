# -*- coding: utf-8 -*-
"""
download_sector_data.py - 下载申万一级行业指数数据并入库

1. 获取18个申万一级行业指数数据
2. 统一数据口径（板块指数是价格指数，不复权）
3. 存入数据库
4. 验证数据完整性
5. 输出映射表和验证报告
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime
import time
import json

from data_fetcher import DataFetcher
from database import ETFDatabase
from config import (
    SECTOR_INDEX_UNIVERSE, ETF_TO_SECTOR_MAPPING, SECTOR_CODES,
    ETF_UNIVERSE, CONCEPT_UNIVERSE, DEFENSE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE, CORE_UNIVERSE
)


def download_sectors():
    """下载所有板块指数数据"""
    print("="*80)
    print("下载申万一级行业指数数据")
    print("="*80)
    
    fetcher = DataFetcher()
    db = ETFDatabase()
    
    # 获取数据截止日期
    conn = sqlite3.connect(db.db_path)
    cursor = conn.execute('SELECT MAX(date) FROM market_data')
    max_date = cursor.fetchone()[0]
    conn.close()
    print(f"\n数据库最新数据日期: {max_date}")
    print(f"板块指数下载起始日期: 2019-01-01")
    print(f"板块指数下载截止日期: {max_date}")
    print(f"共 {len(SECTOR_CODES)} 个板块指数:\n")
    
    for code in SECTOR_CODES:
        name = SECTOR_INDEX_UNIVERSE.get(f"{code}.SI", ('', []))[0]
        print(f"  {code} - {name}")
    
    print("\n开始下载...")
    
    all_data = []
    failed = []
    
    for code in SECTOR_CODES:
        name = SECTOR_INDEX_UNIVERSE.get(f"{code}.SI", ('', []))[0]
        print(f"\n[{code}] {name} ...", end='')
        
        try:
            df = fetcher.fetch_sector_history(code, '2019-01-01', max_date)
            if not df.empty:
                print(f" OK ({len(df)} rows, {df['date'].min().date()} ~ {df['date'].max().date()})")
                all_data.append(df)
            else:
                print(f" EMPTY")
                failed.append((code, name, 'empty'))
        except Exception as e:
            print(f" FAILED: {e}")
            failed.append((code, name, str(e)))
        
        time.sleep(0.5)
    
    if not all_data:
        print("\n错误：没有获取到任何板块数据")
        return None
    
    combined = pd.concat(all_data, ignore_index=True)
    print(f"\n{'='*80}")
    print(f"下载完成：共 {len(combined)} 行数据，{combined['ticker'].nunique()} 个板块")
    print(f"日期范围：{combined['date'].min()} ~ {combined['date'].max()}")
    
    # 存入数据库
    print(f"\n正在存入数据库...")
    
    # 注意：板块指数是价格指数（不复权），adjust_type='none'
    combined['adjust_type'] = 'none'  # 申万指数是价格指数，不复权
    
    # 检查数据库是否已有板块数据，有则删除
    conn = sqlite3.connect(db.db_path)
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM market_data WHERE ticker LIKE 'SECTOR_%'")
        existing = cursor.fetchone()[0]
        if existing > 0:
            print(f"  数据库已有 {existing} 行板块数据，将删除后重新插入...")
            conn.execute("DELETE FROM market_data WHERE ticker LIKE 'SECTOR_%'")
            conn.commit()
    except Exception as e:
        print(f"  警告：检查/删除已有板块数据时出错: {e}")
    
    # 插入数据
    try:
        combined.to_sql('market_data', conn, if_exists='append', index=False)
        print(f"  成功插入 {len(combined)} 行数据")
    except Exception as e:
        print(f"  错误：插入失败: {e}")
    finally:
        conn.close()
    
    if failed:
        print(f"\n失败列表：")
        for code, name, reason in failed:
            print(f"  {code} ({name}): {reason}")
    
    return combined


def verify_data(sector_df):
    """验证数据完整性"""
    print("\n" + "="*80)
    print("数据验证报告")
    print("="*80)
    
    # 1. 每个板块的数据情况
    print("\n1. 板块数据完整性：")
    print(f"{'板块代码':<12} {'板块名称':<12} {'起始日期':<12} {'结束日期':<12} {'数据天数':>8} {'缺失说明':<20}")
    print("-"*80)
    
    for code in SECTOR_CODES:
        ticker = f"SECTOR_{code}"
        name = SECTOR_INDEX_UNIVERSE.get(f"{code}.SI", ('', []))[0]
        df = sector_df[sector_df['ticker'] == ticker]
        
        if df.empty:
            print(f"{code:<12} {name:<12} {'N/A':<12} {'N/A':<12} {'0':>8} {'数据缺失':<20}")
            continue
        
        start = df['date'].min().strftime('%Y-%m-%d')
        end = df['date'].max().strftime('%Y-%m-%d')
        days = len(df)
        
        # 计算缺失天数
        all_dates = pd.date_range(start=start, end=end, freq='B')  # 工作日
        missing = len(all_dates) - days
        missing_pct = missing / len(all_dates) * 100 if len(all_dates) > 0 else 0
        
        note = f"缺失{missing}天({missing_pct:.1f}%)" if missing > 0 else "数据完整"
        
        print(f"{code:<12} {name:<12} {start:<12} {end:<12} {days:>8} {note:<20}")
    
    # 2. 数据口径说明
    print("\n2. 数据口径：")
    print("  - 申万一级行业指数是【价格指数】，不包含分红，不复权")
    print("  - 适合用于动量/趋势计算，不适合用于收益对比")
    print("  - adjust_type='none'（不复权）")
    print("  - 成交量单位为：亿股")
    print("  - 成交额单位为：亿元")
    
    # 3. 映射关系审视
    print("\n3. 板块→ETF映射关系（含非一一对应标注）：")
    print(f"{'板块代码':<12} {'板块名称':<10} {'对应ETF':<30} {'映射类型':<10} {'备注':<30}")
    print("-"*100)
    
    for sector_code, (name, etfs) in SECTOR_INDEX_UNIVERSE.items():
        code = sector_code.split('.')[0]
        
        # 映射类型判断
        if len(etfs) == 1:
            map_type = "一对一"
            note = ""
        elif len(etfs) > 1:
            map_type = "一对多"
            note = f"共{len(etfs)}只ETF"
        else:
            map_type = "无映射"
            note = ""
        
        # 检查是否有ETF映射到多个板块
        multi_sector_etfs = []
        for etf in etfs:
            sectors = ETF_TO_SECTOR_MAPPING.get(etf, [])
            if len(sectors) > 1:
                multi_sector_etfs.append(etf)
        
        if multi_sector_etfs:
            note += f"[ETF跨板块: {','.join(multi_sector_etfs)}]"
        
        etf_names = [ETF_UNIVERSE.get(e, CONCEPT_UNIVERSE.get(e, e)) for e in etfs]
        etf_display = ', '.join(etf_names[:2]) + ('...' if len(etf_names) > 2 else '')
        
        print(f"{code:<12} {name:<10} {etf_display:<30} {map_type:<10} {note:<30}")
    
    # 4. 未映射的ETF
    print("\n4. 未映射到板块指数的ETF（多行业/跨境）：")
    all_etfs = list(CORE_UNIVERSE.keys())
    mapped_etfs = set()
    for etfs in ETF_TO_SECTOR_MAPPING.values():
        mapped_etfs.update(etfs)
    
    unmapped = [e for e in all_etfs if e not in mapped_etfs]
    for etf in unmapped:
        name = CORE_UNIVERSE.get(etf, etf)
        print(f"  {etf} ({name}) - 未映射")
    
    print(f"\n  共 {len(unmapped)} 只ETF未映射，这些将仅使用ETF自身信号")


def generate_correlation_report(sector_df):
    """生成板块-ETF相关性验证报告"""
    print("\n" + "="*80)
    print("板块-ETF相关性验证报告")
    print("="*80)
    
    from database import ETFDatabase
    db = ETFDatabase()
    
    print("\n方法：计算板块指数与对应ETF的日收益率相关性")
    print("（使用重叠时间段，排除无数据日期）")
    
    results = []
    
    for sector_code, (name, etfs) in SECTOR_INDEX_UNIVERSE.items():
        sector_ticker = f"SECTOR_{sector_code.split('.')[0]}"
        sector_df_single = sector_df[sector_df['ticker'] == sector_ticker].copy()
        
        if sector_df_single.empty:
            continue
        
        sector_df_single = sector_df_single.sort_values('date')
        sector_df_single['return'] = sector_df_single['close'].pct_change()
        
        for etf in etfs:
            # 获取ETF数据
            etf_df = db.get_market_data(ticker=etf)
            if etf_df.empty or len(etf_df) < 50:
                continue
            
            etf_df = etf_df.sort_values('date')
            etf_df['return'] = etf_df['close'].pct_change()
            
            # 合并计算相关性
            merged = pd.merge(
                sector_df_single[['date', 'return']],
                etf_df[['date', 'return']],
                on='date', suffixes=('_sector', '_etf')
            )
            
            if len(merged) < 30:
                continue
            
            # 移除NaN
            merged = merged.dropna()
            if len(merged) < 30:
                continue
            
            # 计算相关性
            corr = merged['return_sector'].corr(merged['return_etf'])
            
            # 方向一致性
            same_direction = (merged['return_sector'] * merged['return_etf'] > 0).mean()
            
            # 板块领先性（板块T日 vs ETF T+1日）
            merged['sector_lead'] = merged['return_sector'].shift(1)
            lead_corr = merged['sector_lead'].corr(merged['return_etf'])
            
            etf_name = CORE_UNIVERSE.get(etf, etf)
            
            results.append({
                'sector': sector_code.split('.')[0],
                'sector_name': name,
                'etf': etf,
                'etf_name': etf_name,
                'corr': corr,
                'same_direction': same_direction,
                'lead_corr': lead_corr,
                'overlap_days': len(merged),
            })
    
    results_df = pd.DataFrame(results)
    if results_df.empty:
        print("\n警告：无法计算相关性，数据不足")
        return
    
    # 打印结果
    print(f"\n{'板块':<8} {'板块名':<8} {'ETF':<12} {'ETF名':<12} {'同期相关':>8} {'方向一致':>8} {'领先相关':>8} {'重叠天数':>8}")
    print("-"*100)
    
    for _, row in results_df.iterrows():
        print(f"{row['sector']:<8} {row['sector_name']:<8} {row['etf']:<12} {row['etf_name']:<12} {row['corr']:>8.3f} {row['same_direction']:>7.1%} {row['lead_corr']:>8.3f} {row['overlap_days']:>8}")
    
    # 统计摘要
    print(f"\n{'='*80}")
    print("统计摘要")
    print(f"{'='*80}")
    print(f"  平均同期相关性: {results_df['corr'].mean():.3f}")
    print(f"  中位数同期相关性: {results_df['corr'].median():.3f}")
    print(f"  平均方向一致性: {results_df['same_direction'].mean():.1%}")
    print(f"  平均领先相关性: {results_df['lead_corr'].mean():.3f}")
    print(f"  领先>同期的占比: {(results_df['lead_corr'] > results_df['corr']).mean():.1%}")
    
    # 高相关性配对
    high_corr = results_df[results_df['corr'] > 0.7]
    print(f"\n  高相关性(>0.7)配对: {len(high_corr)}/{len(results_df)}")
    
    # 领先性强的配对
    strong_lead = results_df[results_df['lead_corr'] > results_df['corr']]
    print(f"  板块领先性强(领先>同期)配对: {len(strong_lead)}/{len(results_df)}")
    
    return results_df


if __name__ == '__main__':
    
    # 1. 下载数据
    sector_df = download_sectors()
    
    if sector_df is not None:
        # 2. 验证数据
        verify_data(sector_df)
        
        # 3. 生成相关性报告
        corr_results = generate_correlation_report(sector_df)
        
        # 4. 保存报告
        report = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'sector_count': len(SECTOR_CODES),
            'total_rows': len(sector_df),
            'date_range': {
                'start': sector_df['date'].min().strftime('%Y-%m-%d'),
                'end': sector_df['date'].max().strftime('%Y-%m-%d'),
            },
            'mapping': {k: v[1] for k, v in SECTOR_INDEX_UNIVERSE.items()},
        }
        
        if corr_results is not None and not corr_results.empty:
            report['correlation_stats'] = {
                'avg_corr': float(corr_results['corr'].mean()),
                'median_corr': float(corr_results['corr'].median()),
                'avg_direction': float(corr_results['same_direction'].mean()),
                'avg_lead_corr': float(corr_results['lead_corr'].mean()),
            }
            report['correlation_details'] = corr_results.to_dict('records')
        
        with open('reports/sector_data_report.json', 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n报告已保存: reports/sector_data_report.json")
        print("\n[OK] 板块数据接入完成！")
