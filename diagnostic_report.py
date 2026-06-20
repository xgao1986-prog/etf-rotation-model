# -*- coding: utf-8 -*-
"""
diagnostic_report.py - 诊断报告生成器

生成完整的诊断报告，包含：
1. 回测运行
2. 交易归因分析（退出原因、持有期、ETF/板块）
3. 每周对比报告
4. 元数据记录（git commit、数据库版本、config快照）
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import json
import os
import subprocess
from datetime import datetime, timedelta
from collections import defaultdict

from config import (
    STRATEGY_CONFIG, TRADING_RULES_CONFIG, BACKTEST_CONFIG,
    ETF_UNIVERSE, DEFENSE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE, CONCEPT_UNIVERSE, BENCHMARK, CORE_UNIVERSE
)
from backtest import BacktestEngine
from database import ETFDatabase
from baseline_recorder import record_baseline

def get_environment_info():
    """获取环境信息"""
    import platform
    import importlib
    env = {
        'python_version': platform.python_version(),
        'platform': platform.platform(),
    }
    for lib in ['pandas', 'numpy', 'sqlite3']:
        try:
            mod = importlib.import_module(lib)
            env[f'{lib}_version'] = getattr(mod, '__version__', 'unknown')
        except Exception:
            env[f'{lib}_version'] = 'unknown'
    return env


def get_git_info():
    """获取git信息"""
    info = {'commit': 'unknown', 'branch': 'unknown', 'dirty': True}
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, cwd='.')
        if result.returncode == 0:
            info['commit'] = result.stdout.strip()[:12]
        result = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True, text=True, cwd='.')
        if result.returncode == 0:
            info['branch'] = result.stdout.strip()
        result = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, cwd='.')
        if result.returncode == 0:
            info['dirty'] = len(result.stdout.strip()) > 0
    except Exception as e:
        info['error'] = str(e)
    return info


def get_db_info():
    """获取数据库信息"""
    from config import DB_PATH
    info = {'db_path': DB_PATH}
    if os.path.exists(DB_PATH):
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.execute('SELECT COUNT(*) FROM market_data')
            info['market_data_rows'] = cursor.fetchone()[0]
            cursor = conn.execute('SELECT MIN(date), MAX(date) FROM market_data')
            min_date, max_date = cursor.fetchone()
            info['date_range'] = {'min': min_date, 'max': max_date}
            cursor = conn.execute('SELECT COUNT(DISTINCT ticker) FROM market_data')
            info['num_tickers'] = cursor.fetchone()[0]
            cursor = conn.execute('SELECT adjust_type, COUNT(*) FROM market_data GROUP BY adjust_type')
            info['adjust_type'] = {r[0]: r[1] for r in cursor.fetchall()}
        finally:
            conn.close()
        info['file_size_mb'] = round(os.path.getsize(DB_PATH) / (1024*1024), 2)
        info['file_mtime'] = datetime.fromtimestamp(os.path.getmtime(DB_PATH)).strftime('%Y-%m-%d %H:%M:%S')
    return info


def analyze_trades(trades_df):
    """交易归因分析"""
    # 按 ticker 分组，重建每笔完整交易
    trades_df = trades_df.copy()
    trades_df['date'] = pd.to_datetime(trades_df['date'])
    
    # 按 ticker 分组
    grouped = trades_df.groupby('ticker')
    
    completed_trades = []  # 完成的交易（有买有卖）
    open_trades = []  # 未完成的交易（只有买没有卖）
    
    for ticker, tdf in grouped:
        tdf = tdf.sort_values('date').reset_index(drop=True)
        
        # 用FIFO匹配买卖
        buy_queue = []  # 队列中的买入
        
        for _, row in tdf.iterrows():
            if row['action'] == 'BUY':
                buy_queue.append(row)
            elif row['action'] in ['SELL', 'STOP_LOSS']:
                if buy_queue:
                    buy_row = buy_queue.pop(0)  # FIFO
                    hold_days = (row['date'] - buy_row['date']).days
                    completed_trades.append({
                        'ticker': ticker,
                        'buy_date': buy_row['date'],
                        'sell_date': row['date'],
                        'hold_days': hold_days,
                        'buy_price': buy_row['price'],
                        'sell_price': row['price'],
                        'shares': row['shares'],
                        'pnl_pct': row['pnl_pct'],
                        'exit_reason': row['reason'],
                        'exit_action': row['action'],
                    })
                else:
                    # 没有对应买入的卖出（不应该发生）
                    pass
        
        # 剩余未卖出的买入
        for buy_row in buy_queue:
            open_trades.append({
                'ticker': ticker,
                'buy_date': buy_row['date'],
                'buy_price': buy_row['price'],
                'shares': buy_row['shares'],
            })
    
    completed_df = pd.DataFrame(completed_trades)
    
    # 1. 按退出原因归因
    exit_reasons = {}
    for reason in completed_df['exit_reason'].unique():
        subset = completed_df[completed_df['exit_reason'] == reason]
        exit_reasons[reason] = {
            'count': len(subset),
            'avg_pnl': subset['pnl_pct'].mean(),
            'total_pnl': subset['pnl_pct'].sum(),
            'win_rate': (subset['pnl_pct'] > 0).mean(),
            'avg_hold_days': subset['hold_days'].mean(),
        }
    
    # 合并分类：调出候选列表、止损、其他
    exit_categories = {'调出候选列表': 0, '止损': 0, '其他': 0}
    for reason, stats in exit_reasons.items():
        if '候选列表' in reason or '调出' in reason:
            exit_categories['调出候选列表'] += stats['count']
        elif '止损' in reason or 'STOP' in reason:
            exit_categories['止损'] += stats['count']
        else:
            exit_categories['其他'] += stats['count']
    
    # 2. 按持有期归因
    hold_bins = {
        '≤7天': completed_df[completed_df['hold_days'] <= 7],
        '≤14天': completed_df[completed_df['hold_days'] <= 14],
        '≤20天': completed_df[(completed_df['hold_days'] > 14) & (completed_df['hold_days'] <= 20)],
        '≤30天': completed_df[(completed_df['hold_days'] > 20) & (completed_df['hold_days'] <= 30)],
        '>30天': completed_df[completed_df['hold_days'] > 30],
    }
    
    hold_analysis = {}
    for label, subset in hold_bins.items():
        if len(subset) > 0:
            hold_analysis[label] = {
                'count': len(subset),
                'avg_pnl': subset['pnl_pct'].mean(),
                'total_pnl': subset['pnl_pct'].sum(),
                'win_rate': (subset['pnl_pct'] > 0).mean(),
                'avg_hold_days': subset['hold_days'].mean(),
            }
    
    # 3. 按 ETF/板块归因
    etf_analysis = {}
    for ticker in completed_df['ticker'].unique():
        subset = completed_df[completed_df['ticker'] == ticker]
        name = ETF_UNIVERSE.get(ticker, DEFENSE_UNIVERSE.get(ticker, FALLBACK_EQUITY_UNIVERSE.get(ticker, CONCEPT_UNIVERSE.get(ticker, ticker))))
        etf_analysis[ticker] = {
            'name': name,
            'count': len(subset),
            'total_pnl': subset['pnl_pct'].sum(),
            'avg_pnl': subset['pnl_pct'].mean(),
            'win_rate': (subset['pnl_pct'] > 0).mean(),
            'avg_hold_days': subset['hold_days'].mean(),
            'max_gain': subset['pnl_pct'].max(),
            'max_loss': subset['pnl_pct'].min(),
        }
    
    # 4. 贡献/拖累收益的交易
    top_gainers = completed_df.nlargest(10, 'pnl_pct')[['ticker', 'buy_date', 'sell_date', 'hold_days', 'pnl_pct', 'exit_reason']]
    top_losers = completed_df.nsmallest(10, 'pnl_pct')[['ticker', 'buy_date', 'sell_date', 'hold_days', 'pnl_pct', 'exit_reason']]
    
    # 5. 统计
    total_buys = len(trades_df[trades_df['action'] == 'BUY'])
    total_sells = len(trades_df[trades_df['action'] == 'SELL'])
    total_stops = len(trades_df[trades_df['action'] == 'STOP_LOSS'])
    
    return {
        'completed_trades': completed_df.to_dict('records'),
        'exit_reasons': exit_reasons,
        'exit_categories': exit_categories,
        'hold_analysis': hold_analysis,
        'etf_analysis': etf_analysis,
        'top_gainers': top_gainers.to_dict('records'),
        'top_losers': top_losers.to_dict('records'),
        'total_buys': total_buys,
        'total_sells': total_sells,
        'total_stops': total_stops,
        'avg_hold_days': completed_df['hold_days'].mean() if len(completed_df) > 0 else 0,
        'median_hold_days': completed_df['hold_days'].median() if len(completed_df) > 0 else 0,
    }


def generate_weekly_report(market_df, trades_df, nav_df):
    """生成每周报告"""
    target_weekday = STRATEGY_CONFIG['rebalance_weekday']
    
    # 获取所有调仓日
    all_dates = pd.to_datetime(market_df['date'].unique())
    rebalance_dates = sorted([d for d in all_dates if d.weekday() == target_weekday])
    
    weekly_reports = []
    
    for i in range(1, len(rebalance_dates)):
        week_start = rebalance_dates[i-1]
        week_end = rebalance_dates[i]
        
        # 本周交易（调仓日发生在 week_end，即本周）
        week_trades = trades_df[
            (trades_df['date'] >= week_start.strftime('%Y-%m-%d')) &
            (trades_df['date'] <= week_end.strftime('%Y-%m-%d'))
        ].copy()
        
        # 统计
        buys = week_trades[week_trades['action'] == 'BUY']
        sells = week_trades[week_trades['action'].isin(['SELL', 'STOP_LOSS'])]
        
        # 本周持仓（从nav_df获取）
        week_nav = nav_df[
            (nav_df['date'] >= week_start) & (nav_df['date'] <= week_end)
        ].copy()
        
        # 获取周末持仓
        if len(week_nav) > 0:
            last_nav = week_nav.iloc[-1]
            positions_pct = last_nav.get('positions_pct', {})
            if isinstance(positions_pct, str):
                positions_pct = json.loads(positions_pct) if positions_pct else {}
            our_tickers = set(positions_pct.keys()) if positions_pct else set()
        else:
            our_tickers = set()
        
        # 本周也加入新买入的标的
        our_tickers = our_tickers | set(buys['ticker'].unique())
        
        # 本周ETF表现
        week_market = market_df[
            (market_df['date'] >= week_start.strftime('%Y-%m-%d')) &
            (market_df['date'] <= week_end.strftime('%Y-%m-%d'))
        ].copy()
        
        etf_returns = []
        for ticker in week_market['ticker'].unique():
            t_df = week_market[week_market['ticker'] == ticker].sort_values('date')
            if len(t_df) >= 2:
                ret = (t_df.iloc[-1]['close'] - t_df.iloc[0]['close']) / t_df.iloc[0]['close']
                etf_returns.append({'ticker': ticker, 'return': ret})
        
        etf_returns_df = pd.DataFrame(etf_returns).sort_values('return', ascending=False)
        top5 = etf_returns_df.head(5)
        bottom5 = etf_returns_df.tail(5)
        
        # 策略收益
        if len(week_nav) >= 2:
            strategy_return = (week_nav.iloc[-1]['nav'] - week_nav.iloc[0]['nav']) / week_nav.iloc[0]['nav']
        else:
            strategy_return = 0
        
        # 抓住Top5
        top5_tickers = set(top5['ticker'].tolist())
        caught = our_tickers & top5_tickers
        
        weekly_reports.append({
            'week': week_end.strftime('%Y-%m-%d'),
            'week_start': week_start.strftime('%Y-%m-%d'),
            'strategy_return': strategy_return,
            'num_buys': len(buys),
            'num_sells': len(sells),
            'our_tickers': list(our_tickers),
            'top5': top5.to_dict('records'),
            'bottom5': bottom5.to_dict('records'),
            'caught_top5': list(caught),
            'caught_top5_count': len(caught),
        })
    
    return weekly_reports


def generate_diagnostic_report(results, trades_analysis, weekly_reports, config_snapshot, meta):
    """生成诊断报告 Markdown"""
    
    md = f"""# ETF轮动策略诊断报告

> 生成时间: {meta['timestamp']}  
> 基线ID: {meta['baseline_id']}  

---

## 一、元数据

### 1.1 Git信息

| 项目 | 值 |
|------|------|
| Commit | `{meta['git']['commit']}` |
| Branch | {meta['git']['branch']} |
| 未提交修改 | {'是' if meta['git']['dirty'] else '否'} |

### 1.2 数据库信息

| 项目 | 值 |
|------|------|
| 路径 | `{meta['db']['db_path']}` |
| 文件大小 | {meta['db'].get('file_size_mb', 'N/A')} MB |
| 最后修改 | {meta['db'].get('file_mtime', 'N/A')} |
| 数据行数 | {meta['db'].get('market_data_rows', 'N/A')} |
| 日期范围 | {meta['db'].get('date_range', {}).get('min', 'N/A')} ~ {meta['db'].get('date_range', {}).get('max', 'N/A')} |
| ETF数量 | {meta['db'].get('num_tickers', 'N/A')} |
| adjust_type | {meta['db'].get('adjust_type', {})} |

### 1.3 配置快照

```json
{json.dumps(config_snapshot, ensure_ascii=False, indent=2, default=str)}
```

---

## 二、回测结果

| 指标 | 值 |
|------|------|
| 总收益率 | {results['total_return']:+.2%} |
| 年化收益率 | {results['annual_return']:+.2%} |
| 夏普比率 | {results['sharpe_ratio']:.2f} |
| 最大回撤 | {results['max_drawdown']:.2%} |
| 交易次数 | {results['num_trades']} |
| 买入次数 | {trades_analysis['total_buys']} |
| 卖出次数 | {trades_analysis['total_sells']} |
| 止损次数 | {trades_analysis['total_stops']} |
| 胜率 | {results['win_rate']:.1%} |
| 平均持仓 | {results['avg_holdings']:.1f}只 |
| 平均持有期 | {trades_analysis['avg_hold_days']:.1f}天 |
| 中位持有期 | {trades_analysis['median_hold_days']:.1f}天 |

---

## 三、交易归因分析

### 3.1 按退出原因归因

| 退出原因 | 次数 | 占比 | 平均盈亏 | 胜率 | 总盈亏贡献 |
|----------|------|------|----------|------|------------|
"""
    
    total_exits = sum(trades_analysis['exit_categories'].values())
    for reason, count in trades_analysis['exit_categories'].items():
        pct = count / total_exits if total_exits > 0 else 0
        subset = [t for t in trades_analysis['completed_trades'] if reason in t['exit_reason'] or (reason == '止损' and 'STOP' in t['exit_action']) or (reason == '调出候选列表' and '候选' in t['exit_reason'])]
        if subset:
            avg_pnl = np.mean([t['pnl_pct'] for t in subset])
            win_rate = np.mean([t['pnl_pct'] > 0 for t in subset])
            total_pnl = sum([t['pnl_pct'] for t in subset])
        else:
            avg_pnl = 0
            win_rate = 0
            total_pnl = 0
        md += f"| {reason} | {count} | {pct:.1%} | {avg_pnl:+.2%} | {win_rate:.1%} | {total_pnl:+.2%} |\n"
    
    md += f"""
### 3.2 按持有期归因

| 持有期 | 次数 | 占比 | 平均盈亏 | 胜率 | 总盈亏贡献 |
|--------|------|------|----------|------|------------|
"""
    
    total_completed = len(trades_analysis['completed_trades'])
    for label, stats in trades_analysis['hold_analysis'].items():
        pct = stats['count'] / total_completed if total_completed > 0 else 0
        md += f"| {label} | {stats['count']} | {pct:.1%} | {stats['avg_pnl']:+.2%} | {stats['win_rate']:.1%} | {stats['total_pnl']:+.2%} |\n"
    
    md += f"""
### 3.3 按ETF/板块归因

| ETF | 名称 | 交易次数 | 平均盈亏 | 胜率 | 总盈亏 | 平均持有 | 最大盈利 | 最大亏损 |
|-----|------|----------|----------|------|--------|----------|----------|----------|
"""
    
    # 按总盈亏排序
    sorted_etfs = sorted(trades_analysis['etf_analysis'].items(), key=lambda x: x[1]['total_pnl'], reverse=True)
    for ticker, stats in sorted_etfs[:20]:
        md += f"| {ticker} | {stats['name']} | {stats['count']} | {stats['avg_pnl']:+.2%} | {stats['win_rate']:.1%} | {stats['total_pnl']:+.2%} | {stats['avg_hold_days']:.1f}天 | {stats['max_gain']:+.2%} | {stats['max_loss']:+.2%} |\n"
    
    md += f"""
### 3.4 最贡献收益的交易（Top 10）

| 标的 | 买入日期 | 卖出日期 | 持有天数 | 盈亏 | 退出原因 |
|------|----------|----------|----------|------|----------|
"""
    
    for t in trades_analysis['top_gainers'][:10]:
        buy_date = str(t['buy_date'])[:10] if t['buy_date'] else 'N/A'
        sell_date = str(t['sell_date'])[:10] if t['sell_date'] else 'N/A'
        md += f"| {t['ticker']} | {buy_date} | {sell_date} | {t['hold_days']} | {t['pnl_pct']:+.2%} | {str(t['exit_reason'])[:20]} |\n"
    
    md += f"""
### 3.5 最拖累收益的交易（Bottom 10）

| 标的 | 买入日期 | 卖出日期 | 持有天数 | 盈亏 | 退出原因 |
|------|----------|----------|----------|------|----------|
"""
    
    for t in trades_analysis['top_losers'][:10]:
        buy_date = str(t['buy_date'])[:10] if t['buy_date'] else 'N/A'
        sell_date = str(t['sell_date'])[:10] if t['sell_date'] else 'N/A'
        md += f"| {t['ticker']} | {buy_date} | {sell_date} | {t['hold_days']} | {t['pnl_pct']:+.2%} | {str(t['exit_reason'])[:20]} |\n"
    
    md += f"""
---

## 四、每周对比报告（最近20周）

| 周 | 策略收益 | 买入 | 卖出 | 抓住Top5 | 持仓标的 |
|------|----------|------|------|----------|----------|
"""
    
    for r in weekly_reports[-20:]:
        md += f"| {r['week']} | {r['strategy_return']:+.2%} | {r['num_buys']} | {r['num_sells']} | {r['caught_top5_count']}/{len(r['top5'])} | {', '.join(r['our_tickers'][:5]) if r['our_tickers'] else '空仓'} |\n"
    
    md += f"""
---

> 完整逐周数据请查看 JSON 文件。
"""
    
    return md


def main():
    print("="*80)
    print("诊断报告生成器")
    print("="*80)
    
    # 1. 获取元数据
    print("\n[1/5] 获取元数据...")
    git_info = get_git_info()
    db_info = get_db_info()
    env_info = get_environment_info()
    
    # 2. 配置快照
    print("[2/5] 获取配置快照...")
    config_snapshot = {
        'strategy_config': dict(STRATEGY_CONFIG),
        'trading_rules_config': dict(TRADING_RULES_CONFIG),
        'backtest_config': dict(BACKTEST_CONFIG),
        'universe': {
            'etf_universe': {k: v for k, v in ETF_UNIVERSE.items()},
            'defense_universe': {k: v for k, v in DEFENSE_UNIVERSE.items()},
            'fallback_equity_universe': {k: v for k, v in FALLBACK_EQUITY_UNIVERSE.items()},
            'concept_universe': {k: v for k, v in CONCEPT_UNIVERSE.items()},
            'benchmark': BENCHMARK,
            'core_universe': list(CORE_UNIVERSE.keys()),
        },
    }
    
    # 3. 运行回测
    print("[3/5] 运行回测...")
    db = ETFDatabase()
    market_df = db.get_market_data()
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    engine = BacktestEngine(STRATEGY_CONFIG)
    results = engine.run(market_df, bench_df)
    
    print(f"    总收益: {results['total_return']:+.2%}, 夏普: {results['sharpe_ratio']:.2f}, 回撤: {results['max_drawdown']:.2%}")
    
    trades_df = results['trades_df']
    nav_df = results['nav_df']
    
    # 4. 归因分析
    print("[4/5] 交易归因分析...")
    trades_analysis = analyze_trades(trades_df)
    print(f"    完成交易: {len(trades_analysis['completed_trades'])}, 买入: {trades_analysis['total_buys']}, 卖出: {trades_analysis['total_sells']}, 止损: {trades_analysis['total_stops']}")
    print(f"    平均持有: {trades_analysis['avg_hold_days']:.1f}天, 中位: {trades_analysis['median_hold_days']:.1f}天")
    
    print("[4/5] 每周报告...")
    weekly_reports = generate_weekly_report(market_df, trades_df, nav_df)
    print(f"    共 {len(weekly_reports)} 周")
    
    # 5. 记录基线
    print("[5/5] 记录基线...")
    baseline_id, record_path = record_baseline(
        results,
        output_files=[],
        notes=f'诊断报告: 周四调仓, 冷静期0, 无动态止盈, 仓位20%, 固定止损-8%'
    )
    print(f"    基线ID: {baseline_id}")
    
    # 6. 生成报告
    meta = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'baseline_id': baseline_id,
        'git': git_info,
        'db': db_info,
        'env': env_info,
    }
    
    md = generate_diagnostic_report(results, trades_analysis, weekly_reports, config_snapshot, meta)
    
    report_path = f'reports/diagnostic_report_{baseline_id}.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(md)
    
    print(f"\n诊断报告已保存: {report_path}")
    
    # 7. 保存 JSON
    json_data = {
        'meta': meta,
        'config': config_snapshot,
        'backtest_results': {
            'total_return': results['total_return'],
            'annual_return': results['annual_return'],
            'sharpe_ratio': results['sharpe_ratio'],
            'max_drawdown': results['max_drawdown'],
            'num_trades': results['num_trades'],
            'win_rate': results['win_rate'],
        },
        'trades_analysis': trades_analysis,
        'weekly_reports': weekly_reports,
    }
    
    json_path = f'reports/diagnostic_data_{baseline_id}.json'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2, default=str)
    
    print(f"数据已保存: {json_path}")
    
    return baseline_id, report_path


if __name__ == '__main__':
    main()
