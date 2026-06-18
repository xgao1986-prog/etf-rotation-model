# -*- coding: utf-8 -*-
"""
freeze_b0.py - 彻底冻结 B0 基线配置

保存回测引擎最终实际生效的完整配置，包括：
- 交易规则（TRADING_RULES_CONFIG）
- 防御模块（DEFENSE_CONFIG）
- 宽基模块（FALLBACK_EQUITY_CONFIG）
- 市场状态配置（MARKET_REGIME_CONFIG）
- 策略参数（STRATEGY_CONFIG）
- 回测参数（BACKTEST_CONFIG）
- 回测引擎硬编码参数
- 策略引擎硬编码参数
- 代码 commit SHA

生成配置哈希（SHA256），确保后续任何参数变化都可追溯。
"""
import sys
sys.path.insert(0, 'src')

import json
import hashlib
import subprocess
from datetime import datetime
import pandas as pd
import numpy as np
import config


def get_git_sha():
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd='D:/etf_rotation_model',
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def get_file_sha256(filepath):
    """计算文件SHA256哈希"""
    try:
        import hashlib
        h = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        return f"ERROR: {e}"


def main():
    print("="*80)
    print("B0 基线冻结")
    print("="*80)
    print(f"冻结时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. Git commit SHA
    git_sha = get_git_sha()
    print(f"[1/5] Git Commit SHA: {git_sha}")

    # 2. 收集所有配置
    print("\n[2/5] 收集所有配置...")
    
    # 文件哈希
    file_hashes = {
        'config.py': get_file_sha256('src/config.py'),
        'backtest.py': get_file_sha256('src/backtest.py'),
        'strategy.py': get_file_sha256('src/strategy.py'),
        'market_regime.py': get_file_sha256('src/market_regime.py'),
    }
    
    # 配置字典（排除可调用对象）
    def clean_dict(d):
        """递归清理字典，排除可调用对象和循环引用"""
        if isinstance(d, dict):
            return {k: clean_dict(v) for k, v in d.items() if not callable(v)}
        elif isinstance(d, (list, tuple)):
            return [clean_dict(x) for x in d if not callable(x)]
        elif isinstance(d, (int, float, str, bool, type(None))):
            return d
        else:
            return str(d)
    
    b0_config = {
        # 版本信息
        'version': 'B0',
        'freeze_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'git_sha': git_sha,
        'code_file_hashes': file_hashes,
        
        # 核心配置
        'STRATEGY_CONFIG': clean_dict(config.STRATEGY_CONFIG),
        'BACKTEST_CONFIG': clean_dict(config.BACKTEST_CONFIG),
        'TRADING_RULES_CONFIG': clean_dict(config.TRADING_RULES_CONFIG),
        'DEFENSE_CONFIG': clean_dict(config.DEFENSE_CONFIG),
        'FALLBACK_EQUITY_CONFIG': clean_dict(config.FALLBACK_EQUITY_CONFIG),
        'MARKET_REGIME_CONFIG': clean_dict(config.MARKET_REGIME_CONFIG),
        'EXECUTION_CONFIG': clean_dict(config.EXECUTION_CONFIG),
        'STABILITY_CONFIG': clean_dict(config.STABILITY_CONFIG),
        
        # 标的数据
        'ETF_UNIVERSE': dict(config.ETF_UNIVERSE),
        'DEFENSE_UNIVERSE': dict(config.DEFENSE_UNIVERSE),
        'FALLBACK_EQUITY_UNIVERSE': dict(config.FALLBACK_EQUITY_UNIVERSE),
        'CONCEPT_UNIVERSE': dict(config.CONCEPT_UNIVERSE),
        'CORE_UNIVERSE': dict(config.CORE_UNIVERSE),
        'BENCHMARK': config.BENCHMARK,
        
        # 关键参数
        'CORRELATION_THRESHOLD': config.CORRELATION_THRESHOLD,
        'DEFENSE_ALLOCATION': config.DEFENSE_ALLOCATION,
        'DEFENSE_ALLOCATION_MODE': config.DEFENSE_ALLOCATION_MODE,
        'VOLATILITY_ENHANCEMENT': clean_dict(config.VOLATILITY_ENHANCEMENT),
        'ETF_GROUP_MAP': config.ETF_GROUP_MAP,
        
        # 数据截止
        'COMMON_CUTOFF': '2026-06-05',
        
        # 回测引擎硬编码参数（从backtest.py中提取）
        'BACKTEST_ENGINE_HARDCODED': {
            'HARD_REDUNDANCY_THRESHOLD': 0.97,
            'SOFT_PENALTY_MIN': 0.85,
            'SOFT_PENALTY_MAX': 0.97,
            'SOFT_PENALTY_MAX_REDUCTION': 0.15,
            'corr_window': 60,
            'min_valid_pairs_ratio': 0.67,
            'price_mode': 'close',
            'slippage_enabled': False,
            'slippage_bps': 5,
        },
        
        # 策略引擎硬编码参数（从strategy.py中提取）
        'STRATEGY_ENGINE_HARDCODED': {
            'trend_score_weights': {
                'above_ma20': 15,
                'above_ma50': 10,
                'ma20_slope_positive': 5,
            },
            'confirm_score_per_day': 4,
            'confirm_max_days': 5,
            'volume_score_weights': {
                'volume_up_and_price_up': 15,
                'volume_ratio_high': 10,
                'normal': 5,
            },
            'vol_score_weights': {
                'moderate_vol': 10,  # 1-4%
                'high_vol': 5,       # 4-6%
            },
            'fallback_ma_check': 'ma20_or_ma50',
            'fallback_ma_slope_check': 'not_steep_down',
        },
        
        # 市场状态检测器硬编码参数
        'MARKET_REGIME_HARDCODED': {
            'state_names': {1: '强牛', 2: '弱牛', 3: '震荡', 4: '熊市'},
            'default_state': 3,
            'default_confidence': 0.5,
        },
    }
    
    # 3. 生成配置哈希
    print("\n[3/5] 生成配置哈希...")
    config_json = json.dumps(b0_config, sort_keys=True, ensure_ascii=False, default=str)
    config_hash = hashlib.sha256(config_json.encode('utf-8')).hexdigest()
    print(f"  B0 配置哈希 (SHA256): {config_hash}")
    
    # 4. 保存冻结文件
    print("\n[4/5] 保存冻结文件...")
    freeze_path = 'reports/b0_baseline_freeze.json'
    with open(freeze_path, 'w', encoding='utf-8') as f:
        json.dump(b0_config, f, indent=2, ensure_ascii=False, default=str)
    print(f"  冻结文件: {freeze_path}")
    
    # 5. 生成Markdown摘要
    print("\n[5/5] 生成Markdown摘要...")
    md_path = 'reports/b0_baseline_freeze.md'
    lines = []
    lines.append("# B0 基线冻结报告")
    lines.append(f"\n**冻结时间**: {b0_config['freeze_time']}")
    lines.append(f"**Git Commit SHA**: `{git_sha}`")
    lines.append(f"**配置哈希 (SHA256)**: `{config_hash}`")
    lines.append(f"**数据截止**: {b0_config['COMMON_CUTOFF']}")
    
    lines.append(f"\n## 代码文件哈希")
    lines.append(f"\n| 文件 | SHA256 |")
    lines.append(f"|------|--------|")
    for fname, fhash in file_hashes.items():
        lines.append(f"| {fname} | `{fhash}` |")
    
    lines.append(f"\n## 核心配置摘要")
    
    lines.append(f"\n### 交易规则（TRADING_RULES_CONFIG）")
    lines.append(f"- 调仓频率: {b0_config['TRADING_RULES_CONFIG']['rebalance_freq']}")
    lines.append(f"- 调仓日: 星期{b0_config['TRADING_RULES_CONFIG']['rebalance_weekday']}")
    lines.append(f"- 冷静期: {b0_config['TRADING_RULES_CONFIG']['cooling_period']} 天")
    lines.append(f"- 动态止盈: {b0_config['TRADING_RULES_CONFIG']['trailing_stop_mode']}")
    lines.append(f"- 止损线: {b0_config['STRATEGY_CONFIG']['stop_loss']}")
    lines.append(f"- 止损模式: {b0_config['STRATEGY_CONFIG']['stop_loss_mode']}")
    
    lines.append(f"\n### 防御模块（DEFENSE_CONFIG）")
    lines.append(f"- 启用: {b0_config['DEFENSE_CONFIG']['defense_enabled']}")
    lines.append(f"- 模式: {b0_config['DEFENSE_CONFIG']['defense_mode']}")
    lines.append(f"- 牛市防御上限: {b0_config['DEFENSE_CONFIG']['defense_fill_max_ratio_bull']}")
    lines.append(f"- 熊市防御上限: {b0_config['DEFENSE_CONFIG']['defense_fill_max_ratio_bear']}")
    
    lines.append(f"\n### 宽基模块（FALLBACK_EQUITY_CONFIG）")
    lines.append(f"- 启用: {b0_config['FALLBACK_EQUITY_CONFIG']['fallback_equity_enabled']}")
    lines.append(f"- 宽基最低总评分: {b0_config['FALLBACK_EQUITY_CONFIG']['min_fallback_total_score']}")
    lines.append(f"- 宽基趋势最低分: {b0_config['FALLBACK_EQUITY_CONFIG']['min_fallback_trend_score']}")
    
    lines.append(f"\n### 市场状态配置（MARKET_REGIME_CONFIG）")
    lines.append(f"- 启用: {b0_config['MARKET_REGIME_CONFIG']['enabled']}")
    lines.append(f"- 模式: {b0_config['MARKET_REGIME_CONFIG']['mode']}")
    lines.append(f"- 确认天数: {b0_config['MARKET_REGIME_CONFIG']['confirmation_days']}")
    lines.append(f"- 强牛阈值: {b0_config['MARKET_REGIME_CONFIG']['trend_position_threshold_strong']}")
    lines.append(f"- 震荡下限: {b0_config['MARKET_REGIME_CONFIG']['trend_position_threshold_weak']}")
    
    lines.append(f"\n### 策略参数（STRATEGY_CONFIG）")
    lines.append(f"- 评分权重: 趋势{b0_config['STRATEGY_CONFIG']['weights']['trend']:.0%} + 确认{b0_config['STRATEGY_CONFIG']['weights']['confirm']:.0%} + 动量{b0_config['STRATEGY_CONFIG']['weights']['momentum']:.0%} + 成交量{b0_config['STRATEGY_CONFIG']['weights']['volume']:.0%} + 波动率{b0_config['STRATEGY_CONFIG']['weights']['volatility']:.0%}")
    lines.append(f"- 均线: MA{b0_config['STRATEGY_CONFIG']['ma_short']}/MA{b0_config['STRATEGY_CONFIG']['ma_long']}")
    lines.append(f"- 最低总评分: {b0_config['STRATEGY_CONFIG']['min_total_score']}")
    lines.append(f"- 最大持仓: {b0_config['STRATEGY_CONFIG']['max_holdings']} 只")
    lines.append(f"- 单只上限: {b0_config['STRATEGY_CONFIG']['max_position_per_etf']:.0%}")
    lines.append(f"- 佣金率: {b0_config['STRATEGY_CONFIG']['commission_rate']:.4%}")
    lines.append(f"- 最低佣金: {b0_config['STRATEGY_CONFIG']['min_commission']}")
    
    lines.append(f"\n### 回测参数（BACKTEST_CONFIG）")
    lines.append(f"- 初始资金: {b0_config['BACKTEST_CONFIG']['initial_capital']:,}")
    lines.append(f"- 开始日期: {b0_config['BACKTEST_CONFIG']['start_date']}")
    lines.append(f"- 样本内截止: {b0_config['BACKTEST_CONFIG']['in_sample_end']}")
    lines.append(f"- 样本外开始: {b0_config['BACKTEST_CONFIG']['out_sample_start']}")
    
    lines.append(f"\n### 回测引擎硬编码参数")
    lines.append(f"- 硬去重阈值: {b0_config['BACKTEST_ENGINE_HARDCODED']['HARD_REDUNDANCY_THRESHOLD']}")
    lines.append(f"- 软惩罚最小: {b0_config['BACKTEST_ENGINE_HARDCODED']['SOFT_PENALTY_MIN']}")
    lines.append(f"- 软惩罚最大: {b0_config['BACKTEST_ENGINE_HARDCODED']['SOFT_PENALTY_MAX']}")
    lines.append(f"- 软惩罚最大降幅: {b0_config['BACKTEST_ENGINE_HARDCODED']['SOFT_PENALTY_MAX_REDUCTION']}")
    lines.append(f"- 相关性窗口: {b0_config['BACKTEST_ENGINE_HARDCODED']['corr_window']}")
    
    lines.append(f"\n### ETF池")
    lines.append(f"- 行业ETF: {len(b0_config['ETF_UNIVERSE'])} 只")
    lines.append(f"- 概念ETF: {len(b0_config['CONCEPT_UNIVERSE'])} 只")
    lines.append(f"- 防御资产: {len(b0_config['DEFENSE_UNIVERSE'])} 只")
    lines.append(f"- 宽基补仓: {len(b0_config['FALLBACK_EQUITY_UNIVERSE'])} 只")
    lines.append(f"- 核心池: {len(b0_config['CORE_UNIVERSE'])} 只")
    lines.append(f"- 基准: {b0_config['BENCHMARK']}")
    
    lines.append(f"\n### 相关性阈值")
    lines.append(f"- CORRELATION_THRESHOLD: {b0_config['CORRELATION_THRESHOLD']}")
    
    lines.append(f"\n### 防御资产配置")
    lines.append(f"- 配置模式: {b0_config['DEFENSE_ALLOCATION_MODE']}")
    lines.append(f"- 配置映射: {b0_config['DEFENSE_ALLOCATION']}")
    
    lines.append(f"\n## 完整冻结配置（JSON）")
    lines.append(f"\n```json")
    lines.append(config_json)
    lines.append(f"```")
    
    lines.append(f"\n## 版本边界")
    lines.append(f"- B0 已冻结，配置哈希: {config_hash}")
    lines.append(f"- 后续任何参数变更需重新生成哈希并记录差异")
    lines.append(f"- 当前冻结用于 B1 单变量测试的基准对比")
    
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"  Markdown摘要: {md_path}")
    
    print(f"\n{'='*80}")
    print(f"[OK] B0 基线冻结完成")
    print(f"  配置哈希: {config_hash}")
    print(f"  Git SHA: {git_sha}")
    print(f"  冻结文件: {freeze_path}")
    print(f"{'='*80}")


if __name__ == '__main__':
    main()
