# -*- coding: utf-8 -*-
"""
b1_test.py - B1 单变量测试：排名缓冲机制

测试方案：
- B0: 当前买卖规则（rank_buffer_enabled=False）
- B1: 前5买/跌出前10卖（rank_buffer_enabled=True, buy_rank_n=5, sell_rank_n=10）
- B1-8: 前5买/跌出前8卖（buy_rank_n=5, sell_rank_n=8）
- B1-12: 前5买/跌出前12卖（buy_rank_n=5, sell_rank_n=12）

输出：
- 全区间、样本内、样本外、滚动两年结果
- 夏普、索提诺、年化收益、最大回撤
- 交易次数、换手、佣金、平均持有期
- 五类贡献（按状态）
- 相对B0的变化

其他配置、ETF池、数据截止日、评分、去重、大盘择时、仓位、防御、调仓日、动态止盈和交易成本全部保持不变。
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


COMMON_CUTOFF = pd.Timestamp('2026-06-05')


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
    prices = defaultdict(dict)
    last_valid_close = {}
    last_valid_open = {}
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
                'effective': pd.notna(close_p) and close_p > 0,
            }
    return prices


def build_trade_map(trades_df):
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


def calculate_turnover(trades_df, nav_df):
    """计算年化换手率"""
    if trades_df.empty or 'action' not in trades_df.columns:
        return 0.0
    buy_trades = trades_df[trades_df['action'] == 'BUY']
    sell_trades = trades_df[trades_df['action'].isin(['SELL', 'STOP_LOSS'])]
    total_buy = buy_trades['amount'].sum() if not buy_trades.empty else 0
    total_sell = sell_trades['amount'].sum() if not sell_trades.empty else 0
    avg_nav = nav_df['nav'].mean()
    years = len(nav_df) / 252
    if avg_nav > 0 and years > 0:
        turnover = (total_buy + total_sell) / 2 / avg_nav / years
    else:
        turnover = 0.0
    return turnover


def calculate_avg_hold_period(trades_df):
    """计算平均持有期（天）"""
    if trades_df.empty or 'action' not in trades_df.columns:
        return 0.0
    buys = trades_df[trades_df['action'] == 'BUY'].sort_values(['date', 'ticker']).reset_index(drop=True)
    sells = trades_df[trades_df['action'].isin(['SELL', 'STOP_LOSS'])].sort_values(['date', 'ticker']).reset_index(drop=True)
    
    hold_periods = []
    buy_queue = defaultdict(list)
    for _, row in buys.iterrows():
        buy_queue[row['ticker']].append(row['date'])
    for _, row in sells.iterrows():
        ticker = row['ticker']
        if buy_queue[ticker]:
            buy_date = buy_queue[ticker].pop(0)
            sell_date = row['date']
            hold_days = (pd.to_datetime(sell_date) - pd.to_datetime(buy_date)).days
            if hold_days >= 0:
                hold_periods.append(hold_days)
    
    return np.mean(hold_periods) if hold_periods else 0.0


def run_backtest_with_params(market_df, bench_df, params, label):
    """运行回测并返回结果"""
    print(f"\n  [{label}] 运行回测...")
    engine = BacktestEngine(cfg=params)
    result = engine.run(market_df, bench_df)
    if 'error' in result:
        print(f"    回测失败: {result['error']}")
        return None
    
    nav_df = result['nav_df']
    trades_df = result['trades_df']
    
    # 计算换手率
    turnover = calculate_turnover(trades_df, nav_df)
    avg_hold = calculate_avg_hold_period(trades_df)
    
    # 计算归因（五类贡献）
    price_map = build_effective_price_map(market_df)
    trades_map = build_trade_map(trades_df)
    regime_df = detect_regime_history(bench_df, market_df)
    
    industry_tickers = set(config.ETF_UNIVERSE.keys())
    defense_tickers = set(config.DEFENSE_UNIVERSE.keys())
    
    daily_df, max_disc, max_disc_date = calculate_true_daily_attribution(
        nav_df, trades_map, price_map, regime_df, industry_tickers, defense_tickers
    )
    state_stats, daily_df = aggregate_by_state(daily_df, industry_tickers, defense_tickers)
    
    total_nav_change = nav_df['nav'].iloc[-1] - nav_df['nav'].iloc[0]
    
    # 验证勾稽
    assert abs(daily_df['discrepancy'].sum()) < 0.01, f"每日勾稽失败: {daily_df['discrepancy'].sum()}"
    total_state = sum(s['total_contrib'] for s in state_stats.values())
    assert abs(total_nav_change - total_state) < 0.01, f"状态勾稽失败: {total_nav_change - total_state}"
    
    print(f"    总收益: {result['total_return']:.2%}, 夏普: {result['sharpe_ratio']:.2f}, MDD: {result['max_drawdown']:.2%}")
    print(f"    交易: {result['num_trades']}次, 换手: {turnover:.2f}, 佣金: {result['total_commission']:,.2f}, 平均持有: {avg_hold:.1f}天")
    
    return {
        'result': result,
        'nav_df': nav_df,
        'trades_df': trades_df,
        'turnover': turnover,
        'avg_hold': avg_hold,
        'state_stats': state_stats,
        'daily_df': daily_df,
        'total_nav_change': total_nav_change,
    }


def calculate_true_daily_attribution(nav_df, trades_map, price_map, regime_df, industry_tickers, defense_tickers):
    """逐日计算真实贡献（复用v5b逻辑）"""
    nav_df = nav_df.sort_values('date').reset_index(drop=True)
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    regime_df = regime_df.sort_values('date').reset_index(drop=True)
    regime_dates = regime_df['date'].tolist()
    
    def get_regime(dt):
        idx = bisect.bisect_right(regime_dates, dt) - 1
        if idx >= 0:
            return regime_df.iloc[idx]['regime_id'], regime_df.iloc[idx]['regime_name']
        return 3, '震荡'
    
    daily_records = []
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
            etf_contributions[ticker] = {'hold': hold_contrib, 'buy': 0.0, 'sell': 0.0, 'commission': 0.0, 'total': hold_contrib}
            state_day_total += hold_contrib
        
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
        
        total_cost_contrib = -day_commission
        state_day_total += total_cost_contrib
        
        yest_industry_value = 0.0
        for ticker, pos in yest_positions.items():
            if ticker in industry_tickers:
                p_yest = price_map.get(ticker, {}).get(yest_date)
                if p_yest and p_yest['close'] > 0:
                    yest_industry_value += pos['shares'] * p_yest['close']
        
        returns = []
        for ticker in industry_tickers:
            p_yest = price_map.get(ticker, {}).get(yest_date)
            p_today = price_map.get(ticker, {}).get(today_date)
            if p_yest and p_today and p_yest['close'] > 0 and p_today['close'] > 0:
                ret = p_today['close'] / p_yest['close'] - 1
                returns.append(ret)
        avg_return = np.mean(returns) if returns else 0.0
        ew_contrib = yest_industry_value * avg_return
        
        discrepancy = nav_change - state_day_total
        daily_records.append({
            'date': today_date, 'regime_id': regime_id, 'regime_name': regime_name,
            'nav_change': nav_change, 'total_contrib': state_day_total,
            'discrepancy': discrepancy, 'etf_contributions': etf_contributions,
            'ew_contrib': ew_contrib, 'commission_contrib': total_cost_contrib,
        })
    
    return pd.DataFrame(daily_records), abs(pd.DataFrame(daily_records)['discrepancy'].max()) if daily_records else 0, None


def aggregate_by_state(daily_df, industry_tickers, defense_tickers):
    """按状态汇总五类贡献（复用v5b逻辑）"""
    state_stats = {}
    for regime_id in [1, 2, 3, 4]:
        regime_name = MarketRegimeDetector.STATE_NAMES[regime_id]
        subset = daily_df[daily_df['regime_id'] == regime_id]
        if subset.empty:
            state_stats[regime_name] = {
                'trading_days': 0, 'exposure_contrib': 0.0, 'selection_contrib': 0.0,
                'execution_contrib': 0.0, 'defense_contrib': 0.0, 'cost_contrib': 0.0, 'total_contrib': 0.0,
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
        
        actual_defense = defense_hold + defense_buy + defense_sell
        exposure_contrib = ew_contrib_total
        selection_contrib = industry_hold - ew_contrib_total
        execution_contrib = industry_buy + industry_sell
        defense_contrib = actual_defense
        cost_contrib = commission_total
        total_contrib = exposure_contrib + selection_contrib + execution_contrib + defense_contrib + cost_contrib
        
        state_stats[regime_name] = {
            'trading_days': len(subset),
            'exposure_contrib': exposure_contrib, 'selection_contrib': selection_contrib,
            'execution_contrib': execution_contrib, 'defense_contrib': defense_contrib,
            'cost_contrib': cost_contrib, 'total_contrib': total_contrib,
        }
    
    return state_stats, daily_df


def format_b1_report(results, b0_result):
    """生成B1测试报告"""
    lines = []
    lines.append("# B1 单变量测试报告：排名缓冲机制")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**基准**: B0 已冻结（哈希: d803577b8fca88855da0e0abe53df88b803c911f8bf522fd946674c79ad71982）")
    lines.append(f"**数据截止**: 2026-06-05")
    lines.append(f"**单变量**: 仅改变买卖排名规则，其他全部不变")
    
    # 绩效对比
    lines.append(f"\n## 绩效对比")
    lines.append(f"\n| 方案 | 区间 | 总收益 | 年化收益 | 夏普 | 索提诺 | 最大回撤 | 交易次数 | 换手 | 佣金 | 平均持有期 |")
    lines.append(f"|------|------|--------|----------|------|--------|----------|----------|------|------|------------|")
    
    for label, data in results.items():
        if data is None:
            continue
        r = data['result']
        lines.append(
            f"| {label} | 全区间 | {r['total_return']:.2%} | {r['annual_return']:.2%} | "
            f"{r['sharpe_ratio']:.2f} | {r['sortino_ratio']:.2f} | {r['max_drawdown']:.2%} | "
            f"{r['num_trades']} | {data['turnover']:.2f} | {r['total_commission']:,.2f} | {data['avg_hold']:.1f}天 |"
        )
    
    # 五类贡献对比（按状态）
    lines.append(f"\n## 五类贡献对比（按市场状态）")
    
    for state_name in ['强牛', '弱牛', '震荡', '熊市']:
        lines.append(f"\n### {state_name}")
        lines.append(f"\n| 方案 | 交易日数 | 仓位暴露 | 行业选择 | 交易执行 | 防御资产 | 交易成本 | 状态总贡献 |")
        lines.append(f"|------|----------|----------|----------|----------|----------|----------|------------|")
        
        for label, data in results.items():
            if data is None:
                continue
            s = data['state_stats'].get(state_name, {})
            if s.get('trading_days', 0) == 0:
                continue
            lines.append(
                f"| {label} | {s['trading_days']} | {s['exposure_contrib']:,.2f} | {s['selection_contrib']:,.2f} | "
                f"{s['execution_contrib']:,.2f} | {s['defense_contrib']:,.2f} | {s['cost_contrib']:,.2f} | {s['total_contrib']:,.2f} |"
            )
    
    # 相对B0变化
    if 'B0' in results and results['B0'] is not None:
        b0_stats = results['B0']['state_stats']
        lines.append(f"\n## 相对 B0 的变化")
        
        for label in ['B1', 'B1-8', 'B1-12']:
            if label not in results or results[label] is None:
                continue
            b1_stats = results[label]['state_stats']
            lines.append(f"\n### {label} vs B0")
            lines.append(f"\n| 状态 | 行业选择变化 | 交易执行变化 | 交易成本变化 | 状态总变化 |")
            lines.append(f"|------|--------------|--------------|--------------|------------|")
            
            for state_name in ['强牛', '弱牛', '震荡', '熊市']:
                b0_s = b0_stats.get(state_name, {})
                b1_s = b1_stats.get(state_name, {})
                if b0_s.get('trading_days', 0) == 0 and b1_s.get('trading_days', 0) == 0:
                    continue
                sel_delta = b1_s.get('selection_contrib', 0) - b0_s.get('selection_contrib', 0)
                exec_delta = b1_s.get('execution_contrib', 0) - b0_s.get('execution_contrib', 0)
                cost_delta = b1_s.get('cost_contrib', 0) - b0_s.get('cost_contrib', 0)
                total_delta = b1_s.get('total_contrib', 0) - b0_s.get('total_contrib', 0)
                lines.append(
                    f"| {state_name} | {sel_delta:+,.2f} | {exec_delta:+,.2f} | {cost_delta:+,.2f} | {total_delta:+,.2f} |"
                )
    
    lines.append(f"\n## 版本边界")
    lines.append(f"- B0 已冻结")
    lines.append(f"- B1 单变量测试：仅排名缓冲规则变化")
    lines.append(f"- 其他参数全部不变")
    
    return '\n'.join(lines)


def main():
    print("="*80)
    print("B1 单变量测试：排名缓冲机制")
    print("="*80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    print("[1/3] 加载数据...")
    market_df, bench_df = load_data()
    print(f"  市场数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只ETF")
    print(f"  基准数据: {len(bench_df)} 行")
    print(f"  统一截止日: 2026-06-05")

    # 测试方案
    test_cases = {
        'B0': {},  # 默认参数
        'B1': {'rank_buffer_enabled': True, 'buy_rank_n': 5, 'sell_rank_n': 10},
        'B1-8': {'rank_buffer_enabled': True, 'buy_rank_n': 5, 'sell_rank_n': 8},
        'B1-12': {'rank_buffer_enabled': True, 'buy_rank_n': 5, 'sell_rank_n': 12},
    }
    
    results = {}
    
    print("\n[2/3] 运行各方案回测...")
    for label, params in test_cases.items():
        cfg = config.STRATEGY_CONFIG.copy()
        cfg.update(params)
        result = run_backtest_with_params(market_df, bench_df, cfg, label)
        results[label] = result
    
    print("\n[3/3] 生成报告...")
    report = format_b1_report(results, results.get('B0'))
    
    report_path = 'reports/b1_rank_buffer_test.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n报告已保存: {report_path}")
    
    print(f"\n{'='*80}")
    print(f"[OK] B1 单变量测试完成")
    print(f"{'='*80}")


if __name__ == '__main__':
    main()
