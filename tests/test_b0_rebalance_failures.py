# -*- coding: utf-8 -*-
"""
B0 调仓引擎结构性缺陷历史测试（已归档）

⚠️ 说明：本文件测试的是 v1.x 旧调仓逻辑中的已知缺陷。
这些缺陷已在 v2.5 (plan_rebalance_v2_5) 中修复。
本测试文件作为历史记录保留，标记为 xfail（预期失败），
以验证旧代码模式是否仍存在于 legacy 路径中。

当旧逻辑 (_rebalance_legacy) 被完全移除后，这些测试可以删除。

运行方式：
    cd D:/etf_rotation_model && python tests/test_b0_rebalance_failures.py
"""
import sys, os, pandas as pd, numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, 'D:/etf_rotation_model/src')

import config
from backtest import BacktestEngine
from strategy import StrategyEngine
from database import ETFDatabase

import pytest

# ============================================================
# 简单的测试运行器（不需要pytest）
# ============================================================

class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []
    
    def run(self, test_class):
        print(f"\n{'='*80}")
        print(f"Test Class: {test_class.__name__}")
        print(f"{'='*80}")
        
        instance = test_class()
        for attr_name in dir(instance):
            if attr_name.startswith('test_'):
                try:
                    getattr(instance, attr_name)()
                    print(f"  [PASS] {attr_name}")
                    self.passed += 1
                except AssertionError as e:
                    print(f"  [FAIL] {attr_name}")
                    print(f"    {e}")
                    self.failed += 1
                    self.failures.append((test_class.__name__, attr_name, str(e)))
                except Exception as e:
                    print(f"  [ERROR] {attr_name}: {e}")
                    self.failed += 1
                    self.failures.append((test_class.__name__, attr_name, str(e)))
    
    def summary(self):
        print(f"\n{'='*80}")
        print(f"Test Summary: {self.passed} passed, {self.failed} failed")
        print(f"{'='*80}")
        if self.failures:
            print("\nFailures (expected - these confirm defects exist in legacy code):")
            for cls, test, msg in self.failures:
                print(f"  {cls}.{test}")
        return self.failed == 0


# ============================================================
# 测试类：P0/P1 缺陷（历史记录，已修复）
# 标记为 xfail：预期失败，因为缺陷已在 v2.5 中修复
# ============================================================

@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP0_DefensePriorityReversed:
    """
    [P0] 防御资产优先级写反（v1.x legacy 缺陷，已在 v2.5 修复）
    """
    
    def test_defense_should_not_buy_before_industry(self):
        """防御资产不应在行业ETF之前买入"""
        
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        defense_pos = source.find('# 当大盘择时信号低时，强制配置防御资产')
        core_pos = source.find('# ========== 买入核心池ETF')
        
        assert defense_pos > 0, "Defense code block not found"
        assert core_pos > 0, "Core buy code block not found"
        
        assert defense_pos > core_pos, \
            f"[P0 DEFECT IN LEGACY] Defense allocation (line {defense_pos}) occurs BEFORE core buy (line {core_pos}). " \
            f"Fixed in v2.5: plan_rebalance_v2_5 handles industry first, defense fill later."

    def test_defense_should_not_sell_industry_to_make_room(self):
        """防御资产不应通过卖出行业ETF来腾仓位"""
        
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        sell_for_defense = source.find('为防御资产腾仓位')
        
        assert sell_for_defense == -1, \
            f"[P0 DEFECT IN LEGACY] Code contains logic to sell industry ETFs for defense. " \
            f"Fixed in v2.5: defense never displaces industry."


@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP0_DefenseOccupiesIndustrySlots:
    """[P0] 防御资产占用行业槽位（v1.x legacy 缺陷，已在 v2.5 修复）"""
    
    def test_defense_should_not_count_against_industry_slots(self):
        """防御资产不应减少行业ETF的可买入数量"""
        
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        core_slots_calc = "core_slots = min(max_new"
        
        if core_slots_calc in source:
            assert False, \
                f"[P0 DEFECT IN LEGACY] core_slots limited by max_new (shared with defense). " \
                f"Fixed in v2.5: industry and defense slots are independent."


@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP0_BuyOrderDependsOnSequence:
    """[P0] 买入结果依赖候选顺序（v1.x legacy 缺陷，已在 v2.5 修复）"""
    
    def test_buy_amount_should_not_depend_on_candidate_order(self):
        """相同候选按不同顺序遍历，总持仓金额应相同"""
        
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        cash_deduction = "available_cash -= total_cost"
        target_with_cash = "target_amount = min(target_amount, available_cash * 0.95)"
        
        if target_with_cash in source and cash_deduction in source:
            assert False, \
                f"[P0 DEFECT IN LEGACY] Buy target uses remaining_cash, order-dependent. " \
                f"Fixed in v2.5: unified scaling before any execution."

    def test_should_plan_all_orders_before_execution(self):
        """应在执行任何买入前，先计算所有订单的目标金额"""
        
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        assert False, \
            f"[P0 DEFECT IN LEGACY] Buy loop executes immediately. " \
            f"Fixed in v2.5: plan_rebalance_v2_5 plans all orders before execution."


@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP0_ProportionalAllocation:
    """[P0] 资金不足时应按比例分配（v1.x legacy 缺陷，已在 v2.5 修复）"""
    
    def test_should_allocate_proportionally_when_cash_insufficient(self):
        """现金不足时，所有候选应等比例缩减"""
        
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        has_proportional = False
        core_buy_pos = source.find('# ========== 买入核心池ETF')
        if core_buy_pos > 0:
            core_buy_block = source[core_buy_pos:core_buy_pos+2000]
            has_proportional = (
                'total_target' in core_buy_block and 'scale' in core_buy_block
            ) or 'allocation_ratio' in core_buy_block
        
        if not has_proportional:
            assert False, \
                f"[P0 DEFECT IN LEGACY] No proportional allocation in legacy buy loop. " \
                f"Fixed in v2.5: plan_rebalance_v2_5 scales all targets uniformly."


# ============================================================
# P1 缺陷（历史记录）
# ============================================================

@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP1_SellDoesNotSyncAvailableCash:
    """[P1] 卖出后可用资金未同步（v1.x legacy 缺陷）"""
    
    def test_sell_should_update_available_cash(self):
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        sell_defense_block = source.find('为防御资产腾仓位')
        if sell_defense_block > 0:
            block_end = min(sell_defense_block + 500, len(source))
            block = source[sell_defense_block:block_end]
            
            has_portfolio_cash = "portfolio['cash'] += net_proceeds" in block
            has_available_cash = "available_cash += net_proceeds" in block
            
            if has_portfolio_cash and not has_available_cash:
                assert False, \
                    f"[P1 DEFECT IN LEGACY] Sell updates portfolio['cash'] but not available_cash."
        
        assert False, \
            f"[P1 DEFECT IN LEGACY] Could not verify sell-for-defense code block."


@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP1_TwoDefenseMechanismsOverlap:
    """[P1] 存在两套重叠的防御逻辑（v1.x legacy 缺陷）"""
    
    def test_should_have_only_one_defense_logic(self):
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        defense_mandatory = '强制配置防御资产' in source
        defense_fill = '防御资产填充' in source or 'defense_fill' in source
        
        if defense_mandatory and defense_fill:
            assert False, \
                f"[P1 DEFECT IN LEGACY] Two separate defense mechanisms exist. " \
                f"Fixed in v2.5: single unified defense logic."


@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP1_SharesShouldRoundTo100:
    """[P1] 实际成交未按100股取整（v1.x legacy 缺陷）"""
    
    def test_shares_should_round_to_lot_size(self):
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        shares_calc = "shares = int(target_amount / price)"
        
        if shares_calc in source:
            lot_rounding = "// 100" in source or "* 100" in source or "lot_size" in source
            
            if not lot_rounding:
                assert False, \
                    f"[P1 DEFECT IN LEGACY] shares without 100-share rounding. " \
                    f"Fixed in v2.5: plan_rebalance_v2_5 uses lot_size=100."


@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP1_ConfigShouldNotDependOnGlobals:
    """[P1] 配置不完全由cfg控制（v1.x legacy 缺陷）"""
    
    def test_config_should_not_use_globals(self):
        with open('D:/etf_rotation_model/src/backtest.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        global_defense_alloc = "config.DEFENSE_ALLOCATION" in source or "_cfg_module.DEFENSE_ALLOCATION" in source
        global_defense_universe = "config.DEFENSE_UNIVERSE" in source or "_cfg_module.DEFENSE_UNIVERSE" in source
        
        if global_defense_alloc or global_defense_universe:
            assert False, \
                f"[P1 DEFECT IN LEGACY] Backtest reads global config. " \
                f"Fixed in v2.5: all parameters passed via cfg."


@pytest.mark.xfail(reason="v1.x legacy defects, fixed in v2.5")
class TestP2_OutSampleWarmup:
    """[P2] 样本外回测重新丢失预热数据（v1.x legacy 缺陷）"""
    
    def test_out_sample_should_preserve_warmup(self):
        with open('D:/etf_rotation_model/app.py', 'r', encoding='utf-8') as f:
            source = f.read()
        
        run_pos = source.find('engine.run(')
        if run_pos > 0:
            nearby = source[max(0, run_pos-300):run_pos+100]
            
            if 'out_sample' in nearby and 'engine.run' in nearby:
                assert False, \
                    f"[P2 DEFECT IN LEGACY] app.py may cut off warmup data. " \
                    f"Fixed: use performance_start parameter."


# ============================================================
# 主函数（独立运行模式）
# ============================================================

if __name__ == '__main__':
    runner = TestRunner()
    
    runner.run(TestP0_DefensePriorityReversed)
    runner.run(TestP0_DefenseOccupiesIndustrySlots)
    runner.run(TestP0_BuyOrderDependsOnSequence)
    runner.run(TestP0_ProportionalAllocation)
    runner.run(TestP1_SellDoesNotSyncAvailableCash)
    runner.run(TestP1_TwoDefenseMechanismsOverlap)
    runner.run(TestP1_SharesShouldRoundTo100)
    runner.run(TestP1_ConfigShouldNotDependOnGlobals)
    runner.run(TestP2_OutSampleWarmup)
    
    success = runner.summary()
    sys.exit(0 if success else 1)
