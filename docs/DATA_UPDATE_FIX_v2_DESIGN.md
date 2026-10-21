# 行情更新修复 v2 设计文档（待验收）

> 生成时间：2026-10-20
> 修订：2026-10-20（第二轮，Trae 复审 BLOCKED 后修复拼接错误数据）
> 状态：待验收（PENDING ACCEPTANCE）
> 范围：仅修复"更新最新行情"工程链路，不修改 B0.4 策略、参数、ETF 池、冻结数据快照

---

## 1. 问题清单（Codex 确认 + 本地复核）

| # | 问题 | 现状 |
|---|------|------|
| 1 | AKShare 单次请求约 4.34s，程序只等 3s、重试 1 次 | 大量提前放弃 |
| 2 | 预检一只 ETF 失败就停止全部更新 | health_check 失败直接 return |
| 3 | 页面 rerun 后失败提示消失 | 结果未持久化到 session_state |
| 4 | 用全库最新日期作下载起点 | get_latest_date()=全局 MAX，per-ticker 缺口被漏掉 |
| 5 | 数据库多数标的停在 2026-06-29，3 只到 07-10，评分停在 06-29 | 数据过期近 4 个月 |
| 6 | 无逐只结果报告 | 用户无法看到成功/失败/新增条数/逐只最新日期 |

## 2. 数据源审计结论（2026-10-20 实测）

### 2.1 iFinD 能力（Kimi 插件网关，已实测可调用）

- 插件：`managed/ifind`，脚本 `scripts/ifind_tool.py`，SDK `agent_gw`（Python310 已安装）
- 关键 API：`ifind_get_price`（历史日频，每次最多 3 ticker，区间 ≤3 年）
- `adjust` 参数：A 股默认 forward（前复权）；支持 forward/backward/none
- 凭证：KIMI_API_KEY 环境变量（运行时注入），不写代码、不进提交

### 2.2 复权口径核查（关键发现）

**数据库现状**（`market_data` 表，`adjust_type` 混用 qfq/forward/none，均为既有事实）：

| 标的 | 数据库最新 | 缺口期拆分/除权 | 衔接说明 |
|------|-----------|----------------|---------|
| 512480.SH 半导体 | 06-29 (2.78) | **7/2→7/3 发生 1:2 份额拆分** | 绝不可直接拼 iFinD forward（6/29 fwd=1.455，-47% 假跳变） |
| 515880.SH 通信 | 07-10 (0.779) | **7/3→7/6 发生 1:2 拆分** | 数据库内部已断裂：6/29=1.7(拆前) → 7/7=0.761(拆后)；缺 6/30~7/6 |
| 512400.SH 有色 | 06-29 | 有除权（6/29 前已发生） | factor 锚定法自动吸收 |
| 511010.SH 国债 | 06-29 | 有除息（债息） | factor 锚定法自动吸收 |
| 其余 14 ETF + 000300 | 06-29/07-10 | 无 | iFinD none 价与数据库已验证一致（512480 6/24-25 精确相等） |

**成交量口径**：数据库 volume 单位=手（iFinD 批次混入）；iFinD volume 标准单位=份。导入拼接时统一为等效口径（×factor，见 2.5 节）。

**⚠️ 原表述修正（2026-10-20 第二轮）**：本节原写"512480 数据库 6/29 close=2.78 vs iFinD none=2.909 属于冻结历史，不动"。Trae 复审指出该表述错误地把**运行库错误数据**当成可保留历史。用户已明确授权修复运行库错误（B0.4 冻结快照、历史冻结指标及交易规则保持不变）。经双源核查（iFinD none + Yahoo Finance 第三源），512480 于 6/22~6/29 批次**旧数据与核验来源不一致**（15/19 标的与 iFinD none 存在 -2.4%~+0.6% 随机偏差，000300 指数同批次也偏差 -1.48%，排除复权因素），**原因尚待确认**，已按第 5 节流程修复。

### 2.3 拼接算法：就近锚定因子法

对每只正式标的 t：
1. 取数据库全部已有 (date, close)。
2. 从 iFinD 拉 [最早缺失日-5, today] 的 **forward** 序列（含全历史复权，拆分已内部处理）。
3. 交易日历 = iFinD 000300.SH 的日期序列。
4. 缺失日 = 交易日历 ∩ (首条数据日, today] − 已有日期。
5. 对每个缺失日 D：锚定日 A = 数据库中 < D 的最近已有日；factor = close_db(A) / fwd_close(A)；等效 OHLC = fwd(D) × factor；**volume = vol_ifind(D) × factor（等效口径，见 2.5）**。
6. 数学性质：factor 在拆分边界自动吸收拆分比例（fwd 全历史统一复权），跨拆分拼接无假跳变；因子随锚定日分段更新，可处理 515880 这类"库内已有断裂+区间内缺失"的情形。
7. source='iFinD-KimiGateway'，adjust_type='forward_spliced'（如实标记非标准复权口径）。

### 2.5 等效口径与断裂预检（第二轮新增）

- **等效口径**：拼接段 OHLC、volume 全部乘以锚定因子 factor，使拼接段与库内既有段处于同一价格坐标系。策略内部（评分、动量、止损、估值）全部使用同一坐标系的收盘价，金额自洽；披露事项：等效 volume 不是真实成交量，等效价格不是真实成交价——虚拟盘未来若按真实价格成交需换算（当前虚拟盘无 512480/515880 持仓，无影响）。
- **断裂预检 `detect_splice_breaks`**：扫描日收益率 |r| > 35%（ETF 涨跌停约 10%，35% 基本可判定口径断裂）。`update_latest_data_v2` 在 iFinD 与 AKShare 两条路径都先预检：发现断裂时 status 压至 `partial`，message 明示"请先修复历史数据再更新"，**阻止误报成功**（第二轮新增，测试覆盖）。

## 3. 实现规格

### 3.1 新文件 `src/ifind_gateway.py`

- `is_available()`：检测插件路径 + Python + agent_gw（结果缓存）
- `get_price(tickers, start, end, adjust, timeout=60, max_retries=2)`：
  - subprocess 调 ifind_tool.py call ifind_get_price
  - 自动 3 只/组拆分；单组超时 60s；指数退避重试 2 次；整体耗时预算（默认 20 分钟，可配）
  - 读回 CSV → 标准 DataFrame(open/high/low/close/volume,thscode,time)
  - 任何一步失败返回部分结果 + 失败明细，不抛裸异常
- 环境配置：IFIND_PLUGIN_DIR / IFIND_PYTHON 环境变量优先，默认路径兜底

### 3.2 改造 `src/data_fetcher.py`

新增 `update_latest_data_v2(db, source='ifind', ...) -> dict`：
```
{
  status: success|partial|no_data|source_unavailable,
  saved_count, fetched_count,
  per_ticker: {t: {new_rows, latest_date, gaps: [...], error}},
  gaps: [...],            # 全库中间缺口清单
  splice_factors: {t: factor或分段},   # 拼接因子披露
  trading_calendar_end, elapsed_seconds, message
}
```
- 每只标的独立 try/except，单只失败不阻断
- AKShare 路径保留为 fallback（原有逻辑不删）
- 旧 `update_latest_data` 保留（向后兼容），app 改用 v2

### 3.3 改造 `app.py` 数据管理页

1. 三个日期指标卡：最新行情日期 / 全部正式标的共同完整日期（MIN per-ticker MAX）/ 最新评分日期。
2. 更新结果持久化：`st.session_state['last_update_result']`，rerun 后仍显示成功/失败/实际新增条数 + 逐只最新日期 st.dataframe。
3. 完整性检查：共同完整日期 < 最新行情日期 → 黄色警告"数据不完整"；= 才允许"全部成功"话术。
4. 行情有效后才触发评分重算；不完整时明确提示用户。
5. iFinD 可用状态纳入数据源状态卡片。
6. 数据管理页只操作 market_data；sector_market_data（行业研究）仍由独立入口，不混入。

### 3.4 测试 `tests/test_data_fetcher.py` 扩充

mock iFinD 网关，用临时数据库验证：
1. 超时重试与整体耗时上限
2. 单只失败不阻断其他标的
3. 日期缺口检测与补齐（构造 6/29、7/7 已有 → 补 6/30~7/6）
4. 拆分拼接：mock 缺口期含 1:2 拆分 → 验证无 -50% 假跳变
5. 重复导入幂等（INSERT OR IGNORE，saved_count 不重复计）
6. 逐只最新日期与缺口清单正确性

### 3.5 正式数据补齐流程

1. 备份 `database/etf_model.db` → `database/backups/etf_model_YYYYMMDD_HHMMSS_pre_data_update_v2.db`
2. 对 18 ETF + 000300 执行补齐（iFinD forward + 锚定拼接）
3. 重算评分（ strategy 评分管线），验证 daily_scores 截止日期推进
4. 抽查 3 只标的（含拆分标的 512480）拼接点连续性

## 4. 明确不做

- 不改 B0.4 任何策略参数、回测逻辑、冻结快照（SHA-256 校验通过：正式池 <=2026-06-18 全部行与修复前备份零差异）
- **第一轮原则"不覆盖/修改任何已有历史行情行"在第二轮按用户授权修订**：允许修复运行库中的错误数据（512480 与核验来源不一致段、515880 拆分错位段），但 B0.4 冻结段（<=2026-06-18）逐行比对零差异；所有修改留痕（source='iFinD-KimiGateway-correction' + 修复报告 reports/splice_fix_report_*.json）
- 不动 sector_market_data / sector_scores（行业研究数据，独立保存约定不变）
- 不生成、不猜测、不插值任何行情（修复全部以 iFinD none/forward 实测值为依据）
- 不提交、不推送，等待用户复核

## 5. 第二轮：拼接错误数据修复（2026-10-20，Trae BLOCKED → 修复 → 待复审）

### 5.1 问题定性

| 标的 | 错误 | 根因 | 修复方式 |
|------|------|------|---------|
| 512480.SH | 6/22~6/29 收盘价与核验来源不一致（6/29: 2.78 vs 核验值 2.909），原因尚待确认 | AKShare 批次与核验源存在偏差（Yahoo Finance 第三源辅证） | UPDATE 126 行历史段（修正锚点 6/29→2.909 后 factor 精确=2.0），删除 6/30~10-19 旧拼接段 1389 行，按新锚点重新拼接 |
| 515880.SH | 库内 6/29=1.7（拆前）→ 7/7=0.761（拆后），-49% 假断裂 | 1:2 拆分（7/6 生效）前后口径混杂 | 6/29 锚点修正 1.7→1.715，7/7~7/10 四行 ×2（0.761→1.522 等），7/13 起经 7/10 锚定自动延续，全段统一拆前口径 |

### 5.2 修复流程（先副本验证后正式库）

1. 修复脚本 `scripts/fix_splice_errors.py`（幂等可重跑）：iFinD 可用性校验 → 拉 none 基准 [6/22,7/10] → 逐行核对 UPDATE 历史段（跳过 source='iFinD-KimiGateway' 行，防覆盖等效口径）→ DELETE 错误拼接段 → 调 update_latest_data_v2 重新拼接 → JSON 修复报告。
2. 数据库副本全流程验证通过后，备份正式库（`database/backups/etf_model_20261020_141214_pre_splice_fix.db`，注意 141013 文件为 0 字节坏文件，勿用）再执行。
3. 修复后按 B0.4 标准配置重算评分（33877 条至 2026-10-19）。

### 5.3 验证结果（全部实测）

| 验证项 | 结果 |
|--------|------|
| B0.4 冻结段（19 只 <=2026-06-18）与修复前备份逐行比对 | 0 差异 |
| 条数守恒 | 更新前 29078 = 更新后 29078（UPDATE 126 + DELETE 1389 + INSERT 1389） |
| 512480 拆分边界 7/2→7/3 | -1.41%，无假跳变 |
| 515880 7/6→7/7 | +0.59%，-49% 断裂消失 |
| 评分连续性（512480/515880 拆分前后） | 平滑无跳变（60~65 平稳；65→45→25→10 逐步回落） |
| 重复更新幂等 | saved=0，状态"已是最新，无需更新" |
| 虚拟账户 | 512480/515880 无持仓无成交，不受影响 |
| UI 浏览器实测（localhost:8502 数据管理页） | 三日期卡均 2026-10-19；更新按钮端到端跑通 iFinD 拉取；结果持久化展示 |
| pytest tests/test_data_fetcher.py | 15 passed |

### 5.4 遗留披露

- AKShare fallback 路径未真实调用，标记"未独立验证"
- paper_trading 无拆分处理逻辑（架构级）：未来虚拟盘持仓穿越拆分日需股数 ×2 调整；等效口径下金额自洽但股数≠真实可交易股数，须在报告中披露
