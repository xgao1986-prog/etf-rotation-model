"""
B0 调仓资金规划纯函数 v2.5

设计目标：
1. 函数内部重新排序，消除传入顺序依赖
2. 先过滤不可交易候选，再让防御腾槽位（避免无效往返）
3. 分离 valuation_prices（用于NAV/估值）和 execution_prices（用于交易）
4. 防御卖出按净到账约束求解，使用 math.ceil 真正向上取整到整手
5. 防御填充受 max_total_position 约束
6. 保留低于20%不补仓：既有行为，不主动调整保留持仓

约束：
- industry_max_holdings=5
- defense_max_holdings=2
- total_max_holdings=5
- max_position_per_etf=0.15（默认）或0.20
- max_total_position=1.0（默认）或根据大盘择时
- lot_size=100
- commission_rate=0.0003
- min_commission=5.0
"""

import math
from typing import Dict, List, Tuple, Set, Optional


def _calc_commission(amount: float, rate: float, min_comm: float) -> float:
    """计算佣金"""
    return max(amount * rate, min_comm)


def _calc_buy_shares(target_amount: float, price: float, lot_size: int = 100) -> int:
    """计算买入股数，向下取整到lot_size"""
    if target_amount <= 0 or price <= 0:
        return 0
    raw = int(target_amount / price)
    return (raw // lot_size) * lot_size


def _calc_sell_shares(target_amount: float, price: float, lot_size: int = 100) -> int:
    """计算卖出股数，向上取整到lot_size，确保卖出金额 >= target_amount"""
    if target_amount <= 0 or price <= 0:
        return 0
    # 真正向上取整：先计算所需股数，再向上取整到整手
    raw_shares = math.ceil(target_amount / price)
    return math.ceil(raw_shares / lot_size) * lot_size


def _get_valuation_price(ticker: str, prices: Dict[str, float], last_prices: Optional[Dict[str, float]]) -> Optional[float]:
    """获取估值价格：优先当日价格，其次最近有效价格"""
    if ticker in prices and prices[ticker] > 0:
        return prices[ticker]
    if last_prices and ticker in last_prices and last_prices[ticker] > 0:
        return last_prices[ticker]
    return None


def plan_rebalance_v2_5(
    nav: float,
    cash: float,
    current_positions: Dict[str, int],  # ticker -> shares
    industry_candidates: List[Tuple[str, float]],  # [(ticker, score), ...] 任意顺序
    defense_candidates: List[Tuple[str, float]],  # [(ticker, score), ...] 任意顺序
    prices: Dict[str, float],  # ticker -> 当日市场价（用于估值和可交易性判断）
    industry_tickers: Set[str],
    defense_tickers: Set[str],
    last_prices: Optional[Dict[str, float]] = None,  # 最近有效价格（用于缺价估值）
    max_industry_holdings: int = 5,
    max_defense_holdings: int = 2,
    max_total_holdings: int = 5,
    max_position_per_etf: float = 0.15,
    max_total_position: float = 1.0,
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    lot_size: int = 100,
    sell_prices: Optional[Dict[str, float]] = None,  # 卖出价（滑点后的），默认=prices
    buy_prices: Optional[Dict[str, float]] = None,   # 买入价（滑点后的），默认=prices
) -> Tuple[List[Dict], Dict]:
    """
    v2.5 纯函数：顺序独立，防御让路，总仓位受控，缺价不强制归零

    核心逻辑：
    1. 内部按评分重新排序，消除传入顺序依赖
    2. 先过滤不可交易候选（缺价/零价），避免防御无效往返
    3. NAV校验：有当日价格用当日，无当日用last_prices，两者皆无不纳入
    4. 统一计算行业目标，资金不足时统一缩放（确保所有订单可执行）
    5. 防御卖出：按净到账约束，向上取整到整手
    6. 防御填充：先检查剩余风险预算，不突破max_total_position
    """

    # ========== Step 0: 初始校验 ==========
    # 按评分重新排序，确保内部顺序一致（消除传入顺序依赖）
    industry_candidates = sorted(industry_candidates, key=lambda x: (-x[1], x[0]))
    defense_candidates = sorted(defense_candidates, key=lambda x: (-x[1], x[0]))

    # 默认执行价格=估值价格（向后兼容，无滑点时行为不变）
    sell_prices = sell_prices if sell_prices is not None else prices
    buy_prices = buy_prices if buy_prices is not None else prices

    industry_score_map = {t: s for t, s in industry_candidates}
    defense_score_map = {t: s for t, s in defense_candidates}

    # 计算"有估值价格"的持仓市值（缺价不强制归零，用last_prices或忽略）
    valued_positions = 0.0
    unpriced_positions = []
    for t, shares in current_positions.items():
        vp = _get_valuation_price(t, prices, last_prices)
        if vp is None:
            unpriced_positions.append(t)
        else:
            valued_positions += shares * vp

    if unpriced_positions:
        raise ValueError(
            f"持仓缺少估值价格: {sorted(unpriced_positions)}。"
            "请提供当日价格或 last_prices。"
        )

    if abs(valued_positions + cash - nav) >= 0.01:
        raise ValueError(
            f"NAV恒等式不成立（有估值持仓）: cash={cash} + valued_positions={valued_positions} "
            f"= {valued_positions + cash} != nav={nav}, diff={abs(valued_positions + cash - nav)}"
        )

    orders = []
    working_cash = cash
    working_positions = dict(current_positions)

    # ========== Step 1: 过滤不可交易候选 ==========
    # 可交易 = prices中有且价格 > 0
    tradable_industry = [
        (t, s) for t, s in industry_candidates
        if t in prices and prices[t] > 0
    ]
    tradable_defense = [
        (t, s) for t, s in defense_candidates
        if t in prices and prices[t] > 0
    ]

    tradable_industry_tickers = [t for t, _ in tradable_industry]
    tradable_defense_tickers = [t for t, _ in tradable_defense]

    # ========== Step 2: 分类持仓和候选 ==========
    # 保留的行业持仓：在"可交易"候选列表中
    retained_industry = [
        t for t in working_positions
        if t in industry_tickers and t in tradable_industry_tickers
    ]
    # 保留的防御持仓
    retained_defense = [
        t for t in working_positions
        if t in defense_tickers and t in tradable_defense_tickers
    ]

    # 需要卖出的持仓：不在"可交易"候选列表中的
    sell_tickers = []
    for t in list(working_positions.keys()):
        if t in industry_tickers and t not in tradable_industry_tickers:
            sell_tickers.append(t)
        elif t in defense_tickers and t not in tradable_defense_tickers:
            sell_tickers.append(t)

    # ========== Step 3: 执行卖出（不在候选的持仓） ==========
    for t in sell_tickers:
        if t not in working_positions:
            continue
        shares = working_positions[t]
        price = sell_prices.get(t, 0)
        if price is None or price <= 0 or shares <= 0:
            continue  # 缺价无法卖出
        amount = shares * price
        commission = _calc_commission(amount, commission_rate, min_commission)
        net_proceeds = amount - commission
        working_cash += net_proceeds
        del working_positions[t]
        orders.append({
            'action': 'SELL',
            'ticker': t,
            'shares': shares,
            'price': price,
            'amount': amount,
            'commission': commission,
            'reason': '调出候选列表',
        })

    # ========== Step 4: 确定行业目标组合（基于可交易候选） ==========
    # 新候选 = 不在当前持仓中的可交易行业候选
    new_industry_candidates = [
        t for t in tradable_industry_tickers if t not in working_positions
    ]

    # 行业槽位
    raw_industry_slots = min(
        len(new_industry_candidates),
        max_industry_holdings - len(retained_industry)
    )
    industry_slots = min(raw_industry_slots, max_total_holdings - len(working_positions))

    # 如果槽位不足，防御让路（腾槽位）
    if industry_slots < raw_industry_slots and len(working_positions) > 0:
        slots_needed = raw_industry_slots - industry_slots
        # 当前防御持仓（按评分从低到高排序，低分先卖）
        current_defense = [
            (t, working_positions[t], defense_score_map.get(t, 0))
            for t in list(working_positions.keys()) if t in defense_tickers
        ]
        current_defense.sort(key=lambda x: x[2])  # 按评分升序

        for t, shares, _ in current_defense:
            if slots_needed <= 0:
                break
            price = sell_prices.get(t, 0)
            if price is None or price <= 0 or shares <= 0:
                continue
            amount = shares * price
            commission = _calc_commission(amount, commission_rate, min_commission)
            net_proceeds = amount - commission
            working_cash += net_proceeds
            del working_positions[t]
            slots_needed -= 1
            orders.append({
                'action': 'SELL',
                'ticker': t,
                'shares': shares,
                'price': price,
                'amount': amount,
                'commission': commission,
                'reason': '防御让路（腾槽位）',
            })

        industry_slots = min(raw_industry_slots, max_total_holdings - len(working_positions))

    buy_industry = new_industry_candidates[:industry_slots]
    n_industry_target = len(retained_industry) + len(buy_industry)

    # ========== Step 5: 计算行业目标金额（统一分配，避免顺序依赖） ==========
    if n_industry_target > 0:
        per_etf_pct = min(max_position_per_etf, 1.0 / n_industry_target)
        per_etf_target = nav * per_etf_pct * max_total_position
    else:
        per_etf_target = 0.0

    # 先计算所有订单的目标股数（未缩放）
    industry_buy_orders = []
    for t in buy_industry:
        price = buy_prices.get(t, 0)
        if price is None or price <= 0:
            continue
        shares = _calc_buy_shares(per_etf_target, price, lot_size)
        if shares > 0:
            amount = shares * price
            commission = _calc_commission(amount, commission_rate, min_commission)
            industry_buy_orders.append({
                'ticker': t,
                'shares': shares,
                'price': price,
                'amount': amount,              # 取整后的实际金额（会被缩放覆盖）
                'original_amount': amount,      # 保留原始目标金额，用于循环中重新计算scale
                'commission': commission,
                'score': industry_score_map.get(t, 0),  # 记录评分，用于排序
            })

    # ========== Step 6: 检查现金与总仓位预算，并处理防御让路 ==========
    total_industry_cost = sum(o['amount'] + o['commission'] for o in industry_buy_orders)
    total_industry_amount = sum(o['amount'] for o in industry_buy_orders)

    def _current_position_value() -> float:
        total = 0.0
        for ticker, shares in working_positions.items():
            valuation_price = _get_valuation_price(ticker, prices, last_prices)
            if valuation_price is None:
                raise ValueError(f"持仓缺少估值价格: {ticker}")
            total += shares * valuation_price
        return total

    max_position_value = nav * max_total_position
    current_position_value = _current_position_value()
    cash_shortfall = max(0.0, total_industry_cost - working_cash)
    position_excess = max(
        0.0,
        current_position_value + total_industry_amount - max_position_value,
    )

    if (cash_shortfall > 0 or position_excess > 0) and industry_buy_orders:

        # 按评分从低到高排序当前防御持仓
        current_defense = [
            (t, working_positions[t], defense_score_map.get(t, 0))
            for t in list(working_positions.keys()) if t in defense_tickers
        ]
        current_defense.sort(key=lambda x: (x[2], x[0]))

        for t, shares, _ in current_defense:
            if cash_shortfall <= 0 and position_excess <= 0:
                break
            price = sell_prices.get(t, 0)
            if price is None or price <= 0 or shares <= 0:
                continue

            # 同时满足两种需求：
            # 1) 净到账覆盖现金缺口；2) 卖出市值释放总仓位预算。
            gross_for_cash = 0.0
            if cash_shortfall > 0:
                gross_for_cash = cash_shortfall / (1 - commission_rate)
                if gross_for_cash * commission_rate < min_commission:
                    gross_for_cash = cash_shortfall + min_commission
            estimated_amount = max(gross_for_cash, position_excess)

            # 向上取整到整手
            sell_shares = _calc_sell_shares(estimated_amount, price, lot_size)
            sell_shares = min(sell_shares, shares)

            if sell_shares > 0:
                amount = sell_shares * price
                commission = _calc_commission(amount, commission_rate, min_commission)
                net_proceeds = amount - commission
                working_cash += net_proceeds
                cash_shortfall = max(0.0, cash_shortfall - net_proceeds)
                position_excess = max(0.0, position_excess - amount)
                working_positions[t] -= sell_shares
                if working_positions[t] <= 0:
                    del working_positions[t]
                orders.append({
                    'action': 'SELL',
                    'ticker': t,
                    'shares': sell_shares,
                    'price': price,
                    'amount': amount,
                    'commission': commission,
                    'reason': '防御减持让路',
                })

        # 减持后如果现金或总仓位预算仍不足，先统一缩放全部候选；
        # 只有缩放后某候选不足一手，或总成本仍超现金（取整效应）时，才淘汰评分最低者
        current_position_value = _current_position_value()
        allowed_industry_amount = max(0.0, max_position_value - current_position_value)
        total_industry_cost = sum(o['amount'] + o['commission'] for o in industry_buy_orders)
        total_industry_amount = sum(o['amount'] for o in industry_buy_orders)

        if (
            total_industry_cost > working_cash
            or total_industry_amount > allowed_industry_amount
        ):
            while industry_buy_orders:
                original_total = sum(o['original_amount'] for o in industry_buy_orders)
                max_scale_by_position = (
                    allowed_industry_amount / original_total
                    if original_total > 0 else 0.0
                )
                high = max(0.0, min(1.0, max_scale_by_position))
                low = 0.0

                # 订单成本随scale单调不减；二分寻找满足现金和仓位预算的最大共同scale。
                for _ in range(60):
                    mid = (low + high) / 2
                    scaled_amount = 0.0
                    scaled_cost = 0.0
                    for o in industry_buy_orders:
                        shares = _calc_buy_shares(
                            o['original_amount'] * mid,
                            o['price'],
                            lot_size,
                        )
                        amount = shares * o['price']
                        commission = (
                            _calc_commission(amount, commission_rate, min_commission)
                            if shares > 0 else 0.0
                        )
                        scaled_amount += amount
                        scaled_cost += amount + commission
                    if (
                        scaled_cost <= working_cash
                        and scaled_amount <= allowed_industry_amount
                    ):
                        low = mid
                    else:
                        high = mid

                for o in industry_buy_orders:
                    o['shares'] = _calc_buy_shares(
                        o['original_amount'] * low,
                        o['price'],
                        lot_size,
                    )
                    o['amount'] = o['shares'] * o['price']
                    o['commission'] = (
                        _calc_commission(o['amount'], commission_rate, min_commission)
                        if o['shares'] > 0 else 0.0
                    )

                if all(o['shares'] >= lot_size for o in industry_buy_orders):
                    break

                # 缩放后不足一手：淘汰最低评分；同分时保留ticker较小者。
                lowest_score = min(o['score'] for o in industry_buy_orders)
                remove_index = max(
                    (
                        idx for idx, o in enumerate(industry_buy_orders)
                        if o['score'] == lowest_score
                    ),
                    key=lambda idx: industry_buy_orders[idx]['ticker'],
                )
                industry_buy_orders.pop(remove_index)

            industry_buy_orders = [o for o in industry_buy_orders if o['shares'] > 0]

    # ========== Step 7: 统一执行行业买入（不逐只跳过） ==========
    total_cost = sum(o['amount'] + o['commission'] for o in industry_buy_orders)
    if total_cost <= working_cash:
        for o in industry_buy_orders:
            working_cash -= (o['amount'] + o['commission'])
            ticker = o['ticker']
            working_positions[ticker] = working_positions.get(ticker, 0) + o['shares']
            orders.append({
                'action': 'BUY',
                'ticker': ticker,
                'shares': o['shares'],
                'price': o['price'],
                'amount': o['amount'],
                'commission': o['commission'],
                'reason': '行业买入',
            })

    # ========== Step 8: 防御资产填充（受max_total_position约束） ==========
    # 计算当前总仓位（使用估值价格）
    current_total_value = 0.0
    for t, shares in working_positions.items():
        vp = _get_valuation_price(t, prices, last_prices)
        if vp is not None:
            current_total_value += shares * vp
    current_total_pct = current_total_value / nav if nav > 0 else 0

    remaining_budget = max(0, max_total_position - current_total_pct)

    current_defense_count = sum(1 for t in working_positions if t in defense_tickers)
    remaining_slots = max_total_holdings - len(working_positions)
    defense_slots = min(remaining_slots, max_defense_holdings - current_defense_count)
    new_defense = [t for t in tradable_defense_tickers if t not in working_positions][:defense_slots]

    if new_defense and working_cash > 0 and remaining_budget > 0:
        # 防御预算 = min(剩余现金, 剩余风险预算)
        defense_budget = min(working_cash * 0.95, nav * remaining_budget)
        defense_per_etf = min(
            defense_budget / len(new_defense) if len(new_defense) > 0 else 0,
            nav * max_position_per_etf * max_total_position,
        )

        defense_orders = []
        for t in new_defense:
            price = buy_prices.get(t, 0)
            if price is None or price <= 0:
                continue
            shares = _calc_buy_shares(defense_per_etf, price, lot_size)
            if shares <= 0:
                continue
            amount = shares * price
            commission = _calc_commission(amount, commission_rate, min_commission)
            defense_orders.append({
                'ticker': t,
                'shares': shares,
                'price': price,
                'amount': amount,
                'commission': commission,
            })

        # 检查防御订单是否突破总仓位上限
        defense_total_value = sum(o['amount'] for o in defense_orders)
        new_total_pct = (current_total_value + defense_total_value) / nav
        if new_total_pct > max_total_position + 0.001:
            # 按剩余预算重新分配
            max_defense_value = max(0, nav * max_total_position - current_total_value)
            if max_defense_value > 0 and defense_orders:
                defense_per_etf_v2 = min(max_defense_value / len(defense_orders), defense_per_etf)
                for o in defense_orders:
                    o['shares'] = _calc_buy_shares(defense_per_etf_v2, o['price'], lot_size)
                    o['amount'] = o['shares'] * o['price']
                    o['commission'] = _calc_commission(o['amount'], commission_rate, min_commission)
                defense_orders = [o for o in defense_orders if o['shares'] > 0]

        # 执行防御订单
        total_defense_cost = sum(o['amount'] + o['commission'] for o in defense_orders)
        if total_defense_cost <= working_cash:
            for o in defense_orders:
                working_cash -= (o['amount'] + o['commission'])
                working_positions[o['ticker']] = o['shares']
                orders.append({
                    'action': 'BUY',
                    'ticker': o['ticker'],
                    'shares': o['shares'],
                    'price': o['price'],
                    'amount': o['amount'],
                    'commission': o['commission'],
                    'reason': '防御填充',
                })

    # ========== Step 9: 最终状态 ==========
    industry_value = 0.0
    defense_value = 0.0
    for t, shares in working_positions.items():
        vp = _get_valuation_price(t, prices, last_prices)
        if vp is not None:
            if t in industry_tickers:
                industry_value += shares * vp
            elif t in defense_tickers:
                defense_value += shares * vp

    total_positions_value = industry_value + defense_value

    # NAV勾稽：working_cash + total_positions_value + 总佣金 = nav
    total_commission = sum(o['commission'] for o in orders)
    nav_check = working_cash + total_positions_value + total_commission
    nav_diff = abs(nav_check - nav)

    final_state = {
        'cash': working_cash,
        'positions': working_positions,
        'industry_value': industry_value,
        'defense_value': defense_value,
        'total_positions_value': total_positions_value,
        'total_slots': len(working_positions),
        'total_position_pct': total_positions_value / nav if nav > 0 else 0,
        'industry_position_pct': industry_value / nav if nav > 0 else 0,
        'defense_position_pct': defense_value / nav if nav > 0 else 0,
        'nav': nav,
        'nav_check': nav_check,
        'nav_diff': nav_diff,
        'num_orders': len(orders),
    }

    return orders, final_state


# 向后兼容v2.4阶段的研究脚本和测试；Phase 2集成应使用v2.5名称。
plan_rebalance_v2_4 = plan_rebalance_v2_5
