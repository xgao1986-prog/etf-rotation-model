# 请 Trae 定向复审：ROUND3 六项收口（评分一致性 / 确认闸门 / 回归复现）

## 一、背景与授权边界（请先读）

项目 `D:\etf_rotation_model`，分支 `feature/paper-trading-ui-v0.3`（HEAD bbbd466，本轮**未提交未推送**）。

你上一轮（ROUND3）对真实价格架构 v3 提出 6 项意见，用户要求逐项收口后交你定向复审：

1. **修 main.py 三处评分入口**：真实价格调整必须接入 main.py 的信号生成路径，并验证与页面在相同配置、相同数据下得到一致评分
2. **核实全部 9 条份额调整记录**：自动推算只能生成待确认记录，不能直接改变账户持仓
3. **以基金正式公告确认比例和生效日**：保存依据，不能直接使用价格比值推算出的近似比例
4. **516160 的 0.30908 核实前禁止自动执行**：不能仅添加注释后继续启用
5. **补测试**：持仓跨拆分、拆分当日卖出及止损检查，确认不会误止损、错误调整份额或重复调整
6. **B0.4 冻结回归**：使用冻结数据重新运行，确认仍复现原有净值和 804 笔交易，不得修改预期结果或放宽误差

**授权边界**：只检查上述问题和本次改动影响，不重复扩大研究范围。允许修运行库数据；不允许动 B0.4 冻结快照/冻结指标/交易规则；备份留痕。

---

## 二、意见一：main.py 评分入口修复 + 一致性验证

**修复**：`main.py cmd_signal` 三处评分入口（原 177/200/215 行）全部包上 `apply_adjustment_for_scoring`：

- 行业 ETF：`engine.calculate_indicators_and_scores(apply_adjustment_for_scoring(ticker_df))`
- 宽基补仓：`engine.calculate_fallback_equity_score(apply_adjustment_for_scoring(ticker_df))`
- 防御资产：`engine.calculate_defense_score(apply_adjustment_for_scoring(ticker_df))`

`calculate_indicators_and_scores` 与页面用的 `calculate_total_score` 均为 `calculate_scores` 的等价入口（前者是后者的直接包装），评分函数本体完全相同。

**一致性验证**（`scripts/verify_score_consistency.py`）：

- 方法：正式库复制两份临时副本，一份走 `main.cmd_signal` 完整评分路径，一份走 `app._recalculate_scores`，同配置（`STRATEGY_CONFIG` 默认值，页面滑条默认值同源）同数据，逐 ticker+date 逐列比对 13 个评分列
- 结果：**两路径各产出 33,895 行，全部完全一致**（差异 < 1e-9，NaN 模式一致）

**顺带修复一个既有 bug**：`main.py cmd_signal` 第 285 行用 `build_config()` 但从未导入，直接运行 `python main.py signal` 会在信号打印前抛 `NameError`。已补 `from config import build_config`。

---

## 三、意见二/三/四：corporate_actions 确认闸门 + 9 条记录官方核实

### 3.1 确认闸门（硬规则，代码级强制）

`src/database.py`：

- `corporate_actions` 表新增 `status TEXT NOT NULL DEFAULT 'pending'` + `evidence TEXT` 列（老库 `ALTER TABLE` 自动升级）
- `save_corporate_actions` 缺省 status='pending'——**自动检测/推算的记录只能以 pending 入库**
- 新增 `confirm_corporate_actions(ticker, effective_date, ratio, evidence)`：以公告为准修正 ratio、置 confirmed、存公告标题+URL，**是唯一产生 confirmed 记录的入口**
- `get_corporate_actions(..., status=None)` 支持状态过滤

`src/paper_trading/runner.py _apply_corporate_actions`：

- **仅 `status=='confirmed'` 可调整持仓**；status 缺省按 pending 处理（老调用方兼容 = 不自动执行）
- pending 记录：禁止调整持仓，加入 blocked 集合并留痕 `CORPORATE_ACTION` skipped 记录
- blocked 标的当日**跳过止损检查**（`_check_stop_loss(skip_tickers=...)`）并留痕 `skipped: pending corporate action`——未确认拆分的 -50% 价差不得误触发止损
- 同标的单日 `applied` 集合防重复调整；ratio 非法（≤0 或无法解析）时不调整也不止损，留待人工

### 3.2 九条记录全部以基金正式公告核实并 confirmed

| ticker | effective_date | ratio（官方） | 依据 |
|---|---|---|---|
| 159928.SZ | 2021-06-25 | 4.0 | 汇添富基金 fhsp 公告页 |
| 512000.SH | 2025-08-04 | 2.0 | 华宝基金 fhsp 公告页 |
| 512010.SH | 2021-06-28 | **4.0** | 易方达基金公告（自动推算 3.905 被修正） |
| 512480.SH | 2021-03-29 | **2.0** | 国联安基金公告（自动推算 1.966 被修正） |
| 512480.SH | 2026-07-03 | 2.0 | 上交所公告（7-02 登记 / 7-03 除权） |
| 512800.SH | 2025-07-07 | 2.0 | 华宝基金 fhsp 公告页 |
| 515880.SH | 2026-02-03 | 3.0 | 国泰基金公告（折算日 2026-02-02） |
| 515880.SH | 2026-07-06 | 2.0 | 上交所公告 PDF 原文（7-03 登记 / 7-06 除权） |
| 516160.SH | 2024-09-18 | **0.30912005** | 南方基金**份额合并**公告（原推算 0.30908 被修正，8 位小数） |

三条实证坐实"价格比值推算不可直接用于持仓调整"：512010 偏差 2.4%（3.905 vs 4.0）、512480-2021 偏差 1.7%（1.966 vs 2.0）、516160 偏差 0.013%（0.30908 vs 0.30912005）。

### 3.3 意见四专项：516160

已核实为**份额合并**（非分拆）：南方基金公告合并比例 0.30912005，权益登记日 2024-09-13、除权日 2024-09-18。已按官方比例修正入库并 confirmed——**不是"加注释继续启用"，而是比例修正 + 状态确认双落实**。

---

## 四、意见五：场景测试补齐

`tests/test_real_price_adjustment.py` 从 11 项扩到 **15 项全过**。新增 `TestCorporateActionConfirmationGate` 5 项：

1. **拆分当日卖出**：7/2 持仓 1000 股@5.40 → 7/3 盘前 confirmed 拆分调整为 2000 股@2.70 → 执行昨日低分卖出信号，按真实开盘价 2.70 卖出 2000 股，现金回收 = 2000×2.70 − 佣金（市值不腰斩）
2. **pending 不自动执行 + 不误止损**：status 缺省（=pending）时持仓不变（仍 1000 股@5.40），-50% 价差不触发止损，双留痕（CORPORATE_ACTION + STOP_LOSS skipped）
3. **重复调整防护**：同日传入两条 confirmed 记录只应用一次（2000 股而非 4000 股）
4. **份额合并（ratio<1，516160 场景）**：股数 ×0.30912005、成本 ÷0.30912005、市值与 NAV 不变、不触发止损

既有用例同步修正：所有传 corporate_actions 的用例补 `status='confirmed'`（否则按新闸门被拦）。

**顺带修复一个真 bug**：`src/paper_trading/store.py` 保存 skipped 记录的 `skipped_id` 用微秒时间戳，同一条 run 里两条 `order_id=None` 的记录（pending 闸门场景必现）会撞 UNIQUE 约束——加了序号后缀。

---

## 五、意见六：B0.4 冻结回归

- 入口：`tests/test_b0_4_slippage.py`（冻结快照 SHA-256 `dbdb041d…` 校验通过，未动快照）
- 结果：**9 passed**。`test_0bp_matches_baseline` 精确复现 NAV = 2,761,288.07（±1.0）+ **804 笔交易**，未修改任何预期值或误差带
- 机制保障：冻结快照 CSV 只喂 7 列（无 adj_close），`apply_adjustment_for_scoring` 恒等返回，评分输入与 B0.4 冻结时完全一致

---

## 六、全量回归

`pytest tests/ -q`：**383 passed, 2 failed**——两个失败均为基线既有问题（`test_missing_data_antipattern`、`test_v1_3_step6 regime 分布`），已在 HEAD 干净副本复证与本轮改动无关。无新增失败。

---

## 七、改动文件清单（本轮，未提交）

| 文件 | 改动 |
|---|---|
| `main.py` | 3 处评分入口包调整价 + 补 `build_config` 导入（既有 bug） |
| `src/database.py` | corporate_actions status/evidence 列 + confirm_corporate_actions + status 过滤 |
| `src/paper_trading/runner.py` | 确认闸门版 `_apply_corporate_actions` + `_check_stop_loss(skip_tickers)` |
| `src/paper_trading/store.py` | skipped_id 加序号（撞 UNIQUE 修复） |
| `tests/test_real_price_adjustment.py` | 11 → 15 项（新增确认闸门 5 项 + 既有用例补 confirmed） |
| `scripts/verify_score_consistency.py` | 新增：main vs 页面评分一致性验证脚本 |

正式库 corporate_actions 表 9 条记录已更新为官方比例 + confirmed + evidence（含公告标题与 URL）。

---

## 八、请复审的问题清单

1. **main.py 三处评分入口**是否与页面严格同口径（调整价驱动、真实价不入境）？
2. **确认闸门**是否有绕过路径（除 `confirm_corporate_actions` 外是否还有其他方式让记录变 confirmed）？
3. **pending 当日跳过止损**的语义是否恰当（会不会被利用来永久逃避止损）？
4. **516160 合并比例 0.30912005** 的处理（ratio<1 时 shares×ratio、cost÷ratio）是否与南方基金公告的实际份额变动一致？
5. **拆分当日卖出**测试中，信号是拆前一日低分准备的、拆分日盘前调整后再执行——这个时序是否与你的预期一致？
