# 实盘交互与信号发布模块设计文档 (v0.1)

> 文档版本：v0.1
> 最后更新：2026-06-26

## 1. 设计目标

把当前 B0.4 模型转成可用于真实持仓管理的操作模块。

**本阶段边界：**
- 不修改 B0.4 策略规则
- 不接入 C/D/状态切换增强
- 不自动下单
- 真实持仓以用户录入为准
- 模型只生成目标组合和交易建议

## 2. 核心原则

### 2.1 真实持仓以用户录入为准

- 初始持仓通过 CSV / 表格录入
- 每日收盘后更新价格，但不改变持仓股数
- 实际成交后，用户录入真实成交价格和数量，系统再更新真实持仓
- 不假设模型建议一定被完全执行

### 2.2 目标组合与真实持仓分离

- `actual_positions.csv`：用户真实持仓
- `latest_trade_plan.csv`：模型建议的目标持仓
- 两者对比生成订单建议
- 用户执行后，录入 `actual_trades.csv`

### 2.3 不自动下单

- v0.1 只输出建议订单
- 用户手动确认后执行
- 实际成交记录由用户手动录入或导入

## 3. 数据文件结构

### 3.1 真实持仓 (data/live/actual_positions.csv)

```csv
ticker,name,shares,cost_price,current_price,market_value,update_time
512400.SH,信息技术ETF,200,0.636,0.650,130.0,2026-06-26
```

额外行：
```csv
__CASH__,现金,0,0,0,150000.0,2026-06-26
```

### 3.2 实际成交记录 (data/live/actual_trades.csv)

```csv
date,ticker,action,shares,actual_price,commission,note
2026-06-26,512400.SH,BUY,100,0.650,0.2,首次建仓
```

### 3.3 最新交易计划 (data/live/latest_trade_plan.csv)

```csv
ticker,action,current_shares,target_shares,delta_shares,estimated_price,estimated_amount,reason,commission,post_cash
512400.SH,BUY,0,200,200,0.650,130.0,调仓买入,0.2,149869.8
```

### 3.4 止损检查报告 (reports/live/daily_stop_loss_alert.md)

- 每日收盘后生成
- 列出触发止损的持仓
- 给出建议操作

### 3.5 每周调仓计划 (reports/live/weekly_rebalance_plan.md)

- 每周四收盘后生成
- 对比真实持仓 vs 目标持仓
- 输出建议订单列表

## 4. 核心模块设计 (src/live_trading_assistant.py)

### 4.1 LiveTradingAssistant 类

```python
class LiveTradingAssistant:
    """实盘交互与信号发布模块 v0.1"""

    def __init__(self, config, positions_path, trades_path, plan_path):
        self.cfg = config
        self.positions_path = positions_path
        self.trades_path = trades_path
        self.plan_path = plan_path

    # 持仓管理
    def load_positions(self) -> pd.DataFrame
    def save_positions(self, df: pd.DataFrame)
    def validate_positions(self, positions_df) -> ValidationReport

    # 价格更新
    def update_prices(self, date: str) -> pd.DataFrame

    # 止损检查
    def check_stop_loss(self, date: str) -> pd.DataFrame

    # 调仓计划
    def generate_trade_plan(self, target_positions: dict, date: str) -> pd.DataFrame

    # 成交记录
    def record_trade(self, trade: dict) -> pd.DataFrame
    def apply_trade(self, trade: dict) -> pd.DataFrame

    # 报告生成
    def generate_daily_alert(self, date: str, output_path: str)
    def generate_weekly_plan(self, date: str, output_path: str)
```

### 4.2 校验规则 (ValidationReport)

| 校验项 | 规则 | 错误级别 |
|--------|------|----------|
| ETF池内 | ticker 必须在 ETF_UNIVERSE 或 DEFENSE_UNIVERSE 中 | ERROR |
| 100股整数 | shares 必须是100的整数倍 | WARNING |
| NAV恒等式 | 现金 + 持仓市值 = 总资产 | ERROR |
| 模型外持仓 | 非池内持仓需标注 | WARNING |
| 缺价 | current_price 必须 > 0 | ERROR |
| 总仓位 | 总仓位不得超过 max_total_position | WARNING |

### 4.3 订单生成逻辑

1. 读取真实持仓 `P_actual`
2. 读取目标持仓 `P_target`（来自 B0.4 信号）
3. 对比生成订单：
   - 目标股数 > 实际股数 → BUY
   - 目标股数 < 实际股数 → SELL
   - 目标股数 = 实际股数 → HOLD
4. 计算预计金额 = 差额股数 × 预计价格
5. 检查现金是否足够：
   - 不足 → 缩放订单或提示
6. 计算佣金
7. 输出订单列表

### 4.4 现金不足处理

- 先执行 SELL 订单（释放现金）
- 再执行 BUY 订单
- 如果 BUY 订单总额 > 可用现金：
  - 按比例缩放 BUY 订单
  - 或标记为"现金不足，需手动调整"

## 5. 命令行脚本

### 5.1 scripts/live_update_positions.py

```bash
py scripts/live_update_positions.py --date 2026-06-26
```

功能：更新持仓价格，计算市值

### 5.2 scripts/live_check_stop_loss.py

```bash
py scripts/live_check_stop_loss.py --date 2026-06-26
```

功能：检查止损，生成 alert

### 5.3 scripts/live_generate_trade_plan.py

```bash
py scripts/live_generate_trade_plan.py --date 2026-06-26
```

功能：运行 B0.4 信号，生成调仓计划

### 5.4 scripts/live_record_trade.py

```bash
py scripts/live_record_trade.py \
  --date 2026-06-26 \
  --ticker 512400.SH \
  --action BUY \
  --shares 100 \
  --price 0.650 \
  --commission 0.2 \
  --note "首次建仓"
```

功能：记录实际成交，更新持仓

## 6. UI 设计 (app.py 新增"实盘助手"页)

### 6.1 页面结构

```
📈 B0-18 ETF轮动量化策略
├── 仪表盘
├── 回测结果
├── ETF分析
├── 数据管理
├── 策略配置
└── 实盘助手 (新增)  ← 第6个 tab
```

### 6.2 实盘助手页面布局

```
┌─────────────────────────────────────────────────┐
│  真实持仓概览 (KPI cards)                          │
│  - 总资产 | 现金 | 持仓市值 | 总仓位 | 今日盈亏      │
├─────────────────────────────────────────────────┤
│  持仓明细表 (editable)                             │
│  - ticker | 名称 | 股数 | 成本 | 现价 | 市值 | 盈亏 │
├─────────────────────────────────────────────────┤
│  操作区                                            │
│  - 上传持仓 CSV                                    │
│  - 手动添加/修改持仓                                │
│  - 更新价格                                        │
├─────────────────────────────────────────────────┤
│  止损检查 (daily)                                  │
│  - 触发止损的持仓列表                              │
│  - 建议操作                                        │
├─────────────────────────────────────────────────┤
│  调仓建议 (weekly)                                 │
│  - 目标持仓 vs 真实持仓对比                         │
│  - 建议订单列表                                     │
│  - 执行前后仓位对比                                 │
├─────────────────────────────────────────────────┤
│  成交记录                                          │
│  - 今日/本周/全部成交记录                           │
│  - 手动录入成交                                     │
└─────────────────────────────────────────────────┘
```

### 6.3 图片识别预留

v0.1：
- 提供"截图上传"按钮
- 上传后显示图片，提示用户手动录入表格
- 预留 `uploaded_image` 变量，v0.2 接入 OCR

## 7. 测试策略

### 7.1 测试文件

- `tests/test_live_positions.py` — 持仓读取、校验、更新
- `tests/test_live_trade_plan.py` — 订单生成、现金不足处理
- `tests/test_live_trades.py` — 成交记录、持仓更新
- `tests/test_live_stop_loss.py` — 止损检查

### 7.2 测试用例

| 测试 | 描述 | 期望 |
|------|------|------|
| 持仓读取 | 读取 valid CSV | 返回正确 DataFrame |
| NAV恒等式 | 现金 + 市值 = 总资产 | 校验通过 |
| 非100股 | 输入 150 股 | 抛出 WARNING |
| 模型外ETF | 输入 999999.SH | 抛出 ERROR |
| 缺价 | current_price = 0 | 抛出 ERROR |
| 订单生成 | 目标 vs 实际 | 生成正确 BUY/SELL/HOLD |
| 现金不足 | BUY 总额 > 现金 | 缩放或提示 |
| 成交更新 | 记录一笔 BUY | 持仓股数 +100 |
| 止损检查 | 价格跌破止损线 | 输出 alert |
| 总仓位 | 仓位 > 100% | 抛出 WARNING |

## 8. 实现计划

### Phase 1: 核心模块
- [ ] `src/live_trading_assistant.py` — 核心类
- [ ] `data/live/` 目录和示例 CSV
- [ ] `reports/live/` 目录

### Phase 2: 命令行脚本
- [ ] `scripts/live_update_positions.py`
- [ ] `scripts/live_check_stop_loss.py`
- [ ] `scripts/live_generate_trade_plan.py`
- [ ] `scripts/live_record_trade.py`

### Phase 3: UI
- [ ] `app.py` 新增"实盘助手" tab
- [ ] 持仓录入界面
- [ ] 止损检查界面
- [ ] 调仓建议界面
- [ ] 成交记录界面

### Phase 4: 测试
- [ ] `tests/test_live_*.py` (10+ 测试用例)
- [ ] pytest 全部通过

### Phase 5: 文档
- [ ] `docs/LIVE_TRADING_ASSISTANT_DESIGN.md`
- [ ] `docs/CHANGES.md` 更新
- [ ] `docs/CURRENT_STATE.md` 更新

## 9. 风险与边界

### 9.1 v0.1 不做的事

- 不自动下单（只输出建议）
- 不做实时行情推送（每日收盘后更新）
- 不做图片自动识别（预留接口，手动录入）
- 不接入 C/D/状态切换（只用 B0.4）
- 不修改 B0.4 策略规则
- 不做多账户管理
- 不做资金归集/拆分

### 9.2 已知限制

- 日终数据更新依赖 iFinD/AKShare 数据获取
- 持仓录入需要用户手动操作
- 成交记录需要用户手动录入或从券商导出后导入
- 图片识别 v0.2 才支持

## 10. 后续版本 (v0.2+)

| 版本 | 功能 |
|------|------|
| v0.2 | 图片自动识别（OCR） |
| v0.3 | 多账户管理 |
| v0.4 | 自动下单接口（对接券商 API） |
| v0.5 | 实时行情推送 + 盘中预警 |
| v1.0 | 接入 C/D/状态切换增强 |
