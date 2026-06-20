# -*- coding: utf-8 -*-
"""
fetch_telecom_curl.py - 使用curl下载通信行业成分股数据
"""
import subprocess
import json
import pandas as pd
import time
from datetime import datetime

START_DATE = '20220101'
END_DATE = '20260612'

def get_constituents():
    import akshare as ak
    df = ak.index_stock_cons(symbol='801770')
    return df['品种代码'].tolist()

def get_market_type(code):
    if code.startswith('6') or code.startswith('8'):
        return '1'
    else:
        return '0'

def fetch_with_curl(code):
    market = get_market_type(code)
    url = f'"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={market}.{code}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=0&beg={START_DATE}&end={END_DATE}"'
    
    cmd = f'curl -s -m 15 {url}'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
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
    return pd.DataFrame()

if __name__ == '__main__':
    stocks = get_constituents()
    print(f'成分股: {len(stocks)}')
    
    all_data = []
    success = 0
    
    for i, code in enumerate(stocks):
        df = fetch_with_curl(code)
        if not df.empty and len(df) > 100:
            all_data.append(df)
            success += 1
            print(f'  {i+1}/{len(stocks)}: {code} OK ({len(df)} rows), 成功={success}')
        else:
            if (i+1) % 10 == 0:
                print(f'  {i+1}/{len(stocks)}: {code} FAIL, 成功={success}')
        time.sleep(1.5)
    
    print(f'\n完成: 成功={success}/{len(stocks)}')
    
    if all_data:
        combined = pd.concat(all_data, ignore_index=True)
        combined.to_csv('cache/telecom_curl.csv', index=False, encoding='utf-8-sig')
        print(f'保存: {len(combined)} rows, {combined["code"].nunique()} stocks')
    else:
        print('无数据')
