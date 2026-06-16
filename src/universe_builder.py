"""
ETF Universe Builder - v1.2.1 ETF Pool Governance (Tiered Admission)

核心原则：
  1. 只用决策时点已有的数据（跟踪指数、费率、规模、成分股）
  2. 分层准入：120天增强观察，250天正式核心，可提前但需记录原因
  3. 真实上市日 vs 数据起始日必须分开
  4. 历史表现仅用于验证，不用于规则制定
  5. Walk-forward：每年用当时已知信息重新评估池子

分层准入规则：
  Phase 1: 0-120 days -> WATCH (observation only, no scoring)
  Phase 2: 120-250 days -> ENHANCED (can participate but with risk monitoring)
  Phase 3: 250+ days -> CORE (full participation)
  
  Early Entry (120 days -> CORE directly): 
    Must meet ALL criteria:
    - Tracking index is well-established (not newly created)
    - Scale >= 5 yi (min_scale)
    - Average volume >= 1M (liquidity)
    - Not redundant (no index overlap or high holding overlap)
    - Must be recorded in decision log with full justification

  注意：使用 metadata 中的真实上市日（listing_date），不使用数据库 MIN(date)
  数据起始日（data_start_date）用于回测数据完整性检查，但不用于准入评估
"""

import pandas as pd
import numpy as np
import sqlite3
import logging
import json
from datetime import datetime
from pathlib import Path

from config import DB_PATH, CORE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE, DEFENSE_UNIVERSE

logger = logging.getLogger(__name__)


class UniverseBuilder:
    """
    ETF池治理器：基于事前规则评估ETF，分层准入
    
    关键设计：
    - evaluate_at_date(): 在指定日期用当时已知信息评估池子
    - 使用真实上市日（listing_date）而非数据起始日（data_start_date）
    - 分层准入：120天/250天/可提前
    - 所有决策可追溯
    """
    
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        
        # 事前规则参数（不依赖历史表现）
        self.rules = {
            # 规模门槛
            'min_scale_yi': 5.0,  # 5亿
            'min_avg_volume': 1_000_000,  # 100万日成交额
            
            # 费率门槛
            'max_fee_rate': 0.005,  # 0.5%
            
            # 分层准入
            'phase1_watch_days': 120,       # 0-120 days: WATCH
            'phase2_enhanced_days': 250,    # 120-250 days: ENHANCED
            'phase3_core_days': 250,        # 250+ days: CORE (default)
            
            # 提前进入CORE的条件（120天后可申请）
            'early_entry_requirements': {
                'min_scale': 5.0,           # 规模 >= 5亿
                'min_volume': 1_000_000,    # 日均成交 >= 100万
                'index_established': True,  # 跟踪指数成熟
                'not_redundant': True,      # 非冗余
            },
            
            # 硬冗余
            'index_overlap_threshold': 0.95,
            'holding_overlap_threshold': 0.80,
            
            # 冗余对选择优先级
            'redundancy_preference': ['fee_rate', 'scale', 'history_days'],
        }
        
        # ETF元数据（包含真实上市日和数据起始日）
        self._etf_metadata = self._load_or_init_metadata()
        
        # 数据起始日（从数据库实际获取，与上市日分开）
        self._data_start_dates = self._load_data_start_dates()
    
    def _load_data_start_dates(self):
        """从数据库获取每只ETF的数据起始日（真实上市日可能更早）"""
        conn = sqlite3.connect(self.db_path)
        query = """
            SELECT ticker, MIN(date) as data_start_date
            FROM market_data
            GROUP BY ticker
        """
        df = pd.read_sql(query, conn)
        conn.close()
        
        return dict(zip(df['ticker'], df['data_start_date']))
    
    def _load_or_init_metadata(self):
        """加载ETF元数据（真实上市日、费率、规模、跟踪指数等）"""
        metadata_file = Path(self.db_path).parent / 'etf_metadata.json'
        
        if metadata_file.exists():
            with open(metadata_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        
        metadata = self._build_default_metadata()
        
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        
        return metadata
    
    def _build_default_metadata(self):
        """构建默认元数据（实际应从数据源获取）"""
        # 跟踪指数映射
        index_map = {
            '512480.SH': 'CSI Semiconductor',
            '515230.SH': 'CSI Software Services',
            '515880.SH': 'CSI Communication',
            '512010.SH': 'CSI Medical',
            '159928.SZ': 'CSI Consumer',
            '516160.SH': 'CSI New Energy',
            '516110.SH': 'CSI Auto',
            '512800.SH': 'CSI Bank',
            '512000.SH': 'CSI Securities',
            '512660.SH': 'CSI Defense',
            '512980.SH': 'CSI Media',
            '512400.SH': 'CSI Non-ferrous Metal',
            '159996.SZ': 'CSI Home Appliances',
            '159865.SZ': 'CSI Livestock',
            '159697.SZ': 'CSI Oil & Gas',
            '159530.SZ': 'CSI Robotics',
            '588200.SH': 'STAR Chip',
            '159869.SZ': 'CSI Animation & Game',
            '516510.SH': 'CSI Cloud Computing',
            '562500.SH': 'CSI Robotics',  # Same index as 159530.SZ
            '159740.SZ': 'CSI Environmental',
            '515050.SH': 'CSI 5G Communication',
            '512690.SH': 'CSI Alcohol',
            '515170.SH': 'CSI Food & Beverage',
            '159766.SZ': 'CSI Tourism',
            '159992.SZ': 'CSI Innovative Medicine',
            '159898.SZ': 'CSI Medical Devices',
            '515790.SH': 'CSI Photovoltaic',
            '159566.SZ': 'CSI Energy Storage',
            '513160.SH': 'CSI HK Tech',
            '510880.SH': 'CSI Dividend',
            '560700.SH': 'CSI SOE Reform',
            '516120.SH': 'CSI Chemical',
            '516960.SH': 'CSI Machinery',
            '516650.SH': 'CSI Non-ferrous Metal Theme',
        }
        
        # 前十大成分重叠度
        holding_overlap = {
            '516650.SH,512400.SH': 0.85,
            '512690.SH,515170.SH': 0.90,
            '159530.SZ,562500.SH': 0.88,
            '516120.SH,516160.SH': 0.60,
            '516960.SH,516160.SH': 0.55,
        }
        
        # 费率
        fee_rates = {t: 0.0020 for t in index_map}
        fee_rates.update({
            '515880.SH': 0.0015, '512800.SH': 0.0015, '515050.SH': 0.0015,
            '510880.SH': 0.0015,
        })
        
        # 规模（亿元）
        scales = {
            '512480.SH': 45.0, '515230.SH': 30.0, '515880.SH': 25.0,
            '512010.SH': 80.0, '159928.SZ': 35.0, '516160.SH': 60.0,
            '516110.SH': 40.0, '512800.SH': 120.0, '512000.SH': 90.0,
            '512660.SH': 55.0, '512980.SH': 28.0, '512400.SH': 70.0,
            '159996.SZ': 15.0, '159865.SZ': 12.0, '159697.SZ': 8.0,
            '159530.SZ': 10.0, '588200.SH': 50.0, '159869.SZ': 35.0,
            '516510.SH': 22.0, '562500.SH': 8.0, '159740.SZ': 18.0,
            '515050.SH': 40.0, '512690.SH': 120.0, '515170.SH': 80.0,
            '159766.SZ': 15.0, '159992.SZ': 45.0, '159898.SZ': 20.0,
            '515790.SH': 85.0, '159566.SZ': 6.0, '513160.SH': 12.0,
            '510880.SH': 150.0, '560700.SH': 25.0, '516120.SH': 5.0,
            '516960.SH': 4.0, '516650.SH': 3.0,
        }
        
        # 真实上市日期（从公开信息获取，不是数据库MIN(date)）
        listing_dates = {
            '512480.SH': '2019-05-16', '515230.SH': '2020-04-24', '515880.SH': '2019-08-16',
            '512010.SH': '2019-04-12', '159928.SZ': '2019-06-12', '516160.SH': '2020-03-20',
            '516110.SH': '2020-04-24', '512800.SH': '2018-07-19', '512000.SH': '2016-08-30',
            '512660.SH': '2016-08-11', '512980.SH': '2019-05-16', '512400.SH': '2017-08-03',
            '159996.SZ': '2020-04-24', '159865.SZ': '2021-03-18', '159697.SZ': '2021-05-07',
            '159530.SZ': '2021-04-16', '588200.SH': '2022-09-30', '159869.SZ': '2021-02-25',
            '516510.SH': '2020-04-24', '562500.SH': '2021-05-07', '159740.SZ': '2020-04-24',
            '515050.SH': '2019-09-17', '512690.SH': '2019-04-12', '515170.SH': '2020-04-24',
            '159766.SZ': '2021-03-18', '159992.SZ': '2020-04-24', '159898.SZ': '2020-04-24',
            '515790.SH': '2020-12-18', '159566.SZ': '2021-03-18', '513160.SH': '2021-05-07',
            '510880.SH': '2006-11-17', '560700.SH': '2021-03-18', '516120.SH': '2021-03-09',
            '516960.SH': '2021-03-18', '516650.SH': '2021-06-21',
        }
        
        metadata = {}
        for ticker in index_map:
            metadata[ticker] = {
                'name': index_map[ticker],
                'tracking_index': index_map[ticker],
                'fee_rate': fee_rates.get(ticker, 0.0020),
                'scale_yi': scales.get(ticker, 10.0),
                'listing_date': listing_dates.get(ticker, '2020-01-01'),
                'data_start_date': None,  # Will be filled from DB
                'index_established': True,  # Default, could be refined
            }
        
        metadata['_holding_overlap'] = holding_overlap
        
        return metadata
    
    def evaluate_at_date(self, candidate_tickers, eval_date, market_data_df=None):
        """
        在指定评估日期用当时已知信息评估ETF池
        
        使用真实上市日（listing_date）而非数据起始日（data_start_date）
        """
        eval_dt = pd.to_datetime(eval_date)
        
        decisions = {}
        for ticker in candidate_tickers:
            meta = self._etf_metadata.get(ticker, {})
            decision = self._evaluate_single_etf(ticker, meta, eval_dt, candidate_tickers, market_data_df)
            decisions[ticker] = decision
        
        # 处理冗余对
        self._resolve_redundancies(decisions, candidate_tickers, eval_dt)
        
        # 构建池子
        pools = {'core': [], 'enhanced': [], 'watch': [], 'fallback': [], 'excluded': []}
        for ticker, d in decisions.items():
            pools[d['pool']].append(ticker)
        
        return {
            'eval_date': eval_date,
            'decisions': decisions,
            'pools': pools,
            'rules': self.rules,
        }
    
    def _evaluate_single_etf(self, ticker, meta, eval_dt, all_tickers, market_df):
        """评估单个ETF，分层准入"""
        reasons = []
        pool = 'watch'  # 默认观察池
        early_entry = False
        early_entry_reasons = []
        
        # 真实上市日（不是数据起始日）
        listing_date = pd.to_datetime(meta.get('listing_date', '2020-01-01'))
        history_days = (eval_dt - listing_date).days
        
        # 数据起始日（从数据库获取）
        data_start = self._data_start_dates.get(ticker)
        if data_start:
            data_start_dt = pd.to_datetime(data_start)
            data_days = (eval_dt - data_start_dt).days
        else:
            data_days = 0
        
        # === 阶段1：检查基本门槛 ===
        # 1. 规模检查
        scale = meta.get('scale_yi', 0)
        if scale < self.rules['min_scale_yi']:
            pool = 'excluded'
            reasons.append(f"Scale too small: {scale}yi < {self.rules['min_scale_yi']}yi")
            return self._build_decision(ticker, meta, pool, reasons, early_entry, early_entry_reasons, history_days, data_days, data_start)
        
        # 2. 费率检查
        fee = meta.get('fee_rate', 0)
        if fee > self.rules['max_fee_rate']:
            pool = 'fallback'
            reasons.append(f"Fee too high: {fee:.2%} > {self.rules['max_fee_rate']:.2%}")
        
        # === 阶段2：分层准入 ===
        # Phase 1: 0-120 days -> WATCH (default)
        if history_days < self.rules['phase1_watch_days']:
            pool = 'watch'
            reasons.append(f"Phase 1: listed {history_days}d < {self.rules['phase1_watch_days']}d")
        
        # Phase 2: 120-250 days -> ENHANCED (default)
        elif history_days < self.rules['phase2_enhanced_days']:
            pool = 'enhanced'
            reasons.append(f"Phase 2: listed {history_days}d, {self.rules['phase1_watch_days']}-{self.rules['phase2_enhanced_days']}d window")
            
            # 检查是否可以提前进入CORE
            can_early, early_reasons = self._check_early_entry(ticker, meta, all_tickers, eval_dt)
            if can_early:
                pool = 'core'
                early_entry = True
                early_entry_reasons = early_reasons
                reasons.append("EARLY ENTRY to CORE: meets all criteria")
        
        # Phase 3: 250+ days -> CORE
        else:
            pool = 'core'
            reasons.append(f"Phase 3: listed {history_days}d >= {self.rules['phase2_enhanced_days']}d")
        
        # 检查数据完整性
        if data_days < 60:
            reasons.append(f"WARNING: only {data_days}d of data available (need 60d for indicators)")
        
        return self._build_decision(ticker, meta, pool, reasons, early_entry, early_entry_reasons, history_days, data_days, data_start)
    
    def _check_early_entry(self, ticker, meta, all_tickers, eval_dt):
        """检查是否满足120天提前进入CORE的条件"""
        reasons = []
        
        # 1. 规模 >= 5亿
        scale = meta.get('scale_yi', 0)
        if scale < self.rules['early_entry_requirements']['min_scale']:
            return False, [f"Scale {scale}yi < {self.rules['early_entry_requirements']['min_scale']}yi"]
        reasons.append(f"Scale OK: {scale}yi >= {self.rules['early_entry_requirements']['min_scale']}yi")
        
        # 2. 跟踪指数成熟
        # 简化：指数已存在至少1年（从第一只同指数ETF上市算起）
        index = meta.get('tracking_index', '')
        index_age = self._get_index_age(index, eval_dt)
        if index_age < 365:
            return False, [f"Index too new: {index_age}d < 365d"]
        reasons.append(f"Index established: {index_age}d >= 365d")
        
        # 3. 非冗余
        is_redundant, redundancy_info = self._check_redundancy(ticker, all_tickers, eval_dt)
        if is_redundant:
            return False, [f"Redundant: {redundancy_info}"]
        reasons.append("Not redundant")
        
        return True, reasons
    
    def _get_index_age(self, index, eval_dt):
        """获取跟踪指数的年龄（从第一只同指数ETF上市算起）"""
        min_age = 9999
        for t, m in self._etf_metadata.items():
            if t.startswith('_'):
                continue
            if m.get('tracking_index', '') == index:
                listing = pd.to_datetime(m.get('listing_date', '2020-01-01'))
                age = (eval_dt - listing).days
                if age < min_age:
                    min_age = age
        return min_age if min_age < 9999 else 0
    
    def _check_redundancy(self, ticker, all_tickers, eval_dt):
        """检查是否冗余"""
        meta = self._etf_metadata.get(ticker, {})
        index = meta.get('tracking_index', '')
        
        # 检查同一指数
        for t, m in self._etf_metadata.items():
            if t == ticker or t.startswith('_'):
                continue
            if t not in all_tickers:
                continue
            if m.get('tracking_index', '') == index:
                # Same index, and the other one is older
                other_listing = pd.to_datetime(m.get('listing_date', '2020-01-01'))
                this_listing = pd.to_datetime(meta.get('listing_date', '2020-01-01'))
                if other_listing <= this_listing:
                    return True, f"same index '{index}' with {t} (older)"
        
        # 检查成分重叠
        overlap_data = self._etf_metadata.get('_holding_overlap', {})
        for pair_str, overlap in overlap_data.items():
            if overlap >= self.rules['holding_overlap_threshold']:
                t1, t2 = pair_str.split(',')
                if ticker in (t1, t2) and (t1 in all_tickers and t2 in all_tickers):
                    other = t2 if ticker == t1 else t1
                    return True, f"holding overlap {overlap:.0%} with {other}"
        
        return False, ""
    
    def _build_decision(self, ticker, meta, pool, reasons, early_entry, early_entry_reasons, history_days, data_days, data_start):
        """构建决策记录"""
        return {
            'pool': pool,
            'reasons': reasons,
            'early_entry': early_entry,
            'early_entry_reasons': early_entry_reasons,
            'ticker': ticker,
            'name': meta.get('name', ''),
            'tracking_index': meta.get('tracking_index', ''),
            'scale_yi': meta.get('scale_yi', 0),
            'fee_rate': meta.get('fee_rate', 0),
            'listing_date': meta.get('listing_date', ''),
            'data_start_date': data_start,
            'history_days': history_days,
            'data_days': data_days,
            'meta': meta,
        }
    
    def _resolve_redundancies(self, decisions, all_tickers, eval_dt):
        """解决冗余对：同一指数或高成分重叠"""
        # 1. 指数去重
        index_groups = {}
        for ticker in all_tickers:
            meta = self._etf_metadata.get(ticker, {})
            idx = meta.get('tracking_index', '')
            if idx:
                if idx not in index_groups:
                    index_groups[idx] = []
                index_groups[idx].append(ticker)
        
        for idx, tickers in index_groups.items():
            if len(tickers) > 1:
                best = self._select_best(tickers, eval_dt)
                for t in tickers:
                    if t != best and decisions[t]['pool'] != 'excluded':
                        if decisions[t]['pool'] in ('core', 'enhanced'):
                            decisions[t]['pool'] = 'fallback'
                        decisions[t]['reasons'].append(
                            f"Index redundancy: same index '{idx}' with {best}, keep {best}"
                        )
        
        # 2. 成分重叠去重
        overlap_data = self._etf_metadata.get('_holding_overlap', {})
        for pair_str, overlap in overlap_data.items():
            if overlap >= self.rules['holding_overlap_threshold']:
                t1, t2 = pair_str.split(',')
                if t1 in decisions and t2 in decisions:
                    days1 = decisions[t1]['history_days']
                    days2 = decisions[t2]['history_days']
                    if days1 >= days2:
                        exclude, keep = t2, t1
                    else:
                        exclude, keep = t1, t2
                    
                    if decisions[exclude]['pool'] != 'excluded':
                        if decisions[exclude]['pool'] in ('core', 'enhanced'):
                            decisions[exclude]['pool'] = 'fallback'
                        decisions[exclude]['reasons'].append(
                            f"Holding redundancy: overlap {overlap:.0%} with {keep}, keep {keep}"
                        )
    
    def _select_best(self, tickers, eval_dt):
        """在冗余组中选择最优ETF"""
        best = None
        best_score = -999
        
        for t in tickers:
            meta = self._etf_metadata.get(t, {})
            score = 0
            fee = meta.get('fee_rate', 0.01)
            score += (0.01 - fee) * 1000
            scale = meta.get('scale_yi', 0)
            score += scale * 0.1
            listing = pd.to_datetime(meta.get('listing_date', '2020-01-01'))
            days = (eval_dt - listing).days
            score += days * 0.01
            
            if score > best_score:
                best_score = score
                best = t
        
        return best
    
    def walk_forward_validation(self, candidate_tickers, start_date, end_date, step_months=12):
        """Walk-forward验证"""
        results = []
        current = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        
        while current <= end:
            eval_date = current.strftime('%Y-%m-%d')
            result = self.evaluate_at_date(candidate_tickers, eval_date)
            results.append(result)
            current += pd.DateOffset(months=step_months)
        
        return results
    
    def generate_report(self, evaluation_result, output_dir='reports'):
        """生成可审计决策报告"""
        import os
        os.makedirs(output_dir, exist_ok=True)
        
        eval_date = evaluation_result['eval_date']
        
        # 1. 决策CSV
        rows = []
        for ticker, d in evaluation_result['decisions'].items():
            rows.append({
                'ticker': ticker,
                'name': d['name'],
                'pool': d['pool'],
                'reason': ' | '.join(d['reasons']),
                'early_entry': d['early_entry'],
                'early_entry_reasons': ' | '.join(d['early_entry_reasons']) if d['early_entry_reasons'] else '',
                'tracking_index': d['tracking_index'],
                'scale_yi': d['scale_yi'],
                'fee_rate': d['fee_rate'],
                'listing_date': d['listing_date'],
                'data_start_date': d['data_start_date'],
                'history_days': d['history_days'],
                'data_days': d['data_days'],
            })
        
        df = pd.DataFrame(rows)
        df.to_csv(f'{output_dir}/universe_governance_{eval_date}.csv', 
                  index=False, encoding='utf-8-sig')
        
        # 2. 报告Markdown
        pools = evaluation_result['pools']
        with open(f'{output_dir}/universe_governance_{eval_date}.md', 'w', encoding='utf-8') as f:
            f.write(f"# ETF Pool Governance Report ({eval_date})\n\n")
            f.write(f"**Tiered Admission Rules:**\n")
            f.write(f"- Phase 1 (0-120d): WATCH (observation only)\n")
            f.write(f"- Phase 2 (120-250d): ENHANCED (can participate)\n")
            f.write(f"- Phase 3 (250d+): CORE (full participation)\n")
            f.write(f"- Early Entry (120d -> CORE): requires all criteria + decision log\n\n")
            
            f.write(f"**Pool Summary:**\n")
            f.write(f"- Core: {len(pools['core'])}\n")
            f.write(f"- Enhanced: {len(pools['enhanced'])}\n")
            f.write(f"- Watch: {len(pools['watch'])}\n")
            f.write(f"- Fallback: {len(pools['fallback'])}\n")
            f.write(f"- Excluded: {len(pools['excluded'])}\n\n")
            
            # Early entry details
            early_entries = [(t, evaluation_result['decisions'][t]) for t in pools['core'] 
                             if evaluation_result['decisions'][t]['early_entry']]
            if early_entries:
                f.write(f"## Early Entry to CORE (120d)\n\n")
                for t, d in early_entries:
                    f.write(f"- **{t}** ({d['name']}):\n")
                    for r in d['early_entry_reasons']:
                        f.write(f"  - {r}\n")
                f.write(f"\n")
            
            for pool_name in ['core', 'enhanced', 'watch', 'fallback', 'excluded']:
                if pools[pool_name]:
                    f.write(f"## {pool_name.upper()} Pool\n\n")
                    for t in pools[pool_name]:
                        d = evaluation_result['decisions'][t]
                        f.write(f"- **{t}** ({d['name']}): ")
                        if d['reasons']:
                            f.write(f"{' | '.join(d['reasons'])}\n")
                        else:
                            f.write(f"OK\n")
                    f.write(f"\n")
        
        return {
            'csv': f'{output_dir}/universe_governance_{eval_date}.csv',
            'md': f'{output_dir}/universe_governance_{eval_date}.md',
        }


if __name__ == '__main__':
    builder = UniverseBuilder()
    
    experimental_pool = {**CORE_UNIVERSE}
    experimental_pool['516120.SH'] = 'Chemical ETF'
    experimental_pool['516960.SH'] = 'Machinery ETF'
    experimental_pool['516650.SH'] = 'Non-ferrous Metal ETF'
    
    # 在2021-06-01评估
    result = builder.evaluate_at_date(experimental_pool, '2021-06-01')
    files = builder.generate_report(result)
    
    print(f"=== Evaluation at 2021-06-01 ===")
    pools = result['pools']
    print(f"Core: {len(pools['core'])} ETFs")
    print(f"Enhanced: {len(pools['enhanced'])} ETFs")
    print(f"Watch: {len(pools['watch'])} ETFs")
    print(f"Fallback: {len(pools['fallback'])} ETFs")
    print(f"Excluded: {len(pools['excluded'])} ETFs")
    
    # Early entries
    early = [t for t in pools['core'] if result['decisions'][t]['early_entry']]
    if early:
        print(f"\n=== Early Entry (120d -> CORE) ===")
        for t in early:
            d = result['decisions'][t]
            print(f"  {t}: {' | '.join(d['early_entry_reasons'])}")
    
    print(f"\n=== Watch Pool (0-120d) ===")
    for t in pools['watch']:
        d = result['decisions'][t]
        print(f"  {t}: {' | '.join(d['reasons'])}")
    
    print(f"\n=== Enhanced Pool (120-250d) ===")
    for t in pools['enhanced']:
        d = result['decisions'][t]
        print(f"  {t}: {' | '.join(d['reasons'])}")
    
    # Walk-forward
    print(f"\n=== Walk-forward (2021-06 to 2023-06) ===")
    wf = builder.walk_forward_validation(experimental_pool, '2021-06-01', '2023-06-01', step_months=12)
    for r in wf:
        print(f"\n{r['eval_date']}: Core={len(r['pools']['core'])}, Enhanced={len(r['pools']['enhanced'])}, Watch={len(r['pools']['watch'])}, Fallback={len(r['pools']['fallback'])}, Excluded={len(r['pools']['excluded'])}")
        early = [t for t in r['pools']['core'] if r['decisions'][t]['early_entry']]
        if early:
            print(f"  Early entry: {early}")
    
    print(f"\nReports: {files}")
