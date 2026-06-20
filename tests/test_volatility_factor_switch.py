#!/usr/bin/env python3
"""
Phase 5.7 回归测试：volatility_factor_enabled 配置开关

验证：
1. 开关关闭时，vol_score 不计入 total_score
2. 开关开启时，结果保持旧逻辑兼容（B0.2之前，vol_score≈0）
3. 开关不影响 vol_score 本身的计算（只影响 total_score）
4. 关闭前后其他因子分数不变
5. 默认配置包含开关
6. 两个开关（momentum+volatility）独立工作
"""

import pytest
import sys
import os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy import StrategyEngine
from config import build_config


class TestVolatilityFactorSwitch:
    """回归测试：volatility_factor_enabled 开关"""
    
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
    
    def test_switch_off_excludes_vol_from_total(self, sample_scores):
        """1. 开关关闭时，vol_score 不计入 total_score"""
        cfg = build_config()
        cfg['volatility_factor_enabled'] = False
        cfg['momentum_factor_enabled'] = False  # B0.3 两者都关闭
        engine = StrategyEngine(cfg)
        
        result = engine.compute_total_score(sample_scores)
        
        # 手动计算期望的 total_score（不含 momentum_rank 和 vol_score）
        expected = sample_scores['trend_score'] + sample_scores['confirm_score'] + sample_scores['volume_score']
        
        pd.testing.assert_series_equal(
            result['total_score'].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False
        )
    
    def test_switch_on_includes_vol_for_compat(self, sample_scores):
        """2. 开关开启时，vol_score 计入 total_score（兼容旧逻辑）"""
        cfg = build_config()
        cfg['volatility_factor_enabled'] = True
        cfg['momentum_factor_enabled'] = True
        engine = StrategyEngine(cfg)
        
        result = engine.compute_total_score(sample_scores)
        
        # 手动计算期望的 total_score（含全部5个因子）
        expected = (sample_scores['trend_score'] + sample_scores['confirm_score'] + 
                    sample_scores['momentum_rank'] + sample_scores['volume_score'] + 
                    sample_scores['vol_score'])
        
        pd.testing.assert_series_equal(
            result['total_score'].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False
        )
    
    def test_switch_does_not_affect_vol_score_column(self, sample_scores):
        """3. 开关不影响 vol_score 本身的计算，只影响 total_score"""
        cfg_off = build_config()
        cfg_off['volatility_factor_enabled'] = False
        engine_off = StrategyEngine(cfg_off)
        
        cfg_on = build_config()
        cfg_on['volatility_factor_enabled'] = True
        engine_on = StrategyEngine(cfg_on)
        
        result_off = engine_off.compute_total_score(sample_scores)
        result_on = engine_on.compute_total_score(sample_scores)
        
        # vol_score 列本身不变
        assert (result_off['vol_score'] == sample_scores['vol_score']).all()
        assert (result_on['vol_score'] == sample_scores['vol_score']).all()
        
        # total_score 不同（当 vol_score 非零时）
        assert not (result_off['total_score'] == result_on['total_score']).all()
    
    def test_other_factor_scores_unchanged(self, sample_scores):
        """4. 关闭前后其他因子分数不变"""
        cfg = build_config()
        cfg['volatility_factor_enabled'] = False
        engine = StrategyEngine(cfg)
        
        result = engine.compute_total_score(sample_scores)
        
        for col in ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score']:
            pd.testing.assert_series_equal(
                result[col].reset_index(drop=True),
                sample_scores[col].reset_index(drop=True),
                check_names=False
            )
    
    def test_default_config_has_vol_switch(self):
        """5. 默认配置包含 volatility_factor_enabled 开关"""
        cfg = build_config()
        assert 'volatility_factor_enabled' in cfg
        assert cfg['volatility_factor_enabled'] is False  # 当前默认关闭
    
    def test_both_switches_independent(self, sample_scores):
        """6. 两个开关独立工作：momentum关闭+volatility关闭 = B0.3"""
        cfg = build_config()
        cfg['momentum_factor_enabled'] = False
        cfg['volatility_factor_enabled'] = False
        engine = StrategyEngine(cfg)
        
        result = engine.compute_total_score(sample_scores)
        
        # 只有 trend + confirm + volume
        expected = sample_scores['trend_score'] + sample_scores['confirm_score'] + sample_scores['volume_score']
        
        pd.testing.assert_series_equal(
            result['total_score'].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False
        )
    
    def test_both_switches_on_full_compat(self, sample_scores):
        """7. 两个开关都开启 = 完整5因子"""
        cfg = build_config()
        cfg['momentum_factor_enabled'] = True
        cfg['volatility_factor_enabled'] = True
        engine = StrategyEngine(cfg)
        
        result = engine.compute_total_score(sample_scores)
        
        expected = (sample_scores['trend_score'] + sample_scores['confirm_score'] + 
                    sample_scores['momentum_rank'] + sample_scores['volume_score'] + 
                    sample_scores['vol_score'])
        
        pd.testing.assert_series_equal(
            result['total_score'].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False
        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
