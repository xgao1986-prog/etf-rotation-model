"""
Streamlit visual app - ETF rotation strategy dashboard v2.0.
Run: streamlit run app.py
"""

import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots


sys.path.insert(0, "src")

from backtest import BacktestEngine
from config import BACKTEST_CONFIG, BENCHMARK, ETF_UNIVERSE, STRATEGY_CONFIG
from database import ETFDatabase
from strategy import StrategyEngine


st.set_page_config(
    page_title="ETF轮动策略 v2.0",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


APP_VERSION = "v2.0"
SCORE_MAX = {
    "trend": 30,
    "confirm": 20,
    "momentum": 25,
    "volume": 15,
    "volatility": 10,
}


def inject_style():
    st.markdown(
        """
        <style>
        .main .block-container {
            padding-top: 1.6rem;
            padding-bottom: 2.2rem;
        }
        [data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #e6edf5;
            border-radius: 8px;
            padding: 14px 16px;
            box-shadow: 0 4px 14px rgba(20, 39, 68, 0.05);
        }
        [data-testid="stMetricLabel"] p {
            color: #506176;
            font-size: 0.86rem;
        }
        div[data-testid="stTabs"] button p {
            font-weight: 650;
            font-size: 0.98rem;
        }
        .section-note {
            color: #66758a;
            font-size: 0.92rem;
            margin-top: -0.4rem;
            margin-bottom: 0.75rem;
        }
        .status-chip {
            display: inline-block;
            padding: 0.18rem 0.5rem;
            border-radius: 999px;
            background: #edf6ff;
            color: #1b5e8f;
            border: 1px solid #cfe5f7;
            font-size: 0.78rem;
            font-weight: 650;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_resource
def get_database():
    return ETFDatabase()


@st.cache_data(show_spinner=False, ttl=60)
def load_market_data(ticker=None, start_date=None, end_date=None):
    return ETFDatabase().get_market_data(ticker=ticker, start_date=start_date, end_date=end_date)


@st.cache_data(show_spinner=False, ttl=60)
def load_scores(date=None, ticker=None):
    return ETFDatabase().get_scores(date=date, ticker=ticker)


@st.cache_data(show_spinner=False, ttl=60)
def load_stats():
    return ETFDatabase().get_stats()


def normalize_weights(raw_weights):
    total = sum(raw_weights.values())
    if total <= 0:
        defaults = STRATEGY_CONFIG["weights"]
        total = sum(defaults.values())
        return {k: v / total for k, v in defaults.items()}, 0
    return {k: v / total for k, v in raw_weights.items()}, total


def build_sidebar_config():
    st.sidebar.title("⚙️ 策略控制台")
    st.sidebar.caption("参数变更会触发页面重算，回测需点击按钮重新运行。")

    with st.sidebar.expander("评分权重", expanded=True):
        raw_weights = {}
        col1, col2 = st.columns(2)
        raw_weights["trend"] = col1.slider("趋势", 0.0, 1.0, 0.30, 0.05)
        raw_weights["confirm"] = col2.slider("确认", 0.0, 1.0, 0.20, 0.05)
        raw_weights["momentum"] = col1.slider("动量", 0.0, 1.0, 0.25, 0.05)
        raw_weights["volume"] = col2.slider("成交量", 0.0, 1.0, 0.15, 0.05)
        raw_weights["volatility"] = col1.slider("波动率", 0.0, 1.0, 0.10, 0.05)
        weights, raw_total = normalize_weights(raw_weights)
        st.caption(f"输入合计 {raw_total:.2f}，已自动归一化用于实时评分。")

    with st.sidebar.expander("入场阈值", expanded=True):
        min_trend = st.slider("趋势最低分", 0, 30, STRATEGY_CONFIG["min_trend_score"])
        min_confirm = st.slider("确认最低分", 0, 20, STRATEGY_CONFIG["min_confirm_score"])
        min_total = st.slider("总评分最低分", 0, 100, STRATEGY_CONFIG["min_total_score"])

    with st.sidebar.expander("持仓与风控", expanded=True):
        max_holdings = st.slider("最大持仓数", 1, 10, STRATEGY_CONFIG["max_holdings"])
        max_per_etf = st.slider("单只上限(%)", 5, 50, int(STRATEGY_CONFIG["max_position_per_etf"] * 100)) / 100
        stop_loss = st.slider("止损线(%)", -20, -1, int(STRATEGY_CONFIG["stop_loss"] * 100)) / 100
        use_timing = st.checkbox("启用大盘择时", STRATEGY_CONFIG["market_timing"])

    cfg = STRATEGY_CONFIG.copy()
    cfg["weights"] = weights
    cfg["min_trend_score"] = min_trend
    cfg["min_confirm_score"] = min_confirm
    cfg["min_total_score"] = min_total
    cfg["max_holdings"] = max_holdings
    cfg["max_position_per_etf"] = max_per_etf
    cfg["stop_loss"] = stop_loss
    cfg["market_timing"] = use_timing

    st.sidebar.divider()
    latest = get_database().get_latest_date()
    st.sidebar.metric("最新数据日", latest or "N/A")
    st.sidebar.metric("当前ETF池", f"{len(ETF_UNIVERSE)} 只")
    return cfg


def apply_weighted_scores(df, cfg):
    if df.empty:
        return df

    out = df.copy()
    weights = cfg["weights"]
    components = [
        ("trend_score", "trend", SCORE_MAX["trend"]),
        ("confirm_score", "confirm", SCORE_MAX["confirm"]),
        ("momentum_rank", "momentum", SCORE_MAX["momentum"]),
        ("volume_score", "volume", SCORE_MAX["volume"]),
        ("vol_score", "volatility", SCORE_MAX["volatility"]),
    ]

    weighted = pd.Series(0.0, index=out.index)
    for column, weight_key, max_score in components:
        if column not in out.columns:
            out[column] = 0
        normalized_component = out[column].fillna(0).clip(lower=0, upper=max_score) / max_score
        weighted += normalized_component * weights[weight_key] * 100

    out["raw_total_score"] = out.get("total_score", weighted)
    out["total_score"] = weighted.round(2)
    return out


def format_pct(value, digits=2):
    if pd.isna(value):
        return "N/A"
    return f"{value:.{digits}%}"


def format_money(value):
    if pd.isna(value):
        return "N/A"
    return f"¥{value:,.0f}"


def etf_label(ticker):
    return f"{ETF_UNIVERSE.get(ticker, ticker)} ({ticker})"


def cfg_signature(cfg):
    weights = tuple((key, round(value, 6)) for key, value in sorted(cfg["weights"].items()))
    params = (
        cfg["min_trend_score"],
        cfg["min_confirm_score"],
        cfg["min_total_score"],
        cfg["max_holdings"],
        round(cfg["max_position_per_etf"], 6),
        round(cfg["stop_loss"], 6),
        cfg["market_timing"],
    )
    return weights + params


def get_latest_score_table(cfg):
    db = get_database()
    latest = db.get_latest_date()
    if not latest:
        return latest, pd.DataFrame()

    scores = load_scores(date=latest)
    if scores.empty:
        return latest, pd.DataFrame()

    prices = load_market_data(start_date=latest, end_date=latest)
    price_cols = ["ticker", "close", "open", "high", "low", "volume"]
    prices = prices[[col for col in price_cols if col in prices.columns]]
    scores = scores.merge(prices, on="ticker", how="left")
    scores = apply_weighted_scores(scores, cfg)
    scores["name"] = scores["ticker"].map(lambda x: ETF_UNIVERSE.get(x, x))
    scores["qualified"] = (
        (scores["trend_score"] >= cfg["min_trend_score"])
        & (scores["confirm_score"] >= cfg["min_confirm_score"])
        & (scores["total_score"] >= cfg["min_total_score"])
    )
    return latest, scores.sort_values("total_score", ascending=False)


def run_weighted_backtest(cfg, sample_type):
    db = get_database()
    market_df = db.get_market_data(ticker=list(ETF_UNIVERSE.keys()))
    bench_df = db.get_market_data(ticker=BENCHMARK)
    if market_df.empty:
        return {"error": "数据库无行情数据"}

    if sample_type == "样本内(2019-2023)":
        end = BACKTEST_CONFIG["in_sample_end"]
        market_df = market_df[market_df["date"] <= end]
        bench_df = bench_df[bench_df["date"] <= end]
    elif sample_type == "样本外(2024-至今)":
        start = BACKTEST_CONFIG["out_sample_start"]
        market_df = market_df[market_df["date"] >= start]
        bench_df = bench_df[bench_df["date"] >= start]

    strategy = StrategyEngine(cfg)
    all_scores = []
    for ticker in market_df["ticker"].unique():
        ticker_df = market_df[market_df["ticker"] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        scored = strategy.calculate_total_score(ticker_df)
        all_scores.append(scored)

    if not all_scores:
        return {"error": "无有效评分数据"}

    scores_df = pd.concat(all_scores, ignore_index=True)
    scores_df = apply_weighted_scores(scores_df, cfg)
    signals_df = strategy.generate_signals(scores_df, bench_df)
    engine = BacktestEngine(cfg)
    return engine._execute_backtest(signals_df, market_df, bench_df)


def make_score_bar(df):
    if df.empty:
        return go.Figure()

    top = df.head(12).sort_values("total_score")
    colors = np.where(top["qualified"], "#1f77b4", "#aab7c4")
    fig = go.Figure(
        go.Bar(
            x=top["total_score"],
            y=top["name"],
            orientation="h",
            marker_color=colors,
            customdata=top[["ticker", "trend_score", "confirm_score"]].to_numpy(),
            hovertemplate="<b>%{y}</b><br>代码 %{customdata[0]}<br>总分 %{x:.1f}<br>趋势 %{customdata[1]:.0f}<br>确认 %{customdata[2]:.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=420,
        margin=dict(l=10, r=16, t=24, b=10),
        xaxis_title="实时加权评分",
        yaxis_title=None,
        showlegend=False,
    )
    return fig


def make_nav_figure(result):
    nav_df = result["nav_df"].copy()
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=("净值曲线", "回撤"),
        row_heights=[0.7, 0.3],
    )

    base_nav = nav_df["nav"].iloc[0]
    fig.add_trace(
        go.Scatter(
            x=nav_df["date"],
            y=nav_df["nav"] / base_nav,
            name="策略",
            line=dict(color="#1769aa", width=2.4),
        ),
        row=1,
        col=1,
    )

    if "bench_return" in nav_df.columns and nav_df["bench_return"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=nav_df["date"],
                y=1 + nav_df["bench_return"],
                name="沪深300",
                line=dict(color="#7d8793", width=1.6, dash="dash"),
            ),
            row=1,
            col=1,
        )

    fig.add_trace(
        go.Scatter(
            x=nav_df["date"],
            y=nav_df["drawdown"],
            name="回撤",
            fill="tozeroy",
            line=dict(color="#d64f4f", width=1.1),
        ),
        row=2,
        col=1,
    )
    fig.update_yaxes(tickformat=".0%", row=2, col=1)
    fig.update_layout(height=620, hovermode="x unified", margin=dict(l=10, r=16, t=52, b=10))
    return fig


def make_candlestick(ticker, days=180):
    market = load_market_data(ticker=ticker)
    if market.empty:
        return go.Figure()

    market = market.sort_values("date").tail(days).copy()
    market["ma20"] = market["close"].rolling(20).mean()
    market["ma50"] = market["close"].rolling(50).mean()

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.74, 0.26],
    )
    fig.add_trace(
        go.Candlestick(
            x=market["date"],
            open=market["open"],
            high=market["high"],
            low=market["low"],
            close=market["close"],
            name="K线",
            increasing_line_color="#d64f4f",
            decreasing_line_color="#2e9d75",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(go.Scatter(x=market["date"], y=market["ma20"], name="MA20", line=dict(color="#1769aa")), row=1, col=1)
    fig.add_trace(go.Scatter(x=market["date"], y=market["ma50"], name="MA50", line=dict(color="#f0a202")), row=1, col=1)
    fig.add_trace(
        go.Bar(x=market["date"], y=market["volume"], name="成交量", marker_color="#9fb3c8", opacity=0.65),
        row=2,
        col=1,
    )
    fig.update_layout(
        height=560,
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        margin=dict(l=8, r=16, t=20, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
    )
    return fig


def make_radar(row):
    labels = ["趋势", "确认", "动量", "成交量", "波动率"]
    values = [
        row.get("trend_score", 0) / SCORE_MAX["trend"] * 100,
        row.get("confirm_score", 0) / SCORE_MAX["confirm"] * 100,
        row.get("momentum_rank", 0) / SCORE_MAX["momentum"] * 100,
        row.get("volume_score", 0) / SCORE_MAX["volume"] * 100,
        row.get("vol_score", 0) / SCORE_MAX["volatility"] * 100,
    ]
    fig = go.Figure(
        go.Scatterpolar(
            r=values + [values[0]],
            theta=labels + [labels[0]],
            fill="toself",
            name="评分结构",
            line=dict(color="#1769aa", width=2),
            fillcolor="rgba(23,105,170,0.22)",
        )
    )
    fig.update_layout(
        height=360,
        margin=dict(l=20, r=20, t=28, b=20),
        polar=dict(radialaxis=dict(visible=True, range=[0, 100], tickfont=dict(size=10))),
        showlegend=False,
    )
    return fig


def make_score_trend(ticker, cfg, days=180):
    scores = load_scores(ticker=ticker)
    if scores.empty:
        return go.Figure()

    scores = apply_weighted_scores(scores.sort_values("date").tail(days), cfg)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=scores["date"],
            y=scores["total_score"],
            name="总评分",
            line=dict(color="#1769aa", width=2.2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=scores["date"],
            y=scores["trend_score"],
            name="趋势",
            line=dict(color="#d64f4f", width=1.5),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=scores["date"],
            y=scores["momentum_rank"],
            name="动量",
            line=dict(color="#2e9d75", width=1.5),
        )
    )
    fig.add_hline(y=cfg["min_total_score"], line_dash="dot", line_color="#687385", annotation_text="总分阈值")
    fig.update_layout(height=380, hovermode="x unified", margin=dict(l=8, r=16, t=20, b=8))
    return fig


def render_header():
    st.title("📈 A股ETF轮动量化策略")
    st.markdown(
        f'<span class="status-chip">Streamlit UI {APP_VERSION}</span> '
        '<span class="section-note">五页工作台：仪表盘 / 回测结果 / ETF分析 / 数据管理 / 策略配置</span>',
        unsafe_allow_html=True,
    )


def render_dashboard(cfg):
    latest, scores = get_latest_score_table(cfg)
    st.header("仪表盘")
    st.markdown('<div class="section-note">最新信号、评分排行和数据健康度会跟随侧边栏参数即时刷新。</div>', unsafe_allow_html=True)

    stats = load_stats()
    signal_df = scores[scores["qualified"]] if not scores.empty else pd.DataFrame()

    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
    kpi1.metric("最新交易日", latest or "N/A")
    kpi2.metric("候选信号", len(signal_df))
    kpi3.metric("最高评分", f"{scores['total_score'].max():.1f}" if not scores.empty else "N/A")
    kpi4.metric("数据记录", f"{stats.get('market_data_count', 0):,}")
    kpi5.metric("覆盖标的", stats.get("ticker_count", 0))

    left, right = st.columns([1.25, 1])
    with left:
        st.subheader("ETF评分排行")
        if scores.empty:
            st.warning("暂无评分数据，请先更新或计算指标。")
        else:
            st.plotly_chart(make_score_bar(scores), use_container_width=True)

    with right:
        st.subheader("最新交易信号")
        if signal_df.empty:
            st.info("当前参数下没有满足入场阈值的ETF。")
        else:
            for _, row in signal_df.head(cfg["max_holdings"]).iterrows():
                st.metric(
                    etf_label(row["ticker"]),
                    f"{row['total_score']:.1f}分",
                    f"趋势 {row['trend_score']:.0f} / 确认 {row['confirm_score']:.0f}",
                )

    st.subheader("详细评分表")
    if not scores.empty:
        display = scores[
            [
                "ticker",
                "name",
                "total_score",
                "raw_total_score",
                "trend_score",
                "confirm_score",
                "momentum_rank",
                "volume_score",
                "vol_score",
                "close",
                "qualified",
            ]
        ].copy()
        display.columns = ["代码", "名称", "实时总分", "原始总分", "趋势", "确认", "动量", "成交量", "波动率", "收盘价", "入选"]
        st.dataframe(display, use_container_width=True, hide_index=True)


def render_backtest(cfg):
    st.header("回测结果")
    st.markdown('<div class="section-note">回测会使用当前侧边栏阈值、仓位、风控与实时加权评分。</div>', unsafe_allow_html=True)

    control, view = st.columns([0.9, 2.4])
    with control:
        sample_type = st.radio("回测区间", ["全区间", "样本内(2019-2023)", "样本外(2024-至今)"])
        st.caption(f"初始资金：{format_money(BACKTEST_CONFIG['initial_capital'])}")
        if st.button("🚀 运行回测", type="primary", use_container_width=True):
            with st.spinner("正在按当前参数运行回测..."):
                result = run_weighted_backtest(cfg, sample_type)
                if "error" in result:
                    st.error(result["error"])
                else:
                    st.session_state["backtest_result"] = result
                    st.session_state["backtest_sample_type"] = sample_type
                    st.session_state["backtest_cfg_signature"] = cfg_signature(cfg)
                    st.success("回测完成")

        if "backtest_result" in st.session_state:
            st.caption(f"当前结果：{st.session_state.get('backtest_sample_type', '全区间')}")

    with view:
        result = st.session_state.get("backtest_result")
        if not result:
            st.info("点击左侧按钮生成回测结果。")
            return

        active_signature = cfg_signature(cfg)
        if st.session_state.get("backtest_cfg_signature") != active_signature:
            with st.spinner("侧边栏参数已变化，正在刷新回测结果..."):
                sample_type = st.session_state.get("backtest_sample_type", sample_type)
                result = run_weighted_backtest(cfg, sample_type)
                if "error" in result:
                    st.error(result["error"])
                    return
                st.session_state["backtest_result"] = result
                st.session_state["backtest_cfg_signature"] = active_signature

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("总收益率", format_pct(result["total_return"]))
        c2.metric("年化收益", format_pct(result["annual_return"]))
        c3.metric("夏普比率", f"{result['sharpe_ratio']:.2f}")
        c4.metric("最大回撤", format_pct(result["max_drawdown"]))
        c5.metric("交易次数", result["num_trades"])

        st.plotly_chart(make_nav_figure(result), use_container_width=True)

        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("胜率", format_pct(result["win_rate"], 1))
        s2.metric("索提诺", f"{result['sortino_ratio']:.2f}")
        s3.metric("止损次数", result["stop_loss_count"])
        s4.metric("平均持仓数", f"{result['avg_holdings']:.1f}")
        s5.metric("总佣金", format_money(result["total_commission"]))

        trades = result["trades_df"]
        if not trades.empty:
            st.subheader("交易记录")
            trades_view = trades.copy()
            trades_view["name"] = trades_view["ticker"].map(lambda x: ETF_UNIVERSE.get(x, x))
            st.dataframe(trades_view, use_container_width=True, hide_index=True)


def render_etf_analysis(cfg):
    st.header("ETF分析")
    st.markdown('<div class="section-note">新增单标的分析页：K线图、雷达图、评分趋势图。</div>', unsafe_allow_html=True)

    latest, scores = get_latest_score_table(cfg)
    tickers = sorted(ETF_UNIVERSE.keys())
    default_index = 0
    if not scores.empty:
        default_ticker = scores.iloc[0]["ticker"]
        default_index = tickers.index(default_ticker) if default_ticker in tickers else 0

    top_controls = st.columns([1.4, 0.7, 1.4])
    ticker = top_controls[0].selectbox("选择ETF", tickers, index=default_index, format_func=etf_label)
    window = top_controls[1].selectbox("分析窗口", [90, 180, 360], index=1, format_func=lambda x: f"{x}日")

    ticker_scores = scores[scores["ticker"] == ticker] if not scores.empty else pd.DataFrame()
    if ticker_scores.empty:
        top_controls[2].info("该标的暂无最新评分。")
    else:
        row = ticker_scores.iloc[0]
        top_controls[2].metric("最新实时评分", f"{row['total_score']:.1f}", f"截至 {latest}")

    k1, k2, k3, k4 = st.columns(4)
    if not ticker_scores.empty:
        row = ticker_scores.iloc[0]
        k1.metric("趋势", f"{row['trend_score']:.0f}/30")
        k2.metric("确认", f"{row['confirm_score']:.0f}/20")
        k3.metric("动量", f"{row['momentum_rank']:.1f}/25")
        k4.metric("收盘价", f"{row.get('close', np.nan):.3f}" if pd.notna(row.get("close", np.nan)) else "N/A")

    st.subheader("K线与成交量")
    st.plotly_chart(make_candlestick(ticker, window), use_container_width=True)

    left, right = st.columns([1, 1.25])
    with left:
        st.subheader("评分雷达图")
        if ticker_scores.empty:
            st.info("暂无评分结构。")
        else:
            st.plotly_chart(make_radar(ticker_scores.iloc[0]), use_container_width=True)
    with right:
        st.subheader("评分趋势")
        st.plotly_chart(make_score_trend(ticker, cfg, window), use_container_width=True)


def render_data_management():
    st.header("数据管理")
    st.markdown('<div class="section-note">查看数据库覆盖、更新行情、检查运行日志。</div>', unsafe_allow_html=True)

    db = get_database()
    stats = load_stats()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("行情数据", f"{stats.get('market_data_count', 0):,}")
    c2.metric("评分数据", f"{stats.get('scores_count', 0):,}")
    c3.metric("信号记录", f"{stats.get('signals_count', 0):,}")
    c4.metric("最早日期", stats.get("earliest_date", "N/A"))
    c5.metric("最新日期", stats.get("latest_date", "N/A"))

    actions, logs_col = st.columns([0.9, 1.5])
    with actions:
        st.subheader("数据操作")
        if st.button("🔄 更新最新行情", type="primary", use_container_width=True):
            with st.spinner("更新中..."):
                from data_fetcher import update_latest_data

                count = update_latest_data(db=db)
                load_market_data.clear()
                load_scores.clear()
                load_stats.clear()
                st.success(f"已更新 {count} 条记录")
                st.rerun()
        st.caption("更新会调用现有 data_fetcher，不修改 src 目录。")

    with logs_col:
        st.subheader("运行日志")
        logs = db.get_logs(limit=20)
        if logs.empty:
            st.info("暂无运行日志。")
        else:
            st.dataframe(logs, use_container_width=True, hide_index=True)

    st.subheader("ETF数据覆盖")
    coverage_data = []
    for ticker in db.get_all_tickers():
        min_d, max_d = db.get_date_range(ticker)
        days = (pd.to_datetime(max_d) - pd.to_datetime(min_d)).days if min_d and max_d else 0
        coverage_data.append(
            {
                "代码": ticker,
                "名称": ETF_UNIVERSE.get(ticker, "沪深300" if ticker == BENCHMARK else ticker),
                "最早日期": min_d,
                "最新日期": max_d,
                "跨度天数": days,
            }
        )
    st.dataframe(pd.DataFrame(coverage_data), use_container_width=True, hide_index=True)


def render_strategy_config(cfg):
    st.header("策略配置")
    st.markdown('<div class="section-note">当前 sidebar 参数快照、评分规则和ETF标的池。</div>', unsafe_allow_html=True)

    left, right = st.columns([1, 1])
    with left:
        st.subheader("当前参数")
        weight_df = pd.DataFrame(
            [
                {"维度": "趋势", "权重": cfg["weights"]["trend"], "满分": 30},
                {"维度": "确认", "权重": cfg["weights"]["confirm"], "满分": 20},
                {"维度": "动量", "权重": cfg["weights"]["momentum"], "满分": 25},
                {"维度": "成交量", "权重": cfg["weights"]["volume"], "满分": 15},
                {"维度": "波动率", "权重": cfg["weights"]["volatility"], "满分": 10},
            ]
        )
        st.dataframe(weight_df, use_container_width=True, hide_index=True)

        param_df = pd.DataFrame(
            [
                {"参数": "趋势最低分", "当前值": cfg["min_trend_score"]},
                {"参数": "确认最低分", "当前值": cfg["min_confirm_score"]},
                {"参数": "总评分最低分", "当前值": cfg["min_total_score"]},
                {"参数": "最大持仓数", "当前值": cfg["max_holdings"]},
                {"参数": "单只上限", "当前值": f"{cfg['max_position_per_etf']:.0%}"},
                {"参数": "止损线", "当前值": f"{cfg['stop_loss']:.0%}"},
                {"参数": "大盘择时", "当前值": "启用" if cfg["market_timing"] else "关闭"},
            ]
        )
        st.dataframe(param_df, use_container_width=True, hide_index=True)

    with right:
        st.subheader("评分与交易规则")
        st.markdown(
            """
            - 趋势：收盘价高于20日/50日均线，且20日均线斜率向上。
            - 确认：连续站上20日均线的天数，最多计5天。
            - 动量：20日收益率横截面排名。
            - 成交量：放量和放量上涨加分。
            - 波动率：偏好适中波动区间。
            - 调仓：每周五从满足阈值的候选池中按评分选取。
            - 风控：跌破均线调出，单只亏损触及止损线时止损。
            """
        )

    st.subheader("ETF标的池")
    etf_df = pd.DataFrame([{"代码": code, "名称": name} for code, name in ETF_UNIVERSE.items()])
    st.dataframe(etf_df, use_container_width=True, hide_index=True)


def main():
    inject_style()
    cfg = build_sidebar_config()
    render_header()

    tab_dashboard, tab_backtest, tab_etf, tab_data, tab_config = st.tabs(
        ["仪表盘", "回测结果", "ETF分析", "数据管理", "策略配置"]
    )

    with tab_dashboard:
        render_dashboard(cfg)
    with tab_backtest:
        render_backtest(cfg)
    with tab_etf:
        render_etf_analysis(cfg)
    with tab_data:
        render_data_management()
    with tab_config:
        render_strategy_config(cfg)

    st.divider()
    st.caption("A股ETF轮动量化策略 v2.0 | 本地运行版 | 参数来自侧边栏实时状态")


if __name__ == "__main__":
    main()
