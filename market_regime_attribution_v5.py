# -*- coding: utf-8 -*-
"""
market_regime_attribution_v5.py - v5 逐日逐ETF真实持仓归因（修复缺失价格+等权影子组合）

核心口径：
- 逐日逐ETF真实持仓归因，会计式勾稽
- 缺失价格采用最近有效收盘价（与回测引擎一致）
- 构建等权影子组合：同仓位规模下，行业持仓均分给所有可交易ETF
- 超额拆分为：仓位暴露贡献、行业选择贡献、交易执行贡献、防御资产贡献、交易成本

验收条件：
1. 每日贡献与NAV收益精确勾稽（零误差）
2. 四个状态金额贡献合计与全组合净利润精确勾稽（零误差）

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
    
    # v5b: 统一截断到全标的共同截止日 2026-06-05
    COMMON_CUTOFF = pd.Timestamp('2026-06-05')
    market_df = market_df[market_df['date'] <= COMMON_CUTOFF].copy()
    bench_df = bench_df[bench_df['date'] <= COMMON_CUTOFF].copy()
    
    return market_df, bench_df


def detect_regime_history(bench_df, market_df):
    detector = MarketRegimeDetector()
    core_tickers = list(config.ETF_UNIVERSE.keys())
    market_for_breadth = market_df[market_df['ticker'].isin(core_tickers)].copy()
    regime_df = detector.detect_history(bench_df, market_for_breadth)
    regime_df['date'] = pd.to_datetime(regime_df['date'])
    return regime_df


def build_effective_price_map(market_df):
    """
    构建有效价格映射，缺失价格使用最近有效收盘价。
    返回：{ticker: {date: {'open': ..., 'close': ..., 'effective': True/False}}}
    """
    prices = defaultdict(dict)
    last_valid_close = {}
    last_valid_open = {}
    
    # 按日期排序
    all_dates = sorted(market_df['date'].unique())
    
    for date in all_dates:
        day_data = market_df[market_df['date'] == date]
        for _, row in day_data.iterrows():
            ticker = row['ticker']
            open_p = row['open']
            close_p = row['close']
            
            if pd.notna(close_p) and close_p > 0:
                last_valid_close[ticker] = close_p
            if pd.notna(open_p) and open_p > 0:
                last_valid_open[ticker] = open_p
            
            prices[ticker][date] = {
                'open': open_p if pd.notna(open_p) and open_p > 0 else last_valid_open.get(ticker, 0),
                'close': close_p if pd.notna(close_p) and close_p > 0 else last_valid_close.get(ticker, 0),
                'effective': pd.notna(close_p) and close_p > 0,  # 当日是否有有效价格
            }
    
    return prices


def build_trade_map(trades_df):
    """构建交易映射"""
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
            trades[date][ticker]['buy_price'] = price
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
    
    同时计算等权影子组合：
    - 影子组合行业收益 = 昨日行业持仓总市值 × 等权收益率
    
    验证：sum = nav_t - nav_{t-1}
    """
    nav_df = nav_df.sort_values('date').reset_index(drop=True)
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    
    # 合并市场状态
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
        
        etf_contributions = {}
        state_day_total = 0.0
        
        # 1. 既有持仓浮动（所有昨日持有的ETF）
        for ticker, pos in yest_positions.items():
            shares_yest = pos['shares']
            if shares_yest <= 0:
                continue
            
            p_today = price_map.get(ticker, {}).get(today_date)
            p_yest = price_map.get(ticker, {}).get(yest_date)
            
            if p_yest is None or p_today is None:
                continue
            
            close_yest = p_yest['close']
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
        
        # 2. 新买贡献 & 卖出修正 & 交易成本
        day_commission = 0.0
        for ticker, trade_info in today_trades.items():
            day_commission += trade_info['commission']
            
            p_today = price_map.get(ticker, {}).get(today_date)
            if p_today is None:
                continue
            close_today = p_today['close']
            
            if ticker not in etf_contributions:
                etf_contributions[ticker] = {'hold': 0.0, 'buy': 0.0, 'sell': 0.0, 'commission': 0.0, 'total': 0.0}
            
            if trade_info['buy'] > 0:
                buy_price = trade_info['buy_price']
                if buy_price > 0:
                    buy_contrib = trade_info['buy'] * (close_today - buy_price)
                    etf_contributions[ticker]['buy'] += buy_contrib
                    etf_contributions[ticker]['total'] += buy_contrib
                    state_day_total += buy_contrib
            
            if trade_info['sell'] > 0:
                sell_price = trade_info['sell_price']
                if sell_price > 0:
                    sell_contrib = trade_info['sell'] * (sell_price - close_today)
                    etf_contributions[ticker]['sell'] += sell_contrib
                    etf_contributions[ticker]['total'] += sell_contrib
                    state_day_total += sell_contrib
            
            etf_contributions[ticker]['commission'] += trade_info['commission']
        
        # 3. 交易成本
        total_cost_contrib = -day_commission
        state_day_total += total_cost_contrib
        
        # 4. 等权影子组合行业收益
        # 昨日行业持仓总市值
        yest_industry_value = 0.0
        for ticker, pos in yest_positions.items():
            if ticker in industry_tickers:
                p_yest = price_map.get(ticker, {}).get(yest_date)
                if p_yest and p_yest['close'] > 0:
                    yest_industry_value += pos['shares'] * p_yest['close']
        
        # 所有可交易行业ETF的当日收益率
        returns = []
        for ticker in industry_tickers:
            p_yest = price_map.get(ticker, {}).get(yest_date)
            p_today = price_map.get(ticker, {}).get(today_date)
            if p_yest and p_today and p_yest['close'] > 0 and p_today['close'] > 0:
                ret = p_today['close'] / p_yest['close'] - 1
                returns.append(ret)
        
        avg_return = np.mean(returns) if returns else 0.0
        ew_contrib = yest_industry_value * avg_return
        
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
            'ew_contrib': ew_contrib,
            'yest_industry_value': yest_industry_value,
            'avg_return': avg_return,
            'num_active_etfs': len(returns),
        })
    
    daily_df = pd.DataFrame(daily_records)
    return daily_df, max_discrepancy, max_discrepancy_date


def aggregate_by_state(daily_df, industry_tickers, defense_tickers):
    """
    按市场状态汇总贡献，拆分为五类：
    - 仓位暴露贡献（等权影子组合行业收益）
    - 行业选择贡献（实际行业 - 等权影子组合）
    - 交易执行贡献（行业新买 + 卖出修正）
    - 防御资产贡献（防御总收益）
    - 交易成本（总佣金）
    """
    state_stats = {}
    
    for regime_id in [1, 2, 3, 4]:
        regime_name = MarketRegimeDetector.STATE_NAMES[regime_id]
        subset = daily_df[daily_df['regime_id'] == regime_id]
        
        if subset.empty:
            state_stats[regime_name] = {
                'trading_days': 0,
                'exposure_contrib': 0.0,
                'selection_contrib': 0.0,
                'execution_contrib': 0.0,
                'defense_contrib': 0.0,
                'cost_contrib': 0.0,
                'total_contrib': 0.0,
            }
            continue
        
        industry_hold = 0.0
        industry_buy = 0.0
        industry_sell = 0.0
        defense_hold = 0.0
        defense_buy = 0.0
        defense_sell = 0.0
        commission_total = 0.0
        ew_contrib_total = 0.0
        
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
            ew_contrib_total += row['ew_contrib']
        
        actual_industry = industry_hold + industry_buy + industry_sell
        actual_defense = defense_hold + defense_buy + defense_sell
        
        # v5b: 互斥五类贡献口径
        exposure_contrib = ew_contrib_total              # 等权影子组合行业收益
        selection_contrib = industry_hold - ew_contrib_total  # 既有持仓的行业超额
        execution_contrib = industry_buy + industry_sell      # 当日交易净损益
        defense_contrib = actual_defense                      # 防御资产总收益
        cost_contrib = commission_total                        # 交易成本
        total_contrib = exposure_contrib + selection_contrib + execution_contrib + defense_contrib + cost_contrib
        
        state_stats[regime_name] = {
            'trading_days': len(subset),
            'industry_hold': industry_hold,
            'industry_buy': industry_buy,
            'industry_sell': industry_sell,
            'defense_hold': defense_hold,
            'defense_buy': defense_buy,
            'defense_sell': defense_sell,
            'commission': commission_total,
            'exposure_contrib': exposure_contrib,
            'selection_contrib': selection_contrib,
            'execution_contrib': execution_contrib,
            'defense_contrib': defense_contrib,
            'cost_contrib': cost_contrib,
            'total_contrib': total_contrib,
        }
    
    return state_stats, daily_df


def format_report_v5(state_stats, daily_df, total_nav_change, max_disc, max_disc_date, industry_tickers, defense_tickers, result):
    lines = []
    
    lines.append("# 市场状态归因分析报告（v5 - 修复缺失价格+等权影子组合）")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**策略**: ETF轮动 v1.2 基线（{len(industry_tickers)}只行业ETF）")
    lines.append(f"**数据口径**: 前复权，统一数据至2026-06-05")
    lines.append(f"**核心口径**: 逐日逐ETF真实持仓归因，会计式勾稽")
    lines.append(f"**修复**: 缺失价格采用最近有效收盘价，禁止持仓市值归零")
    lines.append(f"**规则**: 连续5日确认切换；不改策略、不调参数、不改市场状态算法")
    
    # 基线绩效
    lines.append(f"\n## 修复后基线绩效")
    lines.append(f"\n| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总收益 | {result['total_return']:.2%} |")
    lines.append(f"| 年化收益 | {result['annual_return']:.2%} |")
    lines.append(f"| 夏普比率 | {result['sharpe_ratio']:.2f} |")
    lines.append(f"| 索提诺比率 | {result['sortino_ratio']:.2f} |")
    lines.append(f"| 最大回撤 | {result['max_drawdown']:.2%} |")
    lines.append(f"| 总交易次数 | {result['num_trades']} |")
    lines.append(f"| 总佣金 | {result['total_commission']:,.2f} |")
    lines.append(f"| 平均持仓数 | {result['avg_holdings']:.1f} |")
    
    # 验收条件1
    lines.append(f"\n## 验收条件1：每日贡献与NAV收益精确勾稽")
    lines.append(f"\n| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总交易日 | {len(daily_df)} |")
    lines.append(f"| 全组合NAV变化合计 | {total_nav_change:,.2f} |")
    lines.append(f"| 每日贡献合计 | {daily_df['total_contrib'].sum():,.2f} |")
    lines.append(f"| 最大单日误差 | {max_disc:,.4f} |")
    lines.append(f"| 最大误差日期 | {max_disc_date.strftime('%Y-%m-%d') if max_disc_date else 'N/A'} |")
    lines.append(f"| 总误差 | {daily_df['discrepancy'].sum():,.4f} |")
    
    # 验收条件2
    total_state_contrib = sum(s['total_contrib'] for s in state_stats.values())
    lines.append(f"\n## 验收条件2：四个状态金额贡献合计与全组合净利润精确勾稽")
    lines.append(f"\n| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 全组合NAV变化（净利润） | {total_nav_change:,.2f} |")
    lines.append(f"| 四个状态贡献合计 | {total_state_contrib:,.2f} |")
    lines.append(f"| 状态合计误差 | {total_nav_change - total_state_contrib:,.4f} |")
    
    # 按状态汇总
    lines.append(f"\n## 按市场状态汇总（五类贡献）")
    lines.append(f"\n| 状态 | 交易日数 | 仓位暴露贡献 | 行业选择贡献 | 交易执行贡献 | 防御资产贡献 | 交易成本 | 状态总贡献 |")
    lines.append(f"|------|----------|--------------|--------------|--------------|--------------|----------|------------|")
    
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = state_stats.get(name, {})
        if s.get('trading_days', 0) == 0:
            lines.append(f"| {name} | 0 | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {name} | {s['trading_days']} | "
            f"{s['exposure_contrib']:,.2f} | {s['selection_contrib']:,.2f} | {s['execution_contrib']:,.2f} | "
            f"{s['defense_contrib']:,.2f} | {s['cost_contrib']:,.2f} | {s['total_contrib']:,.2f} |"
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
        lines.append(f"- 等权影子组合收益: {s['exposure_contrib']:,.2f}")
        lines.append(f"- **行业选择贡献**: {s['selection_contrib']:,.2f}")
        lines.append(f"- 交易执行贡献: {s['execution_contrib']:,.2f}")
        lines.append(f"- 防御资产贡献: {s['defense_contrib']:,.2f}")
        lines.append(f"- 交易成本: {s['cost_contrib']:,.2f}")
        lines.append(f"- 状态总贡献: {s['total_contrib']:,.2f}")
    
    # 缺失价格事件
    missing_log = result.get('missing_price_log', pd.DataFrame())
    if not missing_log.empty:
        lines.append(f"\n## 缺失价格事件（v5修复）")
        lines.append(f"\n| 日期 | ETF | 连续缺失天数 | 最近有效价格 | 持仓股数 | 虚假冲击金额 |")
        lines.append(f"|------|-----|--------------|--------------|----------|--------------|")
        for _, row in missing_log.iterrows():
            lines.append(f"| {row['date']} | {row['ticker']} | {row['consecutive_missing_days']} | {row['last_valid_price']:.2f} | {row['shares']} | {row['impact']:,.2f} |")
    
    lines.append(f"\n## 版本边界")
    lines.append(f"- v1.2.2 已收口")
    lines.append(f"- v5 研究阶段：修复缺失价格估值，构建等权影子组合")
    lines.append(f"- 不改交易规则")
    lines.append(f"- 不设计自适应规则")
    lines.append(f"- 不改市场状态检测算法")
    
    return '\n'.join(lines)


def main():
    print("="*80)
    print("v5 逐日逐ETF真实持仓归因（修复缺失价格+等权影子组合）")
    print("="*80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    print("[1/5] 加载数据...")
    market_df, bench_df = load_data()
    print(f"  市场数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只ETF")
    print(f"  基准数据: {len(bench_df)} 行")
    
    industry_tickers = set(config.ETF_UNIVERSE.keys())
    defense_tickers = set(config.DEFENSE_UNIVERSE.keys())

    print("\n[2/5] 运行修复后回测...")
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
    print(f"  总佣金: {result['total_commission']:,.2f}")
    
    missing_log = result.get('missing_price_log', pd.DataFrame())
    if not missing_log.empty:
        print(f"  缺失价格记录: {len(missing_log)} 条")
    
    print("\n[3/5] 检测市场状态历史...")
    regime_df = detect_regime_history(bench_df, market_df)
    print(f"  状态检测完成: {len(regime_df)} 个交易日")
    state_dist = regime_df['regime_name'].value_counts()
    for name, count in state_dist.items():
        print(f"    {name}: {count} 天 ({count/len(regime_df)*100:.1f}%)")

    print("\n[4/5] 构建有效价格映射与交易映射...")
    price_map = build_effective_price_map(market_df)
    trades_map = build_trade_map(trades_df)
    print(f"  有效价格映射: {len(price_map)} 只ETF")
    print(f"  交易映射: {len(trades_map)} 个交易日有交易")

    print("\n[5/5] 计算逐日逐ETF真实归因并验证勾稽...")
    daily_df, max_disc, max_disc_date = calculate_true_daily_attribution(
        nav_df, trades_map, price_map, regime_df, industry_tickers, defense_tickers
    )
    
    state_stats, daily_df = aggregate_by_state(daily_df, industry_tickers, defense_tickers)
    
    total_nav_change = nav_df['nav'].iloc[-1] - nav_df['nav'].iloc[0]
    
    print(f"\n  每日勾稽验证:")
    print(f"    全组合NAV变化: {total_nav_change:,.2f}")
    print(f"    每日贡献合计: {daily_df['total_contrib'].sum():,.2f}")
    print(f"    最大单日误差: {max_disc:,.4f}")
    print(f"    总误差: {daily_df['discrepancy'].sum():,.4f}")
    
    total_state_contrib = sum(s['total_contrib'] for s in state_stats.values())
    print(f"\n  状态勾稽验证:")
    print(f"    四个状态合计: {total_state_contrib:,.2f}")
    print(f"    状态合计误差: {total_nav_change - total_state_contrib:,.4f}")

    print("\n生成报告...")
    report = format_report_v5(
        state_stats, daily_df, total_nav_change, max_disc, max_disc_date,
        industry_tickers, defense_tickers, result
    )
    
    report_path = 'reports/market_regime_attribution_v5.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n报告已保存: {report_path}")

    print(f"\n[OK] v5 逐日逐ETF真实持仓归因完成")


if __name__ == '__main__':
    main()
