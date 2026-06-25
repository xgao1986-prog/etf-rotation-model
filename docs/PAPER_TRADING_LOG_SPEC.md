# 纸面交易日志规范 (Paper Trading Log Spec) v0.1

> 文档版本：v0.1
> 最后更新：2026-06-26
> 状态：B1 候选收敛验证准备阶段

## 1. 目的

为后续 3-6 个月纸面实盘验证做准备，设计纸面交易日志的字段、格式和流程。

**纸面交易 = 不自动交易，只记录建议 vs 实际执行的差异。**

## 2. 核心原则

1. **不自动交易**：模型只生成建议，实际执行由用户手动确认
2. **逐笔记录**：每一笔建议订单和实际执行都记录
3. **偏差追踪**：记录建议价 vs 实际执行价的差异（滑点）
4. **原因归因**：记录为什么没执行、为什么卖飞、为什么提前止损

## 3. 日志文件

### 3.1 主日志文件

**路径**：`data/live/paper_trading_log.csv`

**字段定义**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plan_id` | string | 是 | 交易计划唯一ID，格式：YYYYMMDD_NNN |
| `signal_date` | date | 是 | 信号生成日期（模型计算日） |
| `suggested_trade_date` | date | 是 | 建议交易日期（通常是信号日次日） |
| `ticker` | string | 是 | ETF 代码 |
| `action` | enum | 是 | BUY / SELL / HOLD / STOP_LOSS |
| `suggested_shares` | int | 是 | 模型建议的股数 |
| `suggested_price` | float | 是 | 模型建议的成交价格（通常是信号日收盘价） |
| `actual_executable_price` | float | 否 | 用户在建议交易日实际可以执行的价格（开盘价/盘中价） |
| `actual_executed_price` | float | 否 | 用户实际成交的价格 |
| `slippage_bp` | float | 否 | 滑点 = (actual_executed - suggested) / suggested * 10000，单位：基点 |
| `executed` | bool | 是 | 是否实际执行 |
| `not_executed_reason` | string | 否 | 未执行原因：现金不足 / 价格跳空 / 用户判断 / 技术故障 / 其他 |
| `stop_loss_triggered` | bool | 否 | 是否由止损触发 |
| `model_reason` | string | 是 | 模型建议原因：调仓买入 / 调出候选 / 止损 / 止盈 / 保留 |
| `manual_note` | string | 否 | 用户手动备注 |
| `linked_actual_trade_id` | string | 否 | 关联到 actual_trades.csv 中的成交记录ID |
| `created_at` | datetime | 是 | 记录创建时间 |

### 3.2 示例记录

```csv
plan_id,signal_date,suggested_trade_date,ticker,action,suggested_shares,suggested_price,actual_executable_price,actual_executed_price,slippage_bp,executed,not_executed_reason,stop_loss_triggered,model_reason,manual_note,linked_actual_trade_id,created_at
20260626_001,2026-06-26,2026-06-27,512400.SH,BUY,200,0.650,0.652,0.651,1.54,True,,False,调仓买入,,TRADE_001,2026-06-26T15:30:00
20260626_002,2026-06-26,2026-06-27,515230.SH,SELL,300,1.150,1.145,1.148,-17.39,True,,False,调出候选列表,,TRADE_002,2026-06-26T15:30:00
20260626_003,2026-06-26,2026-06-27,512010.SH,BUY,100,1.200,1.205,,,False,价格跳空后追高犹豫,False,调仓买入,早盘跳空3%未追,,2026-06-26T15:30:00
```

## 4. 日志流程

### 4.1 每周调仓日

```
1. 周四收盘后运行 B0.4 信号
   → 生成交易计划（latest_trade_plan.csv）
   → 每条计划生成 plan_id

2. 写入 paper_trading_log.csv（建议部分）
   → signal_date = 周四
   → suggested_trade_date = 周五
   → executed = False
   → 其他字段留空

3. 周五用户执行交易
   → 实际成交后填写 actual_executed_price, executed, slippage_bp
   → 未执行则填写 not_executed_reason
   → 关联到 actual_trades.csv

4. 周日/下周一复盘
   → 补充 manual_note
   → 记录 stop_loss_triggered（如果触发）
```

### 4.2 每日止损检查

```
1. 每日收盘后运行止损检查
   → 如果触发止损，生成紧急交易建议
   → plan_id = 当日日期 + 紧急编号

2. 次日开盘执行
   → 记录实际执行情况
```

## 5. 字段详细说明

### 5.1 plan_id 生成规则

- 格式：`YYYYMMDD_NNN`
- 每周调仓：从 `YYYYMMDD_001` 开始递增
- 紧急止损：使用 `YYYYMMDD_E01` 等编号
- 示例：`20260626_001`, `20260626_002`, `20260626_E01`

### 5.2 slippage_bp 计算

```python
if suggested_price > 0 and actual_executed_price is not None:
    slippage_bp = (actual_executed_price - suggested_price) / suggested_price * 10000
else:
    slippage_bp = None
```

- 正值 = 买贵了 / 卖高了
- 负值 = 买便宜了 / 卖低了
- 单位：基点（1bp = 0.01%）

### 5.3 not_executed_reason 枚举

| 原因 | 说明 |
|------|------|
| `现金不足` | 账户可用资金不够 |
| `价格跳空` | 次日开盘价与建议价差距过大 |
| `用户判断` | 用户主观决定不执行 |
| `技术故障` | 系统或券商问题 |
| `冷却期` | 该ETF在止损冷却期内 |
| `其他` | 其他原因，补充在 manual_note 中 |

### 5.4 model_reason 枚举

| 原因 | 说明 |
|------|------|
| `调仓买入` | 新进入Top5，建议买入 |
| `调仓卖出` | 跌出候选列表，建议卖出 |
| `止损` | 触发止损线，建议卖出 |
| `止盈` | 触发动态止盈，建议卖出 |
| `保留` | 继续持有，无需操作 |
| `防御买入` | 大盘择时触发防御配置 |
| `防御卖出` | 防御资产退出 |
| `腾槽位` | 为更高评分标的腾槽位 |
| `强制减仓` | 大盘择时强制减仓 |

## 6. 与实盘助手的集成

### 6.1 数据流

```
B0.4 信号
  ↓
生成交易计划 (latest_trade_plan.csv)
  ↓
生成纸面日志 (paper_trading_log.csv) ← 建议部分
  ↓
用户执行交易
  ↓
更新实际持仓 (actual_positions.csv)
  ↓
记录实际成交 (actual_trades.csv)
  ↓
回填纸面日志 ← 执行部分
```

### 6.2 在 app.py 中的展示

在"实盘助手"页面的"成交记录"子页中，新增纸面交易日志表格：
- 显示每笔建议 vs 实际执行的对比
- 高亮滑点过大的记录（|slippage_bp| > 50）
- 统计未执行原因分布
- 显示建议准确率（建议买入后N日涨跌）

## 7. 分析报告

### 7.1 每周报告

`reports/live/paper_trading_weekly.md`

内容：
- 本周建议订单数
- 实际执行率
- 平均滑点
- 未执行原因分布
- 模型建议 vs 实际盈亏对比

### 7.2 月度报告

`reports/live/paper_trading_monthly.md`

内容：
- 累计建议准确率
- 累计滑点成本
- 未执行导致的收益/损失估算
- 止损触发频率
- 与B0.4回测结果的偏差分析

## 8. 3-6 个月验证目标

### 8.1 验证指标

| 指标 | 目标 | 说明 |
|------|------|------|
| 执行率 | ≥ 80% | 建议订单中实际执行的比例 |
| 滑点 | ≤ 20bp | 平均滑点不超过20基点 |
| 止损准确率 | ≥ 70% | 止损后10日内下跌的比例 |
| 买入准确率 | ≥ 60% | 买入后20日上涨的比例 |
| 跟踪误差 | ≤ 5% | 纸面组合净值 vs B0.4回测净值偏差 |

### 8.2 通过标准

3-6个月后，如果满足以下全部条件，可以进入小资金实盘：
1. 执行率 ≥ 80%
2. 滑点 ≤ 20bp
3. 跟踪误差 ≤ 5%
4. 用户操作舒适度良好（主观评估）

## 9. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v0.1 | 2026-06-26 | 初始设计，18字段，支持B0.4纸面交易 |

## 10. 声明

> **纸面交易日志目前仅用于验证模型可跟踪性，不进入实际交易决策。**
> 实际交易决策仍以用户手动确认为准。
