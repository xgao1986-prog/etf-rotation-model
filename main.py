"""
主入口 - ETF轮动策略
支持: 数据更新、回测、信号生成、状态查看
"""

import sys
import argparse
import pandas as pd
from datetime import datetime

# 添加src到路径
sys.path.insert(0, 'src')

from config import DB_PATH, ETF_UNIVERSE, BENCHMARK, BACKTEST_CONFIG, DEFENSE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE, ALL_TRADABLE_ETFS
from database import ETFDatabase
from data_fetcher import download_all_data, update_latest_data
from strategy import StrategyEngine
from backtest import BacktestEngine


def cmd_update(args):
    """更新数据"""
    db = ETFDatabase()
    
    if args.full:
        print("执行全量数据下载...")
        download_all_data(BACKTEST_CONFIG['start_date'], db=db)
    else:
        print("执行增量更新...")
        count = update_latest_data(db=db)
        if count == 0:
            print("无新数据需要更新")
    
    # 显示统计
    stats = db.get_stats()
    print(f"\n数据库统计:")
    print(f"  行情数据: {stats.get('market_data_count', 0):,} 条")
    print(f"  标的数量: {stats.get('ticker_count', 0)} 只")
    print(f"  日期范围: {stats.get('earliest_date', 'N/A')} ~ {stats.get('latest_date', 'N/A')}")


def cmd_backtest(args):
    """运行回测"""
    db = ETFDatabase()
    
    # 加载数据 - 包含所有三类资产
    print("加载数据...")
    from config import ALL_TRADABLE_ETFS
    etf_tickers = list(ALL_TRADABLE_ETFS.keys())
    market_df = db.get_market_data(ticker=etf_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    if market_df.empty or bench_df.empty:
        print("数据库无数据，请先运行: python main.py update --full")
        return
    
    engine = BacktestEngine()
    
    if args.sample == 'in':
        print("运行样本内回测 (2019-2023)...")
        result = engine.run_in_sample(market_df, bench_df)
    elif args.sample == 'out':
        print("运行样本外验证 (2024-至今)...")
        result = engine.run_out_sample(market_df, bench_df)
    else:
        print("运行全区间回测...")
        result = engine.run(market_df, bench_df)
    
    if 'error' in result:
        print(f"回测失败: {result['error']}")
        return
    
    # 显示结果
    print(f"\n{'='*50}")
    print(f"回测结果")
    print(f"{'='*50}")
    print(f"总收益率:    {result['total_return']:.2%}")
    print(f"年化收益率:  {result['annual_return']:.2%}")
    print(f"年化波动率:  {result['volatility']:.2%}")
    print(f"夏普比率:    {result['sharpe_ratio']:.2f}")
    print(f"索提诺比率:  {result['sortino_ratio']:.2f}")
    print(f"最大回撤:    {result['max_drawdown']:.2%}")
    print(f"交易次数:    {result['num_trades']}")
    print(f"胜率:        {result['win_rate']:.1%}")
    print(f"平均盈利:    {result['avg_win']:.2%}")
    print(f"平均亏损:    {result['avg_loss']:.2%}")
    print(f"总佣金:      {result['total_commission']:,.2f} 元")
    print(f"止损次数:    {result['stop_loss_count']}")
    print(f"平均持仓:    {result['avg_holdings']:.1f} 只")
    print(f"最大持仓:    {result['max_holdings']} 只")
    print(f"{'='*50}")
    
    # v1.2: 显示市场状态分布（observer 模式）
    if 'regime_summary' in result:
        summary = result['regime_summary']
        print(f"\n{'='*50}")
        print(f"市场状态分布 (v1.2 observer)")
        print(f"{'='*50}")
        for state_id, info in summary['state_distribution'].items():
            print(f"  {info['name']}: {info['days']}天 ({info['percentage']:.1%})  置信度:{info['avg_confidence']:.1%}")
        print(f"  状态切换: {summary['switch_count']} 次")
        print(f"{'='*50}")
    
    # 保存结果
    if args.save:
        from config import REPORT_DIR
        import os
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # 保存净值曲线
        nav_path = os.path.join(REPORT_DIR, f'nav_{timestamp}.csv')
        result['nav_df'].to_csv(nav_path, index=False)
        print(f"净值曲线已保存: {nav_path}")
        
        # 保存交易记录
        if not result['trades_df'].empty:
            trades_path = os.path.join(REPORT_DIR, f'trades_{timestamp}.csv')
            result['trades_df'].to_csv(trades_path, index=False)
            print(f"交易记录已保存: {trades_path}")
        
        # 保存回测结果到数据库
        db.save_backtest_result({
            'run_date': datetime.now().strftime('%Y-%m-%d'),
            'start_date': result['nav_df']['date'].min().strftime('%Y-%m-%d'),
            'end_date': result['nav_df']['date'].max().strftime('%Y-%m-%d'),
            'total_return': result['total_return'],
            'annual_return': result['annual_return'],
            'sharpe_ratio': result['sharpe_ratio'],
            'max_drawdown': result['max_drawdown'],
            'num_trades': result['num_trades'],
            'win_rate': result['win_rate'],
            'params_json': str(result['params'])
        })


def cmd_signal(args):
    """生成最新交易信号"""
    db = ETFDatabase()
    
    # 确保数据最新（如果更新失败，继续使用现有数据）
    latest_db = db.get_latest_date()
    today = datetime.now().strftime('%Y-%m-%d')
    
    if not latest_db or latest_db < today:
        print("数据不是最新，尝试执行更新...")
        try:
            update_latest_data(db=db)
        except Exception as e:
            print(f"更新失败（可能数据已存在）: {e}")
            print("继续使用现有数据生成信号...")
    
    # 加载数据（包含所有三类资产）
    from config import ALL_TRADABLE_ETFS, FALLBACK_EQUITY_UNIVERSE, DEFENSE_UNIVERSE
    etf_tickers = list(ALL_TRADABLE_ETFS.keys())
    market_df = db.get_market_data(ticker=etf_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    if market_df.empty or bench_df.empty:
        print("数据库无数据，请先运行: python main.py update --full")
        return
    
    # 计算评分（按回测一致的三层评分逻辑）
    engine = StrategyEngine()
    
    # 分离三类资产
    stock_df = market_df[market_df['ticker'].isin(ETF_UNIVERSE.keys())].copy()
    fallback_df = market_df[market_df['ticker'].isin(FALLBACK_EQUITY_UNIVERSE.keys())].copy()
    defense_df = market_df[market_df['ticker'].isin(DEFENSE_UNIVERSE.keys())].copy()
    
    # 步骤1-2：对行业ETF逐只计算指标和评分
    stock_scores = []
    for ticker in stock_df['ticker'].unique():
        ticker_df = stock_df[stock_df['ticker'] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        scored = engine.calculate_indicators_and_scores(ticker_df)
        stock_scores.append(scored)
    
    if not stock_scores:
        print("无有效行业ETF数据")
        return
    
    scores_df = pd.concat(stock_scores, ignore_index=True)
    
    # 步骤3：行业ETF全池横截面动量排名
    scores_df = engine.rank_all_momentum(scores_df)
    
    # 步骤4：计算行业ETF总评分
    scores_df = engine.compute_total_score(scores_df)
    
    # 步骤5：对宽基补仓ETF单独计算简化评分
    fallback_scores = []
    for ticker in fallback_df['ticker'].unique():
        ticker_df = fallback_df[fallback_df['ticker'] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        scored = engine.calculate_fallback_equity_score(ticker_df)
        fallback_scores.append(scored)
    
    if fallback_scores:
        fallback_scores_df = pd.concat(fallback_scores, ignore_index=True)
        fallback_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
        fallback_scores_df['total_score'] = fallback_scores_df[fallback_cols].fillna(0).sum(axis=1)
        scores_df = pd.concat([scores_df, fallback_scores_df], ignore_index=True)
    
    # 步骤6：对防御资产单独计算简化评分
    defense_scores = []
    for ticker in defense_df['ticker'].unique():
        ticker_df = defense_df[defense_df['ticker'] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        scored = engine.calculate_defense_score(ticker_df)
        defense_scores.append(scored)
    
    if defense_scores:
        defense_scores_df = pd.concat(defense_scores, ignore_index=True)
        defense_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
        defense_scores_df['total_score'] = defense_scores_df[defense_cols].fillna(0).sum(axis=1)
        scores_df = pd.concat([scores_df, defense_scores_df], ignore_index=True)
    
    # 确保所有total_score已计算（防御/宽基已单独计算，这里fillna保险）
    if 'total_score' not in scores_df.columns:
        scores_df['total_score'] = 0
    scores_df['total_score'] = scores_df['total_score'].fillna(0)
    
    # 过滤列：只保留daily_scores表存在的列
    db_score_cols = ['ticker', 'date', 'ma20', 'ma50', 'ma20_slope', 
                     'above_ma20_days', 'volatility_20', 'momentum_20', 
                     'volume_ratio', 'trend_score', 'confirm_score', 
                     'momentum_rank', 'volume_score', 'vol_score', 'total_score']
    scores_to_save = scores_df[[c for c in db_score_cols if c in scores_df.columns]].copy()
    
    # 保存评分到数据库
    saved_scores = db.save_scores(scores_to_save)
    print(f"评分已保存到数据库: {saved_scores} 条")
    
    # 生成信号
    signals_df = engine.generate_signals(scores_df, bench_df)
    
    # 保存信号到数据库
    signal_records = []
    for _, row in signals_df.iterrows():
        ticker = row['ticker']
        if ticker in ETF_UNIVERSE:
            name = ETF_UNIVERSE[ticker]
            asset_type = '行业ETF'
        elif ticker in FALLBACK_EQUITY_UNIVERSE:
            name = FALLBACK_EQUITY_UNIVERSE[ticker]
            asset_type = '宽基补仓'
        elif ticker in DEFENSE_UNIVERSE:
            name = DEFENSE_UNIVERSE[ticker]
            asset_type = '防御资产'
        else:
            name = ticker
            asset_type = '其他'
        
        signal_records.append({
            'date': row['date'],
            'ticker': row['ticker'],
            'name': name,
            'signal_type': row['signal_type'],
            'close_price': row['close'],
            'ma20': row['ma20'],
            'total_score': row['total_score'],
            'target_weight': 0.0,
            'actual_weight': 0.0,
            'reason': f"{asset_type}:评分{row['total_score']:.1f}"
        })
    
    if signal_records:
        signal_df = pd.DataFrame(signal_records)
        saved_signals = db.save_signals(signal_df)
        print(f"信号已保存到数据库: {saved_signals} 条")
    
    # 获取最新日期的买入信号
    latest = scores_df['date'].max()
    
    # v1.2: 检测当前市场状态
    from market_regime import MarketRegimeDetector
    from config import build_config
    regime_cfg = build_config()
    detector = MarketRegimeDetector(regime_cfg)
    stock_df = market_df[market_df['ticker'].isin(ETF_UNIVERSE.keys())].copy()
    regime = detector.detect(bench_df, stock_df)
    
    latest_signals = signals_df[signals_df['date'] == latest]
    buy_signals = latest_signals[
        latest_signals['signal_type'] == 'BUY'
    ].sort_values('total_score', ascending=False)
    
    # 显示市场状态（在信号之前）
    print(f"\n{'='*50}")
    print(f"市场状态 ({latest.strftime('%Y-%m-%d')})")
    print(f"{'='*50}")
    print(f"  状态: {regime['regime_name']} (ID={regime['regime_id']})")
    print(f"  置信度: {regime['confidence']:.1%}")
    print(f"  原因: {regime['reason']}")
    print(f"  趋势位置: {regime['trend_position']:.3f}")
    print(f"  波动率: {regime['vol_20']:.2%} ({regime['vol_regime']})")
    if not pd.isna(regime['market_breadth']):
        print(f"  市场宽度: {regime['market_breadth']:.1%}")
    print(f"{'='*50}")
    
    if buy_signals.empty:
        print(f"\n{'='*50}")
        print(f"最新交易信号 ({latest.strftime('%Y-%m-%d')})")
        print(f"{'='*50}")
        print("无买入信号（所有ETF评分未达入场阈值）")
        print(f"{'='*50}")
        return
    
    # 显示信号（按资产类型分组）
    print(f"\n{'='*50}")
    print(f"最新交易信号 ({latest.strftime('%Y-%m-%d')})")
    print(f"{'='*50}")
    
    # 行业ETF
    stock_buys = buy_signals[buy_signals['ticker'].isin(ETF_UNIVERSE.keys())]
    if not stock_buys.empty:
        print("\n【行业/主题ETF】（第一层 - 核心alpha）")
        for _, row in stock_buys.iterrows():
            ticker = row['ticker']
            name = ETF_UNIVERSE.get(ticker, ticker)
            print(f"  [BUY] {name} ({ticker})")
            print(f"     评分: {row['total_score']:.1f}")
            print(f"     趋势: {row['trend_score']:.0f}/30  确认: {row['confirm_score']:.0f}/20")
            print(f"     动量: {row['momentum_rank']:.1f}/25  成交: {row['volume_score']:.0f}/15")
            print(f"     波动: {row['vol_score']:.0f}/10")
    
    # 宽基补仓
    fallback_buys = buy_signals[buy_signals['ticker'].isin(FALLBACK_EQUITY_UNIVERSE.keys())]
    if not fallback_buys.empty:
        print("\n【宽基补仓ETF】（第二层 - 补足beta）")
        for _, row in fallback_buys.iterrows():
            ticker = row['ticker']
            name = FALLBACK_EQUITY_UNIVERSE.get(ticker, ticker)
            print(f"  [BUY] {name} ({ticker})")
            print(f"     评分: {row['total_score']:.1f}")
    
    # 防御资产
    defense_buys = buy_signals[buy_signals['ticker'].isin(DEFENSE_UNIVERSE.keys())]
    if not defense_buys.empty:
        print("\n【防御资产】（第三层 - 低相关补仓）")
        for _, row in defense_buys.iterrows():
            ticker = row['ticker']
            name = DEFENSE_UNIVERSE.get(ticker, ticker)
            print(f"  [BUY] {name} ({ticker})")
            print(f"     评分: {row['total_score']:.1f}")
    
    print(f"{'='*50}")
    
    # 保存信号
    if args.save:
        from config import SIGNAL_DIR
        import os
        
        signal_path = os.path.join(SIGNAL_DIR, f"{today}_signal.csv")
        buy_signals.to_csv(signal_path, index=False)
        print(f"信号已保存: {signal_path}")


def cmd_status(args):
    """查看项目状态"""
    db = ETFDatabase()
    stats = db.get_stats()
    
    print(f"{'='*50}")
    print(f"ETF轮动策略 - 项目状态")
    print(f"{'='*50}")
    print(f"数据库路径: {DB_PATH}")
    print(f"行情数据:   {stats.get('market_data_count', 0):,} 条")
    print(f"评分数据:   {stats.get('scores_count', 0):,} 条")
    print(f"交易信号:   {stats.get('signals_count', 0)} 条")
    print(f"回测记录:   {stats.get('backtest_count', 0)} 条")
    print(f"标的数量:   {stats.get('ticker_count', 0)} 只")
    print(f"最早日期:   {stats.get('earliest_date', 'N/A')}")
    print(f"最新日期:   {stats.get('latest_date', 'N/A')}")
    print(f"{'='*50}")
    
    # v1.2: 显示当前市场状态
    latest_date = stats.get('latest_date')
    if latest_date:
        from market_regime import MarketRegimeDetector
        from config import build_config, ALL_TRADABLE_ETFS, ETF_UNIVERSE
        
        regime_cfg = build_config()
        detector = MarketRegimeDetector(regime_cfg)
        
        bench_df = db.get_market_data(ticker=BENCHMARK)
        market_df = db.get_market_data(ticker=list(ALL_TRADABLE_ETFS.keys()))
        stock_df = market_df[market_df['ticker'].isin(ETF_UNIVERSE.keys())].copy()
        
        if not bench_df.empty:
            regime = detector.detect(bench_df, stock_df)
            print(f"\n{'='*50}")
            print(f"市场状态 (v1.2)")
            print(f"{'='*50}")
            print(f"  状态: {regime['regime_name']} (ID={regime['regime_id']})")
            print(f"  置信度: {regime['confidence']:.1%}")
            print(f"  原因: {regime['reason']}")
            print(f"  趋势位置: {regime['trend_position']:.3f}")
            print(f"  波动率: {regime['vol_20']:.2%} ({regime['vol_regime']})")
            if not pd.isna(regime['market_breadth']):
                print(f"  市场宽度: {regime['market_breadth']:.1%}")
            print(f"{'='*50}")
    
    # 显示最近日志
    logs = db.get_logs(limit=5)
    if not logs.empty:
        print(f"\n最近日志:")
        for _, log in logs.iterrows():
            print(f"  [{log['log_type']}] {log['message']}")


def main():
    parser = argparse.ArgumentParser(description='ETF轮动量化策略')
    subparsers = parser.add_subparsers(dest='command', help='可用命令')
    
    # update 命令
    update_parser = subparsers.add_parser('update', help='更新数据')
    update_parser.add_argument('--full', action='store_true', help='全量下载')
    
    # backtest 命令
    backtest_parser = subparsers.add_parser('backtest', help='运行回测')
    backtest_parser.add_argument('--sample', choices=['in', 'out', 'all'], 
                                  default='all', help='回测区间')
    backtest_parser.add_argument('--save', action='store_true', help='保存结果')
    
    # signal 命令
    signal_parser = subparsers.add_parser('signal', help='生成交易信号')
    signal_parser.add_argument('--save', action='store_true', help='保存信号')
    
    # status 命令
    subparsers.add_parser('status', help='查看状态')
    
    args = parser.parse_args()
    
    if args.command == 'update':
        cmd_update(args)
    elif args.command == 'backtest':
        cmd_backtest(args)
    elif args.command == 'signal':
        cmd_signal(args)
    elif args.command == 'status':
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
