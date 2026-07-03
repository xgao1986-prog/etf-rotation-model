# src/paper_trading/ui.py — Streamlit virtual-account page rendering only.
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from paper_trading.models import AccountType, ManualFill, OpeningPosition, OrderStatus
from paper_trading.metrics import build_account_comparison, calculate_account_metrics
from strategy_presets import load_strategy_presets


def render_paper_trading_page(ui_service, data_provider):
    """Render the standalone "虚拟盘" page."""
    st.header("📊 虚拟盘")
    st.caption("基于已保存策略预设创建虚拟账户，自动运行对比账户并手动确认影子账户订单。")

    tab_overview, tab_create, tab_run, tab_orders, tab_details = st.tabs(
        ["账户总览", "创建账户", "今日运行", "待确认订单", "账户详情与策略对比"]
    )

    presets = load_strategy_presets()
    summaries = ui_service.list_account_summaries()

    with tab_overview:
        _render_account_overview(ui_service, summaries)

    with tab_create:
        _render_account_creation(ui_service, presets)

    with tab_run:
        _render_daily_run(ui_service, summaries, data_provider)

    with tab_orders:
        _render_pending_orders(ui_service, summaries)

    with tab_details:
        _render_account_details(ui_service, summaries)


def _render_account_overview(ui_service, summaries: List[Dict[str, Any]]):
    st.subheader("账户总览")
    if not summaries:
        st.info("暂无虚拟账户，请先在“创建账户”页签新建。")
        return

    df = pd.DataFrame(summaries)
    df['收益'] = df.apply(
        lambda r: f"{((r['nav'] / r['initial_capital']) - 1) * 100:.2f}%"
        if r['initial_capital'] else "-",
        axis=1,
    )

    metric_rows = []
    for s in summaries:
        history = ui_service.service.store.list_nav_history(s['account_id'])
        trades = ui_service.service.store.list_trades(s['account_id'], None)
        if history:
            metrics = calculate_account_metrics(history, trades)
            metric_rows.append({
                'account_id': s['account_id'],
                '年化收益': f"{metrics['annualized_return'] * 100:.2f}%",
                '夏普': f"{metrics['sharpe']:.2f}" if not pd.isna(metrics['sharpe']) else '-',
                '最大回撤': f"{metrics['max_drawdown'] * 100:.2f}%",
                'Calmar': f"{metrics['calmar']:.2f}" if not pd.isna(metrics['calmar']) else '-',
                '胜率': f"{metrics['win_rate'] * 100:.1f}%" if not pd.isna(metrics['win_rate']) else '-',
                '换手': f"{metrics['turnover'] * 100:.1f}%",
                '佣金': f"¥{metrics['total_commission']:,.2f}",
            })
        else:
            metric_rows.append({
                'account_id': s['account_id'],
                '年化收益': '-',
                '夏普': '-',
                '最大回撤': '-',
                'Calmar': '-',
                '胜率': '-',
                '换手': '-',
                '佣金': '-',
            })
    if metric_rows:
        df = df.merge(pd.DataFrame(metric_rows), on='account_id', how='left')

    display_cols = [
        'name', 'account_type', 'strategy_name', 'cash', 'positions_value',
        'nav', '收益', '年化收益', '夏普', '最大回撤', 'Calmar',
        '胜率', '换手', '佣金', 'latest_nav_date', 'status',
    ]
    st.dataframe(df[[c for c in display_cols if c in df.columns]], use_container_width=True)


def _render_account_creation(ui_service, presets: Mapping[str, Mapping[str, Any]]):
    st.subheader("创建账户")

    creation_type = st.radio("账户类型", ["对比账户", "影子账户"], horizontal=True)

    if creation_type == "对比账户":
        with st.form("create_comparison_accounts"):
            selected_presets = st.multiselect(
                "选择策略预设", options=list(presets.keys()), default=[]
            )
            initial_capital = st.number_input(
                "统一初始资金", min_value=10_000.0, value=1_000_000.0, step=10_000.0
            )
            start_date = st.date_input("起始日期", value=pd.Timestamp('2026-06-29'))
            submitted = st.form_submit_button("批量创建")

        if submitted:
            if not selected_presets:
                st.warning("请至少选择一个策略预设。")
                return
            try:
                ids = ui_service.create_comparison_accounts(
                    preset_names=selected_presets,
                    presets=presets,
                    initial_capital=float(initial_capital),
                    start_date=start_date.strftime('%Y-%m-%d'),
                )
                st.success(f"已创建 {len(ids)} 个对比账户：{', '.join(ids)}")
                st.rerun()
            except Exception as exc:
                st.error(f"创建失败：{exc}")

    else:
        with st.form("create_shadow_account"):
            preset_name = st.selectbox("策略预设", options=list(presets.keys()))
            name = st.text_input("账户名称", value="我的实盘")
            total_nav = st.number_input(
                "总资产（现金 + 持仓市值）", min_value=0.0, value=1_000_000.0, step=10_000.0
            )
            cash = st.number_input("现金", min_value=0.0, value=900_000.0, step=10_000.0)
            holdings_text = st.text_area(
                "持仓（每行：代码,股数,成本价,现价）",
                value="512400.SH,10000,10.0,10.0",
            )
            start_date = st.date_input("起始日期", value=pd.Timestamp('2026-06-29'), key='shadow_start')
            submitted = st.form_submit_button("创建影子账户")

        if submitted:
            positions = []
            try:
                for line in holdings_text.strip().splitlines():
                    if not line.strip():
                        continue
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) != 4:
                        raise ValueError(f"持仓格式错误：{line}")
                    ticker, shares, cost, last = parts
                    positions.append(
                        OpeningPosition(ticker, int(shares), float(cost), float(last))
                    )
            except Exception as exc:
                st.error(f"持仓解析失败：{exc}")
                return

            try:
                account_id = ui_service.create_shadow_account(
                    name=name,
                    preset_name=preset_name,
                    preset=presets[preset_name],
                    initial_capital=float(total_nav),
                    opening_cash=float(cash),
                    opening_positions=tuple(positions),
                    start_date=start_date.strftime('%Y-%m-%d'),
                )
                st.success(f"已创建影子账户：{account_id}")
                st.rerun()
            except Exception as exc:
                st.error(f"创建失败：{exc}")


def _render_daily_run(
    ui_service,
    summaries: List[Dict[str, Any]],
    data_provider,
):
    st.subheader("今日运行")

    if not summaries:
        st.info("暂无账户可运行。")
        return

    open_prices, close_prices, scores_df, data_date, coverage = data_provider()

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("行情数据价格数", f"{coverage['actual_price_count']}/{coverage['expected_price_count']}")
    with col2:
        st.metric("评分数据行数", coverage['actual_score_count'])
    with col3:
        st.metric("待运行账户数", len(summaries))

    st.caption(f"行情与评分数据日期：{data_date or '未知'}")

    missing_prices = coverage.get('missing_prices', [])
    missing_scores = coverage.get('missing_scores', [])
    invalid_prices = coverage.get('invalid_prices', [])
    invalid_scores = coverage.get('invalid_scores', [])
    if missing_prices:
        st.warning(f"⚠️ 缺少 {len(missing_prices)} 只 ETF 价格：{', '.join(missing_prices)}")
    for item in invalid_prices:
        st.warning(f"⚠️ {item['ticker']} 价格无效：{item['reason']}")
    if missing_scores:
        st.warning(f"⚠️ 缺少 {len(missing_scores)} 只 ETF 评分：{', '.join(missing_scores)}")
    for item in invalid_scores:
        st.warning(f"⚠️ {item['ticker']} 评分无效：{item['reason']}")
    if coverage['actual_score_count'] == 0:
        st.error("评分数据为空，无法运行。")

    default_trade_date = pd.Timestamp(data_date) if data_date else pd.Timestamp('2026-07-03')
    trade_date = st.date_input("交易日期", value=default_trade_date)
    trade_date_str = trade_date.strftime('%Y-%m-%d')

    selected = st.multiselect(
        "选择要运行的账户",
        options=[s['account_id'] for s in summaries],
        default=[s['account_id'] for s in summaries],
        format_func=lambda x: next((s['name'] for s in summaries if s['account_id'] == x), x),
    )

    if st.button("运行选中账户", use_container_width=True):
        if data_date is None:
            st.error("无法获取行情数据日期，请检查数据库。")
            return
        if trade_date_str != data_date:
            st.error(
                f"交易日期 ({trade_date_str}) 与行情数据日期 ({data_date}) 不一致，已阻止运行。"
            )
            return
        if missing_prices:
            st.error(
                f"价格数据不完整，缺少 {len(missing_prices)} 只 ETF："
                f"{', '.join(missing_prices)}，已阻止运行。"
            )
            return
        if invalid_prices:
            for item in invalid_prices:
                st.error(f"{item['ticker']} 价格无效：{item['reason']}，已阻止运行。")
            return
        if missing_scores or coverage['actual_score_count'] == 0:
            if missing_scores:
                st.error(
                    f"评分数据不完整，缺少 {len(missing_scores)} 只 ETF："
                    f"{', '.join(missing_scores)}，已阻止运行。"
                )
            if coverage['actual_score_count'] == 0:
                st.error("评分数据为空，已阻止运行。")
            return
        if invalid_scores:
            for item in invalid_scores:
                st.error(f"{item['ticker']} 评分无效：{item['reason']}，已阻止运行。")
            return
        results = ui_service.run_accounts(
            account_ids=selected,
            trade_date=trade_date_str,
            open_prices=open_prices,
            close_prices=close_prices,
            scores_df=scores_df,
        )
        if results['success']:
            st.success(f"成功运行 {len(results['success'])} 个账户")
        if results['failure']:
            st.error(f"{len(results['failure'])} 个账户运行失败")
            for account_id, reason in results['failure'].items():
                st.error(f"{account_id}: {reason}")
        st.rerun()


def _render_pending_orders(ui_service, summaries: List[Dict[str, Any]]):
    st.subheader("待确认订单")

    pending = ui_service.list_pending_shadow_orders()
    if not pending:
        st.info("没有待确认的影子账户订单。")
        return

    for order in pending:
        account_name = next(
            (s['name'] for s in summaries if s['account_id'] == order['account_id']),
            order['account_id'],
        )
        with st.form(f"shadow_order_{order['order_id']}"):
            st.write(f"**{account_name}** | {order['ticker']} | {order['action']} | "
                     f"建议 {order['delta_shares']} 股 @ {order['reference_price']:.3f}")
            cols = st.columns(3)
            with cols[0]:
                actual_price = st.number_input(
                    "实际价格", min_value=0.001, value=float(order['reference_price']),
                    step=0.001, key=f"price_{order['order_id']}",
                )
            with cols[1]:
                actual_shares = st.number_input(
                    "实际数量", min_value=0, value=int(abs(order['delta_shares'])),
                    step=100, key=f"shares_{order['order_id']}",
                )
            with cols[2]:
                reason = st.text_input("未执行/拒绝原因", key=f"reason_{order['order_id']}")

            c1, c2, c3 = st.columns(3)
            with c1:
                confirm = st.form_submit_button("✅ 确认成交")
            with c2:
                reject = st.form_submit_button("❌ 标记未执行")
            with c3:
                cancel = st.form_submit_button("🚫 取消")

        if confirm:
            try:
                fill = ManualFill(
                    account_id=order['account_id'],
                    order_id=order['order_id'],
                    trade_date=order['trade_date'],
                    actual_price=float(actual_price),
                    actual_shares=int(actual_shares),
                )
                ui_service.service.confirm_shadow_order(fill)
                st.success(f"已确认 {order['order_id']}")
                st.rerun()
            except Exception as exc:
                st.error(f"确认失败：{exc}")

        if reject:
            if not reason or not str(reason).strip():
                st.error("请填写未执行原因后再标记未执行。")
            else:
                try:
                    ui_service.service.reject_shadow_order(
                        order['account_id'], order['order_id'], reason
                    )
                    st.success(f"已标记未执行 {order['order_id']}")
                    st.rerun()
                except Exception as exc:
                    st.error(f"拒绝失败：{exc}")

        if cancel:
            if not reason or not str(reason).strip():
                st.error("请填写取消原因后再取消订单。")
            else:
                try:
                    ui_service.service.cancel_shadow_order(
                        order['account_id'], order['order_id'], reason
                    )
                    st.success(f"已取消 {order['order_id']}")
                    st.rerun()
                except Exception as exc:
                    st.error(f"取消失败：{exc}")


def _render_account_details(ui_service, summaries: List[Dict[str, Any]]):
    st.subheader("账户详情与策略对比")

    if not summaries:
        st.info("暂无账户。")
        return

    options = {f"{s['name']} ({s['account_id']})": s['account_id'] for s in summaries}
    selected_key = st.selectbox("选择账户", options=list(options.keys()))
    selected_id = options[selected_key]

    nav_history = ui_service.service.store.list_nav_history(selected_id)
    trades = ui_service.service.store.list_trades(selected_id, None)
    orders = ui_service.service.store.list_orders(selected_id)

    if nav_history:
        metrics = calculate_account_metrics(nav_history, trades)
        st.write("**绩效指标**")
        st.write(pd.DataFrame([metrics]).T.rename(columns={0: '值'}))

    st.write("**持仓**")
    if nav_history:
        positions = ui_service.service.store.list_positions(selected_id, nav_history[-1]['nav_date'])
        if positions:
            st.dataframe(pd.DataFrame(positions), use_container_width=True)
        else:
            st.write("无持仓")

    st.write("**历史订单**")
    if orders:
        st.dataframe(pd.DataFrame(orders), use_container_width=True)
    else:
        st.write("无订单")

    st.write("**历史成交**")
    if trades:
        st.dataframe(pd.DataFrame(trades), use_container_width=True)
    else:
        st.write("无成交")

    st.write("**净值与回撤曲线**")
    if len(nav_history) >= 2:
        nav_df = pd.DataFrame(nav_history)
        nav_df['nav_date'] = pd.to_datetime(nav_df['nav_date'])
        nav_df = nav_df.sort_values('nav_date')
        nav_df['drawdown'] = nav_df['nav'] / nav_df['nav'].cummax() - 1

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=nav_df['nav_date'], y=nav_df['nav'],
            mode='lines', name='NAV', line=dict(color='#1769aa')
        ))
        fig.update_layout(
            title='净值曲线',
            xaxis_title='日期',
            yaxis_title='NAV',
            height=400,
            margin=dict(l=10, r=16, t=40, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)

        dd_fig = go.Figure()
        dd_fig.add_trace(go.Scatter(
            x=nav_df['nav_date'], y=nav_df['drawdown'],
            mode='lines', name='回撤', fill='tozeroy',
            line=dict(color='#d64f4f')
        ))
        dd_fig.update_layout(
            title='回撤曲线',
            xaxis_title='日期',
            yaxis_title='回撤',
            height=320,
            margin=dict(l=10, r=16, t=40, b=10),
        )
        st.plotly_chart(dd_fig, use_container_width=True)
    else:
        st.write("历史净值不足，无法绘制曲线。")

    st.write("**策略对比（以 B0.4 为参照）**")
    selected_summary = next(s for s in summaries if s['account_id'] == selected_id)
    peers = [
        s for s in summaries
        if s.get('account_type') == 'COMPARISON'
        and s.get('group_id')
        and s.get('group_id') == selected_summary.get('group_id')
        and s.get('start_date') == selected_summary.get('start_date')
    ]
    if len(peers) < 2:
        st.info("同一批次内暂无其他对比账户可供对比。")
    else:
        ref_summary = next(
            (s for s in peers
             if s.get('strategy_name') == 'B0.4'
             or 'B0.4' in str(s.get('strategy_name', ''))),
            None,
        )
        if ref_summary is None:
            st.info("同一批次中未找到 B0.4 参照账户。")
        else:
            all_metrics = {}
            for s in peers:
                history = ui_service.service.store.list_nav_history(s['account_id'])
                account_trades = ui_service.service.store.list_trades(s['account_id'], None)
                if history:
                    all_metrics[s['name']] = calculate_account_metrics(history, account_trades)
            if all_metrics:
                comparison = build_account_comparison(all_metrics, reference_name=ref_summary['name'])
                st.dataframe(comparison, use_container_width=True)
