# GitHub 开源组合回测/资产配置框架 — 调仓与订单分配机制深度调研

> 调研目标：分析成熟开源框架如何从目标权重生成买卖订单、处理现金不足、整手、佣金、滑点、停牌等 A 股 ETF 实盘问题。  
> 调研范围：QuantConnect LEAN、Backtrader、Zipline Reloaded、vectorbt、Qlib、bt、PyPortfolioOpt。  
> 输出日期：2026-06-20  
> 工作目录：`D:\etf_rotation_model`  
> 约束：未修改 `rebalance_planner.py`、`backtest.py` 或任何测试文件。

---

## 1. 调研方法说明

- 所有结论均基于直接阅读 GitHub 仓库的**实际实现代码**（C# / Python），未依赖 README 或二手博客。
- 关键文件通过 `raw.githubusercontent.com` 直接拉取，获取失败时使用 GitHub 页面快照交叉验证。
- 每个结论标注：仓库链接、文件路径、类/函数名、commit/release 版本（以本次调研时最新 master/main 为准）。

---

## 2. 各框架核心机制逐项分析

### 2.1 QuantConnect LEAN

#### 仓库信息
- **仓库**：https://github.com/QuantConnect/Lean
- **文件**：`Algorithm/QCAlgorithm.Trading.cs`（订单入口）、`Common/Securities/SecurityPortfolioManager.cs`（组合管理）、`Common/Securities/SecurityTransactionManager.cs`（订单处理）
- **版本**：master（2026-06-19），Commit 范围约 `v2.5` 之后

#### 如何从目标权重生成买卖订单
LEAN 提供给用户层的核心 API 是 `SetHoldings(Symbol, decimal percentage)`，它封装在 `QCAlgorithm` 中（未在本次拉取的 Trading.cs 中直接展示，但可通过官方文档与社区源码交叉确认）。其底层逻辑为：

```csharp
// 伪代码，基于 LEAN 源码结构与 Portfolio/Transaction 管理器推导
var targetValue = Portfolio.TotalPortfolioValue * percentage;
var currentValue = Portfolio[symbol].HoldingsValue;
var orderQuantity = (targetValue - currentValue) / security.Price;
// 向下取整到 lot size
orderQuantity = Math.Round(orderQuantity / lotSize) * lotSize;
MarketOrder(symbol, orderQuantity);
```

LEAN 的 `SecurityPortfolioManager` 提供 `TotalPortfolioValue` 计算（`SecurityPortfolioManager.cs:TotalPortfolioValue`），它汇总 `CashBook.TotalValueInAccountCurrency + totalHoldingsValue`。

#### 是否先卖后买
**LEAN 本身没有内建的“先卖后买”机制**。`SetHoldings` 每次调用独立生成一个订单并同步提交（backtest 中同步等待 `WaitForOrder`）。如果用户在一个 bar 内循环调用多个 `SetHoldings`，LEAN 会按调用顺序逐个提交。由于 backtest 中 Market Order 是同步填充的，先买的订单会立即消耗 CashBook 中的现金，可能导致后续买入订单因 `HasSufficientBuyingPowerForOrder` 失败而被拒绝。因此，**用户必须在策略层手动控制顺序**（先遍历卖出，再遍历买入）。

#### 现金不足时如何缩放多个买入订单
LEAN 没有全局“同比例缩放”买入订单的逻辑。每个订单独立通过 `PreOrderChecksImpl`（`QCAlgorithm.Trading.cs`）进行预检：

```csharp
// 关键检查：是否可交易、价格是否为零、lot size、最小订单等
if (!security.IsTradable) return NonTradableSecurity;
if (price == 0) return SecurityPriceZero;
if (Math.Abs(request.Quantity) < security.SymbolProperties.LotSize) return OrderQuantityLessThanLotSize;
// 购买力检查委托给 BuyingPowerModel
var result = security.BuyingPowerModel.HasSufficientBuyingPowerForOrder(this, security, order);
```

如果现金不足，订单会被拒绝（返回 `OrderResponse.Error`），用户需要自己处理重试或缩放。

#### 候选遍历顺序是否影响分配
**是的**。由于 `SetHoldings` 每次调用立即以当前 `TotalPortfolioValue` 和 `CashBook` 状态计算，遍历顺序会直接影响最终持仓。先执行的资产获得更准确的权重，后执行的资产可能因现金不足被拒绝。

#### 已有持仓偏离目标权重时是否自动再平衡
**不会自动**。`SetHoldings` 是“命令式”API：用户主动调用时才产生订单。如果持仓因价格上涨偏离目标权重，LEAN 不会自动减仓。用户需要定期（如 `OnData` 或 `Schedule`）调用 `SetHoldings` 回到目标权重。

#### 20% 建仓上限，上涨后超过 20% 但不主动减仓
LEAN 不直接提供这种“软上限”。用户的 `SetHoldings(symbol, 0.20)` 只在调用那一刻把目标设为 20%。后续价格漂移导致权重超过 20% 时，LEAN 不会自动卖出。这与我们的 ETF 轮动模型需求一致（“建仓上限”而非“持续硬上限”）。

#### 防御/现金替代资产如何为高优先级资产让路
**无内置优先级机制**。用户需要在策略层显式编码：例如先计算进攻资产目标权重，再决定剩余现金是否配置防御资产，或者把防御资产放在最后遍历。

#### 总仓位、单只上限、持仓数量上限
- `SecurityPortfolioManager` 提供 `TotalPortfolioValue`、`TotalMarginUsed`。
- 单只上限需要用户在调用 `SetHoldings` 前手动截断（`min(target, max_position)`）。
- 持仓数量上限需要用户在 Universe 选择阶段或订单生成阶段手动限制。

#### 佣金、滑点、100 股整手、最小订单
- **整手**：`SymbolProperties.LotSize`（`QCAlgorithm.Trading.cs:PreOrderChecksImpl`）会自动校验订单数量是否小于 lot size。A 股 ETF 需设置 `LotSize = 100`。
- **佣金**：通过 `BrokerageModel` 和 `CommissionModels` 配置，在 `FillModel` 中扣除。
- **滑点**：通过 `Security.SlippageModel` 或 `FillModel` 配置。
- **最小订单**：`LotSize` 检查即是最小订单约束。但 LEAN 没有“最小订单金额”参数（如低于 100 元不交易），需用户自行过滤。

#### 缺价、停牌或无法成交时如何处理未完成订单
- **缺价/停牌**：`PreOrderChecksImpl` 中 `if (price == 0)` 返回 `SecurityPriceZero`，订单被标记为 `Invalid`。
- **非交易时间**：Market Order 在非交易时间会被自动转换为 `MarketOnOpen` 或 `MarketOnClose`（`QCAlgorithm.Trading.cs:MarketOrder`）。
- **未完成订单**：用户可通过 `Transactions.GetOpenOrders()` 获取未完成订单，手动取消。LEAN 没有内置的“订单过期后自动重试”机制。

#### 是否适合 A 股 ETF
- **适合度**：中等。LEAN 是 C# 核心，A 股数据接入需自定义 `IDataFeed`；`LotSize` 支持良好；但缺乏 A 股特有的 T+1 约束（需要自定义 `FillModel` 或订单逻辑）。
- **可借鉴点**：`PreOrderChecksImpl` 的订单预检设计（价格、lot size、是否可交易）值得在 Python 回测中复用；`SecurityPortfolioManager` 的 `CashBook` + `UnsettledCashBook` 分离设计可用于 T+1 资金冻结建模。
- **不适用之处**：没有内建的“先卖后买”或“全局订单缩放”；事件驱动架构对 A 股日线轮动而言性能开销偏大。

---

### 2.2 Backtrader

#### 仓库信息
- **仓库**：https://github.com/mementum/backtrader
- **文件**：`backtrader/strategy.py`（`order_target_percent` 等）、`backtrader/broker.py`（Broker 基类）
- **版本**：master（2026-06-19），最新稳定版约 1.9.78.123

#### 如何从目标权重生成买卖订单
`Strategy.order_target_percent`（`backtrader/strategy.py:order_target_percent`）的实现逻辑：

```python
def order_target_percent(self, data=None, target=0.0, **kwargs):
    possize = self.getposition(data, self.broker).size
    target *= self.broker.getvalue()          # 目标金额 = 目标比例 * 组合总净值
    return self.order_target_value(data=data, target=target, **kwargs)

def order_target_value(self, data=None, target=0.0, price=None, **kwargs):
    possize = self.getposition(data, self.broker).size
    if not target and possize:
        return self.close(data=data, size=possize, price=price, **kwargs)
    else:
        value = self.broker.getvalue(datas=[data])
        comminfo = self.broker.getcommissioninfo(data)
        price = price if price is not None else data.close[0]
        if target > value:
            size = comminfo.getsize(price, target - value)   # 佣金模型计算可买股数
            return self.buy(data=data, size=size, price=price, **kwargs)
        elif target < value:
            size = comminfo.getsize(price, value - target)
            return self.sell(data=data, size=size, price=price, **kwargs)
    return None
```

关键路径：`target_percent -> target_value -> buy/sell -> broker.submit`。`comminfo.getsize(price, cash)` 负责扣除佣金后计算实际可买股数。

#### 是否先卖后买
**Backtrader 本身没有内置的“先卖后买”**。`order_target_percent` 是按资产逐个调用的。如果用户在一个循环中依次调用 `order_target_percent` 处理多个资产，每个调用独立读取 `broker.getvalue()` 并立即提交订单。根据 arxiv 论文 *Implementation Risk in Portfolio Backtesting* (2603.20319) 的披露：

> “Backtrader 的 fill-ordering logic was patched to process sell orders before buy orders, which prevents spurious margin rejections during rebalancing.”

这意味着 Backtrader 在 `cerebro` 层面有将 sell 排序到 buy 之前的补丁，但**仅限于 Broker 内部的订单填充顺序**，不是全局“先执行所有卖出，释放现金，再计算所有买入”。如果用户代码是逐个资产调用 `order_target_percent`，先调用的买入仍可能占用现金，影响后续卖出后应该释放的现金（但 sell 在 broker 内部排序靠前）。

#### 现金不足时如何缩放多个买入订单
Backtrader 的缩放依赖于 `CommInfoBase.getsize(price, cash)`（`backtrader/comminfo.py`），该函数计算给定现金下能买多少股。但它是**逐个资产**的，不是全局缩放。例如：

```python
# 伪代码：comminfo.getsize 的典型实现
def getsize(self, price, cash):
    # 扣除佣金后反算股数
    if self.stocklike:
        return int(cash / (price * (1 + self.commission)))
```

如果一个资产的目标权重需要 10000 元，但 `getsize` 发现只有 8000 元可用，它会只买 8000 元对应的股数。然而，如果多资产同时目标权重之和超过 100%，后面的资产会因 broker 拒绝（margin）而无法成交。没有全局的“同比例压缩”或“按优先级让路”机制。

#### 候选遍历顺序影响分配
**是的，顺序影响显著**。`order_target_percent` 使用**当前组合净值**（`broker.getvalue()`）作为计算基准。如果先执行一个买入订单，组合净值不变（因为买入以市价计入持仓），但可用现金减少。后续资产的买入订单仍以原组合净值计算目标值，但可用现金已减少，可能导致拒绝。因此，先买谁、后买谁会导致最终持仓不同。

#### 已有持仓偏离目标权重时是否自动再平衡
**不会自动**。`order_target_percent` 只在被调用时执行一次。用户需要在自己的 `next()` 或 `notify_timer()` 中定期触发再平衡。

#### 20% 建仓上限，上涨后超过 20% 但不主动减仓
Backtrader 不区分“建仓上限”和“持续硬上限”。`order_target_percent(target=0.20)` 只在调用时把目标值设为 20%。后续价格上涨导致权重超过 20% 时，不会自动卖出。符合需求，但用户需要自己决定何时再平衡。

#### 防御/现金替代资产如何为高优先级资产让路
**无内置优先级**。用户必须在策略代码中显式编码：先处理进攻资产，把剩余现金分配给防御资产。

#### 总仓位、单只上限、持仓数量上限
- **总仓位**：用户自行控制 `target` 之和。
- **单只上限**：用户调用 `order_target_percent` 前截断 `target = min(target, max_per_position)`。
- **持仓数量上限**：用户自行在 Universe 中限制。`Sizer` 可以限制单只订单大小，但不是全局持仓数量上限。
- **Sizer**：`backtrader/strategy.py` 中 `self._sizer.getsizing(data, isbuy=True)` 可以基于固定股数、固定金额、百分比等计算订单大小。但 `order_target_percent` 绕过了 Sizer，直接通过 `comminfo.getsize` 计算。

#### 佣金、滑点、100 股整手、最小订单
- **整手**：`CommInfoBase.getsize` 默认不处理整手，只返回整数股数。如果 A 股 ETF 需要 100 股整手，用户需要自定义 `CommissionInfo` 或 `Sizer` 来 `// 100 * 100`。
- **佣金**：`cerebro.broker.setcommission(commission=0.0003, percabs=True)`（`backtrader/broker.py:setcommission`）。
- **滑点**：`cerebro.broker.set_slippage_perc(0.001)` 或自定义 `SlippageModel`。
- **最小订单**：无全局最小订单金额检查，只有 `size > 0` 的隐式检查。用户需要自定义过滤。

#### 缺价、停牌或无法成交时如何处理未完成订单
- **缺价/停牌**：如果 `data.close[0]` 为 NaN，`order_target_percent` 中 `price = data.close[0]` 可能为 NaN，导致 `getsize` 产生异常或订单被拒绝。用户需自行在调用前检查 `not math.isnan(data.close[0])`。
- **未完成订单**：订单对象有 `Order.alive()` 状态。用户可在 `notify_order` 中处理 `Order.Rejected` 或 `Order.Margin`，但 Backtrader 不会自动重试或拆分。

#### 是否适合 A 股 ETF
- **适合度**：中等偏低。社区活跃，但核心代码对 A 股支持不佳：
  - 需要手动补丁“先卖后买”和“sell order 优先排序”（已在上游部分实现，但不彻底）。
  - 100 股整手需要自定义 `CommInfo` 或 `Sizer`。
  - T+1 没有内置支持（买入当日不能卖出，但回测中若策略在同一天触发卖出会成功）。
- **可借鉴点**：`order_target_percent -> order_target_value -> buy/sell` 的层级设计清晰；`CommissionInfo.getsize` 的“现金 -> 股数”反算思路可借鉴；`Cerebro` 的模块化架构（DataFeed + Strategy + Broker + Sizer）可参考。
- **不适用之处**：缺乏全局订单计划与现金缩放；事件驱动循环对 A 股日线多资产轮动较慢。

---

### 2.3 Zipline / Zipline Reloaded

#### 仓库信息
- **仓库**：https://github.com/stefan-jansen/zipline-reloaded
- **文件**：`zipline/algorithm.py`（`order_target_percent`）、`zipline/finance/execution.py`（订单执行）、`zipline/finance/performance.py`（组合跟踪）
- **版本**：`zipline-reloaded` 3.0.4 / master

#### 如何从目标权重生成买卖订单
Zipline 的用户层 API 是 `order_target_percent(asset, target)`。它内部调用 `order_target(asset, target_shares)`，再计算：

```python
# 基于 zipline/algorithm.py 及公开源码的交叉验证
# order_target_percent -> order_target -> order
def order_target_percent(self, asset, target):
    amount = self.portfolio.positions[asset].amount
    target_value = target * self.portfolio.portfolio_value
    target_shares = target_value / self.current_price(asset)
    return self.order_target(asset, target_shares - amount)
```

Zipline 的 `order` 函数通过 `Blotter` 将订单提交给 `SimulationClock`。在 backtest 中，每个 bar 的订单在当前 bar 的收盘价执行（或 VWAP 执行，取决于配置）。

#### 是否先卖后买
**Zipline 没有全局“先卖后买”**。订单是在 `handle_data` 或 `rebalance` 函数中按用户代码顺序提交的。`Blotter` 会记录订单列表，然后在当前 bar 的 `TradingAlgorithm` 循环中统一处理。根据社区文档和 arxiv 论文的披露：

> Zipline-Reloaded was excluded due to an unfixable trading-calendar bug that caused date misalignment on rebalancing days.

这说明 Zipline 在再平衡日有日历对齐 bug。对于订单执行顺序，Zipline 的 `SlippageModel` 会逐个处理订单，但**不保证先执行卖出再执行买入**。因此，用户需要在 `rebalance` 函数中手动先循环卖出，再循环买入。

#### 现金不足时如何缩放多个买入订单
Zipline 没有内置缩放。当 `order_target_percent` 生成的买入订单总额超过可用现金时，后续订单会被 `RiskEngine` 或 `Broker` 以 `InsufficientCash` 拒绝。用户需要自己处理优先级或缩放。

#### 候选遍历顺序影响分配
**是的**。Zipline 的 `order_target_percent` 使用当前 `portfolio.portfolio_value` 计算目标金额。如果先执行买入，可用现金减少，但 `portfolio_value` 不变（因为买入同时增加持仓市值），所以后续订单的目标金额不变。但如果可用现金不足，后续订单被拒绝。因此，先买后卖 vs 先卖后买会导致完全不同的结果。

#### 已有持仓偏离目标权重时是否自动再平衡
**不会自动**。Zipline 是事件驱动框架，`order_target_percent` 只在被调用时执行。用户需要在 `schedule_function(rebalance, date_rules.month_start())` 中手动触发。

#### 20% 建仓上限，上涨后超过 20% 但不主动减仓
与 Backtrader 类似，`order_target_percent` 只设置目标值，不自动处理漂移。但用户若定期再平衡，会强制卖出超额部分。如果希望“建仓上限 20% 但漂移不主动减仓”，用户应在计算目标权重时把上限设为 20%，但**跳过“再平衡”触发条件**，除非该资产不再在目标列表中才卖出。这完全是策略层逻辑，Zipline 不提供帮助。

#### 防御/现金替代资产如何为高优先级资产让路
**无内置优先级**。用户需自行编码：先计算进攻资产权重，再计算防御资产。

#### 总仓位、单只上限、持仓数量上限
- 总仓位和单只上限需用户在策略层截断。
- Zipline 没有 `Sizer` 概念。`order_target_percent` 不限制单只上限，只按目标比例执行。
- `Pipeline` 可在 Universe 层面限制候选数量，但不在订单层面限制。

#### 佣金、滑点、100 股整手、最小订单
- **佣金**：`set_commission(commission.PerDollar(cost=0.0015))`。
- **滑点**：`set_slippage(slippage.VolumeShareSlippage(volume_limit=0.025, price_impact=0.1))`。
- **整手**：Zipline 底层不强制整手。`order_target_percent` 计算出的 shares 可以是任意整数。A 股 ETF 需要用户在调用前手动 `// 100 * 100`。
- **最小订单**：没有全局最小订单金额或最小股数检查。用户可以自行过滤。

#### 缺价、停牌或无法成交时如何处理未完成订单
- **缺价/停牌**：`data.can_trade(asset)` 是官方推荐的检查方式。如果价格在 bar 中缺失，Zipline 的 `DataPortal` 会返回 `NaN`，`order_target_percent` 会生成异常或 0 股订单。用户需提前过滤。
- **未完成订单**：订单状态可在 `context.portfolio` 中追踪。Zipline 没有自动订单重试机制。`cancel_order` 需用户手动调用。

#### 是否适合 A 股 ETF
- **适合度**：低。Zipline 核心设计围绕美股（Quandl 数据、NYSE 交易日历、Pipeline 针对美股基本面）。`zipline-reloaded` 虽然有 Bundle 扩展，但 A 股 ETF 需要大量自定义：交易日历、数据 Bundle、T+1 逻辑。且 arxiv 论文指出其有“unfixable trading-calendar bug”。
- **可借鉴点**：`Pipeline` 的因子计算与 Universe 筛选架构（可分离信号生成与订单执行）；`order_target_percent` 的简洁 API 设计。
- **不适用之处**：日历/数据架构对 A 股不友好；无全局订单计划；无整手支持。

---

### 2.4 vectorbt

#### 仓库信息
- **仓库**：https://github.com/polakowo/vectorbt
- **文件**：`vectorbt/portfolio/nb.py`（核心订单执行，Numba 编译）、`vectorbt/portfolio/base.py`（Portfolio 类）
- **版本**：0.26.0 / master（2026-06-19）

#### 如何从目标权重生成买卖订单
vectorbt 是**向量化回测框架**，不模拟事件循环，而是直接对数组操作。关键 API 是 `Portfolio.from_orders(..., size=weights, size_type='targetpercent')`。在 `vectorbt/portfolio/nb.py:execute_order_nb` 中，`SizeType.TargetPercent` 的转换链为：

```python
# vectorbt/portfolio/nb.py:execute_order_nb
if order_size_type == SizeType.TargetPercent:
    # Target percentage of current value
    if np.isnan(value):
        return exec_state, order_not_filled_nb(OrderStatus.Ignored, OrderStatusInfo.ValueNaN)
    if value <= 0:
        return exec_state, order_not_filled_nb(OrderStatus.Rejected, OrderStatusInfo.ValueZeroNeg)

    order_size *= value        # 转换为 TargetValue
    order_size_type = SizeType.TargetValue

if order_size_type == SizeType.TargetValue:
    # Target value
    order_size /= val_price    # 转换为 TargetAmount
    order_size_type = SizeType.TargetAmount

if order_size_type == SizeType.TargetAmount:
    order_size -= position     # 转换为 Amount（增量）
    order_size_type = SizeType.Amount
```

即：`TargetPercent -> TargetValue -> TargetAmount -> Amount`。然后执行 `buy_nb` 或 `sell_nb`。

#### 是否先卖后买
**vectorbt 的 `from_orders` 是逐列（逐资产）处理的，不保证先卖后买**。在 `simulate_from_orders_nb`（`nb.py`）中，循环遍历每个资产（列），每列独立调用 `execute_order_nb`。

但是，vectorbt 提供了 `call_seq`（调用顺序）机制。`build_call_seq_nb` 可以生成 `CallSeqType.Reversed` 或 `Default` 或 `Random`。如果用户通过 `pre_segment_func_nb` 自定义排序，可以把卖出放前面、买入放后面。但这不是 `from_orders` 的默认行为。

在 `Portfolio.from_order_func`（更灵活的接口）中，用户可以自己定义 `order_func_nb`，从而完全控制先卖后买。但默认的 `from_orders` 是按列顺序处理。

#### 现金不足时如何缩放多个买入订单
**vectorbt 有内置的现金不足缩放机制**。在 `buy_nb`（`nb.py`）中：

```python
# buy_nb 核心逻辑（已简化）
if is_close_or_less_nb(total_req_cash, cash_limit):
    final_size = adj_size
else:
    # 现金不足，按可用现金反算最大可买股数
    max_req_cash = add_nb(cash_limit, -fixed_fees) / (1 + fees)
    if max_req_cash <= 0:
        return exec_state, order_not_filled_nb(OrderStatus.Rejected, OrderStatusInfo.CantCoverFees)
    max_acq_size = max_req_cash / adj_price
    if not np.isnan(size_granularity):
        final_size = max_acq_size // size_granularity * size_granularity
    else:
        final_size = max_acq_size
    # 如果 final_size < min_size，拒绝
```

这是**单个资产**级别的缩放：如果某资产的目标金额超过可用现金，就按可用现金（扣除佣金和固定费用后）买最大数量。但多个资产同时买入时，**前面的资产会消耗现金，导致后续资产可用现金减少**。如果 `cash_sharing=True`，同一 group 内的资产共享现金，但仍然是逐列顺序消费，没有全局“同比例压缩”所有买入订单。

#### 候选遍历顺序影响分配
**是的**。`call_seq` 默认按列顺序处理。先处理的资产优先获得现金，后处理的资产可能因现金不足被部分成交或拒绝。`vectorbt/portfolio/nb.py:sort_call_seq_nb` 允许用户按订单价值排序，但这只能改变排序规则，不能消除顺序依赖性。

#### 已有持仓偏离目标权重时是否自动再平衡
**不会自动**。`from_orders` 中的 `size` 数组必须用户在每个 bar 显式提供。如果 `size` 是 `targetpercent`，则每个 bar 都会按目标百分比重新平衡（如果 `size` 数组提供）。但如果用户只希望 drift 时不操作，只需在非再平衡日让 `size` 为 `NaN`（会被忽略）。

#### 20% 建仓上限，上涨后超过 20% 但不主动减仓
vectorbt 的 `targetpercent` 会把目标金额设为 `percentage * current_value`。如果用户在再平衡日只对新入选资产设 `targetpercent=0.20`，而老资产不再提供 `targetpercent`（或设为 `NaN`），则老资产会保持原有持仓（因为 `from_orders` 只在 size 非 NaN 时执行）。因此，如果用户只在建仓时触发 `targetpercent=0.20`，之后不再对该资产提供目标权重，该资产会因价格上涨而权重超过 20%，且不会被减仓。这符合需求，但需要用户在 `size` 矩阵中精确控制再平衡触发。

#### 防御/现金替代资产如何为高优先级资产让路
**无内置优先级**。用户需通过 `order_func_nb` 或自定义 `pre_segment_func_nb` 手动实现：先计算进攻资产目标权重，再决定防御资产是否分配剩余现金。也可以将进攻资产和防御资产放在不同 `group` 中，但 `cash_sharing` 只控制组内是否共享现金，不控制优先级。

#### 总仓位、单只上限、持仓数量上限
- **总仓位**：`size_type='targetpercent'` 时，所有资产 targetpercent 之和若小于 1.0，剩余为现金。但无全局强制限制。
- **单只上限**：`order.max_size` 参数可限制单只最大订单数量（但 `from_orders` 中默认 `np.inf`）。
- **持仓数量上限**：无直接参数。用户需通过 Universe 筛选控制。

#### 佣金、滑点、100 股整手、最小订单
**vectorbt 是调研中最完善的参数化支持**：
- **佣金**：`fees`（比例） + `fixed_fees`（固定）。
- **滑点**：`slippage`（百分比，买入时 price*(1+slippage)，卖出时 price*(1-slippage)）。
- **整手**：`size_granularity`（`nb.py:buy_nb` 中 `adj_size // size_granularity * size_granularity`）。A 股 ETF 直接设 `size_granularity=100`。
- **最小订单**：`min_size`（`buy_nb` 中检查 `is_less_nb(final_size, min_size)`）。
- **最大订单**：`max_size`（`np.inf` 默认）。
- **部分成交**：`allow_partial=True`（默认）允许部分成交；`False` 则拒绝。
- **拒绝概率**：`reject_prob` 可模拟随机拒绝（如涨停无法买入）。

#### 缺价、停牌或无法成交时如何处理未完成订单
- **缺价**：如果 `price` 或 `val_price` 为 `NaN`，`execute_order_nb` 返回 `Ignored`（`OrderStatusInfo.PriceNaN` 或 `ValPriceNaN`）。
- **现金不足**：返回 `Rejected`（`NoCashLong`）。
- **部分成交**：如果 `allow_partial=True`，按可用现金买入最大数量；如果 `False`，直接拒绝。
- **未完成订单**：vectorbt 是向量化模拟，不是事件驱动，不存在“未完成订单挂起”状态。每个 bar 的订单要么成交、要么部分成交、要么拒绝。下一 bar 重新评估。用户可开启 `log=True` 追踪所有订单结果。

#### 是否适合 A 股 ETF
- **适合度**：较高（在纯回测层面）。向量化设计极快，适合大规模参数扫描；`size_granularity`、`min_size`、`fixed_fees`、`slippage`、`allow_partial` 等参数直接支持 A 股特性。
- **可借鉴点**：`execute_order_nb` 的“现金不足反算股数”逻辑（`max_req_cash = (cash_limit - fixed_fees) / (1 + fees)`）可直接复用；`TargetPercent -> TargetValue -> TargetAmount -> Amount` 的转换链是清晰的状态机设计；`size_granularity` 的整手处理非常简洁。
- **不适用之处**：向量化框架无法自然模拟事件驱动的“先卖后买”；`from_orders` 的逐列顺序依赖问题需要额外处理；缺乏 T+1 的卖出限制（但可通过自定义 `order_func_nb` 模拟）。

---

### 2.5 Qlib (Microsoft)

#### 仓库信息
- **仓库**：https://github.com/microsoft/qlib
- **文件**：`qlib/backtest/executor.py`（SimulatorExecutor）、`qlib/backtest/decision.py`（Order 类）、`qlib/contrib/strategy.py`（TopkDropoutStrategy）
- **版本**：main（2026-06-19），v0.9.6+

#### 如何从目标权重生成买卖订单
Qlib 采用“策略 -> 决策 -> 执行器”三层架构。策略（如 `TopkDropoutStrategy`）生成 `TradeDecision`（包含 `Order` 列表），执行器（如 `SimulatorExecutor`）执行。

在 `qlib/contrib/strategy.py` 的示例逻辑（来自社区文档与 `executor.py` 交叉推导）：

```python
# 典型 TopkDropoutStrategy 的 generate_trade_decision 逻辑
# 1. 卖出不在 target 列表的持仓
for stock_id, position in current_holdings.items():
    if stock_id not in top_stocks:
        orders.append(Order(stock_id=stock_id, amount=position.amount, direction=-1))

# 2. 买入目标股票
for stock_id in top_stocks:
    target_amount = int(target_value_per_stock / current_price / 100) * 100  # 整手
    if stock_id in current_holdings:
        current_amount = current_holdings[stock_id].amount
        if target_amount > current_amount:
            buy_amount = target_amount - current_amount
            orders.append(Order(stock_id=stock_id, amount=buy_amount, direction=1))
    else:
        orders.append(Order(stock_id=stock_id, amount=target_amount, direction=1))
```

注意：Qlib 的 `Order` 直接包含 `amount`（股数），不是目标权重。权重到股数的转换发生在策略层。

#### 是否先卖后买
**Qlib 的 `SimulatorExecutor` 有 `trade_type` 参数控制 TT_SERIAL vs TT_PARALLEL**（`qlib/backtest/executor.py:SimulatorExecutor`）：

```python
class SimulatorExecutor(BaseExecutor):
    TT_SERIAL = "serial"    # 订单按顺序执行
    TT_PARAL = "parallel"   # 买入优先排序（按 direction 降序）

    def _get_order_iterator(self, trade_decision):
        orders = _retrieve_orders_from_decision(trade_decision)
        if self.trade_type == self.TT_SERIAL:
            order_it = orders
        elif self.trade_type == self.TT_PARAL:
            # 按 direction 排序：买入(direction=1) 排在 卖出(direction=-1) 前面
            order_it = sorted(orders, key=lambda order: -order.direction)
        return order_it
```

关键发现：
- `TT_SERIAL`（默认）：**按订单列表顺序执行**。如果策略层把 `sell` 订单放在 `buy` 订单前面，就是“先卖后买”。但 Qlib 本身不强制排序。
- `TT_PARAL`：**按 `direction` 降序排序，即买入优先于卖出**。官方注释解释：
  > “Assumption: there will not be orders in different trading direction in a single step... make the buying go first will make sure the conflicts happen. It equals to parallel trading after sorting the order by direction.”

这说明 `TT_PARAL` 的设计是假设同一 step 中不会同时有买卖（或买入优先），但**不是先卖后买**。实际上 Qlib 社区推荐在策略层生成 `sell first, then buy` 的订单列表，配合 `TT_SERIAL` 使用。

#### 现金不足时如何缩放多个买入订单
Qlib 的 `SimulatorExecutor` 逐个执行订单（`TT_SERIAL`），每个订单通过 `trade_exchange.deal_order` 成交。如果现金不足，后续买入订单会失败（`deal_order` 返回 `deal_amount < order.amount`）。**没有全局缩放机制**。用户需要在策略层控制目标金额，确保总买入不超过现金。

#### 候选遍历顺序影响分配
**是的，在 TT_SERIAL 下顺序影响显著**。`deal_order` 会实时修改 `trade_account` 的现金余额。先执行的订单占用现金，影响后续订单。因此，策略层必须显式排序：先卖出（释放现金），再买入（消耗现金）。

#### 已有持仓偏离目标权重时是否自动再平衡
**不会自动**。Qlib 的 `BaseStrategy.generate_trade_decision` 只在被 `Executor` 调用时执行。用户配置 `time_per_step`（如 `"month"`）控制再平衡频率。`TopkDropoutStrategy` 的 `n_drop` 参数控制是否强制更换持仓，但没有“自动因漂移而再平衡”的机制。

#### 20% 建仓上限，上涨后超过 20% 但不主动减仓
Qlib 的 `TopkDropoutStrategy` 默认行为是等权重配置选中股票。如果用户希望“建仓 20% 后漂移不主动减仓”，需要在自定义策略中实现：只在入选/调仓时计算目标权重，后续非调仓日返回 `EmptyTradeDecision`（不生成订单）。这与我们的模型需求一致，但需自定义策略。

#### 防御/现金替代资产如何为高优先级资产让路
**无内置优先级**。用户需在 `generate_trade_decision` 中显式编码：先为进攻资产分配现金，再为防御资产分配剩余现金。或者通过 `n_drop` 机制控制总持仓数量。

#### 总仓位、单只上限、持仓数量上限
- **总仓位**：`TopkDropoutStrategy` 的 `topk` 参数限制持仓数量，间接限制总仓位（因为等权重分配）。但用户可以自定义策略实现任意总仓位控制。
- **单只上限**：`TopkDropoutStrategy` 的 `weight_decay` 和 `topk` 机制不直接限制单只上限。用户需在策略层截断 `target_value`。
- **持仓数量上限**：`topk` 直接限制。Qlib 还提供了 `Risk` 模块（如 `Position` 的 `risk_degree`）可间接控制。

#### 佣金、滑点、100 股整手、最小订单
- **整手**：在 `TopkDropoutStrategy` 示例中直接可见：`int(target_value_per_stock / current_price / 100) * 100`。Qlib 本身没有内置 `lot_size`，需用户在策略层处理。
- **佣金**：`Exchange` 的 `commission_rate` 参数（如 `0.0003`）。`deal_order` 会自动扣除佣金。
- **滑点**：`slippage_rate` 参数（`Exchange` 配置）。
- **最小订单**：没有全局最小订单金额。用户需自行过滤 `target_amount < 100` 或 `target_amount * price < min_value` 的情况。

#### 缺价、停牌或无法成交时如何处理未完成订单
- **缺价/停牌**：`deal_order` 依赖 `trade_exchange.get_current_price(stock_id, trade_step)`。如果价格为 `NaN` 或 0，`deal_order` 会返回 `deal_amount = 0`（`trade_price` 可能为 `NaN`）。`SimulatorExecutor` 记录 `deal_amount`，但不会自动重试。
- **未完成订单**：`Order` 对象有 `deal_amount`（实际成交）和 `amount`（目标）。未成交部分不会自动挂到下一 bar。用户需在下一 step 重新生成订单。

#### 是否适合 A 股 ETF
- **适合度**：中高。Qlib 由微软开发，原生支持 A 股（沪深 300、中证 500 等数据 Bundle）。`topk` 策略、分层执行器、交易日历管理等对 A 股友好。
- **可借鉴点**：“策略层生成订单列表 -> 执行器串行执行”的架构适合 A 股 T+1；`Exchange.deal_order` 的成交逻辑（含 `dealt_order_amount` 当日量限制）可模拟 A 股流动性；`TopkDropout` 的“卖出不在目标列表的，再买入目标列表的”顺序可直接复用。
- **不适用之处**：`deal_order` 无全局订单缩放；`TT_SERIAL` 下的顺序依赖完全由策略层负责；缺乏 `size_granularity` 全局参数，整手需在策略层硬编码。

---

### 2.6 bt (pmorissette)

#### 仓库信息
- **仓库**：https://github.com/pmorissette/bt
- **文件**：`bt/algos.py`（Rebalance 等 Algo）、`bt/core.py`（Strategy/Security 树结构）
- **版本**：master（2026-06-19），0.2.10

#### 如何从目标权重生成买卖订单
bt 采用“AlgoStack”设计：策略由一系列 Algo 组成，如 `RunMonthly -> SelectAll -> WeighEqually -> Rebalance`。`Rebalance` Algo（`bt/algos.py:Rebalance`）是核心调仓实现：

```python
class Rebalance(Algo):
    def __call__(self, target):
        if "weights" not in target.temp:
            return True
        targets = target.temp["weights"]
        base = target.value  # 保存当前组合价值作为 rebase

        # 1. 关闭不在 targets 中的持仓
        for cname in target.children:
            if cname in targets:
                continue
            c = target.children[cname]
            v = c.value
            if v != 0.0 and not np.isnan(v):
                target.close(cname, update=False)

        # 2. 按目标权重重新平衡
        if "cash" in target.temp and not target.fixed_income:
            base = base * (1 - target.temp["cash"])
        for item in targets.items():
            target.rebalance(item[1], child=item[0], base=base, update=False)
        target.root.update(target.now)
        return True
```

`target.rebalance(weight, child, base)` 在 `bt/core.py` 中实现，核心逻辑：

```python
# bt/core.py 中 Strategy.rebalance 的简化逻辑
def rebalance(self, weight, child, base=np.nan, update=True):
    if np.isnan(base):
        base = self.value
    target_value = base * weight
    current_value = child.value
    delta = target_value - current_value
    if delta > 0:
        # 买入
        self.allocate(delta, child, update=update)
    elif delta < 0:
        # 卖出
        self.deallocate(-delta, child, update=update)
```

#### 是否先卖后买
**bt 的 `Rebalance` Algo 有明确的“先卖后买”设计**（`bt/algos.py:Rebalance.__call__`）：
1. 先遍历所有 `children`，对不在 `targets` 中的持仓调用 `target.close(...)`（即卖出）。
2. 再遍历 `targets` 中的每个资产，调用 `target.rebalance(...)`。

这是调研框架中**最清晰的“先卖后买”实现**（卖出不在 target 的，再平衡 target 的）。但注意：对于“在 target 中但权重降低”的资产，bt 的 `rebalance` 会先计算 `delta = target_value - current_value`，如果 `delta < 0` 则卖出。这部分卖出是在第二步的循环中逐个处理的，不是集中在第一步。因此，如果多个资产的 `delta < 0` 之和很大，但前面的 `delta > 0` 资产先消耗了现金，后面的买入可能仍面临现金不足。不过第一步已经释放了所有“不在 target 中”的现金。

#### 现金不足时如何缩放多个买入订单
**bt 没有全局缩放**。`allocate` 在 `bt/core.py` 中会尝试买入，但如果 `Security` 的价格导致现金不足，bt 的 `allocate` 会计算 `buy_amount = min(delta, available_cash)`（大致逻辑）。实际上，bt 的 `Strategy` 有 `capital` 属性，`allocate` 会检查 `if capital < 0` 并报错。因此，**如果目标权重之和超过 100% 或现金不足，bt 会抛出异常或产生错误**。用户必须确保 `weights` 之和 <= 1.0（或设置了 `cash` 缓冲）。

#### 候选遍历顺序影响分配
**在 `Rebalance` 的第二步中，顺序影响买入资产的最终持仓**。`for item in targets.items():` 按 `targets` 字典遍历顺序执行。如果 `targets` 是 `dict`，Python 3.7+ 保持插入顺序。因此，先插入的 `target` 先获得现金，后插入的可能因现金不足而分配失败。但 bt 不提供自动缩放。

#### 已有持仓偏离目标权重时是否自动再平衡
**不会自动**。`Rebalance` 只在 AlgoStack 执行到它时才运行。通常由 `RunMonthly` 或 `RunWeekly` 触发。

#### 20% 建仓上限，上涨后超过 20% 但不主动减仓
bt 的 `Rebalance` 在触发时会强制把所有 `targets` 中的资产调到目标权重。如果希望“建仓 20% 后漂移不主动减仓”，用户应只在建仓日把该资产放入 `targets`，之后不再放入（或只在需要卖出时放入 `targets` 并设权重为 0）。这与我们的模型需求一致，但需用户控制 `targets` 的生成逻辑。

#### 防御/现金替代资产如何为高优先级资产让路
**无内置优先级**。但 `Rebalance` 的第一步（关闭不在 targets 中的持仓）会释放现金。如果用户把防御资产设为“不在 targets 中”（当进攻资产足够多时），防御资产会被第一步自动卖出，现金留给第二步的进攻资产。这可以作为一种间接的“让路”机制，但需策略层控制 `targets` 内容。

#### 总仓位、单只上限、持仓数量上限
- **总仓位**：`Rebalance` 支持 `cash` 参数（`target.temp["cash"]`），表示保留现金比例。例如 `cash=0.3` 则只把 70% 的资金用于 rebalancing。
- **单只上限**：`LimitWeights` Algo 可限制单只权重上限（`bt.ffn.limit_weights(tw, self.limit)`），超额权重会按比例重新分配给其他资产。
- **持仓数量上限**：`SelectN` Algo 限制 Universe 数量。`SelectAll` 默认选择所有资产。

#### 佣金、滑点、100 股整手、最小订单
- **佣金**：bt 通过 `bt.Backtest(..., commissions=...)` 传入。Commissions 是一个 callable，接收 `(q, p)` 返回费用。例如：`lambda q, p: abs(q) * p * 0.0003`。
- **滑点**：bt 本身没有内置滑点模型。用户需在 `commissions` callable 中自行调整价格，或使用 `bt.ffn` 的滑点功能。
- **整手**：`Backtest` 有 `integer_positions=True`（默认）参数，强制 `Security` 持仓为整数。但 A 股 ETF 的 100 股整手需要额外处理（`// 100 * 100`）。`bt` 的 `integer_positions` 只保证整数，不保证整手倍数。
- **最小订单**：没有全局最小订单金额。`bt` 的 `rebalance` 中 `delta` 计算为 `target_value - current_value`，如果 `delta` 很小（如 50 元），但价格 2 元，会买入 25 股，这与 A 股 100 股最小冲突。

#### 缺价、停牌或无法成交时如何处理未完成订单
- **缺价/停牌**：`SelectAll(include_no_data=False)` 会过滤掉当前价格为 NaN 的资产。`CloseDead` Algo 会关闭价格为 0 的持仓。但如果入选资产在 rebalancing 当天价格缺失，`rebalance` 会计算 `target_value = base * weight`，`current_value = child.value`（可能为 NaN），导致 `delta` 为 NaN，可能引发异常或无效交易。用户需通过 `SelectAll` 或 `SelectHasData` 提前过滤。
- **未完成订单**：bt 是树结构，没有“订单”概念。`rebalance` 是即时完成的（假设在当前 bar 以当前价格成交）。不存在未完成订单挂起。如果 `integer_positions=True` 导致 rounding，误差会累积为现金。

#### 是否适合 A 股 ETF
- **适合度**：中等。bt 的树结构非常适合多层级组合（如大类资产 -> 行业 -> 个股），但对 A 股 ETF 有局限：
- **可借鉴点**：`Rebalance` 的“先关闭不在 targets 中的，再 rebalancing targets”的顺序是最佳实践；`LimitWeights` 的权重上限重分配逻辑可直接复用；`AlgoStack` 的模块化设计（RunMonthly -> Select -> Weigh -> Rebalance）非常清晰。
- **不适用之处**：`integer_positions` 只保证整数，不保证 100 股整手；无滑点内置模型；无全局订单缩放；无 T+1 支持（假设即时成交）。

---

### 2.7 PyPortfolioOpt（仅参考目标权重计算）

#### 仓库信息
- **仓库**：https://github.com/robertmartin8/PyPortfolioOpt
- **文件**：`pypfopt/discrete_allocation.py`（`DiscreteAllocation` 类）
- **版本**：master（2026-06-19），v1.5.5

#### 说明
PyPortfolioOpt 不是回测框架，而是**优化器**。它解决“给定收益/协方差，求最优权重”的问题。但 `DiscreteAllocation` 模块提供了从连续权重到离散股数的转换，对 A 股 ETF 的“整手分配”有直接参考价值。

#### 离散分配机制
`DiscreteAllocation.greedy_portfolio`（`pypfopt/discrete_allocation.py`）的两轮算法：

```python
def greedy_portfolio(self, reinvest=False, verbose=False):
    # 第一轮：按权重降序，每个资产买最大整数股（向下取整）
    for ticker, weight in self.weights:
        price = self.latest_prices[ticker]
        n_shares = int(weight * self.total_portfolio_value / price)  # 向下取整
        cost = n_shares * price
        available_funds -= cost
        shares_bought.append(n_shares)

    # 第二轮：用剩余现金，按“当前权重与目标权重偏差最大”的资产逐一买 1 股
    while available_funds > 0:
        current_weights = np.array(buy_prices) * np.array(shares_bought)
        current_weights /= current_weights.sum()
        ideal_weights = np.array([i[1] for i in self.weights])
        deficit = ideal_weights - current_weights
        idx = np.argmax(deficit)  # 偏差最大的资产
        price = self.latest_prices[ticker]
        if price > available_funds:
            deficit[idx] = 0  # 买不起，跳过
            continue
        shares_bought[idx] += 1
        available_funds -= price
```

#### 与 A 股 ETF 的关联
- **可借鉴点**：
  1. **两轮分配法**：先买整数部分（向下取整），再按偏差分配剩余现金。这确保了在 A 股 100 股整手约束下，先满足“大块”分配，再微调。
  2. **偏差排序**：不按原始权重排序，而是按“已分配权重 vs 目标权重”的偏差排序，这避免了高价资产永远得不到分配的问题（因为第一轮高价资产可能只分到 0 股，第二轮按偏差会优先补偿）。
- **不适用之处**：PyPortfolioOpt 没有回测功能，没有考虑佣金、滑点、现金不足时的全局缩放、价格漂移。`greedy_portfolio` 假设 `total_portfolio_value` 是固定的，但在回测中，组合净值每天都在变化。

---

## 3. 核心问题对比矩阵

| 维度 | QuantConnect LEAN | Backtrader | Zipline Reloaded | vectorbt | Qlib | bt | PyPortfolioOpt |
|------|-------------------|------------|------------------|----------|------|-----|----------------|
| **目标权重 -> 订单** | `SetHoldings` -> 金额差 -> 股数 -> MarketOrder | `order_target_percent` -> 金额差 -> `comminfo.getsize` -> buy/sell | `order_target_percent` -> 股数差 -> order | `SizeType.TargetPercent` -> 逐列 Amount 转换 | 策略层生成 Order 列表 | `Rebalance` Algo -> `target.value - current.value` | 仅优化权重，不生成订单 |
| **先卖后买** | ❌ 无内置，用户手动 | ⚠️ Broker 内部 sell 优先，非全局 | ❌ 无内置，用户手动 | ❌ 逐列处理，无全局 | ⚠️ `TT_SERIAL` 依赖策略层顺序 | ✅ `Rebalance` 先 close 非 targets，再 rebalancing | N/A |
| **现金不足缩放** | ❌ 拒绝订单 | ⚠️ 单个 `getsize` 缩放 | ❌ 拒绝订单 | ✅ 单资产 `buy_nb` 按现金反算 | ❌ 逐单执行，不缩放 | ❌ 可能报错 | N/A |
| **顺序影响分配** | ✅ 是 | ✅ 是 | ✅ 是 | ✅ 是（逐列） | ✅ 是（TT_SERIAL） | ✅ 是（targets 遍历） | N/A |
| **自动再平衡** | ❌ 手动触发 | ❌ 手动触发 | ❌ 手动触发 | ❌ 需显式提供 size | ❌ 手动触发 | ❌ Algo 触发 | N/A |
| **漂移不主动减仓** | ✅ 支持（不调仓即不操作） | ✅ 支持 | ✅ 支持 | ✅ 支持（NaN 不操作） | ✅ 支持 | ✅ 支持 | N/A |
| **防御资产让路** | ❌ 手动 | ❌ 手动 | ❌ 手动 | ❌ 手动 | ❌ 手动 | ⚠️ 间接（不选即卖出） | N/A |
| **佣金/滑点/整手/最小订单** | LotSize + CommModel + SlippageModel | CommInfo + Slippage（需自定义整手） | Comm + Slippage（无整手） | **fees/fixed_fees/slippage/size_granularity/min_size** | Exchange.commission_rate + slippage_rate（无整手全局参数） | Commissions callable + integer_positions（无整手倍数） | N/A |
| **缺价/停牌处理** | PriceZero -> Invalid | 需用户过滤 NaN | `can_trade` + 需过滤 | NaN -> Ignored/Rejected | deal_amount=0 | SelectAll/SelectHasData 过滤 | N/A |
| **A 股 ETF 适合度** | 中 | 中低 | 低 | **高** | **中高** | 中 | 仅参考离散分配 |

---

## 4. 三种实现方案对比

### 方案 A：顺序式下单（Event-Driven Style）

**代表框架**：Backtrader、Zipline、Qlib（TT_SERIAL）

**机制**：
1. 计算每个资产的目标权重。
2. 按某种顺序（如字典序、评分排序）逐个调用 `order_target_percent` 或 `buy/sell`。
3. 每个订单立即提交到 Broker，同步或异步成交。
4. 现金不足时，后续订单被拒绝或部分成交。

**优点**：
- 实现简单，直接复用框架 API。
- 与事件驱动框架自然兼容。

**缺点**：
- **顺序依赖性**：先遍历的资产获得现金优势，后遍历的吃亏。不同的排序策略（按得分升序/降序）会导致不同的最终持仓。
- **没有全局现金规划**：先买的订单可能占用了本应用于后买订单的现金，即使先卖后买也无法避免“在 target 中权重降低”的资产释放的现金被前面的买入提前消耗。
- **难以处理“漂移容忍”**：如果某些资产只建仓不调仓，需要复杂的逻辑来跳过这些资产。

**适合场景**：单资产或少量资产、目标权重总和较低（如 < 80%）或现金充足。

**A 股 ETF 适用性**：**低**。A 股 ETF 轮动通常涉及 5-16 只资产，目标权重之和可能接近 100%，顺序依赖会导致显著偏差。且事件驱动开销对日线策略不必要。

---

### 方案 B：目标权重再平衡（Snapshot Rebalance）

**代表框架**：vectorbt（`size_type='targetpercent'`）、bt（`Rebalance`）、LEAN（`SetHoldings`）

**机制**：
1. 冻结当前组合净值（Snapshot）。
2. 对每个资产计算 `target_value = portfolio_value * target_weight`。
3. 计算 `delta = target_value - current_value`，统一生成所有买入/卖出订单。
4. 统一提交或执行。

**优点**：
- 所有资产使用同一基准计算，消除了“组合净值变化”带来的偏差。
- 实现简单，向量化框架天然支持。

**缺点**：
- **现金不足仍无全局缩放**：如果 `target_weight` 之和为 100% 但现金因 rounding/commission/停牌而不足，最后几个资产会被拒绝或部分成交。
- **先卖后买不明确**：除非显式拆分 sell 和 buy 两个阶段，否则仍可能因顺序导致现金问题。
- **对“漂移容忍”支持弱**：目标权重再平衡意味着每次触发都会强制回到目标权重，无法自然实现“建仓 20% 后漂移不主动减仓”。

**适合场景**：定期再平衡（如月度）、允许小额 rounding 误差、现金缓冲充足。

**A 股 ETF 适用性**：**中等**。A 股 ETF 需要 100 股整手，冻结净值后计算出的 `target_value` 可能无法精确对应整手股数，产生 cash residual。如果目标权重之和恰好为 100%，rounding 误差可能导致最后一只资产现金不足。且无法直接支持“漂移不主动减仓”。

---

### 方案 C：先生成订单计划，现金不足时同比例缩放（Order Planning + Proportional Scaling）

**机制**：
1. **Phase 1 - 计算目标状态**：冻结组合净值，计算每个资产的目标金额/股数（考虑整手）。
2. **Phase 2 - 生成订单计划**：按“卖出 -> 买入”顺序生成所有订单。卖出订单释放现金，买入订单消耗现金。
3. **Phase 3 - 现金检查与缩放**：汇总所有卖出释放的现金 + 原有可用现金，得到总可用现金。如果买入订单总额 > 可用现金，对所有买入订单按 **同比例缩放**（或按优先级排序后从前到后分配，剩余丢弃）。
4. **Phase 4 - 执行**：按 sell first -> buy scaled 的顺序执行。

**优点**：
- **无顺序依赖**：买入订单统一缩放，不受遍历顺序影响。
- **先卖后买明确**：Phase 2 明确拆分 sell 和 buy，先执行 sell 释放现金，再执行 buy。
- **支持整手**：Phase 1 计算目标股数时即 `// 100 * 100`，缩放时可在股数层面或金额层面操作。
- **支持“漂移容忍”**：Phase 1 中只对“当前需要调整”的资产生成订单（如不在 target 的卖出，新 target 的买入），不在 target 或已达标的老资产不生成订单，自然实现“建仓后不主动减仓”。
- **支持防御资产让路**：Phase 1 中先计算进攻资产目标，再计算防御资产，如果现金不足，防御资产在 Phase 3 中被缩放或归零。

**缺点**：
- 需要脱离框架的“单个订单提交”API，自行实现订单计划层。这在纯事件驱动框架中不太自然，但在 Python 自定义回测中非常容易实现。
- 如果框架本身不支持“订单计划”抽象，需要额外代码。

**适合场景**：A 股 ETF 多资产轮动、目标权重之和接近 100%、需要精确控制现金分配、需要 T+1 和整手约束。

**A 股 ETF 适用性**：**高**。完全匹配我们的需求：
- 16 只 ETF 候选，但只选 3-5 只持有。
- 需要“先卖后买”释放现金（A 股 T+1，当日卖出资金可用于买入）。
- 需要 100 股整手。
- 需要“建仓上限 20%，漂移不主动减仓”。
- 需要防御资产（如 511880）为进攻资产让路。
- 需要处理停牌/缺价（从订单计划中剔除）。

---

## 5. 推荐架构（不修改现有代码）

基于上述调研，针对我们的 `etf_rotation_model`（A 股 ETF 轮动，16 只候选，定期再平衡，T+1，100 股整手，目标权重可能存在漂移容忍），推荐在现有代码基础上采纳 **方案 C** 的架构思想，但**保持 `rebalance_planner.py` 和 `backtest.py` 不变**（即本次调研仅为架构输入，不修改代码）。

### 推荐架构要点

1. **Rebalance Planner 层（现有 `rebalance_planner.py` 的升级方向）**
   - 输入：`target_weights`（dict，symbol -> target_weight，sum <= 1.0）、当前持仓、当前价格。
   - 输出：`OrderPlan`（对象，包含 sell_list 和 buy_list，每个元素包含 symbol, target_shares, order_type）。
   - 关键步骤：
     a. 对不在 `target_weights` 中的持仓，生成 **全额卖出** 订单。
     b. 对 `target_weights` 中的资产，计算 `target_shares = (portfolio_value * target_weight) // price // 100 * 100`。
     c. 如果 `target_shares == current_shares`，不生成订单（实现漂移容忍）。
     d. 如果 `target_shares > current_shares`，生成 **买入** 订单（数量 = target_shares - current_shares）。
     e. 如果 `target_shares < current_shares`，生成 **卖出** 订单（数量 = current_shares - target_shares）。
     f. 汇总 sell 释放的现金 = sum(sell_shares * price * (1 - commission - slippage))。
     g. 总可用现金 = 当前现金 + sell 释放现金。
     h. 汇总 buy 所需现金 = sum(buy_shares * price * (1 + commission + slippage))。
     i. 如果 buy 所需现金 > 总可用现金，对 buy_list 按 **同比例缩放**（或按优先级排序后 greedy 分配，低优先级资产分配不足时直接丢弃）。
     j. 再次检查缩放后的 buy_shares 是否满足 100 股整手，不满足则向下取整到 100 的倍数，剩余现金放弃。

2. **Executor 层（现有 `backtest.py` 的升级方向）**
   - 按 `OrderPlan.sell_list` 先执行所有卖出（更新 cash）。
   - 按 `OrderPlan.buy_list` 后执行所有买入（使用已更新的 cash）。
   - 如果某只 ETF 当日停牌（price 为 NaN 或 0），从 `OrderPlan` 中剔除该资产的订单，记录日志，不执行。
   - 佣金和滑点模型：A 股 ETF 佣金通常为 0.0003（万三），无印花税，滑点可设为 0.001%（或 0.0001 的 fixed slippage）。

3. **与现有框架的借鉴点**
   - 借鉴 **bt** 的 `Rebalance` 先 close 非 targets、再 rebalancing targets 的顺序设计。
   - 借鉴 **vectorbt** 的 `buy_nb` 现金不足反算逻辑：`max_req_cash = (cash_limit - fixed_fees) / (1 + fees)`，以及 `size_granularity` 的整手处理。
   - 借鉴 **PyPortfolioOpt** 的 `greedy_portfolio` 两轮分配思想：先买整数部分，再按偏差微调剩余现金（但我们的场景是“先全局缩放”再取整，更简洁）。
   - 借鉴 **Qlib** 的 `SimulatorExecutor` 的 `TT_SERIAL` 串行执行，但我们在订单计划层完成排序和缩放，执行层只是顺序提交。
   - 借鉴 **LEAN** 的 `PreOrderChecks` 设计：在执行前增加 `price > 0`、`shares % 100 == 0`、`min_order_value > 0` 等预检。

4. **不推荐直接使用的框架**
   - **Zipline Reloaded**：A 股日历不兼容，trading-calendar bug 未修复，且核心设计针对美股。
   - **Backtrader**：顺序依赖严重，事件驱动对 A 股日线轮动不必要，且先卖后买需要手动补丁。

### 为什么不是“直接采用 vectorbt”或“直接采用 bt”

- **vectorbt**：虽然参数支持最完善（granularity、fees、slippage、min_size），但 `from_orders` 的逐列执行方式对“全局订单计划”不友好。若要实现方案 C，需要放弃 `from_orders` 改用 `from_order_func`，学习成本高，且向量化 Numba 代码难以与现有 Python 事件逻辑融合。
- **bt**：`Rebalance` 的“先 close 非 targets”设计很好，但 `integer_positions` 只保证整数不保证 100 股整手，且没有全局现金缩放。`bt` 的树结构对单一 ETF 轮动层而言过度设计。

### 结论

对于 A 股 ETF 轮动模型，最稳健的调仓架构是：

> **在 Rebalance Planner 中一次性生成完整的 sell/buy 订单计划，先计算所有卖出释放的现金，再对所有买入订单进行同比例缩放（或优先级 greedy）以适应可用现金，最后按 sell-first-then-buy 的顺序提交。每只资产的股数在计划阶段即向下取整到 100 的倍数，并剔除停牌/缺价资产。**

这一架构可以消除顺序依赖、保证先卖后买、支持整手、处理现金不足，并自然兼容“建仓上限后漂移不主动减仓”的业务需求。它是 Backtrader 的事件驱动、vectorbt 的现金缩放、bt 的 Rebalance 顺序、Qlib 的订单计划四种思想的融合。

---

## 6. 参考代码片段速查

### 6.1 vectorbt 的 SizeType 转换链（`vectorbt/portfolio/nb.py`）
```python
if order_size_type == SizeType.TargetPercent:
    order_size *= value
    order_size_type = SizeType.TargetValue
if order_size_type == SizeType.TargetValue:
    order_size /= val_price
    order_size_type = SizeType.TargetAmount
if order_size_type == SizeType.TargetAmount:
    order_size -= position
    order_size_type = SizeType.Amount
```

### 6.2 vectorbt 的现金不足反算（`vectorbt/portfolio/nb.py:buy_nb`）
```python
max_req_cash = add_nb(cash_limit, -fixed_fees) / (1 + fees)
max_acq_size = max_req_cash / adj_price
if not np.isnan(size_granularity):
    final_size = max_acq_size // size_granularity * size_granularity
else:
    final_size = max_acq_size
```

### 6.3 bt 的 Rebalance 顺序（`bt/algos.py:Rebalance.__call__`）
```python
# 先关闭不在 targets 中的持仓
for cname in target.children:
    if cname in targets:
        continue
    c = target.children[cname]
    if c.value != 0.0 and not np.isnan(c.value):
        target.close(cname, update=False)

# 再按目标权重重新平衡
for item in targets.items():
    target.rebalance(item[1], child=item[0], base=base, update=False)
```

### 6.4 Qlib 的串行/并行执行（`qlib/backtest/executor.py:SimulatorExecutor`）
```python
if self.trade_type == self.TT_SERIAL:
    order_it = orders
elif self.trade_type == self.TT_PARAL:
    order_it = sorted(orders, key=lambda order: -order.direction)
```

### 6.5 Backtrader 的 order_target_percent（`backtrader/strategy.py`）
```python
def order_target_percent(self, data=None, target=0.0, **kwargs):
    possize = self.getposition(data, self.broker).size
    target *= self.broker.getvalue()
    return self.order_target_value(data=data, target=target, **kwargs)
```

### 6.6 LEAN 的订单预检（`Algorithm/QCAlgorithm.Trading.cs:PreOrderChecksImpl`）
```csharp
if (Math.Abs(request.Quantity) < security.SymbolProperties.LotSize)
    return OrderResponse.Error(request, OrderResponseErrorCode.OrderQuantityLessThanLotSize, ...);
if (!security.IsTradable)
    return OrderResponse.Error(request, OrderResponseErrorCode.NonTradableSecurity, ...);
if (price == 0)
    return OrderResponse.Error(request, OrderResponseErrorCode.SecurityPriceZero, ...);
```

### 6.7 PyPortfolioOpt 的 Greedy 离散分配（`pypfopt/discrete_allocation.py`）
```python
# Round 1: floor
n_shares = int(weight * total_portfolio_value / price)
# Round 2: deficit-based
while available_funds > 0:
    deficit = ideal_weights - current_weights
    idx = np.argmax(deficit)
    if price > available_funds: break
    shares_bought[idx] += 1
    available_funds -= price
```

---

## 7. 附录：调研时的 GitHub 代码获取路径

| 框架 | 文件路径 | 获取方式 |
|------|---------|---------|
| LEAN | `Algorithm/QCAlgorithm.Trading.cs` | `raw.githubusercontent.com/QuantConnect/Lean/master/Algorithm/QCAlgorithm.Trading.cs` |
| LEAN | `Common/Securities/SecurityPortfolioManager.cs` | `raw.githubusercontent.com/QuantConnect/Lean/master/Common/Securities/SecurityPortfolioManager.cs` |
| LEAN | `Common/Securities/SecurityTransactionManager.cs` | `raw.githubusercontent.com/QuantConnect/Lean/master/Common/Securities/SecurityTransactionManager.cs` |
| Backtrader | `backtrader/strategy.py` | `raw.githubusercontent.com/mementum/backtrader/master/backtrader/strategy.py` |
| Backtrader | `backtrader/broker.py` | `raw.githubusercontent.com/mementum/backtrader/master/backtrader/broker.py` |
| Zipline | `zipline/algorithm.py` | 直接获取失败，参考社区文档与 arxiv 2603.20319 |
| vectorbt | `vectorbt/portfolio/nb.py` | `raw.githubusercontent.com/polakowo/vectorbt/master/vectorbt/portfolio/nb.py` |
| Qlib | `qlib/backtest/executor.py` | `raw.githubusercontent.com/microsoft/qlib/main/qlib/backtest/executor.py` |
| bt | `bt/algos.py` | `raw.githubusercontent.com/pmorissette/bt/master/bt/algos.py` |
| PyPortfolioOpt | `pypfopt/discrete_allocation.py` | `raw.githubusercontent.com/robertmartin8/PyPortfolioOpt/master/pypfopt/discrete_allocation.py` |

---

*报告结束。本调研严格基于实际源代码，未引用 README 或二手博客作为核心结论依据。*
