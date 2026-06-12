"""
参数扫描脚本 - 批量测试不同因子组合
用于优化: 调仓日、调仓周期、冷静期等因子

用法:
    python scripts/parameter_scan.py --mode grid      # 网格搜索（小范围）
    python scripts/parameter_scan.py --mode random --n 50   # 随机采样50组
    python scripts/parameter_scan.py --mode single --freq weekly --weekday 3 --cooling 5
"""

import sys
import os
import argparse
import pandas as pd
import numpy as np
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from src.config import build_config, FACTOR_SPACE, FACTOR_CONFIG, BACKTEST_CONFIG
from src.backtest import BacktestEngine
from src.database import ETFDatabase


def run_single_backtest(factor_cfg, market_df, bench_df, sector_df=None, label=""):
    """运行单次回测，返回结果摘要"""
    cfg = build_config(factor_cfg=factor_cfg)
    engine = BacktestEngine(cfg)
    
    try:
        result = engine.run(market_df, bench_df, sector_df)
        
        if 'error' in result:
            return None
        
        return {
            'label': label,
            'factor_cfg': factor_cfg,
            'total_return': result['total_return'],
            'annual_return': result['annual_return'],
            'sharpe_ratio': result['sharpe_ratio'],
            'sortino_ratio': result['sortino_ratio'],
            'max_drawdown': result['max_drawdown'],
            'volatility': result['volatility'],
            'num_trades': result['num_trades'],
            'win_rate': result['win_rate'],
            'avg_holdings': result['avg_holdings'],
            'stop_loss_count': result['stop_loss_count'],
        }
    except Exception as e:
        print(f"  [ERROR] {label}: {e}")
        return None


def generate_test_combinations(mode='grid', n_samples=50, seed=42):
    """生成待测试的因子组合"""
    
    if mode == 'single':
        # 单组测试（用当前FACTOR_CONFIG）
        return [FACTOR_CONFIG.copy()]
    
    elif mode == 'grid':
        # 小范围网格搜索（控制组合数）
        combinations = []
        
        # 调仓频率（先只测weekly，减少组合数）
        freqs = ['weekly']
        
        # 调仓日
        weekdays = [0, 1, 2, 3, 4]  # 周一到周五
        
        # 冷静期（精简）
        cooling_periods = [0, 5, 10]
        
        # 冷静期评分提升（固定一个值，减少组合数）
        cooling_boosts = [10]
        
        for freq in freqs:
            for weekday in weekdays:
                for cooling in cooling_periods:
                    for boost in cooling_boosts:
                        combo = {
                            'rebalance_freq': freq,
                            'rebalance_weekday': weekday,
                            'rebalance_ordinal': 1,
                            'cooling_period': cooling,
                            'cooling_score_boost': boost,
                        }
                        combinations.append(combo)
        
        print(f"网格搜索: {len(combinations)} 种组合")
        return combinations
    
    elif mode == 'random':
        # 随机采样
        import random
        random.seed(seed)
        
        combinations = []
        for _ in range(n_samples):
            combo = {
                'rebalance_freq': random.choice(['weekly', 'biweekly', 'monthly']),
                'rebalance_weekday': random.randint(0, 4),
                'rebalance_ordinal': random.choice([1, 2]),
                'cooling_period': random.randint(0, 20),
                'cooling_score_boost': random.randint(0, 30),
            }
            combinations.append(combo)
        
        print(f"随机采样: {n_samples} 种组合")
        return combinations
    
    else:
        raise ValueError(f"未知模式: {mode}")


def main():
    parser = argparse.ArgumentParser(description='ETF轮动策略 - 因子参数扫描')
    parser.add_argument('--mode', type=str, default='grid', choices=['grid', 'random', 'single'],
                        help='扫描模式: grid=网格搜索, random=随机采样, single=单组测试')
    parser.add_argument('--n', type=int, default=50, help='随机采样时的组合数')
    parser.add_argument('--seed', type=int, default=42, help='随机种子')
    parser.add_argument('--sample', type=str, default='all', choices=['in', 'out', 'all'],
                        help='回测区间: in=样本内, out=样本外, all=全区间')
    parser.add_argument('--top', type=int, default=10, help='输出前N个最佳组合')
    parser.add_argument('--save', type=str, default=None, help='保存结果到CSV文件')
    
    # 单组测试专用参数
    parser.add_argument('--freq', type=str, default=None, help='调仓频率 (weekly/biweekly/monthly)')
    parser.add_argument('--weekday', type=int, default=None, help='调仓日 (0-4)')
    parser.add_argument('--cooling', type=int, default=None, help='冷静期 (0-20)')
    parser.add_argument('--boost', type=int, default=None, help='冷静期评分提升 (0-30)')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("ETF轮动策略 - 因子参数扫描")
    print("=" * 60)
    
    # 加载数据
    print("\n[1/4] 加载数据...")
    db = ETFDatabase()
    market_df = db.get_market_data()
    
    # 基准数据从market_data中过滤（BENCHMARK = '000300.SH'）
    from src.config import BENCHMARK
    bench_df = db.get_market_data(ticker=BENCHMARK)
    sector_df = db.get_sector_data()
    
    if market_df.empty or bench_df.empty:
        print("错误: 数据库中没有数据，请先运行数据更新")
        return
    
    # 从market_df中排除基准数据
    market_df = market_df[market_df['ticker'] != BENCHMARK]
    
    print(f"  ETF数据: {len(market_df)} 条, {market_df['ticker'].nunique()} 只")
    print(f"  基准数据: {len(bench_df)} 条 ({BENCHMARK})")
    print(f"  板块数据: {len(sector_df)} 条" if not sector_df.empty else "  板块数据: 无")
    
    # 根据sample参数过滤数据
    if args.sample == 'in':
        end = BACKTEST_CONFIG['in_sample_end']
        market_df = market_df[market_df['date'] <= end]
        bench_df = bench_df[bench_df['date'] <= end]
        if not sector_df.empty:
            sector_df = sector_df[sector_df['date'] <= end]
        print(f"  样本内区间: ~{end}")
    elif args.sample == 'out':
        start = BACKTEST_CONFIG['out_sample_start']
        market_df = market_df[market_df['date'] >= start]
        bench_df = bench_df[bench_df['date'] >= start]
        if not sector_df.empty:
            sector_df = sector_df[sector_df['date'] >= start]
        print(f"  样本外区间: {start}~")
    
    # 生成因子组合
    print(f"\n[2/4] 生成因子组合 (模式={args.mode})...")
    
    if args.mode == 'single' and (args.freq or args.weekday is not None):
        # 使用命令行参数覆盖
        combo = FACTOR_CONFIG.copy()
        if args.freq:
            combo['rebalance_freq'] = args.freq
        if args.weekday is not None:
            combo['rebalance_weekday'] = args.weekday
        if args.cooling is not None:
            combo['cooling_period'] = args.cooling
        if args.boost is not None:
            combo['cooling_score_boost'] = args.boost
        combinations = [combo]
    else:
        combinations = generate_test_combinations(args.mode, args.n, args.seed)
    
    # 运行回测
    print(f"\n[3/4] 运行回测 ({len(combinations)} 组)...")
    results = []
    
    for i, factor_cfg in enumerate(combinations):
        freq = factor_cfg['rebalance_freq']
        weekday = factor_cfg['rebalance_weekday']
        cooling = factor_cfg['cooling_period']
        boost = factor_cfg['cooling_score_boost']
        
        label = f"[{i+1}/{len(combinations)}] {freq} 周{weekday+1} 冷静{cooling}天 提升{boost}"
        print(f"  {label}...", end=" ")
        
        result = run_single_backtest(factor_cfg, market_df, bench_df, sector_df, label)
        
        if result:
            print(f"夏普={result['sharpe_ratio']:.3f} 收益={result['total_return']:.2%} 回撤={result['max_drawdown']:.2%}")
            results.append(result)
        else:
            print("失败")
    
    if not results:
        print("\n错误: 所有回测均失败")
        return
    
    # 整理结果
    print(f"\n[4/4] 整理结果 ({len(results)} 组成功)...")
    
    results_df = pd.DataFrame(results)
    
    # 按夏普比率排序
    results_df = results_df.sort_values('sharpe_ratio', ascending=False).reset_index(drop=True)
    
    # 展开factor_cfg为独立列
    factor_cols = pd.DataFrame(results_df['factor_cfg'].tolist())
    results_df = pd.concat([factor_cols, results_df.drop('factor_cfg', axis=1)], axis=1)
    
    # 输出前N名
    print(f"\n{'='*60}")
    print(f"Top {args.top} 最佳因子组合 (按夏普比率排序)")
    print(f"{'='*60}")
    
    display_cols = ['rebalance_freq', 'rebalance_weekday', 'cooling_period', 
                    'cooling_score_boost', 'total_return', 'annual_return', 
                    'sharpe_ratio', 'max_drawdown', 'num_trades', 'win_rate']
    
    top_df = results_df[display_cols].head(args.top)
    
    # 格式化输出
    for idx, row in top_df.iterrows():
        weekday_names = ['周一', '周二', '周三', '周四', '周五']
        print(f"\n  排名 #{idx+1}:")
        print(f"    调仓: {row['rebalance_freq']} / {weekday_names[int(row['rebalance_weekday'])]}")
        print(f"    冷静期: {int(row['cooling_period'])}天 / 评分提升: {int(row['cooling_score_boost'])}")
        print(f"    收益: {row['total_return']:.2%} (年化{row['annual_return']:.2%})")
        print(f"    夏普: {row['sharpe_ratio']:.3f} / 回撤: {row['max_drawdown']:.2%}")
        print(f"    交易: {int(row['num_trades'])}次 / 胜率: {row['win_rate']:.1%}")
    
    # 保存结果
    if args.save:
        save_path = args.save
        results_df.to_csv(save_path, index=False, encoding='utf-8-sig')
        print(f"\n  结果已保存: {save_path}")
    
    # 统计洞察
    print(f"\n{'='*60}")
    print("因子统计洞察")
    print(f"{'='*60}")
    
    # 最佳调仓日
    best_by_weekday = results_df.groupby('rebalance_weekday')['sharpe_ratio'].mean().sort_values(ascending=False)
    weekday_names = ['周一', '周二', '周三', '周四', '周五']
    print(f"\n  按调仓日平均夏普:")
    for wd, sharpe in best_by_weekday.items():
        print(f"    {weekday_names[int(wd)]}: {sharpe:.3f}")
    
    # 最佳调仓频率
    if 'rebalance_freq' in results_df.columns and results_df['rebalance_freq'].nunique() > 1:
        best_by_freq = results_df.groupby('rebalance_freq')['sharpe_ratio'].mean().sort_values(ascending=False)
        print(f"\n  按调仓频率平均夏普:")
        for freq, sharpe in best_by_freq.items():
            print(f"    {freq}: {sharpe:.3f}")
    
    # 最佳冷静期
    best_by_cooling = results_df.groupby('cooling_period')['sharpe_ratio'].mean().sort_values(ascending=False).head(5)
    print(f"\n  按冷静期平均夏普 (Top 5):")
    for cp, sharpe in best_by_cooling.items():
        print(f"    {int(cp)}天: {sharpe:.3f}")
    
    print(f"\n{'='*60}")
    print("扫描完成")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
