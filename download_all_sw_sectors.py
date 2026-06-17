# -*- coding: utf-8 -*-
"""
download_all_sw_sectors.py - 下载全部31个标准申万一级行业指数数据

修正说明：
- 当前配置中的18个代码包含10个非标准代码（如801740对应国防军工，应为801270）
- 需要修正为标准31个申万一级行业代码（2021版）
- 下载所有31个标准代码的数据，替换旧数据

标准31个申万一级行业代码（2021版）：
801010 农林牧渔     801020 基础化工     801030 钢铁         801040 有色金属
801050 建筑材料     801060 建筑装饰     801070 机械设备     801080 电子
801090 汽车         801100 家用电器     801110 食品饮料     801120 纺织服饰
801130 轻工制造     801140 医药生物     801150 商贸零售     801160 社会服务
801170 非银金融     801180 银行         801190 房地产       801200 交通运输
801210 煤炭         801220 石油石化     801230 环保         801240 公用事业
801250 综合         801260 美容护理     801270 国防军工     801280 通信
801290 计算机       801300 传媒         801730 电力设备
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
from config import CORE_UNIVERSE


# 标准31个申万一级行业代码（2021版）
SW31_SECTORS = {
    '801010.SI': ('农林牧渔', []),
    '801020.SI': ('基础化工', []),
    '801030.SI': ('钢铁', []),
    '801040.SI': ('有色金属', []),
    '801050.SI': ('建筑材料', []),
    '801060.SI': ('建筑装饰', []),
    '801070.SI': ('机械设备', []),
    '801080.SI': ('电子', ['512480.SH', '588200.SH']),
    '801090.SI': ('汽车', ['516110.SH']),
    '801100.SI': ('家用电器', ['159996.SZ']),
    '801110.SI': ('食品饮料', ['512690.SH', '515170.SH']),
    '801120.SI': ('纺织服饰', []),
    '801130.SI': ('轻工制造', []),
    '801140.SI': ('医药生物', ['512010.SH', '159992.SZ', '159898.SZ']),
    '801150.SI': ('商贸零售', []),
    '801160.SI': ('社会服务', ['159766.SZ']),
    '801170.SI': ('非银金融', ['512000.SH']),
    '801180.SI': ('银行', ['512800.SH']),
    '801190.SI': ('房地产', []),
    '801200.SI': ('交通运输', []),
    '801210.SI': ('煤炭', []),
    '801220.SI': ('石油石化', ['159697.SZ']),
    '801230.SI': ('环保', []),
    '801240.SI': ('公用事业', []),
    '801250.SI': ('综合', []),
    '801260.SI': ('美容护理', []),
    '801270.SI': ('国防军工', ['512660.SH']),
    '801280.SI': ('通信', ['515880.SH', '515050.SH']),
    '801290.SI': ('计算机', ['515230.SH', '516510.SH']),
    '801300.SI': ('传媒', ['512980.SH', '159869.SZ']),
    '801730.SI': ('电力设备', ['516160.SH', '515790.SH', '159566.SZ']),
}

SW31_CODES = [code.split('.')[0] for code in SW31_SECTORS.keys()]


def download_all_sectors():
    """下载所有31个标准申万行业指数数据"""
    print("="*80)
    print("下载标准31个申万一级行业指数数据")
    print("="*80)
    
    fetcher = DataFetcher()
    
    # 获取数据截止日期
    conn = sqlite3.connect('database/etf_model.db')
    cursor = conn.execute('SELECT MAX(date) FROM market_data')
    max_date = cursor.fetchone()[0]
    conn.close()
    print(f"\n数据库最新数据日期: {max_date}")
    print(f"下载起始日期: 2019-01-01")
    print(f"下载截止日期: {max_date}")
    print(f"共 {len(SW31_CODES)} 个标准申万一级行业指数:\n")
    
    for code in SW31_CODES:
        name = SW31_SECTORS.get(f"{code}.SI", ('', []))[0]
        print(f"  {code} - {name}")
    
    print("\n开始下载...")
    
    all_data = []
    failed = []
    
    for code in SW31_CODES:
        name = SW31_SECTORS.get(f"{code}.SI", ('', []))[0]
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
    print(f"\n正在清理旧数据并入库...")
    
    conn = sqlite3.connect('database/etf_model.db')
    try:
        # 删除所有旧板块数据（包括非标准代码的）
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


def verify_mapping():
    """验证新的板块-ETF映射关系"""
    print("\n" + "="*80)
    print("验证新的板块-ETF映射关系")
    print("="*80)
    
    from database import ETFDatabase
    db = ETFDatabase()
    
    # 有ETF映射的板块
    mapped_sectors = {k: v for k, v in SW31_SECTORS.items() if v[1]}
    
    print(f"\n有ETF映射的板块: {len(mapped_sectors)} 个")
    print(f"无ETF映射的板块: {len(SW31_SECTORS) - len(mapped_sectors)} 个")
    
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
    unmapped = {k: v for k, v in SW31_SECTORS.items() if not v[1]}
    for sector_code, (name, _) in unmapped.items():
        print(f"  {sector_code.split('.')[0]} {name}")


if __name__ == '__main__':
    # 1. 下载所有标准31个行业数据
    sector_df = download_all_sectors()
    
    if sector_df is not None:
        # 2. 验证映射
        verify_mapping()
        
        # 3. 保存报告
        report = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'sector_count': len(SW31_CODES),
            'total_rows': len(sector_df),
            'date_range': {
                'start': sector_df['date'].min().strftime('%Y-%m-%d'),
                'end': sector_df['date'].max().strftime('%Y-%m-%d'),
            },
            'mapping': {k: v[1] for k, v in SW31_SECTORS.items()},
            'standard_codes': SW31_CODES,
        }
        
        with open('reports/sw31_sector_data_report.json', 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n报告已保存: reports/sw31_sector_data_report.json")
        print("\n[OK] 标准31个申万一级行业数据下载完成！")
