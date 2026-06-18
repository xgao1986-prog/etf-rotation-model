# B0 基线冻结报告

**冻结时间**: 2026-06-18 13:29:27
**Git Commit SHA**: `2bb40c95acebff5f6363466f2337b5c297f2e6e2`
**配置哈希 (SHA256)**: `d803577b8fca88855da0e0abe53df88b803c911f8bf522fd946674c79ad71982`
**数据截止**: 2026-06-05

## 代码文件哈希

| 文件 | SHA256 |
|------|--------|
| config.py | `e4ea007508e1552df2f6c70c590b58aeb90b83b3997a7e467aede286da1bdd46` |
| backtest.py | `bc96bf3c0c11530c4f02427f088162a784d347af014b9413898c496767832276` |
| strategy.py | `3b0f176d253778e6b66d6063586ea08cc75ca6d4a444a4d8e445926cfecdb634` |
| market_regime.py | `eb74823a8898c3ef06eb61af151f0bd1bc94f535353001dbd41f0caffecd0f83` |

## 核心配置摘要

### 交易规则（TRADING_RULES_CONFIG）
- 调仓频率: weekly
- 调仓日: 星期3
- 冷静期: 0 天
- 动态止盈: none
- 止损线: -0.08
- 止损模式: fixed

### 防御模块（DEFENSE_CONFIG）
- 启用: True
- 模式: mandatory
- 牛市防御上限: 0.3
- 熊市防御上限: 0.5

### 宽基模块（FALLBACK_EQUITY_CONFIG）
- 启用: False
- 宽基最低总评分: 25
- 宽基趋势最低分: 10

### 市场状态配置（MARKET_REGIME_CONFIG）
- 启用: True
- 模式: observer
- 确认天数: 5
- 强牛阈值: 1.02
- 震荡下限: 0.98

### 策略参数（STRATEGY_CONFIG）
- 评分权重: 趋势30% + 确认20% + 动量25% + 成交量15% + 波动率10%
- 均线: MA20/MA50
- 最低总评分: 40
- 最大持仓: 5 只
- 单只上限: 20%
- 佣金率: 0.0300%
- 最低佣金: 5.0

### 回测参数（BACKTEST_CONFIG）
- 初始资金: 1,000,000
- 开始日期: 2019-06-03
- 样本内截止: 2023-12-31
- 样本外开始: 2024-01-01

### 回测引擎硬编码参数
- 硬去重阈值: 0.97
- 软惩罚最小: 0.85
- 软惩罚最大: 0.97
- 软惩罚最大降幅: 0.15
- 相关性窗口: 60

### ETF池
- 行业ETF: 16 只
- 概念ETF: 16 只
- 防御资产: 2 只
- 宽基补仓: 4 只
- 核心池: 32 只
- 基准: 000300.SH

### 相关性阈值
- CORRELATION_THRESHOLD: 0.9

### 防御资产配置
- 配置模式: linear
- 配置映射: {0.0: 0.8, 0.2: 0.5, 0.5: 0.2, 1.0: 0.0}

## 完整冻结配置（JSON）

```json
{"BACKTEST_CONFIG": {"end_date": null, "in_sample_end": "2023-12-31", "initial_capital": 1000000, "out_sample_start": "2024-01-01", "start_date": "2019-06-03"}, "BACKTEST_ENGINE_HARDCODED": {"HARD_REDUNDANCY_THRESHOLD": 0.97, "SOFT_PENALTY_MAX": 0.97, "SOFT_PENALTY_MAX_REDUCTION": 0.15, "SOFT_PENALTY_MIN": 0.85, "corr_window": 60, "min_valid_pairs_ratio": 0.67, "price_mode": "close", "slippage_bps": 5, "slippage_enabled": false}, "BENCHMARK": "000300.SH", "COMMON_CUTOFF": "2026-06-05", "CONCEPT_UNIVERSE": {"159566.SZ": "储能电池ETF", "159740.SZ": "碳中和ETF", "159766.SZ": "旅游ETF", "159869.SZ": "游戏ETF", "159898.SZ": "医疗器械ETF", "159992.SZ": "创新药ETF", "510880.SH": "红利ETF", "512690.SH": "白酒ETF", "513160.SH": "港股科技30ETF", "515050.SH": "5GETF", "515170.SH": "食品饮料ETF", "515790.SH": "光伏ETF", "516510.SH": "云计算ETF", "560700.SH": "央企改革ETF", "562500.SH": "机器人ETF", "588200.SH": "科创芯片ETF"}, "CORE_UNIVERSE": {"159530.SZ": "机器人ETF", "159566.SZ": "储能电池ETF", "159697.SZ": "油气ETF", "159740.SZ": "碳中和ETF", "159766.SZ": "旅游ETF", "159865.SZ": "养殖ETF", "159869.SZ": "游戏ETF", "159898.SZ": "医疗器械ETF", "159928.SZ": "消费ETF", "159992.SZ": "创新药ETF", "159996.SZ": "家电ETF", "510880.SH": "红利ETF", "512000.SH": "券商ETF", "512010.SH": "医药ETF", "512400.SH": "有色金属ETF", "512480.SH": "半导体ETF", "512660.SH": "军工ETF", "512690.SH": "白酒ETF", "512800.SH": "银行ETF", "512980.SH": "传媒ETF", "513160.SH": "港股科技30ETF", "515050.SH": "5GETF", "515170.SH": "食品饮料ETF", "515230.SH": "软件ETF", "515790.SH": "光伏ETF", "515880.SH": "通信ETF", "516110.SH": "汽车ETF", "516160.SH": "新能源ETF", "516510.SH": "云计算ETF", "560700.SH": "央企改革ETF", "562500.SH": "机器人ETF", "588200.SH": "科创芯片ETF"}, "CORRELATION_THRESHOLD": 0.9, "DEFENSE_ALLOCATION": {"0.0": 0.8, "0.2": 0.5, "0.5": 0.2, "1.0": 0.0}, "DEFENSE_ALLOCATION_MODE": "linear", "DEFENSE_CONFIG": {"defense_enabled": true, "defense_fill_max_ratio_bear": 0.5, "defense_fill_max_ratio_bull": 0.3, "defense_mode": "mandatory", "min_defense_total_score": 25, "min_defense_trend_score": 10}, "DEFENSE_UNIVERSE": {"511010.SH": "国债ETF", "518880.SH": "黄金ETF"}, "ETF_GROUP_MAP": {"159530.SZ": "robot", "159566.SZ": "new_energy", "159697.SZ": "energy", "159740.SZ": "carbon", "159766.SZ": "tourism", "159865.SZ": "livestock", "159869.SZ": "tech_media", "159898.SZ": "medicine", "159915.SZ": "index", "159928.SZ": "consumption", "159992.SZ": "medicine", "159996.SZ": "appliance", "510300.SH": "index", "510500.SH": "index", "510880.SH": "state_owned", "511010.SH": "defense", "512000.SH": "finance", "512010.SH": "medicine", "512400.SH": "metal", "512480.SH": "chip", "512660.SH": "military", "512690.SH": "food_drink", "512800.SH": "finance", "512980.SH": "tech_media", "513160.SH": "hk_tech", "515050.SH": "telecom", "515170.SH": "food_drink", "515230.SH": "software", "515790.SH": "new_energy", "515880.SH": "telecom", "516110.SH": "auto", "516160.SH": "new_energy", "516510.SH": "tech_media", "518880.SH": "defense", "560700.SH": "state_owned", "562500.SH": "robot", "588000.SH": "chip", "588200.SH": "chip"}, "ETF_UNIVERSE": {"159530.SZ": "机器人ETF", "159697.SZ": "油气ETF", "159865.SZ": "养殖ETF", "159928.SZ": "消费ETF", "159996.SZ": "家电ETF", "512000.SH": "券商ETF", "512010.SH": "医药ETF", "512400.SH": "有色金属ETF", "512480.SH": "半导体ETF", "512660.SH": "军工ETF", "512800.SH": "银行ETF", "512980.SH": "传媒ETF", "515230.SH": "软件ETF", "515880.SH": "通信ETF", "516110.SH": "汽车ETF", "516160.SH": "新能源ETF"}, "EXECUTION_CONFIG": {"price_mode": "close", "slippage_bps": 5, "slippage_enabled": false}, "FALLBACK_EQUITY_CONFIG": {"fallback_equity_enabled": false, "fallback_ma_check": "ma20_or_ma50", "fallback_ma_slope_check": "not_steep_down", "min_fallback_confirm_score": 2, "min_fallback_total_score": 25, "min_fallback_trend_score": 10}, "FALLBACK_EQUITY_UNIVERSE": {"159915.SZ": "创业板ETF", "510300.SH": "沪深300ETF", "510500.SH": "中证500ETF", "588000.SH": "科创50ETF"}, "MARKET_REGIME_CONFIG": {"confirmation_days": 5, "enabled": true, "ma_long": 50, "ma_short": 20, "mode": "observer", "slope_accel_threshold": 1.0, "states": {"1": {"cooling_period_days": 3, "defense_fill_max_ratio_bull": 0.0, "fallback_equity_enabled": true, "fallback_equity_min_score": 35, "max_position_per_etf": 0.2, "min_total_score": 35, "stop_loss": -0.1, "trailing_stop_mode": "standard"}, "2": {"cooling_period_days": 5, "defense_fill_max_ratio_bull": 0.15, "fallback_equity_enabled": true, "fallback_equity_min_score": 35, "max_position_per_etf": 0.18, "min_total_score": 35, "stop_loss": -0.08, "trailing_stop_mode": "tiered"}, "3": {"cooling_period_days": 5, "defense_fill_max_ratio_bull": 0.3, "fallback_equity_enabled": false, "fallback_equity_min_score": 25, "max_position_per_etf": 0.15, "min_total_score": 40, "stop_loss": -0.08, "trailing_stop_mode": "tiered"}, "4": {"cooling_period_days": 7, "defense_fill_max_ratio_bull": 0.5, "fallback_equity_enabled": false, "fallback_equity_min_score": 25, "max_position_per_etf": 0.12, "min_total_score": 45, "stop_loss": -0.12, "trailing_stop_mode": "standard"}}, "trend_position_threshold_strong": 1.02, "trend_position_threshold_weak": 0.98, "vol_high_threshold": 0.025, "vol_low_threshold": 0.015}, "MARKET_REGIME_HARDCODED": {"default_confidence": 0.5, "default_state": 3, "state_names": {"1": "强牛", "2": "弱牛", "3": "震荡", "4": "熊市"}}, "STABILITY_CONFIG": {"buy_rank_n": 5, "enabled": false, "exit_confirm_weeks": 2, "hold_rank_n": 12, "min_hold_days": 20, "replacement_score_gap": 8, "same_group_max_holdings": 1}, "STRATEGY_CONFIG": {"atr_period": 14, "atr_stop_multiplier": 2.0, "commission_rate": 0.0003, "defense_max_holdings": 2, "fallback_equity_max_holdings": 3, "ma_long": 50, "ma_short": 20, "market_ma_long": 50, "market_ma_short": 20, "market_timing": false, "max_holdings": 5, "max_position_per_etf": 0.2, "min_commission": 5.0, "min_confirm_score": 4, "min_total_score": 40, "min_trend_score": 15, "rebalance_weekday": 3, "stock_max_holdings": 5, "stop_loss": -0.08, "stop_loss_mode": "fixed", "total_max_holdings": 5, "weights": {"confirm": 0.2, "momentum": 0.25, "trend": 0.3, "volatility": 0.1, "volume": 0.15}}, "STRATEGY_ENGINE_HARDCODED": {"confirm_max_days": 5, "confirm_score_per_day": 4, "fallback_ma_check": "ma20_or_ma50", "fallback_ma_slope_check": "not_steep_down", "trend_score_weights": {"above_ma20": 15, "above_ma50": 10, "ma20_slope_positive": 5}, "vol_score_weights": {"high_vol": 5, "moderate_vol": 10}, "volume_score_weights": {"normal": 5, "volume_ratio_high": 10, "volume_up_and_price_up": 15}}, "TRADING_RULES_CONFIG": {"cooling_period": 0, "cooling_score_boost": 10, "rebalance_freq": "weekly", "rebalance_ordinal": 1, "rebalance_weekday": 3, "tier_1_drawdown": -0.05, "tier_1_pnl": 0.05, "tier_2_drawdown": -0.08, "tier_2_pnl": 0.15, "tier_3_drawdown": -0.12, "tier_3_pnl": 0.3, "trailing_stop": null, "trailing_stop_mode": "none"}, "VOLATILITY_ENHANCEMENT": {"adjustment_high": 0.1, "adjustment_low": -0.05, "enabled": false, "lookback": 20, "max_allocation": 0.8, "min_allocation": 0.0, "threshold_high": 0.3, "threshold_low": 0.15}, "code_file_hashes": {"backtest.py": "bc96bf3c0c11530c4f02427f088162a784d347af014b9413898c496767832276", "config.py": "e4ea007508e1552df2f6c70c590b58aeb90b83b3997a7e467aede286da1bdd46", "market_regime.py": "eb74823a8898c3ef06eb61af151f0bd1bc94f535353001dbd41f0caffecd0f83", "strategy.py": "3b0f176d253778e6b66d6063586ea08cc75ca6d4a444a4d8e445926cfecdb634"}, "freeze_time": "2026-06-18 13:29:27", "git_sha": "2bb40c95acebff5f6363466f2337b5c297f2e6e2", "version": "B0"}
```

## 版本边界
- B0 已冻结，配置哈希: d803577b8fca88855da0e0abe53df88b803c911f8bf522fd946674c79ad71982
- 后续任何参数变更需重新生成哈希并记录差异
- 当前冻结用于 B1 单变量测试的基准对比