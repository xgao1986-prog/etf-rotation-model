# -*- coding: utf-8 -*-
"""
sector_volume_lead_analysis.py - 板块成交额/成交量领先性验证

验证问题：板块量能指标是否能预测对应ETF的未来收益？

测试指标：
1. 板块成交额 5日均值 / 20日均值（放量比率）
2. 板块成交额当日分位数（过去120日百分位）
3. 板块涨幅 + 成交额放大（量价共振）
4. 板块成交额排名变化（vs前20日平均排名）
5. 板块成交额放大后，ETF未来1/3/5/10日收益

输出：
- 每个板块-ETF配对的相关性/胜率
- 有量能放大 vs 无量能放大的ETF后续收益对比
- 哪些板块的量能指标有先导价值
- 如果没有显著效果，明确说明

重要：这一步只做验证，不改策略。
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import warnings
warnings.filterwarnings('ignore')

from database import ETFDatabase
from config import SECTOR_INDEX_UNIVERSE, ETF_TO_SECTOR_MAPPING, CORE_UNIVERSE

# 量能指标定义
VOLUME_LOOKBACK = 120  # 分位数回望期
SHORT_MA = 5           # 短均线
LONG_MA = 20           # 长均线
RANK_LOOKBACK = 20     # 排名变化回望期

# 未来收益持有期
HOLD_PERIODS = [1, 3, 5, 10]

# 放量阈值
VOLUME_RATIO_THRESHOLD = 1.5  # 5日/20日 > 1.5视为放量
VOLUME_PERCENTILE_THRESHOLD = 80  # 120日百分位 > 80视为放量
VOLUME_RANK_CHANGE_THRESHOLD = 5  # 排名上升超过5位视为排名上升


def load_sector_data(db, sector_code):
    """加载板块数据"""
    ticker = f"SECTOR_{sector_code.split('.')[0]}"
    df = db.get_market_data(ticker=ticker)
    if df.empty:
        return None
    df = df.sort_values('date').reset_index(drop=True)
    df['date'] = pd.to_datetime(df['date'])
    return df


def load_etf_data(db, etf_code):
    """加载ETF数据"""
    df = db.get_market_data(ticker=etf_code)
    if df.empty:
        return None
    df = df.sort_values('date').reset_index(drop=True)
    df['date'] = pd.to_datetime(df['date'])
    # 计算未来收益
    for p in HOLD_PERIODS:
        df[f'future_return_{p}d'] = df['close'].shift(-p) / df['close'] - 1
    return df


def calculate_volume_indicators(df):
    """计算板块量能指标"""
    df = df.copy()
    
    # 1. 成交额5日/20日均值比率
    df['amount_ma5'] = df['amount'].rolling(SHORT_MA).mean()
    df['amount_ma20'] = df['amount'].rolling(LONG_MA).mean()
    df['amount_ratio_5_20'] = df['amount_ma5'] / df['amount_ma20']
    
    # 2. 成交额120日百分位
    df['amount_pct_120'] = df['amount'].rolling(VOLUME_LOOKBACK).apply(
        lambda x: (x.iloc[-1] - x.min()) / (x.max() - x.min()) * 100 if x.max() != x.min() else 50,
        raw=False
    )
    
    # 更准确的百分位计算
    def rolling_percentile(x):
        if len(x) < 2 or np.all(x == x[0]):
            return 50.0
        return (np.searchsorted(np.sort(x), x[-1]) / len(x)) * 100
    
    df['amount_pct_120'] = df['amount'].rolling(VOLUME_LOOKBACK).apply(rolling_percentile, raw=True)
    
    # 3. 板块涨幅
    df['sector_return_1d'] = df['close'].pct_change()
    df['sector_return_5d'] = df['close'] / df['close'].shift(5) - 1
    
    # 4. 量价共振：涨幅>0且放量
    df['volume_surge'] = df['amount_ratio_5_20'] > VOLUME_RATIO_THRESHOLD
    df['price_up'] = df['sector_return_1d'] > 0
    df['volume_surge_strong'] = df['amount_ratio_5_20'] > 2.0
    df['volume_surge_mild'] = df['amount_ratio_5_20'] > 1.2
    
    # 量价共振信号
    df['volume_price_resonance'] = df['volume_surge'] & df['price_up']
    df['volume_price_resonance_strong'] = df['volume_surge_strong'] & df['price_up']
    
    # 5. 成交额排名变化（在当日所有板块中的排名）
    # 这个需要全局计算，在配对分析中处理
    
    return df


def calculate_cross_sectional_rank(df_all, date_col='date', value_col='amount', rank_col='amount_rank', window=RANK_LOOKBACK):
    """计算所有板块在某日成交额排名，以及排名变化"""
    # 按日期分组，计算每日排名
    daily_amount = df_all.groupby('date')[value_col].sum().reset_index()
    daily_amount.columns = ['date', 'total_amount']
    
    # 对每个板块，计算其成交额在所有板块中的排名
    rank_data = []
    for date, group in df_all.groupby('date'):
        group = group.copy()
        group['amount_rank'] = group[value_col].rank(ascending=False, method='min')
        rank_data.append(group[['date', 'ticker', 'amount_rank']])
    
    rank_df = pd.concat(rank_data, ignore_index=True)
    
    # 计算排名变化（vs前20日平均排名）
    rank_df['amount_rank_ma20'] = rank_df.groupby('ticker')['amount_rank'].transform(
        lambda x: x.rolling(window, min_periods=1).mean()
    )
    rank_df['rank_change'] = rank_df['amount_rank_ma20'] - rank_df['amount_rank']  # 正值=排名上升
    
    return rank_df


def analyze_single_pair(sector_df, etf_df, sector_name, etf_name, sector_code, etf_code):
    """分析单个板块-ETF配对的量能领先性"""
    
    # 合并数据
    merged = pd.merge(
        sector_df[['date', 'amount_ratio_5_20', 'amount_pct_120', 'volume_surge', 
                   'volume_surge_strong', 'volume_surge_mild', 'volume_price_resonance',
                   'volume_price_resonance_strong', 'sector_return_1d', 'sector_return_5d',
                   'amount', 'volume']],
        etf_df[['date', 'close', 'future_return_1d', 'future_return_3d', 
                'future_return_5d', 'future_return_10d']],
        on='date', how='inner'
    )
    
    if len(merged) < 100:
        return None
    
    merged = merged.dropna()
    if len(merged) < 50:
        return None
    
    results = {
        'sector_code': sector_code.split('.')[0],
        'sector_name': sector_name,
        'etf_code': etf_code,
        'etf_name': etf_name,
        'overlap_days': len(merged),
        'indicators': {}
    }
    
    # ==================== 指标1: 5日/20日成交额比率 ====================
    indicator = 'amount_ratio_5_20'
    ind_results = {}
    
    # 量能放大 vs 未放大的未来收益对比
    for p in HOLD_PERIODS:
        col = f'future_return_{p}d'
        
        # 放量日
        surge_mask = merged['amount_ratio_5_20'] > VOLUME_RATIO_THRESHOLD
        surge_returns = merged.loc[surge_mask, col]
        
        # 未放量日
        no_surge_mask = merged['amount_ratio_5_20'] <= 1.0
        no_surge_returns = merged.loc[no_surge_mask, col]
        
        # 正常日（无显著放量或缩量）
        normal_mask = (merged['amount_ratio_5_20'] > 0.8) & (merged['amount_ratio_5_20'] <= 1.2)
        normal_returns = merged.loc[normal_mask, col]
        
        ind_results[f'surge_{p}d'] = {
            'count': int(surge_mask.sum()),
            'mean_return': float(surge_returns.mean()) if len(surge_returns) > 0 else None,
            'median_return': float(surge_returns.median()) if len(surge_returns) > 0 else None,
            'win_rate': float((surge_returns > 0).mean()) if len(surge_returns) > 0 else None,
        }
        ind_results[f'no_surge_{p}d'] = {
            'count': int(no_surge_mask.sum()),
            'mean_return': float(no_surge_returns.mean()) if len(no_surge_returns) > 0 else None,
            'median_return': float(no_surge_returns.median()) if len(no_surge_returns) > 0 else None,
            'win_rate': float((no_surge_returns > 0).mean()) if len(no_surge_returns) > 0 else None,
        }
        ind_results[f'normal_{p}d'] = {
            'count': int(normal_mask.sum()),
            'mean_return': float(normal_returns.mean()) if len(normal_returns) > 0 else None,
            'median_return': float(normal_returns.median()) if len(normal_returns) > 0 else None,
            'win_rate': float((normal_returns > 0).mean()) if len(normal_returns) > 0 else None,
        }
        
        # 相关性：量能比率 vs 未来收益
        corr = merged['amount_ratio_5_20'].corr(merged[col])
        ind_results[f'corr_{p}d'] = float(corr) if not pd.isna(corr) else None
        
        # 领先1日：板块T日量能 vs ETF T+1日收益
        corr_lead = merged['amount_ratio_5_20'].shift(1).corr(merged[col])
        ind_results[f'lead_corr_{p}d'] = float(corr_lead) if not pd.isna(corr_lead) else None
    
    results['indicators']['amount_ratio_5_20'] = ind_results
    
    # ==================== 指标2: 120日百分位 ====================
    indicator = 'amount_pct_120'
    ind_results = {}
    
    for p in HOLD_PERIODS:
        col = f'future_return_{p}d'
        
        # 高百分位（放量）
        high_pct_mask = merged['amount_pct_120'] > VOLUME_PERCENTILE_THRESHOLD
        high_pct_returns = merged.loc[high_pct_mask, col]
        
        # 低百分位（缩量）
        low_pct_mask = merged['amount_pct_120'] < 20
        low_pct_returns = merged.loc[low_pct_mask, col]
        
        # 中百分位
        mid_pct_mask = (merged['amount_pct_120'] >= 40) & (merged['amount_pct_120'] <= 60)
        mid_pct_returns = merged.loc[mid_pct_mask, col]
        
        ind_results[f'high_pct_{p}d'] = {
            'count': int(high_pct_mask.sum()),
            'mean_return': float(high_pct_returns.mean()) if len(high_pct_returns) > 0 else None,
            'win_rate': float((high_pct_returns > 0).mean()) if len(high_pct_returns) > 0 else None,
        }
        ind_results[f'low_pct_{p}d'] = {
            'count': int(low_pct_mask.sum()),
            'mean_return': float(low_pct_returns.mean()) if len(low_pct_returns) > 0 else None,
            'win_rate': float((low_pct_returns > 0).mean()) if len(low_pct_returns) > 0 else None,
        }
        ind_results[f'mid_pct_{p}d'] = {
            'count': int(mid_pct_mask.sum()),
            'mean_return': float(mid_pct_returns.mean()) if len(mid_pct_returns) > 0 else None,
            'win_rate': float((mid_pct_returns > 0).mean()) if len(mid_pct_returns) > 0 else None,
        }
        
        # 相关性
        corr = merged['amount_pct_120'].corr(merged[col])
        ind_results[f'corr_{p}d'] = float(corr) if not pd.isna(corr) else None
        
        corr_lead = merged['amount_pct_120'].shift(1).corr(merged[col])
        ind_results[f'lead_corr_{p}d'] = float(corr_lead) if not pd.isna(corr_lead) else None
    
    results['indicators']['amount_pct_120'] = ind_results
    
    # ==================== 指标3: 量价共振 ====================
    indicator = 'volume_price_resonance'
    ind_results = {}
    
    for p in HOLD_PERIODS:
        col = f'future_return_{p}d'
        
        # 量价共振日
        resonance_mask = merged['volume_price_resonance'] == True
        resonance_returns = merged.loc[resonance_mask, col]
        
        # 仅涨但未放量
        up_no_volume_mask = (merged['sector_return_1d'] > 0) & (merged['volume_surge'] == False)
        up_no_volume_returns = merged.loc[up_no_volume_mask, col]
        
        # 仅放量但下跌
        volume_down_mask = (merged['volume_surge'] == True) & (merged['sector_return_1d'] <= 0)
        volume_down_returns = merged.loc[volume_down_mask, col]
        
        # 无信号日
        no_signal_mask = (merged['sector_return_1d'] <= 0) & (merged['volume_surge'] == False)
        no_signal_returns = merged.loc[no_signal_mask, col]
        
        ind_results[f'resonance_{p}d'] = {
            'count': int(resonance_mask.sum()),
            'mean_return': float(resonance_returns.mean()) if len(resonance_returns) > 0 else None,
            'win_rate': float((resonance_returns > 0).mean()) if len(resonance_returns) > 0 else None,
        }
        ind_results[f'up_no_volume_{p}d'] = {
            'count': int(up_no_volume_mask.sum()),
            'mean_return': float(up_no_volume_returns.mean()) if len(up_no_volume_returns) > 0 else None,
            'win_rate': float((up_no_volume_returns > 0).mean()) if len(up_no_volume_returns) > 0 else None,
        }
        ind_results[f'volume_down_{p}d'] = {
            'count': int(volume_down_mask.sum()),
            'mean_return': float(volume_down_returns.mean()) if len(volume_down_returns) > 0 else None,
            'win_rate': float((volume_down_returns > 0).mean()) if len(volume_down_returns) > 0 else None,
        }
        ind_results[f'no_signal_{p}d'] = {
            'count': int(no_signal_mask.sum()),
            'mean_return': float(no_signal_returns.mean()) if len(no_signal_returns) > 0 else None,
            'win_rate': float((no_signal_returns > 0).mean()) if len(no_signal_returns) > 0 else None,
        }
    
    results['indicators']['volume_price_resonance'] = ind_results
    
    # ==================== 指标4: 强量价共振 ====================
    indicator = 'volume_price_resonance_strong'
    ind_results = {}
    
    for p in HOLD_PERIODS:
        col = f'future_return_{p}d'
        
        strong_mask = merged['volume_price_resonance_strong'] == True
        strong_returns = merged.loc[strong_mask, col]
        
        ind_results[f'strong_resonance_{p}d'] = {
            'count': int(strong_mask.sum()),
            'mean_return': float(strong_returns.mean()) if len(strong_returns) > 0 else None,
            'win_rate': float((strong_returns > 0).mean()) if len(strong_returns) > 0 else None,
        }
    
    results['indicators']['volume_price_resonance_strong'] = ind_results
    
    return results


def analyze_rank_change(all_sector_data, etf_data_map, sector_etf_map):
    """分析成交额排名变化的领先性（跨板块分析）"""
    
    # 合并所有板块数据
    all_sectors = pd.concat(all_sector_data, ignore_index=True)
    
    # 计算每日排名
    rank_df = calculate_cross_sectional_rank(all_sectors)
    
    results = []
    
    for sector_code, (sector_name, etfs) in sector_etf_map.items():
        sector_ticker = f"SECTOR_{sector_code.split('.')[0]}"
        sector_rank = rank_df[rank_df['ticker'] == sector_ticker].copy()
        
        if sector_rank.empty:
            continue
        
        for etf_code in etfs:
            if etf_code not in etf_data_map or etf_data_map[etf_code] is None:
                continue
            
            etf_df = etf_data_map[etf_code]
            
            merged = pd.merge(
                sector_rank[['date', 'rank_change']],
                etf_df[['date', 'future_return_1d', 'future_return_3d', 'future_return_5d', 'future_return_10d']],
                on='date', how='inner'
            ).dropna()
            
            if len(merged) < 50:
                continue
            
            pair_result = {
                'sector_code': sector_code.split('.')[0],
                'sector_name': sector_name,
                'etf_code': etf_code,
                'etf_name': CORE_UNIVERSE.get(etf_code, etf_code),
                'overlap_days': len(merged),
            }
            
            for p in HOLD_PERIODS:
                col = f'future_return_{p}d'
                
                # 排名上升日
                rank_up_mask = merged['rank_change'] > VOLUME_RANK_CHANGE_THRESHOLD
                rank_up_returns = merged.loc[rank_up_mask, col]
                
                # 排名下降日
                rank_down_mask = merged['rank_change'] < -VOLUME_RANK_CHANGE_THRESHOLD
                rank_down_returns = merged.loc[rank_down_mask, col]
                
                # 排名不变日
                rank_stable_mask = abs(merged['rank_change']) <= 2
                rank_stable_returns = merged.loc[rank_stable_mask, col]
                
                pair_result[f'rank_up_{p}d'] = {
                    'count': int(rank_up_mask.sum()),
                    'mean_return': float(rank_up_returns.mean()) if len(rank_up_returns) > 0 else None,
                    'win_rate': float((rank_up_returns > 0).mean()) if len(rank_up_returns) > 0 else None,
                }
                pair_result[f'rank_down_{p}d'] = {
                    'count': int(rank_down_mask.sum()),
                    'mean_return': float(rank_down_returns.mean()) if len(rank_down_returns) > 0 else None,
                    'win_rate': float((rank_down_returns > 0).mean()) if len(rank_down_returns) > 0 else None,
                }
                pair_result[f'rank_stable_{p}d'] = {
                    'count': int(rank_stable_mask.sum()),
                    'mean_return': float(rank_stable_returns.mean()) if len(rank_stable_returns) > 0 else None,
                    'win_rate': float((rank_stable_returns > 0).mean()) if len(rank_stable_returns) > 0 else None,
                }
                
                # 相关性
                corr = merged['rank_change'].corr(merged[col])
                pair_result[f'corr_{p}d'] = float(corr) if not pd.isna(corr) else None
                
                corr_lead = merged['rank_change'].shift(1).corr(merged[col])
                pair_result[f'lead_corr_{p}d'] = float(corr_lead) if not pd.isna(corr_lead) else None
            
            results.append(pair_result)
    
    return results


def print_summary(results_list):
    """打印汇总结果"""
    print("\n" + "="*80)
    print("板块量能领先性验证汇总")
    print("="*80)
    
    if not results_list:
        print("\n错误：没有有效结果")
        return
    
    # 1. 指标1: 5日/20日比率汇总
    print("\n【指标1】板块成交额 5日/20日均值比率")
    print(f"放量定义: ratio > {VOLUME_RATIO_THRESHOLD}")
    print(f"{'板块':<6} {'ETF':<12} {'ETF名':<10} {'重叠天数':>6} {'放量1d收益':>10} {'放量胜率':>8} {'未放量1d':>10} {'领先相关':>10}")
    print("-"*90)
    
    for r in results_list:
        ind = r['indicators']['amount_ratio_5_20']
        surge = ind.get('surge_1d', {})
        no_surge = ind.get('no_surge_1d', {})
        lead_corr = ind.get('lead_corr_1d', None)
        
        s_ret = f"{surge.get('mean_return', 0):+.3f}" if surge.get('mean_return') is not None else 'N/A'
        s_wr = f"{surge.get('win_rate', 0):.1%}" if surge.get('win_rate') is not None else 'N/A'
        n_ret = f"{no_surge.get('mean_return', 0):+.3f}" if no_surge.get('mean_return') is not None else 'N/A'
        lc = f"{lead_corr:+.3f}" if lead_corr is not None else 'N/A'
        
        print(f"{r['sector_code']:<6} {r['etf_code']:<12} {r['etf_name']:<10} {r['overlap_days']:>6} {s_ret:>10} {s_wr:>8} {n_ret:>10} {lc:>10}")
    
    # 2. 指标2: 120日百分位汇总
    print(f"\n【指标2】板块成交额 120日百分位")
    print(f"高放量定义: percentile > {VOLUME_PERCENTILE_THRESHOLD}")
    print(f"{'板块':<6} {'ETF':<12} {'ETF名':<10} {'高百分位1d':>10} {'高百分位胜率':>10} {'中百分位1d':>10} {'领先相关':>10}")
    print("-"*90)
    
    for r in results_list:
        ind = r['indicators']['amount_pct_120']
        high = ind.get('high_pct_1d', {})
        mid = ind.get('mid_pct_1d', {})
        lead_corr = ind.get('lead_corr_1d', None)
        
        h_ret = f"{high.get('mean_return', 0):+.3f}" if high.get('mean_return') is not None else 'N/A'
        h_wr = f"{high.get('win_rate', 0):.1%}" if high.get('win_rate') is not None else 'N/A'
        m_ret = f"{mid.get('mean_return', 0):+.3f}" if mid.get('mean_return') is not None else 'N/A'
        lc = f"{lead_corr:+.3f}" if lead_corr is not None else 'N/A'
        
        print(f"{r['sector_code']:<6} {r['etf_code']:<12} {r['etf_name']:<10} {h_ret:>10} {h_wr:>10} {m_ret:>10} {lc:>10}")
    
    # 3. 指标3: 量价共振汇总
    print(f"\n【指标3】量价共振（板块涨 + 成交额放大）")
    print(f"{'板块':<6} {'ETF':<12} {'ETF名':<10} {'共振1d':>10} {'共振胜率':>10} {'仅涨未放量':>10} {'仅涨胜率':>10} {'无信号':>10}")
    print("-"*100)
    
    for r in results_list:
        ind = r['indicators']['volume_price_resonance']
        res = ind.get('resonance_1d', {})
        up = ind.get('up_no_volume_1d', {})
        no_sig = ind.get('no_signal_1d', {})
        
        r_ret = f"{res.get('mean_return', 0):+.3f}" if res.get('mean_return') is not None else 'N/A'
        r_wr = f"{res.get('win_rate', 0):.1%}" if res.get('win_rate') is not None else 'N/A'
        u_ret = f"{up.get('mean_return', 0):+.3f}" if up.get('mean_return') is not None else 'N/A'
        u_wr = f"{up.get('win_rate', 0):.1%}" if up.get('win_rate') is not None else 'N/A'
        n_ret = f"{no_sig.get('mean_return', 0):+.3f}" if no_sig.get('mean_return') is not None else 'N/A'
        
        print(f"{r['sector_code']:<6} {r['etf_code']:<12} {r['etf_name']:<10} {r_ret:>10} {r_wr:>10} {u_ret:>10} {u_wr:>10} {n_ret:>10}")
    
    # 4. 统计摘要
    print(f"\n{'='*80}")
    print("统计摘要")
    print(f"{'='*80}")
    
    # 收集所有数据
    all_surge_1d = []
    all_no_surge_1d = []
    all_resonance_1d = []
    all_up_no_vol_1d = []
    all_lead_corr = []
    
    for r in results_list:
        ind1 = r['indicators']['amount_ratio_5_20']
        s = ind1.get('surge_1d', {}).get('mean_return')
        n = ind1.get('no_surge_1d', {}).get('mean_return')
        lc = ind1.get('lead_corr_1d')
        if s is not None: all_surge_1d.append(s)
        if n is not None: all_no_surge_1d.append(n)
        if lc is not None: all_lead_corr.append(lc)
        
        ind3 = r['indicators']['volume_price_resonance']
        res = ind3.get('resonance_1d', {}).get('mean_return')
        up = ind3.get('up_no_volume_1d', {}).get('mean_return')
        if res is not None: all_resonance_1d.append(res)
        if up is not None: all_up_no_vol_1d.append(up)
    
    if all_surge_1d:
        print(f"\n  【5日/20日放量】")
        print(f"    放量日平均1d收益: {np.mean(all_surge_1d):+.4f} (中位数: {np.median(all_surge_1d):+.4f})")
        print(f"    未放量日平均1d收益: {np.mean(all_no_surge_1d):+.4f} (中位数: {np.median(all_no_surge_1d):+.4f})")
        print(f"    放量 vs 未放量差异: {np.mean(all_surge_1d) - np.mean(all_no_surge_1d):+.4f}")
        print(f"    领先相关性(板块T量能 vs ETF T+1收益): {np.mean(all_lead_corr):+.4f} (中位数: {np.median(all_lead_corr):+.4f})")
    
    if all_resonance_1d:
        print(f"\n  【量价共振】")
        print(f"    共振日平均1d收益: {np.mean(all_resonance_1d):+.4f} (中位数: {np.median(all_resonance_1d):+.4f})")
        print(f"    仅涨未放量日平均1d收益: {np.mean(all_up_no_vol_1d):+.4f} (中位数: {np.median(all_up_no_vol_1d):+.4f})")
        print(f"    共振 vs 仅涨差异: {np.mean(all_resonance_1d) - np.mean(all_up_no_vol_1d):+.4f}")
    
    # 5. 分持有期汇总
    print(f"\n  【分持有期：放量 vs 未放量】")
    for p in HOLD_PERIODS:
        surge_list = []
        no_surge_list = []
        for r in results_list:
            ind = r['indicators']['amount_ratio_5_20']
            s = ind.get(f'surge_{p}d', {}).get('mean_return')
            n = ind.get(f'no_surge_{p}d', {}).get('mean_return')
            if s is not None: surge_list.append(s)
            if n is not None: no_surge_list.append(n)
        if surge_list and no_surge_list:
            diff = np.mean(surge_list) - np.mean(no_surge_list)
            print(f"    {p}日持有: 放量 {np.mean(surge_list):+.4f} vs 未放量 {np.mean(no_surge_list):+.4f}, 差异 {diff:+.4f}")


def print_rank_summary(rank_results):
    """打印排名变化汇总"""
    if not rank_results:
        return
    
    print(f"\n{'='*80}")
    print("【指标4】板块成交额排名变化（跨18板块）")
    print(f"{'='*80}")
    print(f"排名上升定义: 排名较前20日平均上升 > {VOLUME_RANK_CHANGE_THRESHOLD} 位")
    
    print(f"\n{'板块':<6} {'ETF':<12} {'ETF名':<10} {'重叠':>6} {'排名上升1d':>10} {'上升胜率':>10} {'排名下降1d':>10} {'领先相关':>10}")
    print("-"*100)
    
    for r in rank_results:
        up = r.get('rank_up_1d', {})
        down = r.get('rank_down_1d', {})
        lc = r.get('lead_corr_1d', None)
        
        u_ret = f"{up.get('mean_return', 0):+.3f}" if up.get('mean_return') is not None else 'N/A'
        u_wr = f"{up.get('win_rate', 0):.1%}" if up.get('win_rate') is not None else 'N/A'
        d_ret = f"{down.get('mean_return', 0):+.3f}" if down.get('mean_return') is not None else 'N/A'
        lc_str = f"{lc:+.3f}" if lc is not None else 'N/A'
        
        print(f"{r['sector_code']:<6} {r['etf_code']:<12} {r['etf_name']:<10} {r['overlap_days']:>6} {u_ret:>10} {u_wr:>10} {d_ret:>10} {lc_str:>10}")
    
    # 统计
    all_up = []
    all_down = []
    all_stable = []
    all_lead_corr = []
    
    for r in rank_results:
        u = r.get('rank_up_1d', {}).get('mean_return')
        d = r.get('rank_down_1d', {}).get('mean_return')
        s = r.get('rank_stable_1d', {}).get('mean_return')
        lc = r.get('lead_corr_1d')
        if u is not None: all_up.append(u)
        if d is not None: all_down.append(d)
        if s is not None: all_stable.append(s)
        if lc is not None: all_lead_corr.append(lc)
    
    if all_up:
        print(f"\n  排名上升日平均1d收益: {np.mean(all_up):+.4f} (中位数: {np.median(all_up):+.4f})")
        print(f"  排名下降日平均1d收益: {np.mean(all_down):+.4f} (中位数: {np.median(all_down):+.4f})")
        print(f"  排名稳定日平均1d收益: {np.mean(all_stable):+.4f} (中位数: {np.median(all_stable):+.4f})")
        print(f"  上升 vs 下降差异: {np.mean(all_up) - np.mean(all_down):+.4f}")
        print(f"  领先相关性: {np.mean(all_lead_corr):+.4f} (中位数: {np.median(all_lead_corr):+.4f})")


def main():
    print("="*80)
    print("板块成交额/成交量领先性验证")
    print("="*80)
    print(f"\n验证时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("验证问题: 板块量能指标是否能预测对应ETF的未来收益？")
    print("重要声明: 只做验证，不改策略\n")
    
    db = ETFDatabase()
    
    # 预加载所有板块数据
    print("加载板块数据...")
    all_sector_data = []
    sector_data_map = {}
    for sector_code in SECTOR_INDEX_UNIVERSE.keys():
        df = load_sector_data(db, sector_code)
        if df is not None and not df.empty:
            all_sector_data.append(df)
            sector_data_map[sector_code] = df
    
    print(f"  加载了 {len(sector_data_map)} 个板块数据")
    
    # 预加载所有ETF数据
    print("加载ETF数据...")
    etf_data_map = {}
    for sector_code, (name, etfs) in SECTOR_INDEX_UNIVERSE.items():
        for etf in etfs:
            if etf not in etf_data_map:
                df = load_etf_data(db, etf)
                if df is not None and not df.empty:
                    etf_data_map[etf] = df
    
    print(f"  加载了 {len(etf_data_map)} 只ETF数据")
    
    # 计算板块量能指标
    print("\n计算板块量能指标...")
    for sector_code, df in sector_data_map.items():
        sector_data_map[sector_code] = calculate_volume_indicators(df)
    
    # 分析每个板块-ETF配对
    print("\n分析板块-ETF配对...")
    results_list = []
    
    for sector_code, (sector_name, etfs) in SECTOR_INDEX_UNIVERSE.items():
        sector_df = sector_data_map.get(sector_code)
        if sector_df is None or sector_df.empty:
            continue
        
        for etf_code in etfs:
            etf_df = etf_data_map.get(etf_code)
            if etf_df is None or etf_df.empty:
                continue
            
            etf_name = CORE_UNIVERSE.get(etf_code, etf_code)
            result = analyze_single_pair(sector_df, etf_df, sector_name, etf_name, sector_code, etf_code)
            if result:
                results_list.append(result)
    
    print(f"  完成 {len(results_list)} 个有效配对分析")
    
    # 分析排名变化（需要所有板块数据）
    print("\n分析成交额排名变化...")
    rank_results = analyze_rank_change(all_sector_data, etf_data_map, SECTOR_INDEX_UNIVERSE)
    print(f"  完成 {len(rank_results)} 个排名变化分析")
    
    # 打印结果
    print_summary(results_list)
    print_rank_summary(rank_results)
    
    # 结论
    print(f"\n{'='*80}")
    print("结论")
    print(f"{'='*80}")
    
    # 计算综合效果
    all_lead_corr_ratio = []
    all_lead_corr_pct = []
    all_lead_corr_rank = []
    all_res_diff = []
    
    for r in results_list:
        lc = r['indicators']['amount_ratio_5_20'].get('lead_corr_1d')
        if lc is not None: all_lead_corr_ratio.append(lc)
        
        lc2 = r['indicators']['amount_pct_120'].get('lead_corr_1d')
        if lc2 is not None: all_lead_corr_pct.append(lc2)
        
        res = r['indicators']['volume_price_resonance'].get('resonance_1d', {}).get('mean_return')
        up = r['indicators']['volume_price_resonance'].get('up_no_volume_1d', {}).get('mean_return')
        if res is not None and up is not None:
            all_res_diff.append(res - up)
    
    for r in rank_results:
        lc = r.get('lead_corr_1d')
        if lc is not None: all_lead_corr_rank.append(lc)
    
    print(f"\n  1. 5日/20日放量比率的领先相关性: {np.mean(all_lead_corr_ratio):+.4f} (中位数: {np.median(all_lead_corr_ratio):+.4f})")
    print(f"     → {'有微弱领先性' if abs(np.mean(all_lead_corr_ratio)) > 0.01 else '无显著领先性'}")
    
    print(f"\n  2. 120日百分位的领先相关性: {np.mean(all_lead_corr_pct):+.4f} (中位数: {np.median(all_lead_corr_pct):+.4f})")
    print(f"     → {'有微弱领先性' if abs(np.mean(all_lead_corr_pct)) > 0.01 else '无显著领先性'}")
    
    print(f"\n  3. 排名变化的领先相关性: {np.mean(all_lead_corr_rank):+.4f} (中位数: {np.median(all_lead_corr_rank):+.4f})")
    print(f"     → {'有微弱领先性' if abs(np.mean(all_lead_corr_rank)) > 0.01 else '无显著领先性'}")
    
    if all_res_diff:
        print(f"\n  4. 量价共振 vs 仅涨未放量的收益差异: {np.mean(all_res_diff):+.4f}")
        print(f"     → {'量能放大有微弱增益' if np.mean(all_res_diff) > 0.001 else '无显著增益'}")
    
    # 保存结果
    report = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'pair_results': results_list,
        'rank_results': rank_results,
        'summary': {
            'lead_corr_ratio_mean': float(np.mean(all_lead_corr_ratio)) if all_lead_corr_ratio else None,
            'lead_corr_pct_mean': float(np.mean(all_lead_corr_pct)) if all_lead_corr_pct else None,
            'lead_corr_rank_mean': float(np.mean(all_lead_corr_rank)) if all_lead_corr_rank else None,
            'resonance_diff_mean': float(np.mean(all_res_diff)) if all_res_diff else None,
        }
    }
    
    with open('reports/sector_volume_lead_analysis.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    
    print(f"\n  详细报告已保存: reports/sector_volume_lead_analysis.json")
    print(f"\n[OK] 板块量能领先性验证完成！")


if __name__ == '__main__':
    main()
