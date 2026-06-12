# A股ETF轮动量化策略 v1.0

## 项目简介

基于趋势跟踪+动量轮动的A股行业ETF量化交易策略，支持本地持久化运行，避免云端上下文丢失问题。

## 项目结构

```
etf_rotation_model/
├── database/              # SQLite数据库文件
│   └── etf_model.db      # 主数据库
├── src/                   # 源代码
│   ├── config.py          # 全局配置
│   ├── database.py        # 数据库操作封装
│   ├── data_fetcher.py    # 数据获取（AKShare）
│   ├── strategy.py        # 策略引擎v1.0
│   └── backtest.py        # 回测引擎
├── reports/               # 回测报告输出
├── signals/               # 交易信号输出
├── notebooks/             # Jupyter分析笔记本
├── main.py                # 命令行主入口
├── app.py                 # Streamlit可视化界面
├── requirements.txt       # Python依赖
└── README.md              # 本文件
```

## 快速开始

### 1. 安装依赖

```bash
cd etf_rotation_model
pip install -r requirements.txt
```

### 2. 下载历史数据

```bash
python main.py update --full
```

### 3. 运行回测

```bash
# 全区间回测
python main.py backtest --save

# 样本内回测 (2019-2023)
python main.py backtest --sample in --save

# 样本外验证 (2024-至今)
python main.py backtest --sample out --save
```

### 4. 生成交易信号

```bash
python main.py signal --save
```

### 5. 查看状态

```bash
python main.py status
```

### 6. 启动可视化界面

```bash
streamlit run app.py
```

## 策略参数

### 评分体系（满分100）

| 维度 | 权重 | 计算方式 |
|------|------|---------|
| 趋势强度 | 30% | 收盘价>20日均线(+15) + >50日均线(+10) + 均线斜率>0(+5) |
| 趋势确认 | 20% | 连续在20日均线之上的天数×4分，最多5天 |
| 动量 | 25% | 20日收益率的横截面排名（百分位×25） |
| 成交量 | 15% | 放量上涨(+15) / 放量(+10) / 普通(+5) |
| 波动率 | 10% | 适中波动率1-4%(+10) / 较高4-6%(+5) |

### 入场条件（同时满足）

1. 趋势得分 ≥ 15
2. 确认得分 ≥ 4（至少1天在均线之上）
3. 总评分 ≥ 40
4. 收盘价 > 20日均线 且 均线斜率 > 0

### 出场条件（任一满足）

1. 收盘价跌破20日均线
2. 单只回撤超过8%（止损）

### 仓位控制

- 最多持有5只ETF
- 单只上限15%
- 每周五调仓
- 交易费率0.03%双向，最低5元

### 大盘择时

- 沪深300 > 20日均线：满仓
- 20-50日均线之间：半仓
- < 50日均线：20%防御仓位

## ETF标的池

| 代码 | 名称 |
|------|------|
| 512480.SH | 半导体ETF |
| 515230.SH | 软件ETF |
| 515880.SH | 通信ETF |
| 512010.SH | 医药ETF |
| 159928.SZ | 消费ETF |
| 516160.SH | 新能源ETF |
| 516110.SH | 汽车ETF |
| 512800.SH | 银行ETF |
| 512000.SH | 券商ETF |
| 512660.SH | 军工ETF |
| 512980.SH | 传媒ETF |
| 512400.SH | 有色金属ETF |
| 516120.SH | 化工ETF |
| 516960.SH | 基建ETF |
| 516650.SH | 煤炭ETF |

## 数据源

- **主数据源**: [AKShare](https://www.akshare.xyz/)（免费）
- **备用数据源**: Tushare / iFinD（需配置API密钥）

## 数据库表结构

| 表名 | 用途 |
|------|------|
| market_data | 历史行情（OHLCV） |
| daily_scores | 每日技术指标与评分 |
| trade_signals | 交易信号记录 |
| portfolio | 持仓跟踪 |
| backtest_results | 回测绩效存档 |
| run_logs | 运行日志 |

## 关键修复（vs云端版本）

1. ✅ **未来数据保护**: 所有指标使用shift(1)，确保决策只用前一日数据
2. ✅ **仓位控制修复**: 严格限制持仓≤5只，避免累积
3. ✅ **交易费率**: 加入0.03%双向佣金，最低5元
4. ✅ **样本内外分离**: 支持独立验证策略稳健性
5. ✅ **本地持久化**: SQLite数据库，数据不丢失

## 待优化项

- [ ] 主力资金真实数据接入
- [ ] 基本面数据（ROE、营收增速）
- [ ] 参数网格搜索优化
- [ ] 自动化部署（定时任务）
- [ ] 邮件/微信通知推送

## 常见问题

**Q: 数据下载慢或失败？**
A: AKShare接口可能受网络影响，可尝试多次运行 `python main.py update`

**Q: 如何更换数据源？**
A: 修改 `src/config.py` 中的 `DATA_SOURCE['primary']` 为 'tushare' 或 'ifind'

**Q: 如何调整策略参数？**
A: 使用Streamlit界面实时调整，或修改 `src/config.py` 中的 `STRATEGY_CONFIG`

## 免责声明

本策略仅供学习和研究使用，不构成投资建议。量化交易有风险，入市需谨慎。
