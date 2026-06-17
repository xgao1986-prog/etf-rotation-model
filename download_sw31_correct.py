# -*- coding: utf-8 -*-
"""
download_sw31_final_correct.py - 下载正确的31个申万一级行业指数数据

申万行业历史：
- 2003版：28个一级行业（旧版代码801010-801230）
- 2014版：新增11个一级行业（新版代码801710-801890），部分旧版代码停用
- 2021版：再次调整，新增4个一级行业（代码801950-801980），从采掘/公用事业拆分

本脚本下载2021版31个行业数据，其中27个覆盖2019-2026，4个从2021-12-13开始。
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

# 31个申万一级行业（2021版）
# 分类说明：
# - 2014前旧版保留：数据完整（1999/2003-2026）
# - 2014新版：数据完整（2014-2026）
# - 2021新版：数据从2021-12-13开始（从旧版拆分）
SW31_SECTORS = {
    # === 2014年前旧版保留（16个）===
    '801010.SI': ('农林牧渔', []),      # 旧版，数据完整
    '801030.SI': ('化工', []),           # 旧版，数据完整
    '801040.SI': ('钢铁', []),           # 旧版，数据完整
    '801050.SI': ('有色金属', []),       # 旧版，数据完整
    '801080.SI': ('电子', ['512480.SH', '588200.SH']),  # 旧版，数据完整
    '801110.SI': ('家用电器', []),       # 旧版，数据完整
    '801120.SI': ('食品饮料', ['512690.SH', '515170.SH']),  # 旧版，数据完整
    '801130.SI': ('纺织服装', []),       # 旧版，数据完整
    '801140.SI': ('轻工制造', []),       # 旧版，数据完整
    '801150.SI': ('医药生物', ['512010.SH', '159992.SZ', '159898.SZ']),  # 旧版，数据完整
    '801170.SI': ('交通运输', []),       # 旧版，数据完整
    '801180.SI': ('房地产', []),         # 旧版，数据完整
    '801200.SI': ('商业贸易', []),       # 旧版，数据完整
    '801210.SI': ('休闲服务', ['159766.SZ']),  # 旧版，数据完整（后改名为社会服务）
    '801230.SI': ('综合', []),           # 旧版，数据完整
    
    # === 2014年新版（11个）===
    '801710.SI': ('建筑材料', []),       # 新版（2014-2-21开始），从旧版拆分
    '801720.SI': ('建筑装饰', []),       # 新版（2014-2-21开始），从旧版拆分
    '801730.SI': ('电力设备', ['516160.SH', '515790.SH', '159566.SZ']),  # 新版（2014-2-21开始），原名电气设备
    '801740.SI': ('国防军工', ['512660.SH']),  # 新版（2014-2-21开始）
    '801750.SI': ('计算机', ['515230.SH', '516510.SH']),  # 新版（2014-2-21开始）
    '801760.SI': ('传媒', ['512980.SH', '159869.SZ']),  # 新版（2014-2-21开始）
    '801770.SI': ('通信', ['515880.SH', '515050.SH']),  # 新版（2014-2-21开始）
    '801780.SI': ('银行', ['512800.SH']),  # 新版（2014-2-21开始）
    '801790.SI': ('非银金融', ['512000.SH']),  # 新版（2014-2-21开始）
    '801880.SI': ('汽车', ['516110.SH']),  # 新版（2014-2-21开始）
    '801890.SI': ('机械设备', ['159530.SZ', '562500.SH']),  # 新版（2014-2-21开始）
    
    # === 2021年新版（4个，从旧版拆分）===
    # 注意：这些代码数据从2021-12-13开始，2019-2021-12-13期间无数据
    # 它们从旧版801020（采掘）和801160（公用事业）拆分而来
    '801950.SI': ('煤炭', []),           # 新版（2021-12-13开始），从采掘拆分
    '801960.SI': ('石油石化', ['159697.SZ']),  # 新版（2021-12-13开始），从采掘拆分
    '801970.SI': ('环保', []),           # 新版（2021-12-13开始），从公用事业拆分
    '801980.SI': ('电力', []),           # 新版（2021-12-13开始），从公用事业拆分
}

SW31_CODES = [code.split('.')[0] for code in SW31_SECTORS.keys()]


def download_sw31_sectors():
    """下载31个申万一级行业数据"""
    print("="*80)
    print("下载31个申万一级行业指数数据（2021版）")
    print("="*80)
    print(f"\n行业总数: 31 个")
    print(f"  - 2014前旧版保留: 16 个")
    print(f"  - 2014年新版: 11 个")
    print(f"  - 2021年新版（从旧版拆分）: 4 个")
    print(f"\n注意：2021年新版4个行业数据从2021-12-13开始，")
    print(f"      2019-2021-12-13期间无数据\n")
    
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
    for code in SW31_CODES:
        name = SW31_SECTORS.get(f"{code}.SI", ('', []))[0]
        category = "2021新版" if code in ['801950', '801960', '801970', '801980'] else \
                   "2014新版" if code >= '801710' else "旧版保留"
        print(f"  {code} - {name} ({category})")
    
    print("\n开始下载...")
    
    all_data = []
    failed = []
    
    for code in SW31_CODES:
        name = SW31_SECTORS.get(f"{code}.SI", ('', []))[0]
        print(f"\n[{code}] {name} ...", end='')
        
        try:
            df = fetcher.fetch_sector_history(code, '2019-01-01', max_date)
            if not df.empty:
                rows = len(df)
                start = df['date'].min().date()
                end = df['date'].max().date()
                print(f" OK ({rows} rows, {start} ~ {end})")
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
    
    # 统计
    early_codes = []
    late_codes = []
    for code in combined['ticker'].unique():
        code_short = code.replace('SECTOR_', '')
        df_code = combined[combined['ticker'] == code]
        start = df_code['date'].min()
        if start > pd.Timestamp('2021-12-20'):
            late_codes.append((code_short, start.date()))
        elif start <= pd.Timestamp('2019-01-05'):
            early_codes.append((code_short, start.date()))
    
    print(f"\n{'='*80}")
    print(f"下载完成：共 {len(combined)} 行数据，{combined['ticker'].nunique()} 个板块")
    print(f"日期范围：{combined['date'].min()} ~ {combined['date'].max()}")
    print(f"\n数据覆盖情况：")
    print(f"  2019年开始: {len(early_codes)} 个")
    for code, start in early_codes:
        print(f"    {code} ({SW31_SECTORS.get(code+'.SI', ('',[]))[0]}): {start}")
    print(f"  2021-12-13开始: {len(late_codes)} 个")
    for code, start in late_codes:
        print(f"    {code} ({SW31_SECTORS.get(code+'.SI', ('',[]))[0]}): {start}")
    
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


def verify_etf_mapping():
    """验证板块-ETF映射关系"""
    print("\n" + "="*80)
    print("验证板块-ETF映射关系")
    print("="*80)
    
    from database import ETFDatabase
    db = ETFDatabase()
    
    # 有ETF映射的板块
    mapped_sectors = {k: v for k, v in SW31_SECTORS.items() if v[1]}
    
    print(f"\n有ETF映射的板块: {len(mapped_sectors)} 个")
    print(f"无ETF映射的板块: {len(SW31_SECTORS) - len(mapped_sectors)} 个")
    
    print(f"\n{'板块代码':<10} {'板块名称':<10} {'映射ETF':<30} {'重叠天数':>8}")
    print("-"*80)
    
    for sector_code, (name, etfs) in mapped_sectors.items():
        sector_ticker = f"SECTOR_{sector_code.split('.')[0]}"
        sector_df = db.get_market_data(ticker=sector_ticker)
        
        for etf in etfs:
            etf_df = db.get_market_data(ticker=etf)
            if not sector_df.empty and not etf_df.empty:
                merged = pd.merge(sector_df[['date']], etf_df[['date']], on='date')
                overlap = len(merged)
                print(f"{sector_code.split('.')[0]:<10} {name:<10} {etf:<30} {overlap:>8}")
    
    print(f"\n无映射的板块（仅观察用）:")
    unmapped = {k: v for k, v in SW31_SECTORS.items() if not v[1]}
    for sector_code, (name, _) in unmapped.items():
        print(f"  {sector_code.split('.')[0]} {name}")


if __name__ == '__main__':
    # 1. 下载所有31个行业数据
    sector_df = download_sw31_sectors()
    
    if sector_df is not None:
        # 2. 验证映射
        verify_etf_mapping()
        
        # 3. 保存报告
        report = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_sector_count': 31,
            'early_start_count': 27,
            'late_start_count': 4,
            'total_rows': len(sector_df),
            'date_range': {
                'start': sector_df['date'].min().strftime('%Y-%m-%d'),
                'end': sector_df['date'].max().strftime('%Y-%m-%d'),
            },
            'sectors': {k: {'name': v[0], 'etfs': v[1]} for k, v in SW31_SECTORS.items()},
        }
        
        with open('reports/sw31_sector_data_report.json', 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n报告已保存: reports/sw31_sector_data_report.json")
        print(f"\n[OK] 31个申万一级行业数据下载完成！")
        print(f"  - 27个行业数据完整（2019-2026）")
        print(f"  - 4个行业数据从2021-12-13开始（煤炭/石油石化/环保/电力）")
