"""通用工具函数，供 app.py 和测试共同调用。"""


def cfg_signature(cfg):
    """B0-18标准配置签名：包含所有影响回测结果的关键参数。

    纯函数，不依赖任何外部状态。可直接从 app.py 或测试导入。
    """
    weights = tuple((key, round(value, 6)) for key, value in sorted(cfg["weights"].items()))

    # trailing_stop 可能为 None，仅对非 None 数值执行 round
    trailing_stop = cfg.get("trailing_stop", -0.1)
    trailing_stop_sig = round(trailing_stop, 6) if trailing_stop is not None else None

    params = (
        cfg["min_trend_score"],
        cfg["min_confirm_score"],
        cfg["min_total_score"],
        cfg["max_holdings"],
        round(cfg["max_position_per_etf"], 6),
        round(cfg["stop_loss"], 6),
        cfg.get("stop_loss_mode", "fixed"),
        cfg.get("atr_stop_multiplier", 2.0),
        cfg["market_timing"],
        cfg.get("cooling_period", 0),
        cfg.get("cooling_score_boost", 0),
        cfg.get("rebalance_freq", "weekly"),
        cfg.get("rebalance_weekday", 3),
        cfg.get("trailing_stop_mode", "none"),
        trailing_stop_sig,
        cfg.get("tier_1_pnl", 0.05),
        cfg.get("tier_1_drawdown", -0.05),
        cfg.get("tier_2_pnl", 0.15),
        cfg.get("tier_2_drawdown", -0.08),
        cfg.get("tier_3_pnl", 0.30),
        cfg.get("tier_3_drawdown", -0.12),
        cfg.get("defense_enabled", True),
        cfg.get("fallback_equity_enabled", False),
        cfg.get("sector_boost_enabled", False),
        cfg.get("momentum_factor_enabled", True),
        cfg.get("volatility_factor_enabled", True),
        round(cfg.get("initial_capital", 1_000_000), 6),
    )
    return weights + params
