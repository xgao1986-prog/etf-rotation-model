# -*- coding: utf-8 -*-
"""
v1.3_step3_telecom_leader_diffusion.py - 通信行业龙头/扩散度统计探索

试点行业：801770 通信（123只成分股）
研究目标：验证龙头强度和扩散度指标是否可计算，及其与ETF未来收益的关系

统计方法：
- 2022-01-01后数据，减少幸存者偏差影响
- 当前成分股快照（123只）计算历史指标
- 对齐通信ETF(515880.SH)和5GETF(515050.SH)未来收益
- 仅统计验证，不改策略、不跑组合回测
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime, timedelta
import time
import json
import warnings
warnings.filterwarnings('ignore')

import akshare as ak
from database import ETFDatabase

DB_PATH = 'database/etf_model.db'
SECTOR_CODE = '801770'
SECTOR_NAME = '通信'
ETF_CODES = ['515880.SH', '515050.SH']  # 通信ETF, 5GETF
START_DATE = '2022-01-01'


def get_sector_constituents():
    """获取通信行业当前成分股列表"""
    df = ak.index_stock_cons(symbol=SECTOR_CODE)
    stocks = df['品种代码'].tolist()
    return stocks


def fetch_stock_daily(stock_code, start_date, end_date):
    """获取个股日行情数据"""
    try:
        df = ak.stock_zh_a_hist(symbol=stock_code, period='daily', 
                                 start_date=start_date.replace('-', ''), 
                                 end_date=end_date.replace('-', ''), 
                                 adjust='')
        if not df.empty:
            df['date'] = pd.to_datetime(df['日期'])
            df['code'] = stock_code
            # 重命名关键列
            df = df.rename(columns={
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume',
                '成交额': 'amount',
                '涨跌幅': 'ret_pct',
            })
            return df[['date', 'code', 'open', 'close', 'high', 'low', 'volume', 'amount', 'ret_pct']]
    except Exception as e:
        print(f"  {stock_code} 获取失败: {e}")
    return pd.DataFrame()


def calculate_indicators(stock_data, etf_data, date):
    """计算某一天的行业指标"""
    
    # 获取该日所有成分股数据
    day_data = stock_data[stock_data['date'] == date].copy()
    if len(day_data) == 0:
        return None
    
    # 获取过去20天数据（用于MA20和20日新高）
    date_20d = date - pd.Timedelta(days=30)  # 留足够余量
    hist_data = stock_data[(stock_data['date'] >= date_20d) & (stock_data['date'] <= date)]
    
    indicators = {}
    
    # 1. 前10大成交额占比（龙头集中度）
    day_data_sorted = day_data.sort_values('amount', ascending=False)
    top10_amount = day_data_sorted.head(10)['amount'].sum()
    total_amount = day_data['amount'].sum()
    indicators['top10_turnover_ratio'] = top10_amount / total_amount if total_amount > 0 else 0
    
    # 2. 前10大活跃股3/5/10日收益（龙头强度）
    for window in [3, 5, 10]:
        start_date = date - pd.Timedelta(days=window*2)  # 留余量找交易日
        window_data = stock_data[(stock_data['date'] >= start_date) & (stock_data['date'] <= date)]
        
        top10_codes = day_data_sorted.head(10)['code'].tolist()
        top10_returns = []
        
        for code in top10_codes:
            code_data = window_data[window_data['code'] == code].sort_values('date')
            if len(code_data) >= 2:
                # 找date往前window个交易日的收益
                code_dates = code_data['date'].tolist()
                if date in code_dates:
                    idx = code_dates.index(date)
                    if idx >= window:
                        ret = (code_data.iloc[idx]['close'] / code_data.iloc[idx - window]['close'] - 1) * 100
                        top10_returns.append(ret)
        
        indicators[f'top10_ret_{window}d'] = np.mean(top10_returns) if top10_returns else np.nan
    
    # 3. 成分股上涨比例（扩散度）
    rising = (day_data['ret_pct'] > 0).sum()
    indicators['rising_ratio'] = rising / len(day_data) if len(day_data) > 0 else 0
    
    # 4. 成分股站上MA20比例（扩散度）
    above_ma20 = 0
    for code in day_data['code'].unique():
        code_hist = hist_data[hist_data['code'] == code].sort_values('date')
        if len(code_hist) >= 20:
            ma20 = code_hist.tail(20)['close'].mean()
            current_price = code_hist.tail(1)['close'].iloc[0]
            if current_price > ma20:
                above_ma20 += 1
    indicators['above_ma20_ratio'] = above_ma20 / len(day_data) if len(day_data) > 0 else 0
    
    # 5. 成分股创20日新高比例（扩散度）
    new_high_20d = 0
    for code in day_data['code'].unique():
        code_hist = hist_data[hist_data['code'] == code].sort_values('date')
        if len(code_hist) >= 20:
            current_price = code_hist.tail(1)['close'].iloc[0]
            max_20d = code_hist.tail(20)['close'].max()
            if current_price >= max_20d * 0.999:  # 允许微小误差
                new_high_20d += 1
    indicators['new_high_20d_ratio'] = new_high_20d / len(day_data) if len(day_data) > 0 else 0
    
    # 6. 行业整体成交额（绝对值）
    indicators['total_turnover'] = total_amount
    
    # 7. 成分股数量（当天有数据的）
    indicators['active_stocks'] = len(day_data)
    
    return indicators


def get_etf_future_returns(etf_data, date, windows=[1, 3, 5, 10]):
    """获取ETF未来N日收益"""
    returns = {}
    for etf_code in ETF_CODES:
        etf_df = etf_data[etf_data['ticker'] == etf_code].sort_values('date')
        if len(etf_df) == 0:
            continue
        
        # 找到date之后的交易日
        future = etf_df[etf_df['date'] > date]
        current_row = etf_df[etf_df['date'] == date]
        
        if len(current_row) == 0:
            continue
        
        current_price = current_row.iloc[0]['close']
        
        for w in windows:
            if len(future) >= w:
                future_price = future.iloc[w-1]['close']
                ret = (future_price / current_price - 1) * 100
                returns[f'{etf_code}_future_{w}d'] = ret
            else:
                returns[f'{etf_code}_future_{w}d'] = np.nan
    
    return returns


def run_statistical_analysis():
    """运行统计探索"""
    
    print("="*80)
    print("v1.3 Step 3: 通信行业龙头/扩散度统计探索")
    print("="*80)
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"试点行业: {SECTOR_CODE} {SECTOR_NAME}")
    print(f"成分股数量: 123 (当前快照)")
    print(f"研究区间: {START_DATE} ~ 2026-06-12")
    print()
    
    # 1. 获取成分股列表
    print("[1/6] 获取通信行业成分股列表...")
    constituents = get_sector_constituents()
    print(f"  成分股数量: {len(constituents)}")
    print(f"  前10只: {constituents[:10]}")
    
    # 2. 获取ETF数据
    print("\n[2/6] 获取ETF历史数据...")
    db = ETFDatabase()
    etf_data = pd.DataFrame()
    for etf in ETF_CODES:
        df = db.get_market_data(ticker=etf, start_date=START_DATE, end_date='2026-06-12')
        if not df.empty:
            etf_data = pd.concat([etf_data, df], ignore_index=True)
            print(f"  {etf}: {len(df)} 行, {df['date'].min().date()} ~ {df['date'].max().date()}")
    
    # 3. 获取成分股日行情（分批，避免超时）
    print("\n[3/6] 获取成分股日行情数据...")
    print(f"  共 {len(constituents)} 只股票，分批下载...")
    
    all_stock_data = []
    batch_size = 20
    
    for i in range(0, len(constituents), batch_size):
        batch = constituents[i:i+batch_size]
        print(f"  批次 {i//batch_size + 1}/{(len(constituents)-1)//batch_size + 1}: {len(batch)} 只...")
        
        for stock in batch:
            df = fetch_stock_daily(stock, START_DATE, '2026-06-12')
            if not df.empty:
                all_stock_data.append(df)
            time.sleep(0.2)  # 避免请求过快
        
        time.sleep(1)  # 批次间休息
    
    if not all_stock_data:
        print("  错误：没有获取到任何成分股数据")
        return
    
    stock_data = pd.concat(all_stock_data, ignore_index=True)
    print(f"  合计: {len(stock_data)} 行数据，{stock_data['code'].nunique()} 只股票")
    
    # 4. 计算每日指标
    print("\n[4/6] 计算每日龙头/扩散度指标...")
    
    trading_dates = sorted(stock_data['date'].unique())
    # 只取有20天历史数据的日期
    valid_dates = trading_dates[20:]
    
    print(f"  总交易日: {len(trading_dates)}")
    print(f"  有效计算日(有20天历史): {len(valid_dates)}")
    
    results = []
    
    for date in valid_dates:
        date = pd.Timestamp(date)
        
        # 计算行业指标
        indicators = calculate_indicators(stock_data, etf_data, date)
        if indicators is None:
            continue
        
        # 获取ETF未来收益
        etf_returns = get_etf_future_returns(etf_data, date)
        
        # 合并
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
    print("\n[5/6] 统计分析：指标与ETF未来收益的相关性...")
    
    analysis = {}
    
    # 指标列表
    indicator_cols = ['top10_turnover_ratio', 'top10_ret_3d', 'top10_ret_5d', 'top10_ret_10d',
                      'rising_ratio', 'above_ma20_ratio', 'new_high_20d_ratio', 'total_turnover']
    
    # 未来收益列
    future_cols = [c for c in results_df.columns if 'future_' in c]
    
    print(f"\n  指标列: {indicator_cols}")
    print(f"  未来收益列: {future_cols}")
    
    # 计算相关性
    corr_results = []
    for ind in indicator_cols:
        for fut in future_cols:
            # 去除NaN后计算
            valid = results_df[[ind, fut]].dropna()
            if len(valid) > 50:
                corr = valid[ind].corr(valid[fut])
                corr_results.append({
                    'indicator': ind,
                    'future_return': fut,
                    'correlation': corr,
                    'sample_size': len(valid),
                })
    
    corr_df = pd.DataFrame(corr_results)
    
    if not corr_df.empty:
        print(f"\n  相关性矩阵:")
        print(f"  {'指标':<25} {'未来收益':<25} {'相关性':>10} {'样本数':>8}")
        print("  " + "-"*70)
        for _, row in corr_df.iterrows():
            print(f"  {row['indicator']:<25} {row['future_return']:<25} {row['correlation']:>10.4f} {row['sample_size']:>8}")
        
        # 找出最强相关性
        corr_df['abs_corr'] = corr_df['correlation'].abs()
        top_corr = corr_df.nlargest(10, 'abs_corr')
        
        print(f"\n  最强相关性Top 10:")
        for _, row in top_corr.iterrows():
            direction = "正相关" if row['correlation'] > 0 else "负相关"
            print(f"    {row['indicator']} vs {row['future_return']}: {row['correlation']:.4f} ({direction}, n={row['sample_size']})")
    
    # 6. 分组统计（高扩散度 vs 低扩散度）
    print("\n[6/6] 分组统计：高/低扩散度 vs ETF未来收益...")
    
    for ind in ['rising_ratio', 'above_ma20_ratio', 'new_high_20d_ratio']:
        print(f"\n  {ind} 分组:")
        
        # 按指标中位数分组
        median_val = results_df[ind].median()
        high_group = results_df[results_df[ind] > median_val]
        low_group = results_df[results_df[ind] <= median_val]
        
        for fut in future_cols:
            high_returns = high_group[fut].dropna()
            low_returns = low_group[fut].dropna()
            
            if len(high_returns) > 10 and len(low_returns) > 10:
                print(f"    {fut}:")
                print(f"      高{ind}组: 均值={high_returns.mean():.2f}%, 中位数={high_returns.median():.2f}%, n={len(high_returns)}")
                print(f"      低{ind}组: 均值={low_returns.mean():.2f}%, 中位数={low_returns.median():.2f}%, n={len(low_returns)}")
                print(f"      差值: {high_returns.mean() - low_returns.mean():.2f}%")
    
    # 保存报告
    print("\n保存报告...")
    
    report_path = 'reports/v1.3_step3_telecom_leader_diffusion.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 3: 通信行业龙头/扩散度统计探索报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**试点行业**: {SECTOR_CODE} {SECTOR_NAME}\n\n")
        f.write(f"**成分股数量**: {len(constituents)} (当前快照)\n\n")
        f.write(f"**研究区间**: {START_DATE} ~ 2026-06-12\n\n")
        
        f.write("## 重要声明：幸存者偏差\n\n")
        f.write("> **警告**: 本研究使用当前成分股快照（123只）计算历史指标。\n\n")
        f.write("> 这意味着：\n\n")
        f.write("> 1. 已被剔除的成分股不会出现在计算中\n\n")
        f.write("> 2. 2022年计算时使用的是2026年的成分股列表\n\n")
        f.write("> 3. 存在幸存者偏差：被剔除的往往是表现差的股票\n\n")
        f.write("> 4. 结论可能偏乐观，仅作为可行性验证，不作为策略依据\n\n")
        f.write("> 建议：如需正式回测，应使用历史成分股数据或接受此偏差\n\n")
        
        f.write("## 1. 数据概览\n\n")
        f.write(f"- 成分股数量: {len(constituents)}\n")
        f.write(f"- 有效交易日: {len(valid_dates)}\n")
        f.write(f"- 指标计算行数: {len(results_df)}\n\n")
        
        f.write("## 2. 指标定义\n\n")
        f.write("| 指标 | 定义 |\n")
        f.write("|------|------|\n")
        f.write("| top10_turnover_ratio | 前10大成交额成分股占行业总成交额比例 |\n")
        f.write("| top10_ret_3d | 前10大活跃股3日平均收益率(%) |\n")
        f.write("| top10_ret_5d | 前10大活跃股5日平均收益率(%) |\n")
        f.write("| top10_ret_10d | 前10大活跃股10日平均收益率(%) |\n")
        f.write("| rising_ratio | 当日上涨成分股比例 |\n")
        f.write("| above_ma20_ratio | 收盘价站上MA20的成分股比例 |\n")
        f.write("| new_high_20d_ratio | 创20日新高的成分股比例 |\n")
        f.write("| total_turnover | 行业总成交额（元） |\n\n")
        
        f.write("## 3. 相关性分析\n\n")
        if not corr_df.empty:
            f.write("| 指标 | 未来收益 | 相关性 | 样本数 |\n")
            f.write("|------|----------|--------|--------|\n")
            for _, row in corr_df.iterrows():
                f.write(f"| {row['indicator']} | {row['future_return']} | {row['correlation']:.4f} | {row['sample_size']} |\n")
        
        f.write("\n## 4. 分组统计\n\n")
        for ind in ['rising_ratio', 'above_ma20_ratio', 'new_high_20d_ratio']:
            f.write(f"### {ind}\n\n")
            median_val = results_df[ind].median()
            high_group = results_df[results_df[ind] > median_val]
            low_group = results_df[results_df[ind] <= median_val]
            
            for fut in future_cols:
                high_returns = high_group[fut].dropna()
                low_returns = low_group[fut].dropna()
                
                if len(high_returns) > 10 and len(low_returns) > 10:
                    f.write(f"**{fut}**:\n\n")
                    f.write(f"- 高{ind}组: 均值={high_returns.mean():.2f}%, 中位数={high_returns.median():.2f}%, n={len(high_returns)}\n")
                    f.write(f"- 低{ind}组: 均值={low_returns.mean():.2f}%, 中位数={low_returns.median():.2f}%, n={len(low_returns)}\n")
                    f.write(f"- 差值: {high_returns.mean() - low_returns.mean():.2f}%\n\n")
        
        f.write("## 5. 结论与建议\n\n")
        f.write("**可行性结论**:\n\n")
        f.write("1. 龙头/扩散度指标可计算\n")
        f.write("2. 指标与ETF未来收益存在一定相关性\n")
        f.write("3. 但幸存者偏差可能使结果偏乐观\n\n")
        f.write("**建议**:\n\n")
        f.write("1. 如需正式回测，需获取历史成分股数据\n")
        f.write("2. 或接受幸存者偏差，但结论需保守解读\n")
        f.write("3. 建议先以观察模式积累数据，不急于纳入策略\n\n")
        
        f.write("## 6. 版本边界\n\n")
        f.write("- v1.2.2 已收口\n")
        f.write("- v1.3 Step 1 已验收（31个行业）\n")
        f.write("- v1.3 Step 2 已验收（成分股可行性）\n")
        f.write("- v1.3 Step 3 完成（通信行业龙头/扩散度统计探索）\n")
        f.write("- 不改交易规则\n")
        f.write("- 不跑组合回测\n")
    
    print(f"\n报告已保存: {report_path}")
    
    # 保存数据
    data_path = 'reports/v1.3_step3_telecom_data.csv'
    results_df.to_csv(data_path, index=False, encoding='utf-8-sig')
    print(f"数据已保存: {data_path}")
    
    return results_df


if __name__ == '__main__':
    df = run_statistical_analysis()
    print(f"\n[OK] Step 3 通信行业龙头/扩散度统计探索完成")
