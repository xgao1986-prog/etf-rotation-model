# -*- coding: utf-8 -*-
"""
split_experiment.py - 拆分实验：A/B/C/D 四组独立变量测试

A. 基线（无修改）
B. 只加卖出防抖（exit_debounce=2）
C. 只加最短持有（min_hold_for_candidate_exit=7）
D. 只加同类ETF分组（same_group_max=1）
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
from datetime import datetime

from config import STRATEGY_CONFIG
from backtest import BacktestEngine
from database import ETFDatabase
from baseline_recorder import record_baseline


def calculate_hold_metrics(trades_df):
    """计算持有期指标"""
    trades_df = trades_df.copy()
    trades_df['date'] = pd.to_datetime(trades_df['date'])
    
    completed = []
    for ticker, tdf in trades_df.groupby('ticker'):
        tdf = tdf.sort_values('date').reset_index(drop=True)
        buy_queue = []
        for _, row in tdf.iterrows():
            if row['action'] == 'BUY':
                buy_queue.append(row)
            elif row['action'] in ['SELL', 'STOP_LOSS']:
                if buy_queue:
                    buy = buy_queue.pop(0)
                    hold_days = (row['date'] - buy['date']).days
                    completed.append({
                        'ticker': ticker,
                        'hold_days': hold_days,
                        'pnl_pct': row['pnl_pct'],
                        'exit_action': row['action'],
                        'exit_reason': row['reason'],
                    })
    
    completed_df = pd.DataFrame(completed)
    if len(completed_df) == 0:
        return {}
    
    return {
        'avg_hold_days': completed_df['hold_days'].mean(),
        'median_hold_days': completed_df['hold_days'].median(),
        'hold_le_14': (completed_df['hold_days'] <= 14).sum(),
        'hold_le_20': (completed_df['hold_days'] <= 20).sum(),
        'hold_gt_30': (completed_df['hold_days'] > 30).sum(),
        'exit_by_candidate': sum(1 for r in completed_df['exit_reason'] if '候选' in r or '调出' in r or '跌出' in r),
        'exit_by_stop': (completed_df['exit_action'] == 'STOP_LOSS').sum(),
        'exit_by_other': len(completed_df) - sum(1 for r in completed_df['exit_reason'] if '候选' in r or '调出' in r or '跌出' in r) - (completed_df['exit_action'] == 'STOP_LOSS').sum(),
    }


def run_experiment():
    db = ETFDatabase()
    market_df = db.get_market_data()
    bench_df = db.get_market_data(ticker='000300.SH')
    
    print("="*100)
    print("拆分实验：A/B/C/D 四组独立变量测试")
    print("="*100)
    
    configs = {
        'A': {
            'name': '基线',
            'desc': '无修改',
            'params': {
                'exit_debounce': 0,
                'min_hold_for_candidate_exit': 0,
                'same_group_max_holdings': 0,
            },
        },
        'B': {
            'name': '只加卖出防抖',
            'desc': 'exit_debounce=2',
            'params': {
                'exit_debounce': 2,
                'min_hold_for_candidate_exit': 0,
                'same_group_max_holdings': 0,
            },
        },
        'C': {
            'name': '只加最短持有',
            'desc': 'min_hold_for_candidate_exit=7',
            'params': {
                'exit_debounce': 0,
                'min_hold_for_candidate_exit': 7,
                'same_group_max_holdings': 0,
            },
        },
        'D': {
            'name': '只加同类分组',
            'desc': 'same_group_max=1',
            'params': {
                'exit_debounce': 0,
                'min_hold_for_candidate_exit': 0,
                'same_group_max_holdings': 1,
            },
        },
    }
    
    results = {}
    
    for key, cfg_info in configs.items():
        print(f"\n[{key}] {cfg_info['name']} ({cfg_info['desc']})...")
        cfg = STRATEGY_CONFIG.copy()
        for k, v in cfg_info['params'].items():
            cfg[k] = v
        
        engine = BacktestEngine(cfg)
        r = engine.run(market_df, bench_df)
        
        # 计算持有期指标
        hold_metrics = calculate_hold_metrics(r['trades_df'])
        
        results[key] = {
            'backtest': r,
            'hold_metrics': hold_metrics,
            'config': cfg_info,
        }
        
        print(f"    Return: {r['total_return']:+.2%}, Sharpe: {r['sharpe_ratio']:.2f}, MaxDD: {r['max_drawdown']:.2%}")
        print(f"    Trades: {r['num_trades']}, Stops: {r['stop_loss_count']}")
        if hold_metrics:
            print(f"    AvgHold: {hold_metrics['avg_hold_days']:.1f}d, Median: {hold_metrics['median_hold_days']:.1f}d")
            print(f"    ≤14d: {hold_metrics['hold_le_14']}, ≤20d: {hold_metrics['hold_le_20']}, >30d: {hold_metrics['hold_gt_30']}")
            print(f"    Exit-Candidate: {hold_metrics['exit_by_candidate']}, Stop: {hold_metrics['exit_by_stop']}, Other: {hold_metrics['exit_by_other']}")
    
    # 打印对比表
    print("\n" + "="*100)
    print("对比结果")
    print("="*100)
    
    headers = ['组别', '名称', '总收益', '夏普', '最大回撤', '交易', '止损', '中位持有', '≤14天', '候选退出', '止损退出', '其他退出']
    print(f"{' | '.join(headers)}")
    print("-"*120)
    
    for key in ['A', 'B', 'C', 'D']:
        r = results[key]['backtest']
        h = results[key]['hold_metrics']
        c = results[key]['config']
        print(f"{key} | {c['name']:<12} | {r['total_return']:>+7.2%} | {r['sharpe_ratio']:>6.2f} | {r['max_drawdown']:>7.2%} | {r['num_trades']:>6} | {r['stop_loss_count']:>6} | {h.get('median_hold_days', 0):>7.1f}d | {h.get('hold_le_14', 0):>6} | {h.get('exit_by_candidate', 0):>8} | {h.get('exit_by_stop', 0):>8} | {h.get('exit_by_other', 0):>8}")
    
    print("="*100)
    
    # 记录基线
    print("\n记录基线...")
    baseline_ids = {}
    for key in ['A', 'B', 'C', 'D']:
        bid, path = record_baseline(
            results[key]['backtest'],
            output_files=[],
            notes=f"拆分实验{key}: {results[key]['config']['name']}"
        )
        baseline_ids[key] = bid
        print(f"  {key}: {bid}")
    
    # 生成对比报告
    report = f"""# 拆分实验报告：A/B/C/D 四组独立变量测试

> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 实验设计

| 组别 | 名称 | 修改内容 | 其他参数 |
|------|------|----------|----------|
| A | 基线 | 无 | 全部默认 |
| B | 只加卖出防抖 | 跌出候选列表后连续2次调仓确认再卖 | 其他不变 |
| C | 只加最短持有 | 持有<7天不因调出候选而卖（止损例外） | 其他不变 |
| D | 只加同类分组 | 同类ETF最多持有1只 | 其他不变 |

## 对比结果

| 组别 | 总收益 | 夏普 | 最大回撤 | 交易 | 止损 | 中位持有 | ≤14天 | 候选退出 | 止损退出 | 其他退出 |
|------|--------|------|----------|------|------|----------|-------|----------|----------|----------|
"""
    for key in ['A', 'B', 'C', 'D']:
        r = results[key]['backtest']
        h = results[key]['hold_metrics']
        c = results[key]['config']
        report += f"| {key} ({c['name']}) | {r['total_return']:+.2%} | {r['sharpe_ratio']:.2f} | {r['max_drawdown']:.2%} | {r['num_trades']} | {r['stop_loss_count']} | {h.get('median_hold_days', 0):.1f}d | {h.get('hold_le_14', 0)} | {h.get('exit_by_candidate', 0)} | {h.get('exit_by_stop', 0)} | {h.get('exit_by_other', 0)} |\n"
    
    report += """
## 分析

### A vs B: 卖出防抖（exit_debounce=2）

- 效果：减少单次排名波动导致的过早卖出
- 观察：交易次数、收益、夏普、回撤变化

### A vs C: 最短持有（min_hold=7）

- 效果：限制持有<7天的标的被过早卖出
- 观察：中位持有期、候选退出次数变化

### A vs D: 同类分组（same_group_max=1）

- 效果：避免同类ETF重复持仓
- 观察：分散度、收益、回撤变化

### 关键结论

- 哪个单一因素对收益/夏普/回撤影响最大？
- 哪个因素对降低"候选退出"最有效？
- 哪个因素对提高持有期最有效？

## 下一步：板块数据提前入场设计

基于实验结果，下一阶段方向：
1. 轻量卖出防抖（仅对核心参数调优）
2. 板块数据提前入场：使用行业板块数据比ETF数据更早发出信号

## 基线ID

"""
    for key, bid in baseline_ids.items():
        report += f"- {key}: {bid}\n"
    
    with open('reports/split_experiment.md', 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"\n对比报告已保存: reports/split_experiment.md")
    
    return results, baseline_ids


if __name__ == '__main__':
    run_experiment()
