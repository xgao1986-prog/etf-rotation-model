# -*- coding: utf-8 -*-
"""
lifecycle_audit.py - B0-18 回测生命周期审计脚本

分析维度：
1. 选对了吗：买入后5/10/20日相对强弱（vs 当天所有BUY候选）
2. 买对了吗：买入后1/3/5/10/20日路径
3. 拿住了吗：持仓期最大浮盈、最终收益、盈利捕获率、最大回撤、峰值日距
4. 卖对了吗：卖出后1/3/5/10/20日收益率

拆分统计：年份、ETF ticker、退出原因、市场状态
输出：D:/etf_rotation_model/reports/lifecycle_audit.md
"""

import sys
import os
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from config import (
    ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, CORE_UNIVERSE,
    STRATEGY_CONFIG, build_config, BACKTEST_CONFIG
)
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine


# ============== 辅助函数 ==============

def generate_signals_df(market_df, bench_df, cfg):
    """生成完整的signals_df，与回测引擎一致"""
    strategy = StrategyEngine(cfg)
    scores_list = []
    for ticker in market_df['ticker'].unique():
        t_df = market_df[market_df['ticker'] == ticker].copy()
        if len(t_df) < 50:
            continue
        if ticker in list(DEFENSE_UNIVERSE.keys()):
            scored = strategy.calculate_defense_score(t_df)
        else:
            scored = strategy.calculate_total_score(t_df)
        scores_list.append(scored)
    scores_df = pd.concat(scores_list, ignore_index=True)
    scores_df = strategy.rank_all_momentum(scores_df)
    scores_df = strategy.compute_total_score(scores_df)
    signals_df = strategy.generate_signals(scores_df, bench_df)
    return signals_df


def classify_exit_reason(reason_str):
    """规范化退出原因"""
    if pd.isna(reason_str):
        return '其他'
    r = str(reason_str)
    if '止损' in r or 'STOP' in r.upper() or 'stop' in r.lower():
        return '止损'
    if '减仓' in r:
        return '大盘择时减仓'
    if '腾仓位' in r or '防御' in r:
        return '防御/腾仓位'
    if '跌出' in r or '调出' in r or '候选' in r:
        return '调出候选列表'
    return '其他'


def classify_market_regime(row, bench_df):
    """按沪深300表现简单分类：涨/跌/震荡"""
    # 使用nav_df中的bench_price，或从bench_df计算20日收益
    date = row['exit_date'] if 'exit_date' in row else row['entry_date']
    if pd.isna(date):
        return '未知'
    
    # 获取该日期前后20日基准数据
    bench_slice = bench_df[bench_df['date'] <= date].tail(21)
    if len(bench_slice) < 21:
        return '未知'
    
    ret_20 = (bench_slice['close'].iloc[-1] / bench_slice['close'].iloc[0]) - 1
    if ret_20 > 0.05:
        return '上涨'
    elif ret_20 < -0.05:
        return '下跌'
    else:
        return '震荡'


def compute_future_returns(ticker, start_date, market_df, days_list=[1, 3, 5, 10, 20]):
    """计算某标的从start_date起后续N个交易日的收益率（使用close）"""
    t_df = market_df[market_df['ticker'] == ticker].sort_values('date').reset_index(drop=True)
    t_df['date'] = pd.to_datetime(t_df['date'])
    
    # 找到start_date当天的数据（或之后第一个交易日）
    idx = t_df[t_df['date'] >= pd.to_datetime(start_date)].index
    if len(idx) == 0:
        return {d: np.nan for d in days_list}
    
    start_idx = idx[0]
    start_price = t_df.loc[start_idx, 'close']
    if pd.isna(start_price) or start_price == 0:
        return {d: np.nan for d in days_list}
    
    result = {}
    for d in days_list:
        target_idx = start_idx + d
        if target_idx < len(t_df):
            target_price = t_df.loc[target_idx, 'close']
            if pd.notna(target_price) and target_price > 0:
                result[d] = (target_price / start_price) - 1
            else:
                result[d] = np.nan
        else:
            result[d] = np.nan
    return result


def compute_path_from_open(ticker, start_date, market_df, days_list=[1, 3, 5, 10, 20]):
    """计算从买入当天开盘价到后续N日close的路径"""
    t_df = market_df[market_df['ticker'] == ticker].sort_values('date').reset_index(drop=True)
    t_df['date'] = pd.to_datetime(t_df['date'])
    
    idx = t_df[t_df['date'] >= pd.to_datetime(start_date)].index
    if len(idx) == 0:
        return {d: np.nan for d in days_list}
    
    start_idx = idx[0]
    start_price = t_df.loc[start_idx, 'open']
    if pd.isna(start_price) or start_price == 0:
        # 回退到close
        start_price = t_df.loc[start_idx, 'close']
    if pd.isna(start_price) or start_price == 0:
        return {d: np.nan for d in days_list}
    
    result = {}
    for d in days_list:
        target_idx = start_idx + d
        if target_idx < len(t_df):
            target_price = t_df.loc[target_idx, 'close']
            if pd.notna(target_price) and target_price > 0:
                result[d] = (target_price / start_price) - 1
            else:
                result[d] = np.nan
        else:
            result[d] = np.nan
    return result


def compute_holding_stats(ticker, entry_date, exit_date, market_df):
    """计算持仓期间统计：最大浮盈、最终收益、最大回撤、峰值日距"""
    t_df = market_df[market_df['ticker'] == ticker].sort_values('date').reset_index(drop=True)
    t_df['date'] = pd.to_datetime(t_df['date'])
    
    entry = pd.to_datetime(entry_date)
    exit_dt = pd.to_datetime(exit_date)
    
    mask = (t_df['date'] >= entry) & (t_df['date'] <= exit_dt)
    hold_df = t_df[mask].copy()
    
    if hold_df.empty or len(hold_df) < 2:
        return {
            'max_float_pnl': np.nan, 'final_return': np.nan,
            'capture_ratio': np.nan, 'max_dd': np.nan, 'peak_day_dist': np.nan,
            'hold_days': len(hold_df)
        }
    
    # 以买入当天收盘价为起点（注意：回测买入用open，但浮盈按close算）
    # 用第一天收盘价作为成本基准（与回测一致：pnl = (current_price - cost) / cost，cost=open）
    cost = hold_df['open'].iloc[0]
    if pd.isna(cost) or cost == 0:
        cost = hold_df['close'].iloc[0]
    
    hold_df['float_pnl'] = (hold_df['close'] - cost) / cost
    hold_df['cummax'] = hold_df['float_pnl'].cummax()
    hold_df['drawdown'] = hold_df['float_pnl'] - hold_df['cummax']
    
    max_float_pnl = hold_df['float_pnl'].max()
    final_return = hold_df['float_pnl'].iloc[-1]
    capture_ratio = final_return / max_float_pnl if max_float_pnl > 0 else np.nan
    max_dd = hold_df['drawdown'].min()
    
    peak_idx = hold_df['float_pnl'].idxmax()
    peak_day_dist = hold_df.index.get_loc(peak_idx)  # 距买入日的天数
    
    return {
        'max_float_pnl': max_float_pnl,
        'final_return': final_return,
        'capture_ratio': capture_ratio,
        'max_dd': max_dd,
        'peak_day_dist': peak_day_dist,
        'hold_days': len(hold_df),
    }


def format_pct(v, decimals=2):
    if pd.isna(v):
        return 'N/A'
    return f"{v:.{decimals}%}"


def format_num(v, decimals=2):
    if pd.isna(v):
        return 'N/A'
    return f"{v:.{decimals}f}"


def safe_agg(series, agg='mean'):
    s = series.dropna()
    if len(s) == 0:
        return np.nan
    if agg == 'mean':
        return s.mean()
    elif agg == 'median':
        return s.median()
    elif agg == 'count':
        return len(s)
    return np.nan


# ============== 主程序 ==============

def main():
    print("=" * 60)
    print("B0-18 生命周期审计")
    print("=" * 60)
    
    # 1. 加载数据
    db = ETFDatabase()
    b0_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=b0_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    print(f"Market data: {market_df.shape[0]} rows, {market_df['ticker'].nunique()} tickers")
    print(f"Bench data: {bench_df.shape[0]} rows")
    
    # 2. 生成完整信号（用于"选对了吗"分析）
    print("\n[1/5] 生成信号数据...")
    signals_df = generate_signals_df(market_df, bench_df, STRATEGY_CONFIG)
    
    # 3. 运行回测
    print("[2/5] 运行回测...")
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    
    if 'error' in result:
        print(f"回测失败: {result['error']}")
        return
    
    trades_df = result['trades_df'].copy()
    trades_df['date'] = pd.to_datetime(trades_df['date'])
    nav_df = result['nav_df'].copy()
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    
    print(f"  总交易: {len(trades_df)}, 配对: {len(trades_df[trades_df['action']=='BUY'])} BUY")
    
    # 4. 交易配对
    print("[3/5] 交易配对...")
    pairs = []
    for ticker in trades_df['ticker'].unique():
        t_trades = trades_df[trades_df['ticker'] == ticker].sort_values('date').reset_index(drop=True)
        pending_buy = None
        for _, row in t_trades.iterrows():
            if row['action'] == 'BUY':
                pending_buy = row.to_dict()
            elif row['action'] in ('SELL', 'STOP_LOSS'):
                if pending_buy is not None:
                    pairs.append({
                        'entry_date': pending_buy['date'],
                        'exit_date': row['date'],
                        'ticker': ticker,
                        'entry_price': pending_buy['price'],
                        'exit_price': row['price'],
                        'shares': pending_buy['shares'],
                        'entry_amount': pending_buy['amount'],
                        'exit_amount': row['amount'],
                        'entry_commission': pending_buy['commission'],
                        'exit_commission': row['commission'],
                        'pnl_pct': row['pnl_pct'],
                        'exit_reason_raw': row['reason'],
                        'exit_action': row['action'],
                    })
                    pending_buy = None
    
    pairs_df = pd.DataFrame(pairs)
    print(f"  成功配对: {len(pairs_df)} 笔")
    
    # 5. 逐笔生命周期分析
    print("[4/5] 逐笔生命周期分析...")
    
    analysis_list = []
    
    for idx, row in pairs_df.iterrows():
        ticker = row['ticker']
        entry_date = row['entry_date']
        exit_date = row['exit_date']
        
        # --- 选对了吗：相对强弱 ---
        # 买入当天所有BUY信号候选
        day_signals = signals_df[signals_df['date'] == entry_date]
        day_buys = day_signals[day_signals['signal_type'] == 'BUY']
        
        candidate_returns_5 = []
        candidate_returns_10 = []
        candidate_returns_20 = []
        
        for _, cand in day_buys.iterrows():
            cand_ticker = cand['ticker']
            rets = compute_future_returns(cand_ticker, entry_date, market_df, [5, 10, 20])
            candidate_returns_5.append(rets.get(5, np.nan))
            candidate_returns_10.append(rets.get(10, np.nan))
            candidate_returns_20.append(rets.get(20, np.nan))
        
        my_ret_5 = compute_future_returns(ticker, entry_date, market_df, [5]).get(5, np.nan)
        my_ret_10 = compute_future_returns(ticker, entry_date, market_df, [10]).get(10, np.nan)
        my_ret_20 = compute_future_returns(ticker, entry_date, market_df, [20]).get(20, np.nan)
        
        # 计算排名（百分位，越高越好）
        def rank_pct(my_val, vals):
            if pd.isna(my_val) or len([v for v in vals if pd.notna(v)]) == 0:
                return np.nan
            clean = [v for v in vals if pd.notna(v)]
            clean.append(my_val)
            # 百分位 = 排名 / 总数
            sorted_vals = sorted(clean, reverse=True)
            rank = sorted_vals.index(my_val) + 1  # 1-based
            return rank / len(sorted_vals)
        
        rank_5 = rank_pct(my_ret_5, candidate_returns_5)
        rank_10 = rank_pct(my_ret_10, candidate_returns_10)
        rank_20 = rank_pct(my_ret_20, candidate_returns_20)
        
        # --- 买对了吗：路径 ---
        path = compute_path_from_open(ticker, entry_date, market_df, [1, 3, 5, 10, 20])
        
        # --- 拿住了吗：持仓统计 ---
        hold_stats = compute_holding_stats(ticker, entry_date, exit_date, market_df)
        
        # --- 卖对了吗：卖出后收益 ---
        sell_rets = compute_future_returns(ticker, exit_date, market_df, [1, 3, 5, 10, 20])
        
        # --- 元数据 ---
        exit_reason = classify_exit_reason(row['exit_reason_raw'])
        market_state = classify_market_regime({'entry_date': entry_date, 'exit_date': exit_date}, bench_df)
        
        analysis_list.append({
            'entry_date': entry_date,
            'exit_date': exit_date,
            'ticker': ticker,
            'entry_year': entry_date.year,
            'exit_reason': exit_reason,
            'market_state': market_state,
            'pnl_pct': row['pnl_pct'],
            
            # 选对了吗
            'my_ret_5': my_ret_5,
            'my_ret_10': my_ret_10,
            'my_ret_20': my_ret_20,
            'rank_pct_5': rank_5,
            'rank_pct_10': rank_10,
            'rank_pct_20': rank_20,
            'num_candidates': len(day_buys),
            
            # 买对了吗
            'path_1d': path.get(1, np.nan),
            'path_3d': path.get(3, np.nan),
            'path_5d': path.get(5, np.nan),
            'path_10d': path.get(10, np.nan),
            'path_20d': path.get(20, np.nan),
            
            # 拿住了吗
            'hold_days': hold_stats['hold_days'],
            'max_float_pnl': hold_stats['max_float_pnl'],
            'final_return': hold_stats['final_return'],
            'capture_ratio': hold_stats['capture_ratio'],
            'max_dd': hold_stats['max_dd'],
            'peak_day_dist': hold_stats['peak_day_dist'],
            
            # 卖对了吗
            'sell_ret_1d': sell_rets.get(1, np.nan),
            'sell_ret_3d': sell_rets.get(3, np.nan),
            'sell_ret_5d': sell_rets.get(5, np.nan),
            'sell_ret_10d': sell_rets.get(10, np.nan),
            'sell_ret_20d': sell_rets.get(20, np.nan),
        })
        
        if (idx + 1) % 50 == 0:
            print(f"  已处理 {idx + 1}/{len(pairs_df)} 笔...")
    
    analysis_df = pd.DataFrame(analysis_list)
    print(f"  分析完成: {len(analysis_df)} 笔")
    
    # 6. 生成报告
    print("[5/5] 生成报告...")
    os.makedirs('D:/etf_rotation_model/reports', exist_ok=True)
    
    lines = []
    lines.append("# B0-18 回测生命周期审计报告")
    lines.append("")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"回测区间: {nav_df['date'].min().strftime('%Y-%m-%d')} ~ {nav_df['date'].max().strftime('%Y-%m-%d')}")
    lines.append(f"总交易配对: {len(analysis_df)} 笔")
    lines.append(f"总收益率: {result['total_return']:.2%}")
    lines.append(f"年化收益率: {result['annual_return']:.2%}")
    lines.append(f"最大回撤: {result['max_drawdown']:.2%}")
    lines.append("")
    
    # 汇总统计
    lines.append("## 一、整体汇总")
    lines.append("")
    lines.append("| 维度 | 指标 | 均值 | 中位数 | 样本数 |")
    lines.append("|------|------|------|--------|--------|")
    
    # 选对了吗
    lines.append(f"| 选对了吗 | 5日排名百分位 | {format_num(safe_agg(analysis_df['rank_pct_5']))} | {format_num(safe_agg(analysis_df['rank_pct_5'], 'median'))} | {int(safe_agg(analysis_df['rank_pct_5'], 'count'))} |")
    lines.append(f"| 选对了吗 | 10日排名百分位 | {format_num(safe_agg(analysis_df['rank_pct_10']))} | {format_num(safe_agg(analysis_df['rank_pct_10'], 'median'))} | {int(safe_agg(analysis_df['rank_pct_10'], 'count'))} |")
    lines.append(f"| 选对了吗 | 20日排名百分位 | {format_num(safe_agg(analysis_df['rank_pct_20']))} | {format_num(safe_agg(analysis_df['rank_pct_20'], 'median'))} | {int(safe_agg(analysis_df['rank_pct_20'], 'count'))} |")
    
    # 买对了吗
    lines.append(f"| 买对了吗 | 1日路径 | {format_pct(safe_agg(analysis_df['path_1d']))} | {format_pct(safe_agg(analysis_df['path_1d'], 'median'))} | {int(safe_agg(analysis_df['path_1d'], 'count'))} |")
    lines.append(f"| 买对了吗 | 5日路径 | {format_pct(safe_agg(analysis_df['path_5d']))} | {format_pct(safe_agg(analysis_df['path_5d'], 'median'))} | {int(safe_agg(analysis_df['path_5d'], 'count'))} |")
    lines.append(f"| 买对了吗 | 20日路径 | {format_pct(safe_agg(analysis_df['path_20d']))} | {format_pct(safe_agg(analysis_df['path_20d'], 'median'))} | {int(safe_agg(analysis_df['path_20d'], 'count'))} |")
    
    # 拿住了吗
    lines.append(f"| 拿住了吗 | 最大浮盈 | {format_pct(safe_agg(analysis_df['max_float_pnl']))} | {format_pct(safe_agg(analysis_df['max_float_pnl'], 'median'))} | {int(safe_agg(analysis_df['max_float_pnl'], 'count'))} |")
    lines.append(f"| 拿住了吗 | 最终收益 | {format_pct(safe_agg(analysis_df['final_return']))} | {format_pct(safe_agg(analysis_df['final_return'], 'median'))} | {int(safe_agg(analysis_df['final_return'], 'count'))} |")
    lines.append(f"| 拿住了吗 | 盈利捕获率 | {format_num(safe_agg(analysis_df['capture_ratio']))} | {format_num(safe_agg(analysis_df['capture_ratio'], 'median'))} | {int(safe_agg(analysis_df['capture_ratio'], 'count'))} |")
    lines.append(f"| 拿住了吗 | 持仓最大回撤 | {format_pct(safe_agg(analysis_df['max_dd']))} | {format_pct(safe_agg(analysis_df['max_dd'], 'median'))} | {int(safe_agg(analysis_df['max_dd'], 'count'))} |")
    lines.append(f"| 拿住了吗 | 峰值日距(天) | {format_num(safe_agg(analysis_df['peak_day_dist']))} | {format_num(safe_agg(analysis_df['peak_day_dist'], 'median'))} | {int(safe_agg(analysis_df['peak_day_dist'], 'count'))} |")
    lines.append(f"| 拿住了吗 | 持仓天数 | {format_num(safe_agg(analysis_df['hold_days']))} | {format_num(safe_agg(analysis_df['hold_days'], 'median'))} | {int(safe_agg(analysis_df['hold_days'], 'count'))} |")
    
    # 卖对了吗
    lines.append(f"| 卖对了吗 | 卖出后1日 | {format_pct(safe_agg(analysis_df['sell_ret_1d']))} | {format_pct(safe_agg(analysis_df['sell_ret_1d'], 'median'))} | {int(safe_agg(analysis_df['sell_ret_1d'], 'count'))} |")
    lines.append(f"| 卖对了吗 | 卖出后5日 | {format_pct(safe_agg(analysis_df['sell_ret_5d']))} | {format_pct(safe_agg(analysis_df['sell_ret_5d'], 'median'))} | {int(safe_agg(analysis_df['sell_ret_5d'], 'count'))} |")
    lines.append(f"| 卖对了吗 | 卖出后20日 | {format_pct(safe_agg(analysis_df['sell_ret_20d']))} | {format_pct(safe_agg(analysis_df['sell_ret_20d'], 'median'))} | {int(safe_agg(analysis_df['sell_ret_20d'], 'count'))} |")
    lines.append("")
    
    # 拆分统计函数
    def make_split_table(df, split_col, title):
        lines = []
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 分组 | 样本数 | 胜率 | 平均收益 | 中位数收益 | 平均持仓天数 | 平均最大浮盈 | 平均捕获率 | 平均峰值日距 |")
        lines.append("|------|--------|------|----------|------------|--------------|--------------|------------|--------------|")
        
        for val in sorted(df[split_col].unique(), key=lambda x: (str(x) if x != '未知' else 'zzz')):
            sub = df[df[split_col] == val]
            if len(sub) == 0:
                continue
            win_rate = (sub['pnl_pct'] > 0).mean()
            lines.append(f"| {val} | {len(sub)} | {win_rate:.1%} | {format_pct(sub['pnl_pct'].mean())} | {format_pct(sub['pnl_pct'].median())} | {format_num(sub['hold_days'].mean())} | {format_pct(sub['max_float_pnl'].mean())} | {format_num(sub['capture_ratio'].mean())} | {format_num(sub['peak_day_dist'].mean())} |")
        
        lines.append("")
        return lines
    
    # 按年份拆分
    lines.extend(make_split_table(analysis_df, 'entry_year', '二、按年份拆分'))
    
    # 按ETF拆分
    lines.extend(make_split_table(analysis_df, 'ticker', '三、按ETF拆分'))
    
    # 按退出原因拆分
    lines.extend(make_split_table(analysis_df, 'exit_reason', '四、按退出原因拆分'))
    
    # 按市场状态拆分
    lines.extend(make_split_table(analysis_df, 'market_state', '五、按市场状态拆分'))
    
    # 详细诊断：每个维度的深入分析
    lines.append("## 六、深度诊断")
    lines.append("")
    
    # 选对了吗诊断
    lines.append("### 6.1 选对了吗 — 相对强弱诊断")
    lines.append("")
    lines.append("**定义**: 该笔买入后N日收益率，在买入当天所有发出BUY信号的候选ETF中的排名百分位（1=最强，0=最弱）。")
    lines.append("")
    lines.append("| 分类 | 5日排名 | 10日排名 | 20日排名 | 样本数 |")
    lines.append("|------|---------|----------|----------|--------|")
    
    def classify_selection(rank):
        if pd.isna(rank):
            return '未知'
        if rank >= 0.75:
            return '优秀(前25%)'
        elif rank >= 0.5:
            return '良好(前50%)'
        elif rank >= 0.25:
            return '一般(前75%)'
        else:
            return '落后(后25%)'
    
    analysis_df['sel_class_5'] = analysis_df['rank_pct_5'].apply(classify_selection)
    analysis_df['sel_class_10'] = analysis_df['rank_pct_10'].apply(classify_selection)
    analysis_df['sel_class_20'] = analysis_df['rank_pct_20'].apply(classify_selection)
    
    for cls in ['优秀(前25%)', '良好(前50%)', '一般(前75%)', '落后(后25%)', '未知']:
        sub = analysis_df[analysis_df['sel_class_5'] == cls]
        if len(sub) > 0:
            lines.append(f"| {cls} | {format_num(sub['rank_pct_5'].mean())} | {format_num(sub['rank_pct_10'].mean())} | {format_num(sub['rank_pct_20'].mean())} | {len(sub)} |")
    
    lines.append("")
    
    # 买对了吗诊断
    lines.append("### 6.2 买对了吗 — 入场时机诊断")
    lines.append("")
    lines.append("**定义**: 从买入当天开盘价到后续N日收盘价的路径收益。正值=买入后上涨，负值=买入后下跌（被套）。")
    lines.append("")
    lines.append("| 分类 | 1日 | 3日 | 5日 | 10日 | 20日 | 样本数 |")
    lines.append("|------|-----|-----|-----|------|------|--------|")
    
    def classify_timing(path_1d, path_5d):
        if pd.isna(path_1d) or pd.isna(path_5d):
            return '未知'
        if path_1d < -0.03 and path_5d < -0.05:
            return '追高被套'
        elif path_1d < 0 and path_5d < 0:
            return '短期回调'
        elif path_1d > 0 and path_5d > 0:
            return '顺势买入'
        else:
            return '震荡入场'
    
    analysis_df['timing_class'] = analysis_df.apply(lambda r: classify_timing(r['path_1d'], r['path_5d']), axis=1)
    
    for cls in ['顺势买入', '震荡入场', '短期回调', '追高被套', '未知']:
        sub = analysis_df[analysis_df['timing_class'] == cls]
        if len(sub) > 0:
            lines.append(f"| {cls} | {format_pct(sub['path_1d'].mean())} | {format_pct(sub['path_3d'].mean())} | {format_pct(sub['path_5d'].mean())} | {format_pct(sub['path_10d'].mean())} | {format_pct(sub['path_20d'].mean())} | {len(sub)} |")
    
    lines.append("")
    
    # 拿住了吗诊断
    lines.append("### 6.3 拿住了吗 — 持仓效率诊断")
    lines.append("")
    lines.append("**定义**: 盈利捕获率 = 最终收益 / 最大浮盈。越接近1，说明越能拿住；接近0或负数，说明见顶后大幅回吐或止损。")
    lines.append("")
    lines.append("| 分类 | 平均最大浮盈 | 平均最终收益 | 平均捕获率 | 平均回撤 | 平均峰值日距 | 样本数 |")
    lines.append("|------|--------------|--------------|------------|----------|--------------|--------|")
    
    def classify_holding(capture, max_float, final):
        if pd.isna(capture):
            return '未知'
        if capture >= 0.8:
            return '优秀(捕获>80%)'
        elif capture >= 0.5:
            return '良好(捕获50-80%)'
        elif capture >= 0:
            return '一般(捕获0-50%)'
        elif final < 0:
            return '亏损离场'
        else:
            return '其他'
    
    analysis_df['hold_class'] = analysis_df.apply(lambda r: classify_holding(r['capture_ratio'], r['max_float_pnl'], r['final_return']), axis=1)
    
    for cls in ['优秀(捕获>80%)', '良好(捕获50-80%)', '一般(捕获0-50%)', '亏损离场', '其他', '未知']:
        sub = analysis_df[analysis_df['hold_class'] == cls]
        if len(sub) > 0:
            lines.append(f"| {cls} | {format_pct(sub['max_float_pnl'].mean())} | {format_pct(sub['final_return'].mean())} | {format_num(sub['capture_ratio'].mean())} | {format_pct(sub['max_dd'].mean())} | {format_num(sub['peak_day_dist'].mean())} | {len(sub)} |")
    
    lines.append("")
    
    # 卖对了吗诊断
    lines.append("### 6.4 卖对了吗 — 退出时机诊断")
    lines.append("")
    lines.append("**定义**: 卖出后N日收益率。正值=卖早了（后续还涨）；负值=卖对了（后续下跌）或卖晚了（后续继续跌）。")
    lines.append("")
    lines.append("| 分类 | 卖出后1日 | 卖出后5日 | 卖出后20日 | 样本数 |")
    lines.append("|------|-----------|-----------|------------|--------|")
    
    def classify_selling(ret_1d, ret_5d):
        if pd.isna(ret_1d) or pd.isna(ret_5d):
            return '未知'
        if ret_1d > 0.02 and ret_5d > 0.05:
            return '卖早了(后续大涨)'
        elif ret_1d < -0.02 and ret_5d < -0.05:
            return '卖对了(后续大跌)'
        elif abs(ret_1d) < 0.02 and abs(ret_5d) < 0.05:
            return '卖在拐点'
        else:
            return '趋势跟随'
    
    analysis_df['sell_class'] = analysis_df.apply(lambda r: classify_selling(r['sell_ret_1d'], r['sell_ret_5d']), axis=1)
    
    for cls in ['卖早了(后续大涨)', '卖对了(后续大跌)', '卖在拐点', '趋势跟随', '未知']:
        sub = analysis_df[analysis_df['sell_class'] == cls]
        if len(sub) > 0:
            lines.append(f"| {cls} | {format_pct(sub['sell_ret_1d'].mean())} | {format_pct(sub['sell_ret_5d'].mean())} | {format_pct(sub['sell_ret_20d'].mean())} | {len(sub)} |")
    
    lines.append("")
    
    # 每笔交易明细表（只放关键字段，太多会太大）
    lines.append("## 七、交易明细摘要")
    lines.append("")
    lines.append("| 买入日 | 卖出日 | ETF | 退出原因 | 收益 | 选对5日 | 买对5日 | 捕获率 | 卖对5日 | 市场 |")
    lines.append("|--------|--------|-----|----------|------|---------|---------|--------|---------|------|")
    
    for _, r in analysis_df.iterrows():
        lines.append(
            f"| {r['entry_date'].strftime('%Y-%m-%d')} | {r['exit_date'].strftime('%Y-%m-%d')} | "
            f"{r['ticker']} | {r['exit_reason']} | {format_pct(r['pnl_pct'])} | "
            f"{format_num(r['rank_pct_5'])} | {format_pct(r['path_5d'])} | "
            f"{format_num(r['capture_ratio'])} | {format_pct(r['sell_ret_5d'])} | {r['market_state']} |"
        )
    
    lines.append("")
    lines.append("---")
    lines.append("报告生成完毕。")
    
    # 写入文件
    report_path = 'D:/etf_rotation_model/reports/lifecycle_audit.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n报告已保存: {report_path}")
    print(f"总行数: {len(lines)}")
    print("=" * 60)
    print("完成!")


if __name__ == '__main__':
    main()
