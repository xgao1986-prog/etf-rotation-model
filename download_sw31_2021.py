# -*- coding: utf-8 -*-
"""
download_sw31_final.py - 下载完整的31个申万一级行业指数数据

申万2021版31个一级行业完整列表：
1.  801010 农林牧渔
2.  801030 基础化工 (原化工)
3.  801040 钢铁
4.  801050 有色金属
5.  801080 电子
6.  801110 家用电器
7.  801120 食品饮料
8.  801130 纺织服饰 (原纺织服装)
9.  801140 轻工制造
10. 801150 医药生物
11. 801160 公用事业
12. 801170 交通运输
13. 801180 房地产
14. 801200 商贸零售 (原商业贸易)
15. 801210 社会服务 (原休闲服务)
16. 801230 综合
17. 801710 建筑材料
18. 801720 建筑装饰
19. 801730 电力设备 (原电气设备)
20. 801740 国防军工
21. 801750 计算机
22. 801760 传媒
23. 801770 通信
24. 801780 银行
25. 801790 非银金融
26. 801880 汽车
27. 801890 机械设备
28. 801950 煤炭 (从采掘拆分)
29. 801960 石油石化 (从采掘拆分)
30. 801970 环保 (从公用事业拆分)
31. 801980 美容护理 (新增)
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import sqlite3
from datetime import datetime
import time

from data_fetcher import DataFetcher

# 31个申万2021版一级行业（标准名称）
SW31_2021 = {
    '801010.SI': ('农林牧渔', ['159865.SZ']),
    '801030.SI': ('基础化工', []),  # 原名化工
    '801040.SI': ('钢铁', []),
    '801050.SI': ('有色金属', ['512400.SH']),
    '801080.SI': ('电子', ['512480.SH', '588200.SH']),
    '801110.SI': ('家用电器', ['159996.SZ']),
    '801120.SI': ('食品饮料', ['512690.SH', '515170.SH']),
    '801130.SI': ('纺织服饰', []),  # 原名纺织服装
    '801140.SI': ('轻工制造', []),
    '801150.SI': ('医药生物', ['512010.SH', '159992.SZ', '159898.SZ']),
    '801160.SI': ('公用事业', []),
    '801170.SI': ('交通运输', []),
    '801180.SI': ('房地产', []),
    '801200.SI': ('商贸零售', []),  # 原名商业贸易
    '801210.SI': ('社会服务', ['159766.SZ']),  # 原名休闲服务
    '801230.SI': ('综合', []),
    '801710.SI': ('建筑材料', []),
    '801720.SI': ('建筑装饰', []),
    '801730.SI': ('电力设备', ['516160.SH', '515790.SH', '159566.SZ']),  # 原名电气设备
    '801740.SI': ('国防军工', ['512660.SH']),
    '801750.SI': ('计算机', ['515230.SH', '516510.SH']),
    '801760.SI': ('传媒', ['512980.SH', '159869.SZ']),
    '801770.SI': ('通信', ['515880.SH', '515050.SH']),
    '801780.SI': ('银行', ['512800.SH']),
    '801790.SI': ('非银金融', ['512000.SH']),
    '801880.SI': ('汽车', ['516110.SH']),
    '801890.SI': ('机械设备', ['159530.SZ', '562500.SH']),
    '801950.SI': ('煤炭', []),  # 从采掘拆分
    '801960.SI': ('石油石化', ['159697.SZ']),  # 从采掘拆分
    '801970.SI': ('环保', []),  # 从公用事业拆分
    '801980.SI': ('美容护理', []),  # 2021新增
}

SW31_CODES = [code.split('.')[0] for code in SW31_2021.keys()]


def download_all_31_sectors():
    """下载31个申万行业数据"""
    print("="*80)
    print("下载31个申万2021版一级行业指数数据")
    print("="*80)
    
    fetcher = DataFetcher()
    
    conn = sqlite3.connect('database/etf_model.db')
    cursor = conn.execute('SELECT MAX(date) FROM market_data')
    max_date = cursor.fetchone()[0]
    conn.close()
    
    print(f"数据库最新数据: {max_date}")
    print(f"下载区间: 2019-01-01 ~ {max_date}\n")
    
    all_data = []
    failed = []
    
    for code in SW31_CODES:
        name = SW31_2021.get(f"{code}.SI", ('', []))[0]
        print(f"[{code}] {name} ...", end='')
        
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
        
        time.sleep(0.3)
    
    if not all_data:
        print("\n错误：没有获取到任何数据")
        return None
    
    combined = pd.concat(all_data, ignore_index=True)
    
    print(f"\n{'='*80}")
    print(f"下载完成：{len(combined)} 行，{combined['ticker'].nunique()} 个板块")
    
    # 清理旧数据并入库
    print(f"\n清理旧板块数据并入库...")
    conn = sqlite3.connect('database/etf_model.db')
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM market_data WHERE ticker LIKE 'SECTOR_%'")
        old_count = cursor.fetchone()[0]
        print(f"  删除旧数据: {old_count} 行")
        conn.execute("DELETE FROM market_data WHERE ticker LIKE 'SECTOR_%'")
        conn.commit()
        
        combined['adjust_type'] = 'none'
        combined.to_sql('market_data', conn, if_exists='append', index=False)
        print(f"  插入新数据: {len(combined)} 行")
        
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
        print(f"\n失败: {len(failed)} 个")
        for code, name, reason in failed:
            print(f"  {code} ({name}): {reason}")
    
    return combined


if __name__ == '__main__':
    download_all_31_sectors()
    print("\n[OK] 31个申万行业数据下载完成")
