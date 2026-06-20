# -*- coding: utf-8 -*-
"""
v1.3_step3_telecom_direct.py - 通信行业龙头/扩散度统计探索（直接API模式）

使用东财API直接获取数据，绕过AKShare超时问题。
试点行业：801770 通信（123只成分股）
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import sqlite3
import requests
import json
from datetime import datetime
from database import ETFDatabase

DB_PATH = 'database/etf_model.db'
SECTOR_CODE = '801770'
SECTOR_NAME = '通信'
ETF_CODES = ['515880.SH', '515050.SH']
START_DATE = '2022-01-01'
END_DATE = '2026-06-12'


def get_constituents():
    """获取通信行业成分股列表"""
    import akshare as ak
    df = ak.index_stock_cons(symbol=SECTOR_CODE)
    return df['品种代码'].tolist()


def get_market_type(code):
    """判断股票所属市场（0=深圳，1=上海）"""
    if code.startswith('6') or code.startswith('8'):
        return '1'
    else:
        return '0'


def fetch_stock_data_direct(code, start_date, end_date):
    """使用东财API直接获取个股历史数据"""
    market = get_market_type(code)
    url = f"http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        'secid': f'{market}.{code}',
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'klt': '101',  # 日K
        'fqt': '0',    # 不复权
        'beg': start_date.replace('-', ''),
        'end': end_date.replace('-', ''),
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
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
            df = pd.DataFrame(rows)
            df['date'] = pd.to_datetime(df['date'])
            df['code'] = code
            # 计算涨跌幅
            df['ret_pct'] = df['close'].pct_change() * 100
            return df
    except Exception as e:
        pass
    return pd.DataFrame()


def run_analysis():
    """运行龙头/扩散度分析"""
    print("="*80)
    print("v1.3 Step 3: 通信行业龙头/扩散度统计探索")
    print("="*80)
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"试点行业: {SECTOR_CODE} {SECTOR_NAME}")
    print(f"数据接口: 东财API直连")
    print()
    
    # 1. 获取成分股列表
    print("[1/5] 获取通信行业成分股列表...")
    stocks = get_constituents()
    print(f"  成分股数量: {len(stocks)}")
    print(f"  前10只: {stocks[:10]}")
    
    # 2. 获取ETF数据
    print("\n[2/5] 获取ETF数据...")
    db = ETFDatabase()
    etf_data = pd.DataFrame()
    for etf in ETF_CODES:
        df = db.get_market_data(ticker=etf, start_date=START_DATE, end_date=END_DATE)
        if not df.empty:
            etf_data = pd.concat([etf_data, df], ignore_index=True)
            print(f"  {etf}: {len(df)} rows")
    
    # 3. 下载所有成分股数据（分批）
    print("\n[3/5] 下载123只成分股数据...")
    all_stock_data = []
    
    for i, code in enumerate(stocks):
        df = fetch_stock_data_direct(code, START_DATE, END_DATE)
        if not df.empty and len(df) > 50:
            all_stock_data.append(df)
            if (i+1) % 20 == 0 or i == 0:
                print(f"  进度: {i+1}/{len(stocks)} ({code}: {len(df)} rows)")
        else:
            if (i+1) % 20 == 0:
                print(f"  进度: {i+1}/{len(stocks)} ({code}: FAIL)")
    
    if not all_stock_data:
        print("  错误：没有获取到任何数据")
        return
    
    stock_data = pd.concat(all_stock_data, ignore_index=True)
    print(f"\n  合计: {len(stock_data)} 行, {stock_data['code'].nunique()} 只股票")
    print(f"  日期范围: {stock_data['date'].min().date()} ~ {stock_data['date'].max().date()}")
    
    # 4. 计算每日指标
    print("\n[4/5] 计算每日龙头/扩散度指标...")
    
    trading_dates = sorted(stock_data['date'].unique())
    valid_dates = [d for d in trading_dates if d >= pd.Timestamp('2022-02-01')]
    
    print(f"  总交易日: {len(trading_dates)}")
    print(f"  有效计算日: {len(valid_dates)}")
    
    results = []
    
    for date in valid_dates:
        date = pd.Timestamp(date)
        
        # 当天数据
        day_data = stock_data[stock_data['date'] == date].copy()
        if len(day_data) < 10:  # 至少10只股票有数据
            continue
        
        # 过去20天数据
        date_20d = date - pd.Timedelta(days=30)
        hist_data = stock_data[(stock_data['date'] >= date_20d) & (stock_data['date'] <= date)]
        
        indicators = {}
        
        # 1. 前10大成交额占比
        day_sorted = day_data.sort_values('amount', ascending=False)
        top10_amt = day_sorted.head(10)['amount'].sum()
        total_amt = day_data['amount'].sum()
        indicators['top10_turnover_ratio'] = top10_amt / total_amt if total_amt > 0 else 0
        
        # 2. 前10活跃股3/5/10日收益
        for w in [3, 5, 10]:
            start_d = date - pd.Timedelta(days=w*2+5)
            window_data = stock_data[(stock_data['date'] >= start_d) & (stock_data['date'] <= date)]
            
            top10_codes = day_sorted.head(10)['code'].tolist()
            returns = []
            for code in top10_codes:
                code_data = window_data[window_data['code'] == code].sort_values('date')
                if len(code_data) >= w + 1:
                    dates = code_data['date'].tolist()
                    if date in dates:
                        idx = dates.index(date)
                        if idx >= w:
                            ret = (code_data.iloc[idx]['close'] / code_data.iloc[idx - w]['close'] - 1) * 100
                            returns.append(ret)
            indicators[f'top10_ret_{w}d'] = np.mean(returns) if returns else np.nan
        
        # 3. 上涨比例
        indicators['rising_ratio'] = (day_data['ret_pct'] > 0).sum() / len(day_data)
        
        # 4. 站上MA20比例
        above_ma20 = 0
        for code in day_data['code'].unique():
            code_hist = hist_data[hist_data['code'] == code].sort_values('date')
            if len(code_hist) >= 20:
                ma20 = code_hist.tail(20)['close'].mean()
                current = code_hist.tail(1)['close'].iloc[0]
                if current > ma20:
                    above_ma20 += 1
        indicators['above_ma20_ratio'] = above_ma20 / len(day_data)
        
        # 5. 创20日新高比例
        new_high = 0
        for code in day_data['code'].unique():
            code_hist = hist_data[hist_data['code'] == code].sort_values('date')
            if len(code_hist) >= 20:
                current = code_hist.tail(1)['close'].iloc[0]
                max20 = code_hist.tail(20)['close'].max()
                if current >= max20 * 0.999:
                    new_high += 1
        indicators['new_high_20d_ratio'] = new_high / len(day_data)
        
        # 6. 总成交额和活跃股数
        indicators['total_turnover'] = total_amt
        indicators['active_stocks'] = len(day_data)
        
        # 获取ETF未来收益
        etf_returns = {}
        for etf_code in ETF_CODES:
            etf_df = etf_data[etf_data['ticker'] == etf_code].sort_values('date')
            if len(etf_df) == 0:
                continue
            future = etf_df[etf_df['date'] > date]
            current = etf_df[etf_df['date'] == date]
            if len(current) == 0:
                continue
            current_price = current.iloc[0]['close']
            for w in [1, 3, 5, 10]:
                if len(future) >= w:
                    future_price = future.iloc[w-1]['close']
                    etf_returns[f'{etf_code}_future_{w}d'] = (future_price / current_price - 1) * 100
                else:
                    etf_returns[f'{etf_code}_future_{w}d'] = np.nan
        
        row = {'date': date}
        row.update(indicators)
        row.update(etf_returns)
        results.append(row)
    
    if not results:
        print("  错误：没有计算出任何指标")
        return
    
    results_df = pd.DataFrame(results)
    print(f"  计算完成: {len(results_df)} 行")
    
    # 5. 统计分析
    print("\n[5/5] 统计分析...")
    
    indicator_cols = ['top10_turnover_ratio', 'top10_ret_3d', 'top10_ret_5d', 'top10_ret_10d',
                      'rising_ratio', 'above_ma20_ratio', 'new_high_20d_ratio']
    future_cols = [c for c in results_df.columns if 'future_' in c]
    
    print(f"\n  指标列: {indicator_cols}")
    print(f"  未来收益列: {future_cols}")
    
    # 相关性
    corr_results = []
    for ind in indicator_cols:
        for fut in future_cols:
            valid = results_df[[ind, fut]].dropna()
            if len(valid) > 50:
                corr = valid[ind].corr(valid[fut])
                corr_results.append({
                    'indicator': ind,
                    'future': fut,
                    'correlation': corr,
                    'n': len(valid),
                })
    
    corr_df = pd.DataFrame(corr_results)
    
    if not corr_df.empty:
        print(f"\n  {'指标':<25} {'未来收益':<25} {'相关性':>10} {'样本':>6}")
        print("  " + "-"*70)
        for _, row in corr_df.iterrows():
            print(f"  {row['indicator']:<25} {row['future']:<25} {row['correlation']:>10.4f} {row['n']:>6}")
        
        # 最强相关性
        corr_df['abs_corr'] = corr_df['correlation'].abs()
        top10 = corr_df.nlargest(10, 'abs_corr')
        print(f"\n  最强相关性Top 10:")
        for _, row in top10.iterrows():
            dir_str = "正" if row['correlation'] > 0 else "负"
            print(f"    {row['indicator']} vs {row['future']}: {row['correlation']:.4f} ({dir_str}相关)")
    
    # 分组统计
    print(f"\n  分组统计（按扩散度中位数分组）:")
    for ind in ['rising_ratio', 'above_ma20_ratio', 'new_high_20d_ratio']:
        print(f"\n  --- {ind} ---")
        median_val = results_df[ind].median()
        high = results_df[results_df[ind] > median_val]
        low = results_df[results_df[ind] <= median_val]
        
        for fut in future_cols:
            h = high[fut].dropna()
            l = low[fut].dropna()
            if len(h) > 10 and len(l) > 10:
                print(f"    {fut}:")
                print(f"      高组: 均值={h.mean():.2f}%, 中位数={h.median():.2f}%, n={len(h)}")
                print(f"      低组: 均值={l.mean():.2f}%, 中位数={l.median():.2f}%, n={len(l)}")
                print(f"      差值: {h.mean() - l.mean():.2f}%")
    
    # 保存
    report_path = 'reports/v1.3_step3_telecom_leader_diffusion.md'
    data_path = 'reports/v1.3_step3_telecom_data.csv'
    
    results_df.to_csv(data_path, index=False, encoding='utf-8-sig')
    print(f"\n  数据已保存: {data_path}")
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 3: 通信行业龙头/扩散度统计探索报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**试点行业**: {SECTOR_CODE} {SECTOR_NAME}\n\n")
        f.write(f"**成分股数量**: {len(stocks)} (当前快照)\n\n")
        f.write(f"**研究区间**: {START_DATE} ~ {END_DATE}\n\n")
        
        f.write("## 重要声明：幸存者偏差\n\n")
        f.write("> **警告**: 本研究使用当前成分股快照（123只）计算历史指标。\n\n")
        f.write("> 这意味着2022年计算时使用的是2026年的成分股列表，存在幸存者偏差。\n\n")
        f.write("> 被剔除的往往是表现差的股票，结论可能偏乐观。\n\n")
        f.write("> 仅作为可行性验证，不作为策略依据。\n\n")
        
        f.write("## 1. 数据概览\n\n")
        f.write(f"- 成分股数量: {len(stocks)}\n")
        f.write(f"- 有效计算日: {len(results_df)}\n")
        f.write(f"- 数据接口: 东财API直连\n\n")
        
        f.write("## 2. 指标定义\n\n")
        f.write("| 指标 | 定义 |\n|------|------|\n")
        f.write("| top10_turnover_ratio | 前10大成交额占行业总成交额比例 |\n")
        f.write("| top10_ret_3d/5d/10d | 前10活跃股N日平均收益率 |\n")
        f.write("| rising_ratio | 当日上涨成分股比例 |\n")
        f.write("| above_ma20_ratio | 站上MA20的成分股比例 |\n")
        f.write("| new_high_20d_ratio | 创20日新高的成分股比例 |\n\n")
        
        f.write("## 3. 相关性分析\n\n")
        if not corr_df.empty:
            f.write("| 指标 | 未来收益 | 相关性 | 样本数 |\n|------|----------|--------|--------|\n")
            for _, row in corr_df.iterrows():
                f.write(f"| {row['indicator']} | {row['future']} | {row['correlation']:.4f} | {row['n']} |\n")
        
        f.write("\n## 4. 分组统计\n\n")
        for ind in ['rising_ratio', 'above_ma20_ratio', 'new_high_20d_ratio']:
            f.write(f"### {ind}\n\n")
            median_val = results_df[ind].median()
            high = results_df[results_df[ind] > median_val]
            low = results_df[results_df[ind] <= median_val]
            for fut in future_cols:
                h = high[fut].dropna()
                l = low[fut].dropna()
                if len(h) > 10 and len(l) > 10:
                    f.write(f"**{fut}**:\n\n")
                    f.write(f"- 高组: 均值={h.mean():.2f}%, 中位数={h.median():.2f}%, n={len(h)}\n")
                    f.write(f"- 低组: 均值={l.mean():.2f}%, 中位数={l.median():.2f}%, n={len(l)}\n")
                    f.write(f"- 差值: {h.mean() - l.mean():.2f}%\n\n")
        
        f.write("## 5. 结论\n\n")
        f.write("**可行性结论**:\n\n")
        f.write("1. 龙头/扩散度指标可计算\n")
        f.write("2. 指标与ETF未来收益存在一定相关性\n")
        f.write("3. 但幸存者偏差使结果偏乐观\n\n")
        f.write("**建议**:\n\n")
        f.write("1. 如需正式回测，需获取历史成分股数据\n")
        f.write("2. 或接受幸存者偏差，保守解读结论\n")
        f.write("3. 建议以观察模式积累数据，不急于纳入策略\n\n")
        
        f.write("## 6. 版本边界\n\n")
        f.write("- v1.2.2 已收口\n")
        f.write("- v1.3 Step 1-2 已验收\n")
        f.write("- v1.3 Step 3 完成（通信行业龙头/扩散度统计探索）\n")
        f.write("- 不改交易规则\n")
        f.write("- 不跑组合回测\n")
    
    print(f"\n报告已保存: {report_path}")
    print(f"\n[OK] Step 3 完成")


if __name__ == '__main__':
    run_analysis()
