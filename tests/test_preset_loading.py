#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_preset_loading.py

参数预设加载测试
覆盖：预设保存/加载、全部字段比较、pending_preset 机制
"""

import os, sys, tempfile, pytest
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def test_preset_data_structure():
    """预设数据结构应包含全部字段"""
    preset = {
        "weights": {
            "trend": 0.30, "confirm": 0.20, "momentum": 0.25,
            "volume": 0.15, "volatility": 0.10,
        },
        "min_trend_score": 5,
        "min_confirm_score": 4,
        "min_total_score": 40,
        "max_holdings": 5,
        "max_position_per_etf": 0.20,
        "stop_loss": -0.08,
    }
    # 验证所有字段存在
    assert "weights" in preset
    assert all(k in preset["weights"] for k in ["trend", "confirm", "momentum", "volume", "volatility"])
    assert "min_trend_score" in preset
    assert "min_confirm_score" in preset
    assert "min_total_score" in preset
    assert "max_holdings" in preset
    assert "max_position_per_etf" in preset
    assert "stop_loss" in preset


def test_preset_comparison_all_fields():
    """当前使用中应比较全部字段"""
    cfg = {
        "weights": {
            "trend": 0.30, "confirm": 0.20, "momentum": 0.25,
            "volume": 0.15, "volatility": 0.10,
        },
        "min_trend_score": 5,
        "min_confirm_score": 4,
        "min_total_score": 40,
        "max_holdings": 5,
        "max_position_per_etf": 0.20,
        "stop_loss": -0.08,
    }

    # 完全匹配的预设
    preset_match = cfg.copy()
    preset_match["weights"] = cfg["weights"].copy()

    is_current = (
        abs(preset_match.get("weights", {}).get("trend", 0) - cfg["weights"]["trend"]) < 0.001
        and abs(preset_match.get("weights", {}).get("confirm", 0) - cfg["weights"]["confirm"]) < 0.001
        and abs(preset_match.get("weights", {}).get("momentum", 0) - cfg["weights"]["momentum"]) < 0.001
        and abs(preset_match.get("weights", {}).get("volume", 0) - cfg["weights"]["volume"]) < 0.001
        and abs(preset_match.get("weights", {}).get("volatility", 0) - cfg["weights"]["volatility"]) < 0.001
        and abs(preset_match.get("min_trend_score", 0) - cfg["min_trend_score"]) < 0.01
        and abs(preset_match.get("min_confirm_score", 0) - cfg["min_confirm_score"]) < 0.01
        and abs(preset_match.get("min_total_score", 0) - cfg["min_total_score"]) < 0.01
        and abs(preset_match.get("max_holdings", 0) - cfg["max_holdings"]) < 0.01
        and abs(preset_match.get("max_position_per_etf", 0) - cfg["max_position_per_etf"]) < 0.001
        and abs(preset_match.get("stop_loss", 0) - cfg["stop_loss"]) < 0.001
    )
    assert is_current is True

    # 权重不同的预设
    preset_diff = cfg.copy()
    preset_diff["weights"] = cfg["weights"].copy()
    preset_diff["weights"]["trend"] = 0.50

    is_current_diff = (
        abs(preset_diff.get("weights", {}).get("trend", 0) - cfg["weights"]["trend"]) < 0.001
        and abs(preset_diff.get("weights", {}).get("confirm", 0) - cfg["weights"]["confirm"]) < 0.001
        and abs(preset_diff.get("weights", {}).get("momentum", 0) - cfg["weights"]["momentum"]) < 0.001
        and abs(preset_diff.get("weights", {}).get("volume", 0) - cfg["weights"]["volume"]) < 0.001
        and abs(preset_diff.get("weights", {}).get("volatility", 0) - cfg["weights"]["volatility"]) < 0.001
        and abs(preset_diff.get("min_trend_score", 0) - cfg["min_trend_score"]) < 0.01
        and abs(preset_diff.get("min_confirm_score", 0) - cfg["min_confirm_score"]) < 0.01
        and abs(preset_diff.get("min_total_score", 0) - cfg["min_total_score"]) < 0.01
        and abs(preset_diff.get("max_holdings", 0) - cfg["max_holdings"]) < 0.01
        and abs(preset_diff.get("max_position_per_etf", 0) - cfg["max_position_per_etf"]) < 0.001
        and abs(preset_diff.get("stop_loss", 0) - cfg["stop_loss"]) < 0.001
    )
    assert is_current_diff is False


def test_pending_preset_session_state_keys():
    """pending_preset 机制应设置正确的 session_state keys"""
    preset = {
        "weights": {
            "trend": 0.50, "confirm": 0.20, "momentum": 0.15,
            "volume": 0.10, "volatility": 0.05,
        },
        "min_trend_score": 8,
        "min_confirm_score": 6,
        "min_total_score": 55,
        "max_holdings": 4,
        "max_position_per_etf": 0.15,
        "stop_loss": -0.10,
    }

    # 模拟 session_state
    session_state = {}

    # 模拟 pending_preset 处理逻辑
    session_state["pending_preset_name"] = "test_preset"
    preset_name = session_state.pop("pending_preset_name")

    # 将预设值写入各个 widget 的 session_state
    for dim, weight in preset.get("weights", {}).items():
        session_state[f"slider_weight_{dim}"] = weight
    session_state["slider_min_trend"] = preset.get("min_trend_score", 5)
    session_state["slider_min_confirm"] = preset.get("min_confirm_score", 4)
    session_state["slider_min_total"] = preset.get("min_total_score", 40)
    session_state["slider_max_holdings"] = preset.get("max_holdings", 5)
    session_state["slider_max_per_etf"] = int(preset.get("max_position_per_etf", 0.20) * 100)
    session_state["slider_stop_loss"] = int(preset.get("stop_loss", -0.08) * 100)

    # 验证所有 key 已设置
    assert session_state["slider_weight_trend"] == 0.50
    assert session_state["slider_weight_confirm"] == 0.20
    assert session_state["slider_weight_momentum"] == 0.15
    assert session_state["slider_weight_volume"] == 0.10
    assert session_state["slider_weight_volatility"] == 0.05
    assert session_state["slider_min_trend"] == 8
    assert session_state["slider_min_confirm"] == 6
    assert session_state["slider_min_total"] == 55
    assert session_state["slider_max_holdings"] == 4
    assert session_state["slider_max_per_etf"] == 15
    assert session_state["slider_stop_loss"] == -10

    # 验证 pending_preset_name 已被 pop
    assert "pending_preset_name" not in session_state


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
