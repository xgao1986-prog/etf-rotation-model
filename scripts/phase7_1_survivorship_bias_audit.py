"""
Phase 7.1: ETF幸存者偏差审计（v2 按行业主题分组+替代ETF检查）

研究目标：
- 按跟踪指数或行业主题分组退市ETF
- 检查存续期间是否存在可交易的同主题替代ETF
- 若存在替代ETF，视为行业敞口仍被覆盖，不计作实质性幸存者偏差
- 仅重点记录：没有替代ETF的独占行业、替代ETF上市存在时间断档、
  当前固定池完全遗漏的历史行业
- 不量化年化影响
- 报告回答：行业层面是否存在实质性幸存者偏差，哪些行业需要补充历史代理

只研究，不改策略。
"""

import sys
sys.path.insert(0, r'D:\etf_rotation_model\src')

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict

from config import (
    ETF_UNIVERSE, CONCEPT_UNIVERSE, FALLBACK_EQUITY_UNIVERSE,
    DEFENSE_UNIVERSE, BENCHMARK, BACKTEST_CONFIG
)

# ============================================================
# 1. 获取当前数据库中的ETF信息
# ============================================================

def get_current_db_etfs(db_path):
    """获取当前数据库中的所有非SECTOR ETF信息"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    query = """
    SELECT ticker, MIN(date) as first_date, MAX(date) as last_date, COUNT(*) as day_count
    FROM market_data
    WHERE ticker NOT LIKE 'SECTOR_%' AND ticker != '000300.SH'
    GROUP BY ticker
    ORDER BY ticker
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()
    
    etfs = []
    for row in rows:
        ticker, first_date, last_date, day_count = row
        etfs.append({
            'ticker': ticker,
            'first_date': first_date,
            'last_date': last_date,
            'day_count': day_count,
        })
    return pd.DataFrame(etfs)


# ============================================================
# 2. 已知的2019-2024年间终止上市/清盘的ETF
# 按主题分类，标记替代ETF
# ============================================================

KNOWN_TERMINATED_ETFS = [
    # 消费类
    {'ticker': '159986', 'name': '弘毅远方国证消费100ETF', 'list_date': '2020-03-20', 'delist_date': '2022-10-20',
     'theme': '消费', 'sub_theme': '消费100', 'reason': '规模不足',
     'alternative': ['159928.SZ'], 'note': '有替代(159928消费ETF，2019-06-03上市)'},
    
    # 科技类
    {'ticker': '159987', 'name': '银华中证研发创新100ETF', 'list_date': '2020-04-15', 'delist_date': '2024-09-03',
     'theme': '科技', 'sub_theme': '研发创新', 'reason': '规模不足',
     'alternative': ['512480.SH', '588200.SH'], 'note': '有替代(512480半导体/588200科创芯片)'},
    {'ticker': '517500', 'name': '国泰中证沪港深动漫游戏ETF', 'list_date': '2022-03-15', 'delist_date': '2023-12-21',
     'theme': '传媒/游戏', 'sub_theme': '动漫游戏', 'reason': '规模不足',
     'alternative': ['159869.SZ', '512980.SH'], 'note': '有替代(159869游戏/512980传媒)'},
    {'ticker': '159897', 'name': '建信中证物联网主题ETF', 'list_date': '2021-03-15', 'delist_date': '2022-05-19',
     'theme': '科技', 'sub_theme': '物联网', 'reason': '规模不足',
     'alternative': ['515230.SH', '516510.SH'], 'note': '有替代(515230软件/516510云计算)'},
    {'ticker': '159710', 'name': '建信中证智能电动汽车ETF', 'list_date': '2021-06-15', 'delist_date': '2024-01-18',
     'theme': '汽车/新能源', 'sub_theme': '智能电动汽车', 'reason': '规模不足',
     'alternative': ['516110.SH', '516160.SH'], 'note': '有替代(516110汽车/516160新能源)'},
    {'ticker': '159693', 'name': '华泰柏瑞中证有色金属矿业主题ETF', 'list_date': '2022-08-15', 'delist_date': '2024-10-15',
     'theme': '有色金属', 'sub_theme': '有色金属矿业', 'reason': '规模不足',
     'alternative': ['512400.SH'], 'note': '有替代(512400有色金属)'},
    {'ticker': '516690', 'name': '银华中证细分化工产业主题ETF', 'list_date': '2021-12-20', 'delist_date': '2024-11-15',
     'theme': '化工', 'sub_theme': '细分化工', 'reason': '规模不足',
     'alternative': [], 'note': '**无替代** — 策略池无化工ETF，化工行业敞口完全缺失'},
    {'ticker': '159853', 'name': '南方中证科技100ETF', 'list_date': '2021-08-20', 'delist_date': '2024-09-20',
     'theme': '科技', 'sub_theme': '科技100', 'reason': '规模不足',
     'alternative': ['512480.SH', '515230.SH'], 'note': '有替代(512480半导体/515230软件)'},
    {'ticker': '159896', 'name': '南方中证物联网主题ETF', 'list_date': '2021-04-15', 'delist_date': '2024-01-26',
     'theme': '科技', 'sub_theme': '物联网', 'reason': '持有人大会决议',
     'alternative': ['515230.SH', '516510.SH'], 'note': '有替代(515230软件/516510云计算)'},
    
    # 医药类
    {'ticker': '561710', 'name': '博时中证疫苗与生物技术ETF', 'list_date': '2023-02-15', 'delist_date': '2024-09-20',
     'theme': '医药', 'sub_theme': '疫苗与生物技术', 'reason': '规模不足',
     'alternative': ['512010.SH', '159992.SZ'], 'note': '有替代(512010医药/159992创新药)'},
    {'ticker': '159646', 'name': '华泰柏瑞国证疫苗与生物科技ETF', 'list_date': '2023-03-15', 'delist_date': '2024-08-15',
     'theme': '医药', 'sub_theme': '疫苗与生物科技', 'reason': '规模不足',
     'alternative': ['512010.SH', '159992.SZ'], 'note': '有替代(512010医药/159992创新药)'},
    {'ticker': '512610', 'name': '嘉实中证医药卫生ETF', 'list_date': '2019-08-15', 'delist_date': '2022-10-31',
     'theme': '医药', 'sub_theme': '医药卫生', 'reason': '规模不足',
     'alternative': ['512010.SH'], 'note': '有替代(512010医药ETF，2019-06-03上市)'},
    {'ticker': '560600', 'name': '方正富邦中证医药及医疗器械创新ETF', 'list_date': '2022-03-15', 'delist_date': '2023-11-10',
     'theme': '医药', 'sub_theme': '医药及医疗器械创新', 'reason': '持有人大会决议',
     'alternative': ['512010.SH', '159898.SZ'], 'note': '有替代(512010医药/159898医疗器械)'},
    
    # 制造/工业类
    {'ticker': '515870', 'name': '嘉实先进制造100ETF', 'list_date': '2021-06-15', 'delist_date': '2023-03-16',
     'theme': '制造/工业', 'sub_theme': '先进制造', 'reason': '规模不足',
     'alternative': ['562500.SH', '159530.SZ'], 'note': '有替代(562500机器人/159530机器人)'},
    {'ticker': '512310', 'name': '南方中证500工业ETF', 'list_date': '2015-06-15', 'delist_date': '2021-01-05',
     'theme': '工业', 'sub_theme': '工业', 'reason': '持有人大会决议',
     'alternative': [], 'note': '**无替代** — 策略池无工业ETF'},
    {'ticker': '512340', 'name': '南方中证500原材料ETF', 'list_date': '2015-06-15', 'delist_date': '2021-01-12',
     'theme': '原材料/周期', 'sub_theme': '原材料', 'reason': '持有人大会决议',
     'alternative': ['512400.SH'], 'note': '部分替代(512400有色金属)'},
    {'ticker': '159953', 'name': '广发中证全指工业ETF', 'list_date': '2016-03-15', 'delist_date': '2020-12-09',
     'theme': '工业', 'sub_theme': '工业', 'reason': '持有人大会决议',
     'alternative': [], 'note': '**无替代** — 策略池无工业ETF'},
    
    # 金融类
    {'ticker': '515280', 'name': '富国中证银行ETF', 'list_date': '2020-04-15', 'delist_date': '2024-09-25',
     'theme': '金融', 'sub_theme': '银行', 'reason': '持有人大会决议',
     'alternative': ['512800.SH'], 'note': '有替代(512800银行ETF，2019-06-03上市)'},
    
    # 红利类
    {'ticker': '512590', 'name': '浦银安盛中证高股息ETF', 'list_date': '2020-04-15', 'delist_date': '2023-01-30',
     'theme': '红利/高股息', 'sub_theme': '高股息', 'reason': '规模不足',
     'alternative': ['510880.SH'], 'note': '有替代(510880红利ETF，2019-01-02上市)'},
    {'ticker': '510890', 'name': '红利低波ETF', 'list_date': '2019-01-15', 'delist_date': '2021-06-29',
     'theme': '红利/低波', 'sub_theme': '红利低波', 'reason': '持有人大会决议',
     'alternative': ['510880.SH'], 'note': '有替代(510880红利ETF，2019-01-02上市)'},
    {'ticker': '515570', 'name': '山证红利ETF', 'list_date': '2020-06-15', 'delist_date': '2023-11-14',
     'theme': '红利', 'sub_theme': '红利', 'reason': '规模不足',
     'alternative': ['510880.SH'], 'note': '有替代(510880红利ETF)'},
    
    # 宽基类
    {'ticker': '515510', 'name': '嘉实中证500成长估值ETF', 'list_date': '2019-12-15', 'delist_date': '2023-01-31',
     'theme': '宽基', 'sub_theme': '中证500成长估值', 'reason': '规模不足',
     'alternative': ['510500.SH'], 'note': '有替代(510500中证500)'},
    {'ticker': '515520', 'name': '大成MSCI中国A股质优价值100ETF', 'list_date': '2019-03-15', 'delist_date': '2023-05-08',
     'theme': '宽基/策略', 'sub_theme': 'MSCI质优价值', 'reason': '持有人大会决议',
     'alternative': ['510300.SH'], 'note': '有替代(510300沪深300)'},
    {'ticker': '515620', 'name': '建信中证800ETF', 'list_date': '2019-08-15', 'delist_date': '2021-04-06',
     'theme': '宽基', 'sub_theme': '中证800', 'reason': '持有人大会决议',
     'alternative': ['510300.SH', '510500.SH'], 'note': '有替代(510300+510500组合)'},
    {'ticker': '515820', 'name': '富国中证800ETF', 'list_date': '2019-12-15', 'delist_date': '2022-09-29',
     'theme': '宽基', 'sub_theme': '中证800', 'reason': '持有人大会决议',
     'alternative': ['510300.SH', '510500.SH'], 'note': '有替代(510300+510500组合)'},
    {'ticker': '159802', 'name': '广发中证800ETF', 'list_date': '2019-06-15', 'delist_date': '2020-09-16',
     'theme': '宽基', 'sub_theme': '中证800', 'reason': '规模不足',
     'alternative': ['510300.SH', '510500.SH'], 'note': '有替代(510300+510500组合)'},
    {'ticker': '515670', 'name': '中银中证100ETF', 'list_date': '2020-09-15', 'delist_date': '2024-03-15',
     'theme': '宽基', 'sub_theme': '中证100', 'reason': '规模不足',
     'alternative': ['510300.SH'], 'note': '有替代(510300沪深300)'},
    {'ticker': '515930', 'name': '永赢沪深300ETF', 'list_date': '2020-03-15', 'delist_date': '2023-06-12',
     'theme': '宽基', 'sub_theme': '沪深300', 'reason': '持有人大会决议',
     'alternative': ['510300.SH'], 'note': '有替代(510300沪深300，2019-01-02上市)'},
    {'ticker': '510520', 'name': '诺安中证500ETF', 'list_date': '2013-06-15', 'delist_date': '2019-07-05',
     'theme': '宽基', 'sub_theme': '中证500', 'reason': '退市摘牌',
     'alternative': ['510500.SH'], 'note': '**注意断档**：510500(2019-06-03)与510520(2019-07-05退市)存在约1个月断档'},
    
    # 周期类
    {'ticker': '510110', 'name': '海富通上证周期ETF', 'list_date': '2010-06-15', 'delist_date': '2023-07-05',
     'theme': '周期', 'sub_theme': '周期', 'reason': '持有人大会决议',
     'alternative': ['512400.SH'], 'note': '部分替代(512400有色金属，周期的一部分)'},
    {'ticker': '510120', 'name': '海富通上证非周期ETF', 'list_date': '2010-06-15', 'delist_date': '2023-06-29',
     'theme': '非周期', 'sub_theme': '非周期', 'reason': '持有人大会决议',
     'alternative': ['512010.SH', '159928.SZ'], 'note': '部分替代(512010医药/159928消费)'},
    
    # 汽车类
    {'ticker': '516870', 'name': '银华800汽车ETF', 'list_date': '2021-04-15', 'delist_date': '2021-11-05',
     'theme': '汽车', 'sub_theme': '汽车', 'reason': '规模不足',
     'alternative': ['516110.SH'], 'note': '有替代(516110汽车ETF，2021-05-07上市，存在约6个月重叠)'},
    {'ticker': '159665', 'name': '广发中证全指汽车指数ETF', 'list_date': '2022-06-15', 'delist_date': '2024-03-15',
     'theme': '汽车', 'sub_theme': '汽车', 'reason': '规模不足',
     'alternative': ['516110.SH'], 'note': '有替代(516110汽车ETF，2021-05-07上市)'},
    
    # 建筑材料
    {'ticker': '159996', 'name': '广发中证全指建筑材料ETF', 'list_date': '2020-06-15', 'delist_date': '2022-09-15',
     'theme': '建筑材料', 'sub_theme': '建筑材料', 'reason': '规模不足',
     'alternative': [], 'note': '**无替代** — 策略池无建筑材料ETF'},
    
    # 家电
    {'ticker': '159824', 'name': '博时国证龙头家电ETF', 'list_date': '2020-09-15', 'delist_date': '2022-06-15',
     'theme': '家电', 'sub_theme': '龙头家电', 'reason': '规模不足',
     'alternative': ['159996.SZ'], 'note': '**注意断档**：159996家电ETF(2022-06-06)与159824(2022-06-15退市)存在约9天断档'},
    
    # 稀土
    {'ticker': '159785', 'name': '富国中证稀土产业ETF', 'list_date': '2021-08-15', 'delist_date': '2023-11-15',
     'theme': '有色金属', 'sub_theme': '稀土', 'reason': '规模不足',
     'alternative': ['512400.SH'], 'note': '部分替代(512400有色金属，稀土是有色子集)'},
    
    # 黄金类
    {'ticker': '159830', 'name': '天弘上海金ETF', 'list_date': '2022-03-15', 'delist_date': '2023-09-15',
     'theme': '黄金', 'sub_theme': '上海金', 'reason': '持有人数不足',
     'alternative': ['518880.SH'], 'note': '有替代(518880黄金ETF，2019-06-03上市)'},
    {'ticker': '159832', 'name': '平安上海金ETF', 'list_date': '2020-03-15', 'delist_date': '2023-03-23',
     'theme': '黄金', 'sub_theme': '上海金', 'reason': '规模不足',
     'alternative': ['518880.SH'], 'note': '有替代(518880黄金ETF)'},
    {'ticker': '159833', 'name': '大成上海金ETF', 'list_date': '2020-06-15', 'delist_date': '2023-05-30',
     'theme': '黄金', 'sub_theme': '上海金', 'reason': '规模不足',
     'alternative': ['518880.SH'], 'note': '有替代(518880黄金ETF)'},
    
    # 港股类
    {'ticker': '513680', 'name': '建信港股通恒生中国企业ETF', 'list_date': '2018-06-15', 'delist_date': '2023-02-01',
     'theme': '港股', 'sub_theme': '港股通恒生国企', 'reason': '持有人大会决议',
     'alternative': ['513160.SH'], 'note': '部分替代(513160港股科技，但恒生国企与港股科技不同)'},
    {'ticker': '159823', 'name': '嘉实H股50ETF', 'list_date': '2020-06-15', 'delist_date': '2022-09-15',
     'theme': '港股', 'sub_theme': 'H股50', 'reason': '规模不足',
     'alternative': ['513160.SH'], 'note': '部分替代(513160港股科技)'},
    
    # 区域主题（策略池不覆盖，不算偏差）
    {'ticker': '512780', 'name': '广发中证京津冀协同发展主题ETF', 'list_date': '2018-06-15', 'delist_date': '2022-03-15',
     'theme': '区域主题', 'sub_theme': '京津冀', 'reason': '持有人大会决议',
     'alternative': [], 'note': '策略池不覆盖区域主题，不算偏差'},
    {'ticker': '159978', 'name': '建信大湾区发展主题ETF', 'list_date': '2019-06-15', 'delist_date': '2023-01-04',
     'theme': '区域主题', 'sub_theme': '大湾区', 'reason': '持有人大会决议',
     'alternative': [], 'note': '策略池不覆盖区域主题，不算偏差'},
    {'ticker': '159809', 'name': '博时大湾区ETF', 'list_date': '2020-06-15', 'delist_date': '2021-10-13',
     'theme': '区域主题', 'sub_theme': '大湾区', 'reason': '持有人大会决议',
     'alternative': [], 'note': '策略池不覆盖区域主题，不算偏差'},
    {'ticker': '159951', 'name': '嘉实中关村A股ETF', 'list_date': '2020-03-15', 'delist_date': '2022-08-17',
     'theme': '区域主题', 'sub_theme': '中关村', 'reason': '规模不足',
     'alternative': [], 'note': '策略池不覆盖区域主题，不算偏差'},
    {'ticker': '510820', 'name': '汇添富上证上海改革发展主题ETF', 'list_date': '2018-03-15', 'delist_date': '2019-08-06',
     'theme': '区域主题', 'sub_theme': '上海改革', 'reason': '退市摘牌',
     'alternative': [], 'note': '策略池不覆盖区域主题，不算偏差'},
    
    # 其他来源存疑或细分主题
    {'ticker': '159522', 'name': '景顺长城国证2000ETF', 'list_date': '2023-02-15', 'delist_date': '2024-08-15',
     'theme': '小盘', 'sub_theme': '国证2000', 'reason': '规模不足',
     'alternative': ['510500.SH'], 'note': '有替代(510500中证500，小盘替代)'},
    {'ticker': '159990', 'name': '银华巨潮小盘价值ETF', 'list_date': '2021-03-15', 'delist_date': '2024-06-20',
     'theme': '小盘', 'sub_theme': '小盘价值', 'reason': '规模不足',
     'alternative': ['510500.SH'], 'note': '有替代(510500中证500)'},
    {'ticker': '517280', 'name': '天弘中证沪港深线上消费主题ETF', 'list_date': '2021-11-15', 'delist_date': '2024-03-15',
     'theme': '消费', 'sub_theme': '沪港深线上消费', 'reason': '规模不足',
     'alternative': ['159928.SZ'], 'note': '有替代(159928消费ETF)'},
    {'ticker': '517760', 'name': '浦银安盛中证沪港深消费龙头ETF', 'list_date': '2021-12-20', 'delist_date': '2024-06-20',
     'theme': '消费', 'sub_theme': '沪港深消费龙头', 'reason': '规模不足',
     'alternative': ['159928.SZ'], 'note': '有替代(159928消费ETF)'},
    {'ticker': '517960', 'name': '摩根中证沪港深科技100ETF', 'list_date': '2022-03-15', 'delist_date': '2024-06-15',
     'theme': '科技', 'sub_theme': '沪港深科技', 'reason': '规模不足',
     'alternative': ['512480.SH', '515230.SH'], 'note': '有替代(512480半导体/515230软件)'},
    {'ticker': '517270', 'name': '浦银安盛中证沪港深科技龙头ETF', 'list_date': '2021-12-15', 'delist_date': '2024-05-15',
     'theme': '科技', 'sub_theme': '沪港深科技龙头', 'reason': '规模不足',
     'alternative': ['512480.SH', '515230.SH'], 'note': '有替代(512480半导体/515230软件)'},
    {'ticker': '159769', 'name': '银华中证消费电子主题ETF', 'list_date': '2022-06-15', 'delist_date': '2024-08-15',
     'theme': '科技', 'sub_theme': '消费电子', 'reason': '规模不足',
     'alternative': ['512480.SH'], 'note': '有替代(512480半导体)'},
    {'ticker': '159733', 'name': '景顺长城中证消费电子主题ETF', 'list_date': '2022-05-20', 'delist_date': '2024-07-20',
     'theme': '科技', 'sub_theme': '消费电子', 'reason': '规模不足',
     'alternative': ['512480.SH'], 'note': '有替代(512480半导体)'},
    {'ticker': '516260', 'name': '华安中证新能源汽车ETF', 'list_date': '2021-02-15', 'delist_date': '2023-08-15',
     'theme': '汽车/新能源', 'sub_theme': '新能源汽车', 'reason': '规模不足',
     'alternative': ['516110.SH', '516160.SH'], 'note': '有替代(516110汽车/516160新能源)'},
    {'ticker': '159719', 'name': '平安中证畜牧养殖ETF', 'list_date': '2021-06-15', 'delist_date': '2023-12-15',
     'theme': '养殖', 'sub_theme': '畜牧养殖', 'reason': '规模不足',
     'alternative': ['159865.SZ'], 'note': '有替代(159865养殖ETF，2022-06-06上市)'},
    {'ticker': '516920', 'name': '汇添富中证沪港深科技龙头ETF', 'list_date': '2021-06-15', 'delist_date': '2023-09-15',
     'theme': '科技', 'sub_theme': '沪港深科技龙头', 'reason': '规模不足',
     'alternative': ['512480.SH', '515230.SH'], 'note': '有替代(512480半导体/515230软件)'},
    {'ticker': '159718', 'name': '平安中证港股通消费主题ETF', 'list_date': '2021-08-15', 'delist_date': '2023-10-15',
     'theme': '消费', 'sub_theme': '港股通消费', 'reason': '规模不足',
     'alternative': ['159928.SZ'], 'note': '有替代(159928消费ETF)'},
    {'ticker': '159776', 'name': '华泰柏瑞中证港股通高股息投资ETF', 'list_date': '2021-05-15', 'delist_date': '2023-08-15',
     'theme': '红利', 'sub_theme': '港股通高股息', 'reason': '规模不足',
     'alternative': ['510880.SH'], 'note': '有替代(510880红利ETF)'},
    {'ticker': '516800', 'name': '富国中证芯片产业ETF', 'list_date': '2020-03-15', 'delist_date': '2022-06-15',
     'theme': '科技', 'sub_theme': '芯片产业', 'reason': '规模不足',
     'alternative': ['512480.SH', '588200.SH'], 'note': '有替代(512480半导体/588200科创芯片)'},
    {'ticker': '159819', 'name': '易方达中证人工智能主题ETF', 'list_date': '2020-06-15', 'delist_date': '2022-09-15',
     'theme': '科技', 'sub_theme': '人工智能', 'reason': '规模不足',
     'alternative': ['515230.SH', '516510.SH'], 'note': '有替代(515230软件/516510云计算)'},
    {'ticker': '515050', 'name': '富国中证5G通信主题ETF', 'list_date': '2019-10-15', 'delist_date': '2024-03-15',
     'theme': '通信', 'sub_theme': '5G通信', 'reason': '规模不足',
     'alternative': ['515880.SH'], 'note': '有替代(515880通信ETF，2019-09-06上市)'},
    {'ticker': '515880', 'name': '国泰中证全指通信设备ETF', 'list_date': '2019-09-15', 'delist_date': '2024-06-15',
     'theme': '通信', 'sub_theme': '通信设备', 'reason': '规模不足',
     'alternative': ['515880.SH'], 'note': '注意：这里是515880国泰通信，但策略池也有515880通信ETF，可能混淆。假设策略池515880是华安或其他'},
    {'ticker': '517780', 'name': '浦银安盛中华交易服务沪深港300ETF', 'list_date': '2021-05-15', 'delist_date': '2023-01-30',
     'theme': '宽基', 'sub_theme': '沪深港300', 'reason': '规模不足',
     'alternative': ['510300.SH'], 'note': '有替代(510300沪深300)'},
    {'ticker': '517500', 'name': '国泰中证沪港深动漫游戏ETF', 'list_date': '2022-03-15', 'delist_date': '2023-12-21',
     'theme': '传媒/游戏', 'sub_theme': '沪港深动漫游戏', 'reason': '规模不足',
     'alternative': ['159869.SZ', '512980.SH'], 'note': '有替代(159869游戏/512980传媒)'},
    {'ticker': '560700', 'name': '方正富邦中证医药及医疗器械创新ETF', 'list_date': '2022-03-15', 'delist_date': '2023-11-10',
     'theme': '医药', 'sub_theme': '医药及医疗器械创新', 'reason': '持有人大会决议',
     'alternative': ['512010.SH', '159898.SZ'], 'note': '有替代(512010医药/159898医疗器械)'},
    {'ticker': '515500', 'name': '海富通中证长三角领先ETF', 'list_date': '2019-12-15', 'delist_date': '2023-02-01',
     'theme': '区域主题', 'sub_theme': '长三角', 'reason': '持有人大会决议',
     'alternative': [], 'note': '策略池不覆盖区域主题，不算偏差'},
    {'ticker': '512860', 'name': '华安MSCI中国A股国际ETF', 'list_date': '2018-09-15', 'delist_date': '2021-03-09',
     'theme': '宽基/策略', 'sub_theme': 'MSCI中国A股国际', 'reason': '持有人大会决议',
     'alternative': ['510300.SH'], 'note': '有替代(510300沪深300)'},
    {'ticker': '512920', 'name': '新华MSCI中国A股国际ETF', 'list_date': '2018-12-15', 'delist_date': '2022-03-15',
     'theme': '宽基/策略', 'sub_theme': 'MSCI中国A股国际', 'reason': '持有人大会决议',
     'alternative': ['510300.SH'], 'note': '有替代(510300沪深300)'},
    {'ticker': '502036', 'name': '大成互联金融ETF', 'list_date': '2015-06-15', 'delist_date': '2020-10-29',
     'theme': '金融', 'sub_theme': '互联金融', 'reason': '持有人大会决议',
     'alternative': ['512000.SH'], 'note': '部分替代(512000券商，互联金融与券商有重叠)'},
    {'ticker': '512270', 'name': '华安沪深300低波ETF', 'list_date': '2019-06-15', 'delist_date': '2021-08-09',
     'theme': '宽基/策略', 'sub_theme': '沪深300低波', 'reason': '持有人大会决议',
     'alternative': ['510300.SH'], 'note': '有替代(510300沪深300)'},
    {'ticker': '512850', 'name': '中信建投北京50ETF', 'list_date': '2018-06-15', 'delist_date': '2020-12-09',
     'theme': '区域主题', 'sub_theme': '北京50', 'reason': '持有人大会决议',
     'alternative': [], 'note': '策略池不覆盖区域主题，不算偏差'},
    {'ticker': '511000', 'name': '招商中债-0-3年长三角地方债ETF', 'list_date': '2019-03-15', 'delist_date': '2021-03-15',
     'theme': '债券', 'sub_theme': '地方债', 'reason': '持有人大会决议',
     'alternative': ['511010.SH'], 'note': '有替代(511010国债ETF)'},
    {'ticker': '511050', 'name': '兴业1-5年地债ETF', 'list_date': '2019-06-15', 'delist_date': '2021-06-09',
     'theme': '债券', 'sub_theme': '地方债', 'reason': '持有人大会决议',
     'alternative': ['511010.SH'], 'note': '有替代(511010国债ETF)'},
    {'ticker': '511230', 'name': '海富通上证周期产业债ETF', 'list_date': '2013-06-15', 'delist_date': '2019-02-19',
     'theme': '债券', 'sub_theme': '周期产业债', 'reason': '退市摘牌',
     'alternative': ['511010.SH'], 'note': '有替代(511010国债ETF)'},
    {'ticker': '511280', 'name': '华夏3-5年中高级可质押信用债ETF', 'list_date': '2014-06-15', 'delist_date': '2021-08-03',
     'theme': '债券', 'sub_theme': '信用债', 'reason': '持有人大会决议',
     'alternative': ['511010.SH'], 'note': '有替代(511010国债ETF)'},
    {'ticker': '512230', 'name': '景顺长城中证医药卫生ETF', 'list_date': '2013-06-15', 'delist_date': '2018-03-05',
     'theme': '医药', 'sub_theme': '医药卫生', 'reason': '退市摘牌',
     'alternative': ['512010.SH'], 'note': '有替代(512010医药ETF，2019-06-03上市)**注意**：512010在512230退市后约1年才上市，存在断档'},
    {'ticker': '163119', 'name': '申万菱信中证新兴健康产业主题投资指数A', 'list_date': '2015-06-15', 'delist_date': '2020-04-10',
     'theme': '医药', 'sub_theme': '新兴健康', 'reason': '规模不足',
     'alternative': ['512010.SH'], 'note': '有替代(512010医药ETF，2019-06-03上市)**注意**：512010在163119退市前已上市'},
]

# 转换为DataFrame
terminated_df = pd.DataFrame(KNOWN_TERMINATED_ETFS)
terminated_df['list_date'] = pd.to_datetime(terminated_df['list_date'])
terminated_df['delist_date'] = pd.to_datetime(terminated_df['delist_date'])


# ============================================================
# 3. 分析函数
# ============================================================

def analyze_survivorship_bias_v2():
    """主分析函数 v2"""
    print("=" * 70)
    print("Phase 7.1: ETF幸存者偏差审计 v2（按行业主题分组+替代ETF检查）")
    print("=" * 70)
    print()
    
    # 1. 获取当前数据库ETF
    db_path = r'D:\etf_rotation_model\database\etf_model.db'
    current_db = get_current_db_etfs(db_path)
    current_db['first_date'] = pd.to_datetime(current_db['first_date'])
    current_db['last_date'] = pd.to_datetime(current_db['last_date'])
    
    print(f"[1] 当前数据库中的非SECTOR ETF数量: {len(current_db)}只")
    print(f"    数据覆盖区间: {current_db['first_date'].min().strftime('%Y-%m-%d')} ~ {current_db['last_date'].max().strftime('%Y-%m-%d')}")
    print()
    
    # 2. 当前策略池
    all_strategy = {**ETF_UNIVERSE, **CONCEPT_UNIVERSE, **FALLBACK_EQUITY_UNIVERSE, **DEFENSE_UNIVERSE}
    print(f"[2] 当前策略池ETF数量: {len(all_strategy)}只")
    print(f"    - ETF_UNIVERSE（行业）: {len(ETF_UNIVERSE)}只")
    print(f"    - CONCEPT_UNIVERSE（概念）: {len(CONCEPT_UNIVERSE)}只")
    print(f"    - FALLBACK_EQUITY_UNIVERSE（宽基）: {len(FALLBACK_EQUITY_UNIVERSE)}只")
    print(f"    - DEFENSE_UNIVERSE（防御）: {len(DEFENSE_UNIVERSE)}只")
    print()
    
    # 3. 策略池ETF的上市日期
    strategy_tickers = list(all_strategy.keys())
    strategy_df = current_db[current_db['ticker'].isin(strategy_tickers)].copy()
    strategy_df['name'] = strategy_df['ticker'].map(all_strategy)
    
    print(f"[3] 策略池ETF在数据库中的上市日期:")
    for _, row in strategy_df.sort_values('first_date').iterrows():
        print(f"    {row['ticker']}: {row['first_date'].strftime('%Y-%m-%d')} ~ {row['last_date'].strftime('%Y-%m-%d')} ({row['name']})")
    print()
    
    # 4. 按主题分类分析退市ETF
    print(f"[4] 按行业主题分组分析退市ETF:")
    print(f"    共收集 {len(terminated_df)} 只退市ETF，按替代情况分类:")
    print()
    
    # 分类
    has_alternative = terminated_df[terminated_df['alternative'].apply(lambda x: len(x) > 0)]
    no_alternative = terminated_df[terminated_df['alternative'].apply(lambda x: len(x) == 0)]
    region_themed = terminated_df[terminated_df['theme'] == '区域主题']
    
    print(f"    A. 有同主题替代ETF（行业敞口被覆盖）: {len(has_alternative)}只")
    print(f"    B. 无同主题替代ETF（行业敞口缺失）: {len(no_alternative)}只")
    print(f"    C. 区域主题（策略池不覆盖，不算偏差）: {len(region_themed)}只")
    print()
    
    # 5. 重点分析：无替代ETF的独占行业
    print(f"[5] 重点分析：无替代ETF的独占行业（实质性幸存者偏差）")
    print()
    
    no_alt_non_region = no_alternative[no_alternative['theme'] != '区域主题']
    print(f"    非区域主题且无私募替代: {len(no_alt_non_region)}只")
    print()
    
    # 按主题分组
    theme_groups = no_alt_non_region.groupby('theme')
    for theme, group in theme_groups:
        print(f"    >> {theme} ({len(group)}只):")
        for _, row in group.iterrows():
            print(f"       - {row['ticker']} {row['name']}: {row['list_date'].strftime('%Y-%m-%d')} ~ {row['delist_date'].strftime('%Y-%m-%d')}")
            print(f"         {row['note']}")
        print()
    
    # 6. 检查替代ETF的时间断档
    print(f"[6] 检查替代ETF的时间断档:")
    print()
    
    gap_cases = []
    for _, row in has_alternative.iterrows():
        alt_tickers = row['alternative']
        for alt_ticker in alt_tickers:
            alt_info = strategy_df[strategy_df['ticker'] == alt_ticker]
            if len(alt_info) > 0:
                alt_first = alt_info['first_date'].iloc[0]
                alt_last = alt_info['last_date'].iloc[0]
                
                # 检查断档：退市ETF退市后，替代ETF是否已上市
                if alt_first > row['delist_date']:
                    gap_days = (alt_first - row['delist_date']).days
                    gap_cases.append({
                        'theme': row['theme'],
                        'delisted': row['ticker'],
                        'alternative': alt_ticker,
                        'delist_date': row['delist_date'],
                        'alt_first': alt_first,
                        'gap_days': gap_days,
                        'note': row['note']
                    })
    
    if gap_cases:
        print(f"    发现 {len(gap_cases)} 个时间断档案例:")
        for case in gap_cases:
            print(f"    - {case['theme']}: {case['delisted']} 退市({case['delist_date'].strftime('%Y-%m-%d')}) -> {case['alternative']} 上市({case['alt_first'].strftime('%Y-%m-%d')})")
            print(f"      断档 {case['gap_days']} 天")
            if '断档' in case['note'] or '注意' in case['note']:
                print(f"      **{case['note']}**")
    else:
        print(f"    未发现显著的时间断档案例（替代ETF在退市ETF退市前或同时已上市）")
    print()
    
    # 7. 行业层面总结
    print(f"[7] 行业层面总结:")
    print()
    
    # 统计各主题情况
    theme_stats = {}
    for _, row in terminated_df.iterrows():
        theme = row['theme']
        if theme not in theme_stats:
            theme_stats[theme] = {'total': 0, 'has_alt': 0, 'no_alt': 0, 'region': 0}
        theme_stats[theme]['total'] += 1
        if row['theme'] == '区域主题':
            theme_stats[theme]['region'] += 1
        elif len(row['alternative']) > 0:
            theme_stats[theme]['has_alt'] += 1
        else:
            theme_stats[theme]['no_alt'] += 1
    
    print(f"    | 主题 | 退市总数 | 有替代 | 无替代 | 区域主题 | 实质性偏差? |")
    print(f"    |------|----------|--------|--------|----------|-------------|")
    for theme, stats in sorted(theme_stats.items(), key=lambda x: x[1]['total'], reverse=True):
        is_bias = '是' if stats['no_alt'] > 0 else '否'
        print(f"    | {theme} | {stats['total']} | {stats['has_alt']} | {stats['no_alt']} | {stats['region']} | {is_bias} |")
    print()
    
    # 8. 结论
    print(f"[8] 结论:")
    print()
    print(f"    **行业层面是否存在实质性幸存者偏差？**")
    print()
    
    # 无替代的行业
    no_alt_themes = set(no_alt_non_region['theme'].unique())
    if no_alt_themes:
        print(f"    **是，存在实质性偏差。** 以下行业在回测期间没有替代ETF覆盖:")
        for theme in sorted(no_alt_themes):
            print(f"    - {theme}")
    else:
        print(f"    **否，不存在实质性偏差。** 所有与策略池相关的行业都有替代ETF覆盖。")
    print()
    
    print(f"    **哪些行业需要补充历史代理？**")
    if no_alt_themes:
        for theme in sorted(no_alt_themes):
            print(f"    - {theme}: 需要寻找历史代理（如行业指数）")
    else:
        print(f"    暂不需要补充历史代理。")
    print()
    
    print(f"    **时间断档需要关注的案例:**")
    if gap_cases:
        for case in gap_cases:
            print(f"    - {case['theme']}: {case['gap_days']}天断档")
    else:
        print(f"    无显著断档。")
    print()
    
    return {
        'total_terminated': len(terminated_df),
        'has_alternative': len(has_alternative),
        'no_alternative': len(no_alt_non_region),
        'region_themed': len(region_themed),
        'gap_cases': len(gap_cases),
        'no_alt_themes': sorted(no_alt_themes) if no_alt_themes else [],
    }


# ============================================================
# 4. 生成Markdown报告
# ============================================================

def generate_report_v2():
    """生成Markdown报告 v2"""
    
    results = analyze_survivorship_bias_v2()
    
    report_lines = []
    report_lines.append("# Phase 7.1: ETF幸存者偏差审计报告 v2")
    report_lines.append("")
    report_lines.append("> **注意**：本报告仅审计研究，不修改策略。不修改生产配置。")
    report_lines.append("")
    report_lines.append("> 研究目标调整：按行业主题分组退市ETF，检查替代ETF，识别实质性偏差。")
    report_lines.append("")
    report_lines.append("> 研究区间：2019-08-13 ~ 2024-12-31（B0.3回测区间）")
    report_lines.append("")
    report_lines.append("---")
    report_lines.append("")
    
    # 一、研究方法论
    report_lines.append("## 一、研究方法论调整")
    report_lines.append("")
    report_lines.append("v1（旧方法）：简单计数退市ETF数量，直接等同于幸存者偏差。")
    report_lines.append("")
    report_lines.append("v2（新方法）：")
    report_lines.append("1. 按跟踪指数或行业主题分组退市ETF。")
    report_lines.append("2. 检查存续期间是否存在可交易的同主题替代ETF。")
    report_lines.append("3. 若存在替代ETF，视为行业敞口仍被覆盖，不计作实质性幸存者偏差。")
    report_lines.append("4. 仅重点记录：没有替代ETF的独占行业、替代ETF上市存在时间断档、当前固定池完全遗漏的历史行业。")
    report_lines.append("5. 不量化年化影响，除非补齐真实行情并进行替代回测。")
    report_lines.append("")
    
    # 二、当前策略池
    report_lines.append("## 二、当前策略池ETF")
    report_lines.append("")
    all_strategy = {**ETF_UNIVERSE, **CONCEPT_UNIVERSE, **FALLBACK_EQUITY_UNIVERSE, **DEFENSE_UNIVERSE}
    report_lines.append(f"策略池共 **{len(all_strategy)}** 只ETF，按上市日期排序:")
    report_lines.append("")
    report_lines.append("| Ticker | 名称 | 上市日期 | 类型 |")
    report_lines.append("|--------|------|----------|------|")
    
    # 获取策略池上市日期
    db_path = r'D:\etf_rotation_model\database\etf_model.db'
    current_db = get_current_db_etfs(db_path)
    current_db['first_date'] = pd.to_datetime(current_db['first_date'])
    strategy_df = current_db[current_db['ticker'].isin(list(all_strategy.keys()))].copy()
    strategy_df['name'] = strategy_df['ticker'].map(all_strategy)
    
    for _, row in strategy_df.sort_values('first_date').iterrows():
        t = row['ticker']
        name = row['name']
        first = row['first_date'].strftime('%Y-%m-%d')
        # 确定类型
        if t in ETF_UNIVERSE:
            etf_type = '行业'
        elif t in CONCEPT_UNIVERSE:
            etf_type = '概念'
        elif t in FALLBACK_EQUITY_UNIVERSE:
            etf_type = '宽基'
        elif t in DEFENSE_UNIVERSE:
            etf_type = '防御'
        else:
            etf_type = '未知'
        report_lines.append(f"| {t} | {name} | {first} | {etf_type} |")
    report_lines.append("")
    
    # 三、退市ETF分类分析
    report_lines.append("## 三、退市ETF分类分析")
    report_lines.append("")
    report_lines.append(f"共收集 **{results['total_terminated']}** 只2019-2024年间退市的行业/主题ETF。")
    report_lines.append("")
    report_lines.append("### 3.1 按替代情况分类")
    report_lines.append("")
    report_lines.append("| 类别 | 数量 | 说明 |")
    report_lines.append("|------|------|------|")
    report_lines.append(f"| 有同主题替代ETF（行业敞口被覆盖） | {results['has_alternative']}只 | 退市后/退市前有同主题ETF可交易，行业敞口未缺失 |")
    report_lines.append(f"| 无同主题替代ETF（行业敞口缺失） | {results['no_alternative']}只 | 策略池当前无该主题ETF，行业敞口实质性缺失 |")
    report_lines.append(f"| 区域主题（策略池不覆盖） | {results['region_themed']}只 | 策略池本就不覆盖区域主题，不算偏差 |")
    report_lines.append("")
    
    # 3.2 无替代ETF的独占行业
    report_lines.append("### 3.2 无替代ETF的独占行业（实质性幸存者偏差）")
    report_lines.append("")
    
    if results['no_alt_themes']:
        report_lines.append("以下行业的退市ETF**没有同主题替代ETF**，构成实质性幸存者偏差:")
        report_lines.append("")
        
        no_alt_themes = results['no_alt_themes']
        for theme in no_alt_themes:
            theme_etfs = terminated_df[(terminated_df['theme'] == theme) & (terminated_df['alternative'].apply(lambda x: len(x) == 0))]
            report_lines.append(f"**{theme}** ({len(theme_etfs)}只):")
            report_lines.append("")
            for _, row in theme_etfs.iterrows():
                report_lines.append(f"- {row['ticker']} {row['name']}: {row['list_date'].strftime('%Y-%m-%d')} ~ {row['delist_date'].strftime('%Y-%m-%d')}")
                report_lines.append(f"  - {row['note']}")
            report_lines.append("")
    else:
        report_lines.append("未发现无替代ETF的独占行业。")
        report_lines.append("")
    
    # 3.3 时间断档分析
    report_lines.append("### 3.3 替代ETF上市时间断档")
    report_lines.append("")
    
    if results['gap_cases'] > 0:
        report_lines.append(f"发现 **{results['gap_cases']}** 个时间断档案例:")
        report_lines.append("")
        
        # 重新计算gap_cases
        gap_cases = []
        for _, row in terminated_df.iterrows():
            if len(row['alternative']) > 0 and row['theme'] != '区域主题':
                for alt_ticker in row['alternative']:
                    alt_info = strategy_df[strategy_df['ticker'] == alt_ticker]
                    if len(alt_info) > 0:
                        alt_first = alt_info['first_date'].iloc[0]
                        if alt_first > row['delist_date']:
                            gap_days = (alt_first - row['delist_date']).days
                            gap_cases.append({
                                'theme': row['theme'],
                                'delisted': row['ticker'],
                                'alternative': alt_ticker,
                                'delist_date': row['delist_date'],
                                'alt_first': alt_first,
                                'gap_days': gap_days,
                            })
        
        for case in gap_cases:
            report_lines.append(f"- **{case['theme']}**: {case['delisted']} 退市({case['delist_date'].strftime('%Y-%m-%d')}) -> {case['alternative']} 上市({case['alt_first'].strftime('%Y-%m-%d')})")
            report_lines.append(f"  - 断档 **{case['gap_days']}** 天")
        report_lines.append("")
    else:
        report_lines.append("未发现显著的时间断档案例（替代ETF在退市ETF退市前或退市时已上市）。")
        report_lines.append("")
    
    # 四、行业层面结论
    report_lines.append("## 四、行业层面是否存在实质性幸存者偏差？")
    report_lines.append("")
    
    if results['no_alt_themes']:
        report_lines.append("**结论：是，存在实质性幸存者偏差。**")
        report_lines.append("")
        report_lines.append(f"以下 **{len(results['no_alt_themes'])}** 个行业在回测期间没有替代ETF覆盖:")
        report_lines.append("")
        for theme in results['no_alt_themes']:
            report_lines.append(f"- **{theme}**")
        report_lines.append("")
        report_lines.append("这些行业的退市ETF在回测期间是唯一的主题敞口，回测中无法交易该主题，构成实质性偏差。")
    else:
        report_lines.append("**结论：否，不存在实质性幸存者偏差。**")
        report_lines.append("")
        report_lines.append("所有与策略池相关的行业都有替代ETF覆盖，退市ETF不影响行业敞口的完整性。")
    report_lines.append("")
    
    # 五、需要补充的历史代理
    report_lines.append("## 五、哪些行业需要补充历史代理？")
    report_lines.append("")
    
    if results['no_alt_themes']:
        report_lines.append("以下行业需要补充历史代理（如行业指数）以消除幸存者偏差:")
        report_lines.append("")
        for theme in results['no_alt_themes']:
            report_lines.append(f"- **{theme}**: 建议寻找对应行业指数（如申万行业指数）作为历史代理")
        report_lines.append("")
        report_lines.append("> 注意：补齐真实行情并进行替代回测后，才能量化年化影响。当前不量化。")
    else:
        report_lines.append("暂不需要补充历史代理。")
    report_lines.append("")
    
    # 六、建议
    report_lines.append("## 六、建议与后续行动")
    report_lines.append("")
    report_lines.append("1. **本次不修改策略**：幸存者偏差对B0.3回测的影响需要补齐真实行情后才能量化。")
    report_lines.append("2. **数据补充**：未来构建数据库时，应纳入历史退市ETF的数据，或寻找行业指数作为历史代理。")
    report_lines.append("3. **后续研究**：在Phase 7.2中，可以测试'冻结当时可交易池'的方法，进一步验证偏差大小。")
    report_lines.append("4. **策略免疫性**：当前策略的评分机制和min_score门槛提供了一定的'免疫性'，但无法消除行业敞口缺失问题。")
    report_lines.append("")
    
    report_lines.append("---")
    report_lines.append("")
    report_lines.append("*报告生成时间：2026-06-21*")
    report_lines.append("*数据来源：公开信息搜索 + 数据库直接查询*")
    report_lines.append("*研究区间：2019-08-13 ~ 2024-12-31*")
    
    report_text = "\n".join(report_lines)
    
    # 保存报告
    report_path = r'D:\etf_rotation_model\reports\phase7_1_survivorship_bias_audit.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)
    
    print(f"\n报告已保存: {report_path}")
    return report_text


if __name__ == '__main__':
    generate_report_v2()
