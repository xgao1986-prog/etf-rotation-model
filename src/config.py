"""
ETF轮动量化策略 - 全局配置 (v1.1 防御型交易规则版本)
A股ETF轮动模型 v1.1 配置文件

版本定位：
  v1.1 = "防御型交易规则版本"
  核心内容：防御资产层（黄金/国债）+ 调仓日规则 + 动态止盈 + 冷静期 + 大盘择时
  
不包含：
  - 行业板块指数数据（移到 v1.2/v1.3 信号增强版本）
  - 板块动量增强（移到 v1.2）

修复内容（v1.0 → v1.1）：
  - 横截面动量排名在合并全universe后计算
  - generate_signals使用groupby shift防止跨ETF污染
  - 大盘择时从signals_df取market_signal
  - 仓位按当前组合净值计算
  - AKShare后缀修复（159xxx/16xxxx=.SZ）
  - 新增防御资产模块（黄金/国债ETF熊市强制配置）
  - 新增动态止盈模块（分档止盈）
  - 新增冷静期模块（止损后冷却）
"""

import os

# ==================== 路径配置 ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'database')
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
SIGNAL_DIR = os.path.join(BASE_DIR, 'signals')

for d in [DATA_DIR, REPORT_DIR, SIGNAL_DIR]:
    os.makedirs(d, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, 'etf_model.db')

# ==================== ETF标的池 ====================
# 16只行业ETF + 黄金ETF(防御资产) + 国债ETF(防御资产) + 沪深300基准
ETF_UNIVERSE = {
    '512480.SH': '半导体ETF',
    '515230.SH': '软件ETF',
    '515880.SH': '通信ETF',
    '512010.SH': '医药ETF',
    '159928.SZ': '消费ETF',
    '516160.SH': '新能源ETF',
    '516110.SH': '汽车ETF',
    '512800.SH': '银行ETF',
    '512000.SH': '券商ETF',
    '512660.SH': '军工ETF',
    '512980.SH': '传媒ETF',
    '512400.SH': '有色金属ETF',
    '159996.SZ': '家电ETF',
    '159865.SZ': '养殖ETF',
    '159697.SZ': '油气ETF',
    '159530.SZ': '机器人ETF',
    '518880.SH': '黄金ETF',   # 防御资产
    '511010.SH': '国债ETF',   # 防御资产
}

BENCHMARK = '000300.SH'  # 沪深300

# 提取纯代码（不带后缀）用于AKShare等数据源
ETF_CODES = [code.split('.')[0] for code in ETF_UNIVERSE.keys()]
BENCHMARK_CODE = '000300'

# ==================== 策略参数 ====================
STRATEGY_CONFIG = {
    # 评分权重（v1.0原始参数）
    'weights': {
        'trend': 0.30,      # 趋势强度
        'confirm': 0.20,    # 趋势确认
        'momentum': 0.25,   # 动量排名
        'volume': 0.15,     # 成交量
        'volatility': 0.10, # 波动率
    },
    
    # 均线参数
    'ma_short': 20,       # 短期均线
    'ma_long': 50,        # 长期均线
    
    # 入场阈值
    'min_trend_score': 15,      # 趋势最低分
    'min_confirm_score': 4,     # 确认最低分（至少1天在均线之上）
    'min_total_score': 40,      # 总评分最低分
    
    # 持仓控制
    'max_holdings': 5,          # 最多持有几只
    'max_position_per_etf': 0.15,  # 单只上限15%
    
    # 风控
    'stop_loss': -0.08,         # 固定止损线-8%（相对于成本价）
    
    # 调仓日
    'rebalance_weekday': 4,     # 调仓日（0=周一, 1=周二, 2=周三, 3=周四, 4=周五）
    
    # 大盘择时
    'market_timing': True,      # 是否启用大盘择时
    'market_ma_short': 20,      # 大盘短期均线
    'market_ma_long': 50,       # 大盘长期均线
    
    # 交易费率
    'commission_rate': 0.0003,  # 佣金率0.03%
    'min_commission': 5.0,      # 最低佣金5元
}

# ==================== 数据源配置 ====================
DATA_SOURCE = {
    'primary': 'ifind_via_kimi',
    'backup': 'akshare',
    'ifind_max_tickers_per_query': 10,
    'ifind_max_years_per_query': 3,
}

# ==================== 回测参数 ====================
BACKTEST_CONFIG = {
    'start_date': '2019-06-03',
    'end_date': None,
    'initial_capital': 1_000_000,
    'in_sample_end': '2023-12-31',
    'out_sample_start': '2024-01-01',
}

# ==================== 通知配置 ====================
NOTIFY_CONFIG = {
    'email': {
        'enabled': False,
        'smtp_server': 'smtp.qq.com',
        'sender': '',
        'password': '',
        'receiver': '',
    },
    'wechat': {
        'enabled': False,
        'webhook_url': '',
    },
}

# ==================== 日志配置 ====================
LOG_CONFIG = {
    'level': 'INFO',
    'file': os.path.join(BASE_DIR, 'etf_strategy.log'),
    'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
}

# ==================== v1.1 防御资产模块 ====================
# 防御资产：低相关性避险资产，熊市强制配置以降低回撤

# 黄金ETF
GOLD_ETF = {
    '518880.SH': '黄金ETF',
}

# 防御资产池（v1.1 核心）
# 当大盘择时信号低时，强制配置防御资产以降低回撤
DEFENSE_UNIVERSE = {
    '518880.SH': '黄金ETF',
    '511010.SH': '国债ETF',
}

# 防御资产配置比例（按大盘择时信号）
# 支持两种模式：'step'=阶梯式, 'linear'=线性插值
DEFENSE_ALLOCATION_MODE = 'linear'  # 'step' 或 'linear'

# 防御比例关键点（market_signal → defense_allocation）
# 线性模式下，在关键点之间做线性插值
DEFENSE_ALLOCATION = {
    0.0: 0.80,   # 空仓: 80%配防御资产（几乎全防御）
    0.2: 0.50,   # 防御仓位: 50%配防御资产
    0.5: 0.20,   # 半仓: 20%配防御资产
    1.0: 0.00,   # 满仓: 不配防御资产
}

# 波动率增强配置（可选）
VOLATILITY_ENHANCEMENT = {
    'enabled': False,           # 是否启用波动率增强
    'lookback': 20,             # 波动率计算回看天数
    'threshold_low': 0.15,      # 低波动率阈值（年化15%）
    'threshold_high': 0.30,     # 高波动率阈值（年化30%）
    'adjustment_low': -0.05,    # 低波动率时减少防御比例
    'adjustment_high': 0.10,    # 高波动率时增加防御比例
    'max_allocation': 0.80,     # 防御比例上限
    'min_allocation': 0.00,     # 防御比例下限
}

# 防御资产评分参数（简化版，不依赖动量排名）
DEFENSE_CONFIG = {
    'defense_enabled': True,        # 是否启用防御模块
    'defense_mode': 'mandatory',    # 'mandatory'=强制配置, 'optional'=可选
    'min_defense_trend_score': 10,  # 防御资产趋势最低分（比股票ETF宽松）
    'min_defense_total_score': 25,  # 防御资产总评分最低分
}

# ==================== v1.1 交易规则配置 ====================
# 调仓日、动态止盈、冷静期等交易规则参数

TRADING_RULES_CONFIG = {
    # 调仓频率
    'rebalance_freq': 'weekly',      # weekly=每周, biweekly=双周, monthly=每月
    'rebalance_ordinal': 1,          # 第几个交易日（1=第一个）
    'rebalance_weekday': 4,          # 调仓日（0=周一, 4=周五）
    
    # 冷静期（止损后冷却）
    'cooling_period': 5,             # 止损后冷却天数
    'cooling_score_boost': 10,       # 冷却期后重新买入的评分加分
    
    # 动态止盈
    'trailing_stop_mode': 'tiered',  # 'none'=关闭, 'simple'=简单, 'tiered'=分档
    'trailing_stop': None,           # 简单模式：回撤阈值
    'tier_1_pnl': 0.05,              # 1档：盈利5%
    'tier_1_drawdown': -0.05,        # 1档：允许回撤5%
    'tier_2_pnl': 0.15,              # 2档：盈利15%
    'tier_2_drawdown': -0.08,        # 2档：允许回撤8%
    'tier_3_pnl': 0.30,              # 3档：盈利30%
    'tier_3_drawdown': -0.12,        # 3档：允许回撤12%
}

# ==================== v1.1 交易执行配置（待完善）====================
# 成交价格选择：当前使用收盘价，存在未来函数边缘问题
# 后续版本需纳入考量，支持多种成交价格模式

EXECUTION_CONFIG = {
    # 成交价格模式
    'price_mode': 'close',           # 'close'=当日收盘价, 'open'=次日开盘价, 'vwap'=成交量加权均价
    
    # 滑点设置（price_mode='close' 时生效，模拟实际成交偏差）
    'slippage_enabled': False,       # 是否启用滑点
    'slippage_bps': 5,               # 滑点基点（5bps = 0.05%）
    
    # 未来函数说明（v1.1 当前状态）
    # 问题：信号基于当日收盘数据计算，交易执行也在当日收盘
    #       实际操作中，收盘前5分钟才能确认信号，对ETF流动性要求较高
    # 影响：回测结果偏乐观（约0.5-1%年化差异）
    # 方案对比：
    #   - close: 当前方案，最乐观，适合策略验证阶段
    #   - open:  次日开盘价，更保守，接近实盘
    #   - vwap:  成交量加权均价，折中方案，需更多数据支持
}

# ==================== v1.2/v1.3 预留：行业信号增强 ====================
# 以下配置属于未来版本（行业板块指数信号增强），v1.1 不启用
# 保留在此文件中以便后续版本迭代，但默认不加载

# 板块指数配置（v1.2 预留）
# SECTOR_INDEX_UNIVERSE = {
#     '801080': '电子',
#     '801750': '计算机',
#     '801770': '通信',
#     '801150': '医药生物',
#     '801120': '食品饮料',
#     '801730': '电力设备',
#     '801880': '汽车',
#     '801780': '银行',
#     '801790': '非银金融',
#     '801740': '国防军工',
#     '801760': '传媒',
#     '801050': '有色金属',
#     '801110': '家用电器',
#     '801010': '农林牧渔',
#     '801960': '石油石化',
#     '801890': '机械设备',
# }
# 
# ETF_TO_SECTOR_MAPPING = {
#     '512480.SH': '801080',
#     '515230.SH': '801750',
#     '515880.SH': '801770',
#     '512010.SH': '801150',
#     '159928.SZ': '801120',
#     '516160.SH': '801730',
#     '516110.SH': '801880',
#     '512800.SH': '801780',
#     '512000.SH': '801790',
#     '512660.SH': '801740',
#     '512980.SH': '801760',
#     '512400.SH': '801050',
#     '159996.SZ': '801110',
#     '159865.SZ': '801010',
#     '159697.SZ': '801960',
#     '159530.SZ': '801890',
# }
# 
# SECTOR_CODES = list(SECTOR_INDEX_UNIVERSE.keys())

# 板块动量增强参数（v1.2 预留）
# SECTOR_BOOST_CONFIG = {
#     'sector_boost_enabled': False,   # 默认关闭
#     'sector_boost_weight': 15,
#     'sector_momentum_lookback': 20,
#     'sector_ma_short': 20,
#     'sector_top_n_threshold': 3,
#     'sector_above_ma_bonus': 5,
#     'sector_momentum_bonus': 10,
# }

# ==================== 实验性配置（已整合到各版本模块） ====================
# 以下配置已分别整合到 TRADING_RULES_CONFIG 和 DEFENSE_CONFIG 中
# 保留此处仅用于向后兼容，新代码请直接使用上述模块配置

EXPERIMENTAL_CONFIG = {
    # 已整合到 TRADING_RULES_CONFIG
    'rebalance_freq': TRADING_RULES_CONFIG['rebalance_freq'],
    'rebalance_ordinal': TRADING_RULES_CONFIG['rebalance_ordinal'],
    'cooling_period': TRADING_RULES_CONFIG['cooling_period'],
    'cooling_score_boost': TRADING_RULES_CONFIG['cooling_score_boost'],
    'trailing_stop_mode': TRADING_RULES_CONFIG['trailing_stop_mode'],
    'trailing_stop': TRADING_RULES_CONFIG['trailing_stop'],
    'tier_1_pnl': TRADING_RULES_CONFIG['tier_1_pnl'],
    'tier_1_drawdown': TRADING_RULES_CONFIG['tier_1_drawdown'],
    'tier_2_pnl': TRADING_RULES_CONFIG['tier_2_pnl'],
    'tier_2_drawdown': TRADING_RULES_CONFIG['tier_2_drawdown'],
    'tier_3_pnl': TRADING_RULES_CONFIG['tier_3_pnl'],
    'tier_3_drawdown': TRADING_RULES_CONFIG['tier_3_drawdown'],
    
    # v1.2 预留（默认关闭）
    'sector_boost_enabled': False,
    'sector_boost_weight': 15,
    'sector_momentum_lookback': 20,
    'sector_ma_short': 20,
    'sector_top_n_threshold': 3,
    'sector_above_ma_bonus': 5,
    'sector_momentum_bonus': 10,
}

# 合并配置工具（v1.1 使用）
def build_config(strategy_cfg=None, trading_rules_cfg=None, defense_cfg=None):
    """合并策略配置、交易规则配置和防御配置"""
    import copy
    cfg = copy.deepcopy(STRATEGY_CONFIG)
    cfg.update(copy.deepcopy(TRADING_RULES_CONFIG))
    cfg.update(copy.deepcopy(DEFENSE_CONFIG))
    cfg.update(copy.deepcopy(EXPERIMENTAL_CONFIG))
    if strategy_cfg:
        cfg.update(strategy_cfg)
    if trading_rules_cfg:
        cfg.update(trading_rules_cfg)
    if defense_cfg:
        cfg.update(defense_cfg)
    return cfg
