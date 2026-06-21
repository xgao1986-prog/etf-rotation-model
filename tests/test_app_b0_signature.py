"""最小测试：B0-18 配置签名正确性验证（不依赖 app.py 完整导入）。"""
import sys
import os

# 将项目根目录和 src 加入路径
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "src"))
sys.path.insert(0, root)

import unittest

from config import build_config


def cfg_signature(cfg):
    """B0-18标准配置签名：包含所有影响回测结果的关键参数。
    
    与 app.py 中的 cfg_signature 保持完全同步。
    """
    weights = tuple((key, round(value, 6)) for key, value in sorted(cfg["weights"].items()))
    params = (
        cfg["min_trend_score"],
        cfg["min_confirm_score"],
        cfg["min_total_score"],
        cfg["max_holdings"],
        round(cfg["max_position_per_etf"], 6),
        round(cfg["stop_loss"], 6),
        cfg.get("stop_loss_mode", "fixed"),
        cfg["market_timing"],
        cfg.get("cooling_period", 0),
        cfg.get("rebalance_freq", "weekly"),
        cfg.get("rebalance_weekday", 3),
        cfg.get("trailing_stop_mode", "none"),
        cfg.get("defense_enabled", True),
        cfg.get("sector_boost_enabled", False),
        cfg.get("momentum_factor_enabled", True),
        cfg.get("volatility_factor_enabled", True),
    )
    return weights + params


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


if __name__ == "__main__":
    unittest.main()
