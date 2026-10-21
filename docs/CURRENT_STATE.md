# 当前工程现场

**最后更新**：2026-07-06
**工作目录**：`D:\etf_rotation_model`
**当前分支**：`feature/paper-trading-ui-v0.3`
**当前版本**：v1.2.3（虚拟盘 v0.3.1）
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
- **Paper Trading v0.3.1：账户生命周期功能已完成，终审修复已应用**
  - `paper_accounts` 表升级到 Schema v3，新增 `is_hidden`、`is_deleted`、`closed_at`、`deleted_at`、`lifecycle_reason` 字段
  - Store 层新增 `update_account_lifecycle`、`soft_delete_account`、`restore_account`、`permanently_delete_account`
  - Service 层新增 `close_account`、`reopen_account`、`hide_account`、`unhide_account`、`soft_delete_account`、`restore_account`、`permanently_delete_account`
  - Runner 在每日运行入口**先于幂等检查**拒绝已结束（`ENDED`）或已软删除（`is_deleted=1`）账户；即使当天已有运行记录也不会绕过
  - UI Service `list_account_summaries` 支持 `include_hidden`/`include_deleted` 过滤，`run_accounts` 跳过已结束/已删除账户并返回 `skipped` 分区
  - UI 页面顶部增加"显示已隐藏账户"和"显示已删除账户"开关；账户总览表格下方为每个账户提供操作按钮：结束/重新打开、隐藏/显示、删除/恢复、彻底删除
  - 永久删除前必须先软删除；确认按钮仅在输入完整账户名称并勾选二次确认后才启用；service 层继续保留同名/确认校验
  - 永久删除会级联清理 `paper_positions/orders/trades/daily_nav/runs/signals/skipped` 子表数据
  - `restore_account` 恢复账户到 `READY` 时同时清空 `closed_at` 和 `deleted_at`
  - 新增 `tests/test_paper_trading_lifecycle.py`：21 项测试覆盖 schema v3、关闭/重新打开、隐藏/显示、软删除/恢复、永久删除级联、列表过滤、runner 拒绝（含已处理日期场景）、UI service 跳过、restore 字段一致性
  - Paper Trading v0.3 原有功能保持不变：账户总览、创建账户、今日运行、待确认订单、账户详情与策略对比
  - `docs/DECISIONS.md` 新增 D-015 记录账户生命周期长期规则
- **数据抓取 AKShare 超时问题已修复，新增 iFinD CSV 自动导入 fallback**
  - 根因：`data_fetcher.update_latest_data()` 对每只 ticker 逐个请求东方财富接口，批量调用下大量 `Read timed out. (read timeout=15)`，导致全部失败、无数据更新
  - `src/data_fetcher.py`：`DataFetcher` 默认 `timeout=3s`、`max_retries=1`；新增 `_fetch_with_timeout` 使用 `ThreadPoolExecutor` 给 AKShare 调用加硬超时（AKShare 内部 `timeout` 参数实测不生效）；新增 `health_check()` 在批量拉取前做快速预检，失败立即返回
  - `fetch_latest` 改为只拉取数据库最新日期次日到今天的窗口；`update_latest_data` 返回包含 `saved_count`/`failed_tickers`/`status`/`message` 的字典，区分"未安装"/"全部失败"/"部分成功"/"成功"
  - `src/data_fetcher.py` 新增 `import_local_ifind_csvs()`：自动扫描 `data/ifind_*.csv` 并批量导入，复用现有 `import_from_kimi` 列名映射
  - `app.py` 数据管理页：顶部显示 AKShare 可用状态、本地 iFinD CSV 数量、数据库最新日期；"更新最新行情"按钮明确标注"AKShare，可能较慢"；"自动导入本地 iFinD CSV"在有 CSV 时高亮为主按钮；失败后明确提示使用 iFinD CSV 导入
  - 新增 `tests/test_data_fetcher.py`：4 项测试覆盖 AKShare 全部失败、部分成功保存、本地 iFinD CSV 自动导入、无文件场景
  - 实测 `update_latest_data(db=db)` 在 AKShare 不可用时约 **3.7 秒**内返回并提示 fallback，不再出现 300 秒超时
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
- `tests/test_paper_trading_lifecycle.py`：21 passed（schema v3、关闭/重新打开、隐藏/显示、软删除/恢复、永久删除级联/回滚/名称校验/确认校验、PENDING 订单取消、v2→v3 迁移、列表过滤、runner 拒绝（含已处理日期场景）、restore 字段一致性、UI service 跳过）
- `tests/test_paper_account_admin.py`：1 passed（命令行创建/列示）
- 数据抓取修复验证：
  - `python -m pytest tests/test_data_fetcher.py -v`：**4 passed**
  - `python -m py_compile app.py src/data_fetcher.py tests/test_data_fetcher.py`：通过
  - `git diff --check -- app.py src/data_fetcher.py tests/test_data_fetcher.py docs/CHANGES.md docs/CURRENT_STATE.md`：通过
  - 实际调用 `update_latest_data(db=db)`：AKShare 不可用时约 **3.7 秒**返回并提示 iFinD fallback
- Paper Trading 专项（本次 v0.3.1 终审修复验证）：
  - `python -m pytest tests/test_paper_trading_service.py tests/test_paper_trading_store.py tests/test_paper_trading_ui_service.py tests/test_paper_trading_runner.py tests/test_paper_trading_ui.py tests/test_paper_trading_lifecycle.py -q`：**129 passed**
  - `.venv-browser-test/Scripts/python -m pytest tests/test_paper_trading_browser.py -v -s`：**2 passed**（含永久删除按钮禁用/启用状态检查）
  - `python -m py_compile app.py src/paper_trading/ui.py src/paper_trading/store.py src/paper_trading/service.py src/paper_trading/runner.py src/paper_trading/ui_service.py tests/test_paper_trading_lifecycle.py tests/test_paper_trading_browser.py`：通过
  - `git diff --check -- CURRENT_VERSION_NOTE.md README.md VERSION_HISTORY.md docs/CHANGES.md docs/CURRENT_STATE.md docs/DECISIONS.md src/paper_trading/runner.py src/paper_trading/service.py src/paper_trading/store.py src/paper_trading/ui.py src/paper_trading/ui_service.py tests/test_paper_trading_browser.py tests/test_paper_trading_lifecycle.py`：当前修改文件无 trailing whitespace；`docs/B0_DATA_ADMISSION_CHECK_v1.md` 由外部脚本更新，不在本次任务范围
- `tests/test_paper_trading_ui.py`：22 passed（页面渲染、资金不变传递、无策略参数编辑器、运行按钮触发、rerun 后结果保留、日期/价格/评分异常阻止运行、价格空值/零/负值阻止运行、评分空值阻止运行、影子订单需确认、确认/拒绝/取消调用服务、拒绝需原因、总览展示绩效指标、详情展示订单与曲线、同一批次 B0.4 对比、排除无批次影子账户）
- `tests/test_paper_trading_browser.py`：1 passed（纯 UI 点击完成对比账户/影子账户创建、运行、确认成交，并验证数据库变化）
- `tests/test_paper_trading_metrics.py`：11 passed（含首日下跌回撤）
- A/B 数据补齐实验：完整数据 NAV=2,761,288.07；排除补齐 NAV=2,809,091.21。首次分歧：2026-06-08。

---

## 5. Task 7 浏览器验收

- 实际启动 `streamlit run app.py` 打开 "虚拟盘" 页签，验证创建账户、运行、影子订单确认等真实交互
- 已通过 Playwright 浏览器验收测试：纯 UI 点击完成创建对比账户、创建影子账户、今日运行、生成影子订单、确认成交、查看账户详情，并验证数据库变化
- 未实际打开页面前不得声称浏览器工作流完成

## 6. Paper Trading v0.3.1 账户生命周期

### 新增生命周期能力

| 操作 | 可恢复 | 数据保留 | 能否运行 |
|------|--------|----------|----------|
| 结束账户（close） | 是（reopen） | 全部保留 | 否 |
| 隐藏账户（hide） | 是（unhide） | 全部保留 | 是（默认列表不显示） |
| 软删除账户（soft delete） | 是（restore） | 全部保留 | 否 |
| 永久删除（permanent delete） | 否 | 级联清理子表 | 否 |

### 关键规则

- 结束账户会设置 `status = ENDED` 并记录 `closed_at`；同一事务中将所有 `PENDING` 订单改为 `CANCELLED`，原因 `ACCOUNT_CLOSED`；重新打开后恢复 `READY` 并清空 `closed_at`，旧订单保持 `CANCELLED`。
- 隐藏账户只设置 `is_hidden=1`，不影响运行；取消隐藏后恢复可见。
- 软删除账户同时设置 `is_deleted=1` 和 `status = ENDED`，记录 `deleted_at`；同一事务中将所有 `PENDING` 订单改为 `CANCELLED`，原因 `ACCOUNT_DELETED`；恢复后回到 `READY` 并清空 `deleted_at`，旧订单保持 `CANCELLED`。
- 永久删除前必须先软删除；service 层再次校验完整账户名称匹配和显式确认标志；永久删除在同一事务中按外键约束顺序清理：先 `paper_trades`，再 `paper_orders`，然后其他子表，最后 `paper_accounts`。
- Runner 拒绝为 `ENDED` 或 `is_deleted=1` 的账户运行。
- UI Service `run_accounts` 返回结果新增 `skipped` 分区，用于报告被跳过的账户。
- UI 默认不显示隐藏和已删除账户；用户可通过顶部开关临时显示。

### 资金勾稽（来自真实测试）

- **现金 + 持仓市值 = 总资产**：`tests/test_paper_trading_runner.py::TestUniqueFinalState::test_single_nav_per_day` 每日验证。
- **佣金已计入现金变化**：`tests/test_paper_trading_service.py::test_confirm_shadow_order_fills_trade_updates_state` 验证，买入 2 笔共 300 元市值产生 10 元佣金，现金从 1,000,000 变为 999,690。
- **买入不得造成负现金**：`tests/test_paper_trading_runner.py::TestUniqueFinalState::test_cash_never_negative` 验证。
- **卖出不得超过实际持仓**：`tests/test_paper_trading_runner.py::TestOversell::test_oversell_skipped` 验证。

### 已知限制

- 尚未定时自动运行
- 不连接券商、不自动实盘下单

## 7. 下一阶段

- Paper Trading v0.3.2：定时自动运行、运行日志或邮件/通知提醒。

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

## 11. 行情更新修复 v2 与拼接错误数据修复（2026-10-20，待验收）

**背景**：数据停更 3.5 个月。第一轮修复"更新最新行情"工程链路（iFinD 就近锚定拼接、三日期指标卡、结果持久化、单只失败不阻断、断裂预检），备份提交 `feature/data-update-recovery`（5247ff8）。Trae 复审 BLOCKED 后第二轮修复运行库拼接错误数据（用户授权：B0.4 冻结快照/冻结指标/交易规则不变）。

**第三轮：真实价格架构 v3（2026-10-20，Codex 复核两个必须解决的问题后实施，已迁移正式库，待 Trae 定向复审）**

针对 Codex 复核意见"真实成交价格与评分价格分开使用"与"重新处理成交量"，实施真实价格架构 v3（设计文档 `docs/REAL_PRICE_ARCH_v3_DESIGN.md`）：

- **价格双轨制**：market_data 存储 iFinD 真实 OHLCV（adjust_type='real_spliced'）+ 校准 adj_close；评分管线（backtest.py / app.py 共 6 处）经 `apply_adjustment_for_scoring` 用调整后价格算分；虚拟盘/回测成交与估值仍用真实 close，股数与佣金按真实市场价格计算
- **成交量规则**：volume 恒为真实股数，不乘任何换算因子；评分用量比换算规则量为 volume÷adj_factor（拆分前等效份数），512480 的 7/3 真实放量 2 倍 ÷ MA20（等效基准同样 2 倍）≈1，量比连续
- **拆分处理**：corporate_actions 表登记 9 条真实拆分（2019 以来双序列 none/fwd 比率阶跃 >20% 检出，含 512480 的 2026-07-03 与 515880 的 2026-07-06）；虚拟盘 runner 盘前 `_apply_corporate_actions` 对拆分日持仓股数 ×ratio、成本 ÷ratio，总资产不变
- **迁移执行**（scripts/migrate_real_prices_v3.py，先在副本验证后跑正式库）：删旧等效拼接行 1389 → 双序列重生成 1408 行（19/19 成功）→ 全历史 adj_close 重算 29530 行（adj = close×fwd/none）→ 评分重算 33895 条 → 515880 的 7/7~7/10 correction 行 OHLCV 全部 ÷2（此前 ×2 成拆前口径）
- **验证包全 PASS**：B0.4 快照 SHA-256 前缀 dbdb041d 不变；512480 6/26~7/6 与 iFinD none 逐日对照 volume 0 不一致；拆分边界 adj 收益率 -1.41% 平滑；无 forward_spliced 残留；全库 OHLC 自相矛盾行 0
- **备份**：`database/backups/etf_model_20261020_172326_pre_real_price_v3.db`
- **测试**：388 passed / 2 failed（两个失败为 HEAD 干净副本可复现的既有问题）；新增 `tests/test_real_price_adjustment.py` 11 项
- **传输适配**：本机 Python TLS 对 agent-gw 握手失败期间，迁移脚本 curl 直连信封备用传输（`--transport curl`）；插件 CSV 的 time 列存在 int/YYYYMMDD/ISO 三种格式，已兼容

**第二轮修复后的数据状态**（已被第三轮取代，留档）：
- 19 只正式池行情/评分对齐至 **2026-10-19**（三日期卡一致）
- 512480.SH：6/22~6/29 旧数据与核验来源不一致（原因尚待确认，6/29 锚点 2.78→2.909），6/30~10-19 按 factor=2.0 精确锚点重新拼接
- 515880.SH：1:2 拆分（7/6）口径已全段统一（6/29 锚点 1.7→1.715，7/7~7/10 ×2），-49% 假断裂消失
- B0.4 冻结段（<=2026-06-18）与修复前备份逐行比对 0 差异；条数守恒 29078→29078
- 评分全量重算 33877 条（B0.4 标准配置），拆分前后平滑无跳变
- 虚拟账户无 512480/515880 持仓/成交，不受影响
- 修复报告 `reports/splice_fix_report_20261020_141215.json`；备份 `database/backups/etf_model_20261020_141214_pre_splice_fix.db`（141013 为 0 字节坏文件勿用）

**验证**：pytest 388 passed / 2 failed（既有）；UI 浏览器实测通过（localhost:8502 数据管理页三日期、更新按钮端到端、结果持久化）；幂等重复更新 saved=0。

**遗留**：
- AKShare fallback 路径未真实调用（标记"未独立验证"）
- 虚拟盘持仓穿越拆分的代表性验证已覆盖（新建仓价格/股数/佣金、跨拆分持仓股数与成本、量比不造假），见 tests/test_real_price_adjustment.py
- 本轮不提交不推送，待 Trae 定向复审

---

**虚拟实盘状态**：Phase 2 每日运行流程、止损检查、每周调仓、模拟成交已完成；Task 6 Streamlit 虚拟盘页面已提交；v0.3.1 账户生命周期已完成。

**版本口径**：
- 策略版本：v1.2.3（B0.4 基线）
- 基线版本：B0.4
- 调仓引擎：v2.5
- 数据准入：v1.1
- 滑点测试：v2
- 虚拟实盘：v0.3.1

---

## 12. 拆分安全修复收口（2026-10-21，已提交推送 feature/paper-trading-ui-v0.3）

真实价格架构 v3 与拆分安全修复已完成定向验收并收口（Trae ROUND4/5/6 全部关闭；冻结快照 SHA-256 前缀 dbdb041d 与策略参数全程未动）。

- **真实成交价格与评分价格分开**：market_data 存真实 OHLCV + adj_close；评分经 `apply_adjustment_for_scoring` 用调整价（价×adj/close、量÷adj/close）；成交/估值用真实价（详见第 11 节 v3）
- **回测与虚拟盘正确处理已确认拆分**：回测引擎 `run()` 支持 company_actions（缺省 None = 冻结路径零变化），盘前 shares×=ratio、cost/high_water/atr÷=ratio；虚拟盘 runner 盘前调整同语义，市值不变、不误止损
- **未确认拆分保护**：pending 涉持仓 → 回测抛 `PendingCorporateActionError` / 虚拟盘 paused（不交易、不估值、不保存当日状态，确认后重跑恢复）；pending 未持仓 → 持续禁买直到确认；**跳期防护**：已确认拆分生效日未处理时禁止直接续跑（提示先补跑，不能带旧股数恢复）；页面查询返回生效日≤当日全部记录 + 全部 pending（历史 confirmed 不丢）
- **公司行动查询失败 → 停止本次运行并明确报错**（不当作无记录继续）
- **基准收益口径统一**：图表/年度表/区间卡全部按各自比较区间首末比（`bench_return_in_range`），年度表旧差分算法（确认有误）已废弃

**验证范围（如实）**：本次会话实测虚拟盘 20 passed、回测拆分+基准口径 15 passed、全量回归 412 passed / 2 failed（均基线既有问题）、CLI 全样本回测正常（140.99%、927 笔）；B0.4 冻结基线（NAV 2,761,288.07 / 804 笔）与恢复流程由 Codex 独立复验；早前轮次结果未全部重跑，仅作历史记录。

**下一步**：回到买入点诊断（8 月复盘中识别的急涨追买问题），暂不增加交易规则。
