# -*- coding: utf-8 -*-
"""
weekly_report.py - 生成每周详细的策略对比报告
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json

from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE
from backtest import BacktestEngine
from database import ETFDatabase


def get_weekly_data(df, end_date):
    """获取一周的数据（上周调仓日到本周调仓日）"""
    target_weekday = STRATEGY_CONFIG['rebalance_weekday']  # 3 = 周四
    
    # 找到end_date之前的最近一个调仓日
    dt = pd.to_datetime(end_date)
    days_since_target = (dt.weekday() - target_weekday) % 7
    current_rebalance = dt - timedelta(days=days_since_target)
    prev_rebalance = current_rebalance - timedelta(days=7)
    
    week_data = df[(df['date'] >= prev_rebalance.strftime('%Y-%m-%d')) & 
                   (df['date'] < current_rebalance.strftime('%Y-%m-%d'))].copy()
    
    return week_data, prev_rebalance, current_rebalance


def analyze_week(market_df, trades_df, nav_df, week_start, week_end):
    """分析单周表现"""
    
    # 1. 本周各ETF表现
    week_market = market_df[(market_df['date'] >= week_start.strftime('%Y-%m-%d')) & 
                            (market_df['date'] <= week_end.strftime('%Y-%m-%d'))].copy()
    
    etf_returns = []
    for ticker in week_market['ticker'].unique():
        t_df = week_market[week_market['ticker'] == ticker].sort_values('date')
        if len(t_df) >= 2:
            start_price = t_df.iloc[0]['close']
            end_price = t_df.iloc[-1]['close']
            ret = (end_price - start_price) / start_price
            etf_returns.append({
                'ticker': ticker,
                'name': ETF_UNIVERSE.get(ticker, DEFENSE_UNIVERSE.get(ticker, ticker)),
                'return': ret,
                'start_price': start_price,
                'end_price': end_price
            })
    
    etf_returns_df = pd.DataFrame(etf_returns).sort_values('return', ascending=False)
    top5 = etf_returns_df.head(5)
    bottom5 = etf_returns_df.tail(5)
    
    # 2. 本周交易
    week_trades = trades_df[(trades_df['date'] >= week_start.strftime('%Y-%m-%d')) & 
                          (trades_df['date'] <= week_end.strftime('%Y-%m-%d'))].copy()
    
    buys = week_trades[week_trades['action'] == 'BUY']
    sells = week_trades[week_trades['action'] == 'SELL']
    
    # 3. 本周持仓（从NAV反推）
    week_nav = nav_df[(nav_df['date'] >= week_start.strftime('%Y-%m-%d')) & 
                      (nav_df['date'] <= week_end.strftime('%Y-%m-%d'))].copy()
    
    # 4. 统计
    # 胜率：本周买入的标的，到周末是否盈利
    win_count = 0
    total_buys = 0
    for _, trade in buys.iterrows():
        ticker = trade['ticker']
        buy_price = trade['price']
        # 找这个标的本周结束时的价格
        t_end = week_market[(week_market['ticker'] == ticker) & (week_market['date'] == week_end.strftime('%Y-%m-%d'))]
        if not t_end.empty:
            end_price = t_end.iloc[0]['close']
            if end_price > buy_price:
                win_count += 1
            total_buys += 1
    
    buy_win_rate = win_count / total_buys if total_buys > 0 else 0
    
    # 卖出胜率
    sell_win_count = 0
    total_sells = 0
    for _, trade in sells.iterrows():
        if trade['pnl_pct'] > 0:
            sell_win_count += 1
        total_sells += 1
    
    sell_win_rate = sell_win_count / total_sells if total_sells > 0 else 0
    
    # 5. 策略净值变化
    if len(week_nav) >= 2:
        start_nav = week_nav.iloc[0]['nav']
        end_nav = week_nav.iloc[-1]['nav']
        strategy_return = (end_nav - start_nav) / start_nav
    else:
        strategy_return = 0
    
    # 6. 是否抓住top performers
    # 检查本周持仓/买入的标的中有多少在top5
    our_tickers = set(buys['ticker'].tolist())
    # 也加上持仓中的（从NAV计算，但这里简化用交易记录）
    # 实际上我们需要知道周末持仓，但nav_df中没有这个信息
    # 简化：只看买入的
    
    top5_tickers = set(top5['ticker'].tolist())
    caught_top5 = our_tickers & top5_tickers
    
    return {
        'week': week_end.strftime('%Y-%m-%d'),
        'week_start': week_start.strftime('%Y-%m-%d'),
        'strategy_return': strategy_return,
        'top5': top5.to_dict('records'),
        'bottom5': bottom5.to_dict('records'),
        'buys': buys.to_dict('records'),
        'sells': sells.to_dict('records'),
        'buy_win_rate': buy_win_rate,
        'sell_win_rate': sell_win_rate,
        'total_buys': total_buys,
        'total_sells': total_sells,
        'caught_top5': list(caught_top5),
        'caught_top5_count': len(caught_top5),
        'top5_total': len(top5_tickers)
    }


def generate_report():
    """生成完整报告"""
    print("="*80)
    print("正在加载数据...")
    
    db = ETFDatabase()
    market_df = db.get_market_data()
    bench_df = db.get_market_data(ticker='000300.SH')
    
    if market_df is None or len(market_df) == 0:
        print("错误：无法加载市场数据")
        return
    
    print(f"数据加载完成：{market_df['date'].min()} ~ {market_df['date'].max()}")
    print(f"共 {len(market_df)} 行数据，{market_df['ticker'].nunique()} 个标的")
    
    # 运行回测
    print("\n正在运行回测（调仓日：周四）...")
    engine = BacktestEngine(STRATEGY_CONFIG)
    results = engine.run(market_df, bench_df)
    
    if 'error' in results:
        print(f"回测错误：{results['error']}")
        return
    
    trades_df = results['trades_df']
    nav_df = results['nav_df']
    
    print(f"回测完成：总收益 {results['total_return']:.2%}，交易 {len(trades_df)} 次")
    
    # 记录基线
    print("\n正在记录基线...")
    from baseline_recorder import record_baseline
    output_files = [
        'reports/weekly_detailed_report.json',
        'reports/weekly_summary_report.md',
    ]
    baseline_id, record_path = record_baseline(
        results, 
        output_files=output_files,
        notes='调仓日改为周四，冷静期0，无动态止盈，仓位20%'
    )
    print(f"基线已记录: {baseline_id}")
    print(f"记录文件: {record_path}")
    
    # 生成基线报告
    from baseline_recorder import generate_baseline_report
    report_path = generate_baseline_report(baseline_id)
    print(f"基线报告: {report_path}")
    
    # 生成每周报告
    print("\n正在生成每周详细报告...")
    
    # 获取所有调仓周
    target_weekday = STRATEGY_CONFIG['rebalance_weekday']
    all_dates = pd.to_datetime(market_df['date'].unique())
    rebalance_dates = [d for d in all_dates if d.weekday() == target_weekday]
    rebalance_dates.sort()
    
    weekly_reports = []
    for i in range(1, len(rebalance_dates)):
        week_start = rebalance_dates[i-1]
        week_end = rebalance_dates[i]
        
        report = analyze_week(market_df, trades_df, nav_df, week_start, week_end)
        weekly_reports.append(report)
    
    # 保存JSON
    with open('reports/weekly_detailed_report.json', 'w', encoding='utf-8') as f:
        json.dump(weekly_reports, f, ensure_ascii=False, indent=2, default=str)
    
    print(f"\n已保存到 reports/weekly_detailed_report.json（共 {len(weekly_reports)} 周）")
    
    # 生成Markdown摘要报告
    generate_md_summary(weekly_reports, results)
    
    return weekly_reports


def generate_md_summary(reports, backtest_results):
    """生成Markdown摘要"""
    
    total_weeks = len(reports)
    positive_weeks = sum(1 for r in reports if r['strategy_return'] > 0)
    
    total_buys = sum(r['total_buys'] for r in reports)
    total_sells = sum(r['total_sells'] for r in reports)
    
    total_caught = sum(r['caught_top5_count'] for r in reports)
    total_top5_opportunities = sum(r['top5_total'] for r in reports if r['top5_total'] > 0)
    
    # 按年份统计
    year_stats = {}
    for r in reports:
        year = r['week'][:4]
        if year not in year_stats:
            year_stats[year] = {'weeks': 0, 'positive': 0, 'caught': 0, 'top5_total': 0, 'strategy_return': 0}
        year_stats[year]['weeks'] += 1
        if r['strategy_return'] > 0:
            year_stats[year]['positive'] += 1
        year_stats[year]['caught'] += r['caught_top5_count']
        year_stats[year]['top5_total'] += r['top5_total']
        year_stats[year]['strategy_return'] += r['strategy_return']
    
    md = f"""# ETF轮动策略 每周详细对比报告

> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}  
> 调仓日：周四（周三收盘计算信号，周四开盘交易）  
> 回测区间：{reports[0]['week_start'] if reports else 'N/A'} ~ {reports[-1]['week'] if reports else 'N/A'}  

---

## 一、整体统计

| 指标 | 数值 |
|------|------|
| 总周数 | {total_weeks} |
| 盈利周数 | {positive_weeks} ({positive_weeks/total_weeks:.1%}) |
| 亏损周数 | {total_weeks - positive_weeks} ({(total_weeks-positive_weeks)/total_weeks:.1%}) |
| 总交易次数（买入） | {total_buys} |
| 总交易次数（卖出） | {total_sells} |
| 抓住Top5次数 | {total_caught} / {total_top5_opportunities} ({total_caught/total_top5_opportunities:.1%}) |
| 策略总收益 | {backtest_results['total_return']:.2%} |
| 夏普比率 | {backtest_results['sharpe_ratio']:.2f} |
| 最大回撤 | {backtest_results['max_drawdown']:.2%} |

---

## 二、按年份统计

| 年份 | 周数 | 盈利周 | 胜率 | 抓住Top5 | 抓住率 | 累计收益 |
|------|------|--------|------|----------|--------|----------|
"""
    
    for year in sorted(year_stats.keys()):
        s = year_stats[year]
        md += f"| {year} | {s['weeks']} | {s['positive']} | {s['positive']/s['weeks']:.1%} | {s['caught']} | {s['caught']/s['top5_total']:.1%} | {s['strategy_return']:.2%} |\n"
    
    md += """
---

## 三、逐周详细记录（最近20周）

"""
    
    # 只展示最近20周
    recent = reports[-20:] if len(reports) > 20 else reports
    
    for r in recent:
        md += f"""
### 周：{r['week']}（{r['week_start']} ~ {r['week']}）

**策略收益**：{r['strategy_return']:+.2%}  
**买入胜率**：{r['buy_win_rate']:.0%}（{r['total_buys']}次买入）  
**卖出胜率**：{r['sell_win_rate']:.0%}（{r['total_sells']}次卖出）  
**抓住Top5**：{r['caught_top5_count']}/{r['top5_total']}（{', '.join(r['caught_top5']) if r['caught_top5'] else '无'}）

**本周Top5表现**：
"""
        for i, etf in enumerate(r['top5'][:5], 1):
            md += f"- {i}. {etf['name']}({etf['ticker']}): {etf['return']:+.2%}\n"
        
        md += "\n**本周Bottom5表现**：\n"
        for i, etf in enumerate(r['bottom5'][-5:], 1):
            md += f"- {i}. {etf['name']}({etf['ticker']}): {etf['return']:+.2%}\n"
        
        if r['buys']:
            md += "\n**本周买入**：\n"
            for t in r['buys']:
                md += f"- {t['date']} BUY {t['ticker']} @ {t['price']:.2f}（{t['reason'] if 'reason' in t else ''}）\n"
        
        if r['sells']:
            md += "\n**本周卖出**：\n"
            for t in r['sells']:
                md += f"- {t['date']} SELL {t['ticker']} @ {t['price']:.2f}（盈亏{t['pnl_pct']:+.2%}）\n"
        
        md += "\n---\n"
    
    md += """
## 四、关键发现

"""
    
    # 统计抓住率高的周和低的周
    high_catch = [r for r in reports if r['caught_top5_count'] >= 2]
    low_catch = [r for r in reports if r['caught_top5_count'] == 0 and r['top5_total'] > 0]
    
    md += f"""
### 抓住Top5表现好的周（≥2只）

共 {len(high_catch)} 周：

"""
    for r in high_catch[-10:]:
        md += f"- {r['week']}: 抓住{r['caught_top5_count']}只（{', '.join(r['caught_top5'])}），策略收益{r['strategy_return']:+.2%}\n"
    
    md += f"""
### 完全错过Top5的周

共 {len(low_catch)} 周：

"""
    for r in low_catch[-10:]:
        md += f"- {r['week']}: 策略收益{r['strategy_return']:+.2%}\n"
    
    md += """
---

> 完整逐周数据请查看 `reports/weekly_detailed_report.json`
"""
    
    with open('reports/weekly_summary_report.md', 'w', encoding='utf-8') as f:
        f.write(md)
    
    print(f"Markdown摘要已保存到 reports/weekly_summary_report.md")


if __name__ == '__main__':
    generate_report()
    print("\n报告生成完成！")
