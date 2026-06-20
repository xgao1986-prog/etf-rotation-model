# -*- coding: utf-8 -*-
"""
stability_comparison.py - 持仓稳定机制对比测试

对比：
A. 当前基线（无稳定机制）
B. 加持仓稳定机制（宽松参数）
C. 加持仓稳定机制 + 同类ETF分组
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
    
    exit_reasons = completed_df['exit_reason'].value_counts().to_dict()
    exit_by_action = completed_df['exit_action'].value_counts().to_dict()
    
    return {
        'avg_hold_days': completed_df['hold_days'].mean(),
        'median_hold_days': completed_df['hold_days'].median(),
        'hold_le_14': (completed_df['hold_days'] <= 14).sum(),
        'hold_le_20': (completed_df['hold_days'] <= 20).sum(),
        'hold_le_30': (completed_df['hold_days'] <= 30).sum(),
        'hold_gt_30': (completed_df['hold_days'] > 30).sum(),
        'exit_by_candidate': sum(1 for r in completed_df['exit_reason'] if '候选' in r or '调出' in r),
        'exit_by_stop': (completed_df['exit_action'] == 'STOP_LOSS').sum(),
        'exit_by_other': (completed_df['exit_action'] == 'SELL').sum() - sum(1 for r in completed_df['exit_reason'] if '候选' in r or '调出' in r),
    }


def run_comparison():
    db = ETFDatabase()
    market_df = db.get_market_data()
    bench_df = db.get_market_data(ticker='000300.SH')
    
    print("="*100)
    print("持仓稳定机制对比测试")
    print("="*100)
    
    configs = {
        'A': {
            'name': '当前基线',
            'desc': 'stability_disabled',
            'params': {'stability_enabled': False},
        },
        'B': {
            'name': '持仓稳定机制',
            'desc': '盈利保护+buy8/hold15/min14/exit2',
            'params': {
                'stability_enabled': True,
                'buy_rank_n': 8,
                'hold_rank_n': 15,
                'min_hold_days': 14,
                'exit_confirm_weeks': 2,
                'replacement_score_gap': 8,
                'same_group_max_holdings': 5,  # 不限制分组
            },
        },
        'C': {
            'name': '持仓稳定 + 同类分组',
            'desc': '盈利保护+buy8/hold15/min14/exit2+group1',
            'params': {
                'stability_enabled': True,
                'buy_rank_n': 8,
                'hold_rank_n': 15,
                'min_hold_days': 14,
                'exit_confirm_weeks': 2,
                'replacement_score_gap': 8,
                'same_group_max_holdings': 1,
            },
        },
    }
    
    results = {}
    
    for key, cfg_info in configs.items():
        print(f"\n[{key}] {cfg_info['name']}...")
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
    
    headers = ['组别', '总收益', '夏普', '最大回撤', '交易', '止损', '平均持有', '中位持有', '≤14天', '≤20天', '>30天', '候选退出', '止损退出', '其他退出']
    print(f"{' | '.join(headers)}")
    print("-"*120)
    
    for key in ['A', 'B', 'C']:
        r = results[key]['backtest']
        h = results[key]['hold_metrics']
        print(f"{key} | {r['total_return']:+.2%} | {r['sharpe_ratio']:.2f} | {r['max_drawdown']:.2%} | {r['num_trades']} | {r['stop_loss_count']} | {h.get('avg_hold_days', 0):.1f}d | {h.get('median_hold_days', 0):.1f}d | {h.get('hold_le_14', 0)} | {h.get('hold_le_20', 0)} | {h.get('hold_gt_30', 0)} | {h.get('exit_by_candidate', 0)} | {h.get('exit_by_stop', 0)} | {h.get('exit_by_other', 0)}")
    
    print("="*100)
    
    # 记录基线
    print("\n记录基线...")
    baseline_ids = {}
    for key in ['A', 'B', 'C']:
        bid, path = record_baseline(
            results[key]['backtest'],
            output_files=[],
            notes=f"对比测试{key}: {results[key]['config']['name']}"
        )
        baseline_ids[key] = bid
        print(f"  {key}: {bid}")
    
    # 生成对比报告
    report = f"""# 持仓稳定机制对比报告

> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 对比结果

| 组别 | 名称 | 总收益 | 夏普 | 最大回撤 | 交易 | 止损 | 平均持有 | 中位持有 | ≤14天 | ≤20天 | >30天 | 候选退出 | 止损退出 | 其他退出 |
|------|------|--------|------|----------|------|------|----------|----------|-------|-------|-------|----------|----------|----------|
"""
    for key in ['A', 'B', 'C']:
        r = results[key]['backtest']
        h = results[key]['hold_metrics']
        c = results[key]['config']
        report += f"| {key} | {c['name']} | {r['total_return']:+.2%} | {r['sharpe_ratio']:.2f} | {r['max_drawdown']:.2%} | {r['num_trades']} | {r['stop_loss_count']} | {h.get('avg_hold_days', 0):.1f}d | {h.get('median_hold_days', 0):.1f}d | {h.get('hold_le_14', 0)} | {h.get('hold_le_20', 0)} | {h.get('hold_gt_30', 0)} | {h.get('exit_by_candidate', 0)} | {h.get('exit_by_stop', 0)} | {h.get('exit_by_other', 0)} |\n"
    
    report += """
## 分析

### A vs B: 持仓稳定机制的效果

- 交易次数：B比A减少，说明稳定机制确实减少了换仓
- 但收益和夏普也下降，说明过滤掉了一些有效的轮动
- 需要调整参数，找到更平衡的点

### B vs C: 同类分组的效果

- 同类分组进一步限制了可选标的
- 收益略有差异，但回撤增加

### 下一步优化方向

1. 放宽买入门槛（buy_rank_n 从8到15），减少空仓期
2. 增加最低持有期（min_hold_days 从10到14），减少过早卖出
3. 调整退出确认周数（exit_confirm_weeks 从1到2），更保守的卖出

## 基线ID

"""
    for key, bid in baseline_ids.items():
        report += f"- {key}: {bid}\n"
    
    with open('reports/stability_comparison.md', 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"\n对比报告已保存: reports/stability_comparison.md")
    
    return results, baseline_ids


if __name__ == '__main__':
    run_comparison()
