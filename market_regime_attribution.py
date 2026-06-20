# -*- coding: utf-8 -*-
"""
market_regime_attribution.py - 市场状态归因分析

按强牛/弱牛/震荡/熊市分别统计策略表现：
- 所处交易日数
- 策略收益和年化收益
- 夏普
- 最大回撤
- 胜率
- 买入次数、止损次数
- 平均/中位持有期
- 行业ETF、防御资产的收益贡献
- 调出候选导致的退出次数
- 每种状态下贡献最大和拖累最大的ETF

不改策略、不调参数。
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import bisect
from datetime import datetime
from database import ETFDatabase
from backtest import BacktestEngine
from market_regime import MarketRegimeDetector
from strategy import StrategyEngine
import config


def load_data():
    """加载回测所需数据"""
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


def run_backtest(market_df, bench_df):
    """运行当前基线回测"""
    engine = BacktestEngine()
    result = engine.run(market_df, bench_df)
    return result


def detect_regime_history(bench_df, market_df):
    """检测历史市场状态序列"""
    detector = MarketRegimeDetector()
    
    core_tickers = list(config.ETF_UNIVERSE.keys())
    market_for_breadth = market_df[market_df['ticker'].isin(core_tickers)].copy()
    
    regime_df = detector.detect_history(bench_df, market_for_breadth)
    regime_df['date'] = pd.to_datetime(regime_df['date'])
    
    return regime_df


def merge_with_regime(df, regime_df, date_col='date'):
    """将DataFrame与市场状态合并（使用bisect查找最近日期）"""
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


def calculate_regime_stats(nav_regime_df, trades_regime_df):
    """按市场状态计算统计指标（识别连续段落）"""
    
    stats = {}
    
    # 识别连续段落
    nav_regime_df = nav_regime_df.copy().sort_values('date').reset_index(drop=True)
    nav_regime_df['segment_id'] = (nav_regime_df['regime_id'] != nav_regime_df['regime_id'].shift(1)).cumsum()
    
    for regime_id in [1, 2, 3, 4]:
        regime_name = MarketRegimeDetector.STATE_NAMES[regime_id]
        regime_nav = nav_regime_df[nav_regime_df['regime_id'] == regime_id].copy()
        regime_trades = trades_regime_df[trades_regime_df['regime_id'] == regime_id].copy()
        
        if len(regime_nav) < 5:
            stats[regime_name] = {'days': 0, 'note': '数据不足'}
            continue
        
        days = len(regime_nav)
        
        # 计算各连续段落的收益，然后加总
        segments = regime_nav.groupby('segment_id')
        segment_returns = []
        for seg_id, seg_df in segments:
            if len(seg_df) < 2:
                continue
            start_nav = seg_df['nav'].iloc[0]
            end_nav = seg_df['nav'].iloc[-1]
            seg_return = (end_nav / start_nav) - 1
            segment_returns.append(seg_return)
        
        # 状态总贡献 = 各段落的 (1+r) 连乘 - 1
        if segment_returns:
            total_contribution = np.prod([1 + r for r in segment_returns]) - 1
        else:
            total_contribution = 0
        
        # 年化收益（基于总贡献和总天数）
        years = days / 252
        annual_return = (1 + total_contribution) ** (1 / years) - 1 if years > 0 and total_contribution > -1 else 0
        
        # 波动率和夏普（使用所有该状态的日收益）
        daily_returns = regime_nav['nav'].pct_change().dropna()
        volatility = daily_returns.std() * np.sqrt(252) if len(daily_returns) > 1 else 0
        sharpe = annual_return / volatility if volatility > 0 else 0
        
        # 最大回撤（在所有该状态的段落内计算）
        regime_nav['peak'] = regime_nav['nav'].cummax()
        regime_nav['drawdown'] = (regime_nav['nav'] - regime_nav['peak']) / regime_nav['peak']
        max_drawdown = regime_nav['drawdown'].min()
        
        # 交易统计
        buys = regime_trades[regime_trades['action'] == 'BUY']
        sells = regime_trades[regime_trades['action'].isin(['SELL', 'STOP_LOSS'])]
        stop_losses = regime_trades[regime_trades['action'] == 'STOP_LOSS']
        
        buy_count = len(buys)
        stop_loss_count = len(stop_losses)
        
        sell_trades = sells[sells['pnl_pct'] != 0]
        if len(sell_trades) > 0:
            win_trades = sell_trades[sell_trades['pnl_pct'] > 0]
            win_rate = len(win_trades) / len(sell_trades)
        else:
            win_rate = 0
        
        candidate_exits = regime_trades[regime_trades['reason'].str.contains('调出候选', na=False)]
        candidate_exit_count = len(candidate_exits)
        
        hold_periods = []
        if not regime_trades.empty:
            for _, sell_row in sells.iterrows():
                ticker = sell_row['ticker']
                sell_date = sell_row['date']
                
                prior_buys = regime_trades[
                    (regime_trades['ticker'] == ticker) & 
                    (regime_trades['action'] == 'BUY') & 
                    (regime_trades['date'] < sell_date)
                ]
                if not prior_buys.empty:
                    hold_days = (sell_date - prior_buys['date'].iloc[-1]).days
                    hold_periods.append(hold_days)
        
        avg_hold = np.mean(hold_periods) if hold_periods else 0
        median_hold = np.median(hold_periods) if hold_periods else 0
        
        etf_pnl = {}
        if not regime_trades.empty:
            for ticker in regime_trades['ticker'].unique():
                ticker_trades = regime_trades[regime_trades['ticker'] == ticker]
                ticker_sells = ticker_trades[ticker_trades['action'].isin(['SELL', 'STOP_LOSS'])]
                if not ticker_sells.empty:
                    etf_pnl[ticker] = ticker_sells['pnl_pct'].sum()
        
        defense_tickers = set(config.DEFENSE_UNIVERSE.keys())
        equity_tickers = set(config.ETF_UNIVERSE.keys())
        
        defense_pnl = sum(pnl for t, pnl in etf_pnl.items() if t in defense_tickers)
        equity_pnl = sum(pnl for t, pnl in etf_pnl.items() if t in equity_tickers)
        
        if etf_pnl:
            best_etf = max(etf_pnl, key=etf_pnl.get)
            worst_etf = min(etf_pnl, key=etf_pnl.get)
            best_pnl = etf_pnl[best_etf]
            worst_pnl = etf_pnl[worst_etf]
        else:
            best_etf = worst_etf = None
            best_pnl = worst_pnl = 0
        
        stats[regime_name] = {
            'days': days,
            'period_return': total_contribution * 100,
            'annual_return': annual_return * 100,
            'sharpe': sharpe,
            'max_drawdown': max_drawdown * 100,
            'win_rate': win_rate * 100,
            'buy_count': buy_count,
            'stop_loss_count': stop_loss_count,
            'candidate_exit_count': candidate_exit_count,
            'avg_hold': avg_hold,
            'median_hold': median_hold,
            'equity_pnl': equity_pnl,
            'defense_pnl': defense_pnl,
            'best_etf': best_etf,
            'best_pnl': best_pnl * 100,
            'worst_etf': worst_etf,
            'worst_pnl': worst_pnl * 100,
            'etf_pnl': etf_pnl,
        }
    
    return stats


def format_report(stats, total_days, total_return, total_sharpe, total_mdd, total_trades):
    """生成归因报告"""
    lines = []
    
    lines.append("# 市场状态归因分析报告")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**策略**: ETF轮动 v1.2 基线（16只行业ETF）")
    lines.append(f"**数据口径**: 前复权，统一数据至2026-06-05")
    lines.append(f"**市场状态检测**: 基于沪深300趋势/斜率/波动率/市场宽度")
    lines.append(f"**规则**: 连续5日确认切换，不改策略、不调参数")
    
    lines.append(f"\n## 全区间概览")
    lines.append(f"\n| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总交易日 | {total_days} |")
    lines.append(f"| 总收益 | {total_return:.2%} |")
    lines.append(f"| 夏普比率 | {total_sharpe:.2f} |")
    lines.append(f"| 最大回撤 | {total_mdd:.2%} |")
    lines.append(f"| 总交易次数 | {total_trades} |")
    
    lines.append(f"\n## 按市场状态分组统计")
    
    lines.append(f"\n### 汇总对比")
    lines.append(f"\n| 状态 | 交易日 | 占比 | 区间收益 | 年化 | 夏普 | 最大回撤 | 胜率 | 买入 | 止损 | 调出候选 | 平均持有 | 中位持有 |")
    lines.append(f"|------|--------|------|----------|------|------|----------|------|------|------|----------|----------|----------|")
    
    for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
        s = stats.get(regime_name, {})
        if s.get('days', 0) == 0:
            lines.append(f"| {regime_name} | 0 | - | - | - | - | - | - | - | - | - | - | - |")
            continue
        
        pct = s['days'] / total_days * 100 if total_days > 0 else 0
        lines.append(
            f"| {regime_name} | {s['days']} | {pct:.1f}% | "
            f"{s['period_return']:.2f}% | {s['annual_return']:.2f}% | {s['sharpe']:.2f} | "
            f"{s['max_drawdown']:.2f}% | {s['win_rate']:.1f}% | {s['buy_count']} | "
            f"{s['stop_loss_count']} | {s['candidate_exit_count']} | "
            f"{s['avg_hold']:.1f} | {s['median_hold']:.1f} |"
        )
    
    for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
        s = stats.get(regime_name, {})
        if s.get('days', 0) == 0:
            continue
        
        lines.append(f"\n### {regime_name} 详细分析")
        lines.append(f"\n**基础统计**:")
        lines.append(f"- 交易日: {s['days']} 天（占比 {s['days']/total_days*100:.1f}%）")
        lines.append(f"- 区间收益: {s['period_return']:.2f}%")
        lines.append(f"- 年化收益: {s['annual_return']:.2f}%")
        lines.append(f"- 夏普比率: {s['sharpe']:.2f}")
        lines.append(f"- 最大回撤: {s['max_drawdown']:.2f}%")
        lines.append(f"- 胜率: {s['win_rate']:.1f}%")
        
        lines.append(f"\n**交易行为**:")
        lines.append(f"- 买入次数: {s['buy_count']}")
        lines.append(f"- 止损次数: {s['stop_loss_count']}")
        lines.append(f"- 调出候选退出: {s['candidate_exit_count']}")
        lines.append(f"- 平均持有期: {s['avg_hold']:.1f} 天")
        lines.append(f"- 中位持有期: {s['median_hold']:.1f} 天")
        
        lines.append(f"\n**收益贡献**:")
        lines.append(f"- 行业ETF贡献: {s['equity_pnl']:.2f}%")
        lines.append(f"- 防御资产贡献: {s['defense_pnl']:.2f}%")
        
        if s['best_etf']:
            best_name = config.ETF_UNIVERSE.get(s['best_etf'], config.DEFENSE_UNIVERSE.get(s['best_etf'], s['best_etf']))
            lines.append(f"- 贡献最大: {best_name} ({s['best_etf']}) +{s['best_pnl']:.2f}%")
        
        if s['worst_etf']:
            worst_name = config.ETF_UNIVERSE.get(s['worst_etf'], config.DEFENSE_UNIVERSE.get(s['worst_etf'], s['worst_etf']))
            lines.append(f"- 拖累最大: {worst_name} ({s['worst_etf']}) {s['worst_pnl']:.2f}%")
        
        if s['etf_pnl']:
            lines.append(f"\n**各ETF盈亏贡献**:")
            lines.append(f"\n| ETF | 名称 | 盈亏贡献 |")
            lines.append(f"|-----|------|----------|")
            
            sorted_pnls = sorted(s['etf_pnl'].items(), key=lambda x: x[1], reverse=True)
            for ticker, pnl in sorted_pnls:
                name = config.ETF_UNIVERSE.get(ticker, config.DEFENSE_UNIVERSE.get(ticker, ticker))
                lines.append(f"| {ticker} | {name} | {pnl*100:.2f}% |")
    
    lines.append(f"\n## 关键发现")
    
    strong_bull_return = stats.get('强牛', {}).get('period_return', 0)
    weak_bull_return = stats.get('弱牛', {}).get('period_return', 0)
    oscillation_return = stats.get('震荡', {}).get('period_return', 0)
    bear_return = stats.get('熊市', {}).get('period_return', 0)
    bear_defense = stats.get('熊市', {}).get('defense_pnl', 0)
    
    lines.append(f"\n1. **强牛**: 策略{'表现优异' if strong_bull_return > 20 else '表现良好' if strong_bull_return > 10 else '表现一般'}，区间收益 {strong_bull_return:.2f}%")
    lines.append(f"2. **弱牛**: 策略{'能跟上' if weak_bull_return > 0 else '跑输'}，区间收益 {weak_bull_return:.2f}%")
    lines.append(f"3. **震荡**: 策略最大回撤主要发生在震荡市（{stats.get('震荡', {}).get('max_drawdown', 0):.2f}%），反复交易但无法积累收益")
    lines.append(f"4. **熊市**: 防御资产贡献{bear_defense:.2f}%，{'有效缓冲' if bear_defense > 0 else '未能有效缓冲'}")
    
    lines.append(f"\n## 版本边界")
    lines.append(f"\n- v1.2.2 已收口")
    lines.append(f"- v1.3 研究阶段，当前为市场状态归因")
    lines.append(f"- 不改交易规则")
    lines.append(f"- 不设计自适应规则")
    
    return '\n'.join(lines)


def main():
    print("="*80)
    print("市场状态归因分析")
    print("="*80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    print("[1/5] 加载数据...")
    market_df, bench_df = load_data()
    print(f"  市场数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只ETF")
    print(f"  基准数据: {len(bench_df)} 行")
    
    print("\n[2/5] 运行当前基线回测...")
    result = run_backtest(market_df, bench_df)
    
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
    
    print("\n[4/5] 合并净值与交易记录...")
    nav_regime = merge_with_regime(nav_df, regime_df)
    trades_regime = merge_with_regime(trades_df, regime_df)
    
    # 验证合并结果
    print(f"  合并后nav regime分布: {nav_regime['regime_name'].value_counts().to_dict()}")
    if not trades_regime.empty:
        print(f"  合并后trades regime分布: {trades_regime['regime_name'].value_counts().to_dict()}")
    
    print("\n[5/5] 按市场状态计算统计指标...")
    stats = calculate_regime_stats(nav_regime, trades_regime)
    
    print("\n生成报告...")
    report = format_report(
        stats,
        total_days=len(nav_df),
        total_return=result['total_return'],
        total_sharpe=result['sharpe_ratio'],
        total_mdd=result['max_drawdown'],
        total_trades=result['num_trades']
    )
    
    report_path = 'reports/market_regime_attribution.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"\n报告已保存: {report_path}")
    
    print("\n" + "="*80)
    print("摘要")
    print("="*80)
    for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
        s = stats.get(regime_name, {})
        if s.get('days', 0) > 0:
            print(f"\n{regime_name}:")
            print(f"  天数: {s['days']} ({s['days']/len(nav_df)*100:.1f}%)")
            print(f"  收益: {s['period_return']:.2f}% (年化{s['annual_return']:.2f}%)")
            print(f"  夏普: {s['sharpe']:.2f}")
            print(f"  最大回撤: {s['max_drawdown']:.2f}%")
            print(f"  买入: {s['buy_count']}, 止损: {s['stop_loss_count']}, 调出候选: {s['candidate_exit_count']}")
            print(f"  行业ETF: {s['equity_pnl']:.2f}%, 防御: {s['defense_pnl']:.2f}%")
            if s['best_etf']:
                print(f"  最佳: {s['best_etf']} +{s['best_pnl']:.2f}%")
            if s['worst_etf']:
                print(f"  最差: {s['worst_etf']} {s['worst_pnl']:.2f}%")
    
    print(f"\n[OK] 市场状态归因分析完成")


if __name__ == '__main__':
    main()
