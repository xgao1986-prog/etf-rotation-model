# 新 ETF 扫描报告

> 生成时间: 2026-06-26
> 状态: observer-only，不进入 B0.4 交易逻辑

## 说明

本报告记录新发现 ETF 的扫描结果。
**这些ETF仅用于研究观察，不进入 B0.4 交易信号、不影响止损、不影响调仓。**

## 扫描方法

- 数据源: AKShare `fund_etf_spot_em()`
- 扫描频率: 每周一次（手动触发）
- 扫描范围: 全部上市ETF

## 新发现ETF

| 代码 | 名称 | 上市日 | 数据起点 | 主题 | 与现有池重复 | 状态 | 说明 |
|------|------|--------|----------|------|--------------|------|------|
| - | - | - | - | - | - | 待扫描 | - |

## 处理规则

1. 新 ETF 默认进入 `watch` 状态
2. 与现有正式池（ticker + name 匹配）重复的标记为 `duplicate`
3. 新行业/新主题标记为 `candidate`
4. 数据覆盖不足（上市 < 1年）标记为 `rejected`
5. 是否纳入正式池必须用户确认

## 隔离声明

> `data/research/` 下的所有数据不得被 `src/backtest.py`、`src/live_trading_assistant.py`、
> `scripts/live_generate_trade_plan.py` 等交易相关代码读取。
> 任何读取 research 数据影响交易逻辑的行为视为 P0 阻断。
