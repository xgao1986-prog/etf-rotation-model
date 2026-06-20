# B0 调仓引擎重构设计文档 v2.2

**状态**: Phase 1 完成（测试通过）  
**目标**: 修复 backtest.py 中的结构性资金分配缺陷  
**原则**: 目标组合优先，防御让路，顺序独立  

---

## 一、当前代码的结构性缺陷（已确认）

### 1. [P0] 防御资产优先级写反
**文件**: `src/backtest.py:927-1044`  
**问题**: 防御配置在核心ETF买入前执行，并在满仓时卖出非防御持仓为防御资产腾位置，直接违背"行业ETF优先、防御资产仅承接剩余资金"的策略定义。

### 2. [P0] 防御资产占用行业槽位
**文件**: `src/backtest.py:909-924`  
**问题**: `max_new = total_max_holdings - current_holdings` 按全部持仓数量计算，防御资产与行业ETF共享 `total_max_holdings=5`。持有2只防御资产后，行业ETF最多只能再买3只。

### 3. [P0] 买入结果依赖候选顺序
**文件**: `src/backtest.py:1193-1212`  
**问题**: 每只ETF依次按 `NAV * 20%` 和 `remaining_cash * 95%` 下单，未先生成全体订单计划。前面的候选会消耗现金，后面的仓位被压缩或跳过。

### 4. [P1] 实际成交未按100股取整
**文件**: `src/backtest.py:1200`  
**问题**: `shares = int(target_amount / price)` 只做了整数截断，未按A股ETF 100股=1手取整。

### 5. [P1] 卖出后可用资金未同步
**文件**: `src/backtest.py:1024-1044`  
**问题**: 为防御资产腾仓后只增加 `portfolio.cash`，没有同步增加 `available_cash`。

---

## 二、v2.2 纯函数设计

### 核心原则

1. **目标组合优先**: 调仓日先计算"目标组合"（哪些保留、哪些卖出、哪些买入），再生成订单计划。
2. **统一资金分配**: 对同一优先级的所有订单，统一计算目标金额，避免顺序依赖。
3. **严格优先级**: 行业ETF → 防御资产 → 现金。防御资产无条件为行业让路（槽位+资金）。
4. **槽位分离**: 行业ETF优先占满5只槽位，防御临时占槽但让路。
5. **100股取整**: 所有买入股数按100股取整，不足100股的部分不买入。
6. **资金同步**: 任何卖出操作后，立即同步更新可用资金。
7. **精确NAV恒等式**: 所有输入必须满足 `cash + sum(shares*price) = nav`，容差≤0.01元。

### 调仓流程

```python
def plan_rebalance_v2_2(nav, cash, current_positions,
                      industry_candidates, defense_candidates, prices,
                      industry_tickers, defense_tickers, ...):
    """
    Step 0: NAV恒等式校验（容差0.01元）
    Step 1: 分类候选和持仓（保留/卖出）
    Step 2: 卖出不在候选的持仓（行业+防御）
    Step 3: 确定行业目标组合（保留+新买入）
    Step 4: 如果槽位不足，防御让路（腾槽位）
    Step 5: 统一计算行业目标金额（按100股取整）
    Step 6: 如果资金不足，防御部分减持（按金额，非清仓）
    Step 7: 执行行业买入
    Step 8: 用剩余资金和槽位填充防御
    Step 9: 计算最终状态并校验NAV勾稽
    """
```

### 关键算法细节

#### 1. 防御让路（槽位）

```python
# 行业槽位 = min(新候选数, 行业上限 - 保留数)
raw_industry_slots = min(len(new_candidates), max_industry_holdings - len(retained))
# 限制总槽位
industry_slots = min(raw_industry_slots, max_total_holdings - len(working_positions))

# 如果槽位不足，防御让路
if industry_slots < raw_industry_slots:
    slots_needed = raw_industry_slots - industry_slots
    # 按评分从低到高卖出防御（低分先卖）
    for defense_ticker in sorted(current_defense, key=lambda t: defense_score_map[t]):
        if slots_needed <= 0: break
        sell_all(defense_ticker)  # 为腾槽位，全部卖出
        slots_needed -= 1
```

#### 2. 防御让路（资金）

```python
# 如果行业总成本 > 现金，部分减持防御
needed = total_industry_cost - working_cash

for defense_ticker in sorted(current_defense, key=lambda t: defense_score_map[t]):
    if needed <= 0: break
    # 计算卖出股数（覆盖needed + 佣金）
    target_sell_amount = max(needed / (1 - rate), needed + min_comm)
    sell_shares = (int(target_sell_amount / price) // 100) * 100
    sell_shares = min(sell_shares, current_shares)
    
    if sell_shares > 0:
        execute_sell(sell_shares)
        needed -= net_proceeds
```

**关键点**: 防御是**部分减持**，不是清仓。只卖出足够覆盖行业买入所需资金的股数。

#### 3. 100股取整

```python
def _calc_shares(target_amount, price, lot_size=100):
    if target_amount <= 0 or price <= 0:
        return 0
    raw = int(target_amount / price)
    return (raw // lot_size) * lot_size
```

#### 4. 统一资金分配（避免顺序依赖）

```python
# 所有行业新买入使用统一目标金额
n_industry = len(retained) + len(new_buy)
per_etf_pct = min(max_position_per_etf, 1.0 / n_industry)
per_etf_target = nav * per_etf_pct * max_total_position

# 对每只新买入标的，独立计算股数
for ticker in new_buy:
    shares = _calc_shares(per_etf_target, prices[ticker])
```

---

## 三、测试覆盖（12个，全部通过）

| 测试 | 场景 | 核心断言 |
|------|------|----------|
| test_empty_portfolio_full_buy | 空仓，5只行业候选 | 买入5只，无防御，槽位=5，NAV勾稽 |
| test_defense_reduction_for_industry | 有防御，资金不足 | 防御部分减持（非清仓），行业买入 |
| test_retained_must_be_in_candidates | 保留标的不在候选 | 必须卖出，不在最终持仓 |
| test_order_independence_with_cash_constraint | 资金受限，候选顺序不同 | 两种顺序总持仓金额相同 |
| test_real_snapshot_2026_03_12 | 2026-03-12真实快照 | NAV恒等式精确成立，518880.SH卖出 |
| test_holding_missing_price | 持仓标的无价格 | 无法卖出，保留在持仓中 |
| test_sell_ticker_not_tradable | 卖出标的无价格 | 无法卖出，保留在持仓中 |
| test_retained_below_20pct_no_rebalance | 保留标的低于20% | 不补仓，股数不变 |
| test_defense_temp_slots_yield_to_industry | 2只防御+5只行业 | 防御让路，行业买入5只 |
| test_nav_identity_check | NAV恒等式不成立 | 抛出ValueError |
| test_defense_does_not_use_industry_slots | 2只防御+5只行业+资金受限 | 防御让路，行业≥3只 |
| test_retained_not_rebought | 已持仓在候选中 | 不再买入，股数不变 |

---

## 四、2026-03-12 真实快照数据

从回测导出：

| 字段 | 值 |
|------|-----|
| NAV | 1,942,444.465 |
| Cash | 997,561.292 |
| 518880.SH | 55,522股 @ close=10.938（市值=607,299.636） |
| 159697.SZ | 216,956股 @ close=1.556（市值=337,583.536） |
| 持仓市值 | 944,883.172 |
| 现金+持仓 | 1,942,444.464（差异0.001元，浮点误差） |

**当日信号**（2026-03-12）：
- BUY: 159697.SZ(90.0), 159865.SZ(78.875), 516160.SH(76.4375), 515880.SH(57.75)
- HOLD: 512800.SH(45.3125)
- SELL: 其他12只
- 防御候选：无

---

## 五、实施状态

- [x] Phase 1: 编写失败测试（12个测试全部通过）
- [ ] Phase 2: 设计文档评审（当前阶段）
- [ ] Phase 3: 在 `src/backtest.py` 中实现新的 `rebalance` 方法
- [ ] Phase 4: 回归验证

**注意**: 本次提交仅包含 `src/rebalance_planner.py`（纯函数）和 `tests/test_rebalance_planner.py`（测试），不修改 `src/backtest.py`。

---

*设计文档 v2.2 完成，等待Phase 2评审。*
