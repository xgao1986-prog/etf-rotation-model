# -*- coding: utf-8 -*-
"""
download_sw31_final.py - 下载最终26个可用的申万行业指数数据

说明：
- 标准31个代码中，部分旧版代码数据只到2014年（如801090汽车）
- 这些行业需要使用新版代码替代（如801880替代801090）
- 最终可用26个行业，缺失5个（建筑装饰、家用电器、房地产、公用事业、基础化工）
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

# 最终26个可用行业代码（标准 + 新版替代）
FINAL_SECTORS = {
    # 标准代码（22个）
    '801010.SI': ('农林牧渔', []),
    '801030.SI': ('钢铁', []),
    '801040.SI': ('有色金属', []),
    '801050.SI': ('建筑材料', []),
    '801080.SI': ('电子', ['512480.SH', '588200.SH']),
    '801110.SI': ('食品饮料', ['512690.SH', '515170.SH']),
    '801120.SI': ('纺织服饰', []),
    '801130.SI': ('轻工制造', []),
    '801140.SI': ('医药生物', ['512010.SH', '159992.SZ', '159898.SZ']),
    '801150.SI': ('商贸零售', []),
    '801160.SI': ('社会服务', ['159766.SZ']),
    '801170.SI': ('非银金融', ['512000.SH']),
    '801180.SI': ('银行', ['512800.SH']),
    '801200.SI': ('交通运输', []),
    '801210.SI': ('煤炭', []),
    '801230.SI': ('环保', []),
    '801250.SI': ('综合', []),
    '801260.SI': ('美容护理', []),
    '801270.SI': ('国防军工', ['512660.SH']),
    '801280.SI': ('通信', ['515880.SH', '515050.SH']),
    '801300.SI': ('传媒', ['512980.SH', '159869.SZ']),
    '801730.SI': ('电力设备', ['516160.SH', '515790.SH', '159566.SZ']),
    
    # 新版替代代码（4个）
    '801880.SI': ('汽车', ['516110.SH']),  # 替代801090（旧版数据只到2014年）
    '801890.SI': ('机械设备', ['159530.SZ', '562500.SH']),  # 替代801070（旧版数据只到2014年）
    '801960.SI': ('石油石化', ['159697.SZ']),  # 替代801220（旧版数据只到2014年）
    '801750.SI': ('计算机', ['515230.SH', '516510.SH']),  # 替代801290（旧版数据到2024年）
}

# 缺失的5个（需要找新版代码）
MISSING_SECTORS = {
    '801060.SI': ('建筑装饰', '旧版数据只到2014年，需找新版代码'),
    '801100.SI': ('家用电器', '旧版数据只到2014年，需找新版代码'),
    '801190.SI': ('房地产', '旧版数据只到2014年，需找新版代码'),
    '801240.SI': ('公用事业', '数据格式错误，需找新版代码'),
    '801020.SI': ('基础化工', '旧版数据到2021年，需找新版代码'),
}

FINAL_CODES = [code.split('.')[0] for code in FINAL_SECTORS.keys()]


def download_final_sectors():
    """下载最终26个可用行业数据"""
    print("="*80)
    print("下载最终26个可用申万行业指数数据")
    print("="*80)
    print(f"\n可用行业: {len(FINAL_CODES)} 个")
    print(f"缺失行业: {len(MISSING_SECTORS)} 个")
    print(f"目标: 补齐26个可用行业数据\n")
    
    fetcher = DataFetcher()
    
    # 获取数据截止日期
    conn = sqlite3.connect('database/etf_model.db')
    cursor = conn.execute('SELECT MAX(date) FROM market_data')
    max_date = cursor.fetchone()[0]
    conn.close()
    print(f"数据库最新数据日期: {max_date}")
    print(f"下载起始日期: 2019-01-01")
    print(f"下载截止日期: {max_date}\n")
    
    print("行业列表:")
    for code in FINAL_CODES:
        name = FINAL_SECTORS.get(f"{code}.SI", ('', []))[0]
        print(f"  {code} - {name}")
    
    print(f"\n缺失行业（需后续补充）:")
    for code, (name, reason) in MISSING_SECTORS.items():
        print(f"  {code.split('.')[0]} - {name} ({reason})")
    
    print("\n开始下载...")
    
    all_data = []
    failed = []
    
    for code in FINAL_CODES:
        name = FINAL_SECTORS.get(f"{code}.SI", ('', []))[0]
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
    
    # 清理旧数据并入库
    print(f"\n正在清理旧板块数据并入库...")
    
    conn = sqlite3.connect('database/etf_model.db')
    try:
        # 删除所有旧板块数据
        cursor = conn.execute("SELECT COUNT(*) FROM market_data WHERE ticker LIKE 'SECTOR_%'")
        old_count = cursor.fetchone()[0]
        print(f"  删除旧板块数据: {old_count} 行")
        conn.execute("DELETE FROM market_data WHERE ticker LIKE 'SECTOR_%'")
        conn.commit()
        
        # 插入新数据
        combined['adjust_type'] = 'none'
        combined.to_sql('market_data', conn, if_exists='append', index=False)
        print(f"  成功插入 {len(combined)} 行新数据")
        
        # 验证
        cursor = conn.execute("SELECT COUNT(*) FROM market_data WHERE ticker LIKE 'SECTOR_%'")
        new_count = cursor.fetchone()[0]
        cursor = conn.execute("SELECT COUNT(DISTINCT ticker) FROM market_data WHERE ticker LIKE 'SECTOR_%'")
        sector_count = cursor.fetchone()[0]
        print(f"  验证: {new_count} 行, {sector_count} 个板块")
        
    except Exception as e:
        print(f"  错误: {e}")
    finally:
        conn.close()
    
    if failed:
        print(f"\n失败列表：")
        for code, name, reason in failed:
            print(f"  {code} ({name}): {reason}")
    
    return combined


def verify_new_mapping():
    """验证新的板块-ETF映射关系"""
    print("\n" + "="*80)
    print("验证新的板块-ETF映射关系")
    print("="*80)
    
    from database import ETFDatabase
    db = ETFDatabase()
    
    # 有ETF映射的板块
    mapped_sectors = {k: v for k, v in FINAL_SECTORS.items() if v[1]}
    
    print(f"\n有ETF映射的板块: {len(mapped_sectors)} 个")
    print(f"无ETF映射的板块: {len(FINAL_SECTORS) - len(mapped_sectors)} 个")
    
    print(f"\n{'板块代码':<10} {'板块名称':<8} {'映射ETF':<30} {'重叠天数':>8}")
    print("-"*80)
    
    for sector_code, (name, etfs) in mapped_sectors.items():
        sector_ticker = f"SECTOR_{sector_code.split('.')[0]}"
        sector_df = db.get_market_data(ticker=sector_ticker)
        
        for etf in etfs:
            etf_df = db.get_market_data(ticker=etf)
            if not sector_df.empty and not etf_df.empty:
                merged = pd.merge(sector_df[['date']], etf_df[['date']], on='date')
                overlap = len(merged)
                print(f"{sector_code.split('.')[0]:<10} {name:<8} {etf:<30} {overlap:>8}")
    
    print(f"\n无映射的板块（仅观察用）:")
    unmapped = {k: v for k, v in FINAL_SECTORS.items() if not v[1]}
    for sector_code, (name, _) in unmapped.items():
        print(f"  {sector_code.split('.')[0]} {name}")
    
    print(f"\n缺失的板块（需后续找新版代码）:")
    for sector_code, (name, reason) in MISSING_SECTORS.items():
        print(f"  {sector_code.split('.')[0]} {name} - {reason}")


if __name__ == '__main__':
    # 1. 下载所有可用行业数据
    sector_df = download_final_sectors()
    
    if sector_df is not None:
        # 2. 验证映射
        verify_new_mapping()
        
        # 3. 保存报告
        report = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'available_sector_count': len(FINAL_CODES),
            'missing_sector_count': len(MISSING_SECTORS),
            'total_rows': len(sector_df),
            'date_range': {
                'start': sector_df['date'].min().strftime('%Y-%m-%d'),
                'end': sector_df['date'].max().strftime('%Y-%m-%d'),
            },
            'available_sectors': {k: v[1] for k, v in FINAL_SECTORS.items()},
            'missing_sectors': {k: v[0] for k, v in MISSING_SECTORS.items()},
        }
        
        with open('reports/sw26_sector_data_report.json', 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n报告已保存: reports/sw26_sector_data_report.json")
        print(f"\n[OK] 最终26个可用行业数据下载完成！")
        print(f"缺失的5个行业需后续找新版代码补充")
