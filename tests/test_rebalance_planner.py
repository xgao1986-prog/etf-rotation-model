import pytest
import sys
import os
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from rebalance_planner import (
    plan_rebalance_v2_4, plan_rebalance_v2_5, _calc_commission, _calc_buy_shares,
    _calc_sell_shares, _get_valuation_price
)


# ========== 测试1: 空仓满额买入（基础场景）==========
def test_empty_portfolio_full_buy():
    """空仓，NAV=100万，5只行业候选，预期买入5只行业，无防御"""
    nav = 1_000_000.0
    cash = 1_000_000.0
    positions = {}
    industry = [
        ('A', 90.0), ('B', 80.0), ('C', 70.0), ('D', 60.0), ('E', 50.0)
    ]
    defense = [('GOLD', 65.0)]
    prices = {'A': 100.0, 'B': 50.0, 'C': 30.0, 'D': 20.0, 'E': 10.0, 'GOLD': 200.0}
    industry_tickers = {'A', 'B', 'C', 'D', 'E'}
    defense_tickers = {'GOLD'}

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    buy_orders = [o for o in orders if o['action'] == 'BUY']
    assert len(buy_orders) == 5, f"Expected 5 BUY orders, got {len(buy_orders)}"

    for o in buy_orders:
        if o['ticker'] in industry_tickers:
            assert o['shares'] > 0
            assert o['shares'] % 100 == 0, "Shares must be multiple of 100"
            expected_amount = o['shares'] * o['price']
            assert abs(o['amount'] - expected_amount) < 0.01
            expected_comm = _calc_commission(expected_amount, 0.0003, 5.0)
            assert abs(o['commission'] - expected_comm) < 0.01

    # 无防御买入
    defense_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] in defense_tickers]
    assert len(defense_buys) == 0, "Should not buy defense when industry fills all slots"

    assert state['total_slots'] == 5, f"Expected 5 slots, got {state['total_slots']}"
    assert state['total_position_pct'] > 0.99, f"Expected ~100% position, got {state['total_position_pct']}"

    # NAV勾稽
    assert state['nav_diff'] < 0.01, f"NAV diff too large: {state['nav_diff']}"
    total_commission = sum(o['commission'] for o in orders)
    nav_check = state['cash'] + state['total_positions_value'] + total_commission
    assert abs(nav_check - nav) < 0.01, f"NAV check failed: {nav_check} != {nav}"


# ========== 测试2: 防御让路（核心场景）==========
def test_defense_reduction_for_industry():
    """持有1只防御（市值50万），cash=50万，行业需要买入60万，防御应部分减持"""
    nav = 1_000_000.0
    cash = 500_000.0
    positions = {'GOLD': 25_000}
    prices = {'GOLD': 20.0, 'A': 100.0, 'B': 50.0, 'C': 30.0}
    industry = [('A', 90.0), ('B', 80.0), ('C', 70.0)]
    defense = [('GOLD', 65.0)]
    industry_tickers = {'A', 'B', 'C'}
    defense_tickers = {'GOLD'}

    assert abs(cash + 25000 * 20.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    industry_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] in industry_tickers]
    assert len(industry_buys) == 3, f"Expected 3 industry buys, got {len(industry_buys)}"

    defense_sells = [o for o in orders if o['action'] == 'SELL' and o['ticker'] == 'GOLD']
    assert len(defense_sells) == 1, f"Expected 1 defense sell, got {len(defense_sells)}"

    gold_remaining = state['positions'].get('GOLD', 0)
    assert gold_remaining > 0, "Defense should be partially reduced"
    assert gold_remaining < 25_000, "Defense should have been reduced"

    # 防御卖出金额 >= 行业买入所需
    sell_order = defense_sells[0]
    assert sell_order['shares'] > 0
    assert sell_order['shares'] % 100 == 0
    assert sell_order['shares'] >= 100
    sell_amount = sell_order['amount']
    assert sell_amount >= 100 * 20.0, "Sell amount should be at least 1 lot"

    assert state['total_slots'] <= 5
    assert state['total_slots'] >= 3
    assert state['nav_diff'] < 0.01


# ========== 测试3: 保留标的必须在候选中 ==========
def test_retained_must_be_in_candidates():
    """持有1只行业ETF，但不在当日候选，必须卖出"""
    nav = 1_000_000.0
    cash = 900_000.0
    positions = {'OLD': 10_000}
    prices = {'OLD': 10.0, 'A': 100.0, 'B': 50.0}
    industry = [('A', 90.0), ('B', 80.0)]
    defense = []
    industry_tickers = {'OLD', 'A', 'B'}
    defense_tickers = set()

    assert abs(cash + 10000 * 10.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    sell_orders = [o for o in orders if o['action'] == 'SELL' and o['ticker'] == 'OLD']
    assert len(sell_orders) == 1, f"Expected OLD to be sold, got {len(sell_orders)} sells"
    assert sell_orders[0]['shares'] == 10_000

    buy_orders = [o for o in orders if o['action'] == 'BUY']
    assert len(buy_orders) == 2, f"Expected 2 buys, got {len(buy_orders)}"

    assert 'OLD' not in state['positions'], "OLD should not be in final positions"
    assert state['nav_diff'] < 0.01


# ========== 测试4: 顺序独立（v2.4最小反例）==========
def test_order_independence_minimal_counterexample():
    """"
    v2.4 最小反例：槽位限制为1，传入不同顺序，结果必须一致。
    在 v2.3 中，顺序 [(A,80), (B,70)] → 买入A；顺序 [(B,70), (A,80)] → 买入B。
    v2.4 必须内部排序，两种顺序都买入A（评分更高）。
    """
    nav = 1_000_000.0
    cash = 1_000_000.0
    positions = {}
    prices = {'A': 100.0, 'B': 50.0}
    defense = []
    industry_tickers = {'A', 'B'}
    defense_tickers = set()

    # 顺序1: A评分高，排前面
    industry_1 = [('A', 80.0), ('B', 70.0)]
    # 顺序2: B评分低，排前面（但A评分更高）
    industry_2 = [('B', 70.0), ('A', 80.0)]

    # 槽位限制为1，只够买1只
    _, state_1 = plan_rebalance_v2_4(
        nav, cash, positions, industry_1, defense, prices,
        industry_tickers, defense_tickers,
        max_industry_holdings=1,
        max_position_per_etf=0.20,
    )

    _, state_2 = plan_rebalance_v2_4(
        nav, cash, positions, industry_2, defense, prices,
        industry_tickers, defense_tickers,
        max_industry_holdings=1,
        max_position_per_etf=0.20,
    )

    # 两种顺序都必须买入A（评分更高），不能买入B
    assert 'A' in state_1['positions'], "Order 1 should buy A (higher score)"
    assert 'B' not in state_1['positions'], "Order 1 should not buy B"
    assert 'A' in state_2['positions'], "Order 2 should buy A (higher score)"
    assert 'B' not in state_2['positions'], "Order 2 should not buy B"

    # 两只持仓股数相同
    assert state_1['positions']['A'] == state_2['positions']['A'], \
        f"A shares should be same: {state_1['positions']['A']} vs {state_2['positions']['A']}"

    assert state_1['nav_diff'] < 0.01
    assert state_2['nav_diff'] < 0.01


# ========== 测试5: 顺序独立（资金受限，逐只对比）==========
def test_order_independence_cash_constrained():
    """资金受限，无防御，5只候选按不同顺序遍历，每只ETF结果应相同"""
    nav = 200_000.0
    cash = 200_000.0
    positions = {}
    prices = {'A': 100.0, 'B': 50.0, 'C': 30.0, 'D': 20.0, 'E': 10.0}

    industry_1 = [('A', 90.0), ('B', 80.0), ('C', 70.0), ('D', 60.0), ('E', 50.0)]
    industry_2 = [('E', 50.0), ('D', 60.0), ('C', 70.0), ('B', 80.0), ('A', 90.0)]
    defense = []
    industry_tickers = {'A', 'B', 'C', 'D', 'E'}
    defense_tickers = set()

    assert abs(cash - nav) < 0.01

    _, state_1 = plan_rebalance_v2_4(
        nav, cash, positions, industry_1, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    _, state_2 = plan_rebalance_v2_4(
        nav, cash, positions, industry_2, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    # 逐只对比
    all_tickers = set(state_1['positions'].keys()) | set(state_2['positions'].keys())
    for t in all_tickers:
        s1 = state_1['positions'].get(t, 0)
        s2 = state_2['positions'].get(t, 0)
        assert s1 == s2, f"Order independence failed for {t}: {s1} vs {s2}"

    assert abs(state_1['total_positions_value'] - state_2['total_positions_value']) < 0.01
    assert state_1['total_slots'] == state_2['total_slots']
    assert state_1['nav_diff'] < 0.01
    assert state_2['nav_diff'] < 0.01


# ========== 测试6: 不可交易候选不导致防御往返（v2.4最小反例）==========
def test_untradable_candidate_no_defense_roundtrip():
    """"
    v2.4 最小反例：行业候选B缺价，不应导致防御D1被卖出后又买入。
    在 v2.3 中，先按候选数量计算槽位，会让防御D1让路；但B缺价无法买入，
    Step 7 又会重新买入D1，产生双边佣金。
    v2.4 必须先过滤不可交易候选，再决定是否让防御腾槽位。
    """
    nav = 1_000_000.0
    cash = 800_000.0
    positions = {'D1': 10_000}  # 防御持仓，市值200,000
    prices = {'D1': 20.0, 'A': 100.0}  # B 缺价
    industry = [('A', 80.0), ('B', 70.0)]  # B 缺价，不可交易
    defense = [('D1', 65.0)]
    industry_tickers = {'A', 'B'}
    defense_tickers = {'D1'}

    assert abs(cash + 10000 * 20.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    # 防御不应被卖出（因为B不可交易，不需要腾槽位）
    d1_sells = [o for o in orders if o['action'] == 'SELL' and o['ticker'] == 'D1']
    assert len(d1_sells) == 0, "D1 should not be sold (B is untradable, no slot needed)"

    # 防御不应被重新买入（已在持仓中）
    d1_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] == 'D1']
    assert len(d1_buys) == 0, "D1 should not be re-bought (already held)"

    # A 应被买入
    a_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] == 'A']
    assert len(a_buys) == 1, "A should be bought"

    # D1 应保留在持仓中
    assert 'D1' in state['positions'], "D1 should remain in positions"
    assert state['positions']['D1'] == 10_000, "D1 shares should not change"

    assert state['nav_diff'] < 0.01


# ========== 测试7: 缺价持仓使用last_prices估值（v2.4最小反例）==========
def test_missing_price_with_last_prices():
    """"
    v2.4 最小反例：缺价持仓MISSING应使用last_prices估值，不应强制归零。
    NAV = cash + A市值 + MISSING市值（使用last_prices）
    """
    nav = 1_050_000.0
    cash = 800_000.0
    positions = {'A': 2_000, 'MISSING': 5_000}
    prices = {'A': 100.0, 'B': 50.0}  # MISSING 不在 prices 中
    last_prices = {'MISSING': 10.0}  # 最近有效价格
    industry = [('A', 90.0), ('B', 80.0)]
    defense = []
    industry_tickers = {'A', 'B', 'MISSING'}
    defense_tickers = set()

    # NAV = 800,000 + 2,000*100 + 5,000*10 = 1,050,000
    assert abs(cash + 2000 * 100.0 + 5000 * 10.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        last_prices=last_prices,
        max_position_per_etf=0.20,
    )

    # MISSING 不在候选列表中，但无当日价格，不应被卖出
    missing_sell = [o for o in orders if o['ticker'] == 'MISSING']
    assert len(missing_sell) == 0, "MISSING should not be sold (no execution price)"

    # MISSING 应保留在持仓中
    assert 'MISSING' in state['positions'], "MISSING should remain in positions"
    assert state['positions']['MISSING'] == 5_000, "MISSING shares should not change"

    # NAV勾稽：最终估值应包含MISSING的last_prices估值
    # 状态中的 total_positions_value 应包含 MISSING 的市值
    missing_value = 5000 * 10.0
    a_value = state['positions'].get('A', 0) * 100.0
    b_value = state['positions'].get('B', 0) * 50.0
    expected_total = a_value + b_value + missing_value
    assert abs(state['total_positions_value'] - expected_total) < 0.01, \
        f"Total positions value should include MISSING: {state['total_positions_value']} != {expected_total}"

    assert state['nav_diff'] < 0.01


# ========== 测试8: 卖出标的不可交易（缺价）==========
def test_sell_ticker_not_tradable():
    """需要卖出的标的缺少执行价时不能卖出，但必须用last_prices继续估值。"""
    nav = 1_000_000.0
    cash = 750_000.0
    positions = {'A': 2_000, 'OLD': 5_000}
    prices = {'A': 100.0, 'B': 50.0}  # OLD 不在 prices 中
    last_prices = {'OLD': 10.0}  # OLD可估值但不可交易
    industry = [('A', 90.0), ('B', 80.0)]
    defense = []
    industry_tickers = {'A', 'B', 'OLD'}
    defense_tickers = set()

    assert abs(cash + 2000 * 100.0 + 5000 * 10.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        last_prices=last_prices,
        max_position_per_etf=0.20,
    )

    old_sell = [o for o in orders if o['ticker'] == 'OLD']
    assert len(old_sell) == 0, "OLD should not be sold (no price available)"
    assert 'OLD' in state['positions'], "OLD should remain in positions"
    assert state['nav_diff'] < 0.01


# ========== 测试9: 保留低于20%不补仓 ==========
def test_retained_below_20pct_no_rebalance():
    """持有1只行业ETF，市值仅占10%（价格跌了），在候选中，不应补仓"""
    nav = 1_000_000.0
    cash = 900_000.0
    positions = {'RETAIN': 10_000}
    prices = {'RETAIN': 10.0, 'A': 100.0, 'B': 50.0}
    industry = [('RETAIN', 90.0), ('A', 80.0), ('B', 70.0)]
    defense = []
    industry_tickers = {'RETAIN', 'A', 'B'}
    defense_tickers = set()

    assert abs(cash + 10000 * 10.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    retain_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] == 'RETAIN']
    assert len(retain_buys) == 0, "RETAIN should not be rebalanced"
    assert 'RETAIN' in state['positions']
    assert state['positions']['RETAIN'] == 10_000
    assert state['nav_diff'] < 0.01


# ========== 测试10: 槽位规则 - 防御让路 ==========
def test_defense_temp_slots_yield_to_industry():
    """持有2只防御，0只行业，5只行业候选，防御应让路，行业可以买入"""
    nav = 1_000_000.0
    cash = 800_000.0
    positions = {'D1': 10_000, 'D2': 5_000}
    prices = {'D1': 10.0, 'D2': 20.0, 'A': 100.0, 'B': 50.0, 'C': 30.0, 'D': 20.0, 'E': 10.0}
    industry = [('A', 90.0), ('B', 80.0), ('C', 70.0), ('D', 60.0), ('E', 50.0)]
    defense = [('D1', 65.0), ('D2', 60.0)]
    industry_tickers = {'A', 'B', 'C', 'D', 'E'}
    defense_tickers = {'D1', 'D2'}

    assert abs(cash + 10000 * 10.0 + 5000 * 20.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    industry_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] in industry_tickers]
    assert len(industry_buys) == 5, f"Expected 5 industry buys, got {len(industry_buys)}"

    defense_sells = [o for o in orders if o['action'] == 'SELL' and o['ticker'] in defense_tickers]
    assert len(defense_sells) >= 1, "Defense should be sold to make room for industry"

    assert state['total_slots'] == 5, f"Expected 5 slots, got {state['total_slots']}"
    assert state['nav_diff'] < 0.01


# ========== 测试11: NAV恒等式校验（有估值持仓）==========
def test_nav_identity_check():
    """输入不满足NAV恒等式（有估值持仓）时应抛出ValueError"""
    nav = 1_000_000.0
    cash = 900_000.0
    positions = {'A': 2_000}
    prices = {'A': 100.0}
    industry = [('A', 90.0)]
    defense = []
    industry_tickers = {'A'}
    defense_tickers = set()

    with pytest.raises(ValueError, match="NAV恒等式不成立"):
        plan_rebalance_v2_4(
            nav, cash, positions, industry, defense, prices,
            industry_tickers, defense_tickers,
        )


# ========== 测试12: max_total_position 约束（核心P0）==========
def test_max_total_position_constraint():
    """行业已占75%，总仓位上限80%，防御不应突破总仓位"""
    nav = 1_000_000.0
    cash = 250_000.0
    positions = {'A': 5_000}  # 75万
    prices = {'A': 150.0, 'GOLD': 200.0, 'BOND': 100.0}
    industry = [('A', 90.0)]
    defense = [('GOLD', 65.0), ('BOND', 60.0)]
    industry_tickers = {'A', 'B'}
    defense_tickers = {'GOLD', 'BOND'}

    assert abs(cash + 5000 * 150.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
        max_total_position=0.80,
    )

    assert state['total_position_pct'] <= 0.80 + 0.001, \
        f"Total position should not exceed 80%: {state['total_position_pct']:.4%}"
    assert state['industry_position_pct'] >= 0.74
    assert state['defense_position_pct'] <= 0.05 + 0.001
    assert state['nav_diff'] < 0.01


# ========== 测试13: 防御资产不占用行业槽位 ==========
def test_defense_does_not_use_industry_slots():
    """持有2只防御，行业上限5只，总上限5只，防御应在让路后不占槽位"""
    nav = 1_000_000.0
    cash = 800_000.0
    positions = {'D1': 10_000, 'D2': 5_000}
    prices = {'D1': 10.0, 'D2': 20.0, 'A': 100.0, 'B': 50.0, 'C': 30.0, 'D': 20.0, 'E': 10.0}
    industry = [('A', 90.0), ('B', 80.0), ('C', 70.0), ('D', 60.0), ('E', 50.0)]
    defense = [('D1', 65.0), ('D2', 60.0)]
    industry_tickers = {'A', 'B', 'C', 'D', 'E'}
    defense_tickers = {'D1', 'D2'}

    assert abs(cash + 10000 * 10.0 + 5000 * 20.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    industry_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] in industry_tickers]
    assert len(industry_buys) >= 3, f"Expected at least 3 industry buys, got {len(industry_buys)}"
    assert state['nav_diff'] < 0.01


# ========== 测试14: 已持仓标的在候选列表中，不应重复买入 ==========
def test_retained_not_rebought():
    """持有1只行业ETF在候选列表中，不应再次买入"""
    nav = 1_000_000.0
    cash = 800_000.0
    positions = {'A': 2_000}
    prices = {'A': 100.0, 'B': 50.0}
    industry = [('A', 90.0), ('B', 80.0)]
    defense = []
    industry_tickers = {'A', 'B'}
    defense_tickers = set()

    assert abs(cash + 2000 * 100.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    a_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] == 'A']
    assert len(a_buys) == 0, "A should not be re-bought"
    b_buys = [o for o in orders if o['action'] == 'BUY' and o['ticker'] == 'B']
    assert len(b_buys) == 1, "B should be bought"
    assert state['positions']['A'] == 2_000
    assert state['nav_diff'] < 0.01


# ========== 测试15: 防御卖出向上取整（P1）==========
def test_defense_sell_rounds_up():
    """防御部分卖出应向上取整到整手，确保筹措足够资金"""
    nav = 1_200_000.0
    cash = 200_000.0
    positions = {'GOLD': 50_000}  # 1,000,000
    prices = {'GOLD': 20.0, 'A': 100.0, 'B': 50.0, 'C': 30.0}
    industry = [('A', 90.0), ('B', 80.0), ('C', 70.0)]
    defense = [('GOLD', 65.0)]
    industry_tickers = {'A', 'B', 'C'}
    defense_tickers = {'GOLD'}

    assert abs(cash + 50000 * 20.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    gold_sells = [o for o in orders if o['action'] == 'SELL' and o['ticker'] == 'GOLD']
    assert len(gold_sells) >= 1, "GOLD should be partially sold"

    sell_order = gold_sells[0]
    assert sell_order['shares'] % 100 == 0, "Sell shares must be multiple of 100"
    # 向上取整：卖出金额应 >= 所需资金
    sell_amount = sell_order['amount']
    net_proceeds = sell_amount - sell_order['commission']
    needed_approx = 600000 - 200000  # 约40万
    assert sell_amount >= needed_approx, \
        f"Sell amount {sell_amount} should be >= needed {needed_approx}"

    assert state['nav_diff'] < 0.01


# ========== 测试16: 佣金精确计算 ==========
def test_commission_exact():
    """验证每只订单的佣金计算精确"""
    nav = 1_000_000.0
    cash = 1_000_000.0
    positions = {}
    prices = {'A': 100.0, 'B': 50.0}
    industry = [('A', 90.0), ('B', 80.0)]
    defense = []
    industry_tickers = {'A', 'B'}
    defense_tickers = set()

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    for o in orders:
        expected_comm = _calc_commission(o['amount'], 0.0003, 5.0)
        assert abs(o['commission'] - expected_comm) < 0.01, \
            f"Commission mismatch for {o['ticker']}: {o['commission']} != {expected_comm}"
        assert o['commission'] >= 5.0 - 0.01, \
            f"Commission below minimum for {o['ticker']}: {o['commission']}"

    assert state['nav_diff'] < 0.01


# ========== 测试17: 2026-03-12真实快照（开盘价）==========
def test_real_snapshot_2026_03_12_open():
    """使用从真实回测导出的2026-03-12快照，开盘价作为成交价"""
    cash = 997_561.292
    positions = {
        '518880.SH': 55_522,
        '159697.SZ': 216_956,
    }
    prices = {
        '518880.SH': 10.928,
        '159697.SZ': 1.562,
        '159865.SZ': 0.658,
        '516160.SH': 3.236,
        '515880.SH': 1.095,
        '512800.SH': 0.785,
    }
    positions_value_open = 55522 * 10.928 + 216956 * 1.562
    nav = cash + positions_value_open
    industry = [
        ('159697.SZ', 90.0), ('159865.SZ', 78.875),
        ('516160.SH', 76.4375), ('515880.SH', 57.75)
    ]
    defense = []
    industry_tickers = {
        '512480.SH', '515230.SH', '515880.SH', '512010.SH', '159928.SZ',
        '516160.SH', '516110.SH', '512800.SH', '512000.SH', '512660.SH',
        '512980.SH', '512400.SH', '159996.SZ', '159865.SZ', '159697.SZ', '159530.SZ',
    }
    defense_tickers = {'518880.SH', '511010.SH'}

    assert abs(cash + positions_value_open - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.15,
    )

    gold_sell = [o for o in orders if o['action'] == 'SELL' and o['ticker'] == '518880.SH']
    assert len(gold_sell) == 1, "518880.SH should be sold"
    assert gold_sell[0]['shares'] == 55_522

    assert '159697.SZ' in state['positions'], "159697.SZ should be retained"
    assert state['total_slots'] <= 5
    assert state['nav_diff'] < 0.01
    assert state['total_position_pct'] <= 1.0 + 0.001

    total_commission = sum(o['commission'] for o in orders)
    nav_check = state['cash'] + state['total_positions_value'] + total_commission
    assert abs(nav_check - nav) < 0.01


# ========== 测试18: 有限现金时同比例缩放，不淘汰（v2.5 核心修正）==========
def test_cash_shortage_equal_scale_all_retained():
    """
    v2.5 核心修正：资金不足时，应对所有已入选候选统一缩放，
    只有缩放后不足一手时才淘汰。不可先淘汰再缩放。
    
    场景：NAV=100万，保留R=60万，现金=40万
    A/B/C 价格=333，各目标=20万
    3只原始各600股=199,800，总成本=599,579 > 40万
    缩放后各400股=133,200，3只总成本=399,719 < 40万
    预期：A/B/C 全部保留，每只约400股，同比例分配
    """
    nav = 1_000_000.0
    cash = 400_000.0
    positions = {'R': 30_000}  # 保留持仓，30,000股 @ 20 = 600,000
    prices = {'R': 20.0, 'A': 333.0, 'B': 333.0, 'C': 333.0}
    industry = [('R', 95.0), ('A', 90.0), ('B', 80.0), ('C', 70.0)]
    defense = []
    industry_tickers = {'R', 'A', 'B', 'C'}
    defense_tickers = set()

    # NAV = 400,000 + 30,000 * 20 = 1,000,000
    assert abs(cash + 30_000 * 20.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    # R 被保留（评分最高，在候选中）
    assert 'R' in state['positions'], "R (score=95) should be retained"
    # A/B/C 全部应被买入（同比例缩放，不淘汰）
    assert 'A' in state['positions'], "A (score=90) should be bought (scaled, not eliminated)"
    assert 'B' in state['positions'], "B (score=80) should be bought (scaled, not eliminated)"
    assert 'C' in state['positions'], "C (score=70) should be bought (scaled, not eliminated)"

    # 每只约400股（133,200 / 333 = 400股）
    assert state['positions']['A'] == 400, f"A should have ~400 shares, got {state['positions'].get('A')}"
    assert state['positions']['B'] == 400, f"B should have ~400 shares, got {state['positions'].get('B')}"
    assert state['positions']['C'] == 400, f"C should have ~400 shares, got {state['positions'].get('C')}"

    # 最终持仓：R保留 + A + B + C = 4只
    assert state['total_slots'] == 4, f"Expected 4 slots, got {state['total_slots']}"

    # NAV 恒等式
    total_commission = sum(o['commission'] for o in orders)
    nav_check = state['cash'] + state['total_positions_value'] + total_commission
    assert abs(nav_check - nav) < 0.01, f"NAV identity failed: {nav_check} != {nav}"
    assert state['nav_diff'] < 0.01


# ========== 测试19: 缩放后不足一手才淘汰（v2.5 淘汰条件）==========
def test_elimination_only_when_below_lot_size():
    """
    v2.5：只有缩放后不足一手时，才按评分淘汰。
    场景：price=1500，100股=15万，现金=20万，3只候选各目标=20万。
    缩放后每只66,600 < 15万，不足一手，淘汰最低分C；
    A/B 缩放后仍不足一手，淘汰B；A 100股=15万 < 20万，保留A。
    """
    nav = 1_000_000.0
    cash = 200_005.0
    positions = {'R': 53_333}  # 保留持仓，53,333股 @ 15 = 799,995
    prices = {'R': 15.0, 'A': 1500.0, 'B': 1500.0, 'C': 1500.0}
    industry = [('R', 95.0), ('A', 90.0), ('B', 80.0), ('C', 70.0)]
    defense = []
    industry_tickers = {'R', 'A', 'B', 'C'}
    defense_tickers = set()

    # NAV = 200,005 + 53,333 * 15 = 1,000,000

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_position_per_etf=0.20,
    )

    # R 被保留
    assert 'R' in state['positions'], "R (score=95) should be retained"
    # A 应被买入（唯一缩放后≥1手的）
    assert 'A' in state['positions'], "A (score=90) should be bought"
    # B 和 C 应被淘汰（缩放后不足一手）
    assert 'B' not in state['positions'], "B (score=80) should be eliminated (below lot size after scale)"
    assert 'C' not in state['positions'], "C (score=70) should be eliminated (below lot size after scale)"

    # A 应为100股（150,000）
    assert state['positions']['A'] == 100, f"A should have 100 shares, got {state['positions'].get('A')}"

    # NAV 恒等式
    total_commission = sum(o['commission'] for o in orders)
    nav_check = state['cash'] + state['total_positions_value'] + total_commission
    assert abs(nav_check - nav) < 1.0, f"NAV identity failed: {nav_check} != {nav}"
    assert state['nav_diff'] < 1.0


# ========== 测试20: 无估值持仓时函数报错（v2.5 契约测试）==========
def test_unpriced_position_raises_error():
    """
    v2.5 契约：当持仓既无当日价格也无 last_prices 时，NAV 恒等式检查应失败。
    函数必须报错，不能静默忽略。
    """
    nav = 1_000_000.0
    cash = 400_000.0
    positions = {'R': 30_000}  # 30,000股，但无价格
    prices = {'A': 333.0, 'B': 333.0, 'C': 333.0}  # R 不在 prices 中
    industry = [('R', 95.0), ('A', 90.0), ('B', 80.0), ('C', 70.0)]
    defense = []
    industry_tickers = {'R', 'A', 'B', 'C'}
    defense_tickers = set()

    # 不传入 last_prices，R 无法估值
    # valued_positions = 0, cash = 400,000, nav = 1,000,000
    # 恒等式不成立，应抛出 ValueError
    with pytest.raises(ValueError):
        plan_rebalance_v2_4(
            nav, cash, positions, industry, defense, prices,
            industry_tickers, defense_tickers,
            max_position_per_etf=0.20,
        )


def test_retained_industry_consumes_total_position_budget():
    """保留行业已占75%、总仓位上限80%时，新行业最多只能使用剩余5%预算。"""
    nav = 1_000_000.0
    cash = 250_000.0
    positions = {'R': 7_500}  # 7,500 * 100 = 750,000
    prices = {'R': 100.0, 'A': 100.0}
    industry = [('R', 100.0), ('A', 90.0)]

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions, industry, [], prices,
        {'R', 'A'}, set(),
        max_position_per_etf=0.20,
        max_total_position=0.80,
    )

    a_buy = next((o for o in orders if o['action'] == 'BUY' and o['ticker'] == 'A'), None)
    assert a_buy is not None
    assert a_buy['amount'] <= 50_000.0
    assert state['total_position_pct'] <= 0.80 + 1e-9
    assert state['nav_diff'] < 0.01


def test_equal_scores_use_ticker_tiebreaker():
    """评分相同时使用ticker作为稳定次级排序，不能依赖传入顺序。"""
    common = dict(
        nav=1_000_000.0,
        cash=1_000_000.0,
        current_positions={},
        defense_candidates=[],
        prices={'A': 100.0, 'B': 100.0},
        industry_tickers={'A', 'B'},
        defense_tickers=set(),
        max_industry_holdings=1,
        max_position_per_etf=0.20,
    )

    _, state_ab = plan_rebalance_v2_4(
        industry_candidates=[('A', 80.0), ('B', 80.0)],
        **common,
    )
    _, state_ba = plan_rebalance_v2_4(
        industry_candidates=[('B', 80.0), ('A', 80.0)],
        **common,
    )

    assert state_ab['positions'] == state_ba['positions']
    assert set(state_ab['positions']) == {'A'}


def test_defense_yields_to_industry_for_total_position_budget():
    """现金足够但总仓位预算不足时，防御仍应减持，为新行业释放风险预算。"""
    nav = 1_000_000.0
    cash = 300_000.0
    positions = {
        'R': 4_000,     # 400,000
        'GOLD': 3_000,  # 300,000
    }
    prices = {'R': 100.0, 'A': 100.0, 'GOLD': 100.0}

    orders, state = plan_rebalance_v2_4(
        nav, cash, positions,
        [('R', 100.0), ('A', 90.0)],
        [('GOLD', 60.0)],
        prices,
        {'R', 'A'},
        {'GOLD'},
        max_position_per_etf=0.20,
        max_total_position=0.80,
    )

    gold_sell = next((o for o in orders if o['action'] == 'SELL' and o['ticker'] == 'GOLD'), None)
    a_buy = next((o for o in orders if o['action'] == 'BUY' and o['ticker'] == 'A'), None)
    assert gold_sell is not None
    assert a_buy is not None
    assert state['total_position_pct'] <= 0.80 + 1e-9
    assert state['nav_diff'] < 0.01


def test_unpriced_position_raises_even_if_nav_matches_known_assets():
    """任何现有持仓缺少估值价都必须报错，不能靠NAV碰巧相等绕过校验。"""
    with pytest.raises(ValueError, match="缺少估值价格"):
        plan_rebalance_v2_4(
            nav=400_000.0,
            cash=400_000.0,
            current_positions={'MISSING': 30_000},
            industry_candidates=[],
            defense_candidates=[],
            prices={},
            industry_tickers={'MISSING'},
            defense_tickers=set(),
        )


def test_v2_5_is_canonical_entrypoint():
    """Phase 2应使用v2.5入口；v2.4名称仅保留向后兼容。"""
    assert plan_rebalance_v2_5 is plan_rebalance_v2_4
