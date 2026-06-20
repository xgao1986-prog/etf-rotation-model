# B0 调仓引擎重构设计文档 v2.5

**状态**: 核心修复完成；最终25项回归重跑待执行  
**目标**: 生成顺序独立、满足现金与总仓位预算的确定性订单计划  
**原则**: 先统一缩放全部候选，只有缩放后不足一手时才按评分淘汰  

---

## 一、v2.3 → v2.5 修复清单

### Codex最终验收补充修复

在Kimi提交的20项测试基础上，Codex增加并修复：

1. **行业买入同样受总仓位预算约束**：保留行业已占75%、总上限80%时，新行业最多使用剩余5%预算。
2. **防御为行业释放风险预算**：即使现金充足，只要行业买入会突破总仓位，仍按需减持防御。
3. **同分候选稳定排序**：评分相同时使用ticker作为次级排序，不依赖输入顺序。
4. **严格估值契约**：任一现有持仓既无当日价又无`last_prices`时立即报错，不能依赖NAV碰巧相等。
5. **确定性共同缩放**：使用二分搜索求同时满足现金、佣金、整手和总仓位预算的最大共同缩放比例。

### [P0] 淘汰循环前置改变策略语义

**问题**: v2.4 将淘汰循环（`sort + pop(0)`）放在统一缩放之前，导致"现金不足时先淘汰低分候选，剩余候选维持原目标仓位"。这与原定规则"所有已入选候选同比例缩放"冲突。

**复现场景**:
- NAV=100万，保留R=60万，现金=40万
- A/B/C 价格=333，各目标=20万
- 合理结果：A/B/C 各约13.3万（同比例缩放）
- v2.4 错误结果：淘汰C，A/B 各20万

**修复** (v2.5 Step 6):
1. 先对所有已入选候选计算统一缩放比例 `scale = working_cash / estimated_total`
2. 对每个候选应用缩放并取整：`shares = _calc_buy_shares(original_amount * scale, price)`
3. **仅当**缩放后某候选不足一手（`shares < lot_size`）或总成本仍超现金（取整效应）时，才淘汰评分最低者
4. 淘汰后，用剩余候选重新计算统一缩放比例
5. 重复直到所有剩余候选都至少一手且总成本不超现金

```python
while industry_buy_orders:
    # 用原始金额计算统一缩放比例
    total_amount = sum(o['original_amount'] for o in industry_buy_orders)
    estimated_total = ...  # 含佣金估算
    scale = working_cash / estimated_total

    # 对所有候选应用同一缩放比例
    for o in industry_buy_orders:
        o['shares'] = _calc_buy_shares(o['original_amount'] * scale, o['price'])
        o['amount'] = o['shares'] * o['price']
        o['commission'] = _calc_commission(o['amount'], ...)

    # 检查是否所有候选都至少一手
    if all(o['shares'] >= lot_size for o in industry_buy_orders):
        if sum(o['amount'] + o['commission'] for o in industry_buy_orders) <= working_cash:
            break  # 成功，保留全部

    # 只有不足一手或超现金时才淘汰
    industry_buy_orders.sort(key=lambda x: x['score'])
    removed = industry_buy_orders.pop(0)
```

**关键实现细节**:
- 保留 `original_amount` 字段，避免循环中修改 `amount` 导致后续迭代基于错误值
- `original_amount` 是 Step 5 中按 `per_etf_target` 计算的目标金额（取整前）
- 每次循环都基于 `original_amount` 重新计算，确保淘汰后剩余候选的正确缩放

### [P0] 无估值持仓必须报错

**问题**: v2.3 对缺价持仓用 `last_prices` 估值，但当持仓既无当日价格也无 `last_prices` 时，`NAV` 恒等式检查可能无法捕获（如果传入的 `nav` 也不包含该持仓）。

**修复** (v2.5 Step 0):
- `NAV` 恒等式检查：`abs(valued_positions + cash - nav) >= 0.01` 时报错
- 如果持仓无估值价，其市值不计入 `valued_positions`
- 如果传入的 `nav` 包含了该持仓市值，`valued_positions + cash < nav`，恒等式失败，报错
- 测试覆盖：`test_unpriced_position_raises_error`

---

## 二、测试覆盖

核心修复后已验证`24 passed`；随后仅增加v2.5规范入口兼容测试。最终25项完整重跑因Codex当前执行额度限制尚未完成，恢复执行能力后必须先运行：

```powershell
python -m pytest tests\test_rebalance_planner.py -q
python -m py_compile src\rebalance_planner.py tests\test_rebalance_planner.py
```

| # | 测试 | 场景 | 核心断言 |
|---|------|------|----------|
| 1 | test_empty_portfolio_full_buy | 空仓，5只行业 | 买入5只，无防御，精确佣金，NAV勾稽 |
| 2 | test_defense_reduction_for_industry | 有防御，资金不足 | 防御部分减持（非清仓），卖出金额≥所需 |
| 3 | test_retained_must_be_in_candidates | 保留标的不在候选 | 必须卖出，不在最终持仓 |
| 4 | test_order_independence_minimal_counterexample | 槽位=1，顺序不同 | 都买入A |
| 5 | test_order_independence_cash_constrained | 资金受限，顺序不同 | 两种顺序持仓相同 |
| 6 | test_untradable_candidate_no_defense_roundtrip | B缺价，D1不卖出 | 防御不无效往返 |
| 7 | test_missing_price_with_last_prices | MISSING用last_prices | 用last_prices估值，不强制归零 |
| 8 | test_sell_ticker_not_tradable | 卖出标的缺价 | 无法卖出，保留在持仓中 |
| 9 | test_retained_below_20pct_no_rebalance | 保留低于20% | 不补仓，股数不变 |
| 10 | test_defense_temp_slots_yield_to_industry | 2防御+5行业 | 防御让路，行业买入5只 |
| 11 | test_nav_identity_check | NAV恒等式不成立 | 抛出ValueError |
| 12 | test_max_total_position_constraint | 行业75%，上限80% | 防御不超过5%，总仓位≤80% |
| 13 | test_defense_does_not_use_industry_slots | 2防御+5行业+资金受限 | 防御让路，行业≥3只 |
| 14 | test_retained_not_rebought | 已持仓在候选中 | 不再买入，股数不变 |
| 15 | test_defense_sell_rounds_up | 防御部分卖出 | 卖出向上取整，筹措≥所需 |
| 16 | test_commission_exact | 佣金精确 | 每只佣金=max(amount*rate, 5)，≥5元 |
| 17 | test_real_snapshot_2026_03_12_open | 2026-03-12真实快照 | 开盘价成交价，NAV精确匹配 |
| 18 | **test_cash_shortage_equal_scale_all_retained** | 现金40万，3候选各目标20万 | **全部保留，各约400股，同比例缩放** |
| 19 | **test_elimination_only_when_below_lot_size** | price=1500，现金20万 | **缩放后0股，淘汰C→B→A，仅保留A** |
| 20 | **test_unpriced_position_raises_error** | R无当日价无last_prices | **抛出ValueError** |

---

## 三、核心API

```python
def plan_rebalance_v2_5(
    nav: float,
    cash: float,
    current_positions: Dict[str, int],
    industry_candidates: List[Tuple[str, float]],  # 任意顺序，内部按评分排序
    defense_candidates: List[Tuple[str, float]],
    prices: Dict[str, float],  # 当日交易价格（用于执行）
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
) -> Tuple[List[Dict], Dict]:
    """返回 (orders, final_state)"""
```

### 关键字段

```python
industry_buy_orders = [{
    'ticker': str,
    'shares': int,          # 当前缩放后的股数
    'price': float,
    'amount': float,         # 当前缩放后的金额
    'original_amount': float,  # 初始整手目标金额（保留用于循环中重新计算）
    'commission': float,
    'score': float,          # 评分，用于淘汰排序
}]
```

---

## 四、资金分配算法

### 步骤

1. **确定已入选候选**：有槽位、可交易、不在当前持仓中
2. **计算原始目标金额**：`per_etf_target = nav * min(max_position, 1/n) * max_total_position`
3. **原始取整**：`shares = _calc_buy_shares(per_etf_target, price)`，`amount = shares * price`
4. **检查资金**：
   - 如果总成本 <= 现金，直接执行（无需缩放）
   - 如果总成本 > 现金，先尝试防御让路（卖出）
5. **统一缩放**：
   - 同时计算现金预算和剩余总仓位预算
   - 使用二分搜索求最大共同`scale`
   - 对所有候选：`new_shares = _calc_buy_shares(original_amount * scale, price)`
   - 如果所有候选 `new_shares >= lot_size` 且总成本 <= 现金，全部保留
   - 否则，淘汰评分最低者，重新计算
6. **执行买入**：统一执行所有剩余候选的买入订单

### 淘汰规则

**唯一淘汰条件**：缩放后某候选的股数 < `lot_size`（100股），或所有候选的总成本 > `working_cash`（取整效应）。

**淘汰顺序**：去掉评分最低者；同分时使用ticker确定性决胜。

**注意**：淘汰不是因为"现金不够买所有候选"，而是因为"即使同比例缩放后，某些候选仍然无法买到一手"。

---

## 五、实施状态

- [ ] Phase 1: 纯函数最终25项回归待重跑
- [ ] Phase 2: 设计文档评审（当前阶段）
- [ ] Phase 3: 在 `src/backtest.py` 中实现新的 `rebalance` 方法
- [ ] Phase 4: 回归验证

**注意**: 当前纯函数尚未集成到`src/backtest.py`。旧名`plan_rebalance_v2_4`仅作为兼容别名；Phase 2应使用`plan_rebalance_v2_5`。

---

*纯函数v2.5核心缺陷已修复；最终回归通过后进入backtest.py集成设计。*
