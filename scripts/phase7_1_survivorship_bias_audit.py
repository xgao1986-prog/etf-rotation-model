#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.1: ETF 幸存者偏差审计报告 v4（最终收口版）

核心原则：
1. 仅用已验证记录（有权威来源 URL）形成结论。
2. 数据库首日（db_first_date）不得称为「实际上市日」。
3. 无官方来源验证上市日期的 ETF，一律标记为「未验证」。
4. 将「幸存者偏差」拆分为 4 类（A/B/C/D），每类单独讨论。
5. 替代 ETF 必须验证日期重叠。
6. 结论极度保守，不扩大化。
7. 只研究，不改策略。Phase 7.1 收口后不进入 Phase 7.2。

作者：Kimi
版本：v4（最终收口版）
"""

import os
from datetime import datetime, date, timedelta
from typing import Dict, List, Tuple, Any, Optional

# ============================================================================
# 一、已验证数据（v4 唯一可用于结论的数据）
# ============================================================================

VERIFIED_ETFS = {
    # 已退市（3只）
    '512310.SH': {
        'name': '南方中证500工业ETF',
        'list_date': '2015-04-08',       # 华宝证券公告
        'delist_date': '2021-01-07',     # 华宝证券公告
        'track_index': '中证500工业指数',
        'sw_sector': '801890',
        'source': '华宝证券终止上市公告',
        'source_url': 'http://www.cnhbstock.com/detail/351742',
        'status': 'terminated',
    },
    '159953.SZ': {
        'name': '广发中证全指工业ETF',
        'list_date': '2017-06-13',       # 天天基金网
        'delist_date': '2020-12-16',     # 天天基金网
        'track_index': '中证全指工业指数',
        'sw_sector': '801890',
        'source': '天天基金网新发基金详情+广发基金公告',
        'source_url': 'http://fund.eastmoney.com/data/xininfo_159953.html',
        'status': 'terminated',
    },
    '516690.SH': {
        'name': '银华中证细分化工产业主题ETF',
        'list_date': '2021-12-21',       # 上交所公告（修正：交易日不是合同日）
        'delist_date': '2024-08-27',     # 上交所公告
        'track_index': '中证细分化工产业主题指数',
        'sw_sector': '801030',
        'source': '上交所终止上市公告',
        'source_url': 'http://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-08-23/516690_20240823_7ZAM.pdf',
        'status': 'terminated',
    },
    # 存续（1只）
    '159996.SZ': {
        'name': '国泰中证全指家用电器ETF',
        'list_date': '2020-03-16',       # 国泰基金产品资料概要
        'db_first_date': '2022-06-06',   # 数据库首日
        'sw_sector': '801110',
        'source': '国泰基金产品资料概要',
        'source_url': 'https://fundf10.eastmoney.com/jbgk_159996.html',
        'status': 'active',
    },
}

# 回测区间（与策略一致）
BACKTEST_START = date(2019, 8, 13)
BACKTEST_END = date(2024, 12, 31)  # 回测区间截止日

# ============================================================================
# 二、策略池 18 只 ETF（其他 17 只均无官方来源验证上市日期）
# ============================================================================

STRATEGY_POOL = {
    '512480.SH': {'name': '半导体ETF', 'sw_sector': '801080', 'db_first_date': '2019-06-03', 'verified': False},
    '512010.SH': {'name': '医药ETF', 'sw_sector': '801150', 'db_first_date': '2019-06-03', 'verified': False},
    '159928.SZ': {'name': '消费ETF', 'sw_sector': '801120', 'db_first_date': '2019-06-03', 'verified': False},
    '512800.SH': {'name': '银行ETF', 'sw_sector': '801780', 'db_first_date': '2019-06-03', 'verified': False},
    '512000.SH': {'name': '券商ETF', 'sw_sector': '801790', 'db_first_date': '2019-06-03', 'verified': False},
    '512660.SH': {'name': '军工ETF', 'sw_sector': '801740', 'db_first_date': '2019-06-03', 'verified': False},
    '512980.SH': {'name': '传媒ETF', 'sw_sector': '801760', 'db_first_date': '2019-06-03', 'verified': False},
    '512400.SH': {'name': '有色金属ETF', 'sw_sector': '801050', 'db_first_date': '2019-06-03', 'verified': False},
    '518880.SH': {'name': '黄金ETF', 'sw_sector': None, 'db_first_date': '2019-06-03', 'verified': False},
    '511010.SH': {'name': '国债ETF', 'sw_sector': None, 'db_first_date': '2019-06-03', 'verified': False},
    '515230.SH': {'name': '软件ETF', 'sw_sector': '801750', 'db_first_date': '2021-03-02', 'verified': False},
    '515880.SH': {'name': '通信ETF', 'sw_sector': '801770', 'db_first_date': '2019-09-06', 'verified': False},
    '516160.SH': {'name': '新能源ETF', 'sw_sector': '801730', 'db_first_date': '2021-02-04', 'verified': False},
    '516110.SH': {'name': '汽车ETF', 'sw_sector': '801880', 'db_first_date': '2021-05-07', 'verified': False},
    '159865.SZ': {'name': '养殖ETF', 'sw_sector': '801010', 'db_first_date': '2022-06-06', 'verified': False},
    '159697.SZ': {'name': '油气ETF', 'sw_sector': '801960', 'db_first_date': '2023-05-04', 'verified': False},
    '159530.SZ': {'name': '机器人ETF', 'sw_sector': '801890', 'db_first_date': '2024-01-18', 'verified': False},
    '159996.SZ': {'name': '国泰中证全指家用电器ETF', 'sw_sector': '801110', 'db_first_date': '2022-06-06', 'verified': True},
}

# 申万行业代码映射（用于显示）
SW_SECTOR_NAMES = {
    '801030': '基础化工',
    '801110': '家用电器',
    '801890': '机械设备',
    '801750': '计算机',
    '801150': '医药生物',
    '801120': '食品饮料',
    '801780': '银行',
    '801790': '非银金融',
    '801740': '国防军工',
    '801760': '传媒',
    '801050': '有色金属',
    '801130': '汽车',  # 黄金/油气也映射到这里，需要修正
    '801140': '钢铁',  # 国债ETF无对应，需要修正
    '801770': '通信',
    '801730': '电力设备',
    '801880': '汽车',
    '801010': '农林牧渔',
}

# 策略池中 ETF 的行业覆盖（注意：一个行业可能有多只 ETF）
# 这里我们记录每只 ETF 对应的行业

# ============================================================================
# 三、辅助函数
# ============================================================================

def str_to_date(s: str) -> date:
    """将字符串转换为 date 对象。"""
    return datetime.strptime(s, '%Y-%m-%d').date()


def date_overlap(start1: date, end1: date, start2: date, end2: date) -> bool:
    """检查两个日期区间是否有重叠。"""
    return start1 <= end2 and start2 <= end1


def find_potential_replacement(terminated_sector: str, pool: Dict) -> Optional[str]:
    """在策略池中寻找同一行业的潜在替代 ETF。"""
    for ticker, info in pool.items():
        if info['sw_sector'] == terminated_sector:
            return ticker
    return None


def verify_replacement_validity(terminated_etf: str, replacement_etf: str, 
                               terminated_info: Dict, replacement_info: Dict,
                               backtest_start: date, backtest_end: date) -> Tuple[bool, str]:
    """
    验证替代 ETF 是否在需要覆盖的期间内有数据。
    
    返回：
        (is_valid, reason)
        is_valid: True 表示替代有效，False 表示无效
        reason: 说明文字
    """
    # 被替代 ETF 在回测区间内的存续期
    term_list = str_to_date(terminated_info['list_date'])
    term_delist = str_to_date(terminated_info['delist_date'])
    
    # 被替代 ETF 在回测区间内的实际存续期
    actual_start = max(term_list, backtest_start)
    actual_end = min(term_delist, backtest_end)
    
    if actual_start > actual_end:
        return False, "被替代 ETF 在回测区间内无存续期"
    
    # 替代 ETF 的数据库首日
    repl_db_first = str_to_date(replacement_info['db_first_date'])
    
    # 检查替代 ETF 在被替代 ETF 存续期内是否有数据
    # 即：替代 ETF 的数据库首日是否早于或等于被替代 ETF 的存续结束日
    if repl_db_first > actual_end:
        return False, f"替代 ETF 数据库首日({replacement_info['db_first_date']})晚于被替代 ETF 存续结束({terminated_info['delist_date']})，无日期重叠"
    
    # 进一步检查：替代 ETF 是否在被替代 ETF 存续期结束后才上市
    # 如果替代 ETF 在存续期开始后才出现数据，那前面一段仍然没有覆盖
    overlap_start = max(actual_start, repl_db_first)
    overlap_end = actual_end
    
    if overlap_start > overlap_end:
        return False, f"日期区间无重叠：替代 ETF 从{replacement_info['db_first_date']}开始，被替代 ETF 存续至{terminated_info['delist_date']}"
    
    return True, f"日期重叠：{overlap_start} 至 {overlap_end}"


# ============================================================================
# 四、核心分析逻辑
# ============================================================================

def analyze_survivorship_bias_v4() -> Dict[str, Any]:
    """
    v4 核心分析：极度保守，仅用已验证记录。
    
    返回结构化结果，供报告生成使用。
    """
    results = {
        'verified_etfs': [],
        'strategy_pool_audit': [],
        'class_a_deviation': [],      # 退市幸存者偏差
        'class_b_deviation': [],      # 固定池回看偏差
        'class_c_deviation': [],      # ETF 尚未上市
        'class_d_deviation': [],      # 历史数据缺失
        'replacement_check': [],      # 替代关系检查
        'conclusions': {},
    }
    
    # ------------------------------------------------------------------------
    # 1. 已验证 ETF 列表
    # ------------------------------------------------------------------------
    for ticker, info in VERIFIED_ETFS.items():
        record = {
            'ticker': ticker,
            'name': info['name'],
            'list_date': info.get('list_date', 'N/A'),
            'delist_date': info.get('delist_date', 'N/A'),
            'db_first_date': info.get('db_first_date', 'N/A'),
            'sw_sector': info['sw_sector'],
            'source': info['source'],
            'source_url': info['source_url'],
            'status': info['status'],
        }
        results['verified_etfs'].append(record)
    
    # ------------------------------------------------------------------------
    # 2. 策略池 ETF 审计
    # ------------------------------------------------------------------------
    for ticker, info in STRATEGY_POOL.items():
        record = {
            'ticker': ticker,
            'name': info['name'],
            'sw_sector': info['sw_sector'],
            'db_first_date': info['db_first_date'],
            'verified': info['verified'],
            'official_source': '有' if info['verified'] else '无',
            'status': '已验证' if info['verified'] else '未验证',
        }
        results['strategy_pool_audit'].append(record)
    
    # ------------------------------------------------------------------------
    # 3. 4 类偏差分析
    # ------------------------------------------------------------------------
    
    # ---- A. 退市幸存者偏差（仅基于 3 只已验证退市 ETF） ----
    for ticker, info in VERIFIED_ETFS.items():
        if info['status'] != 'terminated':
            continue
        
        sector = info['sw_sector']
        sector_name = SW_SECTOR_NAMES.get(sector, '未知')
        list_date = str_to_date(info['list_date'])
        delist_date = str_to_date(info['delist_date'])
        
        # 检查该 ETF 在回测区间内是否存续
        actual_start = max(list_date, BACKTEST_START)
        actual_end = min(delist_date, BACKTEST_END)
        
        if actual_start > actual_end:
            # 回测开始前已退市，不影响
            continue
        
        # 在策略池中寻找同行业的替代 ETF
        replacement = find_potential_replacement(sector, STRATEGY_POOL)
        
        if replacement is None:
            # 策略池无该行业 ETF
            results['class_a_deviation'].append({
                'ticker': ticker,
                'name': info['name'],
                'sector': sector,
                'sector_name': sector_name,
                'period': f"{actual_start} ~ {actual_end}",
                'replacement': '无',
                'has_deviation': False,  # v4 修正：降为待验证，未完成全市场检查
                'status': '待验证',
                'reason': f'策略池无 {sector_name}({sector}) 的 ETF，但未完成全市场同指数ETF检查，不能确认行业敞口缺失。',
            })
        else:
            # 有潜在替代，验证日期重叠
            repl_info = STRATEGY_POOL[replacement]
            is_valid, reason = verify_replacement_validity(
                ticker, replacement, info, repl_info, BACKTEST_START, BACKTEST_END
            )
            
            results['class_a_deviation'].append({
                'ticker': ticker,
                'name': info['name'],
                'sector': sector,
                'sector_name': sector_name,
                'period': f"{actual_start} ~ {actual_end}",
                'replacement': replacement,
                'replacement_name': repl_info['name'],
                'replacement_db_first': repl_info['db_first_date'],
                'is_valid': is_valid,
                'valid_reason': reason,
                'has_deviation': False,  # v4 修正：降为待验证，未完成全市场检查
                'status': '待验证',
                'reason': f'潜在替代 {replacement} 日期不重叠：{reason}；但未完成全市场同指数ETF检查，不能确认行业敞口缺失。',
            })
    
    # ---- B. 固定池回看偏差（与 A 基于相同 3 只 ETF，角度不同） ----
    # 固定池回看偏差是指：用当前固定池（18只）回看历史，但历史上存在其他可交易 ETF 未被纳入池内。
    # 即使这些历史 ETF 已退市，回测的「固定池」仍遗漏了它们。
    # B类与A类不同：B类不依赖替代验证，仅确认"固定池遗漏了历史上可交易的ETF"这一事实。
    for item in results['class_a_deviation']:
        results['class_b_deviation'].append({
            'ticker': item['ticker'],
            'name': item['name'],
            'sector': item['sector'],
            'sector_name': item['sector_name'],
            'period': item['period'],
            'replacement': item.get('replacement', '无'),
            'has_deviation': True,  # B类保留：固定池遗漏历史上可交易ETF是事实
            'reason': f'固定池回看偏差：当前策略池在回测期间遗漏了该已退市 ETF（{item["period"]}），即使它当时可交易。',
        })
    
    # ---- C. ETF 尚未上市（仅基于已验证的 159996.SZ） ----
    # 其余 17 只因无官方来源无法确认
    for ticker, info in VERIFIED_ETFS.items():
        if info['status'] != 'active':
            continue
        
        list_date = str_to_date(info['list_date'])
        
        if list_date > BACKTEST_START:
            # 回测开始时 ETF 尚未上市
            gap_start = BACKTEST_START
            gap_end = list_date - timedelta(days=1)
            days = (gap_end - gap_start).days + 1
            
            results['class_c_deviation'].append({
                'ticker': ticker,
                'name': info['name'],
                'sector': info['sw_sector'],
                'sector_name': SW_SECTOR_NAMES.get(info['sw_sector'], '未知'),
                'gap_period': f"{gap_start} ~ {gap_end}",
                'days': days,
                'note': 'ETF 尚未上市（非偏差，是历史事实）。仅 159996.SZ 可确认，其余 17 只因无官方来源无法确认。',
            })
    
    # ---- D. 历史数据缺失（仅基于已验证的 159996.SZ） ----
    for ticker, info in VERIFIED_ETFS.items():
        if info['status'] != 'active':
            continue
        
        list_date = str_to_date(info['list_date'])
        db_first = str_to_date(info['db_first_date'])
        
        if db_first > list_date:
            # 数据库首日晚于上市日，说明存在数据缺失
            gap_start = list_date
            gap_end = db_first - timedelta(days=1)
            days = (gap_end - gap_start).days + 1
            years = round(days / 365.25, 1)
            
            results['class_d_deviation'].append({
                'ticker': ticker,
                'name': info['name'],
                'sector': info['sw_sector'],
                'sector_name': SW_SECTOR_NAMES.get(info['sw_sector'], '未知'),
                'gap_period': f"{gap_start} ~ {gap_end}",
                'days': days,
                'years': years,
                'note': f'数据库缺失约 {years} 年数据。仅 159996.SZ 可确认，其余 17 只因无官方来源无法确认。',
            })
    
    # ------------------------------------------------------------------------
    # 4. 替代关系检查（含日期重叠验证）
    # ------------------------------------------------------------------------
    for ticker, info in VERIFIED_ETFS.items():
        if info['status'] != 'terminated':
            continue
        
        sector = info['sw_sector']
        sector_name = SW_SECTOR_NAMES.get(sector, '未知')
        list_date = str_to_date(info['list_date'])
        delist_date = str_to_date(info['delist_date'])
        
        replacement = find_potential_replacement(sector, STRATEGY_POOL)
        
        if replacement is None:
            results['replacement_check'].append({
                'terminated_ticker': ticker,
                'terminated_name': info['name'],
                'sector': sector,
                'sector_name': sector_name,
                'survival_period': f"{info['list_date']} ~ {info['delist_date']}",
                'potential_replacement': '无',
                'replacement_db_first': 'N/A',
                'overlap': 'N/A',
                'is_valid': False,
                'conclusion': '策略池无该行业 ETF，替代无效',
            })
        else:
            repl_info = STRATEGY_POOL[replacement]
            is_valid, reason = verify_replacement_validity(
                ticker, replacement, info, repl_info, BACKTEST_START, BACKTEST_END
            )
            
            results['replacement_check'].append({
                'terminated_ticker': ticker,
                'terminated_name': info['name'],
                'sector': sector,
                'sector_name': sector_name,
                'survival_period': f"{info['list_date']} ~ {info['delist_date']}",
                'potential_replacement': replacement,
                'replacement_name': repl_info['name'],
                'replacement_db_first': repl_info['db_first_date'],
                'overlap': reason,
                'is_valid': is_valid,
                'conclusion': '替代有效' if is_valid else '替代无效（日期不重叠）',
            })
    
    # ------------------------------------------------------------------------
    # 5. 结论（极度保守，仅用已验证记录）
    # ------------------------------------------------------------------------
    
    # A类：退市幸存者偏差 — 降为待验证（未完成全市场同指数ETF检查）
    a_class_sectors = set()
    for item in results['class_a_deviation']:
        a_class_sectors.add(item['sector'])
    
    # B类：固定池回看偏差 — 已确认（固定池遗漏历史上可交易ETF是事实）
    b_class_sectors = set()
    for item in results['class_b_deviation']:
        if item['has_deviation']:
            b_class_sectors.add(item['sector'])
    
    results['conclusions'] = {
        'a_class_sectors_count': len(a_class_sectors),
        'a_class_sectors': sorted(list(a_class_sectors)),
        'a_class_sector_names': [SW_SECTOR_NAMES.get(s, '未知') for s in sorted(list(a_class_sectors))],
        'b_class_sectors_count': len(b_class_sectors),
        'b_class_sectors': sorted(list(b_class_sectors)),
        'b_class_sector_names': [SW_SECTOR_NAMES.get(s, '未知') for s in sorted(list(b_class_sectors))],
        'verified_terminated_count': sum(1 for v in VERIFIED_ETFS.values() if v['status'] == 'terminated'),
        'verified_active_count': sum(1 for v in VERIFIED_ETFS.values() if v['status'] == 'active'),
        'unverified_pool_count': sum(1 for v in STRATEGY_POOL.values() if not v['verified']),
        'total_pool_count': len(STRATEGY_POOL),
    }
    
    return results


# ============================================================================
# 五、报告生成
# ============================================================================

def generate_report_v4(output_path: Optional[str] = None) -> str:
    """
    生成 v4 最终收口版 Markdown 报告。
    
    Args:
        output_path: 报告输出路径，默认为 D:\etf_rotation_model\reports\phase7_1_survivorship_bias_audit.md
    
    Returns:
        生成的报告内容（Markdown 字符串）
    """
    if output_path is None:
        output_path = r'D:\etf_rotation_model\reports\phase7_1_survivorship_bias_audit.md'
    
    results = analyze_survivorship_bias_v4()
    
    lines = []
    
    # ========================================================================
    # 标题
    # ========================================================================
    lines.append("# Phase 7.1: ETF 幸存者偏差审计报告 v4（最终收口版 — A类待验证/B类已确认）")
    lines.append("")
    lines.append("> 原则：仅用已验证记录（有权威来源 URL）形成结论。")
    lines.append(">")
    lines.append("> **v4 修订**：A类（退市幸存者偏差）降为**待验证**，B类（固定池回看偏差）保留**已确认**。未完成全市场同指数ETF检查前，不宣称基础化工、机械设备行业敞口缺失。")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 回测区间：{BACKTEST_START} ~ {BACKTEST_END}")
    lines.append("")
    
    # ========================================================================
    # 一、研究方法论（v4 vs v3 改进）
    # ========================================================================
    lines.append("## 一、研究方法论（v4 vs v3 改进）")
    lines.append("")
    lines.append("v4 相比 v3 的核心改进：")
    lines.append("")
    lines.append("1. **新增 4 类偏差拆分**：将模糊的「幸存者偏差」拆分为 A（退市）、B（固定池回看）、C（尚未上市）、D（数据缺失）四类，每类单独讨论。")
    lines.append("2. **数据库首日不等于上市日**：无官方来源（基金公司公告、交易所公告）验证上市日期的 ETF，一律标记为「未验证」，不得断言「上市日=数据库首日」。")
    lines.append("3. **替代 ETF 必须验证日期重叠**：检查替代 ETF 在被替代 ETF 空窗期或退市 ETF 存续期内是否也有数据，替代关系表中增加「替代有效？」列。")
    lines.append("4. **结论极度保守，不扩大化**：仅使用已验证记录（3只退市 + 1只存续）形成结论，其余策略池 ETF 因缺少官方来源不纳入结论。")
    lines.append("5. **修正 516690 上市交易日**：从 2021-12-07（基金合同生效日）修正为 2021-12-21（实际上市交易日）。")
    lines.append("6. **v4 修订（本次）**：A类（退市幸存者偏差）降为**待验证**，B类（固定池回看偏差）保留**已确认**。未完成全市场同指数ETF检查前，不宣称基础化工、机械设备行业敞口缺失。")
    lines.append("")
    
    # ========================================================================
    # 二、已验证的 ETF（4 只）
    # ========================================================================
    lines.append("## 二、已验证的 ETF（4 只）")
    lines.append("")
    lines.append("以下 4 只 ETF 均有权威来源 URL 验证，是 v4 唯一可用于结论的数据：")
    lines.append("")
    lines.append("| 代码 | 名称 | 上市日 | 退市日 | 数据库首日 | 申万行业 | 来源 | 状态 |")
    lines.append("|------|------|--------|--------|------------|----------|------|------|")
    
    for item in results['verified_etfs']:
        delist = item.get('delist_date', 'N/A')
        db_first = item.get('db_first_date', 'N/A')
        lines.append(f"| {item['ticker']} | {item['name']} | {item['list_date']} | {delist} | {db_first} | {item['sw_sector']} | {item['source']} | {'已退市' if item['status'] == 'terminated' else '存续'} |")
    
    lines.append("")
    lines.append("来源 URL：")
    for item in results['verified_etfs']:
        lines.append(f"- {item['ticker']}：{item['source_url']}")
    lines.append("")
    
    # ========================================================================
    # 三、策略池 ETF 审计（18 只）
    # ========================================================================
    lines.append("## 三、策略池 ETF 审计（18 只）")
    lines.append("")
    lines.append("策略池中仅 159996.SZ 有官方来源验证上市日期，其余 17 只均标记为「未验证」：")
    lines.append("")
    lines.append("| 代码 | 名称 | 申万行业 | 数据库首日 | 官方来源验证 | 状态 |")
    lines.append("|------|------|----------|------------|--------------|------|")
    
    for item in results['strategy_pool_audit']:
        lines.append(f"| {item['ticker']} | {item['name']} | {item['sw_sector']} | {item['db_first_date']} | {item['official_source']} | {item['status']} |")
    
    lines.append("")
    lines.append("**注意**：数据库首日（db_first_date）仅反映数据收集起始，不等于上市日。无官方来源验证的 ETF，其数据库首日之前的空窗原因无法确认（可能是「ETF 未上市」，也可能是「数据缺失」）。")
    lines.append("")
    
    # ========================================================================
    # 四、4 类偏差分析
    # ========================================================================
    lines.append("## 四、4 类偏差分析")
    lines.append("")
    
    # ---- A. 退市幸存者偏差 ----
    lines.append("### A. 退市幸存者偏差（基于 3 只已验证退市 ETF）")
    lines.append("")
    lines.append("定义：回测区间内曾经存在、但已退市的 ETF，其行业敞口未被策略池替代 ETF 覆盖。")
    lines.append("")
    
    if results['class_a_deviation']:
        lines.append("| 代码 | 名称 | 申万行业 | 回测区间内存续期 | 潜在替代 | 替代有效？ | 结论 |")
        lines.append("|------|------|----------|------------------|----------|------------|------|")
        for item in results['class_a_deviation']:
            repl = item.get('replacement', '无')
            valid = '是' if item.get('is_valid') else '否'
            conclusion = item.get('status', '待验证')
            lines.append(f"| {item['ticker']} | {item['name']} | {item['sector_name']}({item['sector']}) | {item['period']} | {repl} | {valid} | {conclusion} |")
    else:
        lines.append("无已验证的退市 ETF 在回测区间内存续。")
    
    lines.append("")
    
    # 详细说明
    lines.append("**说明**：A类偏差（退市幸存者偏差）已降为**待验证**。原因：策略池内无替代ETF，但未完成全市场同指数ETF检查，不能确认是否存在其他可交易ETF覆盖了该行业敞口。")
    for item in results['class_a_deviation']:
        lines.append(f"- **{item['ticker']}**（{item['name']}）：{item['reason']}")
    lines.append("")
    
    # ---- B. 固定池回看偏差 ----
    lines.append("### B. 固定池回看偏差（基于 3 只已验证退市 ETF）")
    lines.append("")
    lines.append("定义：用当前固定池（18只）回看历史，但历史上存在其他可交易 ETF（如退市 ETF）未被纳入池内。即使这些历史 ETF 已退市，回测的「固定池」仍遗漏了它们。")
    lines.append("")
    
    if results['class_b_deviation']:
        lines.append("| 代码 | 名称 | 申万行业 | 回测区间内存续期 | 策略池遗漏 | 结论 |")
        lines.append("|------|------|----------|------------------|------------|------|")
        for item in results['class_b_deviation']:
            conclusion = '存在偏差' if item['has_deviation'] else '无偏差'
            lines.append(f"| {item['ticker']} | {item['name']} | {item['sector_name']}({item['sector']}) | {item['period']} | 是 | {conclusion} |")
    else:
        lines.append("无已验证的退市 ETF 在回测区间内存续。")
    
    lines.append("")
    
    # 详细说明
    lines.append("**说明**：B类偏差（固定池回看偏差）已确认。当前18只固定池确实遗漏了这3只历史上可交易的已退市ETF。")
    for item in results['class_b_deviation']:
        lines.append(f"- **{item['ticker']}**（{item['name']}）：{item['reason']}")
    lines.append("")
    
    # ---- C. ETF 尚未上市 ----
    lines.append("### C. ETF 尚未上市（仅基于已验证的 159996.SZ）")
    lines.append("")
    lines.append("定义：策略池中的某些 ETF 在回测早期确实没有成立/上市。此类不是「偏差」，是历史事实。但仅对取得官方来源验证的 ETF 讨论此类。")
    lines.append("")
    
    if results['class_c_deviation']:
        lines.append("| 代码 | 名称 | 申万行业 | 尚未上市期间 | 天数 | 说明 |")
        lines.append("|------|------|----------|--------------|------|------|")
        for item in results['class_c_deviation']:
            lines.append(f"| {item['ticker']} | {item['name']} | {item['sector_name']}({item['sector']}) | {item['gap_period']} | {item['days']} | {item['note']} |")
    else:
        lines.append("无已验证的存续 ETF 在回测开始时未上市。")
    
    lines.append("")
    lines.append("**重要**：其余 17 只策略池 ETF 因无官方来源验证上市日期，无法确认其空窗原因是「ETF 未上市」还是「数据缺失」，不纳入 C 类结论。")
    lines.append("")
    
    # ---- D. 历史数据缺失 ----
    lines.append("### D. 历史数据缺失（仅基于已验证的 159996.SZ）")
    lines.append("")
    lines.append("定义：权威来源验证的上市日早于数据库首日，说明数据库在上市初期缺失数据。此类属于数据采集偏差。仅对取得官方来源验证的 ETF 讨论此类。")
    lines.append("")
    
    if results['class_d_deviation']:
        lines.append("| 代码 | 名称 | 申万行业 | 缺失期间 | 天数 | 约年数 | 说明 |")
        lines.append("|------|------|----------|----------|------|--------|------|")
        for item in results['class_d_deviation']:
            lines.append(f"| {item['ticker']} | {item['name']} | {item['sector_name']}({item['sector']}) | {item['gap_period']} | {item['days']} | {item['years']} | {item['note']} |")
    else:
        lines.append("无已验证的存续 ETF 存在数据库首日晚于上市日的情况。")
    
    lines.append("")
    lines.append("**重要**：其余 17 只策略池 ETF 因无官方来源验证上市日期，无法确认其空窗原因是「ETF 未上市」还是「数据缺失」，不纳入 D 类结论。")
    lines.append("")
    
    # ========================================================================
    # 五、替代关系检查（含日期重叠验证）
    # ========================================================================
    lines.append("## 五、替代关系检查（含日期重叠验证）")
    lines.append("")
    lines.append("| 退市 ETF | 申万行业 | 潜在替代 | 替代数据库首日 | 退市 ETF 存续期 | 日期重叠？ | 结论 |")
    lines.append("|----------|----------|----------|----------------|-----------------|------------|------|")
    
    for item in results['replacement_check']:
        overlap = item.get('overlap', 'N/A')
        lines.append(f"| {item['terminated_ticker']} | {item['sector_name']}({item['sector']}) | {item['potential_replacement']} | {item['replacement_db_first']} | {item['survival_period']} | {overlap} | {item['conclusion']} |")
    
    lines.append("")
    lines.append("**关键发现**：")
    for item in results['replacement_check']:
        if not item['is_valid']:
            lines.append(f"- {item['terminated_ticker']}（{item['terminated_name']}）的潜在替代 {item['potential_replacement']} 在退市 ETF 存续期内无有效数据，替代无效。")
    lines.append("")
    
    # ========================================================================
    # 六、结论（极度保守，仅用已验证记录）
    # ========================================================================
    lines.append("## 六、结论（极度保守，仅用已验证记录）")
    lines.append("")
    lines.append("**基于已验证记录（3 只退市 + 1 只存续）：**")
    lines.append("")
    
    conc = results['conclusions']
    
    lines.append(f"1. **A类：退市幸存者偏差 — 待验证**（涉及 {conc['a_class_sectors_count']} 个行业）")
    if conc['a_class_sectors']:
        sector_desc = ", ".join([f"{name}({code})" for code, name in zip(conc['a_class_sectors'], conc['a_class_sector_names'])])
        lines.append(f"   - 涉及行业：{sector_desc}")
        lines.append(f"   - 策略池内无替代ETF，但**未完成全市场同指数ETF检查**，不能确认是否存在其他可交易ETF覆盖了该行业敞口。")
        lines.append(f"   - 在确认全市场无替代之前，**不宣称**基础化工、机械设备行业敞口缺失。")
    else:
        lines.append("   - 无待验证的行业。")
    
    lines.append("")
    lines.append(f"2. **B类：固定池回看偏差 — 已确认**（涉及 {conc['b_class_sectors_count']} 个行业）")
    if conc['b_class_sectors']:
        sector_desc = ", ".join([f"{name}({code})" for code, name in zip(conc['b_class_sectors'], conc['b_class_sector_names'])])
        lines.append(f"   - 涉及行业：{sector_desc}")
        lines.append(f"   - 3只已验证退市ETF（512310/159953/516690）在回测期间可交易，当前18只固定池确实遗漏了它们。")
        lines.append(f"   - 这是固定池回看偏差的定性确认，不涉及行业敞口是否被其他ETF覆盖。")
    else:
        lines.append("   - 无已确认的行业。")
    
    lines.append("")
    lines.append("3. **已确认 C/D 类但仅 159996.SZ 一例**：")
    if results['class_c_deviation']:
        for item in results['class_c_deviation']:
            lines.append(f"   - {item['gap_period']}：ETF 尚未上市（C 类，非偏差），共 {item['days']} 天。")
    if results['class_d_deviation']:
        for item in results['class_d_deviation']:
            lines.append(f"   - {item['gap_period']}：数据库缺失约 {item['years']} 年数据（D 类）。")
    lines.append("   - 仅 159996.SZ 可确认 C/D 类，其余 17 只因无官方来源无法确认。")
    
    lines.append("")
    lines.append(f"4. **其余 {conc['unverified_pool_count']} 只策略池 ETF**：因缺少官方来源验证的上市日期，无法确认其空窗原因是「ETF 未上市」还是「数据缺失」，**不纳入结论**。数据库首日仅反映数据收集起始，不等同于上市日。")
    
    lines.append("")
    lines.append("5. **v2 中其余约 74 只退市 ETF**：全部标记为「未验证」，不纳入结论。")
    
    lines.append("")
    lines.append("**总结**：v4 不再宣称「9 个行业均存在实质性幸存者偏差」。基于已验证的 3 只退市 ETF，**B类（固定池回看偏差）已确认**：3只已退市ETF在回测期间可交易，当前固定池遗漏了它们。**A类（退市幸存者偏差）降为待验证**：未完成全市场同指数ETF检查前，不宣称基础化工、机械设备行业敞口缺失。其余策略池 ETF 因缺少官方来源验证，不纳入结论。")
    lines.append("")
    
    # ========================================================================
    # 七、建议与后续行动
    # ========================================================================
    lines.append("## 七、建议与后续行动")
    lines.append("")
    lines.append("1. **本次不修改策略**：偏差需量化后才能评估影响，当前仅完成定性识别。")
    lines.append("2. **B类（固定池回看偏差）已确认**：当前18只固定池确实遗漏了3只历史上可交易的已退市ETF。如需评估影响，可测试'冻结当时可交易池'方法。")
    lines.append("3. **A类（退市幸存者偏差）待验证**：在确认全市场无同指数/同行业替代ETF之前，不宣称行业敞口缺失。不扩展验证74只ETF。")
    lines.append("4. **其余策略池 ETF 需获取官方来源验证**：通过基金公司公告或交易所公告获取其余 17 只 ETF 的准确上市日期，确认后方可判断 C/D 类偏差。")
    lines.append("5. **不进入 Phase 7.2**：Phase 7.1 已收口，仅保留定性结论，不进行策略修改。")
    lines.append("")
    
    # ========================================================================
    # 附录：版本历史
    # ========================================================================
    lines.append("---")
    lines.append("")
    lines.append("## 附录：版本历史")
    lines.append("")
    lines.append("- **v4**（最终收口版）：极度保守，4 类偏差拆分，替代日期重叠验证，仅用已验证记录。")
    lines.append("- **v4 修订**：A类（退市幸存者偏差）降为待验证，保留B类（固定池回看偏差）。未完成全市场同指数ETF检查前，不宣称行业敞口缺失。")
    lines.append("- v3：初步分析，含 516690 上市日错误（2021-12-07 合同生效日误作上市日）。")
    lines.append("- v2：扩大化分析，含约 74 只退市 ETF，但多数未验证。")
    lines.append("")
    
    report = "\n".join(lines)
    
    # 确保目录存在
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"[OK] 报告已生成：{output_path}")
    print(f"[INFO] 回测区间：{BACKTEST_START} ~ {BACKTEST_END}")
    print(f"[INFO] 已验证 ETF：{len(results['verified_etfs'])} 只（{conc['verified_terminated_count']} 只退市 + {conc['verified_active_count']} 只存续）")
    print(f"[INFO] 策略池未验证：{conc['unverified_pool_count']}/{conc['total_pool_count']} 只")
    print(f"[INFO] A类（退市幸存者偏差）待验证行业：{conc['a_class_sectors_count']} 个")
    print(f"[INFO] B类（固定池回看偏差）已确认行业：{conc['b_class_sectors_count']} 个")
    
    return report


# ============================================================================
# 主入口
# ============================================================================

if __name__ == '__main__':
    generate_report_v4()
