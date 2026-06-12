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
# 16只行业ETF + 沪深300基准
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
