#!/usr/bin/env python3
"""
A/B测试脚本v2 - 正确实现各实验场景
通过配置参数控制，不修改默认代码路径
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from copy import deepcopy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine
from rebalance_planner import plan_rebalance_v2_5

# 保存原始方法
_original_rebalance_v2 = BacktestEngine._rebalance_v2
_original_generate_signals = StrategyEngine.generate_signals


# ========== 实验1: P0 - 空仓强制防御资产 ==========
def _rebalance_v2_p0(self, portfolio, day_signals, day_prices, effective_close_prices,
                     last_valid_close, date, date_str, buy_signals, trade_records,
                     cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                     _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                     buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                     min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                     calc_commission):
    """P0: 空仓时强制配置防御资产"""
    
    current_positions = {t: p['shares'] for t, p in portfolio['positions'].items()}
    
    raw_industry_candidates = []
    raw_defense_candidates = []
    
    for _, row in buy_signals.iterrows():
        ticker = row['ticker']
        score = row['total_score']
        
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
    
    # P0核心逻辑：如果空仓且没有防御候选，强制添加防御资产
    if self.cfg.get('experiment_empty_defense', False):
        is_empty = len(current_positions) == 0 and len(raw_industry_candidates) == 0
        if is_empty:
            for ticker in _defense_tickers:
                if ticker in effective_close_prices and effective_close_prices[ticker] > 0:
                    if ticker not in [t for t, _ in raw_defense_candidates]:
                        raw_defense_candidates.append((ticker, 50.0))
    
    if rank_buffer_enabled and buy_rank_n is not None:
        raw_industry_candidates = raw_industry_candidates[:buy_rank_n]
    
    min_defense_score = self.cfg.get('min_total_score', 40) - 10
    raw_defense_candidates = [(t, s) for t, s in raw_defense_candidates if s >= min_defense_score]
    
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
    
    # 执行卖出
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


# ========== 实验2: P2 - MA20卖出缓冲 ==========
def generate_signals_p2(self, scores_df, bench_df):
    """P2: MA20卖出缓冲2%"""
    if self.cfg['market_timing'] and bench_df is not None:
        bench_signals = self.market_timing(bench_df)[['date', 'market_signal']]
        scores_df = scores_df.merge(bench_signals, on='date', how='left')
        scores_df['market_signal'] = scores_df['market_signal'].fillna(1.0)
    else:
        scores_df['market_signal'] = 1.0
    
    scores_df['prev_close'] = scores_df.groupby('ticker')['close'].shift(1)
    scores_df['signal_type'] = 'HOLD'
    
    if bench_df is not None and not bench_df.empty:
        bench_sorted = bench_df.sort_values('date').copy()
        bench_sorted['bench_ma50'] = bench_sorted['close'].rolling(self.cfg['market_ma_long']).mean().shift(1)
        bench_sorted['bench_ma50_slope'] = bench_sorted['bench_ma50'].diff().shift(1)
        bench_sorted['bull_market'] = (
            (bench_sorted['close'].shift(1) > bench_sorted['bench_ma50']) &
            (bench_sorted['bench_ma50_slope'] > 0)
        )
        scores_df = scores_df.merge(bench_sorted[['date', 'bull_market']], on='date', how='left')
        scores_df['bull_market'] = scores_df['bull_market'].fillna(False)
    else:
        scores_df['bull_market'] = True
    
    import config as _cfg_module
    _core_tickers = list(getattr(_cfg_module, 'CORE_UNIVERSE', {}).keys())
    core_only = scores_df[scores_df['ticker'].isin(_core_tickers)]
    market_quality = core_only.groupby('date')['momentum_20'].median().reset_index()
    market_quality.columns = ['date', 'market_quality_median']
    scores_df = scores_df.merge(market_quality, on='date', how='left')
    scores_df['market_quality_poor'] = scores_df['market_quality_median'] < 0
    
    effective_min_total = np.where(scores_df['market_quality_poor'], 55, self.cfg['min_total_score'])
    mature_mask = scores_df['history_count'] >= 51
    
    core_mask = scores_df['ticker'].isin(_core_tickers) & mature_mask & (
        (scores_df['trend_score'] >= self.cfg['min_trend_score']) &
        (scores_df['confirm_score'] >= self.cfg['min_confirm_score']) &
        (scores_df['total_score'] >= effective_min_total) &
        (scores_df['prev_close'] > scores_df['ma20']) &
        (scores_df['ma20_slope'] > 0)
    )
    
    _fallback_tickers = list(getattr(_cfg_module, 'FALLBACK_EQUITY_UNIVERSE', {}).keys())
    fallback_mask = scores_df['ticker'].isin(_fallback_tickers) & mature_mask & scores_df['bull_market'] & (
        (scores_df['trend_score'] >= 10) & (scores_df['confirm_score'] >= 2) &
        (scores_df['total_score'] >= 25) & (scores_df['prev_close'] > scores_df['ma20'] * 0.98) &
        (scores_df['ma20_slope'] > -0.01)
    )
    
    _defense_tickers = list(_cfg_module.DEFENSE_UNIVERSE.keys())
    defense_mask = scores_df['ticker'].isin(_defense_tickers) & mature_mask & (
        (scores_df['trend_score'] >= 10) & (scores_df['confirm_score'] >= 2) &
        (scores_df['total_score'] >= 30) & (scores_df['prev_close'] > scores_df['ma20'] * 0.98) &
        (scores_df['ma20_slope'] > -0.001)
    )
    
    scores_df.loc[core_mask | fallback_mask | defense_mask, 'signal_type'] = 'BUY'
    
    # P2核心修改：MA20卖出缓冲
    buffer = self.cfg.get('experiment_ma20_sell_buffer', 1.0)
    sell_mask = scores_df['prev_close'] < scores_df['ma20'] * buffer
    scores_df.loc[sell_mask, 'signal_type'] = 'SELL'
    
    return scores_df


# ========== 运行测试 ==========
def run_scenario(name, cfg_override, market_df, bench_df):
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg.update(cfg_override)
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    
    return {
        'name': name,
        'total_return': result['total_return'],
        'annual_return': result['annual_return'],
        'sharpe_ratio': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
        'avg_holdings': result['avg_holdings'],
        'win_rate': result['win_rate'],
    }


def main():
    print("=" * 70)
    print("A/B测试v2 - 改进场景对比")
    print("=" * 70)
    
    db = ETFDatabase()
    b0_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=b0_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    print(f"\n数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只")
    print(f"日期: {market_df['date'].min()} ~ {market_df['date'].max()}")
    
    scenarios = [
        ('A. 原始逻辑', {}),
        ('B. P0: 空仓强制防御', {'experiment_empty_defense': True}),
        ('C. P2: MA20卖出缓冲2%', {'experiment_ma20_sell_buffer': 0.98}),
        ('D. P3: 空仓加速入场', {'experiment_empty_accel': True}),
        ('E. P0+P2', {'experiment_empty_defense': True, 'experiment_ma20_sell_buffer': 0.98}),
        ('F. P0+P2+P3', {'experiment_empty_defense': True, 'experiment_ma20_sell_buffer': 0.98, 'experiment_empty_accel': True}),
    ]
    
    results = []
    for name, cfg_override in scenarios:
        print(f"\n[Running] {name}...")
        
        # 恢复原始方法
        BacktestEngine._rebalance_v2 = _original_rebalance_v2
        StrategyEngine.generate_signals = _original_generate_signals
        
        # 应用实验patch
        if cfg_override.get('experiment_empty_defense'):
            BacktestEngine._rebalance_v2 = _rebalance_v2_p0
        if cfg_override.get('experiment_ma20_sell_buffer', 1.0) < 1.0:
            StrategyEngine.generate_signals = generate_signals_p2
        
        try:
            result = run_scenario(name, cfg_override, market_df, bench_df)
            results.append(result)
            print(f"  OK: 总收益{result['total_return']:.2%} 年化{result['annual_return']:.2%} "
                  f"夏普{result['sharpe_ratio']:.2f} 回撤{result['max_drawdown']:.2%} 交易{result['num_trades']}")
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
    
    # 恢复原始
    BacktestEngine._rebalance_v2 = _original_rebalance_v2
    StrategyEngine.generate_signals = _original_generate_signals
    
    # 对比报告
    print("\n" + "=" * 70)
    print("A/B测试对比结果")
    print("=" * 70)
    
    baseline = results[0] if results else None
    print(f"\n{'场景':<22} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>10} {'交易':>8} {'总收益Δ':>8} {'胜率':>8}")
    print("-" * 80)
    for r in results:
        delta = r['total_return'] - baseline['total_return'] if baseline else 0
        print(f"{r['name']:<22} {r['total_return']:>8.2%} {r['annual_return']:>8.2%} "
              f"{r['sharpe_ratio']:>8.2f} {r['max_drawdown']:>10.2%} {r['num_trades']:>8} "
              f"{delta:>+8.2%} {r['win_rate']:>8.1%}")
    
    if results:
        best = max(results, key=lambda x: x['total_return'])
        print(f"\n最佳场景: {best['name']} (总收益 {best['total_return']:.2%})")


if __name__ == '__main__':
    main()
