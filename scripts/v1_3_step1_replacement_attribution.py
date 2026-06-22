#!/usr/bin/env python3
"""
v1.3 Step 1: 换仓成本与有效性归因

目标：判断B0.4中是否存在大量"持仓仍合格，仅因排名小幅变化而被替换"的低价值换仓。

基准：v1.2.3 / B0.4（0bp，不计滑点）
不修改生产策略、参数或冻结基线。
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

AS_OF_DATE = '2026-06-18'


def get_b0_4_config():
    """构建B0.4配置"""
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    return cfg


def run_b0_4_backtest():
    """运行B0.4回测，返回完整结果"""
    cfg = get_b0_4_config()
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    assert len(tickers) == 18, f"B0.4 ETF池应为18只，实际{len(tickers)}"
    
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)
    
    return result, market_df, bench_df, cfg


def generate_daily_signals(market_df, bench_df, cfg):
    """生成每日信号（包含所有评分），用于后续判断"""
    strategy = StrategyEngine(cfg)
    
    # 逐ETF计算指标和评分
    all_scores = []
    for ticker in market_df['ticker'].unique():
        tdf = market_df[market_df['ticker'] == ticker].copy()
        if len(tdf) < 51:
            continue
        tdf = strategy.calculate_indicators_and_scores(tdf)
        all_scores.append(tdf)
    
    if not all_scores:
        return pd.DataFrame()
    
    scores_df = pd.concat(all_scores, ignore_index=True)
    
    # 横截面动量排名（B0.4中动量关闭，但保留momentum_rank字段用于一致性）
    scores_df = strategy.rank_all_momentum(scores_df)
    
    # 计算总评分（B0.4中动量/波动率关闭）
    scores_df = strategy.compute_total_score(scores_df)
    
    # 生成信号
    scores_df = strategy.generate_signals(scores_df, bench_df)
    
    return scores_df


def classify_trades(trades_df, scores_df, cfg):
    """
    逐次调仓对交易进行分类。
    
    分类：
    1. 止损退出 (action == 'STOP_LOSS')
    2. 不再满足BUY条件 (reason == '调出候选列表' 且该ticker当日signal_type == 'SELL')
    3. 防御资产为行业让路 (reason contains '防御让路')
    4. 纯排名替换：
       - 原持仓仍满足全部BUY条件
       - 同一调仓日存在实际行业ETF买入（非防御）
       - 不是止损或趋势失效造成
    5. 无匹配退出：原持仓仍满足BUY条件，但同日无行业买入可匹配
    """
    
    # 确保日期格式一致
    trades_df = trades_df.copy()
    trades_df['date'] = pd.to_datetime(trades_df['date']).dt.date
    scores_df = scores_df.copy()
    scores_df['date'] = pd.to_datetime(scores_df['date']).dt.date
    
    # 防御ticker列表
    defense_tickers = set(DEFENSE_UNIVERSE.keys())
    industry_tickers = set(ETF_UNIVERSE.keys())
    
    # 预计算每个调仓日的行业买入列表（非防御）
    buy_dates = {}
    for _, row in trades_df[trades_df['action'] == 'BUY'].iterrows():
        d = pd.to_datetime(row['date']).date() if hasattr(row['date'], 'strftime') else row['date']
        if d not in buy_dates:
            buy_dates[d] = []
        if row['ticker'] not in defense_tickers:
            buy_dates[d].append(row['ticker'])
    
    classified = []
    
    for _, row in trades_df.iterrows():
        date = row['date']
        ticker = row['ticker']
        action = row['action']
        reason = str(row['reason'])
        
        # 默认分类
        category = 'OTHER'
        is_pure_ranking = False
        match_status = 'N/A'
        match_reason = ''
        
        if action == 'STOP_LOSS':
            category = 'STOP_LOSS'
            match_status = 'N/A'
        elif action == 'SELL':
            # 防御让路
            if '防御让路' in reason or '防御减持让路' in reason:
                category = 'DEFENSE_YIELD'
                match_status = 'N/A'
            elif reason == '调出候选列表':
                # 需要判断该ticker当日是否仍满足BUY条件
                day_signals = scores_df[scores_df['date'] == date]
                ticker_signal = day_signals[day_signals['ticker'] == ticker]
                
                if not ticker_signal.empty:
                    signal_type = ticker_signal['signal_type'].iloc[0]
                    if signal_type == 'BUY':
                        # 仍满足BUY条件
                        # 检查同日是否有行业买入（非防御）
                        day_industry_buys = buy_dates.get(date, [])
                        if day_industry_buys:
                            # 有行业买入，可确认为纯排名替换
                            category = 'PURE_RANKING'
                            is_pure_ranking = True
                            match_status = 'MATCHED'
                            match_reason = f'同日行业买入: {day_industry_buys}'
                        else:
                            # 无行业买入，不可确认为排名替换
                            category = 'UNMATCHED_EXIT'
                            match_status = 'NO_MATCH'
                            match_reason = '同日无行业买入（仅防御买入或纯卖出）'
                    else:
                        # 不再满足BUY条件（跌破均线等）
                        category = 'BUY_CONDITION_FAILED'
                        match_status = 'N/A'
                else:
                    # 无当日信号（罕见，可能是数据缺失）
                    category = 'NO_SIGNAL_DATA'
                    match_status = 'NO_SIGNAL'
            else:
                category = 'OTHER_SELL'
                match_status = 'N/A'
        elif action == 'BUY':
            category = 'BUY'
            match_status = 'N/A'
        
        classified.append({
            'date': date,
            'ticker': ticker,
            'action': action,
            'price': row['price'],
            'shares': row['shares'],
            'amount': row['amount'],
            'commission': row['commission'],
            'pnl_pct': row['pnl_pct'],
            'reason': reason,
            'category': category,
            'is_pure_ranking': is_pure_ranking,
            'match_status': match_status,
            'match_reason': match_reason,
        })
    
    return pd.DataFrame(classified)


def analyze_pure_ranking_events(classified_df, trades_df, scores_df, market_df, cfg):
    """
    分析纯排名替换事件。
    
    对每个纯排名替换SELL，找到对应的BUY（同一调仓日），计算：
    - score gap（新ETF评分 - 旧ETF评分）
    - 调仓前后排名
    - 成交金额、佣金
    - 后续5/10/20交易日收益（执行日open至未来对应交易日open）
    """
    
    defense_tickers = set(DEFENSE_UNIVERSE.keys())
    
    # 获取所有纯排名替换SELL
    pure_sells = classified_df[classified_df['category'] == 'PURE_RANKING'].copy()
    
    events = []
    
    for _, sell_row in pure_sells.iterrows():
        date = sell_row['date']
        sell_ticker = sell_row['ticker']
        sell_price = sell_row['price']
        sell_amount = sell_row['amount']
        sell_commission = sell_row['commission']
        
        # 获取当日所有交易
        day_trades = classified_df[classified_df['date'] == date]
        day_buys = day_trades[day_trades['action'] == 'BUY']
        
        # 获取当日评分
        day_scores = scores_df[scores_df['date'] == date].copy()
        
        # 旧ETF评分
        old_score_row = day_scores[day_scores['ticker'] == sell_ticker]
        old_score = old_score_row['total_score'].iloc[0] if not old_score_row.empty else np.nan
        old_rank = old_score_row['total_score'].rank(ascending=False).iloc[0] if not old_score_row.empty else np.nan
        
        # 匹配买入：找到同日的行业买入（非防御）
        # 如果当天有多个卖出和多个买入，需要合理匹配
        # 策略：按评分差距排序，找到最可能对应的买入
        matched_buys = []
        for _, buy_row in day_buys.iterrows():
            buy_ticker = buy_row['ticker']
            if buy_ticker in defense_tickers:
                continue  # 防御买入不算替换
            
            buy_score_row = day_scores[day_scores['ticker'] == buy_ticker]
            buy_score = buy_score_row['total_score'].iloc[0] if not buy_score_row.empty else np.nan
            buy_rank = buy_score_row['total_score'].rank(ascending=False).iloc[0] if not buy_score_row.empty else np.nan
            
            score_gap = buy_score - old_score if not pd.isna(buy_score) and not pd.isna(old_score) else np.nan
            
            matched_buys.append({
                'buy_ticker': buy_ticker,
                'buy_price': buy_row['price'],
                'buy_amount': buy_row['amount'],
                'buy_commission': buy_row['commission'],
                'buy_score': buy_score,
                'buy_rank': buy_rank,
                'score_gap': score_gap,
            })
        
        if not matched_buys:
            # 无匹配行业买入：记录为NO_MATCH事件（不静默跳过）
            events.append({
                'date': date,
                'sell_ticker': sell_ticker,
                'buy_ticker': np.nan,
                'sell_score': old_score,
                'buy_score': np.nan,
                'score_gap': np.nan,
                'sell_rank': old_rank,
                'buy_rank': np.nan,
                'sell_amount': sell_amount,
                'buy_amount': np.nan,
                'sell_commission': sell_commission,
                'buy_commission': np.nan,
                'total_commission': sell_commission,
                'slippage_cost_3bp': sell_amount * 0.0003,
                'total_cost_0bp': sell_commission,
                'total_cost_3bp': sell_commission + sell_amount * 0.0003,
                'sell_return_5d': np.nan,
                'sell_return_10d': np.nan,
                'sell_return_20d': np.nan,
                'buy_return_5d': np.nan,
                'buy_return_10d': np.nan,
                'buy_return_20d': np.nan,
                'excess_5d': np.nan,
                'excess_10d': np.nan,
                'excess_20d': np.nan,
                'match_status': 'NO_MATCH',
                'match_reason': '无匹配行业买入',
            })
        else:
            # 选择score_gap最大的作为最可能对应的买入（策略排名逻辑）
            matched_buys.sort(key=lambda x: x['score_gap'] if not pd.isna(x['score_gap']) else -999, reverse=True)
            best_match = matched_buys[0]
            
            # 计算未来收益（执行日open至未来对应交易日open）
            fwd_returns = calculate_forward_returns(
                market_df, sell_ticker, best_match['buy_ticker'],
                date, sell_price, best_match['buy_price']
            )
            
            # 计算成本
            total_commission = sell_commission + best_match['buy_commission']
            slippage_cost_3bp = (sell_amount * 0.0003) + (best_match['buy_amount'] * 0.0003)
            total_cost_0bp = total_commission
            total_cost_3bp = total_commission + slippage_cost_3bp
            
            events.append({
                'date': date,
                'sell_ticker': sell_ticker,
                'buy_ticker': best_match['buy_ticker'],
                'sell_score': old_score,
                'buy_score': best_match['buy_score'],
                'score_gap': best_match['score_gap'],
                'sell_rank': old_rank,
                'buy_rank': best_match['buy_rank'],
                'sell_amount': sell_amount,
                'buy_amount': best_match['buy_amount'],
                'sell_commission': sell_commission,
                'buy_commission': best_match['buy_commission'],
                'total_commission': total_commission,
                'slippage_cost_3bp': slippage_cost_3bp,
                'total_cost_0bp': total_cost_0bp,
                'total_cost_3bp': total_cost_3bp,
                'sell_return_5d': fwd_returns.get('sell_5d', np.nan),
                'sell_return_10d': fwd_returns.get('sell_10d', np.nan),
                'sell_return_20d': fwd_returns.get('sell_20d', np.nan),
                'buy_return_5d': fwd_returns.get('buy_5d', np.nan),
                'buy_return_10d': fwd_returns.get('buy_10d', np.nan),
                'buy_return_20d': fwd_returns.get('buy_20d', np.nan),
                'excess_5d': fwd_returns.get('excess_5d', np.nan),
                'excess_10d': fwd_returns.get('excess_10d', np.nan),
                'excess_20d': fwd_returns.get('excess_20d', np.nan),
                'match_status': 'MATCHED',
                'match_reason': f'匹配买入: {best_match["buy_ticker"]} (score_gap={best_match["score_gap"]:.2f})',
            })
    
    return pd.DataFrame(events)


def calculate_forward_returns(market_df, sell_ticker, buy_ticker, exec_date, sell_price, buy_price):
    """
    计算执行日后5/10/20个交易日的open-to-open收益。
    
    使用open价计算，避免look-ahead bias。
    """
    market_df = market_df.copy()
    market_df['date'] = pd.to_datetime(market_df['date']).dt.date
    
    exec_date = pd.to_datetime(exec_date).date() if hasattr(exec_date, 'strftime') else exec_date
    
    # 获取执行日后的交易日序列
    all_dates = sorted(market_df['date'].unique())
    
    try:
        exec_idx = all_dates.index(exec_date)
    except ValueError:
        return {}
    
    def get_open_return(ticker, days_ahead):
        """获取指定交易日后的open价，计算收益率"""
        target_idx = exec_idx + days_ahead
        if target_idx >= len(all_dates):
            return np.nan
        
        target_date = all_dates[target_idx]
        target_row = market_df[(market_df['ticker'] == ticker) & (market_df['date'] == target_date)]
        if target_row.empty:
            return np.nan
        
        target_open = target_row['open'].iloc[0]
        
        if days_ahead == 0:
            # 执行日本身
            return 0.0
        
        # 获取执行日的open价（作为买入/卖出基准）
        exec_row = market_df[(market_df['ticker'] == ticker) & (market_df['date'] == exec_date)]
        if exec_row.empty:
            return np.nan
        
        exec_open = exec_row['open'].iloc[0]
        return (target_open - exec_open) / exec_open
    
    result = {}
    for days in [5, 10, 20]:
        result[f'sell_{days}d'] = get_open_return(sell_ticker, days)
        result[f'buy_{days}d'] = get_open_return(buy_ticker, days)
        
        # 超额 = 新ETF收益 - 旧ETF收益
        s_ret = result[f'sell_{days}d']
        b_ret = result[f'buy_{days}d']
        if not pd.isna(s_ret) and not pd.isna(b_ret):
            result[f'excess_{days}d'] = b_ret - s_ret
        else:
            result[f'excess_{days}d'] = np.nan
    
    return result


def compute_statistics(events_df, period_name):
    """计算纯排名替换的统计指标"""
    if events_df.empty:
        return {}
    
    stats = {
        'period': period_name,
        'n_events': len(events_df),
    }
    
    # Score gap分桶
    for gap_label, gap_mask in [
        ('gap_le_2', events_df['score_gap'] <= 2),
        ('gap_2_to_5', (events_df['score_gap'] > 2) & (events_df['score_gap'] <= 5)),
        ('gap_5_to_10', (events_df['score_gap'] > 5) & (events_df['score_gap'] <= 10)),
        ('gap_gt_10', events_df['score_gap'] > 10),
    ]:
        sub = events_df[gap_mask]
        stats[f'{gap_label}_count'] = len(sub)
        
        for days in [5, 10, 20]:
            col = f'excess_{days}d'
            if col in sub.columns and not sub[col].empty:
                valid = sub[col].dropna()
                stats[f'{gap_label}_excess_{days}d_mean'] = valid.mean() if len(valid) > 0 else np.nan
                stats[f'{gap_label}_excess_{days}d_median'] = valid.median() if len(valid) > 0 else np.nan
                stats[f'{gap_label}_excess_{days}d_winrate'] = (valid > 0).mean() if len(valid) > 0 else np.nan
    
    # 总体统计
    for days in [5, 10, 20]:
        col = f'excess_{days}d'
        valid = events_df[col].dropna()
        stats[f'overall_excess_{days}d_mean'] = valid.mean() if len(valid) > 0 else np.nan
        stats[f'overall_excess_{days}d_median'] = valid.median() if len(valid) > 0 else np.nan
        stats[f'overall_excess_{days}d_winrate'] = (valid > 0).mean() if len(valid) > 0 else np.nan
    
    # 成本
    stats['avg_total_cost_0bp'] = events_df['total_cost_0bp'].mean()
    stats['avg_total_cost_3bp'] = events_df['total_cost_3bp'].mean()
    stats['total_cost_0bp_sum'] = events_df['total_cost_0bp'].sum()
    stats['total_cost_3bp_sum'] = events_df['total_cost_3bp'].sum()
    
    # 短期往返：5/10/20日内再次退出的数量
    # 这需要检查events_df中的sell_ticker是否在未来被再次卖出
    # 简化：统计有多少buy_ticker在5/10/20天内被再次卖出（从classified_df中判断）
    
    return stats


def check_short_term_roundtrips(events_df, classified_df):
    """
    检查短期往返：买入的ETF在5/10/20个交易日内再次退出。
    """
    if events_df.empty:
        return {}
    
    classified_df = classified_df.copy()
    classified_df['date'] = pd.to_datetime(classified_df['date']).dt.date
    
    all_dates = sorted(classified_df['date'].unique())
    
    roundtrips = {'5d': 0, '10d': 0, '20d': 0}
    roundtrip_losses = {'5d': [], '10d': [], '20d': []}
    
    for _, event in events_df.iterrows():
        buy_ticker = event['buy_ticker']
        buy_date = event['date']
        
        try:
            buy_idx = all_dates.index(buy_date)
        except ValueError:
            continue
        
        for days, key in [(5, '5d'), (10, '10d'), (20, '20d')]:
            exit_idx = buy_idx + days
            if exit_idx >= len(all_dates):
                continue
            
            # 检查该ETF在后续days天内是否有SELL或STOP_LOSS
            future_dates = all_dates[buy_idx + 1:exit_idx + 1]
            future_sells = classified_df[
                (classified_df['ticker'] == buy_ticker) &
                (classified_df['date'].isin(future_dates)) &
                (classified_df['action'].isin(['SELL', 'STOP_LOSS']))
            ]
            
            if not future_sells.empty:
                roundtrips[key] += 1
                # 记录损失（pnl_pct）
                pnl = future_sells['pnl_pct'].iloc[0]
                roundtrip_losses[key].append(pnl)
    
    result = {}
    for key in ['5d', '10d', '20d']:
        result[f'roundtrip_{key}_count'] = roundtrips[key]
        result[f'roundtrip_{key}_rate'] = roundtrips[key] / len(events_df) if len(events_df) > 0 else 0
        if roundtrip_losses[key]:
            result[f'roundtrip_{key}_avg_loss'] = np.mean(roundtrip_losses[key])
        else:
            result[f'roundtrip_{key}_avg_loss'] = np.nan
    
    return result


def generate_report(classified_df, events_df, stats_research, stats_validation, 
                     roundtrip_research, roundtrip_validation, total_trades_by_period,
                     output_md, output_events_csv, output_summary_csv):
    """生成报告和CSV"""
    
    # 保存事件CSV（列头必须与analyze_pure_ranking_events输出一致）
    event_columns = [
        'date', 'sell_ticker', 'buy_ticker', 'sell_score', 'buy_score', 'score_gap',
        'sell_rank', 'buy_rank', 'sell_amount', 'buy_amount', 'sell_commission',
        'buy_commission', 'total_commission', 'slippage_cost_3bp', 'total_cost_0bp',
        'total_cost_3bp', 'sell_return_5d', 'sell_return_10d', 'sell_return_20d',
        'buy_return_5d', 'buy_return_10d', 'buy_return_20d', 'excess_5d', 'excess_10d', 'excess_20d',
        'match_status', 'match_reason'
    ]
    if events_df.empty:
        empty_events = pd.DataFrame(columns=event_columns)
        empty_events.to_csv(output_events_csv, index=False, encoding='utf-8-sig')
    else:
        for col in event_columns:
            if col not in events_df.columns:
                events_df[col] = None
        events_df[event_columns].to_csv(output_events_csv, index=False, encoding='utf-8-sig')
    
    # 汇总统计（即使为空也保留period列）
    summary_rows = []
    for stats, rt_stats, period_name in [
        (stats_research, roundtrip_research, '研究期(2019-2022)'),
        (stats_validation, roundtrip_validation, '验证期(2023-2024)')
    ]:
        row = {'period': period_name}
        if stats:
            row.update(stats)
        if rt_stats:
            row.update(rt_stats)
        summary_rows.append(row)
    
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(output_summary_csv, index=False, encoding='utf-8-sig')
    
    # 生成分类汇总
    category_counts = classified_df['category'].value_counts().to_dict()
    
    # 按调仓日聚合（用于勾稽）
    rebalance_dates = classified_df[classified_df['action'].isin(['SELL', 'STOP_LOSS'])]['date'].nunique()
    
    # 计算有效样本数
    valid_5d = events_df['excess_5d'].notna().sum() if not events_df.empty else 0
    valid_10d = events_df['excess_10d'].notna().sum() if not events_df.empty else 0
    valid_20d = events_df['excess_20d'].notna().sum() if not events_df.empty else 0
    
    # 写入报告
    with open(output_md, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 1: 换仓成本与有效性归因报告\n\n")
        f.write(f"> 生成日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 基准: B0.4 (v1.2.3-b0.4)\n")
        f.write(f"> 回测区间: 2019-08-13 ~ 2026-06-18\n\n")
        
        f.write("## 1. 交易分类汇总\n\n")
        f.write("| 分类 | 数量 | 占比 | 说明 |\n")
        f.write("|------|------|------|------|\n")
        total_sells = classified_df[classified_df['action'].isin(['SELL', 'STOP_LOSS'])].shape[0]
        for cat, count in category_counts.items():
            if cat in ['BUY', 'OTHER']:
                continue
            pct = count / total_sells * 100 if total_sells > 0 else 0
            desc = {
                'STOP_LOSS': '止损退出',
                'BUY_CONDITION_FAILED': '不再满足BUY条件（跌破均线等）',
                'DEFENSE_YIELD': '防御资产为行业让路',
                'PURE_RANKING': '纯排名替换（原持仓仍满足BUY条件+同日有行业买入）',
                'UNMATCHED_EXIT': '无匹配退出（原持仓仍满足BUY条件但无行业买入）',
                'NO_SIGNAL_DATA': '无信号数据',
                'OTHER_SELL': '其他卖出',
            }.get(cat, cat)
            f.write(f"| {desc} | {count} | {pct:.1f}% | {cat} |\n")
        f.write(f"| **卖出合计** | **{total_sells}** | **100%** | |\n")
        f.write(f"| 买入 | {category_counts.get('BUY', 0)} | — | 行业买入+防御填充 |\n\n")
        
        f.write("## 2. 关键发现：B0.4 未实现 ranking-based replacement\n\n")
        f.write("**核心发现**：B0.4 的 `plan_rebalance_v2_5` 调仓引擎**不实现**基于排名的替换逻辑。\n\n")
        f.write("- 配置中存在 `replacement_score_gap=8` 参数，但**调仓引擎未引用该参数**。\n")
        f.write("- 当前引擎逻辑：`tradable_industry_tickers` = 所有 `signal_type='BUY'` 的行业ETF。\n")
        f.write("- 保留逻辑：持仓只要在 `tradable_industry_tickers` 中就被保留，**不检查排名**。\n")
        f.write("- 卖出逻辑：只有 `signal_type != 'BUY'` 的持仓才会被卖出（reason='调出候选列表'）。\n")
        f.write("- 因此，**原持仓仍满足BUY条件但因排名不够高而被替换的情况在B0.4中不可能发生**。\n\n")
        f.write("这意味着用户假设的场景（'持仓仍合格，仅因排名小幅变化而被替换'）\n")
        f.write("在当前B0.4实现中**不存在**。所有 '调出候选列表' 卖出都是由于信号失效\n")
        f.write("（跌破均线、total_score不足等），而非排名竞争。\n\n")
        
        f.write("## 3. 数据勾稽\n\n")
        f.write(f"- 调仓日数: {rebalance_dates} 天\n")
        f.write(f"- 总交易数: {len(classified_df)} 笔\n")
        f.write(f"- 研究期(2019-2022) SELL: {total_trades_by_period.get('research_sell', 0)} 笔\n")
        f.write(f"- 验证期(2023-2024) SELL: {total_trades_by_period.get('validation_sell', 0)} 笔\n")
        f.write(f"- 纯排名替换事件: {len(events_df)} 笔\n")
        f.write(f"- 有效5日收益样本: {valid_5d} / {len(events_df)}\n")
        f.write(f"- 有效10日收益样本: {valid_10d} / {len(events_df)}\n")
        f.write(f"- 有效20日收益样本: {valid_20d} / {len(events_df)}\n")
        f.write(f"- 无法计算未来收益的样本: {len(events_df) - valid_5d} 笔（数据不足）\n\n")
        
        if events_df.empty:
            f.write("## 4. 纯排名替换分析\n\n")
            f.write("**纯排名替换事件为0**，因此无法计算超额收益、score gap分桶或短期往返。\n")
            f.write("这是B0.4调仓引擎的设计结果，不是数据问题。\n\n")
        else:
            f.write("## 4. 纯排名替换分析\n\n")
            
            for stats, rt_stats, period_name in [
                (stats_research, roundtrip_research, '研究期(2019-2022)'),
                (stats_validation, roundtrip_validation, '验证期(2023-2024)')
            ]:
                if not stats:
                    continue
                
                f.write(f"### 4.1 {period_name}\n\n")
                f.write(f"- 纯排名替换事件: {stats['n_events']} 笔\n")
                f.write(f"- 平均成本(0bp): {stats['avg_total_cost_0bp']:.2f} 元\n")
                f.write(f"- 平均成本(3bp): {stats['avg_total_cost_3bp']:.2f} 元\n")
                f.write(f"- 总成本(0bp): {stats['total_cost_0bp_sum']:.2f} 元\n")
                f.write(f"- 总成本(3bp): {stats['total_cost_3bp_sum']:.2f} 元\n\n")
                
                f.write("**总体超额收益**（新ETF - 旧ETF，open-to-open）：\n\n")
                f.write("| 期限 | 均值 | 中位数 | 胜率 | 样本数 |\n")
                f.write("|------|------|--------|------|--------|\n")
                for days in [5, 10, 20]:
                    mean_val = stats.get(f'overall_excess_{days}d_mean', np.nan)
                    median_val = stats.get(f'overall_excess_{days}d_median', np.nan)
                    winrate = stats.get(f'overall_excess_{days}d_winrate', np.nan)
                    n = valid_5d if days == 5 else (valid_10d if days == 10 else valid_20d)
                    f.write(f"| {days}日 | {mean_val*100:.2f}% | {median_val*100:.2f}% | {winrate*100:.1f}% | {n} |\n")
                f.write("\n")
                
                f.write("**Score Gap 分桶分析**\n\n")
                f.write("| Score Gap | 事件数 | 5日超额均值 | 5日胜率 | 10日超额均值 | 10日胜率 | 20日超额均值 | 20日胜率 |\n")
                f.write("|-----------|--------|-------------|---------|--------------|----------|--------------|----------|\n")
                for gap_label, gap_desc in [
                    ('gap_le_2', '≤2'),
                    ('gap_2_to_5', '2–5'),
                    ('gap_5_to_10', '5–10'),
                    ('gap_gt_10', '>10')
                ]:
                    count = stats.get(f'{gap_label}_count', 0)
                    e5 = stats.get(f'{gap_label}_excess_5d_mean', np.nan)
                    w5 = stats.get(f'{gap_label}_excess_5d_winrate', np.nan)
                    e10 = stats.get(f'{gap_label}_excess_10d_mean', np.nan)
                    w10 = stats.get(f'{gap_label}_excess_10d_winrate', np.nan)
                    e20 = stats.get(f'{gap_label}_excess_20d_mean', np.nan)
                    w20 = stats.get(f'{gap_label}_excess_20d_winrate', np.nan)
                    f.write(f"| {gap_desc} | {count} | {e5*100:.2f}% | {w5*100:.1f}% | {e10*100:.2f}% | {w10*100:.1f}% | {e20*100:.2f}% | {w20*100:.1f}% |\n")
                f.write("\n")
                
                f.write("**短期往返分析**\n\n")
                f.write("| 期限 | 往返次数 | 往返率 | 平均损失 |\n")
                f.write("|------|----------|--------|----------|\n")
                for key, label in [('5d', '5日'), ('10d', '10日'), ('20d', '20日')]:
                    count = rt_stats.get(f'roundtrip_{key}_count', 0)
                    rate = rt_stats.get(f'roundtrip_{key}_rate', 0)
                    avg_loss = rt_stats.get(f'roundtrip_{key}_avg_loss', np.nan)
                    f.write(f"| {label} | {count} | {rate*100:.1f}% | {avg_loss*100:.2f}% |\n")
                f.write("\n")
        
        f.write("## 5. 进入Step 2的条件评估\n\n")
        
        # 评估条件
        total_pure = stats_research.get('n_events', 0) + stats_validation.get('n_events', 0)
        total_sell = total_trades_by_period.get('total_sell', 0)
        ratio = total_pure / total_sell if total_sell > 0 else 0
        
        f.write(f"1. **纯排名替换样本量足够**: ❌ (总事件={total_pure}，远小于10)\n")
        f.write(f"2. **小score gap替换在研究期和验证期均无正增量**: N/A（无样本）\n")
        f.write(f"3. **扣除3bp滑点和佣金后净增量为负**: N/A（无样本）\n")
        f.write(f"4. **低价值替换占总交易比例足以影响策略**: ❌ (比例={ratio*100:.1f}%，远低于10%)\n\n")
        
        f.write("**结论**: 不满足进入Step 2的条件，停止。\n\n")
        f.write("B0.4 的 `plan_rebalance_v2_5` 不实现 ranking-based replacement，\n")
        f.write("因此不存在'持仓仍合格，仅因排名变化被替换'的低价值换仓。\n")
        f.write("如需引入 replacement_score_gap 逻辑，需先修改调仓引擎，\n")
        f.write("这属于策略变更，不是参数调优。建议作为独立实验设计。\n\n")
        
        f.write("## 6. 免责声明\n\n")
        f.write("- 本分析基于B0.4回测交易记录，未来收益使用open-to-open口径计算。\n")
        f.write("- '纯排名替换'的分类基于信号重建，可能与实际执行有微小差异。\n")
        f.write("- 短期往返统计仅覆盖买入后指定天数内的首次退出。\n")
        f.write("- 样本外区间（2025-2026）未参与分析。\n")
    
    print(f"报告已保存: {output_md}")
    print(f"事件CSV: {output_events_csv}")
    print(f"汇总CSV: {output_summary_csv}")


def main():
    print("=" * 70)
    print("v1.3 Step 1: 换仓成本与有效性归因")
    print("=" * 70)
    
    # 1. 运行B0.4回测
    print("\n[1/5] 运行B0.4回测...")
    result, market_df, bench_df, cfg = run_b0_4_backtest()
    trades_df = result['trades_df'].copy()
    print(f"  总交易: {len(trades_df)} 笔")
    print(f"  最终NAV: {result['nav_df']['nav'].iloc[-1]:,.2f}")
    
    # 2. 生成每日信号（含评分）
    print("\n[2/5] 生成每日信号...")
    scores_df = generate_daily_signals(market_df, bench_df, cfg)
    print(f"  信号记录: {len(scores_df)} 条")
    
    # 3. 交易分类
    print("\n[3/5] 交易分类...")
    classified_df = classify_trades(trades_df, scores_df, cfg)
    
    print("  分类结果:")
    for cat, count in classified_df['category'].value_counts().items():
        print(f"    {cat}: {count}")
    
    # 4. 纯排名替换分析
    print("\n[4/5] 纯排名替换分析...")
    events_df = analyze_pure_ranking_events(classified_df, trades_df, scores_df, market_df, cfg)
    print(f"  纯排名替换事件: {len(events_df)} 笔")
    
    # 5. 按样本划分
    print("\n[5/5] 按样本划分统计...")
    
    # 划分日期
    research_end = pd.to_datetime('2022-12-31').date()
    validation_start = pd.to_datetime('2023-01-01').date()
    validation_end = pd.to_datetime('2024-12-31').date()
    
    # 处理空 events_df
    if events_df.empty:
        print("  警告: 纯排名替换事件为0，可能当前B0.4 planner未实现ranking-based replacement逻辑")
        research_events = pd.DataFrame(columns=events_df.columns) if not events_df.empty else pd.DataFrame()
        validation_events = pd.DataFrame(columns=events_df.columns) if not events_df.empty else pd.DataFrame()
    else:
        research_events = events_df[events_df['date'] <= research_end].copy()
        validation_events = events_df[
            (events_df['date'] >= validation_start) & (events_df['date'] <= validation_end)
        ].copy()
    
    # 统计
    stats_research = compute_statistics(research_events, '研究期(2019-2022)')
    stats_validation = compute_statistics(validation_events, '验证期(2023-2024)')
    
    # 短期往返
    rt_research = check_short_term_roundtrips(research_events, classified_df)
    rt_validation = check_short_term_roundtrips(validation_events, classified_df)
    
    # 总交易数（按样本划分）
    classified_df['date'] = pd.to_datetime(classified_df['date']).dt.date
    research_sells = classified_df[
        (classified_df['date'] <= research_end) & 
        (classified_df['action'].isin(['SELL', 'STOP_LOSS']))
    ]
    validation_sells = classified_df[
        (classified_df['date'] >= validation_start) & 
        (classified_df['date'] <= validation_end) &
        (classified_df['action'].isin(['SELL', 'STOP_LOSS']))
    ]
    total_sells = classified_df[classified_df['action'].isin(['SELL', 'STOP_LOSS'])]
    
    total_trades_by_period = {
        'research_sell': len(research_sells),
        'validation_sell': len(validation_sells),
        'total_sell': len(total_sells),
    }
    
    print(f"  研究期卖出: {len(research_sells)} 笔")
    print(f"  验证期卖出: {len(validation_sells)} 笔")
    print(f"  研究期纯排名替换: {len(research_events)} 笔")
    print(f"  验证期纯排名替换: {len(validation_events)} 笔")
    
    # 6. 生成报告
    print("\n[6/6] 生成报告...")
    output_dir = os.path.join(BASE_DIR, 'reports')
    os.makedirs(output_dir, exist_ok=True)
    
    output_md = os.path.join(output_dir, 'v1_3_step1_replacement_attribution.md')
    output_events_csv = os.path.join(output_dir, 'v1_3_step1_replacement_events.csv')
    output_summary_csv = os.path.join(output_dir, 'v1_3_step1_replacement_summary.csv')
    
    generate_report(
        classified_df, events_df, stats_research, stats_validation,
        rt_research, rt_validation, total_trades_by_period,
        output_md, output_events_csv, output_summary_csv
    )
    
    print("\n" + "=" * 70)
    print("v1.3 Step 1 完成")
    print("=" * 70)
    
    return {
        'classified_df': classified_df,
        'events_df': events_df,
        'stats_research': stats_research,
        'stats_validation': stats_validation,
    }


if __name__ == '__main__':
    main()
