# -*- coding: utf-8 -*-
"""
baseline_recorder.py - 基线记录与验证系统

每次跑回测后，记录完整的"实验条件"，确保任何基线都可以在未来复现。

记录内容：
- 数据源：数据库文件、数据版本、日期范围、adjust_type分布
- 假设条件：执行价格、调仓逻辑、信号计算方式等
- 配置参数：STRATEGY_CONFIG、TRADING_RULES_CONFIG、BACKTEST_CONFIG 的完整快照
- 回测结果：总收益、夏普、回撤等
- 文件清单：所有输出文件的路径
- 环境信息：Python版本、关键库版本
- 可复现脚本：基于记录信息可以重新跑回测的命令
"""

import sys
sys.path.insert(0, 'src')

import json
import os
import hashlib
import sqlite3
import pandas as pd
from datetime import datetime
import importlib

# 记录所有基线的索引文件
BASELINE_INDEX = 'reports/baseline_index.json'


def _get_db_stats():
    """获取数据库统计信息"""
    from config import DB_PATH
    stats = {
        'db_path': DB_PATH,
    }
    
    if os.path.exists(DB_PATH):
        stats['file_size_bytes'] = os.path.getsize(DB_PATH)
        stats['file_size_mb'] = round(os.path.getsize(DB_PATH) / (1024*1024), 2)
        stats['file_mtime'] = datetime.fromtimestamp(os.path.getmtime(DB_PATH)).strftime('%Y-%m-%d %H:%M:%S')
        
        conn = sqlite3.connect(DB_PATH)
        try:
            # 数据行数
            cursor = conn.execute('SELECT COUNT(*) FROM market_data')
            stats['market_data_rows'] = cursor.fetchone()[0]
            
            # 日期范围
            cursor = conn.execute('SELECT MIN(date), MAX(date) FROM market_data')
            min_date, max_date = cursor.fetchone()
            stats['date_range'] = {'min': min_date, 'max': max_date}
            
            # ETF数量
            cursor = conn.execute('SELECT COUNT(DISTINCT ticker) FROM market_data')
            stats['num_tickers'] = cursor.fetchone()[0]
            
            # 标的列表
            cursor = conn.execute('SELECT DISTINCT ticker FROM market_data')
            stats['ticker_list'] = sorted([r[0] for r in cursor.fetchall()])
            
            # adjust_type分布
            cursor = conn.execute('''
                SELECT adjust_type, COUNT(*) as cnt 
                FROM market_data 
                GROUP BY adjust_type
            ''')
            stats['adjust_type_distribution'] = {r[0]: r[1] for r in cursor.fetchall()}
            
            # 每只标的的数据条数
            cursor = conn.execute('''
                SELECT ticker, COUNT(*) as cnt, MIN(date) as min_date, MAX(date) as max_date
                FROM market_data
                GROUP BY ticker
                ORDER BY cnt DESC
            ''')
            stats['ticker_data_counts'] = [
                {'ticker': r[0], 'rows': r[1], 'start': r[2], 'end': r[3]}
                for r in cursor.fetchall()
            ]
            
        finally:
            conn.close()
    else:
        stats['error'] = 'Database file not found'
    
    return stats


def _get_config_snapshot():
    """获取配置快照"""
    from config import (
        STRATEGY_CONFIG, TRADING_RULES_CONFIG, BACKTEST_CONFIG,
        ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, CORE_UNIVERSE,
        CORRELATION_THRESHOLD, DEFENSE_ALLOCATION, FALLBACK_EQUITY_UNIVERSE
    )
    
    # 可选配置（可能不存在）
    optional = {}
    try:
        from config import ENHANCED_UNIVERSE, ENHANCED_START_DATE, ENHANCED_SCORE_BONUS
        optional['enhanced_universe'] = list(ENHANCED_UNIVERSE) if ENHANCED_UNIVERSE else []
        optional['enhanced_start_date'] = ENHANCED_START_DATE
        optional['enhanced_score_bonus'] = ENHANCED_SCORE_BONUS
    except ImportError:
        try:
            from config import CONCEPT_UNIVERSE
            optional['concept_universe'] = {k: v for k, v in CONCEPT_UNIVERSE.items()}
        except ImportError:
            pass
    
    try:
        from config import ENHANCED_TICKERS
        optional['enhanced_tickers'] = list(ENHANCED_TICKERS) if ENHANCED_TICKERS else []
    except ImportError:
        pass
    
    return {
        'strategy_config': dict(STRATEGY_CONFIG),
        'trading_rules_config': dict(TRADING_RULES_CONFIG),
        'backtest_config': dict(BACKTEST_CONFIG),
        'universe': {
            'etf_universe': {k: v for k, v in ETF_UNIVERSE.items()},
            'defense_universe': {k: v for k, v in DEFENSE_UNIVERSE.items()},
            'fallback_equity_universe': {k: v for k, v in FALLBACK_EQUITY_UNIVERSE.items()} if FALLBACK_EQUITY_UNIVERSE else {},
            'benchmark': BENCHMARK,
            'core_universe': list(CORE_UNIVERSE) if CORE_UNIVERSE else [],
            **optional,
        },
        'correlation_threshold': CORRELATION_THRESHOLD,
        'defense_allocation': DEFENSE_ALLOCATION,
    }


def _get_assumptions():
    """获取策略假设条件"""
    from config import STRATEGY_CONFIG, TRADING_RULES_CONFIG
    
    assumptions = {
        'execution_price': 'open_price',  # 回测使用开盘价执行
        'signal_calculation': 'daily_close',  # 每日收盘后计算信号
        'rebalance_logic': f"weekly on {['Monday','Tuesday','Wednesday','Thursday','Friday'][STRATEGY_CONFIG.get('rebalance_weekday', 4)]}",
        'rebalance_freq': STRATEGY_CONFIG.get('rebalance_freq', 'weekly'),
        'cooling_period': TRADING_RULES_CONFIG.get('cooling_period', 0),
        'trailing_stop': TRADING_RULES_CONFIG.get('trailing_stop_mode', 'none'),
        'max_position_per_etf': STRATEGY_CONFIG.get('max_position_per_etf', 0.15),
        'max_holdings': STRATEGY_CONFIG.get('max_holdings', 5),
        'stop_loss': STRATEGY_CONFIG.get('stop_loss', -0.08),
        'market_timing': STRATEGY_CONFIG.get('market_timing', False),
        'commission': {
            'rate': STRATEGY_CONFIG.get('commission_rate', 0.0003),
            'min': STRATEGY_CONFIG.get('min_commission', 5.0),
        },
        'defense_module': True,  # 防御模块是否启用（由策略引擎判断）
        'fallback_equity': True,  # 宽基补仓是否启用（由策略引擎判断）
        'correlation_dedup': True,  # 相关性去重是否启用
    }
    
    return assumptions


def _get_environment_info():
    """获取环境信息"""
    import platform
    import subprocess
    
    env = {
        'python_version': platform.python_version(),
        'platform': platform.platform(),
    }
    
    # 尝试获取关键库版本
    for lib in ['pandas', 'numpy', 'sqlite3']:
        try:
            mod = importlib.import_module(lib)
            env[f'{lib}_version'] = getattr(mod, '__version__', 'unknown')
        except Exception:
            env[f'{lib}_version'] = 'unknown'
    
    return env


def record_baseline(results, output_files=None, notes=None):
    """
    记录一条基线
    
    Parameters:
        results: 回测结果字典（BacktestEngine.run()的返回值）
        output_files: 生成的文件路径列表
        notes: 用户备注
    
    Returns:
        baseline_id: 基线唯一标识
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    baseline_id = f'baseline_{timestamp}'
    
    # 构建完整记录
    record = {
        'baseline_id': baseline_id,
        'timestamp': timestamp,
        'notes': notes or '',
        
        # 1. 数据源
        'data_source': _get_db_stats(),
        
        # 2. 假设条件
        'assumptions': _get_assumptions(),
        
        # 3. 配置快照
        'config': _get_config_snapshot(),
        
        # 4. 回测结果
        'backtest_results': {
            'total_return': results.get('total_return'),
            'annual_return': results.get('annual_return'),
            'volatility': results.get('volatility'),
            'sharpe_ratio': results.get('sharpe_ratio'),
            'sortino_ratio': results.get('sortino_ratio'),
            'max_drawdown': results.get('max_drawdown'),
            'num_trades': results.get('num_trades'),
            'win_rate': results.get('win_rate'),
            'avg_win': results.get('avg_win'),
            'avg_loss': results.get('avg_loss'),
            'total_commission': results.get('total_commission'),
            'stop_loss_count': results.get('stop_loss_count'),
            'avg_holdings': results.get('avg_holdings'),
            'max_holdings': results.get('max_holdings'),
        },
        
        # 5. 输出文件
        'output_files': output_files or [],
        
        # 6. 环境信息
        'environment': _get_environment_info(),
        
        # 7. 可复现命令
        'reproduce_command': f'python baseline_recorder.py --reproduce {baseline_id}',
    }
    
    # 保存到单个文件
    record_path = f'reports/{baseline_id}.json'
    with open(record_path, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=2, default=str)
    
    # 更新索引
    _update_index(record)
    
    return baseline_id, record_path


def _update_index(record):
    """更新基线索引"""
    index = []
    if os.path.exists(BASELINE_INDEX):
        try:
            with open(BASELINE_INDEX, 'r', encoding='utf-8') as f:
                index = json.load(f)
        except Exception:
            index = []
    
    # 只保存摘要信息
    summary = {
        'baseline_id': record['baseline_id'],
        'timestamp': record['timestamp'],
        'notes': record['notes'],
        'total_return': record['backtest_results']['total_return'],
        'sharpe_ratio': record['backtest_results']['sharpe_ratio'],
        'max_drawdown': record['backtest_results']['max_drawdown'],
        'num_trades': record['backtest_results']['num_trades'],
        'rebalance_weekday': record['config']['strategy_config'].get('rebalance_weekday', 4),
        'cooling_period': record['config']['trading_rules_config'].get('cooling_period', 0),
        'trailing_stop': record['config']['trading_rules_config'].get('trailing_stop_mode', 'none'),
        'max_position': record['config']['strategy_config'].get('max_position_per_etf', 0.15),
        'data_date_range': record['data_source'].get('date_range', {}),
        'record_file': f'reports/{record["baseline_id"]}.json',
    }
    
    index.append(summary)
    
    with open(BASELINE_INDEX, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2, default=str)


def list_baselines():
    """列出所有基线"""
    if not os.path.exists(BASELINE_INDEX):
        print("暂无基线记录")
        return []
    
    with open(BASELINE_INDEX, 'r', encoding='utf-8') as f:
        index = json.load(f)
    
    print(f"\n{'='*120}")
    print(f"{'ID':<25} {'时间':<16} {'收益':>8} {'夏普':>6} {'回撤':>8} {'交易':>6} {'调仓日':>6} {'冷静期':>6} {'止盈':>8} {'仓位':>6} {'备注':<30}")
    print(f"{'-'*120}")
    
    for b in index:
        weekday_names = ['周一','周二','周三','周四','周五','周六','周日']
        wd = weekday_names[b.get('rebalance_weekday', 4)]
        print(f"{b['baseline_id']:<25} {b['timestamp']:<16} {b['total_return']:>+7.2%} {b['sharpe_ratio']:>6.2f} {b['max_drawdown']:>7.2%} {b['num_trades']:>6} {wd:>6} {b.get('cooling_period', 0):>6} {b.get('trailing_stop', 'none'):>8} {b.get('max_position', 0.15):>5.0%} {b.get('notes', '')[:30]}")
    
    print(f"{'='*120}")
    print(f"共 {len(index)} 条基线记录")
    
    return index


def get_baseline(baseline_id):
    """获取完整基线记录"""
    record_path = f'reports/{baseline_id}.json'
    if not os.path.exists(record_path):
        # 从索引中查找
        if os.path.exists(BASELINE_INDEX):
            with open(BASELINE_INDEX, 'r', encoding='utf-8') as f:
                index = json.load(f)
            for b in index:
                if b['baseline_id'] == baseline_id:
                    record_path = b.get('record_file', f'reports/{baseline_id}.json')
                    break
    
    if not os.path.exists(record_path):
        raise FileNotFoundError(f"基线记录不存在: {baseline_id}")
    
    with open(record_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def reproduce_baseline(baseline_id):
    """
    基于基线记录重新跑回测，验证可复现性
    
    注意：如果数据库已修改，结果可能不完全一致
    """
    print(f"\n正在复现基线: {baseline_id}")
    record = get_baseline(baseline_id)
    
    # 检查数据库
    db_path = record['data_source']['db_path']
    if not os.path.exists(db_path):
        print(f"警告：数据库文件不存在: {db_path}")
        return None
    
    # 检查数据库是否被修改
    current_mtime = datetime.fromtimestamp(os.path.getmtime(db_path)).strftime('%Y-%m-%d %H:%M:%S')
    recorded_mtime = record['data_source'].get('file_mtime', '')
    if current_mtime != recorded_mtime:
        print(f"警告：数据库自记录以来已被修改")
        print(f"  记录时: {recorded_mtime}")
        print(f"  当前:  {current_mtime}")
    
    # 重新加载配置并运行回测
    from config import STRATEGY_CONFIG
    from backtest import BacktestEngine
    from database import ETFDatabase
    
    db = ETFDatabase()
    market_df = db.get_market_data()
    bench_df = db.get_market_data(ticker=record['config']['universe']['benchmark'])
    
    engine = BacktestEngine(STRATEGY_CONFIG)
    results = engine.run(market_df, bench_df)
    
    # 对比结果
    print(f"\n{'='*60}")
    print("复现结果对比")
    print(f"{'='*60}")
    print(f"{'指标':<20} {'记录值':>12} {'复现值':>12} {'差异':>12}")
    print(f"{'-'*60}")
    
    old = record['backtest_results']
    new = results
    
    for key in ['total_return', 'sharpe_ratio', 'max_drawdown', 'num_trades', 'win_rate']:
        old_val = old.get(key, 0)
        new_val = new.get(key, 0)
        diff = new_val - old_val if old_val is not None else 0
        if key in ['total_return', 'max_drawdown', 'win_rate']:
            print(f"{key:<20} {old_val:>+11.4%} {new_val:>+11.4%} {diff:>+11.4%}")
        else:
            print(f"{key:<20} {old_val:>12.4f} {new_val:>12.4f} {diff:>+12.4f}")
    
    print(f"{'='*60}")
    
    return results


def verify_baseline(baseline_id, tolerance=1e-6):
    """
    验证基线可复现性，返回是否通过
    
    Parameters:
        baseline_id: 基线ID
        tolerance: 允许的误差（默认1e-6表示完全匹配）
    """
    results = reproduce_baseline(baseline_id)
    if results is None:
        return False
    
    record = get_baseline(baseline_id)
    old = record['backtest_results']
    
    checks = [
        ('total_return', tolerance),
        ('sharpe_ratio', tolerance),
        ('max_drawdown', tolerance),
        ('num_trades', 0),  # 交易次数必须完全匹配
    ]
    
    all_pass = True
    for key, tol in checks:
        old_val = old.get(key, 0)
        new_val = results.get(key, 0)
        diff = abs(new_val - old_val) if old_val is not None else 0
        passed = diff <= tol
        status = 'PASS' if passed else 'FAIL'
        print(f"  [{status}] {key}: record={old_val:.6f}, reproduce={new_val:.6f}, diff={diff:.6f}")
        if not passed:
            all_pass = False
    
    return all_pass


def generate_baseline_report(baseline_id):
    """生成基线可读报告"""
    record = get_baseline(baseline_id)
    
    md = f"""# 基线记录报告：{baseline_id}

> 生成时间：{record['timestamp']}  
> 备注：{record['notes'] or '无'}

---

## 一、数据源信息

| 项目 | 值 |
|------|------|
| 数据库路径 | `{record['data_source']['db_path']}` |
| 文件大小 | {record['data_source'].get('file_size_mb', 'N/A')} MB |
| 最后修改时间 | {record['data_source'].get('file_mtime', 'N/A')} |
| 数据行数 | {record['data_source'].get('market_data_rows', 'N/A')} |
| 日期范围 | {record['data_source'].get('date_range', {}).get('min', 'N/A')} ~ {record['data_source'].get('date_range', {}).get('max', 'N/A')} |
| ETF数量 | {record['data_source'].get('num_tickers', 'N/A')} |

### 数据调整类型分布

| adjust_type | 行数 |
|-------------|------|
"""
    
    for adj_type, cnt in record['data_source'].get('adjust_type_distribution', {}).items():
        md += f"| {adj_type} | {cnt} |\n"
    
    md += f"""
### 每只标的的数据条数

| 标的 | 行数 | 起始日期 | 结束日期 |
|------|------|----------|----------|
"""
    
    for t in record['data_source'].get('ticker_data_counts', [])[:20]:  # 只展示前20
        md += f"| {t['ticker']} | {t['rows']} | {t['start']} | {t['end']} |\n"
    
    md += f"""
---

## 二、假设条件

| 假设 | 值 | 说明 |
|------|------|------|
| 执行价格 | {record['assumptions']['execution_price']} | 回测使用开盘价执行 |
| 信号计算 | {record['assumptions']['signal_calculation']} | 每日收盘后计算 |
| 调仓逻辑 | {record['assumptions']['rebalance_logic']} | {record['assumptions']['rebalance_freq']} |
| 冷静期 | {record['assumptions']['cooling_period']} | 止损后冷却天数 |
| 动态止盈 | {record['assumptions']['trailing_stop']} | 动态止盈模式 |
| 单只最大仓位 | {record['assumptions']['max_position_per_etf']:.0%} | 单只ETF最大占比 |
| 最大持仓数 | {record['assumptions']['max_holdings']} | 同时最多持仓 |
| 止损线 | {record['assumptions']['stop_loss']:.1%} | 固定止损 |
| 大盘择时 | {record['assumptions']['market_timing']} | 是否启用大盘择时 |
| 佣金率 | {record['assumptions']['commission']['rate']:.4%} | 最低{record['assumptions']['commission']['min']}元 |
| 防御模块 | {record['assumptions']['defense_module']} | 是否启用防御资产 |
| 宽基补仓 | {record['assumptions']['fallback_equity']} | 是否启用宽基补仓 |
| 相关性去重 | {record['assumptions']['correlation_dedup']} | 是否启用相关性去重 |

---

## 三、配置快照

### STRATEGY_CONFIG

```json
{json.dumps(record['config']['strategy_config'], ensure_ascii=False, indent=2)}
```

### TRADING_RULES_CONFIG

```json
{json.dumps(record['config']['trading_rules_config'], ensure_ascii=False, indent=2)}
```

### BACKTEST_CONFIG

```json
{json.dumps(record['config']['backtest_config'], ensure_ascii=False, indent=2)}
```

---

## 四、回测结果

| 指标 | 值 |
|------|------|
| 总收益率 | {record['backtest_results']['total_return']:+.2%} |
| 年化收益率 | {record['backtest_results']['annual_return']:+.2%} |
| 波动率 | {record['backtest_results']['volatility']:.2%} |
| 夏普比率 | {record['backtest_results']['sharpe_ratio']:.2f} |
| 索提诺比率 | {record['backtest_results']['sortino_ratio']:.2f} |
| 最大回撤 | {record['backtest_results']['max_drawdown']:.2%} |
| 交易次数 | {record['backtest_results']['num_trades']} |
| 胜率 | {record['backtest_results']['win_rate']:.1%} |
| 平均盈利 | {record['backtest_results']['avg_win']:.2%} |
| 平均亏损 | {record['backtest_results']['avg_loss']:.2%} |
| 总佣金 | ¥{record['backtest_results']['total_commission']:.2f} |
| 止损次数 | {record['backtest_results']['stop_loss_count']} |
| 平均持仓 | {record['backtest_results']['avg_holdings']:.1f}只 |
| 最大持仓 | {record['backtest_results']['max_holdings']}只 |

---

## 五、输出文件

"""
    
    for f in record['output_files']:
        md += f"- `{f}`\n"
    
    md += f"""
---

## 六、环境信息

| 项目 | 值 |
|------|------|
| Python版本 | {record['environment'].get('python_version', 'N/A')} |
| 平台 | {record['environment'].get('platform', 'N/A')} |
| Pandas版本 | {record['environment'].get('pandas_version', 'N/A')} |
| NumPy版本 | {record['environment'].get('numpy_version', 'N/A')} |

---

## 七、复现命令

```bash
python baseline_recorder.py --reproduce {baseline_id}
```

或验证可复现性：

```bash
python baseline_recorder.py --verify {baseline_id}
```

---

> 注意：如果数据库文件已被修改，复现结果可能与记录值不同。建议在修改数据库前先备份。
"""
    
    report_path = f'reports/{baseline_id}_report.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(md)
    
    return report_path


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='基线记录与验证系统')
    parser.add_argument('--list', action='store_true', help='列出所有基线')
    parser.add_argument('--reproduce', type=str, help='复现指定基线')
    parser.add_argument('--verify', type=str, help='验证指定基线可复现性')
    parser.add_argument('--report', type=str, help='生成指定基线的可读报告')
    
    args = parser.parse_args()
    
    if args.list:
        list_baselines()
    elif args.reproduce:
        reproduce_baseline(args.reproduce)
    elif args.verify:
        verify_baseline(args.verify)
    elif args.report:
        path = generate_baseline_report(args.report)
        print(f"报告已生成: {path}")
    else:
        print("用法：")
        print("  python baseline_recorder.py --list              # 列出所有基线")
        print("  python baseline_recorder.py --reproduce <id>    # 复现基线")
        print("  python baseline_recorder.py --verify <id>         # 验证基线")
        print("  python baseline_recorder.py --report <id>         # 生成报告")
