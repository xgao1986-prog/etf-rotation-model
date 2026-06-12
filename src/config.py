"""
ETF轮动量化策略 - 全局配置 (v1.0 可信修复版)
A股ETF轮动模型 v1.0 配置文件

修复内容：
  - 横截面动量排名在合并全universe后计算
  - generate_signals使用groupby shift防止跨ETF污染
  - 大盘择时从signals_df取market_signal
  - 仓位按当前组合净值计算
  - AKShare后缀修复（159xxx/16xxxx=.SZ）
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
# 16只行业ETF + 黄金ETF(防御资产) + 沪深300基准
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
    '518880.SH': '黄金ETF',  # 防御资产
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

# ==================== 实验性配置 (v1.1/v1.2) ====================
# 以下配置仅供实验分支使用，默认不启用

# 黄金ETF（低相关性避险资产）
GOLD_ETF = {
    '518880.SH': '黄金ETF',
}

# 防御资产池（v1.3 防御模块）
# 当大盘择时信号低时，强制配置防御资产以降低回撤
DEFENSE_UNIVERSE = {
    '518880.SH': '黄金ETF',
    # 未来可扩展国债ETF: '511010.SH': '国债ETF'
}

# 防御资产配置比例（按大盘择时信号）
# 例如: market_signal=0.2 时，防御资产占目标仓位的 50%
DEFENSE_ALLOCATION = {
    0.2: 0.50,   # 防御仓位: 50%配防御资产
    0.5: 0.20,   # 半仓: 20%配防御资产
    1.0: 0.00,   # 满仓: 不配防御资产
}

# 防御资产评分参数（简化版，不依赖动量排名）
DEFENSE_CONFIG = {
    'defense_enabled': True,        # 是否启用防御模块
    'defense_mode': 'mandatory',    # 'mandatory'=强制配置, 'optional'=可选
    'min_defense_trend_score': 10,  # 防御资产趋势最低分（比股票ETF宽松）
    'min_defense_total_score': 25,  # 防御资产总评分最低分
}

# 板块指数配置（v1.1 板块动量增强）
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

# 实验性策略参数（默认关闭，需显式启用）
EXPERIMENTAL_CONFIG = {
    # 板块动量增强（v1.1）
    'sector_boost_enabled': False,   # 默认关闭
    'sector_boost_weight': 15,
    'sector_momentum_lookback': 20,
    'sector_ma_short': 20,
    'sector_top_n_threshold': 3,
    'sector_above_ma_bonus': 5,
    'sector_momentum_bonus': 10,
    
    # 调仓频率因子（v1.2）
    'rebalance_freq': 'weekly',
    'rebalance_ordinal': 1,
    
    # 冷静期因子（v1.2）
    'cooling_period': 5,
    'cooling_score_boost': 10,
    
    # 动态止盈因子（v1.2）
    'trailing_stop_mode': 'none',    # 默认关闭
    'trailing_stop': None,
    'tier_1_pnl': 0.05,
    'tier_1_drawdown': -0.05,
    'tier_2_pnl': 0.15,
    'tier_2_drawdown': -0.08,
    'tier_3_pnl': 0.30,
    'tier_3_drawdown': -0.12,
}

# 合并配置工具（实验分支使用）
def build_config(strategy_cfg=None, experimental_cfg=None):
    """合并策略配置和实验性配置"""
    import copy
    cfg = copy.deepcopy(STRATEGY_CONFIG)
    cfg.update(copy.deepcopy(EXPERIMENTAL_CONFIG))
    if strategy_cfg:
        cfg.update(strategy_cfg)
    if experimental_cfg:
        cfg.update(experimental_cfg)
    return cfg
