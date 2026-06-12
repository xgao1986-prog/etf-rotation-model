"""
ETF轮动量化策略 - 全局配置
A股ETF轮动模型 v1.0 配置文件
"""

import os

# ==================== 路径配置 ====================
# BASE_DIR 指向项目根目录（config.py 的父目录的父目录）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'database')
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
SIGNAL_DIR = os.path.join(BASE_DIR, 'signals')

# 确保目录存在
for d in [DATA_DIR, REPORT_DIR, SIGNAL_DIR]:
    os.makedirs(d, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, 'etf_model.db')

# ==================== ETF标的池 ====================
# 17只行业ETF + 沪深300基准
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
    '518880.SH': '黄金ETF',  # ⭐ 新增：低相关性避险资产
}

BENCHMARK = '000300.SH'  # 沪深300

# 提取纯代码（不带后缀）用于AKShare等数据源
ETF_CODES = [code.split('.')[0] for code in ETF_UNIVERSE.keys()]
BENCHMARK_CODE = '000300'

# ==================== 板块指数配置（AKShare - 申万一级行业）====================
# 板块指数作为ETF轮动的信号增强层，不直接交易
# 数据来源: AKShare index_hist_sw 接口
# 历史长度: 1999-12-30 至今（大部分行业）

SECTOR_INDEX_UNIVERSE = {
    # 代码格式: 申万行业代码.SI (AKShare使用纯数字代码)
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

# ETF → 板块指数映射（一对一主映射）
# 用于板块动量增强评分
ETF_TO_SECTOR_MAPPING = {
    '512480.SH': '801080',   # 半导体ETF → 电子
    '515230.SH': '801750',   # 软件ETF → 计算机
    '515880.SH': '801770',   # 通信ETF → 通信
    '512010.SH': '801150',   # 医药ETF → 医药生物
    '159928.SZ': '801120',   # 消费ETF → 食品饮料（主要权重）
    '516160.SH': '801730',   # 新能源ETF → 电力设备
    '516110.SH': '801880',   # 汽车ETF → 汽车
    '512800.SH': '801780',   # 银行ETF → 银行
    '512000.SH': '801790',   # 券商ETF → 非银金融
    '512660.SH': '801740',   # 军工ETF → 国防军工
    '512980.SH': '801760',   # 传媒ETF → 传媒
    '512400.SH': '801050',   # 有色金属ETF → 有色金属
    '159996.SZ': '801110',   # 家电ETF → 家用电器
    '159865.SZ': '801010',   # 养殖ETF → 农林牧渔
    '159697.SZ': '801960',   # 油气ETF → 石油石化
    '159530.SZ': '801890',   # 机器人ETF → 机械设备
}

# 板块指数纯代码列表（用于AKShare查询）
SECTOR_CODES = list(SECTOR_INDEX_UNIVERSE.keys())

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
    'trailing_stop': None,      # 移动止损线（None=不启用，或设-0.10等）
    
    # 大盘择时
    'market_timing': True,      # 是否启用大盘择时
    'market_ma_short': 20,      # 大盘短期均线
    'market_ma_long': 50,       # 大盘长期均线
    
    # 交易费率
    'commission_rate': 0.0003,  # 佣金率0.03%
    'min_commission': 5.0,      # 最低佣金5元
    
    # 板块动量增强（v1.1新增）
    'sector_boost_enabled': True,   # 是否启用板块动量增强
    'sector_boost_weight': 15,       # 板块动量加分上限（满分15分）
    'sector_momentum_lookback': 20,  # 板块动量计算回看天数
    'sector_ma_short': 20,           # 板块短期均线
    'sector_top_n_threshold': 3,     # 板块排名前N才给加分
    'sector_above_ma_bonus': 5,      # 板块指数站上均线加分
    'sector_momentum_bonus': 10,     # 板块动量排名前N加分
}

# ==================== 可调因子配置（v1.2 新增）====================
# 这些参数被设计为"因子"，可以通过参数扫描/网格搜索/机器学习进行优化
# 与 STRATEGY_CONFIG 分离，便于独立调整和批量测试

FACTOR_CONFIG = {
    # --- 调仓频率因子 ---
    'rebalance_freq': 'weekly',      # 调仓频率: 'weekly'(每周), 'biweekly'(双周), 'monthly'(月度)
    'rebalance_weekday': 3,          # 调仓日（0=周一, 1=周二, 2=周三, 3=周四, 4=周五）
    'rebalance_ordinal': 1,          # 月度调仓时: 1=第一个, 2=第二个, -1=最后一个该星期几
    
    # --- 冷静期因子 ---
    'cooling_period': 5,             # 止损后冷却期（交易日）
    'cooling_score_boost': 10,       # 冷却期后重新买入评分门槛提升
}

# 因子搜索空间（用于网格搜索 / 随机搜索 / 贝叶斯优化）
# 每个因子定义其可调范围和步长
FACTOR_SPACE = {
    'rebalance_freq': {
        'type': 'categorical',
        'values': ['weekly', 'biweekly', 'monthly'],
        'default': 'weekly',
        'description': '调仓频率',
    },
    'rebalance_weekday': {
        'type': 'int',
        'low': 0, 'high': 4,
        'default': 3,
        'description': '调仓日（0=周一~4=周五）',
    },
    'rebalance_ordinal': {
        'type': 'int',
        'low': 1, 'high': 2,  # 月度时: 第1个或第2个该星期几; -1表示最后一个
        'default': 1,
        'description': '月度调仓时取第几个该星期几（1=第一个, 2=第二个）',
    },
    'cooling_period': {
        'type': 'int',
        'low': 0, 'high': 20,
        'default': 5,
        'description': '止损后冷却期（交易日）',
    },
    'cooling_score_boost': {
        'type': 'int',
        'low': 0, 'high': 30,
        'default': 10,
        'description': '冷却期后重新买入评分门槛提升',
    },
}

# 快速生成所有因子组合（用于全网格搜索）
def generate_factor_combinations():
    """生成因子搜索空间中的所有组合（笛卡尔积）
    
    注意: 全网格搜索组合数 = ∏(各因子取值数)
    当前5个因子约 3×5×2×21×31 = 19,530 种组合，建议用随机采样或贝叶斯优化
    """
    import itertools
    
    # 为每个因子生成取值列表
    value_lists = []
    factor_names = []
    for name, spec in FACTOR_SPACE.items():
        factor_names.append(name)
        if spec['type'] == 'categorical':
            value_lists.append(spec['values'])
        elif spec['type'] == 'int':
            value_lists.append(list(range(spec['low'], spec['high'] + 1)))
    
    combinations = []
    for values in itertools.product(*value_lists):
        combo = dict(zip(factor_names, values))
        combinations.append(combo)
    
    return combinations

# 随机采样因子组合（推荐用于大搜索空间）
def sample_factor_combinations(n_samples=100, seed=42):
    """随机采样因子组合"""
    import random
    random.seed(seed)
    
    all_combos = generate_factor_combinations()
    n_total = len(all_combos)
    
    if n_samples >= n_total:
        return all_combos
    
    return random.sample(all_combos, n_samples)

# 合并策略配置 + 因子配置（生成完整配置）
def build_config(factor_cfg=None, strategy_cfg=None):
    """合并策略配置和因子配置，生成完整配置字典
    
    优先级: factor_cfg > FACTOR_CONFIG > STRATEGY_CONFIG
    """
    import copy
    cfg = copy.deepcopy(STRATEGY_CONFIG)
    cfg.update(copy.deepcopy(FACTOR_CONFIG))
    if strategy_cfg:
        cfg.update(strategy_cfg)
    if factor_cfg:
        cfg.update(factor_cfg)
    return cfg

# ==================== 数据源配置 ====================
# 工作流:
#   1. iFinD数据: 通过Kimi对话获取（我调用get_data_source工具），
#      然后调用 import_from_kimi() 写入本地数据库
#   2. AKShare数据: 本地Python代码自动获取（免费，无需账号）
#   3. 数据合并: iFinD为主，AKShare补充缺失部分
#
# 使用方式:
#   - 首次下载: 在Kimi对话中说"用iFinD下载全部历史数据"
#   - 日常更新: 在Kimi对话中说"更新iFinD数据"
#   - AKShare兜底: python main.py update --full（本地自动运行）

DATA_SOURCE = {
    'primary': 'ifind_via_kimi',   # iFinD通过Kimi对话获取
    'backup': 'akshare',            # AKShare本地自动获取
    
    # iFinD限制（Kimi内置）
    'ifind_max_tickers_per_query': 10,   # 每次最多10个ticker
    'ifind_max_years_per_query': 3,       # 日期范围最多3年
}

# ==================== 回测参数 ====================
BACKTEST_CONFIG = {
    'start_date': '2019-06-03',
    'end_date': None,  # 默认到最新
    'initial_capital': 1_000_000,  # 初始资金100万
    
    # 样本内外分离
    'in_sample_end': '2023-12-31',   # 样本内截止
    'out_sample_start': '2024-01-01', # 样本外开始
}

# ==================== 通知配置 ====================
NOTIFY_CONFIG = {
    'email': {
        'enabled': False,
        'smtp_server': 'smtp.qq.com',
        'sender': '',
        'password': '',      # 邮箱授权码
        'receiver': '',
    },
    'wechat': {
        'enabled': False,
        'webhook_url': '',   # 企业微信机器人Webhook
    },
}

# ==================== 日志配置 ====================
LOG_CONFIG = {
    'level': 'INFO',
    'file': os.path.join(BASE_DIR, 'etf_strategy.log'),
    'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
}
