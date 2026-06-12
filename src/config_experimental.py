"""
ETF轮动量化策略 - 实验性配置 (v1.1/v1.2)

此文件包含板块增强、黄金ETF、动态止盈、冷却期、调仓日因子等实验性功能。
这些功能尚未在可信回测中验证，仅供实验分支使用。

使用方法：
    from config_experimental import EXPERIMENTAL_CONFIG
    cfg = {**STRATEGY_CONFIG, **EXPERIMENTAL_CONFIG}
"""

# ==================== 实验性ETF扩展 ====================
# 黄金ETF（低相关性避险资产）
GOLD_ETF = {
    '518880.SH': '黄金ETF',
}

# ==================== 板块指数配置（v1.1 实验性）====================
SECTOR_INDEX_UNIVERSE = {
    '801080': '电子',
    '801750': '计算机',
    '801770': '通信',
    '801150': '医药生物',
    '801120': '食品饮料',
    '801730': '电力设备',
    '801880': '汽车',
    '801780': '银行',
    '801790': '非银金融',
    '801740': '国防军工',
    '801760': '传媒',
    '801050': '有色金属',
    '801110': '家用电器',
    '801010': '农林牧渔',
    '801960': '石油石化',
    '801890': '机械设备',
}

ETF_TO_SECTOR_MAPPING = {
    '512480.SH': '801080',
    '515230.SH': '801750',
    '515880.SH': '801770',
    '512010.SH': '801150',
    '159928.SZ': '801120',
    '516160.SH': '801730',
    '516110.SH': '801880',
    '512800.SH': '801780',
    '512000.SH': '801790',
    '512660.SH': '801740',
    '512980.SH': '801760',
    '512400.SH': '801050',
    '159996.SZ': '801110',
    '159865.SZ': '801010',
    '159697.SZ': '801960',
    '159530.SZ': '801890',
}

SECTOR_CODES = list(SECTOR_INDEX_UNIVERSE.keys())

# ==================== 实验性策略参数 ====================
EXPERIMENTAL_STRATEGY = {
    # 板块动量增强（v1.1）
    'sector_boost_enabled': True,
    'sector_boost_weight': 15,
    'sector_momentum_lookback': 20,
    'sector_ma_short': 20,
    'sector_top_n_threshold': 3,
    'sector_above_ma_bonus': 5,
    'sector_momentum_bonus': 10,
}

# ==================== 可调因子配置（v1.2 实验性）====================
FACTOR_CONFIG = {
    # 调仓频率因子
    'rebalance_freq': 'weekly',
    'rebalance_weekday': 3,
    'rebalance_ordinal': 1,
    
    # 冷静期因子
    'cooling_period': 5,
    'cooling_score_boost': 10,
    
    # 动态止盈因子
    'trailing_stop_mode': 'simple',
    'trailing_stop': None,
    'tier_1_pnl': 0.05,
    'tier_1_drawdown': -0.05,
    'tier_2_pnl': 0.15,
    'tier_2_drawdown': -0.08,
    'tier_3_pnl': 0.30,
    'tier_3_drawdown': -0.12,
}

# 合并为完整实验配置
EXPERIMENTAL_CONFIG = {
    **EXPERIMENTAL_STRATEGY,
    **FACTOR_CONFIG,
}
