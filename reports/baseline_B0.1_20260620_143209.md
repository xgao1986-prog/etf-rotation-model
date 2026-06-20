# B0.1 策略基准文件
# 冻结时间: 2026-06-20 14:32:09
# 数据截止日期: 2026-06-18

## 基本信息

| 项目 | 值 |
|------|-----|
| 基准版本 | B0.1 |
| 冻结时间 | 2026-06-20 14:32:09 |
| 数据截止日期 | 2026-06-18 |
| NAV起始日期 | 2019-08-15 |
| NAV结束日期 | 2026-06-18 |
| 回测区间 | 约 6.8 年 |
| 数据库行数 | 25,772 |
| 参与回测标的数 | 18 |
| 数据库总标的书 | 73 |

## 策略配置

| 配置项 | 值 |
|--------|-----|
| use_v2_rebalance | True |
| fallback_equity_enabled | False |
| rebalance_freq | weekly |
| rebalance_ordinal | 1 |
| rebalance_weekday | 3 (周四) |
| max_holdings | 5 |
| defense_max_holdings | 2 |
| total_max_holdings | 5 |
| max_position_per_etf | 0.15 |
| commission_rate | 0.0003 |
| min_commission | 5.0 |
| min_total_score | 40 |
| cooling_period | 0 |
| atr_stop_loss | 2.0 |
| trailing_stop | 0.10 |

## 完整绩效指标

| 指标 | 值 |
|------|-----|
| 总收益 | 170.64% |
| 年化收益 | 16.33% |
| 波动率 | 19.34% |
| 夏普比率 | 0.8442 |
| 最大回撤 | -21.37% |
| 交易笔数 | 792 |
| 买入次数 | 394 |
| 卖出次数 | 398 |
| 调仓次数 | 337 |
| 胜率 | 45.23% |
| 平均持仓数 | 3.55 |
| 最大持仓数 | 5 |
| 总佣金 | 68,527.03 |
| 初始资金 | 1,000,000.00 |
| 最终NAV | 2,706,375.56 |

## 对比旧逻辑

| 指标 | 旧逻辑 | 新逻辑(v2.5) | 变化 |
|------|--------|-------------|------|
| 总收益 | 132.59% | 170.64% | +28.70% |
| 夏普比率 | 0.7664 | 0.8442 | +10.15% |
| 最大回撤 | -19.02% | -21.37% | -12.38% |
| 交易笔数 | 695 | 792 | +13.96% |

## 版本信息

| 项目 | 值 |
|------|-----|
| Git Commit SHA | d5eb9cd572205b3c0469d960259ce4432a25fcf5 |
| 分支 | feature/v1.2.1-regime-adaptive |
| 工作区状态 | 有未提交修改（见下方） |

## 未提交修改文件

- M .gitignore
- M KIMI_CODEX_SYNC.md
- M reports/walk_forward_pool_backtest.md
- M src/backtest.py
- M src/config.py
- M src/data_fetcher.py
- M src/database.py
- M src/strategy.py

## 输出文件

- 对比报告: reports/contrast_report_20260620_143209.md
- 对比明细: reports/contrast_detail_20260620_143209.csv

## 备注

本次基准基于 v2.5 纯函数调仓引擎（plan_rebalance_v2_5），数据统一截止至 2026-06-18。
旧逻辑（v1.x）同期收益 132.59%，新逻辑提升 +28.70% 相对收益。
