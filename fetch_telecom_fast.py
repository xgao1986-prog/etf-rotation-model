# -*- coding: utf-8 -*-
"""
fetch_telecom_fast.py - 快速下载通信行业全部123只成分股数据（前复权）

优化策略：
- 1.5秒间隔（总耗时约185秒，符合300秒限制）
- curl 10秒超时
- 最多3次重试
- 支持断点续传
"""
import pandas as pd
import subprocess
import json
import time
import os
from datetime import datetime

import akshare as ak

START_DATE = '20220101'
END_DATE = '20260612'
CACHE_FILE = 'cache/telecom_all_progress.json'
OUTPUT_FILE = 'cache/telecom_all_stocks.csv'


def get_constituents_with_dates():
    df = ak.index_stock_cons(symbol='801770')
    result = []
    for _, row in df.iterrows():
        result.append({
            'code': str(row.iloc[0]),
            'name': str(row.iloc[1]),
            'inclusion_date': str(row.iloc[2]),
        })
    return result


def get_market_type(code):
    if code.startswith('6') or code.startswith('8'):
        return '1'
    return '0'


def load_progress():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_progress(progress):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def fetch_stock(code, retries=3):
    """下载单只股票（前复权），快速模式"""
    market = get_market_type(code)
    url = f'"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={market}.{code}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&beg={START_DATE}&end={END_DATE}"'
    
    for attempt in range(retries):
        try:
            cmd = f'curl -s -m 10 {url}'
            result = subprocess.run(cmd, shell=True, capture_output=True, timeout=12)
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout.decode('utf-8', errors='ignore'))
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
                    if rows:
                        df = pd.DataFrame(rows)
                        df['date'] = pd.to_datetime(df['date'])
                        df['code'] = code
                        df['ret_pct'] = df['close'].pct_change() * 100
                        return df
        except Exception:
            pass
        time.sleep(1.5 ** attempt)
    return pd.DataFrame()


def main():
    print("="*80)
    print("通信行业123只成分股数据下载（前复权，快速模式）")
    print("="*80)
    
    constituents = get_constituents_with_dates()
    print(f"成分股总数: {len(constituents)}")
    
    progress = load_progress()
    print(f"已有进度: {len(progress)} 只")
    
    all_data = []
    success_list = []
    fail_list = []
    
    for i, stock in enumerate(constituents):
        code = stock['code']
        name = stock['name']
        inclusion_date = stock['inclusion_date']
        
        if code in progress and progress[code].get('status') == 'success':
            print(f"  [{i+1}/{len(constituents)}] {code} - 已下载，跳过")
            success_list.append({
                'code': code, 'name': name, 'inclusion_date': inclusion_date,
                'rows': progress[code].get('rows', 0),
                'start': progress[code].get('start', ''),
                'end': progress[code].get('end', ''),
            })
            continue
        
        print(f"  [{i+1}/{len(constituents)}] {code} ({name}) ...", end='', flush=True)
        
        df = fetch_stock(code)
        
        if not df.empty and len(df) > 50:
            all_data.append(df)
            start_date = df['date'].min().strftime('%Y-%m-%d')
            end_date = df['date'].max().strftime('%Y-%m-%d')
            rows = len(df)
            
            progress[code] = {
                'status': 'success', 'name': name,
                'inclusion_date': inclusion_date, 'rows': rows,
                'start': start_date, 'end': end_date,
            }
            success_list.append({
                'code': code, 'name': name, 'inclusion_date': inclusion_date,
                'rows': rows, 'start': start_date, 'end': end_date,
            })
            print(f" OK ({rows})")
        else:
            progress[code] = {
                'status': 'failed', 'name': name, 'inclusion_date': inclusion_date,
            }
            fail_list.append({'code': code, 'name': name, 'inclusion_date': inclusion_date})
            print(f" FAIL")
        
        save_progress(progress)
        time.sleep(1.5)  # 1.5秒间隔
    
    print(f"\n下载汇总: 成功={len(success_list)}, 失败={len(fail_list)}")
    
    if all_data:
        combined = pd.concat(all_data, ignore_index=True)
        combined.to_csv(OUTPUT_FILE, index=False, encoding='utf-8-sig')
        print(f"数据保存: {OUTPUT_FILE}")
        print(f"  {len(combined)} rows, {combined['code'].nunique()} stocks")
    
    if fail_list:
        print(f"\n失败名单（{len(fail_list)}只）:")
        for s in fail_list:
            print(f"  {s['code']} {s['name']} (纳入: {s['inclusion_date']})")
    
    print(f"\n[OK] 完成")


if __name__ == '__main__':
    main()
