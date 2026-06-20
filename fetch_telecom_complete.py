# -*- coding: utf-8 -*-
"""
fetch_telecom_complete.py - 补齐通信行业全部123只成分股（前复权）

策略：
1. 已有67只股票在 cache/telecom_all_stocks.csv（前复权，fqt=1）
2. 需要补齐：56只（123 - 67）
3. 对失败股票进行额外重试
4. 最终合并所有数据
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
INPUT_FILE = 'cache/telecom_all_stocks.csv'
OUTPUT_FILE = 'cache/telecom_final.csv'
REPORT_FILE = 'reports/telecom_constituents_final_report.md'


def get_constituents():
    df = ak.index_stock_cons(symbol='801770')
    result = {}
    for _, row in df.iterrows():
        code = str(row.iloc[0])
        inclusion_date = str(row.iloc[2])
        result[code] = inclusion_date
    return result


def get_market_type(code):
    if code.startswith('6') or code.startswith('8'):
        return '1'
    return '0'


def fetch_stock(code, retries=5):
    """下载单只股票（前复权），增强重试"""
    market = get_market_type(code)
    url = f'"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={market}.{code}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&beg={START_DATE}&end={END_DATE}"'
    
    for attempt in range(retries):
        try:
            cmd = f'curl -s -m 15 {url}'
            result = subprocess.run(cmd, shell=True, capture_output=True, timeout=18)
            
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
                        df['code'] = str(code)  # ensure string type
                        df['ret_pct'] = df['close'].pct_change() * 100
                        return df
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return pd.DataFrame()


def main():
    print("="*80)
    print("通信行业123只成分股数据补齐（前复权）")
    print("="*80)
    
    # 1. 获取成分股列表
    constituents = get_constituents()
    print(f"成分股总数: {len(constituents)}")
    
    # 2. 加载已有数据（转换为字符串code）
    existing_df = pd.read_csv(INPUT_FILE)
    existing_df['code'] = existing_df['code'].astype(str)
    existing_codes = set(existing_df['code'].unique())
    print(f"已有数据: {len(existing_codes)} 只股票")
    
    # 3. 确定需要下载的股票
    all_codes = set(constituents.keys())
    missing_codes = all_codes - existing_codes
    print(f"需要补齐: {len(missing_codes)} 只股票")
    
    # 4. 下载缺失的股票
    all_data = [existing_df]
    success_list = []
    fail_list = []
    
    for i, code in enumerate(sorted(missing_codes)):
        inclusion_date = constituents[code]
        print(f"  [{i+1}/{len(missing_codes)}] {code} (纳入: {inclusion_date}) ...", end='', flush=True)
        
        df = fetch_stock(code)
        
        if not df.empty and len(df) > 50:
            all_data.append(df)
            start_date = df['date'].min().strftime('%Y-%m-%d')
            end_date = df['date'].max().strftime('%Y-%m-%d')
            rows = len(df)
            success_list.append({
                'code': code, 'inclusion_date': inclusion_date,
                'rows': rows, 'start': start_date, 'end': end_date,
            })
            print(f" OK ({rows})")
        else:
            fail_list.append({'code': code, 'inclusion_date': inclusion_date})
            print(f" FAIL")
        
        time.sleep(1.5)
    
    # 5. 合并数据
    print(f"\n合并数据...")
    combined = pd.concat(all_data, ignore_index=True)
    combined.to_csv(OUTPUT_FILE, index=False, encoding='utf-8-sig')
    
    total_success = len(existing_codes) + len(success_list)
    total_fail = len(fail_list)
    
    print(f"\n{'='*80}")
    print(f"下载汇总")
    print(f"{'='*80}")
    print(f"  成功: {total_success}/123")
    print(f"  失败: {total_fail}/123")
    print(f"  总数据: {len(combined)} 行, {combined['code'].nunique()} 只股票")
    print(f"  日期范围: {combined['date'].min()} ~ {combined['date'].max()}")
    
    if fail_list:
        print(f"\n  失败名单（{len(fail_list)}只）:")
        for s in fail_list:
            print(f"    {s['code']} (纳入: {s['inclusion_date']})")
    
    # 6. 生成报告
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write("# 通信行业成分股数据下载最终报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**行业**: 801770 通信\n\n")
        f.write(f"**数据口径**: 前复权（fqt=1）\n\n")
        f.write(f"**日期范围**: {START_DATE} ~ {END_DATE}\n\n")
        
        f.write(f"## 下载汇总\n\n")
        f.write(f"- 成分股总数: 123\n")
        f.write(f"- 下载成功: {total_success}\n")
        f.write(f"- 下载失败: {total_fail}\n")
        f.write(f"- 总数据行: {len(combined)}\n")
        f.write(f"- 数据文件: {OUTPUT_FILE}\n\n")
        
        f.write(f"## 成功名单（{total_success}只）\n\n")
        f.write(f"| 代码 | 纳入日期 | 数据行数 | 起始日期 | 结束日期 |\n")
        f.write(f"|------|----------|----------|----------|----------|\n")
        
        # 已有数据
        for code in sorted(existing_codes):
            code_df = combined[combined['code'] == code]
            start = code_df['date'].min().strftime('%Y-%m-%d')
            end = code_df['date'].max().strftime('%Y-%m-%d')
            rows = len(code_df)
            f.write(f"| {code} | - | {rows} | {start} | {end} |\n")
        
        # 新下载数据
        for s in success_list:
            f.write(f"| {s['code']} | {s['inclusion_date']} | {s['rows']} | {s['start']} | {s['end']} |\n")
        
        if fail_list:
            f.write(f"\n## 失败名单（{len(fail_list)}只）\n\n")
            f.write(f"| 代码 | 纳入日期 |\n")
            f.write(f"|------|----------|\n")
            for s in fail_list:
                f.write(f"| {s['code']} | {s['inclusion_date']} |\n")
        
        f.write(f"\n## 纳入日期分布（成功股票）\n\n")
        from collections import Counter
        year_counts = Counter()
        for s in success_list:
            year_counts[s['inclusion_date'][:4]] += 1
        for code in existing_codes:
            year_counts['-'] += 1
        for year in sorted(year_counts.keys()):
            f.write(f"- {year}年: {year_counts[year]} 只\n")
        
        f.write(f"\n## 使用说明\n\n")
        f.write(f"1. 数据口径: 前复权（fqt=1）\n")
        f.write(f"2. 每只股票仅在纳入日期之后参与行业指标计算\n")
        f.write(f"3. 失败的股票暂不参与，后续可补充\n")
        f.write(f"4. 数据用于 v1.3 Step 3 通信行业龙头/扩散度指标计算\n")
    
    print(f"\n报告已保存: {REPORT_FILE}")
    print(f"\n[OK] 完成")


if __name__ == '__main__':
    main()
