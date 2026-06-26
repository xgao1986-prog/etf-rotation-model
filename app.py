"""
Streamlit visual app - ETF rotation strategy dashboard v2.0.
Run: streamlit run app.py
"""

import sys

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots


sys.path.insert(0, "src")

from backtest import BacktestEngine
from config import (BACKTEST_CONFIG, BENCHMARK, ETF_UNIVERSE, DEFENSE_UNIVERSE,
                    FALLBACK_EQUITY_UNIVERSE, ALL_TRADABLE_ETFS, STRATEGY_CONFIG,
                    TRADING_RULES_CONFIG, DEFENSE_CONFIG, build_config)
from database import ETFDatabase
from strategy import StrategyEngine
from utils import cfg_signature


st.set_page_config(
    page_title="B0-18 ETF轮动策略",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


APP_VERSION = "B0-18"
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


def load_presets():
    """加载参数预设"""
    preset_path = os.path.join(os.path.dirname(__file__), "presets", "strategy_presets.json")
    if os.path.exists(preset_path):
        with open(preset_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "v1.0 原始参数": {
            "weights": {"trend": 0.30, "confirm": 0.20, "momentum": 0.25, "volume": 0.15, "volatility": 0.10},
            "min_trend_score": 15, "min_confirm_score": 4, "min_total_score": 40,
            "max_holdings": 5, "max_position_per_etf": 0.15, "stop_loss": -0.08,
        },
        "保守型": {
            "weights": {"trend": 0.40, "confirm": 0.30, "momentum": 0.15, "volume": 0.10, "volatility": 0.05},
            "min_trend_score": 20, "min_confirm_score": 8, "min_total_score": 50,
            "max_holdings": 3, "max_position_per_etf": 0.10, "stop_loss": -0.05,
        },
        "激进型": {
            "weights": {"trend": 0.20, "confirm": 0.10, "momentum": 0.40, "volume": 0.20, "volatility": 0.10},
            "min_trend_score": 10, "min_confirm_score": 2, "min_total_score": 35,
            "max_holdings": 7, "max_position_per_etf": 0.20, "stop_loss": -0.12,
        },
    }


def save_presets(presets):
    """保存参数预设"""
    preset_dir = os.path.join(os.path.dirname(__file__), "presets")
    os.makedirs(preset_dir, exist_ok=True)
    preset_path = os.path.join(preset_dir, "strategy_presets.json")
    with open(preset_path, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


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

        # v1.2.1: 动量/波动率因子总开关（B0.4默认关闭）
        enable_momentum_vol = st.checkbox("启用动量+波动率因子", False, help="关闭时动量排名与波动率评分不计入总分（B0.4基线状态）")
        if not enable_momentum_vol:
            st.caption("⚠️ 动量与波动率因子已关闭，仅趋势+确认+成交量参与评分")
        else:
            st.caption("✅ 全部5个因子参与评分")

    with st.sidebar.expander("入场阈值", expanded=True):
        min_trend = st.slider("趋势最低分", 0, 30, STRATEGY_CONFIG["min_trend_score"])
        min_confirm = st.slider("确认最低分", 0, 20, STRATEGY_CONFIG["min_confirm_score"])
        min_total = st.slider("总评分最低分", 0, 100, STRATEGY_CONFIG["min_total_score"])

    with st.sidebar.expander("持仓与风控", expanded=True):
        max_holdings = st.slider("最大持仓数", 1, 10, STRATEGY_CONFIG["max_holdings"])
        max_per_etf = st.slider("单只上限(%)", 5, 50, int(STRATEGY_CONFIG["max_position_per_etf"] * 100)) / 100
        stop_loss = st.slider("止损线(%)", -20, -1, int(STRATEGY_CONFIG["stop_loss"] * 100)) / 100

        # 止损模式选择
        stop_loss_mode_options = {"固定止损": "fixed", "ATR动态止损": "atr", "不止损": "none"}
        stop_loss_mode_display = list(stop_loss_mode_options.keys())
        stop_loss_mode_selected = st.selectbox("止损模式", stop_loss_mode_display, index=0)
        stop_loss_mode = stop_loss_mode_options[stop_loss_mode_selected]

        atr_multiplier = 2.0
        if stop_loss_mode == "atr":
            atr_multiplier = st.slider("ATR倍数", 1.0, 5.0, 2.0, 0.5)

        use_timing = st.checkbox("启用大盘择时", False)  # 默认关闭，回测数据显示关闭后收益更高

        st.divider()
        st.caption("宽基补仓")
        fallback_equity_enabled = st.checkbox("启用宽基补仓（沪深300/中证500/创业板/科创50）", False, help="行业ETF选不满时，用宽基ETF填充剩余仓位。回测显示当前参数下可能为负贡献。")

        st.divider()
        st.caption("调仓规则")

        # 调仓频率
        freq_options = {"每周": "weekly", "双周": "biweekly", "月度": "monthly"}
        freq_display = list(freq_options.keys())
        freq_selected = st.selectbox("调仓频率", freq_display, index=0)
        rebalance_freq = freq_options[freq_selected]

        # 调仓日
        weekday_options = ["周一", "周二", "周三", "周四", "周五"]
        rebalance_weekday = st.selectbox("调仓日", weekday_options, index=3)
        rebalance_weekday = weekday_options.index(rebalance_weekday)

    # ========== 实验性因子参数 (v1.1) ==========
    with st.sidebar.expander("⚡ v1.1实验因子", expanded=False):
        st.caption("冷静期、动态止盈、防御模块（默认关闭）")

        # 板块动量增强（v1.2 预留，当前回测引擎未实现）
        sector_boost = st.checkbox("启用板块动量增强", False, help="v1.2 预留功能，当前回测引擎暂未实现该因子，勾选不影响结果")

        # 冷静期
        st.divider()
        st.caption("冷静期")
        cooling_period = st.slider("冷静期(交易日)", 0, 20, 0)
        cooling_score_boost = st.slider("冷静期评分提升", 0, 30, 10)

        # 动态止盈
        st.divider()
        st.caption("动态止盈（默认关闭）")
        trailing_mode_options = {"不启用": "none", "单一阈值": "simple", "分档止盈": "tiered"}
        trailing_display = list(trailing_mode_options.keys())
        trailing_selected = st.selectbox("动态止盈模式", trailing_display, index=0)
        trailing_stop_mode = trailing_mode_options[trailing_selected]

        trailing_stop = None
        if trailing_stop_mode == "simple":
            trailing_stop = st.slider("回撤止盈阈值(%)", -20, -1, -10) / 100
        elif trailing_stop_mode == "tiered":
            st.caption("分档参数（盈利门槛 / 回撤容忍）")
            tier_1_pnl = st.slider("1档盈利门槛(%)", 2, 10, 5) / 100
            tier_1_drawdown = st.slider("1档回撤容忍(%)", -10, -2, -5) / 100
            tier_2_pnl = st.slider("2档盈利门槛(%)", 10, 25, 15) / 100
            tier_2_drawdown = st.slider("2档回撤容忍(%)", -15, -5, -8) / 100
            tier_3_pnl = st.slider("3档盈利门槛(%)", 20, 50, 30) / 100
            tier_3_drawdown = st.slider("3档回撤容忍(%)", -20, -8, -12) / 100

        # 防御模块
        st.divider()
        st.caption("防御模块")
        defense_enabled = st.checkbox("启用防御资产", True)
        defense_mode = st.selectbox("防御资产模式", ["黄金+国债", "仅黄金", "仅国债"], index=0)

        # 初始资金
        st.divider()
        st.caption("回测参数")
        initial_capital = st.number_input(
            "初始资金（万元）",
            min_value=10,
            max_value=10000,
            value=int(BACKTEST_CONFIG['initial_capital'] / 10000),
            step=10
        ) * 10000

        st.caption("防御比例（按大盘择时信号）")
        defense_20 = st.slider("防御仓位(0.2)配防御(%)", 0, 100, 50) / 100
        defense_50 = st.slider("半仓(0.5)配防御(%)", 0, 50, 20) / 100
        defense_100 = st.slider("满仓(1.0)配防御(%)", 0, 20, 0) / 100

    # 构建策略配置
    cfg = STRATEGY_CONFIG.copy()
    cfg["weights"] = weights
    cfg["min_trend_score"] = min_trend
    cfg["min_confirm_score"] = min_confirm
    cfg["min_total_score"] = min_total
    cfg["max_holdings"] = max_holdings
    cfg["max_position_per_etf"] = max_per_etf
    cfg["stop_loss"] = stop_loss
    cfg["stop_loss_mode"] = stop_loss_mode
    cfg["atr_stop_multiplier"] = atr_multiplier
    cfg["market_timing"] = use_timing
    cfg["fallback_equity_enabled"] = fallback_equity_enabled
    cfg["momentum_factor_enabled"] = enable_momentum_vol
    cfg["volatility_factor_enabled"] = enable_momentum_vol

    # 构建交易规则配置
    trading_rules_cfg = {
        "sector_boost_enabled": sector_boost,
        "rebalance_freq": rebalance_freq,
        "rebalance_weekday": rebalance_weekday,
        "cooling_period": cooling_period,
        "cooling_score_boost": cooling_score_boost,
        "trailing_stop_mode": trailing_stop_mode,
    }
    if trailing_stop is not None:
        trading_rules_cfg["trailing_stop"] = trailing_stop
    if trailing_stop_mode == "tiered":
        trading_rules_cfg.update({
            "tier_1_pnl": tier_1_pnl,
            "tier_1_drawdown": tier_1_drawdown,
            "tier_2_pnl": tier_2_pnl,
            "tier_2_drawdown": tier_2_drawdown,
            "tier_3_pnl": tier_3_pnl,
            "tier_3_drawdown": tier_3_drawdown,
        })

    # 构建防御配置
    defense_cfg = {
        "defense_enabled": defense_enabled,
    }

    # 回测配置
    backtest_cfg = {
        "initial_capital": initial_capital,
    }

    # 根据防御资产模式设置 DEFENSE_UNIVERSE
    import config as _config_module
    if defense_mode == "仅黄金":
        _config_module.DEFENSE_UNIVERSE = {"518880.SH": "黄金ETF"}
    elif defense_mode == "仅国债":
        _config_module.DEFENSE_UNIVERSE = {"511010.SH": "国债ETF"}
    else:
        _config_module.DEFENSE_UNIVERSE = {"518880.SH": "黄金ETF", "511010.SH": "国债ETF"}

    # 更新防御比例
    _config_module.DEFENSE_ALLOCATION = {
        0.2: defense_20,
        0.5: defense_50,
        1.0: defense_100,
    }

    # 合并为完整配置
    cfg = build_config(strategy_cfg=cfg, trading_rules_cfg=trading_rules_cfg, defense_cfg=defense_cfg, backtest_cfg=backtest_cfg)

    # B0-18 标准配置签名（一键复现基准）
    # 使用完整 B0.4 默认配置生成标准签名，避免在 cfg_signature 后追加参数
    B0_18_CFG = build_config(
        strategy_cfg=STRATEGY_CONFIG,
        trading_rules_cfg=TRADING_RULES_CONFIG,
        defense_cfg=DEFENSE_CONFIG,
        backtest_cfg=BACKTEST_CONFIG,
    )
    B0_18_SIGNATURE = cfg_signature(B0_18_CFG)

    current_sig = cfg_signature(cfg)
    is_b0_18 = (current_sig == B0_18_SIGNATURE)

    st.sidebar.divider()

    # 状态标记
    if is_b0_18:
        st.sidebar.markdown(
            """
            <div style="background:#e8f5e9;border:1px solid #4caf50;border-radius:8px;padding:10px 12px;text-align:center;">
                <div style="font-size:0.85rem;color:#2e7d32;font-weight:700;">✅ 标准 B0-18</div>
                <div style="font-size:0.75rem;color:#558b2f;">当前参数与基准一致</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown(
            """
            <div style="background:#fff3e0;border:1px solid #ff9800;border-radius:8px;padding:10px 12px;text-align:center;">
                <div style="font-size:0.85rem;color:#e65100;font-weight:700;">⚠️ 自定义实验</div>
                <div style="font-size:0.75rem;color:#f57c00;">参数已偏离B0-18基准</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # 一键加载B0-18
    if st.sidebar.button("🔄 重置为B0-18", use_container_width=True):
        st.session_state.clear()
        st.rerun()

    st.sidebar.divider()
    latest = get_database().get_latest_date()
    st.sidebar.metric("最新数据日", latest or "N/A")
    st.sidebar.metric("当前ETF池", "18 只 (B0-18)")
    st.sidebar.caption("16只行业ETF + 2只防御资产 | 概念池已封存")
    return cfg, is_b0_18


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
    if ticker in ETF_UNIVERSE:
        return f"{ETF_UNIVERSE[ticker]} ({ticker})"
    elif ticker in FALLBACK_EQUITY_UNIVERSE:
        return f"{FALLBACK_EQUITY_UNIVERSE[ticker]} ({ticker})"
    elif ticker in DEFENSE_UNIVERSE:
        return f"{DEFENSE_UNIVERSE[ticker]} ({ticker})"
    return f"{ticker} ({ticker})"


def _get_ticker_name(ticker):
    """通用名称映射（支持所有三类资产）"""
    if ticker in ETF_UNIVERSE:
        return ETF_UNIVERSE[ticker]
    elif ticker in FALLBACK_EQUITY_UNIVERSE:
        return FALLBACK_EQUITY_UNIVERSE[ticker]
    elif ticker in DEFENSE_UNIVERSE:
        return DEFENSE_UNIVERSE[ticker]
    elif ticker == BENCHMARK:
        return "沪深300"
    return ticker

def get_latest_score_table(cfg):
    db = get_database()
    # 先从 daily_scores 表获取最新日期（而不是 market_data，因为评分可能还没更新）
    with db._connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(date) FROM daily_scores")
        result = cursor.fetchone()
        latest = result[0] if result and result[0] else None

    if not latest:
        # daily_scores 没有数据，回退到 market_data 的最新日期
        latest = db.get_latest_date()
        if not latest:
            return None, pd.DataFrame()
        return latest, pd.DataFrame()  # 返回日期但提示无评分

    scores = load_scores(date=latest)
    if scores.empty:
        return latest, pd.DataFrame()

    prices = load_market_data(start_date=latest, end_date=latest)
    price_cols = ["ticker", "close", "open", "high", "low", "volume"]
    prices = prices[[col for col in price_cols if col in prices.columns]]
    scores = scores.merge(prices, on="ticker", how="left")
    scores = apply_weighted_scores(scores, cfg)
    scores["name"] = scores["ticker"].map(_get_ticker_name)
    scores["qualified"] = (
        (scores["trend_score"] >= cfg["min_trend_score"])
        & (scores["confirm_score"] >= cfg["min_confirm_score"])
        & (scores["total_score"] >= cfg["min_total_score"])
    )
    return latest, scores.sort_values("total_score", ascending=False)


def run_weighted_backtest(cfg, sample_type):
    db = get_database()
    etf_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=etf_tickers)
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

    # 使用 BacktestEngine.run() 走完整的评分路径
    engine = BacktestEngine(cfg)
    return engine.run(market_df, bench_df)


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

    # v1.2: 市场状态背景着色（连续 regime 段，更明显的透明度）
    if "regime_id" in nav_df.columns and nav_df["regime_id"].notna().any():
        regime_colors = {
            1: "rgba(214, 79, 79, 0.15)",   # 强牛 - 红
            2: "rgba(240, 162, 2, 0.15)",   # 弱牛 - 橙
            3: "rgba(91, 141, 184, 0.15)",  # 震荡 - 蓝
            4: "rgba(46, 157, 117, 0.15)",  # 熊市 - 绿
        }
        # 找连续 regime 段
        nav_df_sorted = nav_df.sort_values("date").reset_index(drop=True)
        current_regime = None
        seg_start = None
        for i, row in nav_df_sorted.iterrows():
            rid = row["regime_id"]
            if pd.isna(rid):
                continue
            if rid != current_regime:
                # 结束上一个段
                if current_regime is not None and seg_start is not None:
                    end_date = nav_df_sorted.iloc[i - 1]["date"]
                    color = regime_colors.get(int(current_regime), "rgba(128,128,128,0.05)")
                    fig.add_vrect(
                        x0=seg_start, x1=end_date,
                        fillcolor=color, opacity=1, line_width=0,
                        row=1, col=1,
                    )
                    fig.add_vrect(
                        x0=seg_start, x1=end_date,
                        fillcolor=color, opacity=1, line_width=0,
                        row=2, col=1,
                    )
                current_regime = rid
                seg_start = row["date"]
        # 结束最后一个段
        if current_regime is not None and seg_start is not None:
            end_date = nav_df_sorted.iloc[-1]["date"]
            color = regime_colors.get(int(current_regime), "rgba(128,128,128,0.05)")
            fig.add_vrect(
                x0=seg_start, x1=end_date,
                fillcolor=color, opacity=1, line_width=0,
                row=1, col=1,
            )
            fig.add_vrect(
                x0=seg_start, x1=end_date,
                fillcolor=color, opacity=1, line_width=0,
                row=2, col=1,
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


def make_combined_figure(result, start_date=None, end_date=None):
    """
    合并净值曲线图 + 仓位分配时序图，4行共享x轴，支持日期范围过滤。
    第1行：净值曲线（策略vs基准）+ 市场状态背景着色
    第2行：回撤
    第3行：牛熊状态
    第4行：仓位分配堆叠面积
    """
    nav_df = result["nav_df"].copy()
    if start_date and end_date:
        nav_df = nav_df[(nav_df["date"] >= pd.Timestamp(start_date)) & (nav_df["date"] <= pd.Timestamp(end_date))]

    nav_df = nav_df.sort_values("date").reset_index(drop=True)

    if nav_df.empty:
        return go.Figure()

    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.40, 0.15, 0.08, 0.37],
        subplot_titles=("", "", "", ""),
    )

    # ===== 第1行：净值曲线 =====
    # 市场状态背景着色
    if "regime_id" in nav_df.columns and nav_df["regime_id"].notna().any():
        regime_colors = {
            1: "rgba(214, 79, 79, 0.12)",
            2: "rgba(240, 162, 2, 0.12)",
            3: "rgba(91, 141, 184, 0.12)",
            4: "rgba(46, 157, 117, 0.12)",
        }
        nav_df_sorted = nav_df.sort_values("date").reset_index(drop=True)
        current_regime = None
        seg_start = None
        for i, row in nav_df_sorted.iterrows():
            rid = row["regime_id"]
            if pd.isna(rid):
                continue
            if rid != current_regime:
                if current_regime is not None and seg_start is not None:
                    end_date = nav_df_sorted.iloc[i - 1]["date"]
                    color = regime_colors.get(int(current_regime), "rgba(128,128,128,0.05)")
                    for r in [1, 2, 3, 4]:
                        fig.add_vrect(x0=seg_start, x1=end_date, fillcolor=color, opacity=1, line_width=0, row=r, col=1)
                current_regime = rid
                seg_start = row["date"]
        if current_regime is not None and seg_start is not None:
            end_date = nav_df_sorted.iloc[-1]["date"]
            color = regime_colors.get(int(current_regime), "rgba(128,128,128,0.05)")
            for r in [1, 2, 3, 4]:
                fig.add_vrect(x0=seg_start, x1=end_date, fillcolor=color, opacity=1, line_width=0, row=r, col=1)

    base_nav = nav_df["nav"].iloc[0]
    fig.add_trace(go.Scatter(
        x=nav_df["date"], y=nav_df["nav"] / base_nav,
        name="策略", line=dict(color="#1769aa", width=2.4),
    ), row=1, col=1)

    # 基准也按选中区间重新归一化到1.0起始
    if "bench_return" in nav_df.columns and nav_df["bench_return"].notna().any():
        bench_start = nav_df["bench_return"].iloc[0]
        normalized_bench = 1 + (nav_df["bench_return"] - bench_start)
        fig.add_trace(go.Scatter(
            x=nav_df["date"], y=normalized_bench,
            name="沪深300", line=dict(color="#7d8793", width=1.6, dash="dash"),
        ), row=1, col=1)

    # ===== 第2行：回撤 =====
    fig.add_trace(go.Scatter(
        x=nav_df["date"], y=nav_df["drawdown"],
        name="回撤", fill="tozeroy", line=dict(color="#d64f4f", width=1.1),
    ), row=2, col=1)
    fig.update_yaxes(tickformat=".0%", row=2, col=1)

    # ===== 第3行：牛熊状态 =====
    if "max_total_position" in nav_df.columns:
        mkt_signals = nav_df["max_total_position"].values
        mkt_colors = []
        mkt_labels = []
        for sig in mkt_signals:
            if sig >= 0.9:
                mkt_colors.append("#2e9d75")
                mkt_labels.append(f"🟢 牛市(满仓) {sig:.0%}")
            elif sig >= 0.4:
                mkt_colors.append("#f0a202")
                mkt_labels.append(f"🟡 震荡(半仓) {sig:.0%}")
            else:
                mkt_colors.append("#d64f4f")
                mkt_labels.append(f"🔴 熊市(防御) {sig:.0%}")

        fig.add_trace(go.Bar(
            x=nav_df["date"], y=[1] * len(nav_df),
            marker=dict(color=mkt_colors, line=dict(width=0, color="rgba(0,0,0,0)"),),
            showlegend=False, hovertemplate="%{customdata}<extra></extra>", customdata=mkt_labels,
        ), row=3, col=1)

        # 图例：牛熊状态
        for color, label in [
            ("#2e9d75", "🟢 牛市(满仓)"),
            ("#f0a202", "🟡 震荡(半仓)"),
            ("#d64f4f", "🔴 熊市(防御)"),
        ]:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(size=10, color=color, line=dict(width=0)),
                name=label, showlegend=True,
            ), row=3, col=1)

        fig.update_yaxes(range=[0, 1], visible=False, row=3, col=1)
        fig.update_layout(bargap=0, bargroupgap=0)

    # ===== 第4行：仓位分配堆叠面积 =====
    if "positions_pct" in nav_df.columns:
        pos_records = []
        for _, row in nav_df.iterrows():
            record = {"date": row["date"]}
            pct_dict = row.get("positions_pct", {})
            if isinstance(pct_dict, dict):
                for ticker, pct in pct_dict.items():
                    record[ticker] = pct
            if row["nav"] > 0:
                record["cash"] = row["cash"] / row["nav"]
            else:
                record["cash"] = 1.0
            pos_records.append(record)

        if pos_records:
            pos_df = pd.DataFrame(pos_records).fillna(0)

            colors = [
                "#1769aa", "#2e9d75", "#f0a202", "#d64f4f", "#9c27b0",
                "#607d8b", "#795548", "#e91e63", "#3f51b5", "#009688",
                "#ff5722", "#673ab7", "#8bc34a", "#cddc39", "#ffeb3b",
                "#00bcd4", "#ff9800"
            ]

            # 行业ETF
            for i, ticker in enumerate(ETF_UNIVERSE.keys()):
                if ticker in pos_df.columns:
                    name = ETF_UNIVERSE.get(ticker, ticker)
                    color = colors[i % len(colors)]
                    hover_texts = [f"<b>{name}</b><br>仓位: {v:.1%}" if v > 0 else "" for v in pos_df[ticker]]
                    fig.add_trace(go.Scatter(
                        x=pos_df["date"], y=pos_df[ticker],
                        name=name, mode="lines", stackgroup="one",
                        line=dict(width=0.5, color=color), fillcolor=color,
                        text=hover_texts, hoverinfo="text",
                    ), row=4, col=1)

            # 宽基ETF
            import config as _config_module
            fallback_tickers = list(getattr(_config_module, "FALLBACK_EQUITY_UNIVERSE", {}).keys())
            fallback_colors = ["#a0aec0", "#718096"]
            for i, ticker in enumerate(fallback_tickers):
                if ticker in pos_df.columns:
                    name = _config_module.FALLBACK_EQUITY_UNIVERSE.get(ticker, ticker)
                    color = fallback_colors[i % len(fallback_colors)]
                    hover_texts = [f"<b>【宽基】{name}</b><br>仓位: {v:.1%}" if v > 0 else "" for v in pos_df[ticker]]
                    fig.add_trace(go.Scatter(
                        x=pos_df["date"], y=pos_df[ticker],
                        name=f"【宽基】{name}", mode="lines", stackgroup="one",
                        line=dict(width=0.5, color=color), fillcolor=color,
                        text=hover_texts, hoverinfo="text",
                    ), row=4, col=1)

            # 防御资产
            defense_tickers = list(getattr(_config_module, "DEFENSE_UNIVERSE", {}).keys())
            defense_colors = ["#ffd700", "#2b6cb0"]
            for i, ticker in enumerate(defense_tickers):
                if ticker in pos_df.columns:
                    name = _config_module.DEFENSE_UNIVERSE.get(ticker, ticker)
                    color = defense_colors[i % len(defense_colors)]
                    hover_texts = [f"<b>【防御】{name}</b><br>仓位: {v:.1%}" if v > 0 else "" for v in pos_df[ticker]]
                    fig.add_trace(go.Scatter(
                        x=pos_df["date"], y=pos_df[ticker],
                        name=f"【防御】{name}", mode="lines", stackgroup="one",
                        line=dict(width=0.5, color=color), fillcolor=color,
                        text=hover_texts, hoverinfo="text",
                    ), row=4, col=1)

            # Cash
            if "cash" in pos_df.columns:
                hover_texts_cash = [f"<b>现金</b><br>占比: {v:.1%}" if v > 0 else "" for v in pos_df["cash"]]
                fig.add_trace(go.Scatter(
                    x=pos_df["date"], y=pos_df["cash"],
                    name="现金", mode="lines", stackgroup="one",
                    line=dict(width=0.5, color="#cccccc"), fillcolor="rgba(200,200,200,0.5)",
                    text=hover_texts_cash, hoverinfo="text",
                ), row=4, col=1)

            fig.update_yaxes(tickformat=".0%", row=4, col=1)

    fig.update_layout(
        height=720,
        hovermode="x unified",
        margin=dict(l=10, r=16, t=20, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5, font=dict(size=9)),
        xaxis_rangeslider_visible=False,
    )

    # 4行x轴都跳过周末（只显示交易日，不显示空白）
    rangebreaks = [dict(bounds=["sat", "mon"])]
    fig.update_xaxes(title_text="", rangebreaks=rangebreaks, row=1, col=1)
    fig.update_xaxes(title_text="", rangebreaks=rangebreaks, row=2, col=1)
    fig.update_xaxes(title_text="", rangebreaks=rangebreaks, row=3, col=1)
    fig.update_xaxes(title_text="日期", rangebreaks=rangebreaks, row=4, col=1)

    return fig


def _resample_weekly(df, end_weekday=4):
    """按周聚合日K线为周K线，以当周最后一个交易日为结束"""
    df = df.copy().sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    # 按自然周（ISO week）分组
    df["year"] = df["date"].dt.isocalendar().year.astype(int)
    df["week"] = df["date"].dt.isocalendar().week.astype(int)
    weekly = df.groupby(["year", "week"]).agg({
        "date": "last",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).reset_index(drop=True)
    return weekly


def make_candlestick(ticker, days=180, weekly=False, rebalance_weekday=4, trades_df=None):
    market = load_market_data(ticker=ticker)
    if market.empty:
        return go.Figure()

    market = market.sort_values("date").tail(days).copy()
    market["date"] = pd.to_datetime(market["date"])

    # 周K线聚合
    if weekly:
        market = _resample_weekly(market, end_weekday=rebalance_weekday)
        market["ma20"] = market["close"].rolling(20).mean()
        market["ma50"] = market["close"].rolling(50).mean()
    else:
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
            name= "周K线" if weekly else "日K线",
            increasing_line_color="#d64f4f",
            decreasing_line_color="#2e9d75",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(go.Scatter(x=market["date"], y=market["ma20"], name="MA20", line=dict(color="#1769aa")), row=1, col=1)
    fig.add_trace(go.Scatter(x=market["date"], y=market["ma50"], name="MA50", line=dict(color="#f0a202")), row=1, col=1)

    # 交易标记（买入/卖出）
    if trades_df is not None and not trades_df.empty:
        trades_df = trades_df.copy()
        trades_df["date"] = pd.to_datetime(trades_df["date"])
        # 日K线：精确匹配日期；周K线：映射到当周最后一个交易日
        if weekly:
            trades_df["year"] = trades_df["date"].dt.isocalendar().year.astype(int)
            trades_df["week"] = trades_df["date"].dt.isocalendar().week.astype(int)
            market["year"] = market["date"].dt.isocalendar().year.astype(int)
            market["week"] = market["date"].dt.isocalendar().week.astype(int)
            trade_weeks = trades_df.merge(market[["date", "year", "week"]], on=["year", "week"], how="left", suffixes=("", "_mapped"))
            trade_weeks = trade_weeks.rename(columns={"date_mapped": "plot_date"})
        else:
            trade_weeks = trades_df.copy()
            trade_weeks["plot_date"] = trade_weeks["date"]

        buy_trades = trade_weeks[trade_weeks["action"] == "BUY"]
        sell_trades = trade_weeks[trade_weeks["action"].isin(["SELL", "STOP_LOSS"])]

        # 关键：过滤交易日期，只保留在当前数据时间范围内，防止Plotly x轴被强制扩展
        min_date = market["date"].min()
        max_date = market["date"].max()

        if not buy_trades.empty:
            buy_dates = pd.to_datetime(buy_trades["plot_date"]).dropna().unique()
            buy_dates = buy_dates[(buy_dates >= min_date) & (buy_dates <= max_date)]
            if len(buy_dates) > 0:
                buy_y = market["high"].max() * 1.03
                fig.add_trace(go.Scatter(
                    x=buy_dates,
                    y=[buy_y] * len(buy_dates),
                    mode="markers",
                    marker=dict(symbol="triangle-up", size=14, color="#2e9d75", line=dict(width=1, color="white")),
                    name="买入",
                    text=[f"买入: {pd.Timestamp(d).strftime('%Y-%m-%d')}" for d in buy_dates],
                    hovertemplate="%{text}<extra></extra>",
                ), row=1, col=1)

        if not sell_trades.empty:
            sell_dates = pd.to_datetime(sell_trades["plot_date"]).dropna().unique()
            sell_dates = sell_dates[(sell_dates >= min_date) & (sell_dates <= max_date)]
            if len(sell_dates) > 0:
                sell_y = market["low"].min() * 0.97
                fig.add_trace(go.Scatter(
                    x=sell_dates,
                    y=[sell_y] * len(sell_dates),
                    mode="markers",
                    marker=dict(symbol="triangle-down", size=14, color="#d64f4f", line=dict(width=1, color="white")),
                    name="卖出",
                    text=[f"卖出: {pd.Timestamp(d).strftime('%Y-%m-%d')}" for d in sell_dates],
                    hovertemplate="%{text}<extra></extra>",
                ), row=1, col=1)

    fig.add_trace(
        go.Bar(x=market["date"], y=market["volume"], name="成交量", marker_color="#9fb3c8", opacity=0.65),
        row=2,
        col=1,
    )

    # 日K线/周K线：均不设置 rangebreaks
    # 注：rangebreaks 的 bounds/values 与 Plotly Candlestick 兼容性差，会导致K线堆叠或坐标异常。
    # 周末/假期空白由数据自然缺失体现，无需额外设置。
    # rangebreaks = [dict(bounds=["sat", "mon"])]  # 已禁用

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
    st.title("📈 B0-18 ETF轮动量化策略")
    st.markdown(
        '<span class="status-chip">B0-18 主线</span> '
        '<span class="status-chip" style="background:#fff3e0;border-color:#ff9800;color:#e65100;">概念池已封存</span> '
        '<span class="section-note">五页工作台：仪表盘 / 回测结果 / ETF分析 / 数据管理 / 策略配置</span>',
        unsafe_allow_html=True,
    )


def render_dashboard(cfg, is_b0_18=True):
    latest, scores = get_latest_score_table(cfg)
    st.header("仪表盘")
    st.markdown('<div class="section-note">最新信号、评分排行和数据健康度会跟随侧边栏参数即时刷新。</div>', unsafe_allow_html=True)

    stats = load_stats()
    signal_df = scores[scores["qualified"]] if not scores.empty else pd.DataFrame()

    # v1.2: 获取市场状态
    db_dash = get_database()
    bench_df_dash = load_market_data(ticker=BENCHMARK)
    market_signal_val = 1.0
    if not bench_df_dash.empty and cfg["market_timing"]:
        engine_dash = StrategyEngine(cfg)
        bench_signals = engine_dash.market_timing(bench_df_dash)
        bench_latest = bench_signals[bench_signals["date"] == latest] if latest else pd.DataFrame()
        if not bench_latest.empty:
            market_signal_val = bench_latest["market_signal"].iloc[0]

    # v1.2: 市场状态检测（使用 detect_history 确保状态确认逻辑正确）
    regime_info = None
    if not bench_df_dash.empty:
        from market_regime import MarketRegimeDetector
        detector = MarketRegimeDetector(cfg)
        market_df_dash = load_market_data(ticker=list(ETF_UNIVERSE.keys()))
        stock_df_dash = market_df_dash[market_df_dash["ticker"].isin(ETF_UNIVERSE.keys())].copy() if not market_df_dash.empty else None
        regime_history = detector.detect_history(bench_df_dash, stock_df_dash)
        if not regime_history.empty:
            regime_info = regime_history.iloc[-1].to_dict()
            regime_info['regime_name'] = detector.STATE_NAMES.get(regime_info.get('regime_id', 3), '震荡')

    if market_signal_val >= 0.9:
        mkt_text, mkt_color = "满仓", "#2e9d75"
    elif market_signal_val >= 0.4:
        mkt_text, mkt_color = "半仓", "#f0a202"
    else:
        mkt_text, mkt_color = "防御", "#d64f4f"

    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
    kpi1.metric("最新交易日", latest or "N/A")
    kpi2.metric("候选信号", len(signal_df))
    kpi3.metric("最高评分", f"{scores['total_score'].max():.1f}" if not scores.empty else "N/A")
    kpi4.metric("数据记录", f"{stats.get('market_data_count', 0):,}")
    with kpi5:
        if regime_info:
            regime_colors = {1: "#d64f4f", 2: "#f0a202", 3: "#5b8db8", 4: "#2e9d75"}
            rc = regime_colors.get(regime_info["regime_id"], "#66758a")
            st.markdown(
                f"""
                <div style='background:#ffffff;border:1px solid #e6edf5;border-radius:8px;padding:14px 16px;box-shadow:0 4px 14px rgba(20,39,68,0.05);text-align:center;'>
                    <div style='font-size:0.86rem;color:#506176;'>市场状态 (v1.2)</div>
                    <div style='font-size:1.5rem;font-weight:700;color:{rc};'>{regime_info["regime_name"]}</div>
                    <div style='font-size:0.78rem;color:#66758a;'>置信度 {regime_info["confidence"]:.0%}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"""
                <div style='background:#ffffff;border:1px solid #e6edf5;border-radius:8px;padding:14px 16px;box-shadow:0 4px 14px rgba(20,39,68,0.05);text-align:center;'>
                    <div style='font-size:0.86rem;color:#506176;'>大盘择时</div>
                    <div style='font-size:1.5rem;font-weight:700;color:{mkt_color};'>{mkt_text}</div>
                    <div style='font-size:0.78rem;color:#66758a;'>仓位 {market_signal_val:.0%}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # v1.2: 市场状态详情（如果 regime 可用）
    if regime_info:
        with st.expander("🔍 市场状态详情", expanded=False):
            c1, c2, c3 = st.columns(3)
            c1.metric("趋势位置", f"{regime_info['trend_position']:.3f}")
            c2.metric("MA50斜率", f"{regime_info['ma50_slope']:.4f}")
            c3.metric("波动率", f"{regime_info['vol_20']:.2%}", regime_info['vol_regime'])
            if not pd.isna(regime_info['market_breadth']):
                c4, c5 = st.columns(2)
                c4.metric("市场宽度", f"{regime_info['market_breadth']:.1%}")
                c5.metric("斜率加速度", f"{regime_info['slope_accel']:.3f}")
            st.caption(f"原因: {regime_info['reason']}")

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
            # 按资产类型分组展示
            stock_signals = signal_df[signal_df["ticker"].isin(ETF_UNIVERSE.keys())]
            fallback_signals = signal_df[signal_df["ticker"].isin(FALLBACK_EQUITY_UNIVERSE.keys())]
            defense_signals = signal_df[signal_df["ticker"].isin(DEFENSE_UNIVERSE.keys())]

            if not stock_signals.empty:
                st.markdown("<div style='font-size:0.85rem;color:#1b5e8f;font-weight:600;'>行业/主题ETF</div>", unsafe_allow_html=True)
                for _, row in stock_signals.head(cfg["max_holdings"]).iterrows():
                    st.metric(
                        etf_label(row["ticker"]),
                        f"{row['total_score']:.1f}分",
                        f"趋势 {row['trend_score']:.0f} / 确认 {row['confirm_score']:.0f}",
                    )

            if not fallback_signals.empty:
                st.markdown("<div style='font-size:0.85rem;color:#2e7d32;font-weight:600;'>宽基补仓ETF</div>", unsafe_allow_html=True)
                for _, row in fallback_signals.iterrows():
                    st.metric(
                        etf_label(row["ticker"]),
                        f"{row['total_score']:.1f}分",
                        f"趋势 {row['trend_score']:.0f} / 确认 {row['confirm_score']:.0f}",
                    )

            if not defense_signals.empty:
                st.markdown("<div style='font-size:0.85rem;color:#d64f4f;font-weight:600;'>防御资产</div>", unsafe_allow_html=True)
                for _, row in defense_signals.iterrows():
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

    # ========== 持仓明细 ==========
    st.subheader("💼 当前持仓明细")
    try:
        with db_dash._connect() as conn:
            portfolio_open = pd.read_sql_query(
                "SELECT * FROM portfolio WHERE status='OPEN' ORDER BY date DESC",
                conn
            )
    except Exception:
        portfolio_open = pd.DataFrame()

    if not portfolio_open.empty:
        hold_cols = st.columns(min(4, len(portfolio_open)))
        for i, (_, row) in enumerate(portfolio_open.iterrows()):
            ticker = row["ticker"]
            name = _get_ticker_name(ticker)
            latest_price_df = load_market_data(ticker=ticker, start_date=latest, end_date=latest)
            if not latest_price_df.empty:
                current_price = latest_price_df["close"].iloc[0]
                cost = row["cost_basis"]
                pnl_pct = (current_price - cost) / cost if cost and cost > 0 else 0
                pnl_color = "#2e9d75" if pnl_pct >= 0 else "#d64f4f"
                pnl_sign = "+" if pnl_pct >= 0 else ""
                unrealized = row.get("unrealized_pnl", 0)
            else:
                current_price = row.get("current_price", 0)
                pnl_pct = 0
                pnl_color = "#333333"
                unrealized = 0

            with hold_cols[i % len(hold_cols)]:
                st.markdown(
                    f"""
                    <div style='background:#ffffff;border:1px solid #e6edf5;border-radius:8px;padding:14px;text-align:center;box-shadow:0 4px 14px rgba(20,39,68,0.05);'>
                        <div style='font-size:14px;font-weight:600;color:#142744;'>{name}</div>
                        <div style='font-size:11px;color:#66758a;'>{ticker}</div>
                        <div style='font-size:12px;color:#506176;margin-top:6px;'>仓位: 15%</div>
                        <div style='font-size:14px;font-weight:700;color:{pnl_color};margin-top:4px;'>
                            {pnl_sign}{pnl_pct:.1%} | ¥{unrealized:,.0f}
                        </div>
                        <div style='font-size:11px;color:#66758a;margin-top:4px;'>
                            成本: {cost:.3f} | 现价: {current_price:.3f}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
    else:
        st.info("当前无持仓，等待买入信号")


def render_backtest(cfg, is_b0_18=True):
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

        # 日期范围选择器（拖动滑块，只含交易日）
        nav_df_all = result["nav_df"].copy()
        # 获取所有唯一交易日（排序）
        trading_dates = nav_df_all["date"].dt.date.unique()
        trading_dates = np.sort(trading_dates)
        num_trading_days = len(trading_dates)

        min_date = trading_dates[0] if num_trading_days > 0 else None
        max_date = trading_dates[-1] if num_trading_days > 0 else None

        if num_trading_days < 2:
            start_date = min_date
            end_date = max_date
        else:
            # 使用交易日序号作为滑块刻度
            slider_idx = st.slider(
                "📅 拖动选择分析时段（交易日）",
                min_value=0,
                max_value=num_trading_days - 1,
                value=(0, num_trading_days - 1),
                step=1,
                key="backtest_date_range_slider",
            )

            start_idx, end_idx = slider_idx
            start_date = trading_dates[start_idx]
            end_date = trading_dates[end_idx]

            st.caption(f"当前时段：{start_date} ~ {end_date}（共 {end_idx - start_idx + 1} 个交易日）")

        # 根据时段过滤数据并重算指标
        filtered_nav = nav_df_all[
            (nav_df_all["date"] >= pd.Timestamp(start_date)) & (nav_df_all["date"] <= pd.Timestamp(end_date))
        ].sort_values("date").reset_index(drop=True) if start_date and end_date else nav_df_all.sort_values("date").reset_index(drop=True)

        is_full_range = (start_date == min_date and end_date == max_date)

        if len(filtered_nav) >= 2:
            nav_start = filtered_nav["nav"].iloc[0]
            nav_end = filtered_nav["nav"].iloc[-1]
            total_return = (nav_end / nav_start) - 1 if nav_start > 0 else 0

            days = len(filtered_nav)
            years = days / 252
            annual_return = (1 + total_return) ** (1 / max(years, 0.01)) - 1

            daily_rets = filtered_nav["nav"].pct_change().dropna()
            vol = daily_rets.std() * np.sqrt(252) if len(daily_rets) > 1 else 0
            sharpe = annual_return / vol if vol > 0 else 0

            peak = filtered_nav["nav"].cummax()
            drawdown = (filtered_nav["nav"] - peak) / peak
            max_drawdown = drawdown.min()

            # 交易次数（在时段内）
            trades = result["trades_df"]
            trades_in_range = 0
            if not trades.empty and "date" in trades.columns:
                trades_in_range = len(trades[
                    (pd.to_datetime(trades["date"]) >= pd.Timestamp(start_date)) &
                    (pd.to_datetime(trades["date"]) <= pd.Timestamp(end_date))
                ])
        else:
            total_return = result["total_return"]
            annual_return = result["annual_return"]
            sharpe = result["sharpe_ratio"]
            max_drawdown = result["max_drawdown"]
            trades_in_range = result["num_trades"]

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("总收益率", format_pct(total_return))
        c2.metric("年化收益", format_pct(annual_return))
        c3.metric("夏普比率", f"{sharpe:.2f}")
        c4.metric("最大回撤", format_pct(max_drawdown))
        c5.metric("交易次数", trades_in_range)

        if not is_full_range:
            st.caption(f"⏱️ 当前显示：{start_date} ~ {end_date}（共 {len(filtered_nav)} 个交易日）")

        # 合并图表：净值曲线 + 回撤 + 牛熊状态 + 仓位分配（4行联动）
        st.plotly_chart(make_combined_figure(result, start_date, end_date), use_container_width=True)

        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("胜率", format_pct(result["win_rate"], 1))
        s2.metric("索提诺", f"{result['sortino_ratio']:.2f}")
        s3.metric("止损次数", result["stop_loss_count"])
        s4.metric("平均持仓数", f"{result['avg_holdings']:.1f}")
        s5.metric("总佣金", format_money(result["total_commission"]))

        # v1.2: 市场状态分布
        if "regime_summary" in result:
            st.subheader("📊 市场状态分布 (v1.2 observer)")
            summary = result["regime_summary"]
            r1, r2, r3, r4 = st.columns(4)
            regime_colors = {1: "#d64f4f", 2: "#f0a202", 3: "#5b8db8", 4: "#2e9d75"}
            for state_id, info in summary["state_distribution"].items():
                color = regime_colors.get(state_id, "#66758a")
                if state_id == 1:
                    with r1:
                        st.markdown(f"""
                        <div style='background:#ffffff;border:1px solid #e6edf5;border-radius:8px;padding:14px;text-align:center;'>
                            <div style='font-size:14px;font-weight:600;color:{color};'>{info['name']}</div>
                            <div style='font-size:28px;font-weight:700;color:#142744;margin-top:8px;'>{info['days']}天</div>
                            <div style='font-size:13px;color:#66758a;margin-top:4px;'>{info['percentage']:.1%}</div>
                        </div>
                        """, unsafe_allow_html=True)
                elif state_id == 2:
                    with r2:
                        st.markdown(f"""
                        <div style='background:#ffffff;border:1px solid #e6edf5;border-radius:8px;padding:14px;text-align:center;'>
                            <div style='font-size:14px;font-weight:600;color:{color};'>{info['name']}</div>
                            <div style='font-size:28px;font-weight:700;color:#142744;margin-top:8px;'>{info['days']}天</div>
                            <div style='font-size:13px;color:#66758a;margin-top:4px;'>{info['percentage']:.1%}</div>
                        </div>
                        """, unsafe_allow_html=True)
                elif state_id == 3:
                    with r3:
                        st.markdown(f"""
                        <div style='background:#ffffff;border:1px solid #e6edf5;border-radius:8px;padding:14px;text-align:center;'>
                            <div style='font-size:14px;font-weight:600;color:{color};'>{info['name']}</div>
                            <div style='font-size:28px;font-weight:700;color:#142744;margin-top:8px;'>{info['days']}天</div>
                            <div style='font-size:13px;color:#66758a;margin-top:4px;'>{info['percentage']:.1%}</div>
                        </div>
                        """, unsafe_allow_html=True)
                elif state_id == 4:
                    with r4:
                        st.markdown(f"""
                        <div style='background:#ffffff;border:1px solid #e6edf5;border-radius:8px;padding:14px;text-align:center;'>
                            <div style='font-size:14px;font-weight:600;color:{color};'>{info['name']}</div>
                            <div style='font-size:28px;font-weight:700;color:#142744;margin-top:8px;'>{info['days']}天</div>
                            <div style='font-size:13px;color:#66758a;margin-top:4px;'>{info['percentage']:.1%}</div>
                        </div>
                        """, unsafe_allow_html=True)
            st.caption(f"状态切换: {summary['switch_count']}次 | 平均置信度: {summary['avg_confidence']:.1%}")

        # v6: 预热期信息
        if "warmup_info" in result:
            warmup = result["warmup_info"]
            st.subheader("📊 预热期与运行时间")
            w1, w2, w3 = st.columns(3)
            w1.metric("数据起始", pd.to_datetime(warmup["earliest_data_start"]).strftime("%Y-%m-%d"))
            w2.metric("实际运行起始", pd.to_datetime(warmup["warmup_end"]).strftime("%Y-%m-%d"))
            w3.metric("预热期", f"{warmup['warmup_days']}个交易日")
            st.caption("预热期：打分标准所需数据积累期，期间策略空仓，不计入年化和基准对比")

        trades = result["trades_df"]

        # ========== 年度收益对比 ==========
        st.subheader("📅 年度收益对比")
        nav_df = result["nav_df"].copy()
        trades_df = result.get("trades_df", pd.DataFrame())
        if not nav_df.empty:
            nav_df["year"] = nav_df["date"].dt.year
            nav_df["daily_return"] = nav_df["nav"].pct_change()
            nav_df["peak"] = nav_df["nav"].cummax()
            nav_df["drawdown"] = (nav_df["nav"] - nav_df["peak"]) / nav_df["peak"]

            yearly_stats = []
            for year, group in nav_df.groupby("year"):
                if len(group) < 5:  # 数据不足，跳过
                    continue

                nav_start = group["nav"].iloc[0]
                nav_end = group["nav"].iloc[-1]
                strategy_return = (nav_end / nav_start) - 1 if nav_start > 0 else 0

                # 基准收益
                bench_return = 0
                if "bench_return" in group.columns and group["bench_return"].notna().any():
                    bench_start = group["bench_return"].iloc[0]
                    bench_end = group["bench_return"].iloc[-1]
                    bench_return = bench_end - bench_start

                # 日收益率统计
                daily_rets = group["daily_return"].dropna()
                trading_days = len(daily_rets)

                # 夏普率
                vol = daily_rets.std() * np.sqrt(252) if len(daily_rets) > 1 else 0
                years = trading_days / 252
                annual_ret = (1 + strategy_return) ** (1 / max(years, 0.01)) - 1
                sharpe = annual_ret / vol if vol > 0 else 0

                # 胜率（正收益交易日占比）
                win_days = (daily_rets > 0).sum()
                win_rate_days = win_days / len(daily_rets) if len(daily_rets) > 0 else 0

                # 最大回撤
                max_dd = group["drawdown"].min()

                # 交易次数
                year_trades = 0
                if not trades_df.empty and "date" in trades_df.columns:
                    year_trades = len(trades_df[pd.to_datetime(trades_df["date"]).dt.year == year])

                # 平均持仓
                avg_hold = group["num_positions"].mean() if "num_positions" in group.columns else 0

                yearly_stats.append({
                    "年份": int(year),
                    "策略收益": strategy_return,
                    "基准收益": bench_return,
                    "超额": strategy_return - bench_return,
                    "夏普率": sharpe,
                    "胜率": win_rate_days,
                    "波动率": vol,
                    "最大回撤": max_dd,
                    "交易次数": year_trades,
                    "平均持仓": avg_hold,
                })

            if yearly_stats:
                yearly = pd.DataFrame(yearly_stats)

                # 格式化显示
                display_yearly = yearly.copy()
                display_yearly["策略"] = display_yearly["策略收益"].apply(lambda x: format_pct(x, 1))
                display_yearly["基准"] = display_yearly["基准收益"].apply(lambda x: format_pct(x, 1))
                display_yearly["超额"] = display_yearly["超额"].apply(lambda x: format_pct(x, 1))
                display_yearly["夏普率"] = display_yearly["夏普率"].apply(lambda x: f"{x:.2f}")
                display_yearly["胜率"] = display_yearly["胜率"].apply(lambda x: format_pct(x, 1))
                display_yearly["波动率"] = display_yearly["波动率"].apply(lambda x: format_pct(x, 1))
                display_yearly["最大回撤"] = display_yearly["最大回撤"].apply(lambda x: format_pct(x, 1))
                display_yearly["交易次数"] = display_yearly["交易次数"].astype(int)
                display_yearly["平均持仓"] = display_yearly["平均持仓"].apply(lambda x: f"{x:.1f}")

                # 按列顺序显示
                cols = ["年份", "策略", "基准", "超额", "夏普率", "胜率", "波动率", "最大回撤", "交易次数", "平均持仓"]
                st.dataframe(
                    display_yearly[cols],
                    use_container_width=True,
                    hide_index=True,
                )


def render_etf_analysis(cfg, is_b0_18=True):
    st.header("ETF分析")
    st.markdown('<div class="section-note">新增单标的分析页：K线图、雷达图、评分趋势图。</div>', unsafe_allow_html=True)

    latest, scores = get_latest_score_table(cfg)
    tickers = sorted(ETF_UNIVERSE.keys())
    default_index = 0
    if not scores.empty:
        default_ticker = scores.iloc[0]["ticker"]
        default_index = tickers.index(default_ticker) if default_ticker in tickers else 0

    top_controls = st.columns([1.4, 0.7, 0.7, 1.4])
    ticker = top_controls[0].selectbox("选择ETF", tickers, index=default_index, format_func=etf_label)
    window = top_controls[1].selectbox("分析窗口", [90, 180, 360], index=1, format_func=lambda x: f"{x}日")
    kline_mode = top_controls[2].selectbox("K线周期", ["日K", "周K"], index=0)
    weekly = kline_mode == "周K"
    # 获取调仓日（周K聚合用）
    rebalance_weekday = cfg.get("rebalance_weekday", 4)
    # 获取回测交易记录
    trades_df = None
    backtest_hint = ""
    if "backtest_result" in st.session_state:
        trades = st.session_state["backtest_result"].get("trades_df", pd.DataFrame())
        if not trades.empty and ticker in trades["ticker"].values:
            trades_df = trades[trades["ticker"] == ticker].copy()
            backtest_hint = f"📝 回测交易: {len(trades_df)} 次（买入 {len(trades_df[trades_df['action']=='BUY'])} / 卖出 {len(trades_df[trades_df['action'].isin(['SELL','STOP_LOSS'])])}）"
        elif not trades.empty:
            backtest_hint = f"📝 回测无该标的交易（回测共 {len(trades)} 次交易）"
        else:
            backtest_hint = "📝 回测结果无交易记录"
    else:
        backtest_hint = "📝 请先运行回测以显示交易标记"
    top_controls[3].caption(backtest_hint)

    ticker_scores = scores[scores["ticker"] == ticker] if not scores.empty else pd.DataFrame()
    if ticker_scores.empty:
        st.info("该标的暂无最新评分。")
    else:
        row = ticker_scores.iloc[0]
        st.metric("最新实时评分", f"{row['total_score']:.1f}", f"截至 {latest}")

    k1, k2, k3, k4 = st.columns(4)
    if not ticker_scores.empty:
        row = ticker_scores.iloc[0]
        k1.metric("趋势", f"{row['trend_score']:.0f}/30")
        k2.metric("确认", f"{row['confirm_score']:.0f}/20")
        k3.metric("动量", f"{row['momentum_rank']:.1f}/25")
        k4.metric("收盘价", f"{row.get('close', np.nan):.3f}" if pd.notna(row.get("close", np.nan)) else "N/A")

    st.subheader("K线与成交量")
    st.plotly_chart(make_candlestick(ticker, window, weekly=weekly, rebalance_weekday=rebalance_weekday, trades_df=trades_df), use_container_width=True)

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



def _recalculate_scores(db, cfg, st_info=None, st_success=None, st_error=None):
    """重新计算所有评分并保存到数据库。

    参数:
        db: ETFDatabase 实例
        cfg: 策略配置 dict
        st_info: st.info 回调（可选）
        st_success: st.success 回调（可选）
        st_error: st.error 回调（可选）
    返回:
        scores_all DataFrame 或 None（失败时）
    """
    engine_calc = StrategyEngine(cfg)
    all_tickers = list(ALL_TRADABLE_ETFS.keys())
    market_df_all = db.get_market_data(ticker=all_tickers)

    stock_df = market_df_all[market_df_all['ticker'].isin(ETF_UNIVERSE.keys())].copy()
    fallback_df = market_df_all[market_df_all['ticker'].isin(FALLBACK_EQUITY_UNIVERSE.keys())].copy()
    defense_df = market_df_all[market_df_all['ticker'].isin(DEFENSE_UNIVERSE.keys())].copy()

    stock_scores = []
    for ticker in stock_df['ticker'].unique():
        ticker_df = stock_df[stock_df['ticker'] == ticker].copy()
        if len(ticker_df) >= 50:
            scored = engine_calc.calculate_total_score(ticker_df)
            stock_scores.append(scored)

    if not stock_scores:
        if st_error:
            st_error("无有效行业ETF数据")
        return None

    scores_all = pd.concat(stock_scores, ignore_index=True)
    scores_all = engine_calc.rank_all_momentum(scores_all)
    scores_all = engine_calc.compute_total_score(scores_all)

    fallback_scores = []
    for ticker in fallback_df['ticker'].unique():
        ticker_df = fallback_df[fallback_df['ticker'] == ticker].copy()
        if len(ticker_df) >= 50:
            scored = engine_calc.calculate_fallback_equity_score(ticker_df)
            fallback_scores.append(scored)

    if fallback_scores:
        fallback_scores_df = pd.concat(fallback_scores, ignore_index=True)
        fallback_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
        fallback_scores_df['total_score'] = fallback_scores_df[fallback_cols].fillna(0).sum(axis=1)
        scores_all = pd.concat([scores_all, fallback_scores_df], ignore_index=True)

    defense_scores = []
    for ticker in defense_df['ticker'].unique():
        ticker_df = defense_df[defense_df['ticker'] == ticker].copy()
        if len(ticker_df) >= 50:
            scored = engine_calc.calculate_defense_score(ticker_df)
            defense_scores.append(scored)

    if defense_scores:
        defense_scores_df = pd.concat(defense_scores, ignore_index=True)
        defense_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
        defense_scores_df['total_score'] = defense_scores_df[defense_cols].fillna(0).sum(axis=1)
        scores_all = pd.concat([scores_all, defense_scores_df], ignore_index=True)

    scores_all['total_score'] = scores_all['total_score'].fillna(0)
    db.save_scores(scores_all)

    if st_success:
        st_success(f"✅ 已计算并保存 {len(scores_all)} 条评分记录")

    return scores_all

def render_data_management():
    st.header("数据管理")
    st.markdown('<div class="section-note">查看数据库覆盖、更新行情、检查运行日志。</div>', unsafe_allow_html=True)

    # 构建策略配置（同 render_sidebar）
    cfg = STRATEGY_CONFIG.copy()
    cfg["weights"] = {
        "trend": 1.0,
        "confirm": 1.0,
        "momentum": 1.0,
        "volatility": 0.8,
    }
    cfg["min_trend_score"] = 5
    cfg["min_total_score"] = 40
    cfg["max_holdings"] = 5
    cfg["market_timing"] = True
    cfg["stop_loss"] = -0.08
    cfg["max_position_per_etf"] = 0.20
    cfg["rebalance_freq"] = 5
    cfg["sector_boost_enabled"] = False
    cfg["trading_fee"] = 0.0003

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

                try:
                    count = update_latest_data(db=db)
                    if count > 0:
                        st.success(f"✅ 已更新 {count} 条行情记录")
                    else:
                        st.info("暂无新行情数据或 AKShare 未安装")

                    # 方案 A：行情更新后自动重新计算评分
                    scores_all = _recalculate_scores(
                        db, cfg,
                        st_info=lambda msg: st.info(msg),
                        st_success=lambda msg: st.success(msg),
                        st_error=lambda msg: st.error(msg)
                    )

                    load_market_data.clear()
                    load_scores.clear()
                    load_stats.clear()
                    st.rerun()
                except ImportError as e:
                    st.error(f"AKShare 未安装: {e}")
                    st.info("请安装 AKShare: `pip install akshare`，或继续使用 iFinD CSV 导入方式")
                except Exception as e:
                    st.error(f"更新失败: {e}")
        st.caption("AKShare 增量更新。若未安装 AKShare，请使用 iFinD CSV 导入。")
        st.caption("当前数据已更新至 2026-06-25。")

        st.divider()

        # iFinD CSV 导入
        uploaded = st.file_uploader("📥 导入 iFinD CSV", type=["csv"], key="ifind_upload")
        if uploaded is not None:
            with st.spinner("导入 iFinD 数据中..."):
                from data_fetcher import import_from_kimi
                try:
                    ifind_df = pd.read_csv(uploaded)
                    count = import_from_kimi(ifind_df, db=db, source_label="iFinD")
                    load_market_data.clear()
                    load_scores.clear()
                    load_stats.clear()
                    st.success(f"✅ 已导入 {count} 条 iFinD 记录")

                    # 导入后自动重新计算评分
                    scores_all = _recalculate_scores(
                        db, cfg,
                        st_info=lambda msg: st.info(msg),
                        st_success=lambda msg: st.success(msg),
                        st_error=lambda msg: st.error(msg)
                    )

                    st.rerun()
                except Exception as e:
                    st.error(f"导入失败: {e}")

        st.divider()

        # 重新计算评分
        if st.button("🧮 重新计算评分", use_container_width=True):
            with st.spinner("重新计算所有评分..."):
                try:
                    engine_calc = StrategyEngine(cfg)

                    # 加载所有三类资产数据
                    all_tickers = list(ALL_TRADABLE_ETFS.keys())
                    market_df_all = db.get_market_data(ticker=all_tickers)

                    # 分离三类资产
                    stock_df = market_df_all[market_df_all['ticker'].isin(ETF_UNIVERSE.keys())].copy()
                    fallback_df = market_df_all[market_df_all['ticker'].isin(FALLBACK_EQUITY_UNIVERSE.keys())].copy()
                    defense_df = market_df_all[market_df_all['ticker'].isin(DEFENSE_UNIVERSE.keys())].copy()

                    # 步骤1-2：行业ETF评分
                    stock_scores = []
                    for ticker in stock_df['ticker'].unique():
                        ticker_df = stock_df[stock_df['ticker'] == ticker].copy()
                        if len(ticker_df) >= 50:
                            scored = engine_calc.calculate_total_score(ticker_df)
                            stock_scores.append(scored)

                    if not stock_scores:
                        st.error("无有效行业ETF数据")
                        raise ValueError("无有效行业ETF数据")

                    scores_all = pd.concat(stock_scores, ignore_index=True)

                    # 步骤3：行业ETF横截面动量排名
                    scores_all = engine_calc.rank_all_momentum(scores_all)
                    scores_all = engine_calc.compute_total_score(scores_all)

                    # 步骤4：宽基补仓ETF评分
                    fallback_scores = []
                    for ticker in fallback_df['ticker'].unique():
                        ticker_df = fallback_df[fallback_df['ticker'] == ticker].copy()
                        if len(ticker_df) >= 50:
                            scored = engine_calc.calculate_fallback_equity_score(ticker_df)
                            fallback_scores.append(scored)

                    if fallback_scores:
                        fallback_scores_df = pd.concat(fallback_scores, ignore_index=True)
                        fallback_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
                        fallback_scores_df['total_score'] = fallback_scores_df[fallback_cols].fillna(0).sum(axis=1)
                        scores_all = pd.concat([scores_all, fallback_scores_df], ignore_index=True)

                    # 步骤5：防御资产评分
                    defense_scores = []
                    for ticker in defense_df['ticker'].unique():
                        ticker_df = defense_df[defense_df['ticker'] == ticker].copy()
                        if len(ticker_df) >= 50:
                            scored = engine_calc.calculate_defense_score(ticker_df)
                            defense_scores.append(scored)

                    if defense_scores:
                        defense_scores_df = pd.concat(defense_scores, ignore_index=True)
                        defense_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
                        defense_scores_df['total_score'] = defense_scores_df[defense_cols].fillna(0).sum(axis=1)
                        scores_all = pd.concat([scores_all, defense_scores_df], ignore_index=True)

                    # 确保 total_score 已填充
                    scores_all['total_score'] = scores_all['total_score'].fillna(0)

                    db.save_scores(scores_all)
                    load_scores.clear()
                    st.success(f"✅ 已计算并保存 {len(scores_all)} 条评分记录")
                    st.rerun()
                except Exception as e:
                    st.error(f"计算失败: {e}")

        # 数据质量检查
        if st.button("📊 数据质量检查", use_container_width=True):
            with st.spinner("数据质量检查中..."):
                issues = []
                try:
                    for ticker in list(ALL_TRADABLE_ETFS.keys()) + [BENCHMARK]:
                        df = db.get_market_data(ticker=ticker)
                        if df.empty:
                            issues.append(f"❌ {ticker}: 无数据")
                            continue
                        df["date"] = pd.to_datetime(df["date"])
                        date_range = pd.date_range(df["date"].min(), df["date"].max(), freq="B")
                        missing = len(date_range) - len(df)
                        if missing > 5:
                            issues.append(f"⚠️ {ticker}: 缺失 {missing} 个交易日")
                        if (df["close"] <= 0).any():
                            issues.append(f"❌ {ticker}: 存在零或负价格")
                        max_daily_change = df["close"].pct_change().abs().max()
                        if max_daily_change > 0.2:
                            issues.append(f"⚠️ {ticker}: 存在单日涨跌幅>{max_daily_change:.1%}的异常值")
                    if issues:
                        for issue in issues:
                            st.markdown(issue)
                    else:
                        st.success("✅ 数据质量检查通过，未发现异常")
                except Exception as e:
                    st.error(f"检查失败: {e}")

        # 清理旧数据
        if st.button("🗑️ 清理90天前数据", use_container_width=True):
            with st.spinner("清理数据中..."):
                try:
                    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
                    with db._connect() as conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM market_data WHERE date < ?", (cutoff,))
                        deleted = cursor.rowcount
                        conn.commit()
                    load_market_data.clear()
                    load_scores.clear()
                    load_stats.clear()
                    st.success(f"✅ 已清理 {deleted} 条旧数据")
                    st.rerun()
                except Exception as e:
                    st.error(f"清理失败: {e}")

        # CSV 导出
        export_type = st.selectbox("导出类型", ["行情数据", "评分数据", "交易信号"], key="export_type")
        if st.button("📤 导出 CSV", use_container_width=True):
            try:
                if export_type == "行情数据":
                    df_exp = db.get_market_data()
                elif export_type == "评分数据":
                    df_exp = db.get_scores()
                else:
                    df_exp = db.get_signals()
                if not df_exp.empty:
                    csv = df_exp.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        label="⬇️ 下载 CSV",
                        data=csv,
                        file_name=f"{export_type}_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
                else:
                    st.warning("无数据可导出")
            except Exception as e:
                st.error(f"导出失败: {e}")

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


def render_strategy_config(cfg, is_b0_18=True):
    st.header("策略配置")
    st.markdown('<div class="section-note">当前 sidebar 参数快照、评分规则和ETF标的池。</div>', unsafe_allow_html=True)

    # ========== 参数预设区 ==========
    st.subheader("💾 参数预设")
    presets = load_presets()
    if presets:
        preset_cols = st.columns(min(4, len(presets)))
        for i, (name, preset) in enumerate(presets.items()):
            with preset_cols[i % len(preset_cols)]:
                is_current = (
                    abs(preset.get("min_total_score", 0) - cfg["min_total_score"]) < 0.01
                    and abs(preset.get("stop_loss", 0) - cfg["stop_loss"]) < 0.001
                )
                border_color = "#1769aa" if is_current else "#e6edf5"
                bg_color = "#edf6ff" if is_current else "#ffffff"
                st.markdown(
                    f"""
                    <div style='background:{bg_color};border:2px solid {border_color};border-radius:8px;padding:12px;'>
                        <div style='font-size:14px;font-weight:600;color:#142744;'>{name}</div>
                        <div style='font-size:11px;color:#66758a;margin:4px 0;'>{'当前使用中' if is_current else ''}</div>
                        <div style='font-size:11px;color:#506176;'>
                            趋势{preset['weights']['trend']:.0%} | 确认{preset['weights']['confirm']:.0%} | 动量{preset['weights']['momentum']:.0%}
                        </div>
                        <div style='font-size:11px;color:#506176;'>
                            总评≥{preset['min_total_score']} | 止损{preset['stop_loss']:.0%} | 持仓{preset['max_holdings']}只
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                btn_cols = st.columns(2)
                with btn_cols[0]:
                    if st.button("加载", key=f"load_{name}", use_container_width=True):
                        st.info(f"请手动在侧边栏调整参数以匹配 '{name}' 预设（Streamlit 限制无法自动同步 slider）")
                with btn_cols[1]:
                    if st.button("删除", key=f"del_{name}", use_container_width=True):
                        del presets[name]
                        save_presets(presets)
                        st.success(f"已删除: {name}")
                        st.rerun()
    else:
        st.info("暂无保存的参数预设。")

    # 保存当前参数为新预设
    with st.expander("➕ 保存当前参数为新预设"):
        new_preset_name = st.text_input("预设名称", "", key="new_preset_name")
        if st.button("保存", use_container_width=True) and new_preset_name:
            presets[new_preset_name] = {
                "weights": cfg["weights"].copy(),
                "min_trend_score": cfg["min_trend_score"],
                "min_confirm_score": cfg["min_confirm_score"],
                "min_total_score": cfg["min_total_score"],
                "max_holdings": cfg["max_holdings"],
                "max_position_per_etf": cfg["max_position_per_etf"],
                "stop_loss": cfg["stop_loss"],
            }
            save_presets(presets)
            st.success(f"✅ 已保存预设: {new_preset_name}")
            st.rerun()

    st.divider()

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

        # 权重饼图
        fig_pie = go.Figure(
            data=[
                go.Pie(
                    labels=["趋势", "确认", "动量", "成交", "波动"],
                    values=[
                        cfg["weights"]["trend"],
                        cfg["weights"]["confirm"],
                        cfg["weights"]["momentum"],
                        cfg["weights"]["volume"],
                        cfg["weights"]["volatility"],
                    ],
                    hole=0.4,
                    marker_colors=["#1769aa", "#9c27b0", "#2e9d75", "#f0a202", "#d64f4f"],
                    textinfo="label+percent",
                    textfont_size=12,
                )
            ]
        )
        fig_pie.update_layout(
            height=260,
            showlegend=False,
            margin=dict(l=20, r=20, t=20, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            annotations=[dict(text="权重", x=0.5, y=0.5, font_size=14, showarrow=False)],
        )
        st.plotly_chart(fig_pie, use_container_width=True)

        import config as _config_module
        param_df = pd.DataFrame(
            [
                {"参数": "趋势最低分", "当前值": cfg["min_trend_score"]},
                {"参数": "确认最低分", "当前值": cfg["min_confirm_score"]},
                {"参数": "总评分最低分", "当前值": cfg["min_total_score"]},
                {"参数": "最大持仓数", "当前值": cfg["max_holdings"]},
                {"参数": "单只上限", "当前值": f"{cfg['max_position_per_etf']:.0%}"},
                {"参数": "止损模式", "当前值": f"{cfg.get('stop_loss_mode', 'fixed')}"},
                {"参数": "止损线", "当前值": f"{cfg['stop_loss']:.0%}"},
                {"参数": "ATR倍数", "当前值": f"{cfg.get('atr_stop_multiplier', 2.0)}"},
                {"参数": "大盘择时", "当前值": "启用" if cfg["market_timing"] else "关闭"},
                {"参数": "防御模块", "当前值": "启用" if cfg.get("defense_enabled", False) else "关闭"},
                {"参数": "防御资产", "当前值": ", ".join([DEFENSE_UNIVERSE.get(t, t) for t in _config_module.DEFENSE_UNIVERSE.keys()]) if cfg.get("defense_enabled", False) else "无"},
                {"参数": "防御比例(0.2)", "当前值": f"{_config_module.DEFENSE_ALLOCATION.get(0.2, 0):.0%}" if cfg.get("defense_enabled", False) else "N/A"},
                {"参数": "防御比例(0.5)", "当前值": f"{_config_module.DEFENSE_ALLOCATION.get(0.5, 0):.0%}" if cfg.get("defense_enabled", False) else "N/A"},
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

    st.info("B0-18 主线：只使用16只行业ETF + 2只防御资产。概念ETF（16只）和宽基补仓（4只）已封存，不参与日常轮动。")

    # 行业ETF（B0-18 核心池）
    st.markdown("**行业ETF（B0-18 核心池 - 16只）**")
    stock_df = pd.DataFrame([{"代码": code, "名称": name} for code, name in ETF_UNIVERSE.items()])
    st.dataframe(stock_df, use_container_width=True, hide_index=True)

    # 防御资产（B0-18 防御池）
    st.markdown("**防御资产（B0-18 防御池 - 2只）**")
    defense_df = pd.DataFrame([{"代码": code, "名称": name} for code, name in DEFENSE_UNIVERSE.items()])
    st.dataframe(defense_df, use_container_width=True, hide_index=True)

    # 已封存池
    st.markdown("**已封存（不参与轮动）**")
    with st.expander("概念ETF + 宽基补仓（16只概念 + 4只宽基）"):
        fallback_df = pd.DataFrame([{"代码": code, "名称": name} for code, name in FALLBACK_EQUITY_UNIVERSE.items()])
        st.caption("宽基补仓：暂停使用，等待进一步验证")
        st.dataframe(fallback_df, use_container_width=True, hide_index=True)

        from config import CONCEPT_UNIVERSE
        concept_df = pd.DataFrame([{"代码": code, "名称": name} for code, name in CONCEPT_UNIVERSE.items()])
        st.caption("概念ETF：已封存。测试显示加入后全区间收益从132%降至79%，为负贡献")
        st.dataframe(concept_df, use_container_width=True, hide_index=True)


def render_live_trading(cfg, is_b0_18=True):
    """实盘助手页面 v0.1"""
    import sys
    sys.path.insert(0, "src")
    from live_trading_assistant import LiveTradingAssistant, CASH_TICKER, ActualTrade

    st.header("实盘助手")
    st.markdown('<div class="section-note">真实持仓以用户录入为准，模型只生成目标组合和交易建议。v0.1 不自动下单。</div>', unsafe_allow_html=True)

    assistant = LiveTradingAssistant(
        positions_path=os.path.join("data", "live", "actual_positions.csv"),
        trades_path=os.path.join("data", "live", "actual_trades.csv"),
        plan_path=os.path.join("data", "live", "latest_trade_plan.csv"),
        config=cfg,
    )

    # ------------------------------------------------------------------
    # 子页面导航
    # ------------------------------------------------------------------
    sub_tab_holdings, sub_tab_alerts, sub_tab_plan, sub_tab_trades = st.tabs(
        ["持仓管理", "止损检查", "调仓建议", "成交记录"]
    )

    # ------------------------------------------------------------------
    # 子页1: 持仓管理
    # ------------------------------------------------------------------
    with sub_tab_holdings:
        st.subheader("当前真实持仓")
        positions_df = assistant.load_positions()

        if positions_df.empty:
            st.info("持仓为空，请通过下方上传 CSV 或手动录入。")
        else:
            # 计算关键指标
            cash_rows = positions_df[positions_df["ticker"] == CASH_TICKER]
            cash = float(cash_rows.iloc[0]["market_value"]) if not cash_rows.empty else 0.0
            holdings = positions_df[positions_df["ticker"] != CASH_TICKER]
            total_mv = holdings["market_value"].sum()
            total_asset = cash + total_mv

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("总资产", f"{total_asset:,.2f}")
            k2.metric("现金", f"{cash:,.2f}")
            k3.metric("持仓市值", f"{total_mv:,.2f}")
            k4.metric("总仓位", f"{total_mv/total_asset:.1%}" if total_asset > 0 else "N/A")

            st.dataframe(holdings[["ticker", "name", "shares", "cost_price", "current_price", "market_value", "update_time"]],
                         use_container_width=True, hide_index=True)

            # 校验
            if st.button("校验持仓"):
                report = assistant.validate_positions(positions_df)
                if report.ok:
                    st.success("✅ 持仓校验通过")
                else:
                    for e in report.errors:
                        st.error(f"❌ {e}")
                    for w in report.warnings:
                        st.warning(f"⚠️ {w}")

        st.divider()
        st.subheader("更新持仓")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**方式1: 上传 CSV**")
            uploaded = st.file_uploader("上传持仓 CSV", type=["csv"], key="live_positions_upload")
            if uploaded:
                try:
                    new_df = pd.read_csv(uploaded)
                    assistant.save_positions(new_df)
                    st.success("✅ 持仓已更新")
                    st.rerun()
                except Exception as e:
                    st.error(f"上传失败: {e}")

        with col2:
            st.markdown("**方式2: 手动录入**")
            with st.form("manual_position"):
                t = st.text_input("ticker", "")
                n = st.text_input("名称", "")
                s = st.number_input("股数", min_value=0, step=100, value=0)
                cp = st.number_input("成本价", min_value=0.0, step=0.001, value=0.0)
                pp = st.number_input("现价", min_value=0.0, step=0.001, value=0.0)
                cash_input = st.number_input("现金", min_value=0.0, step=1000.0, value=0.0)
                submitted = st.form_submit_button("保存")
                if submitted:
                    rows = []
                    if t and s > 0 and pp > 0:
                        rows.append({
                            "ticker": t, "name": n, "shares": s, "cost_price": cp,
                            "current_price": pp, "market_value": s * pp,
                            "available_cash": 0, "update_time": datetime.now().strftime("%Y-%m-%d"),
                        })
                    if cash_input > 0:
                        rows.append({
                            "ticker": CASH_TICKER, "name": CASH_NAME, "shares": 0,
                            "cost_price": 0, "current_price": 0, "market_value": cash_input,
                            "available_cash": cash_input + sum(r["market_value"] for r in rows if r["ticker"] != CASH_TICKER),
                            "update_time": datetime.now().strftime("%Y-%m-%d"),
                        })
                    if rows:
                        existing = assistant.load_positions()
                        for r in rows:
                            mask = existing["ticker"] == r["ticker"]
                            if mask.any():
                                for k, v in r.items():
                                    existing.loc[mask, k] = v
                            else:
                                existing = pd.concat([existing, pd.DataFrame([r])], ignore_index=True)
                        assistant.save_positions(existing)
                        st.success("✅ 已保存")
                        st.rerun()

        # 图片识别预留
        st.divider()
        st.subheader("截图上传 (v0.1 预留接口)")
        img = st.file_uploader("上传券商持仓截图", type=["png", "jpg"], key="live_screenshot")
        if img:
            st.image(img, caption="已上传截图")
            st.info("v0.1 暂不支持自动 OCR，请手动录入持仓。v0.2 将接入自动识别。")

    # ------------------------------------------------------------------
    # 子页2: 止损检查
    # ------------------------------------------------------------------
    with sub_tab_alerts:
        st.subheader("每日止损检查")
        if st.button("运行止损检查"):
            alerts = assistant.check_stop_loss()
            if alerts.empty:
                st.success("✅ 今日无触发止损的持仓")
            else:
                st.warning(f"⚠️ 今日触发 {len(alerts)} 只持仓止损")
                st.dataframe(alerts[["ticker", "name", "shares", "cost_price", "current_price", "loss_pct"]],
                             use_container_width=True, hide_index=True)
            # 生成报告
            content = assistant.generate_daily_alert()
            st.download_button("下载报告", content, file_name="daily_stop_loss_alert.md")

    # ------------------------------------------------------------------
    # 子页3: 调仓建议
    # ------------------------------------------------------------------
    with sub_tab_plan:
        st.subheader("每周调仓建议")

        # 检查持仓是否为空
        positions_check = assistant.load_positions()
        actual_holdings = positions_check[positions_check["ticker"] != "__CASH__"]
        if actual_holdings.empty:
            st.info("📌 当前无持仓（只有现金），以下为首次建仓建议。")
        else:
            st.info("v0.1 调仓建议需通过命令行生成: py scripts/live_generate_trade_plan.py")
        if os.path.exists(assistant.plan_path):
            plan_df = pd.read_csv(assistant.plan_path)
            if not plan_df.empty:
                st.dataframe(plan_df, use_container_width=True, hide_index=True)
                st.download_button("下载交易计划 CSV", plan_df.to_csv(index=False), file_name="latest_trade_plan.csv")
            else:
                st.info("暂无调仓计划")
        else:
            st.info("暂无调仓计划，请先运行命令行生成")

    # ------------------------------------------------------------------
    # 子页4: 成交记录
    # ------------------------------------------------------------------
    with sub_tab_trades:
        st.subheader("成交记录")
        if os.path.exists(assistant.trades_path):
            trades_df = pd.read_csv(assistant.trades_path)
            st.dataframe(trades_df, use_container_width=True, hide_index=True)
        else:
            st.info("暂无成交记录")

        st.divider()
        st.subheader("手动录入成交")
        with st.form("manual_trade"):
            c1, c2 = st.columns(2)
            with c1:
                td = st.date_input("日期", datetime.now())
                tt = st.text_input("ticker", "")
                ta = st.selectbox("操作", ["BUY", "SELL"])
            with c2:
                ts = st.number_input("股数", min_value=0, step=100, value=0)
                tp = st.number_input("成交价格", min_value=0.0, step=0.001, value=0.0)
                tc = st.number_input("佣金", min_value=0.0, step=0.1, value=0.1)
            tn = st.text_input("备注", "")
            submitted = st.form_submit_button("记录成交")
            if submitted and tt and ts > 0 and tp > 0:
                trade = ActualTrade(
                    date=td.strftime("%Y-%m-%d"), ticker=tt, action=ta,
                    shares=ts, actual_price=tp, commission=tc, note=tn,
                )
                assistant.apply_trade(trade)
                st.success(f"✅ 已记录成交: {ta} {tt} {ts}股 @ {tp}")
                st.rerun()


def main():
    inject_style()
    cfg, is_b0_18 = build_sidebar_config()
    render_header()

    tab_dashboard, tab_backtest, tab_etf, tab_data, tab_config, tab_live = st.tabs(
        ["仪表盘", "回测结果", "ETF分析", "数据管理", "策略配置", "实盘助手"]
    )

    with tab_dashboard:
        render_dashboard(cfg, is_b0_18)
    with tab_backtest:
        render_backtest(cfg, is_b0_18)
    with tab_etf:
        render_etf_analysis(cfg, is_b0_18)
    with tab_data:
        render_data_management()
    with tab_config:
        render_strategy_config(cfg, is_b0_18)
    with tab_live:
        render_live_trading(cfg, is_b0_18)

    st.divider()
    st.caption("B0-18 主线 | 18只行业ETF轮动 | 概念池已封存 | 参数来自侧边栏实时状态")


if __name__ == "__main__":
    main()
