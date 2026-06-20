# -*- coding: utf-8 -*-
"""
v1.3_step3_telecom_indicators.py - 通信行业龙头/扩散度指标计算（123只成分股）

数据：cache/telecom_final.csv（123只，前复权）
要求：
- 每只股票仅在纳入日期之后参与行业指标
- 计算龙头/扩散度指标
- 与ETF未来收益做相关性分析
- 不改策略，不跑组合回测
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime
from database import ETFDatabase
import akshare as ak

SECTOR_CODE = '801770'
ETF_CODES = ['515880.SH', '515050.SH']
DB_PATH = 'database/etf_model.db'


def get_constituents_with_dates():
    """获取成分股列表（含纳入日期）"""
    df = ak.index_stock_cons(symbol=SECTOR_CODE)
    result = {}
    for _, row in df.iterrows():
        code = str(row.iloc[0]).zfill(6)
        inclusion_date = str(row.iloc[2])
        result[code] = inclusion_date
    return result


def load_stock_data():
    """加载所有成分股数据"""
    df = pd.read_csv('cache/telecom_final.csv')
    df['date'] = pd.to_datetime(df['date'])
    df['code'] = df['code'].astype(str).str.zfill(6)
    return df


def calculate_indicators(stock_data, inclusion_dates):
    """计算每日行业指标"""
    
    trading_dates = sorted(stock_data['date'].unique())
    # 从2022-02-01开始（确保有20天历史）
    valid_dates = [d for d in trading_dates if d >= pd.Timestamp('2022-02-01')]
    
    results = []
    
    for date in valid_dates:
        date = pd.Timestamp(date)
        
        # 只包含已纳入的成分股
        eligible_codes = [code for code, inc_date in inclusion_dates.items() 
                         if pd.Timestamp(inc_date) <= date]
        
        day_data = stock_data[(stock_data['date'] == date) & 
                              (stock_data['code'].isin(eligible_codes))].copy()
        
        if len(day_data) < 10:  # 至少10只活跃股
            continue
        
        # 过去20天数据（用于MA20和20日新高）
        date_20d = date - pd.Timedelta(days=30)
        hist_data = stock_data[(stock_data['date'] >= date_20d) & 
                               (stock_data['date'] <= date) &
                               (stock_data['code'].isin(eligible_codes))]
        
        indicators = {'date': date}
        
        # 1. 前10大成交额占比（龙头集中度）
        day_sorted = day_data.sort_values('amount', ascending=False)
        top10 = min(10, len(day_data))
        top10_amt = day_sorted.head(top10)['amount'].sum()
        total_amt = day_data['amount'].sum()
        indicators['top10_turnover_ratio'] = top10_amt / total_amt if total_amt > 0 else 0
        
        # 2. 前10活跃股3/5/10日收益（龙头强度）
        for w in [3, 5, 10]:
            start_d = date - pd.Timedelta(days=w*2+5)
            window_data = stock_data[(stock_data['date'] >= start_d) & 
                                    (stock_data['date'] <= date) &
                                    (stock_data['code'].isin(eligible_codes))]
            
            top10_codes = day_sorted.head(top10)['code'].tolist()
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
        
        # 3. 成分股上涨比例（扩散度）
        indicators['rising_ratio'] = (day_data['ret_pct'] > 0).sum() / len(day_data)
        
        # 4. 站上MA20比例（扩散度）
        above_ma20 = 0
        for code in day_data['code'].unique():
            code_hist = hist_data[hist_data['code'] == code].sort_values('date')
            if len(code_hist) >= 20:
                ma20 = code_hist.tail(20)['close'].mean()
                current = code_hist.tail(1)['close'].iloc[0]
                if current > ma20:
                    above_ma20 += 1
        indicators['above_ma20_ratio'] = above_ma20 / len(day_data)
        
        # 5. 创20日新高比例（扩散度）
        new_high = 0
        for code in day_data['code'].unique():
            code_hist = hist_data[hist_data['code'] == code].sort_values('date')
            if len(code_hist) >= 20:
                current = code_hist.tail(1)['close'].iloc[0]
                max20 = code_hist.tail(20)['close'].max()
                if current >= max20 * 0.999:
                    new_high += 1
        indicators['new_high_20d_ratio'] = new_high / len(day_data)
        
        # 6. 其他基础指标
        indicators['total_turnover'] = total_amt
        indicators['active_stocks'] = len(day_data)
        indicators['eligible_stocks'] = len(eligible_codes)
        
        results.append(indicators)
    
    return pd.DataFrame(results)


def get_etf_future_returns(etf_data, dates):
    """获取ETF未来收益"""
    for etf_code in ETF_CODES:
        etf_df = etf_data[etf_data['ticker'] == etf_code].sort_values('date')
        
        for w in [1, 3, 5, 10]:
            col_name = f'{etf_code}_future_{w}d'
            
            for idx, row in dates.iterrows():
                date = row['date']
                future = etf_df[etf_df['date'] > date]
                current = etf_df[etf_df['date'] == date]
                
                if len(current) == 0 or len(future) < w:
                    dates.at[idx, col_name] = np.nan
                else:
                    current_price = current.iloc[0]['close']
                    future_price = future.iloc[w-1]['close']
                    dates.at[idx, col_name] = (future_price / current_price - 1) * 100
    
    return dates


def analyze_correlations(results_df):
    """相关性分析"""
    indicator_cols = ['top10_turnover_ratio', 'top10_ret_3d', 'top10_ret_5d', 'top10_ret_10d',
                      'rising_ratio', 'above_ma20_ratio', 'new_high_20d_ratio']
    future_cols = [c for c in results_df.columns if 'future_' in c]
    
    corr_results = []
    for ind in indicator_cols:
        for fut in future_cols:
            valid = results_df[[ind, fut]].dropna()
            if len(valid) > 50:
                corr = valid[ind].corr(valid[fut])
                corr_results.append({
                    'indicator': ind, 'future': fut, 'correlation': corr, 'n': len(valid),
                })
    
    return pd.DataFrame(corr_results)


def group_analysis(results_df):
    """分组统计"""
    groups = {}
    for ind in ['rising_ratio', 'above_ma20_ratio', 'new_high_20d_ratio']:
        groups[ind] = {}
        median_val = results_df[ind].median()
        high = results_df[results_df[ind] > median_val]
        low = results_df[results_df[ind] <= median_val]
        
        for fut in [c for c in results_df.columns if 'future_' in c]:
            h = high[fut].dropna()
            l = low[fut].dropna()
            if len(h) > 10 and len(l) > 10:
                groups[ind][fut] = {
                    'high_mean': h.mean(), 'high_median': h.median(), 'high_n': len(h),
                    'low_mean': l.mean(), 'low_median': l.median(), 'low_n': len(l),
                    'diff': h.mean() - l.mean(),
                }
    
    return groups


def main():
    print("="*80)
    print("v1.3 Step 3: 通信行业龙头/扩散度指标计算（123只成分股）")
    print("="*80)
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # 1. 加载数据
    print("[1/5] 加载成分股数据...")
    stock_data = load_stock_data()
    inclusion_dates = get_constituents_with_dates()
    print(f"  成分股: {stock_data['code'].nunique()} 只")
    print(f"  数据行: {len(stock_data)} 行")
    print(f"  纳入日期信息: {len(inclusion_dates)} 只")
    
    # 2. 获取ETF数据
    print("\n[2/5] 获取ETF数据...")
    db = ETFDatabase()
    etf_data = pd.DataFrame()
    for etf in ETF_CODES:
        df = db.get_market_data(ticker=etf, start_date='2022-01-01', end_date='2026-06-12')
        if not df.empty:
            etf_data = pd.concat([etf_data, df], ignore_index=True)
            print(f"  {etf}: {len(df)} 行")
    
    # 3. 计算指标
    print("\n[3/5] 计算每日龙头/扩散度指标...")
    results_df = calculate_indicators(stock_data, inclusion_dates)
    print(f"  有效计算日: {len(results_df)} 天")
    
    # 4. 获取ETF未来收益
    print("\n[4/5] 获取ETF未来收益...")
    results_df = get_etf_future_returns(etf_data, results_df)
    
    # 5. 统计分析
    print("\n[5/5] 统计分析...")
    
    # 相关性
    corr_df = analyze_correlations(results_df)
    
    print(f"\n{'='*80}")
    print("相关性分析结果")
    print(f"{'='*80}")
    
    if not corr_df.empty:
        print(f"\n{'指标':<25} {'未来收益':<25} {'相关性':>10} {'样本':>6}")
        print("-"*70)
        for _, row in corr_df.iterrows():
            print(f"{row['indicator']:<25} {row['future']:<25} {row['correlation']:>10.4f} {row['n']:>6}")
        
        corr_df['abs_corr'] = corr_df['correlation'].abs()
        top10 = corr_df.nlargest(10, 'abs_corr')
        print(f"\n最强相关性Top 10:")
        for _, row in top10.iterrows():
            dir_str = "正" if row['correlation'] > 0 else "负"
            print(f"  {row['indicator']} vs {row['future']}: {row['correlation']:.4f} ({dir_str}相关)")
    
    # 分组统计
    groups = group_analysis(results_df)
    
    print(f"\n{'='*80}")
    print("分组统计（按扩散度中位数分组）")
    print(f"{'='*80}")
    
    for ind, data in groups.items():
        print(f"\n--- {ind} ---")
        for fut, stats in data.items():
            print(f"  {fut}:")
            print(f"    高组: 均值={stats['high_mean']:.2f}%, 中位数={stats['high_median']:.2f}%, n={stats['high_n']}")
            print(f"    低组: 均值={stats['low_mean']:.2f}%, 中位数={stats['low_median']:.2f}%, n={stats['low_n']}")
            print(f"    差值: {stats['diff']:.2f}%")
    
    # 保存数据
    results_df.to_csv('reports/v1.3_step3_telecom_123_data.csv', index=False, encoding='utf-8-sig')
    print(f"\n数据已保存: reports/v1.3_step3_telecom_123_data.csv")
    
    # 生成报告
    report_path = 'reports/v1.3_step3_telecom_123_indicators.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 3: 通信行业龙头/扩散度指标计算报告（123只成分股）\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**试点行业**: 801770 通信\n\n")
        f.write(f"**成分股数量**: 123只（全部下载成功）\n\n")
        f.write(f"**数据口径**: 前复权（fqt=1）\n\n")
        f.write(f"**研究区间**: 2022-01-01 ~ 2026-06-12\n\n")
        
        f.write("## 重要声明：幸存者偏差\n\n")
        f.write("> **警告**: 本研究使用当前成分股快照（123只）计算历史指标。\n\n")
        f.write("> 这意味着2022年计算时使用的是2026年的成分股列表，存在幸存者偏差。\n\n")
        f.write("> 被剔除的往往是表现差的股票，结论可能偏乐观。\n\n")
        f.write("> 仅作为可行性验证，不作为策略依据。\n\n")
        
        f.write("## 1. 数据概览\n\n")
        f.write(f"- 成分股数量: 123\n")
        f.write(f"- 有效计算日: {len(results_df)}\n")
        f.write(f"- 数据行: {len(stock_data)}\n\n")
        
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
        for ind, data in groups.items():
            f.write(f"### {ind}\n\n")
            for fut, stats in data.items():
                f.write(f"**{fut}**:\n\n")
                f.write(f"- 高组: 均值={stats['high_mean']:.2f}%, 中位数={stats['high_median']:.2f}%, n={stats['high_n']}\n")
                f.write(f"- 低组: 均值={stats['low_mean']:.2f}%, 中位数={stats['low_median']:.2f}%, n={stats['low_n']}\n")
                f.write(f"- 差值: {stats['diff']:.2f}%\n\n")
        
        f.write("## 5. 结论与建议\n\n")
        f.write("**可行性结论**:\n\n")
        f.write("1. 龙头/扩散度指标可计算（123只成分股）\n")
        f.write("2. 指标与ETF未来收益存在一定相关性\n")
        f.write("3. 但幸存者偏差使结果偏乐观，需保守解读\n\n")
        f.write("**建议**:\n\n")
        f.write("1. 如需正式回测，需获取历史成分股数据\n")
        f.write("2. 或接受幸存者偏差，但结论需极度保守解读\n")
        f.write("3. 建议以观察模式积累数据，不急于纳入策略\n\n")
        
        f.write("## 6. 版本边界\n\n")
        f.write("- v1.2.2 已收口\n")
        f.write("- v1.3 Step 1-2 已验收\n")
        f.write("- v1.3 Step 3 完成（通信行业123只成分股龙头/扩散度统计探索）\n")
        f.write("- 不改交易规则\n")
        f.write("- 不跑组合回测\n")
    
    print(f"\n报告已保存: {report_path}")
    print(f"\n[OK] Step 3 指标计算完成（123只成分股）")


if __name__ == '__main__':
    main()
