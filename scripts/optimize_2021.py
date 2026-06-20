#!/usr/bin/env python3
"""
2021年优化方案测试
核心问题：题材横跳、一日游、无持续性
优化方向：
  1. 持续性过滤器（Sustained Momentum）
  2. 市场质量门控（Market Quality Gate）
  3. 组合方案

不修改核心代码，通过实验性开关实现。
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from copy import deepcopy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine
from rebalance_planner import plan_rebalance_v2_5

# 保存原始方法
_original_rebalance_v2 = BacktestEngine._rebalance_v2


def _rebalance_v2_optimized(self, portfolio, day_signals, day_prices, effective_close_prices,
                             last_valid_close, date, date_str, buy_signals, trade_records,
                             cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                             _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                             buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                             min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                             calc_commission):
    """
    优化版本：支持市场质量门控和持续性过滤
    """
    
    current_positions = {t: p['shares'] for t, p in portfolio['positions'].items()}
    
    raw_industry_candidates = []
    raw_defense_candidates = []
    
    for _, row in buy_signals.iterrows():
        ticker = row['ticker']
        score = row['total_score']
        
        # 冷却期检查
        if ticker in cooling_list:
            days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
            if days_since_stop < self.cfg.get('cooling_period', 0):
                continue
        
        min_score = self.cfg['min_total_score']
        if ticker in cooling_list:
            days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
            if days_since_stop >= self.cfg.get('cooling_period', 0):
                min_score += self.cfg.get('cooling_score_boost', 0)
        
        if score < min_score:
            continue
        
        if ticker in _defense_tickers:
            raw_defense_candidates.append((ticker, score))
        elif ticker in _core_tickers or ticker in _fallback_tickers:
            raw_industry_candidates.append((ticker, score))
    
    # 排名缓冲
    if rank_buffer_enabled and buy_rank_n is not None:
        raw_industry_candidates = raw_industry_candidates[:buy_rank_n]
    
    # 方案1：市场质量门控
    if self.cfg.get('experiment_market_quality_gate', False):
        if 'market_quality_poor' in day_signals.columns and not day_signals.empty:
            is_poor = day_signals['market_quality_poor'].iloc[0]
            if is_poor:
                # 市场质量差时，只保留防御候选（清空行业候选）
                raw_industry_candidates = []
    
    # 方案2：持续性过滤器（简化版：要求当前已有持仓的ETF，或者之前出现过BUY信号）
    sustained_weeks = self.cfg.get('experiment_sustained_weeks', 0)
    if sustained_weeks > 0 and raw_industry_candidates:
        # 这里简化实现：只保留已有持仓的ETF（已有持仓说明之前被选中过）
        # 更严格的版本需要回溯历史信号
        held_tickers = set(portfolio['positions'].keys())
        filtered = [(t, s) for t, s in raw_industry_candidates if t in held_tickers or sustained_weeks == 0]
        # 如果过滤后为空，保留top候选（避免完全空仓）
        if not filtered and raw_industry_candidates:
            filtered = raw_industry_candidates[:1]  # 至少保留最强候选
        raw_industry_candidates = filtered
    
    min_defense_score = self.cfg.get('min_total_score', 40) - 10
    raw_defense_candidates = [(t, s) for t, s in raw_defense_candidates if s >= min_defense_score]
    
    # 计算NAV
    nav = portfolio['cash'] + sum(
        p['shares'] * day_prices.get(t, 0)
        for t, p in portfolio['positions'].items()
        if day_prices.get(t, 0) > 0
    )
    
    prices = dict(day_prices)
    last_prices = dict(last_valid_close) if last_valid_close else None
    
    orders, state = plan_rebalance_v2_5(
        nav=nav, cash=portfolio['cash'], current_positions=current_positions,
        industry_candidates=raw_industry_candidates, defense_candidates=raw_defense_candidates,
        prices=prices, industry_tickers=set(_core_tickers) | set(_fallback_tickers),
        defense_tickers=set(_defense_tickers), last_prices=last_prices,
        max_industry_holdings=self.cfg['max_holdings'],
        max_defense_holdings=self.cfg.get('defense_max_holdings', 2),
        max_total_holdings=self.cfg.get('total_max_holdings', self.cfg['max_holdings']),
        max_position_per_etf=self.cfg['max_position_per_etf'],
        max_total_position=max_total_position, commission_rate=self.cfg['commission_rate'],
        min_commission=self.cfg['min_commission'], lot_size=100,
    )
    
    # 执行卖出（复制原始逻辑）
    for order in orders:
        if order['action'] != 'SELL':
            continue
        ticker = order['ticker']
        if ticker not in portfolio['positions']:
            continue
        pos = portfolio['positions'][ticker]
        shares = order['shares']
        price = order['price']
        amount = order['amount']
        commission = order['commission']
        net_proceeds = amount - commission
        portfolio['cash'] += net_proceeds
        pnl = (price - pos['cost']) / pos['cost'] if pos['cost'] > 0 else 0
        trade_records.append({
            'date': date_str, 'ticker': ticker, 'action': 'SELL',
            'price': price, 'shares': shares, 'amount': amount,
            'commission': commission, 'pnl_pct': pnl, 'reason': order['reason'],
        })
        if shares >= pos['shares']:
            del portfolio['positions'][ticker]
        else:
            pos['shares'] -= shares
    
    # 执行买入
    for order in orders:
        if order['action'] != 'BUY':
            continue
        ticker = order['ticker']
        shares = order['shares']
        price = order['price']
        amount = order['amount']
        commission = order['commission']
        total_cost = amount + commission
        
        if corr_matrix and date in corr_matrix and ticker in corr_matrix[date]:
            skip = False
            for selected_ticker in portfolio['positions']:
                if selected_ticker in corr_matrix[date][ticker]:
                    corr = corr_matrix[date][ticker][selected_ticker]
                    if corr > corr_threshold:
                        skip = True
                        break
            if skip:
                continue
        
        if same_group_max > 0 and ticker in etf_group_map:
            ticker_group = etf_group_map[ticker]
            group_holdings = [t for t in portfolio['positions'] if t in etf_group_map and etf_group_map[t] == ticker_group]
            if len(group_holdings) >= same_group_max:
                continue
        
        if total_cost > portfolio['cash']:
            continue
        
        portfolio['cash'] -= total_cost
        
        atr = 0
        ticker_signals = day_signals[day_signals['ticker'] == ticker]
        if not ticker_signals.empty and 'atr_14' in ticker_signals.columns:
            atr = ticker_signals['atr_14'].iloc[0]
        
        if ticker in portfolio['positions']:
            old_pos = portfolio['positions'][ticker]
            old_shares = old_pos['shares']
            old_cost = old_pos['cost']
            new_shares = old_shares + shares
            new_cost = (old_shares * old_cost + shares * price) / new_shares if new_shares > 0 else price
            old_pos['shares'] = new_shares
            old_pos['cost'] = new_cost
            old_pos['high_water'] = max(old_pos['high_water'], price)
        else:
            portfolio['positions'][ticker] = {
                'shares': shares, 'cost': price, 'entry_date': date_str,
                'high_water': price, 'days_held': 0, 'atr_at_entry': atr,
            }
        
        trade_records.append({
            'date': date_str, 'ticker': ticker, 'action': 'BUY',
            'price': price, 'shares': shares, 'amount': amount,
            'commission': commission, 'pnl_pct': 0, 'reason': order['reason'],
        })
        
        if ticker in cooling_list:
            del cooling_list[ticker]


def run_scenario(name, cfg_override, market_df, bench_df):
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg.update(cfg_override)
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    
    # 提取2021年1-7月数据
    nav_df = result['nav_df'].copy()
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    
    period_2021 = nav_df[
        (nav_df['date'] >= '2021-01-01') & 
        (nav_df['date'] <= '2021-07-31')
    ]
    
    if not period_2021.empty:
        nav_start = period_2021['nav'].iloc[0]
        nav_end = period_2021['nav'].iloc[-1]
        period_return = (nav_end / nav_start) - 1
        
        bench_start = period_2021['bench_price'].iloc[0]
        bench_end = period_2021['bench_price'].iloc[-1]
        bench_return = (bench_end / bench_start) - 1
        
        excess = period_return - bench_return
        avg_positions = period_2021['num_positions'].mean()
        empty_days = (period_2021['num_positions'] == 0).sum()
    else:
        period_return = bench_return = excess = avg_positions = empty_days = 0
    
    return {
        'name': name,
        'total_return': result['total_return'],
        'annual_return': result['annual_return'],
        'sharpe_ratio': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
        'period_2021_return': period_return,
        'period_2021_bench': bench_return,
        'period_2021_excess': excess,
        'period_2021_avg_pos': avg_positions,
        'period_2021_empty_days': empty_days,
    }


def main():
    print("=" * 70)
    print("2021年1-7月优化方案测试")
    print("=" * 70)
    
    db = ETFDatabase()
    b0_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=b0_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    print(f"\n数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只")
    
    scenarios = [
        ('A. 原始逻辑', {}),
        ('B. 门控：差市只买防御', {'experiment_market_quality_gate': True}),
        ('C. 门控：差市+只持有', {'experiment_market_quality_gate': True, 'experiment_sustained_weeks': 1}),
    ]
    
    results = []
    for name, cfg_override in scenarios:
        print(f"\n[Running] {name}...")
        
        # 恢复原始方法
        BacktestEngine._rebalance_v2 = _original_rebalance_v2
        
        # 应用实验patch
        if cfg_override.get('experiment_market_quality_gate') or cfg_override.get('experiment_sustained_weeks'):
            BacktestEngine._rebalance_v2 = _rebalance_v2_optimized
        
        try:
            result = run_scenario(name, cfg_override, market_df, bench_df)
            results.append(result)
            print(f"  全期总收益: {result['total_return']:.2%}")
            print(f"  2021-01~07: 策略{result['period_2021_return']:.2%} 基准{result['period_2021_bench']:.2%} 超额{result['period_2021_excess']:.2%}")
            print(f"  2021-01~07: 平均持仓{result['period_2021_avg_pos']:.1f}只 空仓{result['period_2021_empty_days']}天")
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
    
    # 恢复原始
    BacktestEngine._rebalance_v2 = _original_rebalance_v2
    
    # 对比报告
    print("\n" + "=" * 70)
    print("优化方案对比结果")
    print("=" * 70)
    
    baseline = results[0] if results else None
    print(f"\n{'方案':<30} {'全期总收益':>10} {'2021年收益':>10} {'基准':>10} {'超额':>10} {'平均持仓':>8} {'空仓天数':>8}")
    print("-" * 90)
    for r in results:
        print(f"{r['name']:<30} {r['total_return']:>10.2%} {r['period_2021_return']:>10.2%} "
              f"{r['period_2021_bench']:>10.2%} {r['period_2021_excess']:>+10.2%} "
              f"{r['period_2021_avg_pos']:>8.1f} {r['period_2021_empty_days']:>8}")
    
    if results:
        best_2021 = max(results, key=lambda x: x['period_2021_excess'])
        print(f"\n2021年1-7月最佳方案: {best_2021['name']} (超额 {best_2021['period_2021_excess']:.2%})")
        
        # 保存报告
        lines = []
        lines.append('# 2021年1-7月优化方案测试报告')
        lines.append('')
        lines.append(f'**测试时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
        lines.append('')
        lines.append('## 对比结果')
        lines.append('')
        lines.append('| 方案 | 全期总收益 | 2021年1-7月收益 | 基准 | 超额 | 平均持仓 | 空仓天数 |')
        lines.append('|------|-----------|----------------|------|------|----------|----------|')
        for r in results:
            lines.append(f"| {r['name']} | {r['total_return']:.2%} | {r['period_2021_return']:.2%} | "
                       f"{r['period_2021_bench']:.2%} | {r['period_2021_excess']:+.2%} | "
                       f"{r['period_2021_avg_pos']:.1f} | {r['period_2021_empty_days']} |")
        lines.append('')
        lines.append('## 结论')
        lines.append('')
        if best_2021['name'] != 'A. 原始逻辑':
            lines.append(f'**最佳方案**: {best_2021["name"]}，2021年1-7月超额{best_2021["period_2021_excess"]:.2%}')
            lines.append(f'**vs 原始逻辑**: 超额提升 {best_2021["period_2021_excess"] - baseline["period_2021_excess"]:.2%}')
        else:
            lines.append('**结论**: 优化方案未能改善2021年1-7月表现，原始逻辑已是最优。')
        lines.append('')
        lines.append('---')
        
        report_path = 'D:/etf_rotation_model/reports/optimize_2021_test.md'
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        print(f"\n报告已保存: {report_path}")


if __name__ == '__main__':
    main()
