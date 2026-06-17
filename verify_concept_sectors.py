# -*- coding: utf-8 -*-
"""
verify_concept_sectors.py - 快速验证概念板块数据可用性
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime
import time
import warnings
warnings.filterwarnings('ignore')

from data_fetcher import DataFetcher
from database import ETFDatabase

CONCEPT_CANDIDATES = {
    'robot': {
        'etf': '159530.SZ',
        'etf_name': '机器人ETF',
        'sector_match': '801890',
        'ths_names': ['机器人', '人形机器人']
    },
    'pv': {
        'etf': '516160.SH',
        'etf_name': '新能源ETF',
        'sector_match': '801730',
        'ths_names': ['光伏', '新能源']
    },
    'semiconductor': {
        'etf': '512480.SH',
        'etf_name': '半导体ETF',
        'sector_match': '801080',
        'ths_names': ['半导体', '芯片']
    },
    'ai': {
        'etf': '515230.SH',
        'etf_name': '软件ETF',
        'sector_match': '801750',
        'ths_names': ['人工智能', 'AI']
    },
}


def get_ths_concept_list():
    try:
        import akshare as ak
        df = ak.stock_board_concept_name_ths()
        print(f"同花顺概念板块总数: {len(df)}")
        print(f"列名: {list(df.columns)}")
        return df
    except Exception as e:
        print(f"获取概念板块列表失败: {e}")
        return None


def search_concepts(df, keywords):
    if df is None:
        return None
    name_col = '概念名称' if '概念名称' in df.columns else 'name'
    matches = []
    for kw in keywords:
        found = df[df[name_col].str.contains(kw, na=False, regex=False)]
        if len(found) > 0:
            matches.append(found)
    if matches:
        return pd.concat(matches).drop_duplicates()
    return None


def fetch_concept_history_em(symbol, period='daily', start_date='20240101', end_date='20260612'):
    """使用东财接口获取概念板块历史数据"""
    try:
        import akshare as ak
        df = ak.stock_board_concept_hist_em(symbol=symbol, period=period, 
                                             start_date=start_date, end_date=end_date, adjust='')
        if not df.empty:
            df['date'] = pd.to_datetime(df['日期'])
            df['close'] = df['收盘'].astype(float)
            df['ticker'] = f'CONCEPT_{symbol}'
            return df[['ticker', 'date', 'close']]
    except Exception as e:
        print(f"  EM接口失败: {e}")
    return pd.DataFrame()


def verify_concept_sectors():
    print("="*80)
    print("概念板块数据快速验证")
    print("="*80)
    
    print("\n[1/4] 获取同花顺概念板块列表...")
    concept_df = get_ths_concept_list()
    if concept_df is None:
        print("无法获取概念板块列表，验证终止")
        return
    
    print("\n[2/4] 搜索相关概念板块...")
    db = ETFDatabase()
    results = {}
    
    for concept_key, config in CONCEPT_CANDIDATES.items():
        print(f"\n--- {concept_key} ({config['etf_name']}) ---")
        
        matches = search_concepts(concept_df, config['ths_names'])
        if matches is not None and len(matches) > 0:
            print(f"  找到 {len(matches)} 个匹配概念:")
            name_col = '概念名称' if '概念名称' in matches.columns else 'name'
            code_col = '代码' if '代码' in matches.columns else 'code'
            for _, row in matches.head(5).iterrows():
                print(f"    {row[name_col]} (代码: {row.get(code_col, 'N/A')})")
            
            # 尝试获取第一个概念的数据（使用东财接口）
            first_concept_name = matches.iloc[0][name_col]
            first_concept_code = matches.iloc[0].get(code_col, '')
            print(f"  尝试获取 '{first_concept_name}' 历史数据...")
            
            # 先用东财接口
            concept_hist = fetch_concept_history_em(first_concept_name, start_date='20240101')
            if not concept_hist.empty:
                print(f"  [OK] EM接口成功: {len(concept_hist)} 行, {concept_hist['date'].min().date()} ~ {concept_hist['date'].max().date()}")
            else:
                print(f"  [FAIL] EM接口也失败")
                results[concept_key] = {'error': 'fetch failed'}
                continue
            
            # 获取ETF数据对比
            etf_df = db.get_market_data(ticker=config['etf'], start_date='2024-01-01', end_date='2026-06-12')
            if not etf_df.empty:
                merged = pd.merge(concept_hist, etf_df[['date', 'close']], on='date', suffixes=('_concept', '_etf'))
                if len(merged) > 50:
                    corr = merged['close_concept'].corr(merged['close_etf'])
                    merged['concept_ret'] = merged['close_concept'].pct_change()
                    merged['etf_ret'] = merged['close_etf'].pct_change()
                    same_direction = (merged['concept_ret'] * merged['etf_ret'] > 0).sum()
                    total = (~merged['concept_ret'].isna() & ~merged['etf_ret'].isna()).sum()
                    direction_ratio = same_direction / total if total > 0 else 0
                    
                    print(f"  与ETF对比: {len(merged)} 个重叠交易日")
                    print(f"  价格相关性: {corr:.4f}")
                    print(f"  方向一致性: {direction_ratio:.1%}")
                    
                    results[concept_key] = {
                        'concept_name': first_concept_name,
                        'data_rows': len(concept_hist),
                        'overlap': len(merged),
                        'correlation': corr,
                        'direction_ratio': direction_ratio,
                    }
                else:
                    print(f"  [WARN] 重叠交易日不足: {len(merged)}")
                    results[concept_key] = {'error': 'overlap too small', 'overlap': len(merged)}
            else:
                print(f"  [WARN] ETF数据不可用")
        else:
            print(f"  [FAIL] 未找到匹配概念")
            results[concept_key] = {'error': 'no match'}
    
    print("\n" + "="*80)
    print("[3/4] 验证结果汇总")
    print("="*80)
    
    print(f"\n{'概念':<12} {'概念名称':<16} {'数据行数':>8} {'重叠日':>8} {'价格相关性':>12} {'方向一致性':>12}")
    print("-"*80)
    
    for concept_key, result in results.items():
        if 'error' in result:
            print(f"{concept_key:<12} {result.get('error', 'unknown'):<16} {'N/A':>8} {'N/A':>8} {'N/A':>12} {'N/A':>12}")
        else:
            print(f"{concept_key:<12} {result['concept_name']:<16} {result['data_rows']:>8} {result['overlap']:>8} {result['correlation']:>12.4f} {result['direction_ratio']:>11.1%}")
    
    print("\n" + "="*80)
    print("[4/4] 概念板块 vs 申万行业对比")
    print("="*80)
    
    for concept_key, config in CONCEPT_CANDIDATES.items():
        print(f"\n{concept_key} ({config['etf_name']}):")
        
        sector_df = db.get_market_data(ticker=f"SECTOR_{config['sector_match']}", start_date='2024-01-01', end_date='2026-06-12')
        etf_df = db.get_market_data(ticker=config['etf'], start_date='2024-01-01', end_date='2026-06-12')
        
        if not sector_df.empty and not etf_df.empty:
            merged = pd.merge(sector_df[['date', 'close']], etf_df[['date', 'close']], on='date', suffixes=('_sector', '_etf'))
            if len(merged) > 50:
                corr = merged['close_sector'].corr(merged['close_etf'])
                merged['sector_ret'] = merged['close_sector'].pct_change()
                merged['etf_ret'] = merged['close_etf'].pct_change()
                same_dir = (merged['sector_ret'] * merged['etf_ret'] > 0).sum()
                total = (~merged['sector_ret'].isna() & ~merged['etf_ret'].isna()).sum()
                dir_ratio = same_dir / total if total > 0 else 0
                
                print(f"  申万行业({config['sector_match']}): 相关性={corr:.4f}, 方向一致性={dir_ratio:.1%}")
                
                if concept_key in results and 'correlation' in results[concept_key]:
                    c_corr = results[concept_key]['correlation']
                    c_dir = results[concept_key]['direction_ratio']
                    print(f"  概念板块: 相关性={c_corr:.4f}, 方向一致性={c_dir:.1%}")
                    
                    if c_corr > corr:
                        print(f"  -> 概念板块相关性更高 (+{(c_corr-corr):.4f})")
                    else:
                        print(f"  -> 申万行业相关性更高 (+{(corr-c_corr):.4f})")
    
    print("\n" + "="*80)
    print("结论:")
    print("="*80)
    print("- 概念板块数据获取: 东财接口可用，但同花顺概念名称可能变化")
    print("- 如果概念板块相关性显著高于行业 -> 建议纳入")
    print("- 如果差异不大 -> 继续使用申万行业（数据更稳定）")
    print("- 注意：概念板块调整频繁，历史数据可能不连续")


if __name__ == '__main__':
    verify_concept_sectors()
