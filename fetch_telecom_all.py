# -*- coding: utf-8 -*-
"""
fetch_telecom_all.py - 下载通信行业全部123只成分股数据（前复权）

数据要求：
- 前复权数据（fqt=1）
- 2022-01-01 ~ 2026-06-12
- 123只成分股全部下载
- 记录每只股票的纳入日期（用于后续指标过滤）
- 支持断点续传

下载策略：
- 东财API直连（curl）
- 3秒间隔 + 指数退避重试
- 缓存进度，支持续传
- 前复权口径
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
    """获取通信行业成分股列表（含纳入日期）"""
    df = ak.index_stock_cons(symbol='801770')
    
    # 重命名列（处理中文编码）
    cols = list(df.columns)
    # 假设列顺序：品种代码、品种名称、纳入日期
    result = []
    for _, row in df.iterrows():
        result.append({
            'code': str(row.iloc[0]),
            'name': str(row.iloc[1]),
            'inclusion_date': str(row.iloc[2]),
        })
    return result


def get_market_type(code):
    """判断股票所属市场（0=深圳，1=上海）"""
    if code.startswith('6') or code.startswith('8'):
        return '1'
    else:
        return '0'


def load_progress():
    """加载下载进度"""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_progress(progress):
    """保存下载进度"""
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def fetch_stock_with_retry(code, retries=5):
    """下载单只股票数据（前复权），带指数退避重试"""
    market = get_market_type(code)
    url = f'"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={market}.{code}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&beg={START_DATE}&end={END_DATE}"'
    
    for attempt in range(retries):
        try:
            cmd = f'curl -s -m 20 {url}'
            result = subprocess.run(cmd, shell=True, capture_output=True, timeout=25)
            
            if result.returncode == 0 and result.stdout:
                try:
                    data = json.loads(result.stdout.decode('utf-8', errors='ignore'))
                    if data.get('data') and data['data'].get('klines'):
                        klines = data['data']['klines']
                        rows = []
                        for line in klines:
                            parts = line.split(',')
                            if len(parts) >= 6:
                                rows.append({
                                    'date': parts[0],
                                    'open': float(parts[1]),
                                    'close': float(parts[2]),
                                    'high': float(parts[3]),
                                    'low': float(parts[4]),
                                    'volume': int(parts[5]),
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
        except Exception:
            pass
        
        # 指数退避
        wait_time = min(2 ** attempt, 30)
        time.sleep(wait_time)
    
    return pd.DataFrame()


def main():
    print("="*80)
    print("通信行业全部123只成分股数据下载（前复权）")
    print("="*80)
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据口径: 前复权（fqt=1）")
    print(f"日期范围: {START_DATE} ~ {END_DATE}")
    print()
    
    # 1. 获取成分股列表
    print("[1/3] 获取通信行业成分股列表...")
    constituents = get_constituents_with_dates()
    print(f"  成分股总数: {len(constituents)}")
    
    # 2. 加载进度
    progress = load_progress()
    print(f"  已有进度: {len(progress)} 只股票")
    
    # 3. 下载所有成分股
    print(f"\n[2/3] 开始下载123只成分股...")
    print(f"  间隔: 3秒/只")
    print(f"  重试: 最多5次，指数退避")
    
    all_data = []
    success_list = []
    fail_list = []
    
    for i, stock in enumerate(constituents):
        code = stock['code']
        name = stock['name']
        inclusion_date = stock['inclusion_date']
        
        # 检查是否已下载
        if code in progress and progress[code].get('status') == 'success':
            print(f"  [{i+1}/{len(constituents)}] {code} ({name}) - 已下载，跳过")
            success_list.append({
                'code': code, 'name': name, 'inclusion_date': inclusion_date,
                'rows': progress[code].get('rows', 0),
                'start': progress[code].get('start', ''),
                'end': progress[code].get('end', ''),
            })
            continue
        
        print(f"  [{i+1}/{len(constituents)}] {code} ({name}) - 纳入日期: {inclusion_date} ...", end='')
        
        df = fetch_stock_with_retry(code)
        
        if not df.empty and len(df) > 50:
            all_data.append(df)
            start_date = df['date'].min().strftime('%Y-%m-%d')
            end_date = df['date'].max().strftime('%Y-%m-%d')
            rows = len(df)
            
            progress[code] = {
                'status': 'success',
                'name': name,
                'inclusion_date': inclusion_date,
                'rows': rows,
                'start': start_date,
                'end': end_date,
            }
            
            success_list.append({
                'code': code, 'name': name, 'inclusion_date': inclusion_date,
                'rows': rows, 'start': start_date, 'end': end_date,
            })
            
            print(f" OK ({rows} rows, {start_date} ~ {end_date})")
        else:
            progress[code] = {
                'status': 'failed',
                'name': name,
                'inclusion_date': inclusion_date,
            }
            fail_list.append({
                'code': code, 'name': name, 'inclusion_date': inclusion_date,
            })
            print(f" FAIL")
        
        save_progress(progress)
        time.sleep(3)  # 3秒间隔
    
    # 4. 汇总
    print(f"\n[3/3] 下载汇总")
    print(f"  成功: {len(success_list)}/{len(constituents)}")
    print(f"  失败: {len(fail_list)}/{len(constituents)}")
    
    if success_list:
        total_rows = sum(s['rows'] for s in success_list)
        print(f"  总数据行: {total_rows}")
        
        # 保存数据
        if all_data:
            combined = pd.concat(all_data, ignore_index=True)
            combined.to_csv(OUTPUT_FILE, index=False, encoding='utf-8-sig')
            print(f"\n  数据已保存: {OUTPUT_FILE}")
            print(f"  合计: {len(combined)} rows, {combined['code'].nunique()} stocks")
            print(f"  日期范围: {combined['date'].min()} ~ {combined['date'].max()}")
    
    if fail_list:
        print(f"\n  失败名单（{len(fail_list)}只）:")
        for s in fail_list:
            print(f"    {s['code']} {s['name']} (纳入: {s['inclusion_date']})")
    
    # 保存报告
    report_path = 'reports/telecom_constituents_download_report.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# 通信行业成分股数据下载报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**行业**: 801770 通信\n\n")
        f.write(f"**数据口径**: 前复权（fqt=1）\n\n")
        f.write(f"**日期范围**: {START_DATE} ~ {END_DATE}\n\n")
        
        f.write(f"## 下载汇总\n\n")
        f.write(f"- 成分股总数: {len(constituents)}\n")
        f.write(f"- 下载成功: {len(success_list)}\n")
        f.write(f"- 下载失败: {len(fail_list)}\n")
        if success_list:
            total_rows = sum(s['rows'] for s in success_list)
            f.write(f"- 总数据行: {total_rows}\n")
        f.write(f"- 下载间隔: 3秒/只\n")
        f.write(f"- 重试策略: 最多5次，指数退避\n\n")
        
        f.write(f"## 成功名单（{len(success_list)}只）\n\n")
        f.write(f"| 代码 | 名称 | 纳入日期 | 数据行数 | 起始日期 | 结束日期 |\n")
        f.write(f"|------|------|----------|----------|----------|----------|\n")
        for s in success_list:
            f.write(f"| {s['code']} | {s['name']} | {s['inclusion_date']} | {s['rows']} | {s['start']} | {s['end']} |\n")
        
        if fail_list:
            f.write(f"\n## 失败名单（{len(fail_list)}只）\n\n")
            f.write(f"| 代码 | 名称 | 纳入日期 |\n")
            f.write(f"|------|------|----------|\n")
            for s in fail_list:
                f.write(f"| {s['code']} | {s['name']} | {s['inclusion_date']} |\n")
        
        f.write(f"\n## 纳入日期分布\n\n")
        from collections import Counter
        year_counts = Counter([s['inclusion_date'][:4] for s in success_list])
        for year in sorted(year_counts.keys()):
            f.write(f"- {year}年: {year_counts[year]} 只\n")
        
        f.write(f"\n## 下一步\n\n")
        f.write(f"- 使用成功下载的成分股数据计算行业龙头/扩散度指标\n")
        f.write(f"- 每只股票仅在纳入日期之后参与行业指标计算\n")
        f.write(f"- 失败的股票暂不参与，待后续补充数据\n")
    
    print(f"\n报告已保存: {report_path}")
    print(f"\n[OK] 下载完成")


if __name__ == '__main__':
    main()
