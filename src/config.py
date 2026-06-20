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
# 16只行业ETF（A股板块轮动主体）+ 沪深300基准
# v1.2.1: 默认保持16只，35只实验作为动态池治理的输入
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
}

BENCHMARK = '000300.SH'  # 沪深300

# 申万一级行业指数映射（v1.3 研究用，30个行业）
# 格式: 指数代码: (指数名称, 映射ETF列表)
# 历史：2003版28个 → 2014版新增11个 → 2021版新增4个（从采掘/公用事业拆分）
# 当前可用30个：15个旧版保留 + 11个2014新版 + 4个2021新版

SECTOR_INDEX_UNIVERSE = {
    # === 旧版保留（16个）===
    '801010.SI': ('农林牧渔', ['159865.SZ']),
    '801030.SI': ('基础化工', []),  # 原名化工
    '801040.SI': ('钢铁', []),
    '801050.SI': ('有色金属', ['512400.SH']),
    '801080.SI': ('电子', ['512480.SH', '588200.SH']),
    '801110.SI': ('家用电器', ['159996.SZ']),
    '801120.SI': ('食品饮料', ['512690.SH', '515170.SH']),
    '801130.SI': ('纺织服饰', []),  # 原名纺织服装
    '801140.SI': ('轻工制造', []),
    '801150.SI': ('医药生物', ['512010.SH', '159992.SZ', '159898.SZ']),
    '801160.SI': ('公用事业', []),
    '801170.SI': ('交通运输', []),
    '801180.SI': ('房地产', []),
    '801200.SI': ('商贸零售', []),  # 原名商业贸易
    '801210.SI': ('社会服务', ['159766.SZ']),  # 原名休闲服务
    '801230.SI': ('综合', []),
    
    # === 2014年后新增/调整（11个）===
    '801710.SI': ('建筑材料', []),
    '801720.SI': ('建筑装饰', []),
    '801730.SI': ('电力设备', ['516160.SH', '515790.SH', '159566.SZ']),  # 原名电气设备
    '801740.SI': ('国防军工', ['512660.SH']),
    '801750.SI': ('计算机', ['515230.SH', '516510.SH']),
    '801760.SI': ('传媒', ['512980.SH', '159869.SZ']),
    '801770.SI': ('通信', ['515880.SH', '515050.SH']),
    '801780.SI': ('银行', ['512800.SH']),
    '801790.SI': ('非银金融', ['512000.SH']),
    '801880.SI': ('汽车', ['516110.SH']),
    '801890.SI': ('机械设备', ['159530.SZ', '562500.SH']),
    
    # === 2021版新增/拆分（4个）===
    '801950.SI': ('煤炭', []),  # 从采掘拆分
    '801960.SI': ('石油石化', ['159697.SZ']),  # 从采掘拆分
    '801970.SI': ('环保', []),  # 从公用事业拆分，独立为一级行业
    '801980.SI': ('美容护理', []),  # 2021版新增一级行业
}

# 提取纯代码（不带后缀）用于AKShare等数据源
ETF_CODES = [code.split('.')[0] for code in ETF_UNIVERSE.keys()]
BENCHMARK_CODE = '000300'

# 行业指数代码（纯代码，用于AKShare）
SECTOR_CODES = [code.split('.')[0] for code in SECTOR_INDEX_UNIVERSE.keys()]

# ETF到行业指数的映射（反向查找）
ETF_TO_SECTOR_MAPPING = {}
for sector_code, (name, etfs) in SECTOR_INDEX_UNIVERSE.items():
    for etf in etfs:
        if etf not in ETF_TO_SECTOR_MAPPING:
            ETF_TO_SECTOR_MAPPING[etf] = []
        ETF_TO_SECTOR_MAPPING[etf].append(sector_code)

# ==================== 概念ETF池（新增）====================
CONCEPT_UNIVERSE = {
    '588200.SH': '科创芯片ETF',
    '159869.SZ': '游戏ETF',
    '516510.SH': '云计算ETF',
    '562500.SH': '机器人ETF',
    '159740.SZ': '碳中和ETF',
    '515050.SH': '5GETF',
    '512690.SH': '白酒ETF',
    '515170.SH': '食品饮料ETF',
    '159766.SZ': '旅游ETF',
    '159992.SZ': '创新药ETF',
    '159898.SZ': '医疗器械ETF',
    '515790.SH': '光伏ETF',
    '159566.SZ': '储能电池ETF',
    '513160.SH': '港股科技30ETF',
    '510880.SH': '红利ETF',
    '560700.SH': '央企改革ETF',
}

# ==================== 防御资产池（低相关补仓资产）====================
# 不参与日常轮动，只在行业ETF和宽基ETF都买不满时作为低相关补仓资产
DEFENSE_UNIVERSE = {
    '518880.SH': '黄金ETF',
    '511010.SH': '国债ETF',
}

# ==================== 宽基补仓ETF（第二层补仓）====================
# 当行业/主题ETF信号不足时，优先用宽基ETF补足股票敞口
# 不参与日常轮动排名，只在仓位打不满时补仓
FALLBACK_EQUITY_UNIVERSE = {
    '510300.SH': '沪深300ETF',
    '510500.SH': '中证500ETF',
    '159915.SZ': '创业板ETF',
    '588000.SH': '科创50ETF',
}

# 所有可交易标的（用于数据下载等）
ALL_TRADABLE_ETFS = {**ETF_UNIVERSE, **CONCEPT_UNIVERSE, **FALLBACK_EQUITY_UNIVERSE, **DEFENSE_UNIVERSE}

# 核心池（统一排序）：行业ETF + 概念ETF
CORE_UNIVERSE = {**ETF_UNIVERSE, **CONCEPT_UNIVERSE}

# 备选池（兜底）：宽基 + 防御
FALLBACK_UNIVERSE = {**FALLBACK_EQUITY_UNIVERSE, **DEFENSE_UNIVERSE}

# 相关性去重阈值
# 0.90 = 经验测试最优（收益107%，回撤-21%）
# 0.70 = 数据驱动但回测表现差（收益59%）
# 999 = 不去重，收益最高但回撤大（155%，-24%）
CORRELATION_THRESHOLD = 0.90

# ETF同类分组映射（用于同类ETF限制）
ETF_GROUP_MAP = {
    # 芯片/半导体组
    '512480.SH': 'chip', '588200.SH': 'chip', '588000.SH': 'chip',
    # 新能源/光伏/电池组
    '516160.SH': 'new_energy', '515790.SH': 'new_energy', '159566.SZ': 'new_energy',
    # 证券/金融/银行组
    '512000.SH': 'finance', '512800.SH': 'finance',
    # 白酒/食品饮料组
    '512690.SH': 'food_drink', '515170.SH': 'food_drink',
    # 医药/创新药/医疗器械组
    '512010.SH': 'medicine', '159992.SZ': 'medicine', '159898.SZ': 'medicine',
    # 游戏/传媒/云计算组
    '159869.SZ': 'tech_media', '512980.SH': 'tech_media', '516510.SH': 'tech_media',
    # 机器人组
    '159530.SZ': 'robot', '562500.SH': 'robot',
    # 红利/央企组
    '510880.SH': 'state_owned', '560700.SH': 'state_owned',
    # 通信/5G组
    '515880.SH': 'telecom', '515050.SH': 'telecom',
    # 黄金/国债（防御资产）
    '518880.SH': 'defense', '511010.SH': 'defense',
    # 宽基
    '510300.SH': 'index', '510500.SH': 'index', '159915.SZ': 'index',
    # 其他
    '512660.SH': 'military', '159766.SZ': 'tourism', '159928.SZ': 'consumption',
    '159996.SZ': 'appliance', '159865.SZ': 'livestock', '159697.SZ': 'energy',
    '513160.SH': 'hk_tech', '515230.SH': 'software', '516110.SH': 'auto',
    '512400.SH': 'metal', '159740.SZ': 'carbon',
}

# 持仓稳定机制参数（可开关）
STABILITY_CONFIG = {
    'enabled': False,          # 是否启用持仓稳定机制
    'buy_rank_n': 5,           # 买入门槛：必须进入Top N
    'hold_rank_n': 12,         # 卖出门槛：跌出Top N才考虑卖出
    'exit_confirm_weeks': 2,   # 卖出确认：连续N周跌出hold_rank_n才卖
    'min_hold_days': 20,     # 最低持有期：持有<20天除非止损否则不卖
    'replacement_score_gap': 8, # 替换优势：新标的必须比当前持仓高至少8分
    'same_group_max_holdings': 1, # 同类分组限制：每组最多持有1只
}


# ==================== 策略参数 ====================
STRATEGY_CONFIG = {
    # 因子开关（v1.2.1 新增：可关闭单个因子，保留重新启用能力）
    'momentum_factor_enabled': False,  # 是否计入momentum_rank到total_score（关闭=no_momentum/B0.2）
    
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
    
    # 持仓控制（v1.1 三层补仓结构）
    # 行业/主题ETF：第一层，核心alpha来源
    'stock_max_holdings': 5,       # 行业ETF最多持有几只
    # 宽基补仓ETF：第二层，补足股票beta
    'fallback_equity_max_holdings': 3,  # 宽基ETF最多持有几只（在股票ETF买不满时补仓）
    # 黄金/国债：第三层，低相关现金替代
    'defense_max_holdings': 2,     # 防御资产最多持有几只
    # 总持仓上限（向后兼容，可用可不用）
    'total_max_holdings': 5,       # 总持仓上限（与 max_holdings 一致，保持向后兼容）
    # 向后兼容：max_holdings 保留为 stock_max_holdings 的别名
    'max_holdings': 5,             # 最多持有几只（= stock_max_holdings）
    
    # v2.5 调仓引擎开关（默认启用新引擎：顺序独立、总仓位受控）
    'use_v2_rebalance': True,
    'max_position_per_etf': 0.20,  # 单只上限20%（可用满）
    
    # 风控
    'stop_loss': -0.08,         # 固定止损线-8%（相对于成本价）
    
    # 止损模式
    'stop_loss_mode': 'fixed',   # 'fixed'=固定止损, 'atr'=ATR动态止损, 'none'=不止损
    'atr_period': 14,             # ATR计算周期
    'atr_stop_multiplier': 2.0,  # ATR止损倍数（止损价 = 成本 - multiplier * ATR）
    
    # 调仓日
    'rebalance_weekday': 3,     # 调仓日（0=周一, 1=周二, 2=周三, 3=周四, 4=周五）
    
    # 大盘择时
    'market_timing': False,      # 是否启用大盘择时（默认关闭，回测数据显示关闭后收益更高）
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

# 宽基补仓ETF评分参数（简化趋势条件）
FALLBACK_EQUITY_CONFIG = {
    'fallback_equity_enabled': False,      # 默认关闭：回测显示当前参数下宽基补仓为负贡献
                                           # 设为 True 可启用，未来可配合更严格的强势条件测试
    'min_fallback_trend_score': 10,          # 宽基趋势最低分（比行业ETF宽松）
    'min_fallback_confirm_score': 2,       # 宽基确认最低分
    'min_fallback_total_score': 25,          # 宽基总评分最低分（比行业ETF宽松）
    'fallback_ma_check': 'ma20_or_ma50',   # 趋势条件：收盘价>MA20 或 >MA50
    'fallback_ma_slope_check': 'not_steep_down',  # MA20斜率不急剧向下
}

# 防御资产评分参数（简化版，不依赖动量排名）
DEFENSE_CONFIG = {
    'defense_enabled': True,        # 是否启用防御模块
    'defense_mode': 'mandatory',    # 'mandatory'=强制配置, 'optional'=可选
    'min_defense_trend_score': 10,  # 防御资产趋势最低分（比股票ETF宽松）
    'min_defense_total_score': 25,  # 防御资产总评分最低分
    # v1.1新增：防御资产填充上限（当股票ETF信号不足时）
    'defense_fill_max_ratio_bull': 0.30,  # 牛市时防御资产最多占总资产30%
    'defense_fill_max_ratio_bear': 0.50,  # 熊市/弱市时防御资产最多占总资产50%
}

# ==================== v1.1 交易规则配置 ====================
# 调仓日、动态止盈、冷静期等交易规则参数

TRADING_RULES_CONFIG = {
    # 调仓频率
    'rebalance_freq': 'weekly',      # weekly=每周, biweekly=双周, monthly=每月
    'rebalance_ordinal': 1,          # 第几个交易日（1=第一个）
    'rebalance_weekday': 3,          # 调仓日（0=周一, 4=周五）
    
    # 冷静期（止损后冷却）
    'cooling_period': 0,             # 止损后冷却天数（0=不启用冷静期）
    'cooling_score_boost': 10,       # 冷却期后重新买入的评分加分
    
    # 动态止盈
    'trailing_stop_mode': 'none',    # 'none'=关闭, 'simple'=简单, 'tiered'=分档（本次关闭）
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

# ==================== v1.2 市场状态检测模块 ====================
# v1.2 采用 observer-first 策略：先检测状态，记录展示，不直接改交易参数

MARKET_REGIME_CONFIG = {
    'enabled': True,               # 是否启用市场状态检测
    'mode': 'observer',            # 'observer'=只记录展示；'adaptive'=按状态改参数（后续版本）
    'confirmation_days': 5,        # 状态切换确认天数（避免噪声）
    'ma_short': 20,              # 短期均线（与策略一致）
    'ma_long': 50,               # 长期均线（与策略一致）
    
    # 状态阈值
    'trend_position_threshold_strong': 1.02,   # 强牛：close > MA50 × 1.02
    'trend_position_threshold_weak': 0.98,     # 震荡下限：close > MA50 × 0.98
    'vol_low_threshold': 0.015,                # 低波动率阈值
    'vol_high_threshold': 0.025,               # 高波动率阈值
    'slope_accel_threshold': 1.0,              # 斜率加速阈值
    
    # 状态参数映射（v1.2 只做离线模拟，不默认启用）
    # 后续版本可切换 mode='adaptive' 使用以下映射
    'states': {
        1: {  # 强牛
            'min_total_score': 35,
            'defense_fill_max_ratio_bull': 0.0,
            'max_position_per_etf': 0.20,
            'stop_loss': -0.10,
            'trailing_stop_mode': 'standard',
            'fallback_equity_enabled': True,
            'fallback_equity_min_score': 35,
            'cooling_period_days': 3,
        },
        2: {  # 弱牛
            'min_total_score': 35,
            'defense_fill_max_ratio_bull': 0.15,
            'max_position_per_etf': 0.18,
            'stop_loss': -0.08,
            'trailing_stop_mode': 'tiered',
            'fallback_equity_enabled': True,
            'fallback_equity_min_score': 35,
            'cooling_period_days': 5,
        },
        3: {  # 震荡
            'min_total_score': 40,
            'defense_fill_max_ratio_bull': 0.30,
            'max_position_per_etf': 0.15,
            'stop_loss': -0.08,
            'trailing_stop_mode': 'tiered',
            'fallback_equity_enabled': False,
            'fallback_equity_min_score': 25,
            'cooling_period_days': 5,
        },
        4: {  # 熊市
            'min_total_score': 45,
            'defense_fill_max_ratio_bull': 0.50,
            'max_position_per_etf': 0.12,
            'stop_loss': -0.12,
            'trailing_stop_mode': 'standard',
            'fallback_equity_enabled': False,
            'fallback_equity_min_score': 25,
            'cooling_period_days': 7,
        },
    },
}

# 合并配置工具（v1.1 使用，v1.2 扩展）
def build_config(strategy_cfg=None, trading_rules_cfg=None, defense_cfg=None,
                 fallback_equity_cfg=None, backtest_cfg=None, market_regime_cfg=None):
    """合并策略配置、交易规则配置、防御配置、宽基补仓配置、回测配置和市场状态配置"""
    import copy
    cfg = copy.deepcopy(STRATEGY_CONFIG)
    cfg.update(copy.deepcopy(BACKTEST_CONFIG))
    cfg.update(copy.deepcopy(TRADING_RULES_CONFIG))
    cfg.update(copy.deepcopy(DEFENSE_CONFIG))
    cfg.update(copy.deepcopy(FALLBACK_EQUITY_CONFIG))
    cfg.update(copy.deepcopy(EXPERIMENTAL_CONFIG))
    cfg.update(copy.deepcopy(MARKET_REGIME_CONFIG))
    if strategy_cfg:
        cfg.update(strategy_cfg)
    if trading_rules_cfg:
        cfg.update(trading_rules_cfg)
    if defense_cfg:
        cfg.update(defense_cfg)
    if fallback_equity_cfg:
        cfg.update(fallback_equity_cfg)
    if backtest_cfg:
        cfg.update(backtest_cfg)
    if market_regime_cfg:
        cfg.update(market_regime_cfg)
    return cfg
