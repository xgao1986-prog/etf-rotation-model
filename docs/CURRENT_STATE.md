# 当前工程现场

**最后更新**：2026-06-22  
**工作目录**：`D:\etf_rotation_model`  
**当前分支**：`feature/v1.3-regime-research`  
**当前版本**：v1.2.3
**当前研究提交**：`e206f47`（v1.3 Step 2: B0.4 market regime incremental value diagnosis）  
**发布锚点**：v1.2.3-b0.4 → 5e8eb78  
**正式基线**：B0.4（见 `docs/B0_BASELINE_LOCK.md`）  
**数据截止**：2026-06-18  

> 当前 HEAD 以 `git status` 为准。旧分支 `feature/v1.2.1-regime-adaptive` 保留，不删除、不重置、不强推。标签 `v1.2.3-b0.4` 保持不动。

---

## 1. 当前结论

- B0.4 已正式冻结，取代已废止的 B0.3。
- B0.3 使用的尾部行情不完整：2026-06-08 至 2026-06-12 大部分 ETF 和沪深300数据缺失。
- 受控 A/B 实验证明：排除补齐数据可复现 B0.3；使用完整数据得到 B0.4。差异来自数据补齐，不是策略或历史代码变化。
- B0.4 与 B0.3 的策略规则完全相同，仅数据版本不同。
- `app.py` 已支持动量/波动率因子总开关；默认关闭时识别为标准 B0-18，开启或修改关键参数后识别为自定义实验。
- **B0.4 单变量滑点敏感性测试 v2 已完成**：
  - 0bp 完美复现 B0.4（NAV=2,761,288.07，交易804笔）。
  - v2 修正规划阶段使用滑点价（`sell_prices`/`buy_prices`），所有 BUY 订单可执行，无静默跳过。
  - 3/5/10bp 夏普单调递减，无伪改善。
  - 8 项自动测试全部通过。
- **v1.3 Step 1: 换仓成本与有效性归因 已完成（含P1修复）**：
  - B0.4 的 `plan_rebalance_v2_5` **不实现**基于排名的替换逻辑。
  - 配置中 `replacement_score_gap=8` 存在，但**调仓引擎未引用该参数**。
  - 所有 "调出候选列表" 卖出（341笔，占84.2%）都是由于信号失效（跌破均线、total_score不足），而非排名竞争。
  - 纯排名替换（有匹配行业买入）：0 笔。
  - 无匹配退出（有BUY信号但无行业买入）：1 笔（原被误分类为PURE_RANKING，P1修复后归入UNMATCHED_EXIT）。
  - 交易分类勾稽：341 + 46 + 17 + 1 = 405 = 卖出合计 ✓；纯排名替换0 = 事件CSV行数 ✓。
  - 不满足进入 Step 2（测试 replacement_score_gap）的任何条件。
  - 不创建 replacement_score_gap 规则，不修改 B0.4。
- **v1.3 Step 2: B0.4 市场状态增量价值诊断 已完成**：
  - 自然择时已生效：强牛行业仓位 74.9% > 熊市 42.0%，差异 32.9 个百分点；总持仓强牛 85.4% > 熊市 59.1%。
  - 弱市超额为正：熊市策略超额 +17.6%（全区间），震荡期 +80.8%。
  - 状态分布：熊市 50.0%（701天）、弱牛 18.1%（236天）、震荡 16.4%（214天）、强牛 11.8%（154天）。
  - 状态切换 60 次，平均置信度 0.773。
  - 推荐方向：**证据不足，暂不推荐**。自然择时已覆盖三个方向的大部分价值（A=总仓位、B=买入门槛、C=防御比例），增量价值不确定。
  - 小样本警告：研究期强牛仅60天，年化高度膨胀；验证期震荡45天、弱牛71天同样样本不足。
  - 交付物：`scripts/v1_3_step2_b0_4_regime_diagnosis.py`、`reports/v1_3_step2_regime_diagnosis.md`、`reports/v1_3_step2_regime_stats.csv`。
  - 不修改 B0.4 策略、参数或冻结基线。
- **下一阶段**：暂不推荐进入实验。如需进一步探索，方向 C（仅调整防御比例）的增量空间相对最大，但需更多数据支撑。等待 WorkBuddy 定向复审。

---

## 2. B0.4 冻结口径

### ETF池与规则

- 18只ETF：16只行业ETF + 黄金ETF `518880.SH` + 国债ETF `511010.SH`。
- 每周四调仓。
- 最多持有5只，单只新建仓上限20%。
- 固定止损 -8%。
- 行业ETF优先，防御资产承接剩余资金和槽位。
- 动量因子关闭：`momentum_factor_enabled=False`。
- 波动率因子关闭：`volatility_factor_enabled=False`。
- 市场状态模块不参与交易。
- 佣金双向0.03%，最低5元，100股整手。
- 当前基线不计滑点。

### 执行口径

- 信息日期：前一有效交易日收盘数据。
- 成交记录日期：下一有效交易日。
- 普通信号以次日开盘价执行；等价表述为“T日收盘信号，T+1开盘交易”。
- 关键指标使用 `shift(1)`，已通过同日数据扰动测试。
- 止损采用“开盘检查并按开盘成交”的预置止损单假设。

### 冻结指标

| 指标 | B0.4 |
|------|------|
| 最终NAV | 2,761,288.07 |
| 总收益 | 176.13% |
| 年化收益 | 16.68% |
| 夏普比率 | 0.8816 |
| 最大回撤 | -17.75% |
| 交易次数 | 804 |
| 买入次数 | 399 |
| 卖出次数 | 405 |
| 调仓次数 | 337 |

---

## 3. 数据版本与准入状态

### 冻结快照

- 数据快照：`data/snapshots/B0_4_candidate_data_20260621_210815.csv`
- 元数据：`data/snapshots/B0_4_candidate_metadata_20260621_210815.json`
- 指标：`data/snapshots/B0_4_candidate_metrics_20260621_203453.json`
- 数据库 SHA-256：`e0cf29931df02a9ba3df5ca465804ee0ee70f120f800ed01ccad744901b58ef0`
- 19只标的数据集 SHA-256：`1ecf8f66f8ac51bb0964971f1e73a46cc13e1e9685f0fda569bd655c9bebd721`

### 数据准入检查 v1.1

- 最近14个交易日：18只ETF及沪深300完整。
- 多数据源拼接检查：无已识别断档、重复或异常价格跳变。
- `anomalous_internal=0`：数据库首个有效记录之后没有异常内部缺口。
- 7只ETF存在已知早期覆盖不足，共约2,617个交易日，准入状态为 WARN，不是完全无缺口：
  - `159530.SZ`
  - `159697.SZ`
  - `159865.SZ`
  - `159996.SZ`
  - `515230.SH`
  - `516110.SH`
  - `516160.SH`
- 准入 `exit_code>=2` 时，B0基线入口必须终止回测；`exit_code=1` 时允许运行并保留警告。
- 准入状态为 PASS 或 WARN 时均可生成带 SHA-256 的数据快照。

---

## 4. 已完成的新鲜验证

- `tests/test_b0_data_admission.py`：8 passed。
  - 成熟ETF交易日缺失能够触发准入失败。
  - mock 准入失败会抛出 `RuntimeError`，且 `BacktestEngine.run` 不会执行。
  - 权威上市日与数据库首日分离。
  - 已知覆盖不足与异常内部缺口分开统计。
  - 快照元数据两个 SHA-256 字段均为64位十六进制。
- `tests/test_app_b0_signature.py`：6 passed。
  - 默认B0.4签名匹配。
  - 开启动量或波动率因子后签名偏离。
  - 修改止损、权重或最大持仓数后签名偏离。
- `tests/test_b0_4_slippage.py`：8 passed。
  - 0bp 完美复现 B0.4（NAV=2,761,288.07，交易804笔）。
  - 3bp 下无 BUY 订单被静默跳过（规划阶段使用滑点价）。
  - 3bp 买入价格全部高于 0bp（成交价上调）。
  - 3bp 卖出价格全部低于 0bp（成交价下调）。
  - 0bp>3bp>5bp>10bp NAV 单调递减。
  - STOP_LOSS 独立统计，不与 SELL 混用。
  - 年化使用引擎 CAGR，非总收益/年数。
  - 每日现金+持仓市值=NAV 恒等式通过。
- `python -m py_compile app.py`：通过。
- A/B数据补齐实验：
  - 完整数据：NAV 2,761,288.07，804笔交易。
  - 排除补齐数据：NAV 2,809,091.21，801笔交易。
  - 首次NAV分歧：2026-06-08。

---

## 5. 实际工作区状态

当前分支已同步远端，但工作区不是干净状态：

- 已修改：`docs/CURRENT_STATE.md`
  - 本次 v1.3 Step 1 / Step 2 交接摘要更新，尚未提交。
- 已修改：`docs/CHANGES.md`
  - v1.3 Step 1 / Step 2 记录，尚未提交。
- 已修改：`src/backtest.py`
  - nav_records 增加 `industry_value` / `defense_value`（不改变交易逻辑）。
- 已修改：`reports/ab_test_data_fill_impact.md`
  - 仅文件末尾换行差异；不要在无关任务中处理。
- 未跟踪：`reports/etf_rotation_public_resources_research.md`
- 未跟踪：`scripts/phase7_1_survivorship_bias_audit_v2.py`
- 新增：`src/market_regime.py`
  - 市场状态检测模块（从 D:/ 同步到 workspace）。
- 新增：`scripts/v1_3_step1_replacement_attribution.py`
- 新增：`reports/v1_3_step1_replacement_attribution.md`
- 新增：`reports/v1_3_step1_replacement_events.csv`
- 新增：`reports/v1_3_step1_replacement_summary.csv`
- 新增：`scripts/v1_3_step2_b0_4_regime_diagnosis.py`
- 新增：`reports/v1_3_step2_regime_diagnosis.md`
- 新增：`reports/v1_3_step2_regime_stats.csv`

这些文件均视为用户已有研究材料。不得删除、移动、覆盖、暂存或提交，除非当前任务明确要求。

---

## 6. 未解决问题

1. 7只ETF在权威上市日至数据库首日之间存在早期历史覆盖不足；B0.4接受此限制，但必须持续披露。
2. 回测尚未实现正式滑点参数；当前B0.4只计佣金。
3. 开盘止损成交是假设，极端行情和流动性不足时可能产生额外滑点。
4. Pandas仍有 `DataFrameGroupBy.apply` 等 FutureWarning，不影响当前结果，暂不作为策略任务处理。
5. 数据更新流程今后必须先通过准入检查和哈希快照，不能直接追加后冻结基线。

---

## 7. 下一步唯一任务

等待确认是否进入方向 B 实验设计（状态→仓位映射规则）。所有新实验必须以 B0.4（0bp）为对照，一次只改变一个变量。

---

## 8. 禁止修改范围

下一任务不得修改：

- B0.4 ETF池、评分因子和阈值。
- 动量/波动率开关的冻结状态。
- 调仓日、最大持仓数、单只上限、止损及防御规则。
- 调仓规划器 v2.5 的业务语义。
- 冻结数据快照、SHA-256、B0.4指标和 `docs/B0_BASELINE_LOCK.md`。
- 已封存样本外结果。
- 32只ETF池、申万行业数据、机器学习、宏观或国际市场信号。
- 工作区中与当前任务无关的修改和未跟踪文件。

所有增强必须采用独立开关或实验脚本，以B0.4为对照，一次只改变一个变量。

---

## 9. 新线程恢复方式

依次读取：

1. `AGENTS.md`
2. `docs/CURRENT_STATE.md`
3. `docs/DECISIONS.md`
4. `git status --short --branch`

恢复后只继续“v1.3 Step 1 完成，等待确定下一个单变量实验”，不要重新展开历史研究。
