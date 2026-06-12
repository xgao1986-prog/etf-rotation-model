"""
Streamlit可视化界面 - ETF轮动策略仪表盘
运行方式: streamlit run app.py
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import sys
import os

# 添加src到路径
sys.path.insert(0, 'src')

from config import ETF_UNIVERSE, BENCHMARK, STRATEGY_CONFIG, BACKTEST_CONFIG
from database import ETFDatabase
from strategy import StrategyEngine
from backtest import BacktestEngine

# 页面配置
st.set_page_config(
    page_title="ETF轮动策略",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==================== 侧边栏 - 参数调整 ====================
st.sidebar.title("⚙️ 策略参数")

# 评分权重
st.sidebar.markdown("**评分权重**")
weights = {}
col1, col2 = st.sidebar.columns(2)
weights['trend'] = col1.slider("趋势", 0.0, 1.0, 0.30, 0.05)
weights['confirm'] = col2.slider("确认", 0.0, 1.0, 0.20, 0.05)
weights['momentum'] = col1.slider("动量", 0.0, 1.0, 0.25, 0.05)
weights['volume'] = col2.slider("成交量", 0.0, 1.0, 0.15, 0.05)
weights['volatility'] = col1.slider("波动率", 0.0, 1.0, 0.10, 0.05)

# 归一化权重
total_w = sum(weights.values())
if total_w > 0:
    weights = {k: v/total_w for k, v in weights.items()}

st.sidebar.markdown(f"**权重合计: {total_w:.2f}**")

# 阈值参数
st.sidebar.markdown("**入场阈值**")
min_trend = st.sidebar.slider("趋势最低分", 0, 30, 15)
min_confirm = st.sidebar.slider("确认最低分", 0, 20, 4)
min_total = st.sidebar.slider("总评分最低分", 0, 100, 40)

# 持仓控制
st.sidebar.markdown("**持仓控制**")
max_holdings = st.sidebar.slider("最大持仓数", 1, 10, 5)
max_per_etf = st.sidebar.slider("单只上限(%)", 5, 50, 15) / 100

# 风控
st.sidebar.markdown("**风控参数**")
stop_loss = st.sidebar.slider("止损线(%)", -20, -1, -8) / 100

# 大盘择时
st.sidebar.markdown("**大盘择时**")
use_timing = st.sidebar.checkbox("启用大盘择时", True)

# 构建配置
cfg = STRATEGY_CONFIG.copy()
cfg['weights'] = weights
cfg['min_trend_score'] = min_trend
cfg['min_confirm_score'] = min_confirm
cfg['min_total_score'] = min_total
cfg['max_holdings'] = max_holdings
cfg['max_position_per_etf'] = max_per_etf
cfg['stop_loss'] = stop_loss
cfg['market_timing'] = use_timing

# ==================== 主界面 ====================
st.title("📈 A股ETF轮动量化策略")

# 标签页
tab1, tab2, tab3, tab4 = st.tabs(["最新信号", "回测结果", "数据状态", "参数说明"])

# ==================== Tab 1: 最新信号 ====================
with tab1:
    st.header("最新交易信号")
    
    db = ETFDatabase()
    latest = db.get_latest_date()
    
    if not latest:
        st.warning("数据库无数据，请先运行数据更新")
    else:
        st.info(f"数据最新日期: {latest}")
        
        # 获取最新评分
        engine = StrategyEngine(cfg)
        signals = engine.get_latest_signals(db)
        
        if signals.empty:
            st.warning("当前无买入信号")
        else:
            # 显示信号卡片
            cols = st.columns(min(3, len(signals)))
            
            for i, (_, row) in enumerate(signals.iterrows()):
                ticker = row['ticker']
                name = ETF_UNIVERSE.get(ticker, ticker)
                
                with cols[i % 3]:
                    st.metric(
                        label=f"{name}",
                        value=f"{row['total_score']:.1f}分",
                        delta=f"趋势{row['trend_score']:.0f}/动量{row['momentum_rank']:.1f}"
                    )
            
            # 详细表格
            st.subheader("详细评分")
            display_df = signals[['ticker', 'total_score', 'trend_score', 'confirm_score', 
                                   'momentum_rank', 'volume_score', 'vol_score', 'ma20', 'close']].copy()
            display_df['ticker'] = display_df['ticker'].map(lambda x: f"{ETF_UNIVERSE.get(x, x)}({x})")
            display_df.columns = ['标的', '总分', '趋势', '确认', '动量', '成交量', '波动率', 'MA20', '收盘价']
            st.dataframe(display_df, use_container_width=True)

# ==================== Tab 2: 回测结果 ====================
with tab2:
    st.header("策略回测")
    
    col1, col2 = st.columns([1, 3])
    
    with col1:
        st.markdown("**回测区间**")
        sample_type = st.radio("选择区间", ["全区间", "样本内(2019-2023)", "样本外(2024-至今)"])
        
        if st.button("🚀 运行回测", type="primary"):
            with st.spinner("回测运行中..."):
                db = ETFDatabase()
                market_df = db.get_market_data()
                bench_df = db.get_market_data(ticker=BENCHMARK)
                
                if market_df.empty:
                    st.error("数据库无数据")
                else:
                    engine = BacktestEngine(cfg)
                    
                    if sample_type == "样本内(2019-2023)":
                        result = engine.run_in_sample(market_df, bench_df)
                    elif sample_type == "样本外(2024-至今)":
                        result = engine.run_out_sample(market_df, bench_df)
                    else:
                        result = engine.run(market_df, bench_df)
                    
                    if 'error' in result:
                        st.error(f"回测失败: {result['error']}")
                    else:
                        st.session_state['backtest_result'] = result
                        st.success("回测完成!")
    
    with col2:
        if 'backtest_result' in st.session_state:
            result = st.session_state['backtest_result']
            
            # 绩效指标卡片
            metrics_col1, metrics_col2, metrics_col3, metrics_col4 = st.columns(4)
            metrics_col1.metric("总收益率", f"{result['total_return']:.2%}")
            metrics_col2.metric("年化收益", f"{result['annual_return']:.2%}")
            metrics_col3.metric("夏普比率", f"{result['sharpe_ratio']:.2f}")
            metrics_col4.metric("最大回撤", f"{result['max_drawdown']:.2%}")
            
            # 净值曲线
            nav_df = result['nav_df']
            
            fig = make_subplots(
                rows=2, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.1,
                subplot_titles=('净值曲线', '回撤'),
                row_heights=[0.7, 0.3]
            )
            
            # 策略净值
            fig.add_trace(
                go.Scatter(
                    x=nav_df['date'], 
                    y=nav_df['nav'],
                    name='策略',
                    line=dict(color='blue', width=2)
                ),
                row=1, col=1
            )
            
            # 基准净值
            if 'bench_return' in nav_df.columns and nav_df['bench_return'].notna().any():
                initial_nav = nav_df['nav'].iloc[0]
                bench_nav = initial_nav * (1 + nav_df['bench_return'])
                fig.add_trace(
                    go.Scatter(
                        x=nav_df['date'],
                        y=bench_nav,
                        name='沪深300',
                        line=dict(color='gray', width=1, dash='dash')
                    ),
                    row=1, col=1
                )
            
            # 回撤
            fig.add_trace(
                go.Scatter(
                    x=nav_df['date'],
                    y=nav_df['drawdown'] * 100,
                    name='回撤%',
                    fill='tozeroy',
                    line=dict(color='red', width=1)
                ),
                row=2, col=1
            )
            
            fig.update_layout(
                height=600,
                showlegend=True,
                hovermode='x unified'
            )
            
            st.plotly_chart(fig, use_container_width=True)
            
            # 交易统计
            st.subheader("交易统计")
            trade_col1, trade_col2, trade_col3, trade_col4 = st.columns(4)
            trade_col1.metric("交易次数", result['num_trades'])
            trade_col2.metric("胜率", f"{result['win_rate']:.1%}")
            trade_col3.metric("止损次数", result['stop_loss_count'])
            trade_col4.metric("总佣金", f"¥{result['total_commission']:,.0f}")
            
            # 交易记录
            if not result['trades_df'].empty:
                with st.expander("查看交易记录"):
                    st.dataframe(result['trades_df'], use_container_width=True)

# ==================== Tab 3: 数据状态 ====================
with tab3:
    st.header("数据库状态")
    
    db = ETFDatabase()
    stats = db.get_stats()
    
    col1, col2, col3 = st.columns(3)
    col1.metric("行情数据", f"{stats.get('market_data_count', 0):,}")
    col2.metric("标的数量", stats.get('ticker_count', 0))
    col3.metric("最新日期", stats.get('latest_date', 'N/A'))
    
    # 各标的数据覆盖情况
    st.subheader("数据覆盖")
    tickers = db.get_all_tickers()
    
    coverage_data = []
    for ticker in tickers:
        min_d, max_d = db.get_date_range(ticker)
        coverage_data.append({
            '标的': ETF_UNIVERSE.get(ticker, ticker),
            '代码': ticker,
            '最早': min_d,
            '最新': max_d,
            '天数': (pd.to_datetime(max_d) - pd.to_datetime(min_d)).days if min_d and max_d else 0
        })
    
    coverage_df = pd.DataFrame(coverage_data)
    st.dataframe(coverage_df, use_container_width=True)
    
    # 更新按钮
    if st.button("🔄 更新数据"):
        with st.spinner("更新中..."):
            from data_fetcher import update_latest_data
            count = update_latest_data(db=db)
            st.success(f"已更新 {count} 条记录")
            st.rerun()

# ==================== Tab 4: 参数说明 ====================
with tab4:
    st.header("策略参数说明")
    
    st.markdown("""
    ### 评分体系（满分100）
    
    | 维度 | 权重 | 说明 |
    |------|------|------|
    | 趋势强度 | 30% | 收盘价>20日均线(+15) + >50日均线(+10) + 均线斜率>0(+5) |
    | 趋势确认 | 20% | 连续在20日均线之上的天数×4分，最多5天(20分) |
    | 动量 | 25% | 20日收益率的横截面排名（百分位×25） |
    | 成交量 | 15% | 放量上涨(+15) / 放量(+10) / 普通(+5) |
    | 波动率 | 10% | 适中波动率1-4%(+10) / 较高4-6%(+5) |
    
    ### 入场条件（同时满足）
    1. 趋势得分 ≥ 15
    2. 确认得分 ≥ 4（至少1天在均线之上）
    3. 总评分 ≥ 40
    4. 收盘价 > 20日均线 且 均线斜率 > 0
    
    ### 出场条件（任一满足）
    1. 收盘价跌破20日均线
    2. 单只回撤超过8%（止损）
    
    ### 仓位控制
    - 最多持有5只ETF
    - 单只上限15%
    - 每周五调仓
    
    ### 大盘择时
    - 沪深300 > 20日均线：满仓
    - 20-50日均线之间：半仓
    - < 50日均线：20%防御仓位
    """)
    
    st.subheader("ETF标的池")
    etf_df = pd.DataFrame([
        {'代码': k, '名称': v} 
        for k, v in ETF_UNIVERSE.items()
    ])
    st.dataframe(etf_df, use_container_width=True)

# 页脚
st.markdown("---")
st.caption("A股ETF轮动量化策略 v1.0 | 本地运行版")
