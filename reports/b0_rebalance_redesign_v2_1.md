# B0 调仓引擎重构设计文档 v2.1

**状态**: 设计阶段，提交评审（修正后）
**目标**: 将资金规划提取为独立可测纯函数 `plan_rebalance`
**原则**: 先实现可独立测试的纯函数，再集成到回测引擎
**当前**: 未修改 `backtest.py`，未开始重构
**基线**: 冻结基线 B0-18，v5 回测审计通过

---

## 一、修订记录（v2.1 相对于 v2）

| 修订项 | v2 | v2.1 |
|--------|-----|------|
| 接口参数 | 无 ticker 分类 | 增加 `industry_tickers: set` / `defense_tickers: set`（显式传入，不反推） |
| 测试数据 | 部分不满足 cash+positions=NAV | 全部修正，满足 `cash + sum(shares*price) = nav` |
| NAV 勾稽 | 未扣除佣金，容差<1% | 扣除佣金：`final_cash + final_industry + final_defense + commissions = nav`，容差≤0.05元 |
| 测试数量 | 11 个 | 15 个（新增缺失价格、不可交易ETF、取整后剩余现金、部分防御卖出） |
| 测试结果 | 6 失败 5 通过 | 7 失败 8 通过 |

---

## 二、新纯函数设计

### `plan_rebalance` 接口

```python
def plan_rebalance(
    nav: float,
    cash: float,
    current_positions: dict,       # {ticker: {'shares': int, 'price': float}}
    industry_candidates: list,     # [(ticker, score), ...] 已按评分降序
    defense_candidates: list,      # [(ticker, score), ...] 已按评分降序
    prices: dict,                  # {ticker: float} 当日价格
    industry_tickers: set,         # 行业ETF代码集合（显式传入，不反推）
    defense_tickers: set,          # 防御资产代码集合（显式传入，不反推）
    max_industry_holdings: int,
    max_defense_holdings: int,
    max_total_holdings: int,
    max_position_per_etf: float,
    max_total_position: float,
    commission_rate: float,
    min_commission: float,
    lot_size: int = 100,
    rebalance_date: str = None,
):
    """
    生成调仓日目标组合和订单计划。

    返回: dict
    {
        'orders': [
            {'ticker': str, 'action': 'BUY'|'SELL', 'shares': int,
             'price': float, 'amount': float, 'commission': float,
             'total_cost': float, 'reason': str}
        ],
        'target_positions': {ticker: target_shares},
        'final_cash': float,
        'final_industry_value': float,
        'final_defense_value': float,
        'final_total_position': float,
    }
    """
```

**关键设计决策**：
- `industry_tickers` / `defense_tickers` 必须显式传入，不得从 `industry_candidates` / `defense_candidates` 反推
- 当前持仓的行业/防御分类完全基于这两个集合
- 保留的持仓（仍在候选中的）不触发任何交易

### 算法流程（正确设计，非当前实现）

1. **分类当前持仓**：使用显式传入的 `industry_tickers` / `defense_tickers` 区分行业和防御
2. **确定行业目标组合**：保留仍在候选中的，卖出掉出的，新买入候选中不在持仓的
3. **计算行业目标金额**：统一分配，不依赖顺序。若现金不足，等比例缩减。若仍不足，防御部分卖出让路
4. **生成行业订单**：先卖出（更新可用资金），再买入。100股取整。佣金单独计算
5. **防御资产只承接剩余**：行业完成后，防御用剩余资金和槽位填充。牛市时防御应被卖出为行业让路
6. **返回完整订单计划**：包含所有BUY/SELL订单和最终状态

### 核心设计决策

| 决策 | 说明 |
|------|------|
| 行业ETF优先 | 防御资产只在行业配置完成后执行 |
| 槽位分离 | 行业上限和防御上限独立计算，不共享 `max_new` |
| 统一分配 | 同一优先级的所有订单，先统一计算目标，再按比例缩减 |
| 不补仓 | 保留的行业ETF低于目标时不主动买入，避免频繁交易 |
| 100股取整 | 所有买入股数按 `(int(target/price) // 100) * 100` 取整 |
| 佣金计算 | 每笔交易单独计算 `max(amount * rate, min_commission)` |
| 防御让路 | 牛市时防御应被部分或全部卖出，资金用于行业ETF |
| 部分卖出 | 防御市值大于所需金额时，只卖出所需部分，不一律清仓 |

---

## 三、测试覆盖场景（15个）

### 场景1: 有限资金买3只，资金近似等分
- NAV=100万，cash=40万，持有1防御60万
- 3只行业候选，各评分>40
- 预期：3只各买入约12.67万（40万*0.95/3），差异<5%
- 旧实现缺陷：逐只顺序分配，第一只拿满20万，第三只仅9,442

### 场景2: 防御资产为行业ETF让路（部分减持）
- NAV=100万，cash=20万，持有1防御30万 + 1保留行业50万
- 3只行业候选，每只目标20万，总目标60万
- 预期：防御部分卖出约40万，3只行业全部买入，总行业≈80%
- 旧实现缺陷：防御不卖出，只用20万现金，行业只买约20万

### 场景3: 防御不占行业槽位
- NAV=100万，cash=70万，持有2只防御（各15万=30万）
- 5只行业候选
- 预期：可买入5只行业ETF（防御槽位和行业槽位独立）
- 旧实现缺陷：max_new共享，防御占2槽，行业只能买3

### 场景4: 候选顺序变化不改变分配
- 同样的候选列表，两种不同排序
- 预期：两种排序的总持仓金额相同，每只分配相同
- 旧实现缺陷：逐只分配，顺序影响结果（200,000 vs 9,442）

### 场景5: 已有仓位15%保留，不补仓
- 持有1只行业ETF，市值15万（NAV=100万，15%），cash=85万
- 该ETF仍在候选列表中
- 预期：保留，不主动补仓（保持15%，不补到20%）

### 场景6: 已有仓位超过20%时不买也不卖
- 持有1只行业ETF，市值25万（NAV=100万，25%），cash=75万
- 该ETF仍在候选列表中
- 预期：保留，不卖出，不买入（超过上限不机械减仓）

### 场景7: 总仓位限制（max_total_position=0.8）
- NAV=100万，cash=100万，5只行业+1防御候选
- max_total_position=0.8
- 预期：总持仓不超过80%

### 场景8: 佣金和100股取整
- 买入某ETF，价格=1.5，目标=20万
- 预期：股数 = 133,300股 = 199,950元，佣金=59.99
- 旧实现缺陷：int(200000/1.5) = 133,333股（未取整）

### 场景9: 2026-03-12复现场景（真实数据）
- NAV=2,164,027，cash=1,326,016，保留1行业(185,537) + 1防御(652,473)
- 3只新行业候选：515880.SH(通信)、516160.SH(新能源)、159865.SZ(养殖)
- max_total_position=1.0（牛市）
- 预期：3只新行业ETF全部买入，总行业≈80%（4只×20%）
- 旧实现：防御占用槽位，可能导致部分新候选无法买入

### 场景10: 计划订单执行后现金、持仓与NAV勾稽（多场景）
- 对多个场景验证：`final_cash + final_industry + final_defense + commissions = NAV`
- 容差≤0.05元（考虑浮点舍入）

### 场景11: 卖出后资金同步
- 持有1防御30万，需要卖出为行业让路
- 卖出后，新买入的可用资金应包含卖出所得
- 旧实现缺陷：卖出后 `available_cash` 未更新，后续买入受限

### 场景12: 缺失价格（ETF不在prices中）
- 某候选ETF在prices中缺失
- 预期：被跳过，不影响其他买入

### 场景13: 不可交易ETF（价格为0或负）
- 某候选ETF价格为0或负
- 预期：被跳过，防止除以零

### 场景14: 取整后剩余现金
- 买入目标金额20万，价格=1.5，100股取整后
- 预期：股数=133,300，金额=199,950，剩余50元保留为现金
- 旧实现缺陷：未取整，买入133,333股，金额=199,999.5

### 场景15: 部分防御卖出（防御市值>所需金额）
- 持有1防御100万，需要卖出30万为行业让路
- 预期：防御只卖出30万（约10,000股），保留70万
- 旧实现缺陷：防御一律清仓，卖出全部33,333股

---

## 四、当前测试结果

```
测试文件: tests/test_rebalance_planner.py
骨架实现: src/rebalance_planner.py（有缺陷，模拟旧逻辑）

Summary: 8 passed, 7 failed

Failures（具体数值断言失败）:
  test_equal_allocation_with_limited_cash: 资金分配不均143.1%
  test_defense_partial_yields_to_industry: 防御不卖出
  test_defense_does_not_use_industry_slots: 只买入3只（预期5只）
  test_commission_and_lot_rounding: 未按100股取整（133,333 vs 133,300）
  test_sell_syncs_cash: 防御不卖出
  test_lot_rounding_leaves_cash: 未取整（133,333）
  test_defense_partial_sell: 防御不卖出（应部分卖出）

Passing（旧实现正确或测试验证基本行为）:
  test_order_independence: 两种排序总买入相同（但单只分配不同）
  test_existing_15pct_not_topped_up: 不补仓
  test_existing_over_20pct_unchanged: 不超卖
  test_total_position_limit: 总仓位<=80%
  test_2026_03_12_reproduction: 3只全部买入（旧实现碰巧通过）
  test_nav_consistency_all_scenarios: NAV勾稽通过
  test_missing_price_skipped: 跳过缺失价格
  test_untradable_etf_skipped: 跳过不可交易
```

---

## 五、实施计划

### Phase 1: 当前状态（已完成）
- [x] 设计 `plan_rebalance` 纯函数接口和算法
- [x] 创建有缺陷的骨架实现（模拟旧逻辑）
- [x] 编写15个自动化测试，覆盖所有场景
- [x] 验证测试数据满足 `cash + positions = NAV`
- [x] 验证 NAV 勾稽扣除佣金，容差≤0.05元
- [x] 验证7个测试在骨架实现上因具体数值断言失败

### Phase 2: 实现正确算法（待评审后开始）
- [ ] 重写 `plan_rebalance` 为正确算法
- [ ] 确保所有15个测试从FAIL转为PASS
- [ ] 将 `plan_rebalance` 集成到 `backtest.py`

### Phase 3: 回归验证（待Phase 2完成后）
- [ ] 重新运行B0回测
- [ ] 对比新旧结果
- [ ] 重新冻结基线

---

*设计文档 v2.1 完成，等待评审。*
