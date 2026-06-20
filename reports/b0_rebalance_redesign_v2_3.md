# B0 调仓引擎重构设计文档 v2.3

**状态**: Phase 1 完成（15个测试全部通过）  
**目标**: 修复 v2.2 三个P0缺陷 + 补回测试缺口  
**原则**: 目标组合优先，防御让路，顺序独立，总仓位受控，缺价不强制归零

---

## 一、v2.2 → v2.3 修复清单

### [P0] 防御填充突破 max_total_position

**问题**: 防御买入只检查单只额度（`max_position_per_etf`）和现金，没有检查组合剩余风险预算。行业已占75%、上限80%时，防御仍买入16%，最终91%。

**修复** (v2.3 Step 7):
- 防御填充前计算 `current_total_pct = (industry_value + defense_value) / nav`
- 计算 `remaining_budget = max(0, max_total_position - current_total_pct)`
- 防御目标金额 = `min(remaining_cash * 0.95, remaining_budget * nav / n_defense)`
- 如果新防御总仓位仍突破上限，按 `max_total_position` 重新缩放

```python
current_total_pct = (current_industry_value + current_defense_value) / nav
remaining_budget = max(0, max_total_position - current_total_pct)

# 防御填充后检查
new_total_pct = (current_industry_value + current_defense_value + defense_total_value) / nav
if new_total_pct > max_total_position + 0.001:
    max_defense_value = max(0, nav * max_total_position - current_industry_value - current_defense_value)
    # 重新按 max_defense_value 分配
```

### [P0] 现金缩放仍受候选顺序影响

**问题**: v2.2 按 `working_cash / total_industry_cost` 缩放后，重新计算每只佣金，然后执行时逐个检查 `if total_cost > working_cash: continue`。前面的订单消耗现金，后面的被跳过，交换顺序改变最终持仓。

**修复** (v2.3 Step 5-6):
1. 防御减持后，如果仍不足，统一比例缩减所有订单的目标金额（在取整之前）
2. 按同一 `scale` 缩减每只金额，再统一100股取整
3. 计算新佣金，确保总成本 <= working_cash
4. 如果仍超过，去掉评分最低的一只（最后一只），重新计算
5. 统一执行所有行业买入（不逐只跳过），因为缩放已确保总成本 <= 现金

```python
# 统一缩放
if total_industry_cost > working_cash:
    scale = working_cash / estimated_total
    for o in industry_buy_orders:
        o['shares'] = _calc_buy_shares(o['amount'] * scale, o['price'], lot_size)
        o['amount'] = o['shares'] * o['price']
        o['commission'] = _calc_commission(o['amount'], rate, min_comm)
    
    # 去掉超出现金的最后一只
    while industry_buy_orders and sum(o['amount']+o['commission'] for o in industry_buy_orders) > working_cash:
        industry_buy_orders.pop()  # 去掉最低评分的

# 统一执行（不逐只跳过）
for o in industry_buy_orders:
    working_cash -= (o['amount'] + o['commission'])
    # ... execute buy
```

### [P1] 防御卖出向下取整

**问题**: v2.2 复用 `_calc_shares`（向下取整）计算卖出股数，可能无法筹足 `needed`，继而无谓缩减行业订单。

**修复** (v2.3 Step 5):
- 新增 `_calc_sell_shares` 向上取整到整手
- 确保 `sell_amount >= target_sell_amount + commission`

```python
def _calc_sell_shares(target_amount, price, lot_size=100):
    if target_amount <= 0 or price <= 0:
        return 0
    raw = int(target_amount / price)
    return ((raw + lot_size - 1) // lot_size) * lot_size
```

### 缺价持仓不强制归零

**问题**: v2.2 `prices.get(t, 0)` 将缺价持仓市值归零，测试也人为让NAV恒等式忽略该持仓，无法代表真实组合。

**修复** (v2.3 Step 0):
- NAV校验只校验"有价格"的持仓：`valued_positions = sum(shares * price for t in prices if price > 0)`
- 缺价持仓在计算中不纳入（不能交易），但保留在最终持仓中
- 卖出时：缺价持仓无法卖出（跳过），保留在持仓中

---

## 二、测试覆盖（15个，全部通过）

| # | 测试 | 场景 | 核心断言 |
|---|------|------|----------|
| 1 | test_empty_portfolio_full_buy | 空仓，5只行业 | 买入5只，无防御，精确佣金，NAV勾稽 |
| 2 | test_defense_reduction_for_industry | 有防御，资金不足 | 防御部分减持（非清仓），卖出金额≥所需 |
| 3 | test_retained_must_be_in_candidates | 保留标的不在候选 | 必须卖出，不在最终持仓 |
| 4 | **test_order_independence_per_ticker** | 资金受限，候选顺序不同 | **逐只对比**，每只ETF股数相同 |
| 5 | **test_real_snapshot_2026_03_12_open** | 2026-03-12真实快照 | **开盘价**成交价，NAV精确匹配 |
| 6 | **test_holding_missing_price_not_zeroed** | 缺价持仓 | 不强制归零，保留在持仓中 |
| 7 | test_sell_ticker_not_tradable | 卖出标的缺价 | 无法卖出，保留在持仓中 |
| 8 | test_retained_below_20pct_no_rebalance | 保留低于20% | 不补仓，股数不变 |
| 9 | test_defense_temp_slots_yield_to_industry | 2防御+5行业 | 防御让路，行业买入5只 |
| 10 | test_nav_identity_check | NAV恒等式不成立 | 抛出ValueError |
| 11 | **test_max_total_position_constraint** | **行业75%，上限80%** | **防御不超过5%，总仓位≤80%** |
| 12 | test_defense_does_not_use_industry_slots | 2防御+5行业+资金受限 | 防御让路，行业≥3只 |
| 13 | test_retained_not_rebought | 已持仓在候选中 | 不再买入，股数不变 |
| 14 | **test_defense_sell_rounds_up** | 防御部分卖出 | **卖出向上取整**，筹措≥所需 |
| 15 | **test_commission_exact** | 佣金精确 | 每只佣金=max(amount*rate, 5)，≥5元 |

---

## 三、核心API

```python
def plan_rebalance_v2_3(
    nav: float,                          # 当前组合净值
    cash: float,                        # 当前可用现金
    current_positions: Dict[str, int],  # ticker -> shares
    industry_candidates: List[Tuple[str, float]],  # [(ticker, score), ...] sorted desc
    defense_candidates: List[Tuple[str, float]],   # [(ticker, score), ...] sorted desc
    prices: Dict[str, float],           # ticker -> price (open for trading)
    industry_tickers: Set[str],         # 行业ticker集合
    defense_tickers: Set[str],          # 防御ticker集合
    max_industry_holdings: int = 5,
    max_defense_holdings: int = 2,
    max_total_holdings: int = 5,
    max_position_per_etf: float = 0.15,
    max_total_position: float = 1.0,     # 总仓位上限（大盘择时）
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
    lot_size: int = 100,
) -> Tuple[List[Dict], Dict]:
    """返回 (orders, final_state)"""
```

---

## 四、实施状态

- [x] Phase 1: 编写失败测试（15个测试全部通过）
- [ ] Phase 2: 设计文档评审（当前阶段）
- [ ] Phase 3: 在 `src/backtest.py` 中实现新的 `rebalance` 方法
- [ ] Phase 4: 回归验证

**注意**: 本次提交仅包含 `src/rebalance_planner.py`（纯函数v2.3）和 `tests/test_rebalance_planner.py`（15个测试），不修改 `src/backtest.py`。

---

*设计文档 v2.3 完成，等待Phase 2评审。*
