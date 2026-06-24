# 当前工程现场

**最后更新**：2026-06-24（Step 9：以夏普率为主KPI的 A/B/C/D 策略复核）
**工作目录**：`D:\etf_rotation_model`
**当前分支**：`feature/v1.3-regime-research`
**当前版本**：v1.2.3
**当前 HEAD**：以 `git status` 为准
**发布锚点**：v1.2.3-b0.4 → 5e8eb78
**正式基线**：B0.4（见 `docs/B0_BASELINE_LOCK.md`）
**数据截止**：2026-06-18

> 旧分支 `feature/v1.2.1-regime-adaptive` 保留，不删除、不重置、不强推。标签 `v1.2.3-b0.4` 保持不动。

---

## 1. 当前结论

- **Codex 终审 P1 修复已完成（cfg_signature 提取 + trailing_stop=None 修复）**：
  - `app.py:465` 对默认配置 `trailing_stop=None` 执行 `round(None, 6)` 会导致 `TypeError`。
  - 修复：`None` 保持为 `None`；仅对非 `None` 数值执行 `round`。
  - 将 `cfg_signature` 从 `app.py` 提取到 `src/utils.py`，成为纯函数，供 `app.py` 和测试共同调用同一实现。
  - 测试不再复制 `cfg_signature`；直接导入生产函数 `from utils import cfg_signature`。
  - 新增4项回归测试：默认签名不报错、`trailing_stop=None` 稳定签名、`simple` 模式数值变化改变签名、`None` 与 `-0.1` 签名不同。
  - `tests/test_app_b0_signature.py` 扩展至13项全部通过。
- **App/防御总开关工程修复已完成**：
  - `app.py` 配置签名补齐缺失字段（`fallback_equity_enabled`, `atr_stop_multiplier`, `cooling_score_boost`, `trailing_stop`, 动态止盈档位, `initial_capital`）。
  - `backtest.py` 和 `rebalance_planner.py` 补全 `defense_enabled` 总开关逻辑。
  - 默认 `defense_enabled=True`，B0.4 行为完全不变。
  - 新增 `tests/test_defense_enabled_switch.py`（7个场景）全部通过。
  - B0.4 滑点测试 v2 8项全部通过（175.11s），0bp 复现 NAV=2,761,288.07，交易804笔。
- **v1.3 Step 5补充: B0.4 vs 方案B 三维归因分析已完成**：
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
- **v1.3 Step 2: B0.4 市场状态增量价值诊断 已完成（修正版v3）**：
  - 方法修正：在完整nav_df上先计算 bench_ret = bench_price.pct_change()，再按研究期/验证期筛选；超额收益用 prod(1+r_s)/prod(1+r_b)-1（禁止CAGR-CAGR）。
  - 自然择时已生效：强牛行业仓位 74.9% > 熊市 42.0%，差异 32.9 个百分点；总持仓强牛 85.4% > 熊市 59.1%。
  - 弱市超额为正：熊市超额 +11.10%，震荡 +13.18%（prod比率法，非CAGR-CAGR）。
  - 状态分布：熊市 50.0%（701天）、弱牛 18.1%（236天）、震荡 16.4%（214天）、强牛 11.8%（154天）。
  - 状态切换 60 次，平均置信度 0.773。
  - 方向判断：**撤回 A/B/C 推荐**。三个方向均不可靠，暂不推荐。研究期与验证期震荡方向不一致（+16.31% vs -2.69%），不满足进入实验条件。
  - 小样本警告：研究期强牛仅60天，年化高度膨胀；验证期震荡45天、弱牛71天同样样本不足。
  - 交易勾稽：804 = 642（四状态已归因）+ 162（2025-2026样本外）+ 0（warmup/NaN）。
  - 收益勾稽：四状态增长因子连乘误差 0.000000（策略），0.000000（基准）。
  - 44.7%/31.0%：旧报告错误值，来源不可复现。不能归因于pct_change收益算法。
  - 交付物：scripts/v1_3_step2_b0_4_regime_diagnosis.py（修正版v3）、reports/v1_3_step2_regime_diagnosis.md（修正版v3）、reports/v1_3_step2_regime_stats.csv（修正版v3）。
  - 不修改 B0.4 策略、参数或冻结基线。
- **v1.3 Step 3 v3: 信号失效退出有效性归因 已完成（修正版v3，修复时间分区泄漏）**：
  - **时间分区泄漏修复**：研究期事件未来行情和买回搜索限制在2022-12-31，验证期限制在2024-12-31，样本外截止2026-06-18。
  - 完整保留 341 笔，CSV 严格 341 行。新增 observation_status（COMPLETE_20D/CENSORED_PERIOD_END/CENSORED_DATA_END/NO_FUTURE）。
  - 数据勾稽：341 = COMPLETE_20D(327) + CENSORED_PERIOD_END(8) + CENSORED_DATA_END(4) + NO_FUTURE(2)。
    - 研究期：170 = 完整20日(165) + 分区边界截尾(5) + 数据截止截尾(0) + 无未来数据(0)
    - 验证期：96 = 完整20日(93) + 分区边界截尾(3) + 数据截止截尾(0) + 无未来数据(0)
    - 样本外：75 = 完整20日(69) + 分区边界截尾(0) + 数据截止截尾(4) + 无未来数据(2)
  - 分类结果（仅完整20日样本，研究期 → 验证期）：有效避损 34.1% → 34.4%，误杀卖飞 10.9% → 23.7%，震荡往返 41.8% → 25.8%，中性 15.2% → 16.1%。
  - 重新买回统计（限制在分区边界内）：任意未来首次买回率 92.4% / 86.5%；20日内买回率 42.4% / 28.1%；20日内震荡往返率 40.6% / 25.0%。
  - 往返佣金：sell_commission + rebuy_commission。全部买回均值 146.24元/笔（研究期）、173.99元/笔（验证期）；震荡往返均值 145.75元/笔（研究期）、174.27元/笔（验证期）。
  - 方向一致性（统一15%阈值）：震荡往返 41.8% / 25.8% 两期均>15%（是）；误杀卖飞 10.9% / 23.7% 不满足两期均>15%（否）。
  - 决策建议：震荡往返比例两期均>15%，是支持 holding stability 实验的主要证据；误杀卖飞不能作为独立证据。
  - 交付物：scripts/v1_3_step3_exit_effectiveness.py（v3）、reports/v1_3_step3_exit_effectiveness.md（v3）、reports/v1_3_step3_exit_events.csv（v3）、reports/v1_3_step3_exit_summary.csv（v3）。
  - 不修改 B0.4 策略、参数或冻结基线。
- **v1.3 Step 4: 80/20组合结构机制拆解 已完成（修正版，验收逻辑降级）**：
  - **分析期限定**：所有方案选择结论仅使用2019-2024年数据。2025-2026样本外数据只列出展示，不参与方案选择或结论支持。
  - **B0.4基线复现**：NAV=2,761,288.07，交易804笔，精确复现 ✓。
  - **分析期核心指标**：
    - B0.4：总收益71.32%，夏普0.59，最大回撤-17.75%
    - B（4行业+1防御）：总收益68.15%（**低于**B0.4），夏普0.64（**高于**B0.4），最大回撤-16.38%（**优于**B0.4）
    - A（4行业+现金）：总收益60.64%，夏普0.58，最大回撤-16.66%
    - C（5行业×16%）：总收益48.42%，夏普0.52，最大回撤-16.27%
  - **关键发现**：B改善了夏普和回撤，但**没有改善分析期总收益**（68.15% < 71.32%）。验证期B总收益25.06% < B0.4的27.67%。
  - **防御贡献修正（分析期）**：B黄金约2.39%，国债约2.46%，防御合计约4.86%。B0.4防御合计约5.70%。**B的防御贡献不高于B0.4**。
  - **市场逻辑**：B相对A在研究期、验证期均更好，支持"防御资产优于闲置现金"。B相对B0.4改善了夏普和回撤，但不是提高分析期收益。改善可能来自行业风险降低、现金/防御组合及换手下降的共同作用，**不能证明来自更高防御贡献**。
  - **预注册验收标准**：
    - ✅ 通过：研究期/验证期夏普方向一致、验证期回撤改善、滑点压力测试通过、震荡/弱市改善符合逻辑、强牛机会成本可接受、数据勾稽完整
    - ❌ **未通过**："2023、2024不能仅靠单一年份支撑"——2024年B落后B0.4，验证期收益未持续领先
    - ⚠️ 存疑：防御贡献是否为主要改善来源——分析期B防御合计约4.86% < B0.4约5.70%
  - **结论降级**：方案B是**有经济逻辑的后续稳健性候选**，但不能升级基线或认定为候选增强。B在夏普和回撤上有改善，但分析期总收益未超过B0.4，且验证期收益未持续领先。
  - **交付物**：scripts/v1_3_step4_portfolio_structure_ab.py、reports/v1_3_step4_portfolio_structure_ab.md（修正版）、reports/v1_3_step4_portfolio_metrics.csv、reports/v1_3_step4_portfolio_daily_attribution.csv、reports/v1_3_step4_slippage_test.md、scripts/v1_3_step4_slippage_test.py。
  - **不修改 B0.4 策略、参数或冻结基线。不进入任何参数调优或增强实施。**
- **v1.3 Step 6: 基于市场状态的动态第5槽位 A/B 实验已完成（Codex终审最小修复）**：
  - 三个方案：A(B0.4)、B(固定4+1)、C(动态第5槽位：震荡=5行业，其他=4+1防御；NaN/warmup回退B0.4)。
  - 全期间：A=176.13%, B=180.91%, C=176.45%。C介于A和B之间，更接近A。
  - 研究期(2019-2022)：A=34.69%, B=34.94%, C=33.73%。
  - 验证期(2023-2024)：A=27.67%, B=25.06%, C=24.32%。
  - 观察期(2025-2026)：A=64.19%, B=70.19%, C=70.02%。仅展示，不参与PASS/FAIL判断。
  - **预注册验收标准未全部通过**：
    - 夏普方向不一致（研究期C>A 0.63 vs 0.60，验证期C<A 0.74 vs 0.75）❌
    - 验证期回撤：C绝对回撤更小（-16.30% vs -17.75%），通过 ✅
    - 验证期收益C-A=-3.35%，低于-2%容忍度 ❌
    - leave-one-year-out(分析期2019-2024): C>A 1/6=16.7% < 50% ❌（真实回测结果，非硬编码）
    - 滑点方向不反转：所有滑点下C>A ✅
  - **机制归因**：C在震荡市+0.75%（301天），在熊市-6.97%（826天）。熊市拖累超过震荡增益。
  - **防御ETF贡献（mark-to-market，截止2024-12-31）**：分别统计黄金(518880.SH)和国债(511010.SH)，含期末未平仓估值。字段`gold_final_position_mv_a`等。详见 `reports/v1_3_step6_defense_contribution.csv`。
  - **佣金（截止2024-12-31）**：A=49,409.20, B=40,620.21, C=42,054.07
  - **regime标签验证**：`STATE_NAMES`映射为1=强牛/2=弱牛/3=震荡/4=熊市。机制归因表与`detect_history`一致，标签未互换。
  - **结论**：C只能判定为机制观察候选，不得升级B0.4。
  - **LOO与annual定义区分**：`annual_contribution`是每个自然年的C-A收益差；`loyo`是剔除某年后其余年份组合的总收益差。两者定义不同，不要求正负方向逐年一致。详见 `reports/v1_3_step6_annual_contribution.csv` 和 `reports/v1_3_step6_loyo.csv`。
  - 交付物：`scripts/v1_3_step6_dynamic_fifth_slot_ab.py`（含NaN回退、mark-to-market防御贡献、--output-dir参数）、`reports/v1_3_step6_dynamic_fifth_slot_ab.md`（新鲜重新运行）、12份CSV数据文件（含loyo、annual_contribution、defense_contribution、reconciliation）、`tests/test_v1_3_step6_dynamic_fifth_slot.py`（16项全部通过，含CSV勾稽FAIL不skip、生产佣金公式验证、LOO读取CSV调用生产函数）。
  - 不修改B0.4策略、参数或冻结基线。

  - 不修改B0.4策略、参数或冻结基线。

- **v1.3 Step 8: B0.4 vs D 市场状态分层诊断 已完成（Observer-only）**：
  - **目标**：不修改B0.4，不合并规则，仅做诊断观察
  - **对照组**：A=B0.4(5×20%)，D=4×25%行业集中，防御关闭
  - **市场状态**：沿用已有regime检测结果（强牛/弱牛/震荡/熊市），不修改状态算法
  - **核心发现（修正后）**：
    - 弱牛：研究期D-A=+2.20%，验证期D-A=-0.01% → ❌ 不稳定
    - 强牛：研究期D-A=+0.91%，验证期D-A=-0.18% → ❌ 不稳定
    - 熊市：研究期D-A=-2.73%，验证期D-A=-0.14% → ❌ D不优于A
    - 震荡：研究期D-A=+2.70%（回撤恶化），验证期D-A=+0.11%（回撤恶化） → ⚠️ **风险换收益型候选**，不能视为明确改善
  - **关键结论（修正后）**：
    - D的优势主要来自**观察期（2025-2026）**，研究期和验证期并没有一致支持D
    - 只有**震荡市**在研究期和验证期都显示D-A为正，但**两期均伴随回撤恶化**，因此只能视为**风险换收益型候选**，不能视为明确改善，也不能直接推出震荡市应用D
    - 熊市中D明显弱于A（研究期-2.73%），集中仓位在下跌市场更脆弱
    - D在所有状态下都显示更高行业暴露、更少防御、更少持仓数量
    - **不满足"明确改善"标准**（要求收益改善且回撤不恶化），不能直接合并为策略规则
    - 如需进一步验证，需进入候选测试：震荡市 D 规则 + 风险约束（如止损收紧、仓位上限）
  - **预注册判断（Observer-only）**：
    - 若D在研究期和验证期的同一状态下都优于A，才认为该状态支持D
    - 若D只在研究期优于A、验证期不优于A，判为不稳定
    - 若D收益更高但回撤显著恶化，判为风险换收益，不算明确改善
    - 若D优势主要来自2025-2026，不能作为规则依据
  - **交付物**：`scripts/v1_3_step8_regime_b0_4_vs_d.py`（~380行）、`reports/v1_3_step8_regime_b0_4_vs_d.md`、4份CSV（regime_summary、year_regime_matrix、exposure_by_regime、verdict）
  - **测试**：12/12 通过
  - **验证**：py_compile 通过
  - **不修改**：B0.4生产代码、A/B/C/D四方案规则、回测引擎、市场状态算法

- **v1.3 Step 9: 以夏普率为主KPI的 A/B/C/D 策略复核 已完成（Observer-only）**：
  - **目标**：不修改B0.4，不合并规则，仅做诊断观察。主KPI = 夏普率，最大回撤作为约束条件而非单独否决标准
  - **对照组**：A=B0.4(5×20%)，B=4×20%+现金，C=4×20%+防御，D=4×25%
  - **核心发现**：
    - **全期夏普排名**：C(0.995) > D(0.928) > B(0.914) > A(0.910)
    - **研究期夏普排名**：C(0.651) > D(0.620) > B(0.604) > A(0.600)
    - **验证期夏普排名**：C(0.751) > A(0.741) > D(0.701) > B(0.688)
    - **观察期夏普排名**：C(1.955) > D(1.854) > B(1.831) > A(1.769) — 仅展示
  - **预注册判断（夏普优先）**：
    - **C**：研究期夏普0.65 > A0.60，验证期夏普0.75 > A0.74 → 夏普跨期均优于A
      - 但验证期CAGR 11.87% < A 13.04%，判定为**防守候选**（夏普高但收益略低）
      - 回撤：C研究期-15.02% vs A-15.43%（基本可忽略），验证期-16.38% vs A-17.75%（改善）
      - 结论：C夏普跨期稳定优于A，但验证期CAGR偏低，需解释夏普提升来源（防御贡献 vs 低波动 vs 真实选股）
    - **D**：研究期夏普0.62 > A0.60，但验证期夏普0.70 < A0.74 → **不稳定**
      - 验证期回撤-20.00% vs A-17.75%（显著恶化，差2.25pp）
      - 结论：D验证期夏普和回撤均不支持
    - **B**：研究期夏普0.60 ≈ A0.60，验证期夏普0.69 < A0.74 → **无优势**
  - **三类结论**：
    - **当前正式基线**：A/B0.4 仍保留。B0.4在研究期和验证期均表现稳健，无明确替换理由
    - **风险调整候选**：无。没有方案在夏普、收益、回撤三个维度上同时优于A
    - **防守候选**：C。夏普跨期均优于A，但验证期CAGR偏低（11.87% vs 13.04%）
    - **进攻候选**：无
  - **是否进入下一步测试**：C 在研究期和验证期夏普均优于A，值得进入**状态条件化组合规则**测试，但需先解释验证期CAGR偏低的原因
  - **交付物**：`scripts/v1_3_step9_sharpe_first_strategy_review.py`（~420行）、`reports/v1_3_step9_sharpe_first_strategy_review.md`、5份CSV（metrics_by_period、metrics_by_year、metrics_by_regime、leverage_equivalent、verdict）
  - **验证**：py_compile 通过
  - **不修改**：B0.4生产代码、A/B/C/D四方案规则、回测引擎、市场状态算法

- **v1.3 Step 7: 组合集中度与资金去向正交拆解 已完成（P1修复版 — 统计口径与证据完整性）**：
  - 四个方案：A(5×20% B0.4对照)、B(4×20%+现金 关闭防御)、C(4×20%+防御 防御填充)、D(4×25% 集中度提升)。
  - 全期间：A=176.13%, B=152.97%, C=180.91%, D=203.08%。
  - 研究期(2019-2022)：A=34.69%, B=31.61%, C=34.94%, D=39.24%。
  - 验证期(2023-2024)：A=27.67%, B=22.50%, C=25.06%, D=27.52%。
  - 观察期(2025-2026)：A=64.19%, B=60.46%, C=70.19%, D=75.53%。仅展示，不参与PASS/FAIL判断。
  - **预注册验收标准未全部通过**：
    - 夏普方向不一致（研究期D>A 0.62 vs 0.60，验证期D<A 0.71 vs 0.75）❌
    - 验证期收益D-A=-0.15% ≥ -2% → ✅
    - 验证期回撤：D绝对回撤-20.00% vs A-17.75%，恶化+2.25pp → ❌
    - 滑点方向：所有滑点下D>A → ✅
    - leave-one-year-out(分析期2019-2024): D>A 5/6=83.3% > 50% → ✅
    - 单年驱动：2020年D<A(-8.24%)，其他5年D>A，存在集中风险 ⚠️
  > **指标口径说明**：标准7以 `score_rank_1_4_weight`（模型评分排名前4只的实际仓位权重和）为主。`weight_order_top4`（按实际仓位从大到小排序的前4只权重和）作为辅助观察，两者不可混用。研究期D=48.73%>A=42.82%>B=39.82%，验证期D=40.79%>A=35.05%>B=33.30%，两期方向一致。

- **P1修复（本次最小口径修正）**：
  - **标准7口径**：明确区分 `weight_order_top4`（实际仓位排序）和 `score_rank_1_4_weight`（模型评分排名）。标准7判定以score_rank_1_4为主。
  - **B方案rank5持仓说明**：B方案（4×20%）实际最多持有4个行业ETF，不存在第5个槽位。slot_contribution中出现的entry_rank=5持仓是因为回测引擎rebalance逻辑买入了信号日排名第5的标的（候选池筛选、资金限制等导致）。已在报告中添加解释章节和明细表 `v1_3_step7_b_rank5_trades.csv`。正交归因中"删除rank5"修正为"将行业槽位从5减至4"。
  - **正交归因（RMB，entry_rank，period-specific）**：
    - 研究期 B-A: observed=-30,804.73, rank5=-142,595.51, r14=-25,237.80, residual=137,028.58
    - 研究期 C-B: observed=+33,283.13, defense=+64,181.80, r14=+97,808.47, residual=-128,707.14
    - 研究期 D-B: observed=+76,241.89, r14=+572,494.27, residual=-496,252.39
    - 研究期 D-A: observed=+45,437.16, rank5=-142,595.51, r14=+547,256.48, residual=-359,223.81
    - 所有行均满足 known_effects + residual = observed_diff（容差1.0元）
  - **预注册标准7（D score_rank_1_4 vs A/B，2019-2024）**：
    - 研究期 D=48.73% > A=42.82% > B=39.82% → PASS
    - 验证期 D=40.79% > A=35.05% > B=33.30% → PASS
    - **标准7：PASS**（研究期与验证期方向一致，以score_rank_1_4_weight为主）
    - 辅助观察：weight_order_top4 同样 D>A/B，但差异较小（研究期 D=60.89% vs A=49.54% vs B=49.82%）
  - **防御ETF贡献（C-B，mark-to-market）**：详见 `reports/v1_3_step7_defense_contribution.csv`。
  - **佣金（截止2024-12-31）**：A=49,409.20, B=33,780.86, C=40,620.21, D=44,083.02
  - **新增 7 份机制拆解 CSV（全部纳入Git）**：
    - `v1_3_step7_position_exposure.csv` — 逐日仓位敞口（industry_pct / defense_pct / cash_pct / top1-5 / weight_order_top4 / score_rank_1-4）
    - `v1_3_step7_slot_contribution.csv` — 按entry_rank分层的仓位贡献（rank1-5 PnL / active_days / avg_daily_pnl / max_drawdown_pct / max_drawdown_rmb / avg_weight），含period列
    - `v1_3_step7_slot5_yearly.csv` — 第5名行业ETF逐年
    - `v1_3_step7_yearly_metrics.csv` — 分年度指标（annual_return / monthly_win_rate / sharpe / max_drawdown / avg_exposure / trades / commission），含period列
    - `v1_3_step7_commission_summary.csv` — 佣金统计（n_buys / n_sells / n_stop_loss / total_commission），含period列
    - `v1_3_step7_orthogonal_attribution.csv` — 正交归因（observed_diff / rank5_effect / defense_effect / r14_effect / residual），含period列
    - `v1_3_step7_standard7_verification.csv` — 预注册标准7验证（period / scenario / avg_top4_weight / weight_order_top4 / score_rank_1_4_weight）
  - **测试**：22/22 通过（原8个 + 新增14个，含P1 FIX验证：slot_period_filtering、entry_rank_consistency、orthogonal_attribution_balance、standard7_period_boundary、drawdown_not_below_minus_100、monthly_win_rate_compounding、score_rank_vs_weight_order）
  - **验证器**：通过（全部检查项通过，含新增：period字段、2025-2026排除、正交归因平衡、entry_rank一致性、drawdown>=-100%、monthly_win_rate compound、report-CSV一致性）
  - **隔离运行**：通过（临时目录完整运行并验证）
  - **B0.4 slippage**：8/8 通过
  - **结论**：预注册标准未全部通过，D只能判定为机制观察候选，不得升级B0.4。但机制拆解证据完整，可支持后续 v1.4 集中度决策。
  - 交付物：`scripts/v1_3_step7_portfolio_orthogonal_ab.py`（P1修复版，~1430行，含--use-cached）、`scripts/validate_v1_3_step7_artifacts.py`（P1修复版，~328行）、`tests/test_v1_3_step7_portfolio_orthogonal.py`（22 passed）、`reports/v1_3_step7_portfolio_orthogonal.md`（P1修复版）、20份CSV数据文件（含7份新机制拆解CSV）。
  - 不修改B0.4策略、参数或冻结基线。
- **v1.3 Step 5: 动态组合广度与集中度可行性诊断 已完成（observer-only）**：
  - **只做observer诊断**：不修改交易规则、不制定动态参数、不回测动态仓位策略。不修改B0.4、生产策略、ETF池、数据库或调仓引擎。
  - **数据收集**：271个调仓日，每个调仓日记录所有16只行业ETF的total_score、signal_type、排名、候选数量、质量分位、市场状态。
  - **第5名价值分析**：
    - 完整20日观察样本81笔，平均20日收益+1.54%，胜率58.0%，相对Top4超额+0.81%。
    - 按候选数量分组：无单调趋势（n=5:-0.31%, n=6:-1.31%, n=7:4.51%, n=8:7.90%），样本量不足。
    - 按质量分组：high组+3.24%(29笔), medium组-2.55%(8笔), low组+1.17%(44笔)。medium组样本量不足。
    - 按分差分组：分差<2时+2.39%(34笔), 2-5时+0.87%(19笔), >=5时+0.78%(28笔)。分差小表现略好，但幅度不大。
  - **3-4只候选集中价值反事实**：
    - 3只候选：实际-0.29%，100%预算反事实-0.48%，风险放大。
    - 4只候选：实际+1.57%，100%预算反事实+1.97%，风险从-7.96%放大到-9.95%。
  - **研究期/验证期方向**：研究期0.98% vs 验证期1.22%，方向一致（均为正），但验证期仅18笔，统计功效有限。
  - **预注册决策规则**：
    - 研究期/验证期方向一致：⚠️ 部分满足（方向均为正，但验证期样本小）
    - 第5名价值可由候选广度/质量/相关性解释：❌ 未满足（关系不稳定，样本量不足）
    - 连续变量与分组结果方向一致：⚠️ 存疑（部分一致，但样本量不足）
    - 集中度收益改善足以补偿波动/最大损失：❌ 未满足（反事实未经实际交易验证）
    - 2025-2026不参与规则选择：✅ 满足
  - **结论**：**当前证据不足，继续使用固定B0.4结构**。不存在可解释、可预注册的动态宽度信号。建议继续观察，不在当前数据集上制定动态规则。
  - **交付物**：scripts/v1_3_step5_dynamic_breadth_diagnosis.py、reports/v1_3_step5_dynamic_breadth_diagnosis.md、reports/v1_3_step5_rebalance_events.csv、reports/v1_3_step5_fifth_candidate_events.csv、reports/v1_3_step5_concentration_counterfactual.csv、reports/v1_3_step5_summary.csv。
  - **不修改 B0.4 策略、参数或冻结基线。不进入任何参数调优或增强实施。**

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
- 普通信号以次日开盘价执行；等价表述为"T日收盘信号，T+1开盘交易"。
- 关键指标使用 `shift(1)`，已通过同日数据扰动测试。
- 止损采用"开盘检查并按开盘成交"的预置止损单假设。

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
- `tests/test_v1_3_step6_dynamic_fifth_slot.py`：16 passed。
  - LOO测试读取CSV调用生产函数leave_one_year_out，验证2019-2024（6年），真实结果C>A 1/6=16.7%，标准5判定FAIL。
  - LOO与annual_contribution定义区分验证。
  - 方案A/B/C配置正确性。
  - 动态引擎regime_map构建与cfg调整逻辑。
  - NaN/warmup回退B0.4（5行业）回归测试。
  - `STATE_NAMES`映射验证：1=强牛/2=弱牛/3=震荡/4=熊市。
  - `detect_history` regime_id与regime_name严格一致。
  - 机制归因表regime分布与`detect_history`输出一致。
  - 预注册标准评估逻辑（夏普方向、回撤绝对值比较、收益容忍度、滑点、LOYO严格>50%）。
  - B/C勾稽读取CSV验证：cash+positions_value=NAV、commission合计、最终NAV一致。
- A/B数据补齐实验：
  - 完整数据：NAV 2,761,288.07，804笔交易。
  - 排除补齐数据：NAV 2,809,091.21，801笔交易。
  - 首次NAV分歧：2026-06-08。

---

## 5. 实际工作区状态

当前分支已同步远端，但工作区不是干净状态：

- 已修改：`docs/CURRENT_STATE.md`
  - 本次 Step 6 更新，尚未提交。
- 已修改：`docs/CHANGES.md`
  - Step 6 记录，尚未提交。
- 已修改：`src/backtest.py`
  - nav_records 增加 `industry_value` / `defense_value`（不改变交易逻辑）。
- 已修改：`reports/ab_test_data_fill_impact.md`
  - 仅文件末尾换行差异；不要在无关任务中处理。
- 未跟踪：`reports/etf_rotation_public_resources_research.md`
- 未跟踪：`scripts/phase7_1_survivorship_bias_audit_v2.py`
- 未跟踪：`reports/b0_2_vs_b0_3_20260622_111239.md`
- 未跟踪：`reports/baseline_B0.3_20260622_111239.md`
- 新增：`scripts/v1_3_step4_portfolio_structure_ab.py`
- 新增：`reports/v1_3_step4_portfolio_structure_ab.md`
- 新增：`reports/v1_3_step4_portfolio_metrics.csv`
- 新增：`reports/v1_3_step4_portfolio_daily_attribution.csv`
- 新增：`scripts/v1_3_step4_slippage_test.py`
- 新增：`reports/v1_3_step4_slippage_test.md`
- 新增：`reports/v1_3_step4_slippage_test.csv`

- 新增：`scripts/v1_3_step5_dynamic_breadth_diagnosis.py`
- 新增：`reports/v1_3_step5_dynamic_breadth_diagnosis.md`
- 新增：`reports/v1_3_step5_rebalance_events.csv`
- 新增：`reports/v1_3_step5_fifth_candidate_events.csv`
- 新增：`reports/v1_3_step5_concentration_counterfactual.csv`
- 新增：`reports/v1_3_step5_summary.csv`

- 新增：`scripts/v1_3_step6_dynamic_fifth_slot_ab.py`
- 新增：`reports/v1_3_step6_dynamic_fifth_slot_ab.md`
- 新增：`reports/v1_3_step6_nav_A.csv`
- 新增：`reports/v1_3_step6_nav_B.csv`
- 新增：`reports/v1_3_step6_nav_C.csv`
- 新增：`reports/v1_3_step6_trades_A.csv`
- 新增：`reports/v1_3_step6_trades_B.csv`
- 新增：`reports/v1_3_step6_trades_C.csv`
- 新增：`reports/v1_3_step6_regime_switches.csv`
- 新增：`reports/v1_3_step6_mechanism_attr.csv`
- 新增：`reports/v1_3_step6_regime_summary.csv`
- 新增：`tests/test_v1_3_step6_dynamic_fifth_slot.py`

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

Step 6 已完成。动态第5槽位实验结论：**预注册标准未全部通过，C只能判定为机制观察候选，不得升级B0.4**。等待用户决定下一步研究方向。

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

恢复后只继续"v1.3 Step 6 完成，预注册标准未通过，等待用户决定下一步研究方向"，不要重新展开历史研究。
