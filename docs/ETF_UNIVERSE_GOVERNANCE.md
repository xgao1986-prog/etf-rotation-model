# ETF 池治理与数据分层文档 v0.1

> 文档版本：v0.1
> 最后更新：2026-06-26
> 状态：研究数据扩展层初始化

## 1. 核心原则

### 1.1 数据分层

```
正式交易层（B0.4）      ← 实盘助手使用
├── 16 只行业 ETF
├── 2 只防御 ETF
└── 沪深 300 基准

研究观察层（observer）  ← 研究数据扩展层
├── 申万行业指数（一级/二级）
├── 概念/主题 ETF 观察池
├── 新 ETF 发现队列
└── 行业-ETF 映射关系
```

### 1.2 关键红线

| 红线 | 说明 |
|------|------|
| **不自动纳入交易** | 研究数据不进入 B0.4 实盘信号 |
| **不修改正式池** | B0.4 的 18 只 ETF 不变 |
| **用户确认制** | 后续是否纳入必须用户明确确认 |
| **实盘助手独立** | v0.2 数据更新只使用正式池 |

### 1.3 数据流向

```
研究数据收集 → data/research/*.csv
                    ↓
            研究分析与观察
                    ↓
            用户确认后手动纳入
                    ↓
            修改 config.py 正式池
                    ↓
            重新回测验证
                    ↓
            通过后才进入实盘
```

## 2. 正式交易池（B0.4）

### 2.1 当前构成

- **16 只行业 ETF**：覆盖 A 股主要行业（科技、医药、消费、新能源等）
- **2 只防御 ETF**：国债、黄金等低相关性资产
- **沪深 300**：基准指数

### 2.2 变更流程

1. **提案**：在研究报告中提出纳入/剔除建议
2. **数据验证**：至少 3 个月观察期数据
3. **回测验证**：完整回测，对比 B0.4 基线
4. **用户确认**：用户明确同意变更
5. **修改 config.py**：更新 ETF_UNIVERSE / DEFENSE_UNIVERSE
6. **版本标签**：打标签记录变更

## 3. 研究观察池

### 3.1 申万行业指数

**目的**：观察行业轮动，为 ETF 选择提供宏观参考

**数据文件**：
- `data/research/industry_index_daily.csv` — 日线行情
- 字段：index_code, index_name, date, open, high, low, close, volume, amount

**更新频率**：每日收盘后（可选，不强制）

**使用限制**：
- 仅用于研究观察
- 不直接生成交易信号
- 不与 B0.4 评分合并

### 3.2 概念/主题 ETF 观察池

**目的**：跟踪新兴主题，发现潜在纳入标的

**数据文件**：
- `data/research/concept_etf_daily.csv` — 日线行情
- `data/research/etf_watch_universe.csv` — 观察池元数据
- `data/research/etf_theme_mapping.csv` — 主题映射

**状态定义**：

| 状态 | 说明 |
|------|------|
| **watch** | 正在观察，数据收集中 |
| **duplicate** | 与现有正式池标的重复，不纳入 |
| **candidate** | 潜在候选，需进一步验证 |
| **rejected** | 已拒绝，数据保留但不再跟踪 |

**判断规则**：
- 如果主题与现有 16 只行业 ETF 重叠 → **duplicate**
- 如果是新行业/新主题 → **watch**
- 观察期（≥3个月）夏普、收益、回撤优于 A → **candidate**
- 观察期表现不佳或数据不足 → **rejected**

### 3.3 新 ETF 发现

**扫描来源**：
- AKShare 新上市 ETF 列表
- 交易所公告
- 行业媒体报道

**处理流程**：
1. 发现新 ETF
2. 查询跟踪指数和主题
3. 判断是否已有同类主题 ETF
4. 如果同类重复 → **duplicate**
5. 如果是新主题 → **watch**
6. 收集 ≥3 个月数据后评估

## 4. 数据文件规范

### 4.1 industry_index_daily.csv

```csv
index_code,index_name,date,open,high,low,close,volume,amount,update_time
801010,农林牧渔,2024-01-02,3200.12,3250.34,3190.56,3245.67,123456789,9876543210,2026-06-26
```

### 4.2 concept_etf_daily.csv

```csv
ticker,name,date,open,high,low,close,volume,amount,source,update_time
159992.SZ,创新药ETF,2024-01-02,1.023,1.045,1.012,1.038,1234567,1234567890,AKShare,2026-06-26
```

### 4.3 etf_watch_universe.csv

```csv
ticker,name,tracking_index,theme_tag,sector_mapping,listing_date,aum,avg_volume,status,data_days,notes,update_time
159992.SZ,创新药ETF,CS创新药,医药创新,医药生物,2021-03-15,5.2,1234567,watch,365,潜在候选,2026-06-26
```

### 4.4 etf_theme_mapping.csv

```csv
ticker,theme,sector,overlap_with_existing,confidence,update_time
159992.SZ,创新药,医药生物,512010.SH,0.8,2026-06-26
```

## 5. 脚本说明

### 5.1 research_update_industry_data.py

```bash
py scripts/research_update_industry_data.py --date 2026-06-26
```

- 获取申万一级行业指数日线
- 保存到 `data/research/industry_index_daily.csv`
- 缺数据时只警告，不影响实盘

### 5.2 research_update_concept_etf_data.py

```bash
py scripts/research_update_concept_etf_data.py --date 2026-06-26
```

- 获取观察池概念 ETF 日线
- 保存到 `data/research/concept_etf_daily.csv`
- 更新 `etf_watch_universe.csv` 中的数据天数

### 5.3 research_scan_new_etfs.py

```bash
py scripts/research_scan_new_etfs.py --min-listing-days 30
```

- 扫描近 N 天新上市 ETF
- 查询主题/行业映射
- 判断是否与现有池重复
- 输出到 `etf_watch_universe.csv`
- 生成 `reports/research/universe_watch_report.md`

## 6. 报告

### 6.1 观察报告

`reports/research/universe_watch_report.md` 包含：
- 当前观察池概况
- 新 ETF 发现列表
- 重复判断结果
- 潜在候选推荐
- 明确声明：数据仅用于研究，不进入交易逻辑

## 7. 版本与变更

| 版本 | 日期 | 变更 |
|------|------|------|
| v0.1 | 2026-06-26 | 初始建立研究数据扩展层 |

## 8. 声明

> **行业/概念ETF数据目前仅用于研究观察，不进入B0.4交易逻辑。**
> 
> 任何研究数据的纳入都必须经过：
> 1. 数据收集（≥3个月）
> 2. 回测验证（对比B0.4基线）
> 3. 用户确认
> 4. 修改 config.py 正式池
> 5. 版本标签记录
