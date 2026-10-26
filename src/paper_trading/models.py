# src/paper_trading/models.py — enums, immutable inputs, validation helpers, and configuration hashing
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Tuple


class AccountType(str, Enum):
    COMPARISON = "COMPARISON"
    SHADOW = "SHADOW"


class AccountStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ENDED = "ENDED"
    ERROR = "ERROR"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class StartMode(str, Enum):
    CASH = "CASH"
    IMPORTED = "IMPORTED"


def canonical_config(config: Mapping[str, Any]) -> str:
    return json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_config(config).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 账户策略配置校验（账户设置一致性审计 F6，2026-10-22）
#
# 虚拟盘 runner 实际执行的参数（创建时冻结，逐账户生效）：
#   min_total_score / min_trend_score / min_confirm_score（入场过滤）、
#   max_holdings、max_position_per_etf、stop_loss（固定止损）、
#   defense_enabled、rebalance_weekday
# 内部冻结键（由创建流程写入，不接受用户直接传入以外的语义）：
#   industry_tickers / defense_tickers（创建时的标的池快照）
# 共享评分管线参数（允许携带，但不逐账户生效，页面有明确提示）：
#   weights（账户运行使用数据库统一评分）
# 其余设置一律视为不支持：创建时必须拒绝，不得保存后静默忽略。
# ---------------------------------------------------------------------------

ACCOUNT_EXECUTED_KEYS = frozenset({
    "min_total_score", "min_trend_score", "min_confirm_score",
    "max_holdings", "max_position_per_etf", "stop_loss",
    "defense_enabled", "rebalance_weekday",
})

ACCOUNT_FROZEN_UNIVERSE_KEYS = frozenset({"industry_tickers", "defense_tickers"})

ACCOUNT_SHARED_SCORING_KEYS = frozenset({"weights"})

# 内部键（由创建/确认流程写入，用户不直接设置）：
#   config_version：执行口径版本（2 = 2026-10-22 一致性修复后的完整入场条件口径）
#   score_source：评分来源说明（账户运行始终使用数据库统一评分）
ACCOUNT_INTERNAL_KEYS = frozenset({"config_version", "score_source"})

CURRENT_CONFIG_VERSION = 2

# 已知但虚拟盘不支持的设置 → 允许的取值（缺省/关闭）；取其他值时拒绝创建。
ACCOUNT_UNSUPPORTED_DEFAULTS = {
    "market_timing": (False, None),
    "stop_loss_mode": ("fixed", None),
    "atr_stop_multiplier": (None,),
    "trailing_stop_mode": ("none", None),
    "trailing_stop": (None,),
    "cooling_period": (0, None),
    "cooling_score_boost": (None,),
    "fallback_equity_enabled": (False, None),
    "rebalance_freq": ("weekly", None),
    "market_ma_short": (None,),
    "market_ma_long": (None,),
}

_UNSUPPORTED_LABELS = {
    "market_timing": "大盘择时",
    "stop_loss_mode": "止损模式（虚拟盘仅支持固定止损）",
    "atr_stop_multiplier": "ATR止损倍数",
    "trailing_stop_mode": "动态止盈",
    "trailing_stop": "回撤止盈阈值",
    "cooling_period": "冷静期",
    "cooling_score_boost": "冷静期评分提升",
    "fallback_equity_enabled": "宽基补仓",
    "rebalance_freq": "调仓频率（虚拟盘仅支持每周）",
    "market_ma_short": "大盘短期均线",
    "market_ma_long": "大盘长期均线",
}


def validate_account_strategy_config(config: Mapping[str, Any]) -> None:
    """校验账户创建时的策略配置。

    - 未知键 → 拒绝（不得静默忽略）；
    - 已知但虚拟盘不支持的设置取非缺省值 → 拒绝，错误信息指明设置项；
    - 支持的执行参数做基本取值校验；
    - weights 等共享评分管线参数允许携带（页面负责提示其不逐账户生效）。

    Raises ValueError with a Chinese message naming the offending setting.
    """
    if not isinstance(config, Mapping):
        raise ValueError("账户策略配置必须是字典")

    known = (ACCOUNT_EXECUTED_KEYS | ACCOUNT_FROZEN_UNIVERSE_KEYS
             | ACCOUNT_SHARED_SCORING_KEYS | ACCOUNT_INTERNAL_KEYS
             | set(ACCOUNT_UNSUPPORTED_DEFAULTS))
    for key in config:
        if key not in known:
            raise ValueError(
                f"账户配置包含不支持的设置项 '{key}'：虚拟盘无法执行该设置，"
                f"已阻止创建（不允许保存后静默忽略）。"
            )

    for key, allowed in ACCOUNT_UNSUPPORTED_DEFAULTS.items():
        if key in config and config[key] not in allowed:
            label = _UNSUPPORTED_LABELS.get(key, key)
            raise ValueError(
                f"账户配置包含虚拟盘暂不支持的设置：{label}（{key}="
                f"{config[key]!r}）。该设置只在回测路径生效，已阻止创建账户；"
                f"如需使用该设置请仅运行回测，或等后续版本支持。"
            )

    # weights 兼容性（2026-10-22 复审补修 ③）：账户运行使用数据库统一评分
    # （daily_scores 全局管线），暂不实现逐账户评分。预设 weights 与管线
    # 有效权重不一致时拒绝创建——不允许保存后仍按统一评分运行、冒充策略对比。
    if "weights" in config:
        w = config["weights"]
        if not isinstance(w, Mapping):
            raise ValueError("weights 必须是字典（trend/confirm/momentum/volume/volatility）")
        if not weights_compatible_with_pipeline(w):
            raise ValueError(
                f"预设评分权重与当前统一评分管线不兼容（weights={dict(w)}）。"
                f"虚拟盘账户运行只能使用数据库统一评分（{describe_score_source()}），"
                f"暂不实现逐账户评分；不同权重的预设保存后仍会是同一评分，"
                f"无法构成真实的策略对比，已阻止创建。"
                f"（兼容性口径：关闭的因子权重置零后归一化，须与管线有效权重一致）"
            )

    if "max_holdings" in config:
        v = config["max_holdings"]
        if not isinstance(v, int) or v <= 0:
            raise ValueError("max_holdings 必须是正整数")
    if "max_position_per_etf" in config:
        v = config["max_position_per_etf"]
        if not isinstance(v, (int, float)) or not (0 < v <= 1.0):
            raise ValueError("max_position_per_etf 必须在 (0, 1] 区间")
    if "stop_loss" in config:
        v = config["stop_loss"]
        if not isinstance(v, (int, float)) or v >= 0:
            raise ValueError("stop_loss 必须是负数（如 -0.08 表示 -8%）")
    if "min_total_score" in config:
        if not isinstance(config["min_total_score"], (int, float)):
            raise ValueError("min_total_score 必须是数字")
    if "rebalance_weekday" in config:
        v = config["rebalance_weekday"]
        if not isinstance(v, int) or not (0 <= v <= 4):
            raise ValueError("rebalance_weekday 必须是 0~4 的整数（0=周一 … 4=周五）")
    if "defense_enabled" in config:
        if not isinstance(config["defense_enabled"], bool):
            raise ValueError("defense_enabled 必须是布尔值")
    for key in ACCOUNT_FROZEN_UNIVERSE_KEYS:
        if key in config:
            v = config[key]
            if not isinstance(v, (list, tuple)) or not all(isinstance(t, str) for t in v):
                raise ValueError(f"{key} 必须是标的代码字符串列表（由创建流程写入）")


# ---------------------------------------------------------------------------
# 统一评分管线口径（账户运行评分来源，2026-10-22 复审补修 ③）
# 管线因子满分（strategy.py 硬编码）：趋势30 / 确认20 / 动量25 / 成交量15 / 波动率10；
# 动量、波动率因子由 STRATEGY_CONFIG 开关控制是否计入 total_score。
# ---------------------------------------------------------------------------

PIPELINE_FACTOR_POINTS = {
    "trend": 30.0,
    "confirm": 20.0,
    "momentum": 25.0,
    "volume": 15.0,
    "volatility": 10.0,
}

WEIGHTS_COMPAT_TOLERANCE = 0.005


def effective_pipeline_weights() -> dict:
    """当前统一评分管线的有效权重（关闭的因子计 0 后归一化）。"""
    import config as config_module

    cfg = config_module.STRATEGY_CONFIG
    points = dict(PIPELINE_FACTOR_POINTS)
    if not cfg.get("momentum_factor_enabled", True):
        points["momentum"] = 0.0
    if not cfg.get("volatility_factor_enabled", True):
        points["volatility"] = 0.0
    total = sum(points.values())
    return {k: v / total for k, v in points.items()}


def weights_compatible_with_pipeline(weights: Mapping[str, Any],
                                     tol: float = WEIGHTS_COMPAT_TOLERANCE) -> bool:
    """预设权重与统一评分管线是否兼容。

    口径：管线关闭的因子（当前为动量、波动率）在预设中先置零，再归一化，
    与管线有效权重逐项比较，容差 tol。
    """
    eff = effective_pipeline_weights()
    try:
        w = {k: float(weights.get(k, 0.0)) for k in PIPELINE_FACTOR_POINTS}
    except (TypeError, ValueError):
        return False
    if any(v < 0 for v in w.values()):
        return False
    for k, v in eff.items():
        if v == 0.0:
            w[k] = 0.0
    total = sum(w.values())
    if total <= 0:
        return False
    normalized = {k: v / total for k, v in w.items()}
    return all(abs(normalized[k] - eff[k]) <= tol for k in eff)


def describe_score_source() -> str:
    """账户运行评分来源的人读描述（写入账户配置 score_source，页面展示）。"""
    import config as config_module

    cfg = config_module.STRATEGY_CONFIG
    parts = ["趋势30", "确认20"]
    if cfg.get("momentum_factor_enabled", True):
        parts.append("动量25")
    parts.append("成交量15")
    if cfg.get("volatility_factor_enabled", True):
        parts.append("波动率10")
    return ("数据库统一评分 daily_scores（全局评分管线："
            + "+".join(parts) + "；逐账户权重不生效）")


def resolve_execution_config(account) -> dict | None:
    """解析账户的当前执行配置（2026-10-22 复审补修 ④）。

    - config_json 含 config_version >= 2（新口径创建）→ 直接使用；
    - 老账户（无 config_version）→ 仅在用户显式确认后
      （behavior_ack_config_json 已写入）使用确认快照；
    - 两者皆无 → None：不得按新行为运行，由调用方暂停并提示确认。
    """
    cfg = json.loads(account["config_json"])
    try:
        version = int(cfg.get("config_version", 1))
    except (TypeError, ValueError):
        version = 1
    if version >= CURRENT_CONFIG_VERSION:
        return cfg
    ack = None
    try:
        ack = account.get("behavior_ack_config_json")
    except AttributeError:
        keys = set(account.keys()) if hasattr(account, "keys") else set()
        if "behavior_ack_config_json" in keys:
            ack = account["behavior_ack_config_json"]
    if ack:
        return json.loads(ack)
    return None


@dataclass(frozen=True)
class OpeningPosition:
    ticker: str
    shares: int
    cost_price: float
    last_price: float

    def __post_init__(self):
        if not self.ticker:
            raise ValueError("ticker is required")
        if self.shares <= 0 or self.shares % 100 != 0:
            raise ValueError("shares must be a positive multiple of 100")
        if self.cost_price <= 0 or self.last_price <= 0:
            raise ValueError("opening prices must be positive")

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price


@dataclass(frozen=True)
class ManualFill:
    account_id: str
    order_id: str
    trade_date: str
    actual_price: float
    actual_shares: int

    def __post_init__(self):
        if not self.account_id:
            raise ValueError("account_id is required")
        if not self.order_id:
            raise ValueError("order_id is required")
        if not self.trade_date:
            raise ValueError("trade_date is required")
        if self.actual_price <= 0:
            raise ValueError("actual_price must be positive")
        if self.actual_shares <= 0 or self.actual_shares % 100 != 0:
            raise ValueError("actual_shares must be a positive multiple of 100")


@dataclass(frozen=True)
class AccountCreate:
    account_id: str
    name: str
    account_type: AccountType
    strategy_name: str
    strategy_config: Mapping[str, Any]
    initial_capital: float
    start_mode: StartMode
    start_date: str
    opening_cash: float | None = None
    opening_positions: Tuple[OpeningPosition, ...] = field(default_factory=tuple)
    group_id: str | None = None
    end_date: str | None = None

    def __post_init__(self):
        if not self.account_id or not self.name or not self.strategy_name:
            raise ValueError("account_id, name, and strategy_name are required")
        if self.initial_capital <= 0:
            raise ValueError("initial capital must be positive")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end date cannot be before start date")
        if self.start_mode is StartMode.CASH:
            if self.opening_positions:
                raise ValueError("cash start cannot contain opening positions")
            if self.opening_cash not in (None, self.initial_capital):
                raise ValueError("cash start opening cash must equal initial capital")
        if self.start_mode is StartMode.IMPORTED:
            if self.opening_cash is None or self.opening_cash < 0:
                raise ValueError("imported start requires non-negative opening cash")


def freeze_universe_into_config(strategy_config: Mapping[str, Any]) -> dict:
    """账户创建时把当前标的池冻结进配置（账户设置一致性修复 F5）。

    此后侧栏"防御资产模式"等对全局 config 模块的改动不再影响已有账户。
    若配置中已含冻结键（如测试显式传入），尊重原值不覆盖。
    """
    import config as config_module

    frozen = dict(strategy_config)
    frozen.setdefault("industry_tickers", sorted(config_module.ETF_UNIVERSE.keys()))
    frozen.setdefault("defense_tickers", sorted(config_module.DEFENSE_UNIVERSE.keys()))
    return frozen
