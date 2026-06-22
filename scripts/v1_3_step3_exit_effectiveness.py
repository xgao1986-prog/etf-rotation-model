#!/usr/bin/env python3
"""
v1.3 Step 3: 信号失效退出有效性归因

目标：分析B0.4中341笔因买入条件失效产生的退出，判断它们是在有效避损，
还是造成卖飞和短期往返。

约束：
- 仅做observer诊断，不修改策略、参数、ETF池、数据库、调仓引擎或B0.4冻结基线
- 复用Step 1已经验证的退出分类
- 仅分析BUY_CONDITION_FAILED退出（341笔）
- 研究期：2019-2022；验证期：2023-2024；2025-2026只列为样本外
- 使用有效交易日（非自然日）
- 禁止未来函数
- T日信号、T+1开盘成交口径
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
from collections import defaultdict, Counter

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

AS_OF_DATE = '2026-06-18'


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


def compute_post_exit_performance(bcf_df, market_df, trades_df):
    """
    计算每笔退出后的表现（使用有效交易日，非自然日）。
    
    对于每笔退出，计算：
    - 5个交易日收益（open-to-open）
    - 10个交易日收益
    - 20个交易日收益
    - 期间最大上涨
    - 期间最大下跌
    
    使用T+1成交口径：卖出价格就是实际成交价（已在trades_df中）。
    未来收益从卖出日期后的第一个交易日开始计算。
    """
    market_df = market_df.copy().sort_values(['ticker', 'date'])
    market_df['date'] = pd.to_datetime(market_df['date']).dt.date
    trades_df = trades_df.copy()
    trades_df['date'] = pd.to_datetime(trades_df['date']).dt.date

    results = []

    for _, row in bcf_df.iterrows():
        sell_date = row['date']
        ticker = row['ticker']
        sell_price = row['price']
        shares = row['shares']
        pnl_pct = row['pnl_pct']
        reason = row['reason']
        amount = row['amount']
        commission = row['commission']

        # 获取该ETF的交易日序列
        tdf = market_df[market_df['ticker'] == ticker].sort_values('date').reset_index(drop=True)
        if tdf.empty:
            continue

        # 找到卖出日期后的第一个交易日索引
        sell_idx = tdf[tdf['date'] == sell_date].index
        if len(sell_idx) == 0:
            # 卖出日期可能不在market_df中（取最接近的后续日期）
            future_dates = tdf[tdf['date'] > sell_date]
            if future_dates.empty:
                continue
            start_idx = future_dates.index[0]
        else:
            start_idx = sell_idx[0] + 1  # 卖出后第一个交易日

        if start_idx >= len(tdf):
            continue

        # 获取未来交易日序列
        future_tdf = tdf.iloc[start_idx:].copy()
        n_future = len(future_tdf)

        # 计算各窗口收益（使用open价，T+1口径）
        ret_5d = np.nan
        ret_10d = np.nan
        ret_20d = np.nan
        max_rise = np.nan
        max_fall = np.nan
        data_insufficient = []

        if n_future >= 5:
            price_5 = future_tdf['open'].iloc[4]
            ret_5d = price_5 / sell_price - 1
        else:
            data_insufficient.append('5d')

        if n_future >= 10:
            price_10 = future_tdf['open'].iloc[9]
            ret_10d = price_10 / sell_price - 1
        else:
            data_insufficient.append('10d')

        if n_future >= 20:
            price_20 = future_tdf['open'].iloc[19]
            ret_20d = price_20 / sell_price - 1
            # 最大上涨/下跌（在20个交易日内）
            max_price = future_tdf['high'].iloc[:20].max()
            min_price = future_tdf['low'].iloc[:20].min()
            max_rise = max_price / sell_price - 1
            max_fall = min_price / sell_price - 1
        else:
            data_insufficient.append('20d')
            if n_future > 0:
                max_price = future_tdf['high'].max()
                min_price = future_tdf['low'].min()
                max_rise = max_price / sell_price - 1
                max_fall = min_price / sell_price - 1

        # 查找重新买回
        rebought = False
        rebuy_date = None
        rebuy_price = np.nan
        days_to_rebuy = np.nan
        rebuy_pnl = np.nan
        rebuy_spread = np.nan
        rebuy_commission = np.nan

        ticker_buys = trades_df[(trades_df['ticker'] == ticker) & (trades_df['action'] == 'BUY') & (trades_df['date'] > sell_date)]
        if not ticker_buys.empty:
            rebuy_row = ticker_buys.sort_values('date').iloc[0]
            rebought = True
            rebuy_date = rebuy_row['date']
            rebuy_price = rebuy_row['price']
            rebuy_shares = rebuy_row['shares']
            rebuy_commission = rebuy_row['commission']
            # 计算间隔交易日数（从卖出日期的下一个交易日到买回日期的交易日）
            rebuy_idx = tdf[tdf['date'] == rebuy_date].index
            if len(rebuy_idx) > 0 and start_idx < len(tdf):
                days_to_rebuy = rebuy_idx[0] - start_idx + 1
            else:
                days_to_rebuy = np.nan
            rebuy_pnl = rebuy_price / sell_price - 1
            rebuy_spread = rebuy_price - sell_price
        else:
            rebuy_date = '未买回'

        # 存储结果
        results.append({
            'sell_date': sell_date,
            'ticker': ticker,
            'sell_price': sell_price,
            'shares': shares,
            'amount': amount,
            'commission': commission,
            'pnl_pct': pnl_pct,
            'ret_5d': ret_5d,
            'ret_10d': ret_10d,
            'ret_20d': ret_20d,
            'max_rise': max_rise,
            'max_fall': max_fall,
            'data_insufficient': ','.join(data_insufficient) if data_insufficient else '无',
            'rebuy_date': rebuy_date,
            'rebuy_price': rebuy_price,
            'days_to_rebuy': days_to_rebuy,
            'rebuy_pnl': rebuy_pnl,
            'rebuy_spread': rebuy_spread,
            'rebuy_commission': rebuy_commission,
        })

    return pd.DataFrame(results)


def classify_exits(df):
    """
    按预设规则分类，不根据结果反向修改。
    
    优先级：震荡往返 > 误杀卖飞 > 有效避损 > 中性
    """
    classifications = []
    for _, row in df.iterrows():
        ret_20d = row['ret_20d']
        max_rise = row['max_rise']
        max_fall = row['max_fall']
        rebuy_date = row['rebuy_date']
        days_to_rebuy = row['days_to_rebuy']
        rebuy_price = row['rebuy_price']
        sell_price = row['sell_price']
        rebuy_commission = row['rebuy_commission']

        # 判断各分类条件
        is_round_trip = False
        is_false_kill = False
        is_avoid_loss = False

        # 震荡往返：20日内重新买回，且买回价>=卖出价，并产生额外双边佣金
        if rebuy_date != '未买回' and not pd.isna(days_to_rebuy) and days_to_rebuy <= 20:
            if not pd.isna(rebuy_price) and not pd.isna(sell_price) and rebuy_price >= sell_price:
                if not pd.isna(rebuy_commission) and rebuy_commission > 0:
                    is_round_trip = True

        # 误杀卖飞：20日收益>=+3% 或 最大上涨>=+5%
        if not pd.isna(ret_20d) and ret_20d >= 0.03:
            is_false_kill = True
        elif not pd.isna(max_rise) and max_rise >= 0.05:
            is_false_kill = True

        # 有效避损：20日收益<=-3% 或 最大下跌<=-5%
        if not pd.isna(ret_20d) and ret_20d <= -0.03:
            is_avoid_loss = True
        elif not pd.isna(max_fall) and max_fall <= -0.05:
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

    # 合并市场状态
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

    # 1. 总体
    total = len(df)
    for period_name, period_df in df.groupby('period'):
        if period_name == '样本外':
            continue
        n = len(period_df)
        rt = (period_df['primary'] == '震荡往返').sum()
        fk = (period_df['primary'] == '误杀卖飞').sum()
        al = (period_df['primary'] == '有效避损').sum()
        neu = (period_df['primary'] == '中性').sum()
        rebuy_rate = period_df['rebuy_date'].apply(lambda x: x != '未买回').mean()
        avg_rebuy_pnl = period_df['rebuy_pnl'].mean()
        avg_commission = period_df['rebuy_commission'].mean()

        summaries.append({
            '维度': 'period', '子维度': period_name, '样本数': n,
            '震荡往返': rt, '误杀卖飞': fk, '有效避损': al, '中性': neu,
            '震荡往返%': rt/n*100, '误杀卖飞%': fk/n*100, '有效避损%': al/n*100, '中性%': neu/n*100,
            '重新买回率%': rebuy_rate*100, '平均买回收益%': avg_rebuy_pnl*100 if not pd.isna(avg_rebuy_pnl) else 0,
            '平均佣金': avg_commission if not pd.isna(avg_commission) else 0,
        })

    # 2. 按年份
    for year, year_df in df.groupby('year'):
        n = len(year_df)
        rt = (year_df['primary'] == '震荡往返').sum()
        fk = (year_df['primary'] == '误杀卖飞').sum()
        al = (year_df['primary'] == '有效避损').sum()
        neu = (year_df['primary'] == '中性').sum()
        summaries.append({
            '维度': 'year', '子维度': str(year), '样本数': n,
            '震荡往返': rt, '误杀卖飞': fk, '有效避损': al, '中性': neu,
            '震荡往返%': rt/n*100, '误杀卖飞%': fk/n*100, '有效避损%': al/n*100, '中性%': neu/n*100,
            '重新买回率%': year_df['rebuy_date'].apply(lambda x: x != '未买回').mean()*100,
            '平均买回收益%': year_df['rebuy_pnl'].mean()*100 if not pd.isna(year_df['rebuy_pnl'].mean()) else 0,
            '平均佣金': year_df['rebuy_commission'].mean() if not pd.isna(year_df['rebuy_commission'].mean()) else 0,
        })

    # 3. 按市场状态
    for regime, reg_df in df.groupby('regime_name'):
        if pd.isna(regime):
            continue
        n = len(reg_df)
        if n == 0:
            continue
        rt = (reg_df['primary'] == '震荡往返').sum()
        fk = (reg_df['primary'] == '误杀卖飞').sum()
        al = (reg_df['primary'] == '有效避损').sum()
        neu = (reg_df['primary'] == '中性').sum()
        summaries.append({
            '维度': 'regime', '子维度': str(regime), '样本数': n,
            '震荡往返': rt, '误杀卖飞': fk, '有效避损': al, '中性': neu,
            '震荡往返%': rt/n*100, '误杀卖飞%': fk/n*100, '有效避损%': al/n*100, '中性%': neu/n*100,
            '重新买回率%': reg_df['rebuy_date'].apply(lambda x: x != '未买回').mean()*100,
            '平均买回收益%': reg_df['rebuy_pnl'].mean()*100 if not pd.isna(reg_df['rebuy_pnl'].mean()) else 0,
            '平均佣金': reg_df['rebuy_commission'].mean() if not pd.isna(reg_df['rebuy_commission'].mean()) else 0,
        })

    # 4. 按ETF
    for ticker, tdf in df.groupby('ticker'):
        n = len(tdf)
        rt = (tdf['primary'] == '震荡往返').sum()
        fk = (tdf['primary'] == '误杀卖飞').sum()
        al = (tdf['primary'] == '有效避损').sum()
        neu = (tdf['primary'] == '中性').sum()
        summaries.append({
            '维度': 'ticker', '子维度': ticker, '样本数': n,
            '震荡往返': rt, '误杀卖飞': fk, '有效避损': al, '中性': neu,
            '震荡往返%': rt/n*100, '误杀卖飞%': fk/n*100, '有效避损%': al/n*100, '中性%': neu/n*100,
            '重新买回率%': tdf['rebuy_date'].apply(lambda x: x != '未买回').mean()*100,
            '平均买回收益%': tdf['rebuy_pnl'].mean()*100 if not pd.isna(tdf['rebuy_pnl'].mean()) else 0,
            '平均佣金': tdf['rebuy_commission'].mean() if not pd.isna(tdf['rebuy_commission'].mean()) else 0,
        })

    summary_df = pd.DataFrame(summaries)
    summary_df.to_csv(os.path.join(output_dir, 'v1_3_step3_exit_summary.csv'), index=False, encoding='utf-8-sig')
    print(f"  Summary CSV saved: {os.path.join(output_dir, 'v1_3_step3_exit_summary.csv')}")
    return summary_df


def generate_report(df, summary_df, output_md):
    """生成Markdown报告"""
    with open(output_md, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 3: 信号失效退出有效性归因报告\n\n")
        f.write(f"> 生成日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 基准: B0.4 (v1.2.3-b0.4, 0bp, 不计滑点)\n")
        f.write(f"> 分析样本: 341笔 BUY_CONDITION_FAILED 退出\n")
        f.write(f"> 口径: T+1开盘成交，有效交易日（非自然日），禁止未来函数\n\n")

        f.write("## 1. 样本定义与勾稽\n\n")
        f.write(f"- **BUY_CONDITION_FAILED 样本数**: {len(df)}\n")
        f.write("- 分类逻辑复用 Step 1: reason='调出候选列表' 且 signal_type != 'BUY'\n")
        f.write("- 目标: 341笔 ✓\n\n")

        f.write("## 2. 总体分类结果\n\n")
        total = len(df)
        for period_name in ['研究期', '验证期', '样本外']:
            period_df = df[df['period'] == period_name]
            if len(period_df) == 0:
                continue
            f.write(f"### {period_name}\n\n")
            f.write(f"| 分类 | 笔数 | 占比 | 平均20日收益 | 平均最大上涨 | 平均最大下跌 |\n")
            f.write(f"|------|------|------|-------------|-------------|-------------|\n")
            for cat in ['有效避损', '误杀卖飞', '震荡往返', '中性']:
                cat_df = period_df[period_df['primary'] == cat]
                n = len(cat_df)
                avg_20d = cat_df['ret_20d'].mean() if n > 0 else 0
                avg_rise = cat_df['max_rise'].mean() if n > 0 else 0
                avg_fall = cat_df['max_fall'].mean() if n > 0 else 0
                f.write(f"| {cat} | {n} | {n/len(period_df)*100:.1f}% | {avg_20d*100:.2f}% | {avg_rise*100:.2f}% | {avg_fall*100:.2f}% |\n")
            f.write("\n")

        f.write("## 3. 重新买回统计\n\n")
        for period_name in ['研究期', '验证期']:
            period_df = df[df['period'] == period_name]
            if len(period_df) == 0:
                continue
            rebought = period_df['rebuy_date'].apply(lambda x: x != '未买回').sum()
            not_rebought = (period_df['rebuy_date'] == '未买回').sum()
            avg_days = period_df['days_to_rebuy'].mean()
            avg_rebuy_pnl = period_df['rebuy_pnl'].mean()
            f.write(f"**{period_name}**: {rebought}笔重新买回 ({rebought/len(period_df)*100:.1f}%), {not_rebought}笔未买回\n")
            f.write(f"- 平均买回间隔: {avg_days:.1f}个交易日\n")
            f.write(f"- 平均买回价差收益: {avg_rebuy_pnl*100:.2f}%\n")
            f.write(f"- 平均往返佣金: {period_df['rebuy_commission'].mean():.2f}元\n\n")

        f.write("## 4. 分层分析\n\n")
        f.write("详见 `v1_3_step3_exit_summary.csv`\n\n")

        f.write("## 5. 已知限制与假设\n\n")
        f.write("- **假设**：退出后收益使用该ETF自身未来价格，不假设可组合实现。\n")
        f.write("- **假设**：重新买回定义为同一ETF的后续首次BUY，不假设必然执行。\n")
        f.write("- **假设**：分类阈值（20日收益±3%、最大涨跌±5%）为预设规则，未根据数据反向调整。\n")
        f.write("- **限制**：行情不足5/10/20日的样本已标记为数据不足。\n")
        f.write("- **限制**：样本外（2025-2026）仅列出，不参与结论。\n")
        f.write("- **禁止未来函数**：所有计算均基于卖出日期之前已知的信号和价格。\n\n")

        f.write("## 6. 决策建议\n\n")
        # 研究期vs验证期对比
        research_df = df[df['period'] == '研究期']
        valid_df = df[df['period'] == '验证期']

        if len(research_df) > 0 and len(valid_df) > 0:
            rt_r = (research_df['primary'] == '震荡往返').sum() / len(research_df)
            fk_r = (research_df['primary'] == '误杀卖飞').sum() / len(research_df)
            rt_v = (valid_df['primary'] == '震荡往返').sum() / len(valid_df)
            fk_v = (valid_df['primary'] == '误杀卖飞').sum() / len(valid_df)

            f.write(f"| 指标 | 研究期 | 验证期 | 方向一致？|\n")
            f.write(f"|------|--------|--------|-----------|\n")
            f.write(f"| 震荡往返比例 | {rt_r*100:.1f}% | {rt_v*100:.1f}% | {'是' if (rt_r > 0.1 and rt_v > 0.1) or (rt_r <= 0.1 and rt_v <= 0.1) else '否'} |\n")
            f.write(f"| 误杀卖飞比例 | {fk_r*100:.1f}% | {fk_v*100:.1f}% | {'是' if (fk_r > 0.1 and fk_v > 0.1) or (fk_r <= 0.1 and fk_v <= 0.1) else '否'} |\n")
            f.write(f"| 有效避损比例 | {(research_df['primary'] == '有效避损').sum()/len(research_df)*100:.1f}% | {(valid_df['primary'] == '有效避损').sum()/len(valid_df)*100:.1f}% | — |\n\n")

            if fk_r > 0.15 and fk_v > 0.15:
                f.write(f"**结论**：研究期和验证期均显示较高误杀卖飞比例（>{15}%），建议下一步设计 holding stability 实验。\n")
            elif rt_r > 0.15 and rt_v > 0.15:
                f.write(f"**结论**：研究期和验证期均显示较高震荡往返比例（>{15}%），建议下一步设计 holding stability 实验。\n")
            else:
                f.write(f"**结论**：阶段间差异明显或不一致，只报告异质性，不推荐修改。\n")

        f.write("\n**注意**：本阶段只做observer诊断，不修改策略、参数或冻结基线。\n")

    print(f"  Report saved: {output_md}")


def main():
    print("=" * 70)
    print("v1.3 Step 3: 信号失效退出有效性归因")
    print("=" * 70)

    # 1. 运行B0.4回测
    print("\n[1/5] 运行B0.4回测...")
    result, market_df, bench_df, cfg = run_b0_4_backtest()
    trades_df = result['trades_df']
    nav_df = result['nav_df']
    print(f"  总交易: {len(trades_df)} 笔")

    # 2. 生成每日信号
    print("\n[2/5] 生成每日信号...")
    scores_df = generate_daily_signals(market_df, bench_df, cfg)
    print(f"  信号记录: {len(scores_df)} 行")

    # 3. 提取BUY_CONDITION_FAILED
    print("\n[3/5] 提取BUY_CONDITION_FAILED样本...")
    bcf_df = get_bcf_exits(trades_df, scores_df)
    print(f"  BUY_CONDITION_FAILED: {len(bcf_df)} 笔")
    assert len(bcf_df) == 341, f"样本数应为341，实际{len(bcf_df)}"
    print(f"  OK 样本数验证通过: 341")

    # 4. 计算退出后表现
    print("\n[4/5] 计算退出后表现...")
    perf_df = compute_post_exit_performance(bcf_df, market_df, trades_df)
    print(f"  分析完成: {len(perf_df)} 笔")

    # 5. 分类
    print("\n[5/5] 分类与输出...")
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

    # 生成分层汇总
    summary_df = generate_summary(perf_df, output_dir)

    # 生成报告
    output_md = os.path.join(output_dir, 'v1_3_step3_exit_effectiveness.md')
    generate_report(perf_df, summary_df, output_md)

    print("\n" + "=" * 70)
    print("v1.3 Step 3 完成")
    print("=" * 70)

    return perf_df, summary_df


if __name__ == '__main__':
    main()
