#!/usr/bin/env python3
"""
v1.3 Step 3 v2: 信号失效退出有效性归因（修正版）

修复清单：
1. 完整保留341笔事件，禁止因无未来行情而continue，CSV严格341行。
2. 增加 observation_status、available_future_days 列。
3. 禁止使用不足20日的残缺窗口判断20日指标；数据不足样本主分类设为"数据不足"。
4. 重新买回统计分开输出：任意未来首次买回率、20个交易日内买回率、20日内且买回价>=卖出价的震荡往返率。
5. 往返佣金修正为 sell_commission + rebuy_commission，分别输出全部买回和震荡往返样本的总额、均值。
6. 补齐分层：持有时间区间、退出时total_score区间、具体信号失效条件。
7. 修正方向一致性阈值：统一使用15%，不得混用10%。
8. 增加独立数据勾稽章节。
9. 不修改B0.4策略、参数、交易逻辑或冻结基线。
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
from collections import Counter

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

AS_OF_DATE = '2026-06-18'

TARGET_BCF_COUNT = 341
THRESHOLD_RT = 0.15  # 震荡往返阈值15%
THRESHOLD_FK = 0.15  # 误杀卖飞阈值15%


def get_b0_4_config():
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    return cfg


def run_b0_4_backtest():
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
    """生成每日信号（复用Step 1）"""
    strategy = StrategyEngine(cfg)
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
    scores_df = strategy.rank_all_momentum(scores_df)
    scores_df = strategy.compute_total_score(scores_df)
    scores_df = strategy.generate_signals(scores_df, bench_df)
    return scores_df


def get_bcf_exits(trades_df, scores_df):
    """提取341笔BUY_CONDITION_FAILED退出（复用Step 1分类逻辑）"""
    trades_df = trades_df.copy()
    scores_df = scores_df.copy()
    trades_df['date'] = pd.to_datetime(trades_df['date']).dt.date
    scores_df['date'] = pd.to_datetime(scores_df['date']).dt.date

    bcf_exits = []
    sells = trades_df[trades_df['action'] == 'SELL'].copy()
    for _, row in sells.iterrows():
        date = row['date']
        ticker = row['ticker']
        reason = str(row['reason'])
        if '调出候选列表' in reason:
            day_signals = scores_df[(scores_df['date'] == date) & (scores_df['ticker'] == ticker)]
            if not day_signals.empty and day_signals['signal_type'].iloc[0] != 'BUY':
                bcf_exits.append(row.to_dict())

    return pd.DataFrame(bcf_exits)


def compute_post_exit_performance(bcf_df, market_df, trades_df, scores_df):
    """
    计算每笔退出后的表现。完整保留341笔，不因无未来行情而跳过。

    口径：
    - T+1开盘成交，卖出价格为实际成交价
    - 未来收益从卖出日期后的第一个交易日开始
    - 有效交易日（非自然日）
    - 禁止未来函数
    - 时间分区边界：研究期截止2022-12-31，验证期截止2024-12-31，样本外截止2026-06-18
    """
    market_df = market_df.copy().sort_values(['ticker', 'date'])
    market_df['date'] = pd.to_datetime(market_df['date']).dt.date
    trades_df = trades_df.copy()
    trades_df['date'] = pd.to_datetime(trades_df['date']).dt.date
    scores_df = scores_df.copy()
    scores_df['date'] = pd.to_datetime(scores_df['date']).dt.date

    # 时间分区边界
    PERIOD_BOUNDS = {
        '研究期': pd.Timestamp('2022-12-31').date(),
        '验证期': pd.Timestamp('2024-12-31').date(),
        '样本外': pd.Timestamp('2026-06-18').date(),
    }

    def get_period_end(sell_date):
        if sell_date.year <= 2022:
            return '研究期', PERIOD_BOUNDS['研究期']
        elif sell_date.year <= 2024:
            return '验证期', PERIOD_BOUNDS['验证期']
        else:
            return '样本外', PERIOD_BOUNDS['样本外']

    # 预计算：每只ETF的所有BUY交易，用于查找重新买回
    ticker_buys_map = {}
    for ticker in trades_df['ticker'].unique():
        buys = trades_df[(trades_df['ticker'] == ticker) & (trades_df['action'] == 'BUY')].sort_values('date')
        ticker_buys_map[ticker] = buys

    results = []

    for _, row in bcf_df.iterrows():
        sell_date = row['date']
        ticker = row['ticker']
        sell_price = row['price']
        shares = row['shares']
        pnl_pct = row['pnl_pct']
        reason = row['reason']
        amount = row['amount']
        sell_commission = row['commission']

        # 获取period和边界
        period_name, period_end = get_period_end(sell_date)

        # 获取该ETF的交易日序列，并限制在分区边界内
        tdf = market_df[market_df['ticker'] == ticker].sort_values('date').reset_index(drop=True)
        tdf = tdf[tdf['date'] <= period_end].reset_index(drop=True)
        if tdf.empty:
            # 无市场数据，记录NO_FUTURE
            results.append(_make_result_record(
                sell_date, ticker, sell_price, shares, amount, sell_commission, pnl_pct, reason,
                observation_status='NO_FUTURE', available_future_days=0,
                n_future=0, start_idx=-1, period_end=period_end
            ))
            continue

        # 找到卖出日期后的第一个交易日索引
        sell_idx = tdf[tdf['date'] == sell_date].index
        if len(sell_idx) == 0:
            future_dates = tdf[tdf['date'] > sell_date]
            if future_dates.empty:
                results.append(_make_result_record(
                    sell_date, ticker, sell_price, shares, amount, sell_commission, pnl_pct, reason,
                    observation_status='NO_FUTURE', available_future_days=0,
                    n_future=0, start_idx=-1, period_end=period_end
                ))
                continue
            start_idx = future_dates.index[0]
        else:
            start_idx = sell_idx[0] + 1

        if start_idx >= len(tdf):
            results.append(_make_result_record(
                sell_date, ticker, sell_price, shares, amount, sell_commission, pnl_pct, reason,
                observation_status='NO_FUTURE', available_future_days=0,
                n_future=0, start_idx=-1, period_end=period_end
            ))
            continue

        # 未来交易日序列（已限制在分区边界内）
        future_tdf = tdf.iloc[start_idx:].copy()
        n_future = len(future_tdf)

        # 确定观察状态
        if period_name in ['研究期', '验证期']:
            if n_future >= 20:
                observation_status = 'COMPLETE_20D'
                available_future_days = n_future
            elif n_future > 0:
                observation_status = 'CENSORED_PERIOD_END'
                available_future_days = n_future
            else:
                observation_status = 'NO_FUTURE'
                available_future_days = 0
        else:  # 样本外
            if n_future >= 20:
                observation_status = 'COMPLETE_20D'
                available_future_days = n_future
            elif n_future > 0:
                observation_status = 'CENSORED_DATA_END'
                available_future_days = n_future
            else:
                observation_status = 'NO_FUTURE'
                available_future_days = 0

        # 计算各窗口收益（只有完整20日窗口才计算20日指标）
        ret_5d = np.nan
        ret_10d = np.nan
        ret_20d = np.nan
        max_rise_20d = np.nan
        max_fall_20d = np.nan
        max_rise_all = np.nan
        max_fall_all = np.nan

        if n_future >= 5:
            price_5 = future_tdf['open'].iloc[4]
            ret_5d = price_5 / sell_price - 1
        if n_future >= 10:
            price_10 = future_tdf['open'].iloc[9]
            ret_10d = price_10 / sell_price - 1
        if n_future >= 20:
            price_20 = future_tdf['open'].iloc[19]
            ret_20d = price_20 / sell_price - 1
            max_price_20 = future_tdf['high'].iloc[:20].max()
            min_price_20 = future_tdf['low'].iloc[:20].min()
            max_rise_20d = max_price_20 / sell_price - 1
            max_fall_20d = min_price_20 / sell_price - 1

        # 全部可用窗口的最大涨跌（用于5日/10日分析，不用于20日分类）
        if n_future > 0:
            max_price_all = future_tdf['high'].max()
            min_price_all = future_tdf['low'].min()
            max_rise_all = max_price_all / sell_price - 1
            max_fall_all = min_price_all / sell_price - 1

        # 查找重新买回（同一ETF的后续首次BUY，限制在分区边界内）
        rebought_any = False
        rebought_20d = False
        rebuy_date = None
        rebuy_price = np.nan
        days_to_rebuy = np.nan
        rebuy_pnl = np.nan
        rebuy_spread = np.nan
        rebuy_commission = np.nan

        buys = ticker_buys_map.get(ticker, pd.DataFrame())
        # 限制在分区边界内
        buys = buys[buys['date'] <= period_end]
        future_buys = buys[buys['date'] > sell_date]
        if not future_buys.empty:
            rebuy_row = future_buys.iloc[0]
            rebought_any = True
            rebuy_date = rebuy_row['date']
            rebuy_price = rebuy_row['price']
            rebuy_commission = rebuy_row['commission']
            # 计算间隔交易日数（从卖出日期的下一个交易日到买回日期的交易日）
            rebuy_idx = tdf[tdf['date'] == rebuy_date].index
            if len(rebuy_idx) > 0:
                days_to_rebuy = rebuy_idx[0] - start_idx + 1
            else:
                days_to_rebuy = np.nan
            rebuy_pnl = rebuy_price / sell_price - 1
            rebuy_spread = rebuy_price - sell_price
            # 判断是否在20个交易日内买回
            if not pd.isna(days_to_rebuy) and days_to_rebuy <= 20:
                rebought_20d = True
        else:
            rebuy_date = '未买回'

        # 获取退出时的信号信息（用于分层分析）
        exit_signals = scores_df[(scores_df['date'] == sell_date) & (scores_df['ticker'] == ticker)]
        total_score_at_exit = exit_signals['total_score'].iloc[0] if not exit_signals.empty else np.nan
        ma_condition = exit_signals['ma_condition'].iloc[0] if not exit_signals.empty and 'ma_condition' in exit_signals.columns else np.nan
        multi_condition = exit_signals['multi_condition'].iloc[0] if not exit_signals.empty and 'multi_condition' in exit_signals.columns else np.nan
        signal_type = exit_signals['signal_type'].iloc[0] if not exit_signals.empty else 'UNKNOWN'

        # 计算持有时间（从上一笔BUY到本次SELL的交易日数）
        holding_days = np.nan
        past_buys = buys[buys['date'] < sell_date]
        if not past_buys.empty:
            last_buy_date = past_buys.iloc[-1]['date']
            last_buy_idx = tdf[tdf['date'] == last_buy_date].index
            sell_idx_in_tdf = tdf[tdf['date'] == sell_date].index
            if len(last_buy_idx) > 0 and len(sell_idx_in_tdf) > 0:
                holding_days = sell_idx_in_tdf[0] - last_buy_idx[0]

        results.append({
            'sell_date': sell_date,
            'ticker': ticker,
            'sell_price': sell_price,
            'shares': shares,
            'amount': amount,
            'sell_commission': sell_commission,
            'pnl_pct': pnl_pct,
            'reason': reason,
            'ret_5d': ret_5d,
            'ret_10d': ret_10d,
            'ret_20d': ret_20d,
            'max_rise_20d': max_rise_20d,
            'max_fall_20d': max_fall_20d,
            'max_rise_all': max_rise_all,
            'max_fall_all': max_fall_all,
            'observation_status': observation_status,
            'available_future_days': available_future_days,
            'n_future': n_future,
            'period_end': period_end,
            'rebuy_date': rebuy_date,
            'rebuy_price': rebuy_price,
            'days_to_rebuy': days_to_rebuy,
            'rebuy_pnl': rebuy_pnl,
            'rebuy_spread': rebuy_spread,
            'rebuy_commission': rebuy_commission,
            'total_score_at_exit': total_score_at_exit,
            'ma_condition': ma_condition,
            'multi_condition': multi_condition,
            'signal_type': signal_type,
            'holding_days': holding_days,
            'rebought_any': rebought_any,
            'rebought_20d': rebought_20d,
        })

    return pd.DataFrame(results)


def _make_result_record(sell_date, ticker, sell_price, shares, amount, sell_commission, pnl_pct, reason,
                        observation_status, available_future_days, n_future, start_idx, period_end=None):
    """为无未来数据的样本生成空记录"""
    return {
        'sell_date': sell_date,
        'ticker': ticker,
        'sell_price': sell_price,
        'shares': shares,
        'amount': amount,
        'sell_commission': sell_commission,
        'pnl_pct': pnl_pct,
        'reason': reason,
        'ret_5d': np.nan,
        'ret_10d': np.nan,
        'ret_20d': np.nan,
        'max_rise_20d': np.nan,
        'max_fall_20d': np.nan,
        'max_rise_all': np.nan,
        'max_fall_all': np.nan,
        'observation_status': observation_status,
        'available_future_days': available_future_days,
        'n_future': n_future,
        'period_end': period_end,
        'rebuy_date': '未买回',
        'rebuy_price': np.nan,
        'days_to_rebuy': np.nan,
        'rebuy_pnl': np.nan,
        'rebuy_spread': np.nan,
        'rebuy_commission': np.nan,
        'total_score_at_exit': np.nan,
        'ma_condition': np.nan,
        'multi_condition': np.nan,
        'signal_type': 'UNKNOWN',
        'holding_days': np.nan,
        'rebought_any': False,
        'rebought_20d': False,
    }


def classify_exits(df):
    """
    按预设规则分类。

    约束：
    - 只有 COMPLETE_20D 样本才能参与20日相关分类
    - 数据不足样本（CENSORED_PERIOD_END / CENSORED_DATA_END / NO_FUTURE）主分类设为"数据不足"
    - 20日最大涨跌/收益/误杀/避损不得使用残缺窗口

    优先级：震荡往返 > 误杀卖飞 > 有效避损 > 中性
    """
    classifications = []
    for _, row in df.iterrows():
        obs_status = row['observation_status']
        ret_20d = row['ret_20d']
        max_rise_20d = row['max_rise_20d']
        max_fall_20d = row['max_fall_20d']
        rebought_20d = row['rebought_20d']
        rebuy_price = row['rebuy_price']
        sell_price = row['sell_price']
        rebuy_commission = row['rebuy_commission']

        # 数据不足样本：主分类为"数据不足"，不参与20日判断
        if obs_status != 'COMPLETE_20D':
            classifications.append({
                'primary': '数据不足',
                'tags': '数据不足',
                'is_round_trip': False,
                'is_false_kill': False,
                'is_avoid_loss': False,
                'round_trip_20d': False,
            })
            continue

        # 以下为COMPLETE_20D样本的分类
        is_round_trip = False
        is_false_kill = False
        is_avoid_loss = False
        round_trip_20d = False

        # 震荡往返：20日内重新买回，且买回价>=卖出价，且产生额外佣金
        if rebought_20d and not pd.isna(rebuy_price) and not pd.isna(sell_price) and rebuy_price >= sell_price:
            if not pd.isna(rebuy_commission) and rebuy_commission > 0:
                is_round_trip = True
                round_trip_20d = True

        # 误杀卖飞：20日收益>=+3% 或 20日最大上涨>=+5%
        if not pd.isna(ret_20d) and ret_20d >= 0.03:
            is_false_kill = True
        elif not pd.isna(max_rise_20d) and max_rise_20d >= 0.05:
            is_false_kill = True

        # 有效避损：20日收益<=-3% 或 20日最大下跌<=-5%
        if not pd.isna(ret_20d) and ret_20d <= -0.03:
            is_avoid_loss = True
        elif not pd.isna(max_fall_20d) and max_fall_20d <= -0.05:
            is_avoid_loss = True

        # 优先级：震荡往返 > 误杀卖飞 > 有效避损 > 中性
        primary = '中性'
        tags = []
        if is_round_trip:
            primary = '震荡往返'
            tags.append('震荡往返')
        elif is_false_kill:
            primary = '误杀卖飞'
            tags.append('误杀卖飞')
        elif is_avoid_loss:
            primary = '有效避损'
            tags.append('有效避损')
        else:
            tags.append('中性')

        # 补充标签（即使不是主分类，也记录）
        if is_avoid_loss and primary != '有效避损':
            tags.append('有效避损')
        if is_false_kill and primary != '误杀卖飞':
            tags.append('误杀卖飞')
        if is_round_trip and primary != '震荡往返':
            tags.append('震荡往返')

        classifications.append({
            'primary': primary,
            'tags': ','.join(tags) if tags else '中性',
            'is_round_trip': is_round_trip,
            'is_false_kill': is_false_kill,
            'is_avoid_loss': is_avoid_loss,
            'round_trip_20d': round_trip_20d,
        })

    return pd.DataFrame(classifications)


def add_period_and_regime(df, nav_df):
    """添加研究期/验证期/样本外标签，以及市场状态"""
    df = df.copy()
    df['year'] = pd.to_datetime(df['sell_date']).dt.year

    def period_label(d):
        if d.year <= 2022:
            return '研究期'
        elif d.year <= 2024:
            return '验证期'
        else:
            return '样本外'

    df['period'] = pd.to_datetime(df['sell_date']).apply(period_label)

    if nav_df is not None and 'regime_name' in nav_df.columns:
        nav_df = nav_df.copy()
        nav_df['date'] = pd.to_datetime(nav_df['date']).dt.date
        df = df.merge(
            nav_df[['date', 'regime_name', 'regime_id']].drop_duplicates(),
            left_on='sell_date', right_on='date', how='left'
        )
        df.drop(columns='date', inplace=True, errors='ignore')
    else:
        df['regime_name'] = '未知'
        df['regime_id'] = np.nan

    return df


def generate_summary(df, output_dir):
    """生成分层汇总统计"""
    summaries = []

    # 1. 总体（按period）
    for period_name in ['研究期', '验证期', '样本外']:
        period_df = df[df['period'] == period_name]
        if len(period_df) == 0:
            continue

        n = len(period_df)
        n_complete = (period_df['observation_status'] == 'COMPLETE_20D').sum()
        n_censored_period = (period_df['observation_status'] == 'CENSORED_PERIOD_END').sum()
        n_censored_data = (period_df['observation_status'] == 'CENSORED_DATA_END').sum()
        n_no_future = (period_df['observation_status'] == 'NO_FUTURE').sum()

        # 完整20日样本的主分类
        complete_df = period_df[period_df['observation_status'] == 'COMPLETE_20D']
        n_comp = len(complete_df)

        rt = (complete_df['primary'] == '震荡往返').sum() if n_comp > 0 else 0
        fk = (complete_df['primary'] == '误杀卖飞').sum() if n_comp > 0 else 0
        al = (complete_df['primary'] == '有效避损').sum() if n_comp > 0 else 0
        neu = (complete_df['primary'] == '中性').sum() if n_comp > 0 else 0
        insuff = (complete_df['primary'] == '数据不足').sum() if n_comp > 0 else 0

        # 重新买回统计（全样本）
        rebought_any = period_df['rebought_any'].sum()
        rebought_20d = period_df['rebought_20d'].sum()
        round_trip_20d = (complete_df['round_trip_20d'] == True).sum() if n_comp > 0 else 0

        # 佣金统计（全部买回样本）
        rebought_df = period_df[period_df['rebought_any'] == True]
        total_round_trip_commission_all = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).sum() if len(rebought_df) > 0 else 0
        avg_round_trip_commission_all = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).mean() if len(rebought_df) > 0 else 0

        # 佣金统计（仅震荡往返样本）
        rt_df = complete_df[complete_df['primary'] == '震荡往返']
        total_rt_commission = (rt_df['sell_commission'] + rt_df['rebuy_commission']).sum() if len(rt_df) > 0 else 0
        avg_rt_commission = (rt_df['sell_commission'] + rt_df['rebuy_commission']).mean() if len(rt_df) > 0 else 0

        summaries.append({
            '维度': 'period',
            '子维度': period_name,
            '总样本数': n,
            '完整20日': n_complete,
            '分区边界截尾': n_censored_period,
            '数据截止截尾': n_censored_data,
            '无未来数据': n_no_future,
            '震荡往返': rt,
            '误杀卖飞': fk,
            '有效避损': al,
            '中性': neu,
            '数据不足': insuff,
            '震荡往返%(完整20日为分母)': rt/n_comp*100 if n_comp > 0 else 0,
            '误杀卖飞%(完整20日为分母)': fk/n_comp*100 if n_comp > 0 else 0,
            '有效避损%(完整20日为分母)': al/n_comp*100 if n_comp > 0 else 0,
            '中性%(完整20日为分母)': neu/n_comp*100 if n_comp > 0 else 0,
            '任意未来买回率%': rebought_any/n*100 if n > 0 else 0,
            '20日内买回率%': rebought_20d/n*100 if n > 0 else 0,
            '20日内震荡往返率%': round_trip_20d/n*100 if n > 0 else 0,
            '全部买回-往返佣金总额': total_round_trip_commission_all,
            '全部买回-往返佣金均值': avg_round_trip_commission_all,
            '震荡往返-往返佣金总额': total_rt_commission,
            '震荡往返-往返佣金均值': avg_rt_commission,
        })

    # 2. 按年份
    for year, year_df in df.groupby('year'):
        n = len(year_df)
        n_complete = (year_df['observation_status'] == 'COMPLETE_20D').sum()
        complete_df = year_df[year_df['observation_status'] == 'COMPLETE_20D']
        n_comp = len(complete_df)
        rt = (complete_df['primary'] == '震荡往返').sum() if n_comp > 0 else 0
        fk = (complete_df['primary'] == '误杀卖飞').sum() if n_comp > 0 else 0
        al = (complete_df['primary'] == '有效避损').sum() if n_comp > 0 else 0
        neu = (complete_df['primary'] == '中性').sum() if n_comp > 0 else 0
        insuff = (complete_df['primary'] == '数据不足').sum() if n_comp > 0 else 0

        rebought_any = year_df['rebought_any'].sum()
        rebought_20d = year_df['rebought_20d'].sum()
        round_trip_20d = (complete_df['round_trip_20d'] == True).sum() if n_comp > 0 else 0

        rebought_df = year_df[year_df['rebought_any'] == True]
        total_comm = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).sum() if len(rebought_df) > 0 else 0
        avg_comm = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).mean() if len(rebought_df) > 0 else 0

        rt_df = complete_df[complete_df['primary'] == '震荡往返']
        total_rt_comm = (rt_df['sell_commission'] + rt_df['rebuy_commission']).sum() if len(rt_df) > 0 else 0
        avg_rt_comm = (rt_df['sell_commission'] + rt_df['rebuy_commission']).mean() if len(rt_df) > 0 else 0

        summaries.append({
            '维度': 'year',
            '子维度': str(year),
            '总样本数': n,
            '完整20日': n_complete,
            '分区边界截尾': (year_df['observation_status'] == 'CENSORED_PERIOD_END').sum(),
            '数据截止截尾': (year_df['observation_status'] == 'CENSORED_DATA_END').sum(),
            '无未来数据': (year_df['observation_status'] == 'NO_FUTURE').sum(),
            '震荡往返': rt,
            '误杀卖飞': fk,
            '有效避损': al,
            '中性': neu,
            '数据不足': insuff,
            '震荡往返%(完整20日为分母)': rt/n_comp*100 if n_comp > 0 else 0,
            '误杀卖飞%(完整20日为分母)': fk/n_comp*100 if n_comp > 0 else 0,
            '有效避损%(完整20日为分母)': al/n_comp*100 if n_comp > 0 else 0,
            '中性%(完整20日为分母)': neu/n_comp*100 if n_comp > 0 else 0,
            '任意未来买回率%': rebought_any/n*100 if n > 0 else 0,
            '20日内买回率%': rebought_20d/n*100 if n > 0 else 0,
            '20日内震荡往返率%': round_trip_20d/n*100 if n > 0 else 0,
            '全部买回-往返佣金总额': total_comm,
            '全部买回-往返佣金均值': avg_comm,
            '震荡往返-往返佣金总额': total_rt_comm,
            '震荡往返-往返佣金均值': avg_rt_comm,
        })

    # 3. 按市场状态
    for regime, reg_df in df.groupby('regime_name'):
        if pd.isna(regime):
            continue
        n = len(reg_df)
        n_complete = (reg_df['observation_status'] == 'COMPLETE_20D').sum()
        complete_df = reg_df[reg_df['observation_status'] == 'COMPLETE_20D']
        n_comp = len(complete_df)
        rt = (complete_df['primary'] == '震荡往返').sum() if n_comp > 0 else 0
        fk = (complete_df['primary'] == '误杀卖飞').sum() if n_comp > 0 else 0
        al = (complete_df['primary'] == '有效避损').sum() if n_comp > 0 else 0
        neu = (complete_df['primary'] == '中性').sum() if n_comp > 0 else 0
        insuff = (complete_df['primary'] == '数据不足').sum() if n_comp > 0 else 0

        rebought_any = reg_df['rebought_any'].sum()
        rebought_20d = reg_df['rebought_20d'].sum()
        round_trip_20d = (complete_df['round_trip_20d'] == True).sum() if n_comp > 0 else 0

        rebought_df = reg_df[reg_df['rebought_any'] == True]
        total_comm = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).sum() if len(rebought_df) > 0 else 0
        avg_comm = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).mean() if len(rebought_df) > 0 else 0

        rt_df = complete_df[complete_df['primary'] == '震荡往返']
        total_rt_comm = (rt_df['sell_commission'] + rt_df['rebuy_commission']).sum() if len(rt_df) > 0 else 0
        avg_rt_comm = (rt_df['sell_commission'] + rt_df['rebuy_commission']).mean() if len(rt_df) > 0 else 0

        summaries.append({
            '维度': 'regime',
            '子维度': str(regime),
            '总样本数': n,
            '完整20日': n_complete,
            '分区边界截尾': (reg_df['observation_status'] == 'CENSORED_PERIOD_END').sum(),
            '数据截止截尾': (reg_df['observation_status'] == 'CENSORED_DATA_END').sum(),
            '无未来数据': (reg_df['observation_status'] == 'NO_FUTURE').sum(),
            '震荡往返': rt,
            '误杀卖飞': fk,
            '有效避损': al,
            '中性': neu,
            '数据不足': insuff,
            '震荡往返%(完整20日为分母)': rt/n_comp*100 if n_comp > 0 else 0,
            '误杀卖飞%(完整20日为分母)': fk/n_comp*100 if n_comp > 0 else 0,
            '有效避损%(完整20日为分母)': al/n_comp*100 if n_comp > 0 else 0,
            '中性%(完整20日为分母)': neu/n_comp*100 if n_comp > 0 else 0,
            '任意未来买回率%': rebought_any/n*100 if n > 0 else 0,
            '20日内买回率%': rebought_20d/n*100 if n > 0 else 0,
            '20日内震荡往返率%': round_trip_20d/n*100 if n > 0 else 0,
            '全部买回-往返佣金总额': total_comm,
            '全部买回-往返佣金均值': avg_comm,
            '震荡往返-往返佣金总额': total_rt_comm,
            '震荡往返-往返佣金均值': avg_rt_comm,
        })

    # 4. 按ETF
    for ticker, tdf in df.groupby('ticker'):
        n = len(tdf)
        n_complete = (tdf['observation_status'] == 'COMPLETE_20D').sum()
        complete_df = tdf[tdf['observation_status'] == 'COMPLETE_20D']
        n_comp = len(complete_df)
        rt = (complete_df['primary'] == '震荡往返').sum() if n_comp > 0 else 0
        fk = (complete_df['primary'] == '误杀卖飞').sum() if n_comp > 0 else 0
        al = (complete_df['primary'] == '有效避损').sum() if n_comp > 0 else 0
        neu = (complete_df['primary'] == '中性').sum() if n_comp > 0 else 0
        insuff = (complete_df['primary'] == '数据不足').sum() if n_comp > 0 else 0

        rebought_any = tdf['rebought_any'].sum()
        rebought_20d = tdf['rebought_20d'].sum()
        round_trip_20d = (complete_df['round_trip_20d'] == True).sum() if n_comp > 0 else 0

        rebought_df = tdf[tdf['rebought_any'] == True]
        total_comm = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).sum() if len(rebought_df) > 0 else 0
        avg_comm = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).mean() if len(rebought_df) > 0 else 0

        rt_df = complete_df[complete_df['primary'] == '震荡往返']
        total_rt_comm = (rt_df['sell_commission'] + rt_df['rebuy_commission']).sum() if len(rt_df) > 0 else 0
        avg_rt_comm = (rt_df['sell_commission'] + rt_df['rebuy_commission']).mean() if len(rt_df) > 0 else 0

        summaries.append({
            '维度': 'ticker',
            '子维度': ticker,
            '总样本数': n,
            '完整20日': n_complete,
            '分区边界截尾': (tdf['observation_status'] == 'CENSORED_PERIOD_END').sum(),
            '数据截止截尾': (tdf['observation_status'] == 'CENSORED_DATA_END').sum(),
            '无未来数据': (tdf['observation_status'] == 'NO_FUTURE').sum(),
            '震荡往返': rt,
            '误杀卖飞': fk,
            '有效避损': al,
            '中性': neu,
            '数据不足': insuff,
            '震荡往返%(完整20日为分母)': rt/n_comp*100 if n_comp > 0 else 0,
            '误杀卖飞%(完整20日为分母)': fk/n_comp*100 if n_comp > 0 else 0,
            '有效避损%(完整20日为分母)': al/n_comp*100 if n_comp > 0 else 0,
            '中性%(完整20日为分母)': neu/n_comp*100 if n_comp > 0 else 0,
            '任意未来买回率%': rebought_any/n*100 if n > 0 else 0,
            '20日内买回率%': rebought_20d/n*100 if n > 0 else 0,
            '20日内震荡往返率%': round_trip_20d/n*100 if n > 0 else 0,
            '全部买回-往返佣金总额': total_comm,
            '全部买回-往返佣金均值': avg_comm,
            '震荡往返-往返佣金总额': total_rt_comm,
            '震荡往返-往返佣金均值': avg_rt_comm,
        })

    # 5. 持有时间区间分层
    for label, mask in [
        ('holding_<30d', df['holding_days'] < 30),
        ('holding_30_60d', (df['holding_days'] >= 30) & (df['holding_days'] < 60)),
        ('holding_60_90d', (df['holding_days'] >= 60) & (df['holding_days'] < 90)),
        ('holding_90_120d', (df['holding_days'] >= 90) & (df['holding_days'] < 120)),
        ('holding_>=120d', df['holding_days'] >= 120),
        ('holding_nan', df['holding_days'].isna()),
    ]:
        sub = df[mask]
        if len(sub) == 0:
            continue
        n = len(sub)
        n_complete = (sub['observation_status'] == 'COMPLETE_20D').sum()
        complete_df = sub[sub['observation_status'] == 'COMPLETE_20D']
        n_comp = len(complete_df)
        rt = (complete_df['primary'] == '震荡往返').sum() if n_comp > 0 else 0
        fk = (complete_df['primary'] == '误杀卖飞').sum() if n_comp > 0 else 0
        al = (complete_df['primary'] == '有效避损').sum() if n_comp > 0 else 0
        neu = (complete_df['primary'] == '中性').sum() if n_comp > 0 else 0
        insuff = (complete_df['primary'] == '数据不足').sum() if n_comp > 0 else 0

        rebought_any = sub['rebought_any'].sum()
        rebought_20d = sub['rebought_20d'].sum()
        round_trip_20d = (complete_df['round_trip_20d'] == True).sum() if n_comp > 0 else 0

        summaries.append({
            '维度': 'holding_period',
            '子维度': label,
            '总样本数': n,
            '完整20日': n_complete,
            '分区边界截尾': (sub['observation_status'] == 'CENSORED_PERIOD_END').sum(),
            '数据截止截尾': (sub['observation_status'] == 'CENSORED_DATA_END').sum(),
            '无未来数据': (sub['observation_status'] == 'NO_FUTURE').sum(),
            '震荡往返': rt,
            '误杀卖飞': fk,
            '有效避损': al,
            '中性': neu,
            '数据不足': insuff,
            '震荡往返%(完整20日为分母)': rt/n_comp*100 if n_comp > 0 else 0,
            '误杀卖飞%(完整20日为分母)': fk/n_comp*100 if n_comp > 0 else 0,
            '有效避损%(完整20日为分母)': al/n_comp*100 if n_comp > 0 else 0,
            '中性%(完整20日为分母)': neu/n_comp*100 if n_comp > 0 else 0,
            '任意未来买回率%': rebought_any/n*100 if n > 0 else 0,
            '20日内买回率%': rebought_20d/n*100 if n > 0 else 0,
            '20日内震荡往返率%': round_trip_20d/n*100 if n > 0 else 0,
            '全部买回-往返佣金总额': 0,
            '全部买回-往返佣金均值': 0,
            '震荡往返-往返佣金总额': 0,
            '震荡往返-往返佣金均值': 0,
        })

    # 6. total_score区间分层
    for label, mask in [
        ('score_<0.5', df['total_score_at_exit'] < 0.5),
        ('score_0.5_0.6', (df['total_score_at_exit'] >= 0.5) & (df['total_score_at_exit'] < 0.6)),
        ('score_0.6_0.7', (df['total_score_at_exit'] >= 0.6) & (df['total_score_at_exit'] < 0.7)),
        ('score_0.7_0.8', (df['total_score_at_exit'] >= 0.7) & (df['total_score_at_exit'] < 0.8)),
        ('score_>=0.8', df['total_score_at_exit'] >= 0.8),
        ('score_nan', df['total_score_at_exit'].isna()),
    ]:
        sub = df[mask]
        if len(sub) == 0:
            continue
        n = len(sub)
        n_complete = (sub['observation_status'] == 'COMPLETE_20D').sum()
        complete_df = sub[sub['observation_status'] == 'COMPLETE_20D']
        n_comp = len(complete_df)
        rt = (complete_df['primary'] == '震荡往返').sum() if n_comp > 0 else 0
        fk = (complete_df['primary'] == '误杀卖飞').sum() if n_comp > 0 else 0
        al = (complete_df['primary'] == '有效避损').sum() if n_comp > 0 else 0
        neu = (complete_df['primary'] == '中性').sum() if n_comp > 0 else 0
        insuff = (complete_df['primary'] == '数据不足').sum() if n_comp > 0 else 0

        rebought_any = sub['rebought_any'].sum()
        rebought_20d = sub['rebought_20d'].sum()
        round_trip_20d = (complete_df['round_trip_20d'] == True).sum() if n_comp > 0 else 0

        summaries.append({
            '维度': 'total_score',
            '子维度': label,
            '总样本数': n,
            '完整20日': n_complete,
            '分区边界截尾': (sub['observation_status'] == 'CENSORED_PERIOD_END').sum(),
            '数据截止截尾': (sub['observation_status'] == 'CENSORED_DATA_END').sum(),
            '无未来数据': (sub['observation_status'] == 'NO_FUTURE').sum(),
            '震荡往返': rt,
            '误杀卖飞': fk,
            '有效避损': al,
            '中性': neu,
            '数据不足': insuff,
            '震荡往返%(完整20日为分母)': rt/n_comp*100 if n_comp > 0 else 0,
            '误杀卖飞%(完整20日为分母)': fk/n_comp*100 if n_comp > 0 else 0,
            '有效避损%(完整20日为分母)': al/n_comp*100 if n_comp > 0 else 0,
            '中性%(完整20日为分母)': neu/n_comp*100 if n_comp > 0 else 0,
            '任意未来买回率%': rebought_any/n*100 if n > 0 else 0,
            '20日内买回率%': rebought_20d/n*100 if n > 0 else 0,
            '20日内震荡往返率%': round_trip_20d/n*100 if n > 0 else 0,
            '全部买回-往返佣金总额': 0,
            '全部买回-往返佣金均值': 0,
            '震荡往返-往返佣金总额': 0,
            '震荡往返-往返佣金均值': 0,
        })

    # 7. 信号失效条件分层
    for label, mask in [
        ('ma_false_only', (df['ma_condition'] == False) & (df['multi_condition'] == True)),
        ('multi_false_only', (df['ma_condition'] == True) & (df['multi_condition'] == False)),
        ('both_false', (df['ma_condition'] == False) & (df['multi_condition'] == False)),
        ('ma_true_multi_true', (df['ma_condition'] == True) & (df['multi_condition'] == True)),
        ('condition_unknown', df['ma_condition'].isna() | df['multi_condition'].isna()),
    ]:
        sub = df[mask]
        if len(sub) == 0:
            continue
        n = len(sub)
        n_complete = (sub['observation_status'] == 'COMPLETE_20D').sum()
        complete_df = sub[sub['observation_status'] == 'COMPLETE_20D']
        n_comp = len(complete_df)
        rt = (complete_df['primary'] == '震荡往返').sum() if n_comp > 0 else 0
        fk = (complete_df['primary'] == '误杀卖飞').sum() if n_comp > 0 else 0
        al = (complete_df['primary'] == '有效避损').sum() if n_comp > 0 else 0
        neu = (complete_df['primary'] == '中性').sum() if n_comp > 0 else 0
        insuff = (complete_df['primary'] == '数据不足').sum() if n_comp > 0 else 0

        rebought_any = sub['rebought_any'].sum()
        rebought_20d = sub['rebought_20d'].sum()
        round_trip_20d = (complete_df['round_trip_20d'] == True).sum() if n_comp > 0 else 0

        summaries.append({
            '维度': 'exit_condition',
            '子维度': label,
            '总样本数': n,
            '完整20日': n_complete,
            '分区边界截尾': (sub['observation_status'] == 'CENSORED_PERIOD_END').sum(),
            '数据截止截尾': (sub['observation_status'] == 'CENSORED_DATA_END').sum(),
            '无未来数据': (sub['observation_status'] == 'NO_FUTURE').sum(),
            '震荡往返': rt,
            '误杀卖飞': fk,
            '有效避损': al,
            '中性': neu,
            '数据不足': insuff,
            '震荡往返%(完整20日为分母)': rt/n_comp*100 if n_comp > 0 else 0,
            '误杀卖飞%(完整20日为分母)': fk/n_comp*100 if n_comp > 0 else 0,
            '有效避损%(完整20日为分母)': al/n_comp*100 if n_comp > 0 else 0,
            '中性%(完整20日为分母)': neu/n_comp*100 if n_comp > 0 else 0,
            '任意未来买回率%': rebought_any/n*100 if n > 0 else 0,
            '20日内买回率%': rebought_20d/n*100 if n > 0 else 0,
            '20日内震荡往返率%': round_trip_20d/n*100 if n > 0 else 0,
            '全部买回-往返佣金总额': 0,
            '全部买回-往返佣金均值': 0,
            '震荡往返-往返佣金总额': 0,
            '震荡往返-往返佣金均值': 0,
        })

    summary_df = pd.DataFrame(summaries)
    summary_df.to_csv(os.path.join(output_dir, 'v1_3_step3_exit_summary.csv'), index=False, encoding='utf-8-sig')
    print(f"  Summary CSV saved: {os.path.join(output_dir, 'v1_3_step3_exit_summary.csv')}")
    return summary_df


def generate_report(df, summary_df, output_md):
    """生成Markdown报告"""
    with open(output_md, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 3 v3: 信号失效退出有效性归因报告\n\n")
        f.write(f"> 生成日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 基准: B0.4 (v1.2.3-b0.4, 0bp, 不计滑点)\n")
        f.write(f"> 分析样本: 341笔 BUY_CONDITION_FAILED 退出\n")
        f.write(f"> 口径: T+1开盘成交，有效交易日（非自然日），禁止未来函数\n")
        f.write(f"> 修正: v3（修复时间分区泄漏，未来行情和买回搜索限制在各自期间边界内）\n\n")

        # 1. 数据勾稽
        f.write("## 1. 数据勾稽\n\n")
        total = len(df)
        f.write(f"**总样本**: {total} 笔（必须等于 {TARGET_BCF_COUNT}）\n\n")

        for period_name in ['研究期', '验证期', '样本外']:
            period_df = df[df['period'] == period_name]
            if len(period_df) == 0:
                continue
            n = len(period_df)
            n_complete = (period_df['observation_status'] == 'COMPLETE_20D').sum()
            n_censored_period = (period_df['observation_status'] == 'CENSORED_PERIOD_END').sum()
            n_censored_data = (period_df['observation_status'] == 'CENSORED_DATA_END').sum()
            n_no_future = (period_df['observation_status'] == 'NO_FUTURE').sum()
            f.write(f"**{period_name}**: {n} = 完整20日({n_complete}) + 分区边界截尾({n_censored_period}) + 数据截止截尾({n_censored_data}) + 无未来数据({n_no_future})\n")
            f.write(f"  - 完整20日样本: {n_complete} 笔（可用于20日分类）\n")
            f.write(f"  - 分区边界截尾样本: {n_censored_period} 笔（分区边界内不足20日，主分类=数据不足）\n")
            f.write(f"  - 数据截止截尾样本: {n_censored_data} 笔（数据截止前不足20日，主分类=数据不足）\n")
            f.write(f"  - 无未来数据样本: {n_no_future} 笔（分区边界内无交易日，主分类=数据不足）\n")

            # 主分类合计勾稽（仅完整20日样本）
            complete_df = period_df[period_df['observation_status'] == 'COMPLETE_20D']
            if len(complete_df) > 0:
                cats = complete_df['primary'].value_counts()
                f.write(f"  - 主分类合计（完整20日样本）: {len(complete_df)} = ")
                cat_parts = [f"{cat}({count})" for cat, count in cats.items()]
                f.write(" + ".join(cat_parts) + "\n")
            f.write("\n")

        f.write(f"**events.csv行数**: {len(df)} 笔（必须等于 {TARGET_BCF_COUNT}）\n\n")

        # 全局勾稽
        n_complete_all = (df['observation_status'] == 'COMPLETE_20D').sum()
        n_censored_period_all = (df['observation_status'] == 'CENSORED_PERIOD_END').sum()
        n_censored_data_all = (df['observation_status'] == 'CENSORED_DATA_END').sum()
        n_no_future_all = (df['observation_status'] == 'NO_FUTURE').sum()
        f.write(f"**全局勾稽**: {len(df)} = COMPLETE_20D({n_complete_all}) + CENSORED_PERIOD_END({n_censored_period_all}) + CENSORED_DATA_END({n_censored_data_all}) + NO_FUTURE({n_no_future_all})\n\n")

        # 2. 样本定义
        f.write("## 2. 样本定义\n\n")
        f.write("- **BUY_CONDITION_FAILED 样本数**: 341笔\n")
        f.write("- 分类逻辑复用 Step 1: reason='调出候选列表' 且 signal_type != 'BUY'\n")
        f.write("- 完整保留所有样本，不因无未来行情而跳过\n")
        f.write("- 观察状态: COMPLETE_20D=完整20日观察, CENSORED_PERIOD_END=分区边界截尾(<20日), CENSORED_DATA_END=数据截止截尾(<20日), NO_FUTURE=无未来数据\n\n")

        # 3. 总体分类结果（仅完整20日样本）
        f.write("## 3. 总体分类结果（仅完整20日样本）\n\n")
        for period_name in ['研究期', '验证期', '样本外']:
            period_df = df[df['period'] == period_name]
            complete_df = period_df[period_df['observation_status'] == 'COMPLETE_20D']
            if len(complete_df) == 0:
                continue

            f.write(f"### {period_name}\n\n")
            f.write(f"可用样本: {len(complete_df)} 笔（完整20日观察）\n\n")
            f.write(f"| 分类 | 笔数 | 占比（以完整20日为分母） | 平均20日收益 | 平均20日最大上涨 | 平均20日最大下跌 |\n")
            f.write(f"|------|------|------------------------|-------------|-----------------|-----------------|\n")
            for cat in ['有效避损', '误杀卖飞', '震荡往返', '中性', '数据不足']:
                cat_df = complete_df[complete_df['primary'] == cat]
                n = len(cat_df)
                avg_20d = cat_df['ret_20d'].mean() if n > 0 and cat != '数据不足' else 0
                avg_rise = cat_df['max_rise_20d'].mean() if n > 0 and cat != '数据不足' else 0
                avg_fall = cat_df['max_fall_20d'].mean() if n > 0 and cat != '数据不足' else 0
                f.write(f"| {cat} | {n} | {n/len(complete_df)*100:.1f}% | {avg_20d*100:.2f}% | {avg_rise*100:.2f}% | {avg_fall*100:.2f}% |\n")
            f.write("\n")

        # 4. 重新买回统计（分开输出）
        f.write("## 4. 重新买回统计（分三层）\n\n")
        for period_name in ['研究期', '验证期']:
            period_df = df[df['period'] == period_name]
            if len(period_df) == 0:
                continue

            f.write(f"### {period_name}\n\n")
            n = len(period_df)

            # 任意未来首次买回
            rebought_any = period_df['rebought_any'].sum()
            f.write(f"- **任意未来首次买回**: {rebought_any}/{n} = {rebought_any/n*100:.1f}%\n")

            # 20个交易日内买回
            rebought_20d = period_df['rebought_20d'].sum()
            f.write(f"- **20个交易日内买回**: {rebought_20d}/{n} = {rebought_20d/n*100:.1f}%\n")

            # 20日内且买回价>=卖出价的震荡往返
            round_trip_20d = (period_df['round_trip_20d'] == True).sum()
            f.write(f"- **20日内且买回价>=卖出价的震荡往返**: {round_trip_20d}/{n} = {round_trip_20d/n*100:.1f}%\n")

            # 平均间隔（仅20日内买回的样本）
            rebought_20d_df = period_df[period_df['rebought_20d'] == True]
            if len(rebought_20d_df) > 0:
                avg_days = rebought_20d_df['days_to_rebuy'].mean()
                f.write(f"- 20日内买回平均间隔: {avg_days:.1f} 个交易日\n")

            # 任意买回平均间隔
            rebought_any_df = period_df[period_df['rebought_any'] == True]
            if len(rebought_any_df) > 0:
                avg_days_all = rebought_any_df['days_to_rebuy'].mean()
                f.write(f"- 任意买回平均间隔: {avg_days_all:.1f} 个交易日\n")

            f.write("\n")

        # 5. 往返佣金
        f.write("## 5. 往返佣金统计（sell_commission + rebuy_commission）\n\n")
        for period_name in ['研究期', '验证期']:
            period_df = df[df['period'] == period_name]
            if len(period_df) == 0:
                continue

            f.write(f"### {period_name}\n\n")

            # 全部买回样本
            rebought_df = period_df[period_df['rebought_any'] == True]
            if len(rebought_df) > 0:
                total_comm = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).sum()
                avg_comm = (rebought_df['sell_commission'] + rebought_df['rebuy_commission']).mean()
                f.write(f"- **全部买回样本**（{len(rebought_df)}笔）:\n")
                f.write(f"  - 往返佣金总额: {total_comm:.2f} 元\n")
                f.write(f"  - 往返佣金均值: {avg_comm:.2f} 元/笔\n")
            else:
                f.write(f"- **全部买回样本**: 0 笔\n")

            # 震荡往返样本
            complete_df = period_df[period_df['observation_status'] == 'COMPLETE_20D']
            rt_df = complete_df[complete_df['primary'] == '震荡往返']
            if len(rt_df) > 0:
                total_rt_comm = (rt_df['sell_commission'] + rt_df['rebuy_commission']).sum()
                avg_rt_comm = (rt_df['sell_commission'] + rt_df['rebuy_commission']).mean()
                f.write(f"- **震荡往返样本**（{len(rt_df)}笔）:\n")
                f.write(f"  - 往返佣金总额: {total_rt_comm:.2f} 元\n")
                f.write(f"  - 往返佣金均值: {avg_rt_comm:.2f} 元/笔\n")
            else:
                f.write(f"- **震荡往返样本**: 0 笔\n")

            f.write("\n")

        # 6. 分层分析
        f.write("## 6. 分层分析\n\n")
        f.write("详见 `v1_3_step3_exit_summary.csv`\n\n")
        f.write("### 6.1 持有时间区间\n\n")
        f.write("| 持有时间区间 | 样本数 | 完整20日 | 震荡往返 | 误杀卖飞 | 有效避损 | 中性 | 数据不足 |\n")
        f.write("|-------------|--------|---------|---------|---------|---------|------|---------|\n")
        for label in ['holding_<30d', 'holding_30_60d', 'holding_60_90d', 'holding_90_120d', 'holding_>=120d', 'holding_nan']:
            sub = summary_df[(summary_df['维度'] == 'holding_period') & (summary_df['子维度'] == label)]
            if len(sub) == 0:
                continue
            row = sub.iloc[0]
            f.write(f"| {label} | {row['总样本数']} | {row['完整20日']} | {row['震荡往返']} | {row['误杀卖飞']} | {row['有效避损']} | {row['中性']} | {row['数据不足']} |\n")
        f.write("\n")

        f.write("### 6.2 退出时total_score区间\n\n")
        f.write("| total_score区间 | 样本数 | 完整20日 | 震荡往返 | 误杀卖飞 | 有效避损 | 中性 | 数据不足 |\n")
        f.write("|----------------|--------|---------|---------|---------|---------|------|---------|\n")
        for label in ['score_<0.5', 'score_0.5_0.6', 'score_0.6_0.7', 'score_0.7_0.8', 'score_>=0.8', 'score_nan']:
            sub = summary_df[(summary_df['维度'] == 'total_score') & (summary_df['子维度'] == label)]
            if len(sub) == 0:
                continue
            row = sub.iloc[0]
            f.write(f"| {label} | {row['总样本数']} | {row['完整20日']} | {row['震荡往返']} | {row['误杀卖飞']} | {row['有效避损']} | {row['中性']} | {row['数据不足']} |\n")
        f.write("\n")

        f.write("### 6.3 信号失效条件\n\n")
        f.write("**原因说明**：当前 `scores_df` 中不存在 `ma_condition` 和 `multi_condition` 列。")
        f.write("`generate_signals` 方法使用 `total_score` 和 `market_signal` 生成 `signal_type`，")
        f.write("未将具体失效条件（如均线跌破、动量不足、综合得分下降等）作为独立列输出。")
        f.write("因此无法从现有输出中可靠反推每笔退出的具体信号失效原因。")
        f.write("如需详细信号失效条件，需在 `generate_signals` 中记录更详细的日志。\n\n")
        f.write("当前所有样本标记为 `condition_unknown`。\n\n")
        f.write("| 失效条件 | 样本数 | 完整20日 | 震荡往返 | 误杀卖飞 | 有效避损 | 中性 | 数据不足 |\n")
        f.write("|---------|--------|---------|---------|---------|---------|------|---------|\n")
        for label in ['ma_false_only', 'multi_false_only', 'both_false', 'ma_true_multi_true', 'condition_unknown']:
            sub = summary_df[(summary_df['维度'] == 'exit_condition') & (summary_df['子维度'] == label)]
            if len(sub) == 0:
                continue
            row = sub.iloc[0]
            f.write(f"| {label} | {row['总样本数']} | {row['完整20日']} | {row['震荡往返']} | {row['误杀卖飞']} | {row['有效避损']} | {row['中性']} | {row['数据不足']} |\n")
        f.write("\n")

        # 7. 已知限制与假设
        f.write("## 7. 已知限制与假设\n\n")
        f.write("- **假设**：退出后收益使用该ETF自身未来价格，不假设可组合实现。\n")
        f.write("- **假设**：重新买回定义为同一ETF的后续首次BUY，不假设必然执行。\n")
        f.write("- **假设**：分类阈值（20日收益±3%、20日最大涨跌±5%）为预设规则，未根据数据反向调整。\n")
        f.write("- **约束**：只有 COMPLETE_20D 样本才参与20日收益、最大涨跌、误杀卖飞、有效避损分类。\n")
        f.write('- **约束**：CENSORED_PERIOD_END / CENSORED_DATA_END / NO_FUTURE 样本主分类强制为"数据不足"，不根据部分窗口判断。\n')
        f.write("- **约束**：20日分类以完整20日样本为分母，已披露排除数量。\n")
        f.write("- **约束**：时间分区边界——研究期未来行情和买回搜索截止2022-12-31，验证期截止2024-12-31，样本外截止2026-06-18。\n")
        f.write("- **限制**：样本外（2025-2026）仅列出，不参与结论。\n")
        f.write("- **禁止未来函数**：所有计算均基于卖出日期之前已知的信号和价格。\n\n")

        # 8. 决策建议
        f.write("## 8. 决策建议\n\n")
        research_df = df[df['period'] == '研究期']
        valid_df = df[df['period'] == '验证期']

        if len(research_df) > 0 and len(valid_df) > 0:
            research_complete = research_df[research_df['observation_status'] == 'COMPLETE_20D']
            valid_complete = valid_df[valid_df['observation_status'] == 'COMPLETE_20D']

            if len(research_complete) > 0 and len(valid_complete) > 0:
                rt_r = (research_complete['primary'] == '震荡往返').sum() / len(research_complete)
                fk_r = (research_complete['primary'] == '误杀卖飞').sum() / len(research_complete)
                rt_v = (valid_complete['primary'] == '震荡往返').sum() / len(valid_complete)
                fk_v = (valid_complete['primary'] == '误杀卖飞').sum() / len(valid_complete)

                f.write(f"| 指标 | 研究期 | 验证期 | 两期均>{int(THRESHOLD_RT*100)}%? |\n")
                f.write(f"|------|--------|--------|------------------|\n")
                f.write(f"| 震荡往返比例 | {rt_r*100:.1f}% | {rt_v*100:.1f}% | {'是' if rt_r > THRESHOLD_RT and rt_v > THRESHOLD_RT else '否'} |\n")
                f.write(f"| 误杀卖飞比例 | {fk_r*100:.1f}% | {fk_v*100:.1f}% | {'是' if fk_r > THRESHOLD_FK and fk_v > THRESHOLD_FK else '否'} |\n")
                f.write(f"| 有效避损比例 | {(research_complete['primary'] == '有效避损').sum()/len(research_complete)*100:.1f}% | {(valid_complete['primary'] == '有效避损').sum()/len(valid_complete)*100:.1f}% | — |\n\n")

                if rt_r > THRESHOLD_RT and rt_v > THRESHOLD_RT:
                    f.write(f"**结论**：研究期和验证期震荡往返比例均>{int(THRESHOLD_RT*100)}%（研究期{rt_r*100:.1f}%，验证期{rt_v*100:.1f}%），")
                    f.write(f"是支持 holding stability 实验的主要证据。\n")
                else:
                    f.write(f"**结论**：震荡往返比例未两期均>{int(THRESHOLD_RT*100)}%，不成立 holding stability 建议。\n")

                if fk_r > THRESHOLD_FK and fk_v > THRESHOLD_FK:
                    f.write(f"误杀卖飞比例两期均>{int(THRESHOLD_FK*100)}%（研究期{fk_r*100:.1f}%，验证期{fk_v*100:.1f}%），")
                    f.write(f"也支持 holding stability 实验。\n")
                else:
                    f.write(f"误杀卖飞比例不满足两期均>{int(THRESHOLD_FK*100)}%（研究期{fk_r*100:.1f}%，验证期{fk_v*100:.1f}%），")
                    f.write(f"不能作为 holding stability 的独立证据。\n")

        f.write("\n**注意**：本阶段只做observer诊断，不修改策略、参数或冻结基线。\n")

    print(f"  Report saved: {output_md}")


def main():
    print("=" * 70)
    print("v1.3 Step 3 v2: 信号失效退出有效性归因")
    print("=" * 70)

    # 1. 运行B0.4回测
    print("\n[1/6] 运行B0.4回测...")
    result, market_df, bench_df, cfg = run_b0_4_backtest()
    trades_df = result['trades_df']
    nav_df = result['nav_df']
    print(f"  总交易: {len(trades_df)} 笔")

    # 2. 生成每日信号
    print("\n[2/6] 生成每日信号...")
    scores_df = generate_daily_signals(market_df, bench_df, cfg)
    print(f"  信号记录: {len(scores_df)} 行")

    # 3. 提取BUY_CONDITION_FAILED
    print("\n[3/6] 提取BUY_CONDITION_FAILED样本...")
    bcf_df = get_bcf_exits(trades_df, scores_df)
    print(f"  BUY_CONDITION_FAILED: {len(bcf_df)} 笔")
    assert len(bcf_df) == TARGET_BCF_COUNT, f"样本数应为{TARGET_BCF_COUNT}，实际{len(bcf_df)}"
    print(f"  OK 样本数验证通过: {TARGET_BCF_COUNT}")

    # 4. 计算退出后表现
    print("\n[4/6] 计算退出后表现...")
    perf_df = compute_post_exit_performance(bcf_df, market_df, trades_df, scores_df)
    print(f"  分析完成: {len(perf_df)} 笔")
    assert len(perf_df) == TARGET_BCF_COUNT, f"输出笔数应为{TARGET_BCF_COUNT}，实际{len(perf_df)}"
    print(f"  OK 输出笔数验证通过: {TARGET_BCF_COUNT}")

    # 5. 分类
    print("\n[5/6] 分类...")
    class_df = classify_exits(perf_df)
    perf_df = pd.concat([perf_df.reset_index(drop=True), class_df], axis=1)

    # 添加时期和状态
    perf_df = add_period_and_regime(perf_df, nav_df)

    # 保存事件明细
    output_dir = os.path.join(BASE_DIR, 'reports')
    os.makedirs(output_dir, exist_ok=True)
    events_csv = os.path.join(output_dir, 'v1_3_step3_exit_events.csv')
    perf_df.to_csv(events_csv, index=False, encoding='utf-8-sig')
    print(f"  Events CSV: {events_csv}")

    # 6. 分层汇总与报告
    print("\n[6/6] 生成分层汇总与报告...")
    summary_df = generate_summary(perf_df, output_dir)
    output_md = os.path.join(output_dir, 'v1_3_step3_exit_effectiveness.md')
    generate_report(perf_df, summary_df, output_md)

    print("\n" + "=" * 70)
    print("v1.3 Step 3 v2 完成")
    print("=" * 70)

    return perf_df, summary_df


if __name__ == '__main__':
    main()
