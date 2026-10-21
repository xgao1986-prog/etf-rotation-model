# 请 Trae 定向复审：真实价格架构 v3（Codex 复核两个必须解决的问题）

## 一、背景与授权边界（请先读）

项目 `D:\etf_rotation_model`，分支 `feature/paper-trading-ui-v0.3`（HEAD bbbd466，本轮**未提交未推送**）。

你上一轮（ROUND2）拼接错误数据修复后，用户引入 Codex 复核，提出**两个必须解决的问题**：

1. **真实成交价格与评分价格必须分开使用**——ROUND2 把换算后等效价传入虚拟盘参与订单股数、成交和估值，"金额自洽但股数不真实"不能作为验收理由；实盘助手需同样核查
2. **重新处理成交量**——不得把价格换算倍数直接套用到成交量；统一成交量单位；单独核实拆分前后处理方式，确保不凭空制造放量（重点：512480 6/29~7/3 的成交量与评分变化）

另有表述修正要求："盘中快照冒充收盘"改为"旧数据与核验来源不一致，原因尚待确认"；B0.4 冻结快照不动；先在副本验证再修正式库；更新文档后交你定向复审。

授权边界同 ROUND2：允许修运行库数据；不允许动 B0.4 冻结快照/冻结指标/交易规则；备份留痕。

---

## 二、问题一的解决：价格双轨制

**核心改动**：`market_data` 不再存"锚定换算后等效价"，改为——

- `open/high/low/close/volume` = **iFinD none 真实值**（adjust_type='real_spliced'），是交易/估值/股数/佣金的唯一合法口径
- `adj_close` = fwd × 校准因子（校准因子 = 库内既有 adj_close(锚)/fwd(锚)，与库内既有 adj 尺度衔接），是评分的唯一价格来源

**评分侧**（`database.apply_adjustment_for_scoring`，新增）：评分输入 df 的 OHLC ×= adj_close/close、volume ÷= adj_close/close（=拆前等效份数），返回副本不改原数据。接入点：

- `src/backtest.py` run()：core/fallback/defense 三路评分循环均传调整后副本；**成交与 NAV 仍用真实 close**
- `app.py`：`_recalculate_scores`（1690 行）与页面内联重算（2082 行起）共 6 处 `engine_calc.calculate_xxx` 调用统一包一层

**成交侧**：虚拟盘/回测的订单股数、成交价、持仓估值、佣金全部基于真实 close——股数 = 金额÷真实价（100 股整数手），佣金 = max(股数×真实价×0.0003, 5)，不再出现"股数不真实"。

**实盘助手**（`scripts/live_generate_trade_plan.py`）：**无需改码**。核查结论：候选来自评分排序（评分已走调整价），下单价格取 `market_data.close`——迁移后该列即真实市场价，股数/佣金/现金自动按真实价正确。

**拆分处理**（`corporate_actions` 新表 + runner 盘前调整）：持仓穿越拆分日，盘前 `_apply_corporate_actions` 执行 shares×=ratio、cost_price÷=ratio、last_price÷=ratio——**市值与总资产不变**，不会虚增也不会腰斩，成本价保持真实可交易口径。

---

## 三、问题二的解决：成交量口径定案

**定案规则**：`volume` 列恒为 **iFinD none 真实成交份数**，全库统一单位"份"，任何环节不得乘价格换算因子（旧 `_splice_ticker_with_fwd` 的 volume×factor 逻辑已删除）。

**评分用量怎么处理拆分**（不造假的关键论证）：评分输入的换算规则量为 `volume ÷ adj_factor`（adj_factor = adj_close/close）。含义是"按拆前等效份数比较"：

- 拆分前：真实 8 亿份 ÷ 因子 2 = 16 亿等效份（拆前 1 份 = 拆后 2 份，÷adj_factor 把拆前量统一到拆后坐标系）
- 拆分后：真实 16 亿份 ÷ 因子 1 = 16 亿份——同一坐标系下与拆前可直接比

即换算规则量 = 份额按拆分比例折算后的"拆后等效份数"，分子分母（当日与 MA20 基准）始终同坐标系。

**核验数据**（正式库迁移后实测）：512480 6/26~7/6 存储 volume 与 iFinD none 逐日对照 **0 天不一致**（8.29 亿~15.96 亿份均为真实值）；515880 拆分周 70.7 亿→91.3 亿份均为真实值。

**量比连续性实测**（`rule_vol = volume÷adj_factor`，MA20 基准同坐标系）：

| 日期 | close | 真实 volume | 评分规则量 | volume_ratio |
|---|---|---|---|---|
| 2026-07-02（拆前） | 2.700 | 10.96 亿份 | 21.91 亿等效份 | 1.37 |
| 2026-07-03（拆后首日） | 1.331 | 15.95 亿份 | 15.95 亿份 | **0.99** |
| 2026-07-06 | 1.334 | 15.96 亿份 | 15.96 亿份 | 0.99 |

拆分日量比从 1.37 平滑落到 0.99，**无假跳变**——若按 ROUND2 的 volume×factor 存储，拆前规则量会虚增 2 倍、量比会在拆分日被人为砍半，这才是"凭空改变放量评分"；v3 从根上消除。

---

## 四、代表性验证（你要求的三个场景，新增测试文件全覆盖）

`tests/test_real_price_adjustment.py` 新增 11 项，用生产 `run_daily` 入口：

| 场景 | 验证点 | 结果 |
|---|---|---|
| 拆分后新建仓（512480，2026-07-03） | 建议价=真实市场价 1.331；买入股数按真实价取整手；佣金 max(股数×价×0.0003, 5)；现金扣减=股数×价+佣金，分毫不差 | ✅ |
| 持仓跨越拆分（515880，7/6 生效） | 拆分日盘前 shares×2、cost_price÷2、last_price÷2；总资产前后一致（不虚增不腰斩）；止损检查用调整后成本不会被误触发 | ✅ |
| 量比/成交量边界（512480 6/29~7/3） | 存储 volume=真实值；评分输入规则量跨拆分连续（÷adj_factor），MA20 基准与当日同坐标系，volume_ratio 无假跳变 | ✅ |

另：`apply_adjustment_for_scoring` 无 adj_close 列或因子恒 1 时**恒等返回**——B0.4 冻结路径（测试只喂 7 列无 adj_close）零变化，冻结指标天然隔离。

**测试状态**：`pytest tests/ -q` → **388 passed / 2 failed**。两个失败（test_missing_data_antipattern、test_v1_3_step6_regime 分布）已用 `git worktree` 在 HEAD 干净副本复证为既有问题，与本轮无关。

---

## 五、正式库迁移执行记录（副本验证通过后才执行）

`scripts/migrate_real_prices_v3.py`（幂等，九步：备份→分段双序列拉取→拆分登记→删旧拼接行→515880 correction ÷2→生产路径重生成→全历史 adj_close 重算→评分重算→验证包）：

| 步骤 | 结果 |
|---|---|
| 副本验证（tmp_migration/etf_model_copy.db） | 验证包全 PASS 后放行 |
| 备份 | `database/backups/etf_model_20261020_172326_pre_real_price_v3.db`（30,621,696 字节） |
| 删旧等效拼接行 | 1389 行（source='iFinD-KimiGateway'，>=2026-06-30） |
| 双序列重生成 | 1408 行，19/19 成功，status=success |
| 拆分登记 | 9 条（2019 以来 none/fwd 比率阶跃>20% 检出） |
| adj_close 重算 | 29530 行（adj = close×fwd/none） |
| 评分重算 | 33895 条（B0.4 标准配置 STRATEGY_CONFIG） |

**验证包（正式库实测输出）**：
- (a) B0.4 快照 SHA-256 前缀 `dbdb041d` 不变 ✅
- (b) 512480 6/26~7/6 存储 close/volume 与 iFinD none 逐日对照，volume 0 天不一致 ✅
- (c) 拆分边界 adj 收益率 2026-07-02→07-03 = -1.41%（真实行情，无假跳变）✅
- (d) corporate_actions 9 条（159928 2021-06-25 ratio=4、512000 2025-08-04 ratio=2、512010 2021-06-28 ratio=3.905、512480 2021-03-29 ratio=1.966 + 2026-07-03 ratio=2、512800 2025-07-07 ratio=1.999、515880 2026-02-03 ratio=2.998 + 2026-07-06 ratio=1.999、516160 2024-09-18 ratio=0.309）✅
- (e) forward_spliced 残留 0 ✅
- 补充：全库 OHLC 自相矛盾行（high<low / open>high×2 等粗检）= 0 ✅

---

## 六、副本验证中实测捕获并修复的 3 个 bug（如实披露）

1. **插件 CSV 日期格式陷阱**：插件返回的 time 列存在三种格式——int（`20240102` 被 read_csv 读成 int64）、字符串 `YYYYMMDD`、ISO `YYYY-MM-DD`（随节点不同）。int 直接喂 `pd.to_datetime` 被当成纳秒时间戳 → 全部解析成 1970-01-01，曾导致拆分检测登记 135,344 条垃圾（全挤在 1970-01-01）。修复：先转字符串再嗅探（8 位纯数字走 `%Y%m%d`，其余 `format="mixed"`），三种格式单测通过
2. **515880 correction 行 ÷2 漏字段**：旧 correction 行（7/7~7/10）此前被 ×2 成拆前口径，迁移步骤 5 原本只 ÷2 close/volume/adj_close，漏了 open/high/low——副本验证实测抓到 7/7 行 open=1.5 > close=0.761 的自相矛盾。修复：OHLCV 全部 ÷2
3. **预检拆分排除精确匹配的边界**：迁移删行重建会在拆分日后留下空档（6/30~7/6 重写），拆分跳变显现为空档后第一交易日（515880 报在 7/7 而非登记的 7/6），`detect_splice_breaks` 精确日期匹配漏排 → status 被误压 partial。修复：已登记拆分日 ±7 自然日窗口内跳变均排除（只影响预检告警，不影响数据正确性）

---

## 七、表述修正（按用户要求）

"盘中快照冒充收盘"确定性表述已全部改为"**旧数据与核验来源不一致，原因尚待确认**"：CHANGES.md、CURRENT_STATE.md、DATA_UPDATE_FIX_v2_DESIGN.md（2 处）、TRAe_REVIEW_REQUEST_ROUND2（3 处）、fix_splice_errors.py 注释（3 处）。512480 的 6/22~6/29 偏差原因仍定案为"尚待确认"。

---

## 八、代码与文档改动清单

```
M  src/database.py                     # corporate_actions 表 + save/get + apply_adjustment_for_scoring
M  src/data_fetcher.py                 # 双序列拉取 + _detect_splits_from_none_fwd + _splice_ticker_real_prices
                                       # + detect_splice_breaks 拆分日窗口排除（替代精确匹配）
M  src/backtest.py                     # 评分输入 apply_adjustment_for_scoring（3 处）
M  app.py                              # 6 处评分调用 + corporate_actions provider 注入
M  src/paper_trading/runner.py         # run_daily(corporate_actions=) + 盘前 _apply_corporate_actions
M  src/paper_trading/ui_service.py     # run_accounts(corporate_actions=) + provider 注入
?? scripts/migrate_real_prices_v3.py    # 新建：幂等迁移主脚本（curl 备用传输 --transport curl）
?? tests/test_real_price_adjustment.py  # 新建：11 项代表性验证
M  tests/test_data_fetcher.py          # 15 项更新（双序列 mock + 真实价语义）
?? docs/REAL_PRICE_ARCH_v3_DESIGN.md   # 本轮方案设计文档
M  docs/CHANGES.md                     # v3 章节 + 迁移执行结果
M  docs/CURRENT_STATE.md               # 第 11 节补第三轮 v3 记录
?? docs/TRAe_REVIEW_REQUEST_ROUND3_20261020.md  # 本文档
```

---

## 九、未解决问题（如实披露）

1. **本机 Python TLS 栈对 agent-gw 握手失败**（OpenSSL 1.1.1t/3.5.7 均复现，curl/Schannel 正常，16:22 尚可用 16:27 起失效，疑服务端 TLS 配置变化）——外部端点问题。迁移用 curl 直连信封绕过（`--transport curl`，数据与 iFinD 逐值一致）；网关恢复后 `--transport auto` 自动优先走原生 SDK
2. **AKShare fallback 仍未独立验证**（环境原因，同 ROUND2）
3. **516160.SH ratio=0.309（2024-09-18，<1）**：双序列检出，疑份额合并/缩股类行动。虚拟盘 `_apply_corporate_actions` 对 ratio<1 同样处理（shares×0.309），语义为"1 股变 0.309 股"方向是否正确，请你复核（无当前持仓，无即时影响）
4. 本轮不提交不推送，待你复审通过后由用户决定提交策略

---

## 十、请你定向复审的问题

1. **价格双轨制是否有漏网路径**：除 backtest.py / app.py 这 9 处评分调用外，是否还有其它从 market_data 取价做"评分/信号"的代码路径未走 `apply_adjustment_for_scoring`？
2. **成交量换算规则**（存储恒为真实值；评分量 = volume÷adj_factor = 拆后等效份数，分子分母同坐标系）是否认可？实测 512480 拆分日量比从 1.37 平滑落到 0.99（上表），无假跳变
3. **代表性验证三场景**的测试覆盖是否足够？是否还需要补"拆分日当日卖出/止损触发"等边界？
4. **516160 ratio=0.309** 的处理方向是否正确？
5. 是否还有 BLOCKED 项？如无，是否可转 APPROVED（待用户决定提交）？
