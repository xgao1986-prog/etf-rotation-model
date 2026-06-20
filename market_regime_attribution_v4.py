# -*- coding: utf-8 -*-
"""
market_regime_attribution_v4.py - 逐日逐ETF真实持仓归因（会计式勾稽）

核心口径：
- 对每只ETF，按"日期×ETF"建立持仓账本
- 每日贡献精确分解为：既有持仓浮动 + 新买贡献 + 卖出修正 + 交易成本
- 每日必须满足：所有ETF贡献 + 现金贡献 + 交易成本 = 组合当日NAV变化
- 误差必须接近零
- 每只ETF的浮动盈亏只能归给实际发生浮盈浮亏的当天状态

验收条件：
1. 每日贡献与NAV收益精确勾稽
2. 四个状态的金额贡献合计与全组合净利润精确勾稽

不改策略、不调参数、不改市场状态检测算法。
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import bisect
from collections import defaultdict
from datetime import datetime
from database import ETFDatabase
from backtest import BacktestEngine
from market_regime import MarketRegimeDetector
import config


def load_data():
    db = ETFDatabase()
    tickers = list(config.ETF_UNIVERSE.keys()) + list(config.DEFENSE_UNIVERSE.keys())
    market_dfs = []
    for ticker in tickers:
        df = db.get_market_data(ticker=ticker)
        if not df.empty:
            market_dfs.append(df)
    market_df = pd.concat(market_dfs, ignore_index=True) if market_dfs else pd.DataFrame()
    market_df['date'] = pd.to_datetime(market_df['date'])
    bench_df = db.get_market_data(ticker=config.BENCHMARK)
    bench_df['date'] = pd.to_datetime(bench_df['date'])
    return market_df, bench_df


def merge_with_regime(df, regime_df, date_col='date'):
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    regime_df = regime_df.copy().sort_values('date')
    regime_dates = regime_df['date'].tolist()
    regime_ids = regime_df['regime_id'].tolist()
    regime_names = regime_df['regime_name'].tolist()
    regime_confs = regime_df['confidence'].tolist()

    def find_regime(dt):
        idx = bisect.bisect_right(regime_dates, dt) - 1
        if idx >= 0:
            return regime_ids[idx], regime_names[idx], regime_confs[idx]
        return 3, '震荡', 0.5

    regimes = df[date_col].apply(find_regime)
    df['regime_id'] = regimes.apply(lambda x: x[0])
    df['regime_name'] = regimes.apply(lambda x: x[1])
    df['confidence'] = regimes.apply(lambda x: x[2])
    return df


def detect_regime_history(bench_df, market_df):
    detector = MarketRegimeDetector()
    core_tickers = list(config.ETF_UNIVERSE.keys())
    market_for_breadth = market_df[market_df['ticker'].isin(core_tickers)].copy()
    regime_df = detector.detect_history(bench_df, market_for_breadth)
    regime_df['date'] = pd.to_datetime(regime_df['date'])
    return regime_df


def build_price_map(market_df):
    """构建 {ticker: {date: {'open': ..., 'close': ...}}} 映射"""
    prices = defaultdict(dict)
    for _, row in market_df.iterrows():
        date = row['date']
        ticker = row['ticker']
        prices[ticker][date] = {
            'open': row['open'],
            'close': row['close'],
        }
    return prices


def build_trade_map(trades_df):
    """构建 {date: {ticker: {'buy': shares, 'sell': shares, 'buy_price': price, 'sell_price': price, 'commission': sum}}}"""
    trades = defaultdict(lambda: defaultdict(lambda: {'buy': 0, 'sell': 0, 'buy_price': 0, 'sell_price': 0, 'commission': 0.0}))
    if trades_df.empty or 'action' not in trades_df.columns:
        return trades
    for _, row in trades_df.iterrows():
        date = pd.to_datetime(row['date'])
        ticker = row['ticker']
        action = row['action']
        shares = row['shares']
        price = row['price']
        commission = row.get('commission', 0)
        trades[date][ticker]['commission'] += commission
        if action == 'BUY':
            trades[date][ticker]['buy'] += shares
            trades[date][ticker]['buy_price'] = price  # 同一只ETF同天只买一次，否则后覆盖
        elif action in ('SELL', 'STOP_LOSS'):
            trades[date][ticker]['sell'] += shares
            trades[date][ticker]['sell_price'] = price
    return trades


def calculate_true_daily_attribution(nav_df, trades_map, price_map, regime_df, industry_tickers, defense_tickers):
    """
    逐日计算每只ETF的真实贡献，并验证勾稽。
    
    每日分解：
    - 既有持仓浮动 = shares_{t-1} × (close_t - close_{t-1})
    - 新买贡献 = shares_buy × (close_t - open_t)
    - 卖出修正 = shares_sell × (open_t - close_t)
    - 交易成本 = -commission
    - 现金贡献 = 0
    
    验证：sum = nav_t - nav_{t-1}
    """
    nav_df = nav_df.sort_values('date').reset_index(drop=True)
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    
    # 合并市场状态（按最近日期）
    regime_df = regime_df.sort_values('date').reset_index(drop=True)
    regime_dates = regime_df['date'].tolist()
    
    def get_regime(dt):
        idx = bisect.bisect_right(regime_dates, dt) - 1
        if idx >= 0:
            return regime_df.iloc[idx]['regime_id'], regime_df.iloc[idx]['regime_name']
        return 3, '震荡'
    
    daily_records = []
    max_discrepancy = 0
    max_discrepancy_date = None
    
    for i in range(1, len(nav_df)):
        yest_row = nav_df.iloc[i - 1]
        today_row = nav_df.iloc[i]
        
        yest_date = yest_row['date']
        today_date = today_row['date']
        regime_id, regime_name = get_regime(today_date)
        
        yest_nav = yest_row['nav']
        today_nav = today_row['nav']
        nav_change = today_nav - yest_nav
        
        yest_positions = yest_row.get('positions_detail', {})
        today_trades = trades_map.get(today_date, {})
        
        # 逐ETF计算贡献
        etf_contributions = {}  # {ticker: {'hold': float, 'buy': float, 'sell': float, 'commission': float, 'total': float}}
        state_day_total = 0.0
        
        # 1. 既有持仓浮动（所有昨日持有的ETF）
        for ticker, pos in yest_positions.items():
            shares_yest = pos['shares']
            if shares_yest <= 0:
                continue
            
            p_today = price_map.get(ticker, {}).get(today_date)
            p_yest = price_map.get(ticker, {}).get(yest_date)
            
            # 处理停牌/数据缺失：缺失日期的收盘价视为0（与nav计算一致）
            if p_yest is None or pd.isna(p_yest.get('close')):
                close_yest = 0.0
            else:
                close_yest = p_yest['close']
            
            if p_today is None or pd.isna(p_today.get('close')):
                close_today = 0.0
            else:
                close_today = p_today['close']
            
            hold_contrib = shares_yest * (close_today - close_yest)
            etf_contributions[ticker] = {
                'hold': hold_contrib,
                'buy': 0.0,
                'sell': 0.0,
                'commission': 0.0,
                'total': hold_contrib,
            }
            state_day_total += hold_contrib
        
        # 2. 新买贡献 & 卖出修正 & 交易成本（从交易记录）
        day_commission = 0.0
        for ticker, trade_info in today_trades.items():
            day_commission += trade_info['commission']
            
            p_today = price_map.get(ticker, {}).get(today_date)
            close_today = p_today['close'] if p_today else 0.0
            
            if ticker not in etf_contributions:
                etf_contributions[ticker] = {'hold': 0.0, 'buy': 0.0, 'sell': 0.0, 'commission': 0.0, 'total': 0.0}
            
            # 新买贡献
            if trade_info['buy'] > 0:
                buy_price = trade_info['buy_price']
                if buy_price > 0:
                    buy_contrib = trade_info['buy'] * (close_today - buy_price)
                    etf_contributions[ticker]['buy'] += buy_contrib
                    etf_contributions[ticker]['total'] += buy_contrib
                    state_day_total += buy_contrib
            
            # 卖出修正
            if trade_info['sell'] > 0:
                sell_price = trade_info['sell_price']
                if sell_price > 0:
                    sell_contrib = trade_info['sell'] * (sell_price - close_today)
                    etf_contributions[ticker]['sell'] += sell_contrib
                    etf_contributions[ticker]['total'] += sell_contrib
                    state_day_total += sell_contrib
            
            etf_contributions[ticker]['commission'] += trade_info['commission']
        
        # 3. 交易成本（负贡献）
        total_cost_contrib = -day_commission
        state_day_total += total_cost_contrib
        
        # 勾稽验证
        discrepancy = nav_change - state_day_total
        abs_disc = abs(discrepancy)
        if abs_disc > max_discrepancy:
            max_discrepancy = abs_disc
            max_discrepancy_date = today_date
        
        daily_records.append({
            'date': today_date,
            'regime_id': regime_id,
            'regime_name': regime_name,
            'yest_nav': yest_nav,
            'today_nav': today_nav,
            'nav_change': nav_change,
            'hold_contrib': sum(v['hold'] for v in etf_contributions.values()),
            'buy_contrib': sum(v['buy'] for v in etf_contributions.values()),
            'sell_contrib': sum(v['sell'] for v in etf_contributions.values()),
            'commission_contrib': total_cost_contrib,
            'total_contrib': state_day_total,
            'discrepancy': discrepancy,
            'etf_contributions': etf_contributions,
            'num_etfs': len(etf_contributions),
        })
    
    daily_df = pd.DataFrame(daily_records)
    
    # 调试：打印各分量总和
    print(f"\n  [DEBUG] 归因分量汇总:")
    print(f"    hold_contrib 合计: {daily_df['hold_contrib'].sum():,.2f}")
    print(f"    buy_contrib 合计: {daily_df['buy_contrib'].sum():,.2f}")
    print(f"    sell_contrib 合计: {daily_df['sell_contrib'].sum():,.2f}")
    print(f"    commission 合计: {daily_df['commission_contrib'].sum():,.2f}")
    print(f"    total_contrib 合计: {daily_df['total_contrib'].sum():,.2f}")
    print(f"    nav_change 合计: {daily_df['nav_change'].sum():,.2f}")
    print(f"    nav.iloc[-1] - nav.iloc[0]: {nav_df['nav'].iloc[-1] - nav_df['nav'].iloc[0]:,.2f}")
    print(f"    discrepancy 合计: {daily_df['discrepancy'].sum():,.2f}")
    
    return daily_df, max_discrepancy, max_discrepancy_date


def calculate_equal_weight_pool_return(nav_df, price_map, industry_tickers):
    """
    计算每个交易日的可交易行业ETF池等权收益。
    
    对于每个交易日：
    - 计算所有有数据的行业ETF的当日收益率（close_t / close_{t-1} - 1）
    - 取平均值 = 等权收益率
    - 昨日行业持仓总市值 = 昨日所有行业ETF持仓的市值
    - 同暴露等权池收益 = 昨日行业持仓总市值 × 等权收益率
    """
    nav_df = nav_df.sort_values('date').reset_index(drop=True)
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    
    ew_records = []
    
    for i in range(1, len(nav_df)):
        yest_row = nav_df.iloc[i - 1]
        today_row = nav_df.iloc[i]
        
        yest_date = yest_row['date']
        today_date = today_row['date']
        
        # 昨日行业持仓总市值
        yest_positions = yest_row.get('positions_detail', {})
        yest_industry_value = 0.0
        for ticker, pos in yest_positions.items():
            if ticker in industry_tickers:
                p_yest = price_map.get(ticker, {}).get(yest_date)
                if p_yest and not pd.isna(p_yest['close']):
                    yest_industry_value += pos['shares'] * p_yest['close']
        
        # 所有可交易行业ETF的当日收益率
        returns = []
        for ticker in industry_tickers:
            p_yest = price_map.get(ticker, {}).get(yest_date)
            p_today = price_map.get(ticker, {}).get(today_date)
            if p_yest and p_today and p_yest['close'] > 0 and not pd.isna(p_today['close']) and not pd.isna(p_yest['close']):
                ret = p_today['close'] / p_yest['close'] - 1
                returns.append(ret)
        
        avg_return = np.mean(returns) if returns else 0.0
        ew_contrib = yest_industry_value * avg_return
        
        ew_records.append({
            'date': today_date,
            'yest_industry_value': yest_industry_value,
            'avg_return': avg_return,
            'ew_contrib': ew_contrib,
            'num_active_etfs': len(returns),
        })
    
    return pd.DataFrame(ew_records)


def aggregate_by_state(daily_df, ew_df, industry_tickers, defense_tickers):
    """
    按市场状态汇总贡献。
    """
    # 合并等权池收益
    daily_df = daily_df.merge(ew_df[['date', 'yest_industry_value', 'avg_return', 'ew_contrib', 'num_active_etfs']], on='date', how='left')
    
    state_stats = {}
    
    for regime_id in [1, 2, 3, 4]:
        regime_name = MarketRegimeDetector.STATE_NAMES[regime_id]
        subset = daily_df[daily_df['regime_id'] == regime_id]
        
        if subset.empty:
            state_stats[regime_name] = {
                'trading_days': 0,
                'industry_hold': 0.0,
                'industry_buy': 0.0,
                'industry_sell': 0.0,
                'defense_hold': 0.0,
                'defense_buy': 0.0,
                'defense_sell': 0.0,
                'commission': 0.0,
                'total_contrib': 0.0,
                'ew_contrib': 0.0,
                'selection_alpha': 0.0,
            }
            continue
        
        # 逐日拆解行业/防御贡献
        industry_hold = 0.0
        industry_buy = 0.0
        industry_sell = 0.0
        defense_hold = 0.0
        defense_buy = 0.0
        defense_sell = 0.0
        commission_total = 0.0
        
        for _, row in subset.iterrows():
            etf_contributions = row['etf_contributions']
            for ticker, contrib in etf_contributions.items():
                if ticker in industry_tickers:
                    industry_hold += contrib['hold']
                    industry_buy += contrib['buy']
                    industry_sell += contrib['sell']
                elif ticker in defense_tickers:
                    defense_hold += contrib['hold']
                    defense_buy += contrib['buy']
                    defense_sell += contrib['sell']
            commission_total += row['commission_contrib']
        
        total_contrib = subset['total_contrib'].sum()
        ew_contrib = subset['ew_contrib'].sum()
        selection_alpha = total_contrib - ew_contrib - commission_total  # 实际总贡献 - 等权池 - 交易成本
        # 等等，selection_alpha 应该是 实际行业持仓收益 - 同暴露等权池收益
        # 实际行业持仓收益 = industry_hold + industry_buy + industry_sell
        actual_industry = industry_hold + industry_buy + industry_sell
        selection_alpha = actual_industry - ew_contrib
        
        state_stats[regime_name] = {
            'trading_days': len(subset),
            'industry_hold': industry_hold,
            'industry_buy': industry_buy,
            'industry_sell': industry_sell,
            'defense_hold': defense_hold,
            'defense_buy': defense_buy,
            'defense_sell': defense_sell,
            'commission': commission_total,
            'total_contrib': total_contrib,
            'ew_contrib': ew_contrib,
            'selection_alpha': selection_alpha,
            'actual_industry': actual_industry,
        }
    
    return state_stats, daily_df


def format_report_v4(state_stats, daily_df, total_nav_change, max_disc, max_disc_date, industry_tickers, defense_tickers):
    lines = []
    
    lines.append("# 市场状态归因分析报告（逐日逐ETF真实归因 v4）")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**策略**: ETF轮动 v1.2 基线（{len(industry_tickers)}只行业ETF）")
    lines.append(f"**数据口径**: 前复权，统一数据至2026-06-05")
    lines.append(f"**核心口径**: 逐日逐ETF真实持仓归因，会计式勾稽")
    lines.append(f"**规则**: 连续5日确认切换；不改策略、不调参数、不改市场状态算法")
    
    # 验收条件1：每日勾稽
    lines.append(f"\n## 验收条件1：每日贡献与NAV收益精确勾稽")
    lines.append(f"\n| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总交易日 | {len(daily_df)} |")
    lines.append(f"| 全组合NAV变化合计 | {total_nav_change:,.2f} |")
    lines.append(f"| 每日贡献合计 | {daily_df['total_contrib'].sum():,.2f} |")
    lines.append(f"| 最大单日误差 | {max_disc:,.4f} |")
    lines.append(f"| 最大误差日期 | {max_disc_date.strftime('%Y-%m-%d') if max_disc_date else 'N/A'} |")
    lines.append(f"| 总误差 | {daily_df['discrepancy'].sum():,.4f} |")
    lines.append(f"| 误差均值 | {daily_df['discrepancy'].mean():,.6f} |")
    
    # 验收条件2：四个状态合计与全组合净利润勾稽
    lines.append(f"\n## 验收条件2：四个状态金额贡献合计与全组合净利润勾稽")
    total_state_contrib = sum(s['total_contrib'] for s in state_stats.values())
    lines.append(f"\n| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 全组合NAV变化（净利润） | {total_nav_change:,.2f} |")
    lines.append(f"| 四个状态贡献合计 | {total_state_contrib:,.2f} |")
    lines.append(f"| 状态合计误差 | {total_nav_change - total_state_contrib:,.4f} |")
    
    # 按状态汇总
    lines.append(f"\n## 按市场状态汇总（金额贡献）")
    lines.append(f"\n| 状态 | 交易日数 | 行业既有浮动 | 行业新买贡献 | 行业卖出修正 | 行业合计 | 防御既有浮动 | 防御新买贡献 | 防御卖出修正 | 防御合计 | 交易成本 | 状态总贡献 | 等权池收益 | 轮动选择超额 |")
    lines.append(f"|------|----------|--------------|--------------|--------------|----------|--------------|--------------|--------------|----------|----------|------------|------------|--------------|")
    
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = state_stats.get(name, {})
        if s.get('trading_days', 0) == 0:
            lines.append(f"| {name} | 0 | - | - | - | - | - | - | - | - | - | - | - | - |")
            continue
        industry_total = s['industry_hold'] + s['industry_buy'] + s['industry_sell']
        defense_total = s['defense_hold'] + s['defense_buy'] + s['defense_sell']
        lines.append(
            f"| {name} | {s['trading_days']} | "
            f"{s['industry_hold']:,.2f} | {s['industry_buy']:,.2f} | {s['industry_sell']:,.2f} | {industry_total:,.2f} | "
            f"{s['defense_hold']:,.2f} | {s['defense_buy']:,.2f} | {s['defense_sell']:,.2f} | {defense_total:,.2f} | "
            f"{s['commission']:,.2f} | {s['total_contrib']:,.2f} | {s['ew_contrib']:,.2f} | {s['selection_alpha']:,.2f} |"
        )
    
    # 关键发现
    lines.append(f"\n## 关键发现")
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = state_stats.get(name, {})
        if s.get('trading_days', 0) == 0:
            continue
        lines.append(f"\n### {name}")
        lines.append(f"- 交易日数: {s['trading_days']}")
        lines.append(f"- 行业既有浮动: {s['industry_hold']:,.2f}")
        lines.append(f"- 行业新买贡献: {s['industry_buy']:,.2f}")
        lines.append(f"- 行业卖出修正: {s['industry_sell']:,.2f}")
        lines.append(f"- 行业合计: {s['actual_industry']:,.2f}")
        lines.append(f"- 防御既有浮动: {s['defense_hold']:,.2f}")
        lines.append(f"- 防御新买贡献: {s['defense_buy']:,.2f}")
        lines.append(f"- 防御卖出修正: {s['defense_sell']:,.2f}")
        lines.append(f"- 防御合计: {s['defense_hold']+s['defense_buy']+s['defense_sell']:,.2f}")
        lines.append(f"- 交易成本: {s['commission']:,.2f}")
        lines.append(f"- 状态总贡献: {s['total_contrib']:,.2f}")
        lines.append(f"- 同暴露等权池收益: {s['ew_contrib']:,.2f}")
        lines.append(f"- 轮动选择超额: {s['selection_alpha']:,.2f}")
    
    lines.append(f"\n## 版本边界")
    lines.append(f"- v1.2.2 已收口")
    lines.append(f"- v1.3 研究阶段，逐日逐ETF真实持仓归因")
    lines.append(f"- 不改交易规则")
    lines.append(f"- 不设计自适应规则")
    lines.append(f"- 不改市场状态检测算法")
    
    return '\n'.join(lines)


def main():
    print("="*80)
    print("逐日逐ETF真实持仓归因（v4 - 会计式勾稽）")
    print("="*80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    print("[1/5] 加载数据...")
    market_df, bench_df = load_data()
    print(f"  市场数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只ETF")
    print(f"  基准数据: {len(bench_df)} 行")
    
    industry_tickers = set(config.ETF_UNIVERSE.keys())
    defense_tickers = set(config.DEFENSE_UNIVERSE.keys())

    print("\n[2/5] 运行当前基线回测...")
    engine = BacktestEngine()
    result = engine.run(market_df, bench_df)
    if 'error' in result:
        print(f"  回测失败: {result['error']}")
        return
    nav_df = result['nav_df']
    trades_df = result['trades_df']
    print(f"  总收益: {result['total_return']:.2%}")
    print(f"  最大回撤: {result['max_drawdown']:.2%}")
    print(f"  夏普: {result['sharpe_ratio']:.2f}")
    print(f"  交易次数: {result['num_trades']}")

    print("\n[3/5] 检测市场状态历史...")
    regime_df = detect_regime_history(bench_df, market_df)
    print(f"  状态检测完成: {len(regime_df)} 个交易日")
    state_dist = regime_df['regime_name'].value_counts()
    for name, count in state_dist.items():
        print(f"    {name}: {count} 天 ({count/len(regime_df)*100:.1f}%)")

    print("\n[4/5] 构建价格映射与交易映射...")
    price_map = build_price_map(market_df)
    trades_map = build_trade_map(trades_df)
    print(f"  价格映射: {len(price_map)} 只ETF")
    print(f"  交易映射: {len(trades_map)} 个交易日有交易")

    print("\n[5/5] 计算逐日逐ETF真实归因并验证勾稽...")
    daily_df, max_disc, max_disc_date = calculate_true_daily_attribution(
        nav_df, trades_map, price_map, regime_df, industry_tickers, defense_tickers
    )
    
    # 计算等权池收益
    ew_df = calculate_equal_weight_pool_return(nav_df, price_map, list(industry_tickers))
    
    # 按状态汇总
    state_stats, daily_df = aggregate_by_state(daily_df, ew_df, industry_tickers, defense_tickers)
    
    total_nav_change = nav_df['nav'].iloc[-1] - nav_df['nav'].iloc[0]
    
    print(f"\n  每日勾稽验证:")
    print(f"    全组合NAV变化: {total_nav_change:,.2f}")
    print(f"    每日贡献合计: {daily_df['total_contrib'].sum():,.2f}")
    print(f"    最大单日误差: {max_disc:,.4f} (日期: {max_disc_date.strftime('%Y-%m-%d') if max_disc_date else 'N/A'})")
    print(f"    总误差: {daily_df['discrepancy'].sum():,.4f}")
    
    total_state_contrib = sum(s['total_contrib'] for s in state_stats.values())
    print(f"\n  状态勾稽验证:")
    print(f"    四个状态合计: {total_state_contrib:,.2f}")
    print(f"    状态合计误差: {total_nav_change - total_state_contrib:,.4f}")

    print("\n生成报告...")
    report = format_report_v4(
        state_stats, daily_df, total_nav_change, max_disc, max_disc_date,
        industry_tickers, defense_tickers
    )
    
    report_path = 'reports/market_regime_attribution_v4.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n报告已保存: {report_path}")

    print(f"\n[OK] 逐日逐ETF真实持仓归因完成")


if __name__ == '__main__':
    main()
