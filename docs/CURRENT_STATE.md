# 当前工程现场

**最后更新**：2026-07-05
**工作目录**：`D:\etf_rotation_model`
**当前分支**：`feature/paper-trading-ui-v0.3`
**当前版本**：v1.2.3（虚拟盘 v0.3）
**当前 HEAD**：以 `git status` 为准
**发布锚点**：v1.2.3-b0.4 → 5e8eb78
**正式基线**：B0.4（见 `docs/B0_BASELINE_LOCK.md`）
**数据截止**：2026-06-29

> 旧分支 `feature/v1.2.1-regime-adaptive` 保留，不删除、不重置、不强推。标签 `v1.2.3-b0.4` 保持不动。

---

## 1. 当前结论

- **B1 Holding Stability A/B 实验：已终审收口，不升级 B1**
  - A/B/C 均未通过全部标准（夏普均低于 B0.4 的 0.91，回撤恶化）
  - 结论：卖出缓冲减少换仓有效，但风险调整后表现变差；不再继续调参
- **Universe Time-Consistency Audit：已收口**
  - 7 只 ETF 已知早期覆盖不足，已量化并披露
  - 四组对照回测完成，结论：不足以推翻 B0.4
- **Paper Trading v0.3：Task 1~7 已完成并推送，Task 8 文档收口完成**
  - `src/paper_trading/ui.py` 提供 "虚拟盘" 独立页面：账户总览、批量创建对比账户、创建影子账户、今日运行、待确认订单、账户详情与策略对比
  - 对比账户：由预设批量创建，同批次共享 group_id、初始资金和起始日期；运行后自动生成成交和 NAV 记录
  - 影子账户：手动录入名称、总资产、现金、持仓和起始日期；运行后生成 PENDING 影子订单，不自动改变现金和持仓
  - 影子订单必须由用户确认：支持确认成交、标记未执行、取消；拒绝/取消必须填写原因
  - `app.py` 已集成该页面作为第 7 个页签，并提供行情/评分数据入口
  - 数据完整性检查：18 只 ETF 开盘/收盘价必须存在、有限且大于 0；total_score 必须存在且有限；日期不一致也阻止运行
  - 账户总览展示年化收益、夏普、最大回撤、Calmar、胜率、换手、佣金
  - 账户详情展示历史订单、净值曲线、回撤曲线、持仓明细
  - B0.4 策略对比仅在同一非空批次（同 group_id + start_date）的对比账户内查找参照，用于并行虚拟盘跟踪
  - 修复账户总览 DataFrame 混合类型导致的 Streamlit Arrow 序列化失败，统一转换为字符串列
  - 为兼容 Streamlit 1.41 与 1.58，将 `width='stretch'` 统一改为 `use_container_width=True`
  - 账户创建表单反套用 `st.form`，避免 headless Chromium 中控件状态在 rerun 前丢失
  - 将账户类型切换从 `st.radio` 改为 `st.selectbox`，避免 headless Chromium 中 radio 无法交互
  - 运行结果写入 `st.session_state` 后再 `st.rerun()`，确保成功/失败详情在 rerun 后仍可见
  - Task 7 浏览器验收为纯 UI 点击流程：创建对比账户 → 创建影子账户 → 今日运行 → 生成影子订单 → 确认成交 → 查看账户详情，并验证数据库产生新的 NAV 记录、PENDING/FILLED 状态订单以及 STOP_LOSS 成交记录
  - 本轮新鲜验证：182 项相关自动化测试通过（含 paper/store/service/runner/ui/metrics/ui_service/account_admin/preset/live/app_b0_signature 等）；1 项 Playwright 浏览器验收测试通过
  - 不修改 B0.4 策略、参数或冻结数据
- **实盘助手 v0.2：已交付骨架**
  - 持仓管理、止损检查、调仓建议、成交记录（不自动下单）
  - 22 项自动化测试全部通过
- **v1.3 Step 1~10：已完成，observer-only 结论**
  - Step 1（换仓成本归因）：`plan_rebalance_v2_5` 不实现排名替换，不创建 replacement_score_gap 规则
  - Step 2（市场状态诊断）：自然择时已生效，但方向判断撤回 A/B/C 推荐
  - Step 3（信号失效退出）：震荡往返比例两期均 >15%，支持 holding stability 实验证据；误杀卖飞不能作为独立证据
  - Step 4（组合结构拆解）：方案 B 夏普/回撤改善，但收益未超 B0.4，不升级基线
  - Step 5（动态广度）：当前证据不足，继续使用固定 B0.4 结构
  - Step 6（动态第5槽位）：未通过预注册标准，不得升级 B0.4
  - Step 7（正交拆解）：标准7通过，但预注册标准未全部通过，不升级基线
  - Step 8（B0.4 vs D）：D 为风险换收益型候选，不满足明确改善标准
  - Step 9（夏普优先复核）：C 为防守候选，但无方案在三维度同时优于 A；B0.4 仍保留
  - Step 10（市场形态对比）：仅震荡状态下 D 满足跨期夏普优于 A，但样本小
  - 所有步骤不修改 B0.4 策略、参数或冻结基线

历史细节见 `docs/CHANGES.md` 和 `reports/` 目录。

---

## 2. B0.4 冻结口径

### ETF 池与规则

- 18 只 ETF：16 只行业 ETF + 黄金 `518880.SH` + 国债 `511010.SH`。
- 每周四调仓。最多持有 5 只，单只新建仓上限 20%。固定止损 -8%。
- 行业 ETF 优先，防御资产承接剩余资金和槽位。
- 动量/波动率因子关闭；市场状态模块不参与交易。
- 佣金双向 0.03%，最低 5 元，100 股整手。当前基线不计滑点。

### 执行口径

- 信息日期：前一有效交易日收盘数据。成交记录日期：下一有效交易日。
- 普通信号以次日开盘价执行；止损采用"开盘检查并按开盘成交"的预置止损单假设。
- 关键指标使用 `shift(1)`，已通过同日数据扰动测试。

### 冻结指标

| 指标 | B0.4 |
|------|------|
| 最终 NAV | 2,761,288.07 |
| 总收益 | 176.13% |
| 年化收益 | 16.68% |
| 夏普比率 | 0.8816 |
| 最大回撤 | -17.75% |
| 交易次数 | 804 |

---

## 3. 数据版本与准入状态

### 冻结快照

- 数据快照：`data/snapshots/B0_4_candidate_data_20260621_210815.csv`
- 数据库 SHA-256：`e0cf29931df02a9ba3df5ca465804ee0ee70f120f800ed01ccad744901b58ef0`
- 19 只标的数据集 SHA-256：`1ecf8f66f8ac51bb0964971f1e73a46cc13e1e9685f0fda569bd655c9bebd721`

### 数据准入检查 v1.1

- 最近 14 个交易日：18 只 ETF 及沪深300完整。
- 7 只 ETF 存在已知早期覆盖不足（约 2,617 个交易日），准入状态为 WARN：
  `159530.SZ`、`159697.SZ`、`159865.SZ`、`159996.SZ`、`515230.SH`、`516110.SH`、`516160.SH`
- 准入 `exit_code>=2` 时，B0 基线入口必须终止回测；`exit_code=1` 时允许运行并保留警告。

---

## 4. 已完成的新鲜验证

- `tests/test_b0_data_admission.py`：8 passed（准入失败触发、mock 验证、权威上市日分离、SHA-256 格式）
- `tests/test_app_b0_signature.py`：13 passed（默认签名、因子开关偏离、参数修改偏离）
- `tests/test_b0_4_slippage.py`：9 passed（冻结快照版）。0bp 复现 NAV=2,761,288.07，交易 804 笔；3bp 无静默跳过；NAV 单调递减；SHA-256 哈希校验通过。不再依赖当前数据库。
- `tests/test_v1_3_step6_dynamic_fifth_slot.py`：16 passed（LOO、配置、NaN 回退、regime 标签、B/C 勾稽）
- `tests/test_live_trading.py`：24 passed（持仓、校验、止损、交易计划、报告生成）
- `tests/test_live_daily_workflow.py`：6 passed（每日工作流、纸面日志）
- `tests/test_paper_trading_runner.py`：37 passed（原子成交、满仓手续费、重复运行、缺价、冲突、防御资产填充、T+1 日期分离、超卖保护、交易日历、开盘收盘分离、止损清仓、唯一最终状态、订单状态 FILLED/SKIPPED/CANCELLED）
- `tests/test_paper_trading_models.py`：4 passed（配置哈希、现金启动验证、导入验证）
- `tests/test_paper_trading_store.py`：4 passed（schema、重复事件、配置不可变）
- `tests/test_paper_trading_service.py`：6 passed（账户创建、NAV 勾稽、重复订单、对账）
- `tests/test_paper_account_admin.py`：1 passed（命令行创建/列示）
- 本轮 Task 8 新鲜验证结果：
  - `python -m pytest tests/test_paper_trading_models.py tests/test_paper_trading_store.py tests/test_paper_trading_service.py tests/test_paper_trading_runner.py tests/test_paper_trading_ui.py tests/test_paper_trading_metrics.py tests/test_paper_trading_ui_service.py tests/test_paper_account_admin.py tests/test_strategy_presets.py tests/test_preset_loading.py tests/test_app_b0_signature.py tests/test_live_trading.py tests/test_live_daily_workflow.py -q`：**182 passed**
  - `.venv-browser-test/Scripts/python -m pytest tests/test_paper_trading_browser.py -v -s`：**1 passed**
  - `python -m py_compile app.py src/paper_trading/ui.py tests/test_paper_trading_browser.py`：通过
  - `git diff --check`：通过
- `tests/test_paper_trading_ui.py`：22 passed（页面渲染、资金不变传递、无策略参数编辑器、运行按钮触发、rerun 后结果保留、日期/价格/评分异常阻止运行、价格空值/零/负值阻止运行、评分空值阻止运行、影子订单需确认、确认/拒绝/取消调用服务、拒绝需原因、总览展示绩效指标、详情展示订单与曲线、同一批次 B0.4 对比、排除无批次影子账户）
- `tests/test_paper_trading_browser.py`：1 passed（纯 UI 点击完成对比账户/影子账户创建、运行、确认成交，并验证数据库变化）
- `tests/test_paper_trading_metrics.py`：11 passed（含首日下跌回撤）
- A/B 数据补齐实验：完整数据 NAV=2,761,288.07；排除补齐 NAV=2,809,091.21。首次分歧：2026-06-08。

---

## 5. Task 7 浏览器验收

- 实际启动 `streamlit run app.py` 打开 "虚拟盘" 页签，验证创建账户、运行、影子订单确认等真实交互
- 已通过 Playwright 浏览器验收测试：纯 UI 点击完成创建对比账户、创建影子账户、今日运行、生成影子订单、确认成交、查看账户详情，并验证数据库变化
- 未实际打开页面前不得声称浏览器工作流完成

## 6. Paper Trading v0.3 资金勾稽与已知限制

### 资金勾稽（来自本轮真实测试）

- **现金 + 持仓市值 = 总资产**：`tests/test_paper_trading_runner.py::TestUniqueFinalState::test_single_nav_per_day` 每日验证。
- **佣金已计入现金变化**：`tests/test_paper_trading_service.py::test_confirm_updates_cash_and_positions` 验证，买入 2 笔共 300 元市值产生 10 元佣金，现金从 1,000,000 变为 999,690。
- **买入不得造成负现金**：`tests/test_paper_trading_runner.py::TestUniqueFinalState::test_cash_never_negative` 验证。
- **卖出不得超过实际持仓**：`tests/test_paper_trading_runner.py::TestOversell::test_oversell_skipped` 验证。

### 已知限制

- 尚无结束账户功能
- 尚无永久删除账户功能
- 尚未定时自动运行
- 不连接券商、不自动实盘下单

## 7. 下一阶段

- 版本说明补全
- Paper Trading v0.3.1 账户生命周期（结束账户、删除账户等）

---

## 8. Phase 2 v0.2.2 收口记录

- **交易日历修复**：2026 年端午节从 `2026-06-19/22/23` 修正为 `2026-06-19/20/21`（周六日虽非交易日，但显式标注假期）。
- **订单状态字段**：订单执行流程现在为每笔记名订单写入 `status`：`PENDING` / `FILLED` / `SKIPPED` / `CANCELLED`，便于重跑审计。
- **验证**：115 项 paper/live/rebalance 相关测试通过，30 个现有测试无回归。

**Phase 2 开始前的前置任务（已完成）**：
- B0.4 冻结快照测试口径修复：`test_0bp_matches_baseline` 使用冻结快照，运行前校验 SHA-256

---

## 9. 禁止修改范围

- B0.4 ETF 池、评分因子和阈值。
- 动量/波动率开关的冻结状态。
- 调仓日、最大持仓数、单只上限、止损及防御规则。
- 调仓规划器 v2.5 的业务语义。
- 冻结数据快照、SHA-256、B0.4 指标和 `docs/B0_BASELINE_LOCK.md`。
- 已封存样本外结果。
- 工作区中与当前任务无关的修改和未跟踪文件。

所有增强必须采用独立开关或实验脚本，以 B0.4 为对照，一次只改变一个变量。

---

## 10. 新线程恢复方式

依次读取：

1. `AGENTS.md`
2. `docs/CURRENT_STATE.md`
3. `docs/DECISIONS.md`
4. `git status --short --branch`

---

**虚拟实盘状态**：Phase 2 每日运行流程、止损检查、每周调仓、模拟成交已完成；Task 6 Streamlit 虚拟盘页面已提交。

**版本口径**：
- 策略版本：v1.2.3（B0.4 基线）
- 调仓引擎：v2.5
- 虚拟实盘：v0.3（Phase 2 运行流程 + Streamlit 虚拟盘页面 + 影子订单确认流程）
