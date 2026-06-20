# B0 调仓引擎重构设计文档 v2

**状态**: 设计阶段，提交评审
**目标**: 将资金规划提取为独立可测纯函数 `plan_rebalance`
**原则**: 先实现可独立测试的纯函数，再集成到回测引擎
**当前**: 未修改 `backtest.py`，未开始重构

---

## 一、新纯函数设计

### `plan_rebalance` 接口

```python
def plan_rebalance(
    nav: float,
    cash: float,
    current_positions: dict,       # {ticker: {'shares': int, 'price': float}}
    industry_candidates: list,     # [(ticker, score), ...] 已按评分降序
    defense_candidates: list,      # [(ticker, score), ...] 已按评分降序
    prices: dict,                  # {ticker: float}
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

### 算法流程（正确设计，非当前实现）

1. **分类当前持仓**：区分行业ETF和防御资产
2. **确定行业目标组合**：保留仍在候选中的，卖出掉出的，新买入候选中不在持仓的
3. **计算行业目标金额**：统一分配，不依赖顺序。若现金不足，等比例缩减
4. **生成行业订单**：先卖出，再买入。100股取整
5. **防御资产只承接剩余**：行业完成后，防御用剩余资金和槽位填充
6. **返回完整订单计划**：包含所有BUY/SELL订单和最终状态

### 核心设计决策

| 决策 | 说明 |
|------|------|
| 行业ETF优先 | 防御资产只在行业配置完成后执行 |
| 槽位分离 | 行业上限和防御上限独立计算，不共享 `max_new` |
| 统一分配 | 同一优先级的所有订单，先统一计算目标，再按比例缩减 |
| 不补仓 | 保留的行业ETF低于目标时不主动买入，避免频繁交易 |
| 100股取整 | 所有买入股数按 `lot_size` 取整 |
| 佣金计算 | 每笔交易单独计算 `max(amount * rate, min_commission)` |
| 防御让路 | 牛市时防御应被卖出，资金用于行业ETF |

---

## 二、测试覆盖场景

### 场景1: 40%现金同时买3只，资金近似等分
- NAV=100万，cash=40万，无持仓
- 3只行业候选，各评分>40
- 预期：3只各买入约12.67万（40万*0.95/3），差异<5%
- 旧实现缺陷：逐只顺序分配，第一只拿满20%，后面不足

### 场景2: 防御资产为行业ETF让路
- 持有1只防御（市值30万），cash=70万，nav=100万
- 3只行业候选，max_total_position=1.0（牛市）
- 预期：防御应被卖出，资金用于行业买入
- 旧实现缺陷：防御不会被主动卖出

### 场景3: 防御不占行业槽位
- 持有2只防御（市值30万），无行业持仓，nav=100万，cash=70万
- 5只行业候选
- 预期：可以买入5只行业ETF（防御槽位和行业槽位独立）
- 旧实现缺陷：max_new共享，防御占2槽，行业只能买3

### 场景4: 候选顺序变化不改变分配
- 同样的候选列表，两种不同排序
- 预期：两种排序的总持仓金额相同，每只分配相同
- 旧实现缺陷：逐只分配，顺序影响结果

### 场景5: 已有仓位15%保留，不补仓
- 持有1只行业ETF，市值15万（NAV=100万，15%）
- 该ETF仍在候选列表中，cash=85万
- 预期：保留，不主动补仓（保持15%，不补到20%）
- 设计决策：不补仓，避免频繁交易

### 场景6: 已有仓位超过20%时不买也不卖
- 持有1只行业ETF，市值25万（NAV=100万，25%）
- 该ETF仍在候选列表中，cash=75万
- 预期：保留，不卖出，不买入（不超过上限不机械减仓）

### 场景7: 总仓位限制（max_total_position=0.8）
- NAV=100万，cash=100万，5只候选
- max_total_position=0.8
- 预期：总持仓不超过80%（40万+防御填充）

### 场景8: 佣金和100股取整
- 买入某ETF，价格=1.5，目标=20万
- 预期：股数 = (200000/1.5 // 100) * 100 = 133300股 = 199,950元
- 旧实现缺陷：int(200000/1.5) = 133333股（未取整）

### 场景9: 2026-03-12复现场景
- NAV=172.6万，cash=88.9万
- 保留1只行业（油气18.5万）+ 1只防御（黄金65.2万）
- 3只新行业候选（通信、新能源、养殖）
- max_total_position=1.0（牛市）
- 预期：3只新行业ETF全部买入，总行业≈80%
- 旧实现：只买入1只（因为槽位和资金分配问题）

### 场景10: 计划订单执行后现金、持仓与NAV勾稽
- 对多个场景验证：final_cash + final_industry + final_defense = NAV
- 容差<1%

### 场景11: 卖出后资金同步
- 持有1只防御30万，需要卖出为行业让路
- 卖出后，新买入的可用资金应包含卖出所得
- 旧实现缺陷：卖出后 available_cash 未更新

---

## 三、实施计划

### Phase 1: 当前状态（已完成）
- [x] 设计 `plan_rebalance` 纯函数接口和算法
- [x] 创建有缺陷的骨架实现（模拟旧逻辑）
- [x] 编写11个自动化测试，覆盖所有场景
- [x] 验证测试在骨架实现上因具体数值断言失败

### Phase 2: 实现正确算法（待评审后开始）
- [ ] 重写 `plan_rebalance` 为正确算法
- [ ] 确保所有11个测试从FAIL转为PASS
- [ ] 将 `plan_rebalance` 集成到 `backtest.py`

### Phase 3: 回归验证（待Phase 2完成后）
- [ ] 重新运行B0回测
- [ ] 对比新旧结果
- [ ] 重新冻结基线

---

*设计文档完成，等待评审。*
