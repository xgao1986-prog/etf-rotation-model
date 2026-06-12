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

from config import DB_PATH, ETF_UNIVERSE, BENCHMARK, BACKTEST_CONFIG
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
    
    # 加载数据 - 只读取ETF_UNIVERSE中的标的，排除基准和概念ETF
    print("加载数据...")
    etf_tickers = list(ETF_UNIVERSE.keys())
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
    
    # 加载数据（限定universe为16只ETF）
    etf_tickers = list(ETF_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=etf_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    if market_df.empty or bench_df.empty:
        print("数据库无数据，请先运行: python main.py update --full")
        return
    
    # 计算评分（先逐ETF计算技术指标，再合并做横截面动量排名）
    engine = StrategyEngine()
    all_scores = []
    for ticker in market_df['ticker'].unique():
        ticker_df = market_df[market_df['ticker'] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        scored = engine.calculate_indicators_and_scores(ticker_df)
        all_scores.append(scored)
    
    if not all_scores:
        print("无有效数据")
        return
    
    scores_df = pd.concat(all_scores, ignore_index=True)
    
    # 横截面动量排名（必须在全universe合并后计算）
    scores_df = engine.rank_all_momentum(scores_df)
    scores_df = engine.compute_total_score(scores_df)
    
    # 过滤列：只保留daily_scores表存在的列
    db_score_cols = ['ticker', 'date', 'ma20', 'ma50', 'ma20_slope', 
                     'above_ma20_days', 'volatility_20', 'momentum_20', 
                     'volume_ratio', 'trend_score', 'confirm_score', 
                     'momentum_rank', 'volume_score', 'vol_score', 'total_score']
    scores_to_save = scores_df[[c for c in db_score_cols if c in scores_df.columns]].copy()
    
    # 保存评分到数据库（解决daily_scores=0的问题）
    saved_scores = db.save_scores(scores_to_save)
    print(f"评分已保存到数据库: {saved_scores} 条")
    
    # 生成信号
    signals_df = engine.generate_signals(scores_df, bench_df)
    
    # 保存信号到数据库（解决trade_signals=0的问题）
    # 构造trade_signals表需要的格式
    signal_records = []
    for _, row in signals_df.iterrows():
        signal_records.append({
            'date': row['date'],
            'ticker': row['ticker'],
            'name': ETF_UNIVERSE.get(row['ticker'], row['ticker']),
            'signal_type': row['signal_type'],
            'close_price': row['close'],
            'ma20': row['ma20'],
            'total_score': row['total_score'],
            'target_weight': 0.0,
            'actual_weight': 0.0,
            'reason': f"评分{row['total_score']:.1f}"
        })
    
    if signal_records:
        signal_df = pd.DataFrame(signal_records)
        saved_signals = db.save_signals(signal_df)
        print(f"信号已保存到数据库: {saved_signals} 条")
    
    # 获取最新日期的买入信号
    latest = scores_df['date'].max()
    latest_signals = signals_df[signals_df['date'] == latest]
    buy_signals = latest_signals[
        latest_signals['signal_type'] == 'BUY'
    ].sort_values('total_score', ascending=False)
    
    if buy_signals.empty:
        print(f"\n{'='*50}")
        print(f"最新交易信号 ({latest})")
        print(f"{'='*50}")
        print("无买入信号（所有ETF评分未达入场阈值）")
        print(f"{'='*50}")
        return
    
    # 显示信号
    print(f"\n{'='*50}")
    print(f"最新交易信号 ({latest})")
    print(f"{'='*50}")
    
    for _, row in buy_signals.iterrows():
        ticker = row['ticker']
        name = ETF_UNIVERSE.get(ticker, ticker)
        print(f"[BUY] {name} ({ticker})")
        print(f"   评分: {row['total_score']:.1f}")
        print(f"   趋势: {row['trend_score']:.0f}/30  确认: {row['confirm_score']:.0f}/20")
        print(f"   动量: {row['momentum_rank']:.1f}/25  成交: {row['volume_score']:.0f}/15")
        print(f"   波动: {row['vol_score']:.0f}/10")
        print()
    
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
