# -*- coding: utf-8 -*-
"""B0 买入逻辑失败测试 - 验证订单分配是否正确"""
import sys, os
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from backtest import BacktestEngine
from strategy import StrategyEngine
from database import ETFDatabase
import config

print("=" * 80)
print("B0 买入逻辑失败测试")
print("=" * 80)

# 基础配置
MAX_POS = config.STRATEGY_CONFIG['max_position_per_etf']  # 0.20
STOCK_MAX = config.STRATEGY_CONFIG['stock_max_holdings']   # 5
TOTAL_MAX = config.STRATEGY_CONFIG['total_max_holdings']     # 5

def calc_commission(amount):
    return max(amount * 0.0003, 5.0)

def simulate_buy(current_value, available_cash, max_total_position, 
                 ticker, price, base_weight=0.20, is_enhanced=False):
    """模拟单只买入逻辑"""
    
    # 代码逻辑 (backtest.py:1184-1212)
    if is_enhanced:
        max_position_for_ticker = min(MAX_POS / 2, 0.075)
        max_total_for_ticker = min(config.STRATEGY_CONFIG['max_holdings'], 4)
    else:
        max_position_for_ticker = MAX_POS
        max_total_for_ticker = config.STRATEGY_CONFIG['max_holdings']
    
    base_weight = min(max_position_for_ticker, 1.0 / max_total_for_ticker)
    target_amount = current_value * base_weight * max_total_position
    target_amount = min(target_amount, available_cash * 0.95)
    
    print(f"    current_value={current_value:,.0f}, available_cash={available_cash:,.0f}")
    print(f"    base_weight={base_weight:.4f}, max_total_position={max_total_position}")
    print(f"    target_amount (before cash limit) = {current_value * base_weight * max_total_position:,.2f}")
    print(f"    target_amount (after cash limit) = {target_amount:,.2f}")
    
    if target_amount < 1000:
        print(f"    >>> SKIPPED: target_amount < 1000")
        return None, available_cash
    
    shares = int(target_amount / price)
    print(f"    price={price:.3f}, shares (raw) = {int(target_amount / price)}")
    
    if shares < 1:
        print(f"    >>> SKIPPED: shares < 1")
        return None, available_cash
    
    cost = shares * price
    commission = calc_commission(cost)
    total_cost = cost + commission
    
    print(f"    cost={cost:,.2f}, commission={commission:.2f}, total_cost={total_cost:,.2f}")
    
    if total_cost > available_cash:
        print(f"    >>> SKIPPED: total_cost > available_cash ({total_cost:,.2f} > {available_cash:,.2f})")
        return None, available_cash
    
    print(f"    >>> SUCCESS: Buy {shares} shares @ {price:.3f} = {cost:,.2f}")
    remaining_cash = available_cash - total_cost
    print(f"    remaining_cash = {remaining_cash:,.2f}")
    
    return {
        'ticker': ticker,
        'shares': shares,
        'price': price,
        'cost': cost,
        'commission': commission,
        'total_cost': total_cost,
        'target_amount': target_amount,
        'target_ratio': target_amount / current_value,
    }, remaining_cash

# ============================================================
# 测试场景
# ============================================================

def test_scenario(name, current_value, available_cash, max_total_pos, buys):
    print(f"\n{'='*80}")
    print(f"场景: {name}")
    print(f"{'='*80}")
    print(f"current_value={current_value:,.0f}, available_cash={available_cash:,.0f}, max_total_pos={max_total_pos}")
    
    remaining_cash = available_cash
    total_bought = 0
    results = []
    
    for ticker, price, is_enh in buys:
        print(f"\n  [{ticker}] price={price:.3f}:")
        result, remaining_cash = simulate_buy(
            current_value, remaining_cash, max_total_pos, ticker, price, is_enhanced=is_enh
        )
        if result:
            total_bought += result['cost']
            results.append(result)
    
    print(f"\n--- 结果 ---")
    print(f"  Total bought: {total_bought:,.2f} ({total_bought/current_value:.2%})")
    print(f"  Remaining cash: {remaining_cash:,.2f} ({remaining_cash/current_value:.2%})")
    print(f"  Positions: {len(results)}")
    for r in results:
        print(f"    {r['ticker']}: {r['shares']} shares = {r['cost']:,.2f} ({r['cost']/current_value:.2%}), target={r['target_ratio']:.2%}")
    
    return results, remaining_cash

# 测试1: 空仓买入5只，现金充足
# 预期: 每只买入20%，现金剩5%
test_scenario(
    "空仓买入5只，现金充足 (NAV=100万, cash=100万)",
    1_000_000, 1_000_000, 1.0,
    [
        ('ETF1', 2.500, False),
        ('ETF2', 3.000, False),
        ('ETF3', 1.500, False),
        ('ETF4', 4.000, False),
        ('ETF5', 2.000, False),
    ]
)

# 测试2: 保留1只、新买3只，现金充足
# 预期: 新买3只各20%，保留1只，总持仓80%
test_scenario(
    "保留1只、新买3只 (NAV=100万, cash=80万, 保留1只)",
    1_000_000, 800_000, 1.0,
    [
        ('ETF2', 2.500, False),
        ('ETF3', 3.000, False),
        ('ETF4', 1.500, False),
    ]
)

# 测试3: 保留3只、新买2只，现金充足
# 预期: 新买2只各20%，总持仓100%
test_scenario(
    "保留3只、新买2只 (NAV=100万, cash=40万, 保留3只)",
    1_000_000, 400_000, 1.0,
    [
        ('ETF4', 2.500, False),
        ('ETF5', 3.000, False),
    ]
)

# 测试4: 候选少于槽位 (3候选, 5槽位)
# 预期: 3只各20%，现金剩40%
test_scenario(
    "候选少于槽位 (3候选, 5槽位)",
    1_000_000, 1_000_000, 1.0,
    [
        ('ETF1', 2.500, False),
        ('ETF2', 3.000, False),
        ('ETF3', 1.500, False),
    ]
)

# 测试5: market_signal < 1 (max_total_pos=0.8)
# 预期: 每只 target = 20% * 0.8 = 16%
test_scenario(
    "market_signal=0.8 (弱市)",
    1_000_000, 1_000_000, 0.8,
    [
        ('ETF1', 2.500, False),
        ('ETF2', 3.000, False),
        ('ETF3', 1.500, False),
        ('ETF4', 4.000, False),
        ('ETF5', 2.000, False),
    ]
)

# 测试6: 防御资产填充
# 模拟防御买入逻辑 (backtest.py:1333-1400)
print(f"\n{'='*80}")
print("场景: 防御资产填充")
print(f"{'='*80}")

def simulate_defense_fill(current_value, available_cash, max_total_pos, 
                          defense_tickers, defense_prices, current_defense_value=0):
    """模拟防御填充逻辑"""
    
    if max_total_pos >= 1.0:
        defense_fill_max = config.STRATEGY_CONFIG.get('defense_fill_max_ratio_bull', 0.30)
    else:
        defense_fill_max = config.STRATEGY_CONFIG.get('defense_fill_max_ratio_bear', 0.50)
    
    max_defense_target = current_value * defense_fill_max
    defense_fill_allowance = max(0, max_defense_target - current_defense_value)
    fill_target = min(available_cash * 0.95, defense_fill_allowance)
    
    print(f"  defense_fill_max={defense_fill_max:.2%}")
    print(f"  max_defense_target={max_defense_target:,.2f}")
    print(f"  current_defense_value={current_defense_value:,.2f}")
    print(f"  defense_fill_allowance={defense_fill_allowance:,.2f}")
    print(f"  fill_target={fill_target:,.2f}")
    
    remaining_cash = available_cash
    results = []
    
    for i, (ticker, price) in enumerate(zip(defense_tickers, defense_prices)):
        if fill_target < 1000:
            break
        
        slots = len(defense_tickers) - i
        target = fill_target / slots
        target = min(target, remaining_cash * 0.95)
        
        shares = int(target / price)
        if shares < 1:
            continue
        
        cost = shares * price
        commission = calc_commission(cost)
        total_cost = cost + commission
        
        if total_cost > remaining_cash:
            continue
        
        remaining_cash -= total_cost
        fill_target -= cost
        results.append({
            'ticker': ticker,
            'shares': shares,
            'cost': cost,
            'total_cost': total_cost,
        })
        print(f"  [{ticker}] Buy {shares} shares @ {price:.3f} = {cost:,.2f}")
    
    print(f"  Total defense bought: {sum(r['cost'] for r in results):,.2f}")
    print(f"  Remaining cash: {remaining_cash:,.2f}")
    return results

simulate_defense_fill(
    1_000_000, 300_000, 1.0,
    ['DEF1', 'DEF2'], [3.000, 2.500],
    current_defense_value=0
)

# 测试7: 100股取整导致的目标不足
print(f"\n{'='*80}")
print("场景: 100股取整导致的目标不足")
print(f"{'='*80}")

# NAV=100万, 目标20%=20万, 价格=150元, 需要1333.33股 -> 取整1300股 = 19.5万
price = 150.0
target_amount = 200_000
shares = int(target_amount / price)
shares = (shares // 100) * 100
actual_cost = shares * price
print(f"  NAV=1,000,000, target=20%={target_amount:,.0f}")
print(f"  price={price:.2f}")
print(f"  raw shares = {int(target_amount/price)}")
print(f"  rounded shares = {shares} (100-share lot)")
print(f"  actual cost = {actual_cost:,.2f} ({actual_cost/1_000_000:.2%})")
print(f"  shortfall = {target_amount - actual_cost:,.2f} ({(target_amount - actual_cost)/1_000_000:.2%})")

# 测试8: 2026-03-12 模拟
print(f"\n{'='*80}")
print("场景: 2026-03-12 模拟")
print(f"{'='*80}")

# 从诊断结果：NAV=2,164,027, cash=888,962, 保留2只（黄金+油气），新槽=3
# 但只买入了1只（养殖ETF）

# 模拟3只新候选
print("  假设3只候选，价格分别为1.0, 2.0, 3.0:")

current_value = 2_164_027
available_cash = 888_962
max_total_pos = 1.0

for ticker, price in [('CAND1', 1.0), ('CAND2', 2.0), ('CAND3', 3.0)]:
    print(f"\n  [{ticker}] price={price:.2f}:")
    result, available_cash = simulate_buy(
        current_value, available_cash, max_total_pos, ticker, price
    )
    if result:
        current_value -= result['commission']  # 近似：NAV因交易减少

print(f"\n  Final cash: {available_cash:,.2f}")

print(f"\n{'='*80}")
print("测试完成")
print(f"{'='*80}")
