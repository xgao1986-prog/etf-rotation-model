#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_strategy_presets.py — shared strategy preset reader tests."""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy_presets import (
    DEFAULT_PRESET_PATH,
    load_strategy_presets,
    save_strategy_presets,
    validate_strategy_preset,
)


VALID_PRESET = {
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
    "max_position_per_etf": 0.20,
    "stop_loss": -0.08,
}


def test_default_preset_path_points_to_presets_directory():
    assert DEFAULT_PRESET_PATH.endswith(os.path.join('presets', 'strategy_presets.json'))


def test_valid_preset_loads_unchanged():
    presets = {"B0.4": VALID_PRESET.copy()}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'strategy_presets.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
        loaded = load_strategy_presets(path)
    assert loaded == presets


def test_load_presets_returns_builtin_defaults_when_file_missing():
    loaded = load_strategy_presets('/nonexistent/path/strategy_presets.json')
    assert 'v1.0 原始参数' in loaded
    validate_strategy_preset(loaded['v1.0 原始参数'])


def test_save_and_reload_roundtrip():
    presets = {"roundtrip": VALID_PRESET.copy()}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'strategy_presets.json')
        save_strategy_presets(presets, path)
        loaded = load_strategy_presets(path)
    assert loaded == presets


def test_validate_rejects_missing_weights():
    bad = VALID_PRESET.copy()
    del bad['weights']
    with pytest.raises(ValueError, match='weights'):
        validate_strategy_preset(bad)


def test_validate_rejects_incomplete_weights():
    bad = VALID_PRESET.copy()
    bad['weights'] = {
        'trend': 0.30,
        'confirm': 0.20,
        'momentum': 0.25,
        'volume': 0.15,
    }
    with pytest.raises(ValueError, match='weights'):
        validate_strategy_preset(bad)


def test_validate_rejects_non_positive_max_holdings():
    bad = VALID_PRESET.copy()
    bad['max_holdings'] = 0
    with pytest.raises(ValueError, match='max_holdings'):
        validate_strategy_preset(bad)


def test_validate_rejects_invalid_max_position_per_etf():
    bad = VALID_PRESET.copy()
    bad['max_position_per_etf'] = 1.5
    with pytest.raises(ValueError, match='max_position_per_etf'):
        validate_strategy_preset(bad)


def test_validate_rejects_non_negative_stop_loss():
    bad = VALID_PRESET.copy()
    bad['stop_loss'] = 0.0
    with pytest.raises(ValueError, match='stop_loss'):
        validate_strategy_preset(bad)


def test_validate_rejects_zero_stop_loss():
    bad = VALID_PRESET.copy()
    bad['stop_loss'] = 0.05
    with pytest.raises(ValueError, match='stop_loss'):
        validate_strategy_preset(bad)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
