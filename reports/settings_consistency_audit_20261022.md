# 账户设置与实际执行一致性审计（第一检查点）

- **日期**：2026-10-22
- **范围**：回测（侧栏参数）、虚拟盘（预设/账户冻结参数）、实盘助手（实盘建议）三条路径
- **方法**：逐参数追踪代码链路（`app.py:build_sidebar_config` → `src/backtest.py`/`src/strategy.py`；`src/strategy_presets.py` → `src/paper_trading/*`；`scripts/live_generate_trade_plan.py` + `src/live_trading_assistant.py`）
- **约束**：B0.4 冻结口径未动；审计只读，未修改任何代码与数据

---

## 1. 设置支持清单（审计结论）

### 1.1 回测路径（侧栏 → BacktestEngine）

侧栏全部参数经 `build_config()` 合并进 `cfg`，`BacktestEngine` 逐条读取。逐项核实均到达执行路径：

| 设置项 | 生效 | 证据 |
|--------|------|------|
| 评分权重 weights（5因子） | ✅ | 回测内重新评分（`strategy.py`） |
| 动量/波动率因子开关 | ✅ | `strategy.py` 因子计入开关 |
| 趋势/确认/总分最低分 | ✅ | `strategy.py:515-517` 入场过滤 |
| 最大持仓数 / 单只上限 | ✅ | `backtest.py:527-530` 传入 v2.5 规划器 |
| 止损线 / 止损模式 / ATR倍数 | ✅ | `backtest.py:1001-1025` fixed/atr/none 三模式 |
| 大盘择时 | ✅ | `strategy.py:324-348` 现行规则：close>MA50→满仓1.0，跌破MA50→半仓0.5，shift(1) 防未来函数；**非"严格减至半仓立即执行"以外的任何规则** |
| 防御开关 / 防御比例 / 防御池 | ✅ | `backtest.py:484/537/1362/1710` |
| 调仓频率 / 调仓日 | ✅ | `backtest.py:412-443` cfg 驱动 |
| 冷静期 / 动态止盈 / 宽基补仓 | ✅ | `backtest.py:470-478 / 378-396 / 1625` |
| 初始资金 | ✅ | `backtest.py:42` |

⚠️ 一个实现隐患（不影响回测结果正确性，但影响其他路径）：侧栏"防御资产模式（仅黄金/仅国债）"通过**改写全局模块变量** `config.DEFENSE_UNIVERSE` 实现（`app.py:451-457`），见 1.2 泄漏问题。

### 1.2 虚拟盘路径（预设 → 账户冻结配置 → PaperTradingRunner）

预设仅含 7 个字段（weights、min_trend_score、min_confirm_score、min_total_score、max_holdings、max_position_per_etf、stop_loss），创建时原样冻结进 `config_json`。逐项核实 runner 实际使用：

| 设置项 | 生效 | 问题 |
|--------|------|------|
| min_total_score | ✅ | `runner.py:603` 入场过滤 |
| max_holdings | ✅ | `runner.py:635-637` 传入规划器 |
| max_position_per_etf | ✅ | `runner.py:638` |
| **stop_loss** | ❌ | **硬编码 -0.08**：`runner.py:134` 调用 `_check_stop_loss` 未传账户配置，账户保存的止损线被静默忽略（如"保守型"-5% 实际按 -8% 跑） |
| **min_trend_score / min_confirm_score** | ❌ | **静默忽略**：`runner.py:606` 只过滤 total_score，不过滤 trend/confirm/均线条件（回测路径有过滤） |
| **weights** | ❌ | **静默忽略**：账户运行使用数据库统一评分（daily_scores 全局管线），预设权重不产生任何影响 |
| **大盘择时** | ❌ 不支持 | runner 无任何 market_timing 逻辑；预设无法表达，但也无人告知用户"虚拟盘不支持择时" |
| **防御开关** | ❌ | **硬编码 True**：`runner.py:643` `defense_enabled=True`，账户无法关闭防御 |
| **调仓日** | ❌ | **硬编码周四**：`calendar.py:124-126` `is_rebalance_day` 写死 weekday==3，账户配置无此字段 |
| 止损模式(atr/none)、冷静期、动态止盈、宽基、调仓频率 | ❌ 不支持 | 预设无法表达；创建时不做任何"不支持"提示 |

**泄漏问题（违反"修改侧栏不得偷偷改变已有账户"）**：
- 侧栏切换"防御资产模式"会改写全局 `config.DEFENSE_UNIVERSE`；`runner.py:589` 在**运行时**从 config 模块读 ETF_UNIVERSE/DEFENSE_UNIVERSE——侧栏一动，所有已创建账户的防御池立即跟着变。账户创建时未冻结标的池。

**创建校验缺失**：`service.py:create_account` 不校验 strategy_config 内容，任何键值都能存进账户——不支持项保存后静默忽略。

### 1.3 实盘助手路径（scripts/live_generate_trade_plan.py + LiveTradingAssistant）

| 设置项 | 生效 | 问题 |
|--------|------|------|
| 参数来源 | — | **固定使用 `build_config()` 模块默认值**（B0.4 默认口径），完全不看侧栏和预设；页面无说明 |
| min_total_score / max_holdings / max_position | ✅（默认 40/5/20%） | 固定默认值 |
| **防御候选过滤** | ⚠️ | **无评分门槛**（`live_generate_trade_plan.py:73-74` 全量纳入），与虚拟盘/回测的 min_total-10 门槛不一致 |
| **止损检查** | ❌ | **符号 bug**：`live_trading_assistant.py:249` 取 `config.get("stop_loss", 0.08)`，app.py 传入的 cfg 中 stop_loss=-0.08（负值口径），`:256` 处 `-stop_loss_pct` 变成 +0.08 → **亏损小于 +8% 即报警，几乎全部持仓误报** |
| 大盘择时 / 止损模式 / 调仓日 | ❌ 不支持 | 择时无逻辑；止损只有固定比例；调仓计划由用户手动每周运行脚本 |
| 账户概念 | — | 无独立账户参数，全页面共享 `data/live/*.csv` |

**价格口径差异（允许差异，需说明）**：实盘建议用信号日收盘价估算（T日收盘生成计划，用户 T+1 手动执行）；虚拟盘 runner 用执行日开盘价成交并估值；回测 B0.4 口径为"T日收盘出信号、T+1 开盘成交"。三者语义一致，但估算价格不同会导致股数尾差（整手取整方向一致，均为 floor 到 100 股）。

---

## 2. 修复方案（本检查点实施）

| # | 问题 | 修复 |
|---|------|------|
| F1 | 虚拟盘 stop_loss 硬编码 | runner 使用账户冻结配置的 stop_loss（缺省 -0.08，与旧行为兼容） |
| F2 | 虚拟盘调仓日硬编码周四 | `is_rebalance_day(date, weekday)` 支持参数；runner 使用账户配置（缺省 3=周四，与旧行为兼容） |
| F3 | 虚拟盘 defense_enabled 硬编码 | runner 将账户配置传入规划器（缺省 True，与旧行为兼容） |
| F4 | 虚拟盘入场只过滤总分 | runner 在评分列齐备时补齐 trend/confirm/above_ma20/ma20_slope 过滤，与回测入场语义对齐 |
| F5 | 标的池全局泄漏 | 账户创建时把 industry_tickers/defense_tickers 冻结进 config_json；runner 使用冻结池（老账户缺省回退 config 模块，行为不变） |
| F6 | 不支持项静默忽略 | 新增 `validate_account_strategy_config()`：未知键、market_timing=True、stop_loss_mode≠fixed、trailing≠none、cooling>0、fallback=True 等**拒绝创建**并给出中文明细；weights 属共享评分管线参数，创建页明确提示"不逐账户生效" |
| F7 | 实盘助手止损符号 bug | `abs()` 修正阈值符号 |
| F8 | 实盘防御候选无门槛 | 对齐 min_total-10；行业候选补齐与回测一致的入场过滤 |
| F9 | 页面区分三类参数 | 虚拟盘创建页列出"账户支持/不支持"清单；账户总览展示每个账户的冻结参数；文档明确实盘助手=固定 B0.4 默认口径 |

**行为变更警示**：F1 生效后，既有账户（如按"保守型"创建的账户，止损 -5%）的下一次运行将真正按其保存的止损线执行，与此前实际执行的 -8% 不同。这是"保存参数真正生效"的修复本意，但属于既有账户的行为变化，验收时需明确知悉。

## 3. 验证计划

- TDD：先写失败测试（数值断言、调真实生产函数），确认在旧代码上失败；
- 修复后新测试全部通过；
- 回归：`test_paper_trading_*`、`test_live_*`、`test_b0_4_slippage.py`（B0.4 冻结复现 NAV 2,761,288.07 / 804 笔）；
- 三路径一致性：相同评分/价格/账户状态下，回测规划器调用、虚拟盘 runner、实盘脚本产出相同目标持仓；允许差异逐项书面解释；
- 测试全部使用临时数据库/临时账户/临时 CSV 目录，不碰真实数据库、真实持仓、个人预设、B0.4 冻结快照。

---

## 4. 复审补修第二轮（2026-10-22 晚，验收未通过后的定向补修）

第一检查点复审未通过，要求仅补齐以下四项，不启动页面样稿、不提交。

### 补 1：评分字段缺失 → 停止生成交易并明确提示

- 新增 `src/paper_trading/entry_filter.py::validate_score_data()`：评分数据缺 `prev_close`/`ma20_slope`/`trend_score`/`confirm_score`/`total_score` 任一列，或关键字段含 NaN 时抛 `ValueError`，中文指明缺失字段与涉及标的。
- 删除"缺字段退回仅看总分"的宽松路径；虚拟盘 runner 当日报错（不存信号、不标记已处理），实盘脚本捕获后打印 ERROR 并非零退出；`app.py` 数据富化失败的标的进 `invalid_scores` 阻止运行。
- 既有测试的旧合成评分数据（仅 total_score 等不全字段）已全部补齐。

### 补 2：B0.4 完整入场条件落实到三路径同一实现

- `entry_filter.select_candidates()` 实现正式 B0.4 入场：弱市 min_total 升至 55（`market_quality_poor`：核心池 momentum_20 中位数 < 0 判定）、防御资格（trend≥10 / confirm≥2 / total≥max(30, min_total-10) / prev_close>ma20×0.98 / slope>-0.001）、数据成熟期 history_count≥51、prev_close>ma20、ma20_slope>0。
- 虚拟盘 `_run_planner` 与实盘脚本 `get_b0_4_signals`（按 ticker 富化 prev_close/history_count）均改走该函数，不再各自复制筛选。
- 三路径比较测试改经真实 `generate_signals` 生产信号路径：相同输入下三路径目标持仓逐一相等，弱市场景断言 `meta['effective_min_total']==55.0` 且候选集合与 `generate_signals` 一致，未成熟（hist=50）与前收跌破 MA20 标的被一致排除。

### 补 3：评分来源记录 + 不兼容权重预设拒绝创建

- `models.py` 新增：`PIPELINE_FACTOR_POINTS`（动量/波动率因子关闭→趋势 30/确认 20/成交量 15）、`effective_pipeline_weights()`（归一化）、`weights_compatible_with_pipeline()`（容差 0.005）、`describe_score_source()`；`CURRENT_CONFIG_VERSION=2`。
- `create_account` 对 weights 与统一评分管线不兼容的预设**拒绝创建且不落库**（如保守型/激进型自定义权重）；创建成功时 config 写入 `config_version=2` 与 `score_source`（含 daily_scores 来源说明）；页面冻结参数表展示"评分来源/执行口径"。

### 补 4：老账户行为变更确认门闩

- store schema v4：`paper_accounts` 加 `behavior_ack_date`/`behavior_ack_config_json`（幂等迁移）。
- `resolve_execution_config()`：config_version<2 且未确认的老账户返回 None → `run_daily` 暂停（result kind `behavior_ack_required`，不交易、不估值、不标记已处理），页面显示 warning + "✅ 确认按新执行口径运行"按钮。
- `service.confirm_behavior_update(account_id, confirm_date)` 只写 ack 列：不改 `config_json`、已有冻结标的池不被覆盖；无冻结池的老账户不再回退读侧栏全局模块，无法可靠还原时提示用户确认而非猜测；重复确认、对新账户确认均拒绝。

### 第二轮验证证据（本会话实测）

| 套件 | 结果 |
|------|------|
| `tests/test_settings_consistency.py`（全量重写 36 项：F1~F8 + 补1×4 + 补2×3 + 补3×4 + 补4×7） | 36 passed |
| paper/live 12 套件 | 206 passed |
| UI 套件（venv，plotly 依赖） | 23 passed |
| B0.4 冻结 `test_b0_4_slippage.py` | 9 passed（138s，NAV 2,761,288.07 / 804 笔未动） |
| 数据准入 | 16 passed / 1 failed（`test_missing_data_antipattern` 基线既有） |
| 全量回归 | 429 passed（除 Codex `test_analysis_date_range_ui.py` 与两个基线既有失败） |
| 浏览器 2 项（单独跑） | 各 passed（185s / 224s） |

**测试环境坦白**：8501 端口曾长期被一台 Python3.10 旧 Streamlit 残留服务器（PID 64004，已终止）占用——历史浏览器测试可能一直打在旧代码上；浏览器 fixture 已改用 `sys.executable`，干净环境单跑通过。

**边界**：未提交未推送；B0.4 冻结未动；未修改真实持仓、数据库、个人预设；`app.py` 本轮触碰仅限 `_paper_trading_data_provider` 富化段（属 Codex 文件，需 Codex 复核）。
