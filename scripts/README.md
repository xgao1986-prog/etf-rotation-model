# 🛠️ 项目工具脚本集

> 这些脚本从 Kimi Work 技能库复制到项目中，供 Kimi 和 Codex 直接调用。
> 来源技能：`fund-risk-analyzer`、`stock-tech-analysis`、`stock-assistant`

---

## 📁 脚本清单

| 脚本 | 来源技能 | 功能 | 输入 | 输出 |
|------|---------|------|------|------|
| `etf_screener.py` | fund-risk-analyzer | ETF多维对比分析 | NAV CSV | 年化收益/最大回撤/夏普/相关性矩阵 |
| `compute_indicators.py` | stock-tech-analysis | 15+技术指标计算 | OHLCV CSV | MA/MACD/RSI/布林带/KDJ/ATR等 |
| `stock_atom.py` | stock-assistant | 股票数据原子能力 | 股票代码 | 报价/日K/批量技术指标JSON |
| `kimi_tech.py` | stock-assistant | 技术指标计算 | 股票代码 | MA/RSI/MACD等 |

---

## 🔧 使用说明

### 1. etf_screener.py — ETF对比分析

```bash
# 基础对比（从NAV CSV计算）
python scripts/etf_screener.py --input data/nav_data.csv

# 自定义无风险利率 + 导出CSV
python scripts/etf_screener.py --input data/nav_data.csv --risk-free 0.03 --output report.csv

# JSON输出（便于程序处理）
python scripts/etf_screener.py --input data/nav_data.csv --json
```

**CSV格式要求：** 第一列为日期，后续每列为一只ETF的净值
```
date,半导体ETF,软件ETF,通信ETF,...
2024-01-02,1.000,1.000,1.000,...
2024-01-03,1.010,1.005,1.008,...
```

**输出指标：**
- 年化收益率
- 最大回撤
- 夏普比率
- 年化波动率
- 相关性矩阵

---

### 2. compute_indicators.py — 技术指标计算

```bash
# 计算全部指标
python scripts/compute_indicators.py data/ohlcv.csv

# 输出最近5行
python scripts/compute_indicators.py data/ohlcv.csv --last-n 5

# 保存到文件
python scripts/compute_indicators.py data/ohlcv.csv --output indicators.csv
```

**CSV格式要求：** 必须包含 `date,open,high,low,close,volume` 列

**计算指标：**
- 移动平均线：SMA(5/10/20/60)、EMA(12/26)
- MACD：快线、慢线、柱状图
- RSI(14)
- 布林带(20,2)
- KDJ(9,3,3)
- ATR(14)
- 多空信号汇总

---

### 3. stock_atom.py — 股票数据获取

```bash
# 获取单股报价
python scripts/stock_atom.py quote 512480.SH

# 获取日K数据
python scripts/stock_atom.py kline 512480.SH --days 252

# 批量获取多只ETF
python scripts/stock_atom.py batch --symbols 512480.SH,515230.SH,515880.SH

# 获取自选股列表
python scripts/stock_atom.py watchlist
```

**数据源：** 东方财富、新浪财经等（通过AKShare）

---

### 4. kimi_tech.py — 技术指标（Kimi Code接口）

```bash
# 计算技术指标
python scripts/kimi_tech.py 512480.SH --indicators ma,rsi,macd

# 输出JSON
python scripts/kimi_tech.py 512480.SH --json
```

---

## 🔄 与主项目的集成建议

### 场景1：策略因子扩展
当前 `src/strategy.py` 只有4个维度（趋势/确认/动量/成交量/波动率）。
可以用 `compute_indicators.py` 补充：
- KDJ超买超卖信号
- 布林带突破信号
- ATR波动率确认

### 场景2：ETF池子定期体检
每月运行一次：
```bash
python scripts/etf_screener.py --input data/pool_nav.csv --output reports/monthly_screening.csv
```
检查池子内ETF的相关性是否恶化、夏普是否下降。

### 场景3：实时数据补充
`stock_atom.py` 可作为 `src/data_fetcher.py` 的备用数据源，
当AKShare接口不稳定时切换使用。

---

## ⚠️ 依赖要求

所有脚本依赖：
- Python 3.8+
- pandas
- numpy
- requests（stock_atom.py）

已在 `requirements.txt` 中覆盖。

---

## 📌 备注

- 这些脚本是**只读工具**，不会修改项目核心代码
- 修改脚本前请复制到 `scripts/custom/` 目录，保留原始版本
- 脚本输出可直接导入 `src/database.py` 写入SQLite

---

*安装时间：2025-06-12*
*来源：Kimi Work 内置技能库*
