# -*- coding: utf-8 -*-
"""
fetch_telecom_stocks.py - 下载通信行业所有成分股数据（带延迟和重试）
"""
import pandas as pd
import requests
import time
import json
from datetime import datetime

START_DATE = '2022-01-01'
END_DATE = '2026-06-12'

def get_constituents():
    import akshare as ak
    df = ak.index_stock_cons(symbol='801770')
    return df['品种代码'].tolist()

def get_market_type(code):
    if code.startswith('6') or code.startswith('8'):
        return '1'
    else:
        return '0'

def fetch_stock(code, start_date, end_date, retries=3):
    market = get_market_type(code)
    url = f'http://push2his.eastmoney.com/api/qt/stock/kline/get'
    params = {
        'secid': f'{market}.{code}',
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'klt': '101', 'fqt': '0',
        'beg': start_date.replace('-', ''),
        'end': end_date.replace('-', ''),
    }
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200 and resp.text:
                data = resp.json()
                if data.get('data') and data['data'].get('klines'):
                    klines = data['data']['klines']
                    rows = []
                    for line in klines:
                        parts = line.split(',')
                        if len(parts) >= 6:
                            rows.append({
                                'date': parts[0], 'open': float(parts[1]),
                                'close': float(parts[2]), 'high': float(parts[3]),
                                'low': float(parts[4]), 'volume': int(parts[5]),
                                'amount': float(parts[6]) if len(parts) > 6 else 0,
                            })
                    df = pd.DataFrame(rows)
                    df['date'] = pd.to_datetime(df['date'])
                    df['code'] = code
                    df['ret_pct'] = df['close'].pct_change() * 100
                    return df
        except Exception as e:
            pass
        time.sleep(2 ** attempt)  # Exponential backoff
    return pd.DataFrame()

if __name__ == '__main__':
    stocks = get_constituents()
    print(f'成分股总数: {len(stocks)}')
    print(f'开始下载，每只股票间隔2秒...')
    
    all_data = []
    success = 0
    fail = 0
    
    for i, code in enumerate(stocks):
        df = fetch_stock(code, START_DATE, END_DATE)
        if not df.empty and len(df) > 100:
            all_data.append(df)
            success += 1
            if (i+1) % 10 == 0:
                print(f'  {i+1}/{len(stocks)}: {code} OK ({len(df)} rows), 成功={success}')
        else:
            fail += 1
            if (i+1) % 10 == 0:
                print(f'  {i+1}/{len(stocks)}: {code} FAIL, 失败={fail}')
        time.sleep(2)  # 2秒间隔
    
    print(f'\n下载完成: 成功={success}, 失败={fail}')
    
    if all_data:
        combined = pd.concat(all_data, ignore_index=True)
        combined.to_csv('cache/telecom_all_stocks.csv', index=False, encoding='utf-8-sig')
        print(f'数据已保存: {len(combined)} 行, {combined["code"].nunique()} 只股票')
        print(f'日期范围: {combined["date"].min()} ~ {combined["date"].max()}')
    else:
        print('无数据')
