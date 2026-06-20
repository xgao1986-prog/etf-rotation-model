#!/usr/bin/env python3
"""
Phase 5.5 回归测试：momentum_factor_enabled 配置开关

验证：
1. 开关关闭时，momentum_rank 不计入 total_score
2. 开关开启时，结果保持 B0.1 兼容
3. 开关不影响 momentum_rank 本身的计算（只影响 total_score）
4. 关闭前后其他因子分数不变
"""

import pytest
import sys
import os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy import StrategyEngine
from config import STRATEGY_CONFIG, build_config


class TestMomentumFactorSwitch:
    """回归测试：momentum_factor_enabled 开关"""
    
    @pytest.fixture
    def sample_scores(self):
        """构造测试用的 scores DataFrame"""
        return pd.DataFrame({
            'ticker': ['A', 'B', 'C', 'D'],
            'trend_score': [30, 25, 20, 15],
            'confirm_score': [20, 15, 10, 5],
            'momentum_rank': [25, 20, 15, 10],
            'volume_score': [15, 10, 5, 0],
            'vol_score': [10, 5, 0, 0],
        })
    
    def test_switch_off_excludes_momentum_from_total(self, sample_scores):
        """1. 开关关闭时，momentum_rank 不计入 total_score"""
        cfg = build_config()
        cfg['momentum_factor_enabled'] = False
        engine = StrategyEngine(cfg)
        
        result = engine.compute_total_score(sample_scores)
        
        # 手动计算期望的 total_score（不含 momentum_rank）
        expected = sample_scores['trend_score'] + sample_scores['confirm_score'] + sample_scores['volume_score'] + sample_scores['vol_score']
        
        pd.testing.assert_series_equal(
            result['total_score'].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False
        )
    
    def test_switch_on_includes_momentum_for_b0_1_compat(self, sample_scores):
        """2. 开关开启时，结果保持 B0.1 兼容（含 momentum_rank）"""
        cfg = build_config()
        cfg['momentum_factor_enabled'] = True
        engine = StrategyEngine(cfg)
        
        result = engine.compute_total_score(sample_scores)
        
        # 手动计算期望的 total_score（含 momentum_rank）
        expected = sample_scores['trend_score'] + sample_scores['confirm_score'] + sample_scores['momentum_rank'] + sample_scores['volume_score'] + sample_scores['vol_score']
        
        pd.testing.assert_series_equal(
            result['total_score'].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False
        )
    
    def test_switch_does_not_affect_momentum_rank_column(self, sample_scores):
        """3. 开关不影响 momentum_rank 本身的计算，只影响 total_score"""
        cfg_off = build_config()
        cfg_off['momentum_factor_enabled'] = False
        engine_off = StrategyEngine(cfg_off)
        
        cfg_on = build_config()
        cfg_on['momentum_factor_enabled'] = True
        engine_on = StrategyEngine(cfg_on)
        
        result_off = engine_off.compute_total_score(sample_scores)
        result_on = engine_on.compute_total_score(sample_scores)
        
        # momentum_rank 列本身不变
        assert (result_off['momentum_rank'] == sample_scores['momentum_rank']).all()
        assert (result_on['momentum_rank'] == sample_scores['momentum_rank']).all()
        
        # total_score 不同
        assert not (result_off['total_score'] == result_on['total_score']).all()
    
    def test_other_factor_scores_unchanged(self, sample_scores):
        """4. 关闭前后其他因子分数不变"""
        cfg = build_config()
        cfg['momentum_factor_enabled'] = False
        engine = StrategyEngine(cfg)
        
        result = engine.compute_total_score(sample_scores)
        
        for col in ['trend_score', 'confirm_score', 'volume_score', 'vol_score']:
            pd.testing.assert_series_equal(
                result[col].reset_index(drop=True),
                sample_scores[col].reset_index(drop=True),
                check_names=False
            )
    
    def test_default_config_has_switch(self):
        """5. 默认配置包含 momentum_factor_enabled 开关"""
        cfg = build_config()
        assert 'momentum_factor_enabled' in cfg
        assert cfg['momentum_factor_enabled'] is False  # 当前默认关闭
    
    def test_exclude_factor_still_works_with_switch_off(self, sample_scores):
        """6. exclude_factor 消融测试在开关关闭时仍然有效"""
        cfg = build_config()
        cfg['momentum_factor_enabled'] = False
        engine = StrategyEngine(cfg)
        
        # 再排除 trend_score
        result = engine.compute_total_score(sample_scores, exclude_factor='trend_score')
        
        expected = sample_scores['confirm_score'] + sample_scores['volume_score'] + sample_scores['vol_score']
        
        pd.testing.assert_series_equal(
            result['total_score'].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False
        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
