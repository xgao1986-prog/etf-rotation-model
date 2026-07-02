# src/strategy_presets.py — shared saved-preset loading and validation.
from __future__ import annotations

import json
import os
from typing import Any, Dict


DEFAULT_PRESET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "presets",
    "strategy_presets.json",
)


BUILTIN_DEFAULTS: Dict[str, Any] = {
    "v1.0 原始参数": {
        "weights": {
            "trend": 0.30,
            "confirm": 0.20,
            "momentum": 0.25,
            "volume": 0.15,
            "volatility": 0.10,
        },
        "min_trend_score": 15,
        "min_confirm_score": 4,
        "min_total_score": 40,
        "max_holdings": 5,
        "max_position_per_etf": 0.15,
        "stop_loss": -0.08,
    },
    "保守型": {
        "weights": {
            "trend": 0.40,
            "confirm": 0.30,
            "momentum": 0.15,
            "volume": 0.10,
            "volatility": 0.05,
        },
        "min_trend_score": 20,
        "min_confirm_score": 8,
        "min_total_score": 50,
        "max_holdings": 3,
        "max_position_per_etf": 0.10,
        "stop_loss": -0.05,
    },
    "激进型": {
        "weights": {
            "trend": 0.20,
            "confirm": 0.10,
            "momentum": 0.40,
            "volume": 0.20,
            "volatility": 0.10,
        },
        "min_trend_score": 10,
        "min_confirm_score": 2,
        "min_total_score": 35,
        "max_holdings": 7,
        "max_position_per_etf": 0.20,
        "stop_loss": -0.12,
    },
}


def load_strategy_presets(path: str = DEFAULT_PRESET_PATH) -> Dict[str, Any]:
    """Load saved strategy presets from JSON.

    Returns built-in defaults when the file does not exist.
    """
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return BUILTIN_DEFAULTS.copy()


def save_strategy_presets(presets: Dict[str, Any], path: str = DEFAULT_PRESET_PATH) -> None:
    """Save strategy presets to JSON after validating each one."""
    for name, preset in presets.items():
        validate_strategy_preset(preset)
    preset_dir = os.path.dirname(path)
    os.makedirs(preset_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


def validate_strategy_preset(preset: Dict[str, Any]) -> None:
    """Validate a single strategy preset dictionary."""
    if not isinstance(preset, dict):
        raise ValueError("preset must be a dictionary")

    weights = preset.get("weights")
    if not isinstance(weights, dict):
        raise ValueError("preset must contain a dict of weights")
    required_weights = {"trend", "confirm", "momentum", "volume", "volatility"}
    if set(weights.keys()) != required_weights:
        raise ValueError(f"weights must contain exactly {required_weights}")
    for key, value in weights.items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"weight {key} must be a non-negative number")
    total = sum(weights.values())
    if not (0.99 <= total <= 1.01):
        raise ValueError(f"weights must sum to 1.0, got {total}")

    max_holdings = preset.get("max_holdings")
    if not isinstance(max_holdings, int) or max_holdings <= 0:
        raise ValueError("max_holdings must be a positive integer")

    max_position = preset.get("max_position_per_etf")
    if not isinstance(max_position, (int, float)) or not (0 < max_position <= 1.0):
        raise ValueError("max_position_per_etf must be between 0 and 1")

    stop_loss = preset.get("stop_loss")
    if not isinstance(stop_loss, (int, float)) or stop_loss >= 0:
        raise ValueError("stop_loss must be a negative number")

    for key in ("min_trend_score", "min_confirm_score", "min_total_score"):
        value = preset.get(key)
        if not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
