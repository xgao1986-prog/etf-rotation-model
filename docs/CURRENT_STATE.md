# 当前工程现场

**最后更新**：2026-10-21
**工作目录**：`D:\etf_rotation_model`
**当前分支**：`feature/paper-trading-ui-v0.3`（HEAD `fd20651`，已推送远端）
**当前版本**：v1.2.3（虚拟盘 v0.3.1）
**发布锚点**：v1.2.3-b0.4 → 5e8eb78（标签保持不动）
**正式基线**：B0.4（见 `docs/B0_BASELINE_LOCK.md`）

### 数据截止日（两个口径，勿混淆）

| 口径 | 截止日 | 说明 |
|------|--------|------|
| **B0.4 冻结数据** | **2026-06-18** | 冻结段 ≤2026-06-18，快照 `data/snapshots/B0_4_candidate_data_20260621_210815.csv`，SHA-256 前缀 dbdb041d（全量 e0cf2993…），全轮未动 |
| **当前数据库行情** | **2026-10-21** | market_data 最新日期（2026-10-21 网页实测 iFinD 追加，112,340 行） |
| **当前评分** | **2026-10-21** | daily_scores 最新日期（行情更新后自动重算，同日确认） |

corporate_actions 表 9 条拆分记录全部 confirmed（含公告依据）。旧分支 `feature/v1.2.1-regime-adaptive` 保留不动。

---

## 1. 当前结论（最终状态）

- **真实价格架构 v3 + 拆分安全修复：已验收收口**（提交 `5308015`，Trae ROUND4/5/6 全部关闭）——本节第 3 条详述
- **B1 Holding Stability A/B：已终审收口，不升级 B1**——A/B/C 夏普均低于 B0.4 的 0.91 且回撤恶化；卖出缓冲减少换仓有效但风险调整后变差
- **Universe Time-Consistency Audit：已收口**——7 只 ETF 早期覆盖不足已量化披露，四组对照不足以推翻 B0.4
- **Paper Trading v0.3.1：已完成**——账户生命周期（结束/隐藏/软删除/永久删除级联）21 项测试覆盖，浏览器验收通过
- **实盘助手 v0.2：骨架已交付**（持仓/止损/调仓建议/成交记录，不自动下单，22 项测试）
- **v1.3 Step 1~10：已完成，全部 observer-only**——无任何一步达到升级 B0.4 的预注册标准；B0.4 保留
- **行情更新链路：已修复可用**——AKShare 超时治理 + iFinD CSV 自动导入 fallback + iFinD 双序列拉取
- **iFinD 数据连接凭证链路：已收口**（提交 `fd20651`）——凭证持久化 + 三层状态展示 + 缺凭证快速失败，网页实测对齐至 2026-10-21
- **账户设置一致性第一检查点：按8.7验收报告已收口**——一致性39项、13套件223项及冻结9项通过为该轮报告结果；Playwright跳过与网页订单确认限制保留。本轮不冒充重新执行完整验收。

历史细节见 `docs/CHANGES.md`。

---

## 2. B0.4 冻结口径

- 18 只 ETF（16 行业 + 黄金 518880 + 国债 511010）；每周四调仓；最多 5 只、单只新建仓上限 20%；固定止损 -8%
- 动量/波动率因子关闭；市场状态模块不参与交易；佣金双向 0.03% 最低 5 元、100 股整手、不计滑点
- 执行口径：信息日期 = 前一交易日收盘，成交 = 次日开盘；关键指标用 `shift(1)`，已过同日扰动测试

### 冻结指标（复现见 `tests/test_b0_4_slippage.py`，0bp = 冻结快照）

| 指标 | B0.4 |
|------|------|
| 最终 NAV | 2,761,288.07 |
| 总收益 | 176.13% |
| 交易次数 | 804 |

---

## 3. 本轮修复最终状态（2026-10-20 ~ 10-21，已验收）

**价格双轨制**：market_data 存 iFinD 真实 OHLCV（adjust_type='real_spliced'）+ 校准 adj_close；评分管线经 `apply_adjustment_for_scoring` 用调整价（价 ×adj/close、量 ÷adj/close，无 adj_close 恒等 = 冻结路径零变化）；成交/估值用真实价，股数与佣金按真实市场计算；volume 恒为真实股数不乘换算因子。

**已确认拆分处理**：corporate_actions 登记 9 条真实拆分（status/evidence 列，`confirm_corporate_actions` 为唯一确认入口）。回测引擎 `run()` 支持 corporate_actions（缺省 None），盘前 shares×=ratio、cost/high_water/atr÷=ratio，修复了 512480 在 2026-07-03 被误读为 -51% 错误止损、组合虚假下跌 9.9% 的问题。虚拟盘 runner 盘前同语义调整，市值不变、不误止损。

**未确认拆分保护**：
- pending 涉持仓（含 confirmed 但 ratio 非法）→ 回测抛 `PendingCorporateActionError` 整体暂停；虚拟盘 paused：不交易、不估值、不保存当日状态，确认后重跑该日恢复
- pending 未持仓 → 该标的新买入资格持续暂停直到确认
- **跳期防护**：已确认拆分生效日未处理时，禁止直接续跑后续日期（明确提示先补跑生效日），不能带着旧股数恢复
- 页面查询（`app._load_corporate_actions`）返回生效日 ≤ 当日的全部记录（历史 confirmed 不丢）+ 全部 pending
- **公司行动查询失败 → 停止本次运行并明确报错**，绝不当作无记录继续

**基准收益口径统一**：图表/年度表/区间卡全部按各自比较区间 bench_price 首末比 - 1（`bench_return_in_range`）；年度表旧差分算法确认有误，已废弃。

**迁移执行结果**（正式库，已验证）：删旧等效拼接行 1389 → 双序列重生成 1408 行 → 全历史 adj_close 重算 29,530 行 → 评分重算 33,895 条；B0.4 冻结段与修复前备份逐行比对 0 差异；拆分边界 adj 收益率平滑（512480 7/2→7/3 仅 -1.41%）；无 forward_spliced 残留。备份：`database/backups/etf_model_20261020_172326_pre_real_price_v3.db`。

**遗留（如实）**：AKShare fallback 路径未真实调用（未独立验证）；512480 的 6/22~6/29 旧数据与核验来源不一致，**原因未确认**（不作"盘中快照"定性）。

---

## 3.1 iFinD 数据连接凭证链路收口（2026-10-21，提交 `fd20651`）

**根因**：Streamlit 网页启动环境不继承 Kimi 对话注入的临时 `KIMI_API_KEY`/`KIMI_BASE_URL`，SDK 凭证链为 显式参数 > 环境变量 > `~/.kimi/agent-gw.json`，原配置文件不存在导致 iFinD 批量调用失败并重试。

**修复**：
- 凭证持久化至用户主目录 `~/.kimi/agent-gw.json`（含 `base_url`，SDK 默认是 dev 地址；文件不在仓库，未入 Git）
- `ifind_gateway.check_credentials()` 分层检测（env > 文件 > 无/损坏），消息不回显密钥；`is_available()` 分开展示 `program_installed`/`credentials_configured`（顺带修复 force=True 时凭证缓存不刷新的缺陷）；`get_price()` 缺凭证零子进程零重试直接全部 failed
- `update_latest_data_v2` 凭证预检，缺凭证返回 `status="credentials_missing"`，不写库
- 页面状态栏三层展示：**iFinD 程序（已安装/未安装）**、**iFinD 数据连接（未配置/凭证已配置/连接验证成功）**——凭证存在 ≠ 连接可用，仅 iFinD 实际取数写入成功才显示"连接验证成功"（会话内标记）

**验证**：`tests/test_data_fetcher.py` 新增 4 用例（缺凭证零子进程断言、凭证分层与不回显密钥、is_available 分层字段、credentials_missing 不写库），该文件 19 passed；全量回归 416 passed / 2 failed（基线既有，同前）。Streamlit AppTest 无凭证环境实测：状态栏显示"凭证已配置"、iFinD 更新写入 19 条（成功 19 只失败 0 只）、数据库对齐 2026-10-21（19 只，source=iFinD-KimiGateway 真实价格）、评分自动重算至 10-21；幂等重跑 no_data 不重复写入、不显示"连接成功"。

**边界**：未修改历史行情或策略参数；B0.4 冻结未动；凭证文件未上传。

---

## 3.4 账户设置与实际执行一致性（2026-10-22，Kimi，第一检查点，第二轮补修已验收）

- 审计报告：`reports/settings_consistency_audit_20261022.md`（三路径设置支持清单）。对照回测/虚拟盘/实盘建议逐参数追踪，发现 9 项问题（F1~F9）：虚拟盘止损线硬编码 -0.08、调仓日硬编码周四、防御开关硬编码 True、入场只过滤总分、标的池运行时读全局模块（侧栏改动泄漏到已有账户）、创建账户不校验配置（不支持项静默忽略）、实盘助手止损符号 bug（-0.08 变 +8% 阈值几乎全员误报）、实盘防御候选无门槛、页面未区分三类参数。
- 第一轮修复：runner 止损线/调仓日/防御开关/标的池快照/入场过滤全部取账户创建时冻结配置（缺省值与旧行为一致）；`create_account` 统一校验+冻结标的池（market_timing=True、ATR/不止损、动态止盈、冷静期、宽基、非每周调仓、未知键一律拒绝创建并中文提示）；实盘助手止损取 abs()；实盘脚本防御门槛对齐 min_total-10 并补齐入场过滤；虚拟盘创建页列出支持/不支持清单，账户总览展示冻结参数表。
- **复审补修第二轮（4 项，本会话已实施）**：
  1. **评分字段硬性校验**：新建 `src/paper_trading/entry_filter.py::validate_score_data`——缺 prev_close/ma20_slope/trend_score/confirm_score/total_score 列或含 NaN 时抛 ValueError（中文指明字段与标的），虚拟盘当日报错不存信号不标记已处理、实盘脚本非零退出；删除"缺字段退回仅看总分"宽松路径，旧合成测试数据已补齐字段。
  2. **B0.4 完整入场条件落实到三路径同一实现**：`entry_filter.select_candidates` 实现弱市 55 分门槛（核心池 momentum_20 中位数<0 判定）、防御资格（趋势≥10/确认≥2/总分≥max(30,min_total-10)/prev_close>ma20×0.98/slope>-0.001）、数据成熟期 history_count≥51、prev_close>ma20、slope>0；虚拟盘 `_run_planner` 与实盘脚本 `get_b0_4_signals`（富化 prev_close/history_count）均改走该函数，测试经真实 `generate_signals` 生产信号路径比较，不再复制筛选。
  3. **评分来源与权重兼容**：`models.py` 新增 `PIPELINE_FACTOR_POINTS`/`effective_pipeline_weights`（动量/波动率关闭→趋势30/确认20/成交量15归一化）/`weights_compatible_with_pipeline`（容差0.005）/`describe_score_source`；`create_account` 拒绝与统一评分管线不兼容的 weights 预设（不落库），账户 config 记录 config_version=2 与 score_source，页面冻结参数表展示"评分来源/执行口径"两行。
  4. **老账户行为变更确认门闩**：store schema v4 加 `behavior_ack_date`/`behavior_ack_config_json`（幂等迁移）；`resolve_execution_config` 对 config_version<2 且未确认账户返回 None → run_daily 暂停（新 result kind `behavior_ack_required`，不交易不估值不标记已处理），页面给 warning + "确认按新执行口径运行"按钮；`confirm_behavior_update` 只写 ack 列、不改 config_json、不覆盖已有冻结标的池；无冻结池的老账户不再受侧栏影响，无法可靠还原时提示确认而非猜测。
- 验证（本会话实测）：`tests/test_settings_consistency.py` 全量重写为 36 项（F1~F8 + 补1×4 + 补2×3 + 补3×4 + 补4×7）全部通过；paper/live 12 套件 206 passed；venv 下 UI 23 passed；B0.4 冻结 9 passed（138s，NAV 2,761,288.07/804 笔未动）；准入 16 passed/1 failed（`test_missing_data_antipattern` 基线既有）；全量 429 passed（除 Codex `test_analysis_date_range_ui.py` 与两个基线既有失败）；浏览器 2 项单独跑各 passed（185s/224s）。三路径相同输入下目标持仓逐一相等，且经真实生产信号集合断言（弱市 meta['effective_min_total']==55.0）。
- **行为变更警示**：①既有账户下次运行起将真正按保存的止损线执行（如"保守型"-5%，此前实际按 -8%）；②config_version<2 的老账户未确认前将暂停运行（页面确认后恢复）；③保守型/激进型等权重与统一管线不兼容的预设将被拒绝创建新账户。
- **测试环境坦白**：8501 端口曾长期被一台 Python3.10 旧 Streamlit 残留服务器（PID 64004，已终止）占用——历史浏览器测试可能一直打在旧代码上；本轮浏览器 fixture 已改用 `sys.executable`，干净环境单跑通过。
- 允许差异：实盘建议用信号日收盘价估算 vs 虚拟盘/回测用执行日开盘价，整手尾差已书面说明。
- 边界：未提交未推送；未碰 backtest/strategy/config/rebalance_planner；`app.py` 本轮有触碰（仅 `_paper_trading_data_provider` 评分字段富化段，属 Codex 文件，需 Codex 复核）；未修改真实持仓、数据库、个人预设、冻结快照。
- 下一检查点（待启动）："今日待办／策略模拟／真实账户"可点击样稿。

---

## 4. 验证范围（如实记录，不夸大）

**本会话（Kimi）实测**：
- `tests/test_real_price_adjustment.py` 20 passed、回测拆分 10 + 基准口径 5 passed、data_fetcher 15 passed（提交前合计 50 passed）
- 全量回归 412 passed / **2 failed**——失败为基线既有问题：`test_missing_data_antipattern`、`test_v1_3_step6_dynamic_fifth_slot`（regime 分布）；证据：在 HEAD 干净副本（git worktree）可复现，**原因未确认**，与本轮改动无关
- CLI 全样本回测正常完成（总收益 140.99%、927 笔、拆分日 0 误止损）

**Codex 独立验证（非本会话重跑）**：
- B0.4 冻结基线复现：NAV 2,761,288.07、804 笔交易、冻结快照 SHA-256 不变
- 恢复流程：暂停 → 确认 → 补跑 → 正常恢复

早前各轮结果（388 passed 等）未在本次全部重跑，仅作历史记录。

---

## 5. 工程状态备忘

**Paper Trading 资金勾稽（真实测试保证）**：现金+持仓市值=总资产（每日验证）；佣金计入现金；买入不得负现金；卖出不得超持仓。已知限制：未定时自动运行、不连接券商。

**禁止修改范围**：B0.4 ETF 池/评分因子/阈值、动量与波动率开关冻结状态、调仓日/最大持仓/单只上限/止损及防御规则、规划器 v2.5 业务语义、冻结快照与 SHA-256、`docs/B0_BASELINE_LOCK.md`、已封存样本外结果。所有增强必须独立开关、以 B0.4 为对照、一次只变一个变量。

**版本口径**：策略 v1.2.3（B0.4 基线）/ 引擎 v2.5 / 数据准入 v1.1 / 滑点测试 v2 / 虚拟盘 v0.3.1。

---

## 6. 下一步（策略研究，先查规律再定规则）

按序执行，**先查清规律，再决定是否改规则；不为避开三笔已知亏损而调参数**：

1. **统一 8 月复盘的配置、日期、收益和交易数字**（以 B0.4 冻结配置重跑核对，消除口径分歧）
2. **撤回未经证明的解释**：注意 B0.4 总分本来就不含动量分，"动量排名清零副作用""亏损全因买贵"等归因均不成立，一并撤回
3. **全历史对照：急涨后买入 vs 非急涨买入**——既看亏损也看抓住的大涨，判断追涨是否系统性吃亏

---

## 7. 新线程恢复方式

依次读取：`AGENTS.md` → `docs/CURRENT_STATE.md` → `docs/DECISIONS.md` → `git status --short --branch`

## 8.7 账户设置一致性第二轮独立验收（2026-10-26，Kimi，第一检查点已收口）

- 验收报告：`reports/settings_consistency_acceptance_20261026.md`。六项必要检查通过；正式基线保持 v1.2.3 / B0.4，未启动下一检查点。
- 新发现双轨价格边界：页面与实盘富化曾以真实 `close` 作为 `prev_close`，与正式评分调整价口径不一致。新增3项回归，修复前2 failed/1 passed；在 `app.py` 和 `scripts/live_generate_trade_plan.py` 的评分富化副本上统一 `apply_adjustment_for_scoring`，成交价格仍用真实 open/close。修复后3 passed。
- 本轮实测：一致性39 passed；paper/live/账户管理/一致性13套件223 passed；B0.4冻结9项经拆分全部完成通过，0bp NAV 2,761,288.07、804笔。首次整组300秒超时仅完成4项，未计为通过或失败。
- 当前代码在独立8517端口、临时账户库完成网页创建与运行，显示“成功运行1个账户”，截图位于 `reports/browser_acceptance/settings_consistency_20261026_run_success.png`（本地验收证据，未随仓库发布）；原8501进程未终止，隔离进程已关闭。Playwright两项因依赖缺失 skipped，未冒充通过；网页订单确认本轮未手工重复，自动测试已覆盖。
- 本轮验收记录的起止行情库 SHA-256 均为 6063eecd…，期间未再变化。该指纹与迁移时的 95d10b8d… 不同；用户已确认主动点击更新行情，当前行情与评分截至 2026-10-26。两种指纹对应不同时间点，不应混作同一比较基准。
- 本轮验收期间未修改正式行情库；真实账户、虚拟盘库、凭证、冻结快照及策略语义未修改，虚拟盘库与冻结 CSV 的验收前后指纹不变。未清理、提交、推送。

## 8.13 批次 2 iFinD 默认路径兼容修复（2026-10-27，Kimi，已暂存待审）

- 暂存范围仅含 `src/ifind_gateway.py`、`tests/test_data_fetcher.py` 和本节；未整文件暂存本文，8.11、8.12及顶部既有工作区记录不进入批次2。
- 默认插件目录和旧版 Python 路径改为环境变量优先、用户目录发现兜底，移除个人用户名硬编码。解释器顺序保持：显式 `IFIND_PYTHON` > 仍存在的旧默认 Python > 可用的插件配套网关 Python > 当前应用解释器。
- 先新增环境变量缺失边界测试，修复前实跑1 failed；修复后该测试与解释器选择测试2 passed。完整 `tests/test_data_fetcher.py` 为 **21 passed, 8 warnings in 4.08s**，两个文件编译及差异空白检查通过；警告均为既有 NumPy timedelta 弃用提示。
- 本机只读核对：旧默认 Python 不存在、插件配套 Python 存在且被选中。全部自动测试使用临时目录与模拟数据，未更新正式行情、真实账户或凭证。
- 暂存新增内容扫描未发现个人用户名、本机用户路径或疑似明文凭证赋值。未 commit/push，未启动批次3～5；TLS证书验证和吊销检查保持开启。
