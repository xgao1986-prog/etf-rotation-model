"""最小测试：B0-18 配置签名正确性验证（直接调用生产函数）。"""
import sys
import os

# 将项目根目录和 src 加入路径
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "src"))
sys.path.insert(0, root)

import unittest

from config import build_config
from utils import cfg_signature


class TestB0Signature(unittest.TestCase):
    """验证 B0-18 标准配置签名（B0.4 基线）正确性。"""

    def test_default_is_b0_18(self):
        """默认完整配置（两因子关闭）→ 签名与标准签名一致。"""
        b0_18_cfg = build_config()
        sig = cfg_signature(b0_18_cfg)
        b0_18_sig = cfg_signature(build_config())

        self.assertEqual(sig, b0_18_sig)
        self.assertIsInstance(sig, tuple)
        self.assertTrue(len(sig) > 0)

        # 验证关键因子开关状态
        self.assertFalse(b0_18_cfg["momentum_factor_enabled"])
        self.assertFalse(b0_18_cfg["volatility_factor_enabled"])
        self.assertEqual(b0_18_cfg["cooling_period"], 0)
        self.assertEqual(b0_18_cfg["rebalance_freq"], "weekly")
        self.assertEqual(b0_18_cfg["rebalance_weekday"], 3)
        self.assertEqual(b0_18_cfg["trailing_stop_mode"], "none")
        self.assertTrue(b0_18_cfg["defense_enabled"])
        self.assertFalse(b0_18_cfg["sector_boost_enabled"])
        self.assertFalse(b0_18_cfg["market_timing"])

    def test_enable_momentum_deviates(self):
        """开启动量因子 → 签名偏离 B0-18。"""
        cfg = build_config()
        cfg["momentum_factor_enabled"] = True
        sig = cfg_signature(cfg)
        b0_18_sig = cfg_signature(build_config())
        self.assertNotEqual(sig, b0_18_sig)

    def test_enable_volatility_deviates(self):
        """开启波动率因子 → 签名偏离 B0-18。"""
        cfg = build_config()
        cfg["volatility_factor_enabled"] = True
        sig = cfg_signature(cfg)
        b0_18_sig = cfg_signature(build_config())
        self.assertNotEqual(sig, b0_18_sig)

    def test_change_stop_loss_deviates(self):
        """修改止损线（其他关键参数）→ 签名偏离 B0-18。"""
        cfg = build_config()
        cfg["stop_loss"] = -0.10
        sig = cfg_signature(cfg)
        b0_18_sig = cfg_signature(build_config())
        self.assertNotEqual(sig, b0_18_sig)

    def test_change_weights_deviates(self):
        """修改评分权重 → 签名偏离 B0-18。"""
        cfg = build_config()
        cfg["weights"] = {
            "trend": 0.40,
            "confirm": 0.30,
            "momentum": 0.15,
            "volume": 0.10,
            "volatility": 0.05,
        }
        sig = cfg_signature(cfg)
        b0_18_sig = cfg_signature(build_config())
        self.assertNotEqual(sig, b0_18_sig)

    def test_change_max_holdings_deviates(self):
        """修改最大持仓数 → 签名偏离 B0-18。"""
        cfg = build_config()
        cfg["max_holdings"] = 3
        sig = cfg_signature(cfg)
        b0_18_sig = cfg_signature(build_config())
        self.assertNotEqual(sig, b0_18_sig)

    def test_disable_defense_deviates(self):
        """关闭防御模块 → 签名偏离 B0-18。"""
        cfg = build_config()
        cfg["defense_enabled"] = False
        sig = cfg_signature(cfg)
        b0_18_sig = cfg_signature(build_config())
        self.assertNotEqual(sig, b0_18_sig)

    def test_enable_fallback_equity_deviates(self):
        """开启 fallback equity → 签名偏离 B0-18。"""
        cfg = build_config()
        cfg["fallback_equity_enabled"] = True
        sig = cfg_signature(cfg)
        b0_18_sig = cfg_signature(build_config())
        self.assertNotEqual(sig, b0_18_sig)

    def test_change_initial_capital_deviates(self):
        """修改初始资金 → 签名偏离 B0-18。"""
        cfg = build_config()
        cfg["initial_capital"] = 500_000
        sig = cfg_signature(cfg)
        b0_18_sig = cfg_signature(build_config())
        self.assertNotEqual(sig, b0_18_sig)

    # ========== 回归测试：trailing_stop=None 修复 ==========

    def test_default_signature_does_not_raise(self):
        """build_config() 默认配置调用签名不报错（trailing_stop=None 修复）。"""
        cfg = build_config()
        # trailing_stop 默认为 None，不应触发 TypeError
        sig = cfg_signature(cfg)
        self.assertIsInstance(sig, tuple)
        self.assertTrue(len(sig) > 0)

    def test_trailing_stop_none_stable_signature(self):
        """trailing_stop=None 能生成稳定签名，且与显式 None 一致。"""
        cfg = build_config()
        sig1 = cfg_signature(cfg)

        cfg2 = build_config()
        cfg2["trailing_stop"] = None
        sig2 = cfg_signature(cfg2)

        self.assertEqual(sig1, sig2)
        # None 在签名中应体现为 None（不是被 round 成 -0.1）
        self.assertIn(None, sig1)

    def test_trailing_stop_simple_deviates(self):
        """simple 模式下 trailing_stop 数值变化会改变签名。"""
        cfg = build_config()
        cfg["trailing_stop_mode"] = "simple"
        cfg["trailing_stop"] = -0.05
        sig1 = cfg_signature(cfg)

        cfg2 = build_config()
        cfg2["trailing_stop_mode"] = "simple"
        cfg2["trailing_stop"] = -0.10
        sig2 = cfg_signature(cfg2)

        self.assertNotEqual(sig1, sig2)

    def test_trailing_stop_none_vs_numeric_deviates(self):
        """trailing_stop=None 与 trailing_stop=-0.1 签名不同。"""
        cfg_none = build_config()
        cfg_none["trailing_stop"] = None
        sig_none = cfg_signature(cfg_none)

        cfg_num = build_config()
        cfg_num["trailing_stop"] = -0.1
        sig_num = cfg_signature(cfg_num)

        self.assertNotEqual(sig_none, sig_num)


if __name__ == "__main__":
    unittest.main()
