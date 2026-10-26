#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_settings_consistency.py — 账户设置与实际执行一致性（第一检查点，含复审补修）

审计发现（reports/settings_consistency_audit_20261022.md）：
- F1 虚拟盘止损线硬编码 -0.08，账户保存的 stop_loss 被静默忽略
- F2 虚拟盘调仓日硬编码周四，账户配置无入口
- F3 虚拟盘 defense_enabled 硬编码 True
- F4 虚拟盘入场只过滤 total_score，不过滤 trend/confirm/均线条件
- F5 标的池运行时读全局 config 模块，侧栏"防御资产模式"改动泄漏到已有账户
- F6 创建账户不校验 strategy_config，不支持项保存后静默忽略
- F7 实盘助手止损检查符号 bug（cfg 负值口径 → 阈值变 +8%，几乎全员误报）
- F8 实盘调仓脚本防御候选无评分门槛，与虚拟盘/回测 min_total-10 不一致
- F9 三路径一致性：相同输入下回测规划器/虚拟盘/实盘脚本应产出相同目标持仓

复审补修（2026-10-22 第二轮）：
- 补1 评分字段缺失/无效 → 停止生成交易并明确提示；删除"缺字段退回仅看总分"宽松路径
- 补2 完整 B0.4 入场条件（弱市55分门槛/防御资格/数据成熟期 history_count>=51/
  prev_close>ma20/ma20_slope）；三路径比较必须调真实生产信号函数 generate_signals
- 补3 暂不实现逐账户评分：与统一评分管线不兼容的 weights 预设拒绝创建；记录评分来源
- 补4 老账户（无 config_version）执行行为变化须显式确认并记录生效日期；
  未确认不得按新行为运行；确认时冻结标的池快照（不猜测还原）

全部测试使用临时数据库/临时账户/临时目录，不触碰真实数据库与真实持仓。
"""

import json, os, sys, tempfile, pytest
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from paper_trading.models import (
    AccountCreate, AccountType, OpeningPosition, StartMode,
    effective_pipeline_weights, resolve_execution_config,
)
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore
from paper_trading.runner import PaperTradingRunner
from paper_trading.ui_service import PaperTradingUIService

# B0.4 口径的账户配置（与 STRATEGY_CONFIG 默认值一致）
B04_ACCOUNT_CFG = {
    'min_trend_score': 15,
    'min_confirm_score': 4,
    'min_total_score': 40,
    'max_holdings': 5,
    'max_position_per_etf': 0.20,
    'stop_loss': -0.08,
}

# 复审补修 ①：简化测试数据也必须含完整 B0.4 入场字段
FULL_SCORE_COLS = ['ticker', 'total_score', 'trend_score', 'confirm_score',
                   'ma20', 'ma20_slope', 'momentum_20', 'prev_close', 'history_count']


def _scores(rows):
    """rows: (ticker, total, trend, confirm, ma20, ma20_slope, momentum_20, prev_close, history_count)"""
    return pd.DataFrame(rows, columns=FULL_SCORE_COLS)


def _qualified(ticker, total=50.0):
    """一只完全满足 B0.4 入场条件的评分行（好市场：momentum_20>0）。"""
    return (ticker, total, 20.0, 10.0, 1.0, 0.01, 0.10, 1.2, 100)


@pytest.fixture
def service():
    with tempfile.TemporaryDirectory() as d:
        yield PaperTradingService(PaperTradingStore(os.path.join(d, 'paper.db')))


@pytest.fixture
def runner(service):
    return PaperTradingRunner(service)


def _make_account(service, account_id='acct-t', cash=1_000_000, holdings=None,
                  strategy_config=None, start_date='2026-06-29'):
    holdings = holdings or []
    strategy_config = dict(B04_ACCOUNT_CFG if strategy_config is None else strategy_config)
    if holdings:
        initial = cash + sum(h.market_value for h in holdings)
        request = AccountCreate(
            account_id=account_id, name='T', account_type=AccountType.COMPARISON,
            strategy_name='B0.4', strategy_config=strategy_config,
            initial_capital=initial, start_mode=StartMode.IMPORTED,
            start_date=start_date, opening_cash=cash,
            opening_positions=tuple(holdings),
        )
    else:
        request = AccountCreate(
            account_id=account_id, name='T', account_type=AccountType.COMPARISON,
            strategy_name='B0.4', strategy_config=strategy_config,
            initial_capital=cash, start_mode=StartMode.CASH, start_date=start_date,
        )
    service.create_account(request)
    return account_id


def _insert_legacy_account(service, account_id='acct-old', config=None,
                           cash=1_000_000, start_date='2026-06-29'):
    """直接写库构造"修复前创建"的老账户（config_json 无 config_version）。"""
    from paper_trading.models import canonical_config, config_hash
    cfg = dict(B04_ACCOUNT_CFG if config is None else config)
    now = '2026-06-29T09:00:00'
    service.store.create_account_snapshot(
        {
            'account_id': account_id, 'name': 'Old', 'account_type': 'COMPARISON',
            'group_id': None, 'strategy_name': 'B0.4',
            'config_json': canonical_config(cfg), 'config_hash': config_hash(cfg),
            'initial_capital': cash, 'start_mode': 'CASH',
            'start_date': start_date, 'end_date': None, 'status': 'READY',
            'created_at': now, 'updated_at': now,
        },
        [],
        {'account_id': account_id, 'nav_date': start_date, 'cash': cash,
         'positions_value': 0.0, 'nav': cash, 'data_date': start_date,
         'created_at': now},
    )
    return account_id


# ============== F1: 账户止损线必须真正生效 ==============

class TestAccountStopLoss:
    def test_tighter_stop_loss_triggers(self, service, runner):
        """账户保存 stop_loss=-0.05，持仓亏 6%：必须触发止损（旧代码按 -8% 不触发）。"""
        acct = _make_account(
            service, cash=800_000,
            holdings=[OpeningPosition('512400.SH', 200_000, 1.0, 1.0)],
            strategy_config={**B04_ACCOUNT_CFG, 'stop_loss': -0.05},
        )
        r = runner.run_daily(acct, '2026-06-30',
                             {'512400.SH': 0.94}, {'512400.SH': 0.94})
        sl = [t for t in r['trades'] if t['ticker'] == '512400.SH' and t['action'] == 'STOP_LOSS']
        assert len(sl) == 1, f"stop_loss=-0.05 且亏损6%，应触发止损，实际成交: {r['trades']}"
        assert sl[0]['shares'] == 200_000

    def test_looser_stop_loss_does_not_trigger(self, service, runner):
        """账户保存 stop_loss=-0.15，持仓亏 6%：不得触发止损（旧代码按 -8% 会误触发）。"""
        acct = _make_account(
            service, cash=800_000,
            holdings=[OpeningPosition('512400.SH', 200_000, 1.0, 1.0)],
            strategy_config={**B04_ACCOUNT_CFG, 'stop_loss': -0.15},
        )
        r = runner.run_daily(acct, '2026-06-30',
                             {'512400.SH': 0.94}, {'512400.SH': 0.94})
        sl = [t for t in r['trades'] if t['action'] == 'STOP_LOSS']
        assert len(sl) == 0, f"stop_loss=-0.15 且亏损6%，不应触发止损，实际成交: {r['trades']}"
        assert r['positions']['512400.SH']['shares'] == 200_000


# ============== F2: 账户调仓日必须真正生效 ==============

class TestAccountRebalanceWeekday:
    def test_monday_rebalance_account_ignores_thursday(self, service, runner):
        """账户配置 rebalance_weekday=0（周一）：周四不产生信号，周一产生。"""
        acct = _make_account(
            service, strategy_config={**B04_ACCOUNT_CFG, 'rebalance_weekday': 0})
        scores = _scores([_qualified('512400.SH')])
        prices = {'512400.SH': 10.0}

        # 周四 2026-07-02：旧代码（硬编码周四）会产生信号；周一账户不应产生
        runner.run_daily(acct, '2026-07-02', prices, prices, scores)
        thursday_signals = service.store.load_pending_signals(acct, '2026-07-03')
        assert len(thursday_signals) == 0, \
            f"周一调仓账户在周四不应产生信号，实际: {thursday_signals}"

        # 周一 2026-07-06：应产生信号，供周二执行
        runner.run_daily(acct, '2026-07-06', prices, prices, scores)
        monday_signals = service.store.load_pending_signals(acct, '2026-07-07')
        assert len(monday_signals) == 1, "周一调仓账户在周一应产生 1 条信号"


# ============== F3: 防御开关必须真正生效 ==============

class TestDefenseSwitch:
    def test_defense_disabled_account_never_buys_defense(self, service, runner):
        """账户 defense_enabled=False：防御资产评分再高也不买（旧代码硬编码 True 会买）。"""
        acct = _make_account(
            service, strategy_config={**B04_ACCOUNT_CFG, 'defense_enabled': False})
        # 行业无一达标（total 30 < 40），防御 518880.SH 高分 50
        scores = _scores([
            ('512400.SH', 30.0, 20.0, 10.0, 1.0, 0.01, 0.10, 1.2, 100),
            _qualified('518880.SH', 50.0),
        ])
        prices = {'512400.SH': 10.0, '518880.SH': 100.0}

        runner.run_daily(acct, '2026-07-02', prices, prices, scores)   # 周四出信号
        r = runner.run_daily(acct, '2026-07-03', prices, prices)        # 周五执行
        defense_buys = [t for t in r['trades']
                        if t['ticker'] == '518880.SH' and t['action'] == 'BUY']
        assert len(defense_buys) == 0, \
            f"defense_enabled=False 的账户不得买入防御资产，实际成交: {r['trades']}"
        assert r['cash'] == 1_000_000, f"现金应原封不动，实际: {r['cash']}"


# ============== F4: 入场过滤与回测语义对齐 ==============

class TestEntryFilterParity:
    def test_trend_confirm_thresholds_are_enforced(self, service, runner):
        """total 达标但 trend 不达标的标的不得买入（旧代码只看 total，会误买）。"""
        acct = _make_account(service)
        scores = _scores([
            ('512400.SH', 50.0, 5.0, 10.0, 1.0, 0.01, 0.10, 1.2, 100),  # trend 5 < 15
            _qualified('512480.SH'),                                    # 全部达标
        ])
        prices = {'512400.SH': 10.0, '512480.SH': 10.0}

        runner.run_daily(acct, '2026-07-02', prices, prices, scores)
        r = runner.run_daily(acct, '2026-07-03', prices, prices)
        bought = {t['ticker'] for t in r['trades'] if t['action'] == 'BUY'}
        assert bought == {'512480.SH'}, \
            f"只有全部条件达标的 512480.SH 应被买入，实际买入: {bought}"


# ============== F5: 标的池创建时冻结，不受全局变量后续改动影响 ==============

class TestUniverseFrozen:
    def test_defense_universe_frozen_at_creation(self, service, runner, monkeypatch):
        """创建后即使全局 DEFENSE_UNIVERSE 被改为'仅黄金'，账户仍按创建时的池运行。"""
        import config as config_module
        acct = _make_account(service)
        # 模拟侧栏切换"仅黄金"：国债被移出全局池
        monkeypatch.setattr(config_module, 'DEFENSE_UNIVERSE', {'518880.SH': '黄金ETF'})

        scores = _scores([
            ('512400.SH', 30.0, 20.0, 10.0, 1.0, 0.01, 0.10, 1.2, 100),  # 行业不达标
            _qualified('511010.SH', 45.0),                               # 国债达标（门槛30）
        ])
        prices = {'512400.SH': 10.0, '511010.SH': 100.0, '518880.SH': 100.0}

        runner.run_daily(acct, '2026-07-02', prices, prices, scores)
        r = runner.run_daily(acct, '2026-07-03', prices, prices)
        bought = {t['ticker'] for t in r['trades'] if t['action'] == 'BUY'}
        assert '511010.SH' in bought, \
            f"账户创建时冻结的防御池含国债，侧栏改动不得影响已有账户，实际买入: {bought}"


# ============== F6: 不支持的设置必须拒绝创建并明确提示 ==============

class TestUnsupportedSettingsBlocked:
    @pytest.mark.parametrize('bad_key,bad_value', [
        ('market_timing', True),
        ('stop_loss_mode', 'atr'),
        ('stop_loss_mode', 'none'),
        ('trailing_stop_mode', 'tiered'),
        ('cooling_period', 5),
        ('fallback_equity_enabled', True),
        ('rebalance_freq', 'monthly'),
        ('some_unknown_key', 123),
    ])
    def test_unsupported_config_rejected(self, service, bad_key, bad_value):
        """包含不支持设置的账户配置必须被拒绝，且错误信息指明具体设置项。"""
        cfg = {**B04_ACCOUNT_CFG, bad_key: bad_value}
        request = AccountCreate(
            account_id='acct-bad', name='Bad', account_type=AccountType.COMPARISON,
            strategy_name='X', strategy_config=cfg,
            initial_capital=1_000_000, start_mode=StartMode.CASH,
            start_date='2026-06-29',
        )
        with pytest.raises(ValueError) as excinfo:
            service.create_account(request)
        assert bad_key in str(excinfo.value), \
            f"错误信息必须指明不支持的设置项 {bad_key}，实际: {excinfo.value}"

    def test_supported_b04_config_accepted(self, service):
        """B0.4 口径配置（含调仓日/防御开关）必须可以正常创建。"""
        acct = _make_account(service, account_id='acct-ok',
                             strategy_config={**B04_ACCOUNT_CFG,
                                              'defense_enabled': True,
                                              'rebalance_weekday': 3})
        assert service.get_account(acct)['account_id'] == 'acct-ok'


# ============== F7: 实盘助手止损检查符号 ==============

class TestLiveStopLossSign:
    def test_negative_config_stop_loss_uses_magnitude(self):
        """config 传入 -0.08（负值口径）时：亏 5% 不报警，亏 10% 报警。

        旧代码 `-stop_loss_pct` 把阈值变成 +8%，亏 5% 也会误报。
        """
        from live_trading_assistant import LiveTradingAssistant, CASH_TICKER
        with tempfile.TemporaryDirectory() as d:
            assistant = LiveTradingAssistant(
                positions_path=os.path.join(d, 'pos.csv'),
                trades_path=os.path.join(d, 'trades.csv'),
                plan_path=os.path.join(d, 'plan.csv'),
                config={'stop_loss': -0.08},
            )
            df = pd.DataFrame([
                {'ticker': '512400.SH', 'name': 'A', 'shares': 1000,
                 'cost_price': 1.0, 'current_price': 0.95, 'market_value': 950.0,
                 'available_cash': 0, 'update_time': '2026-10-22'},
                {'ticker': '512480.SH', 'name': 'B', 'shares': 1000,
                 'cost_price': 1.0, 'current_price': 0.90, 'market_value': 900.0,
                 'available_cash': 0, 'update_time': '2026-10-22'},
                {'ticker': CASH_TICKER, 'name': '现金', 'shares': 0,
                 'cost_price': 0, 'current_price': 0, 'market_value': 100000.0,
                 'available_cash': 101850.0, 'update_time': '2026-10-22'},
            ])
            alerts = assistant.check_stop_loss(df)
            alerted = set(alerts['ticker']) if not alerts.empty else set()
            assert '512480.SH' in alerted, "亏 10% 必须触发 -8% 止损报警"
            assert '512400.SH' not in alerted, \
                f"亏 5% 不得触发 -8% 止损报警，实际报警: {alerted}"


# ============== F8 / 补1：实盘调仓脚本门槛与字段纪律 ==============

class _MockDB:
    def __init__(self, scores_df, histories):
        """histories: {ticker: 多行行情 DataFrame}（复审补修后富化需要历史）"""
        self._scores = scores_df
        self._histories = histories

    def get_scores(self, date=None, ticker=None):
        return self._scores.copy()

    def get_market_data(self, ticker=None, start_date=None, end_date=None):
        if ticker not in self._histories:
            return pd.DataFrame()
        df = self._histories[ticker].copy()
        if start_date and end_date:
            df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
        return df


def _market_history(ticker, prev_close=1.0, today_close=1.0, end='2026-10-21', periods=60):
    """60 个工作日历史：最后一条 today_close，其余 prev_close。

    配合 _mock_scores 的 ma20=0.9：富化后 prev_close(=1.0) > ma20，满足入场。
    """
    dates = pd.bdate_range(end=end, periods=periods).strftime('%Y-%m-%d')
    closes = [prev_close] * (periods - 1) + [today_close]
    return pd.DataFrame({
        'ticker': ticker, 'date': dates,
        'open': today_close, 'high': today_close, 'low': today_close,
        'close': closes, 'volume': 1_000_000,
    })


def _live_assistant(tmpdir, cash=1_000_000.0):
    from live_trading_assistant import LiveTradingAssistant, CASH_TICKER
    assistant = LiveTradingAssistant(
        positions_path=os.path.join(tmpdir, 'pos.csv'),
        trades_path=os.path.join(tmpdir, 'trades.csv'),
        plan_path=os.path.join(tmpdir, 'plan.csv'),
        config={},
    )
    df = pd.DataFrame([
        {'ticker': CASH_TICKER, 'name': '现金', 'shares': 0, 'cost_price': 0,
         'current_price': 0, 'market_value': cash, 'available_cash': cash,
         'update_time': '2026-10-21'},
    ])
    assistant.save_positions(df)
    return assistant


def _mock_scores(rows, date='2026-10-21'):
    """完整 B0.4 字段的模拟评分；ma20=0.9 配合历史收盘 1.0 使 prev_close>ma20。"""
    full_rows = []
    for row in rows:
        ticker, total = row[0], row[1]
        trend = row[2] if len(row) > 2 else 20.0
        confirm = row[3] if len(row) > 3 else 10.0
        momentum = row[4] if len(row) > 4 else 0.05
        full_rows.append({
            'ticker': ticker, 'date': date, 'total_score': float(total),
            'trend_score': float(trend), 'confirm_score': float(confirm),
            'ma20': 0.9, 'ma20_slope': 0.01, 'momentum_20': momentum,
        })
    return pd.DataFrame(full_rows)


def _mock_db(rows, tickers, date='2026-10-21', today_close=1.0):
    scores = _mock_scores(rows, date=date)
    histories = {t: _market_history(t, end=date, today_close=today_close)
                 for t in tickers}
    return _MockDB(scores, histories)


class TestLivePlanThresholds:
    def test_defense_candidates_use_min_total_minus_10(self):
        """防御候选必须按 max(30, min_total-10) 过滤（与生产信号路径一致）。"""
        from config import build_config
        from live_generate_trade_plan import get_b0_4_signals
        with tempfile.TemporaryDirectory() as d:
            assistant = _live_assistant(d)
            db = _mock_db(
                [('512400.SH', 30.0),    # 行业不达标
                 ('518880.SH', 35.0),    # 35 >= 30，应入选
                 ('511010.SH', 25.0)],   # 25 < 30，应排除
                ['512400.SH', '518880.SH', '511010.SH'],
            )
            target, _ = get_b0_4_signals(assistant, '2026-10-21', build_config(), db=db)
            assert '518880.SH' in target, f"黄金 35 分应入选防御，实际: {target}"
            assert '511010.SH' not in target, f"国债 25 分应被门槛排除，实际: {target}"

    def test_industry_entry_filter_parity(self):
        """实盘脚本行业入场必须包含 trend/confirm/均线条件（与回测一致）。"""
        from config import build_config
        from live_generate_trade_plan import get_b0_4_signals
        with tempfile.TemporaryDirectory() as d:
            assistant = _live_assistant(d)
            db = _mock_db(
                [('512400.SH', 50.0, 5.0),   # trend 5 < 15 不达标
                 ('512480.SH', 50.0)],
                ['512400.SH', '512480.SH'],
            )
            target, _ = get_b0_4_signals(assistant, '2026-10-21', build_config(), db=db)
            assert set(target.keys()) == {'512480.SH'}, \
                f"只有全部条件达标的 512480.SH 应入选，实际: {target}"


# ============== 补1: 缺字段/无效字段停止生成交易（删除宽松回退） ==============

class TestMissingScoreFieldsBlocked:
    def test_runner_missing_column_stops_on_signal_day(self, service, runner):
        """缺 prev_close 列：调仓信号准备当日即报错，不保存信号、不生成交易。"""
        acct = _make_account(service)
        scores = pd.DataFrame({  # 旧式简化数据：只有 ticker/total_score
            'ticker': ['512400.SH'], 'total_score': [65.0],
        })
        with pytest.raises(ValueError) as excinfo:
            runner.run_daily(acct, '2026-07-02',
                             {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        assert '评分字段缺失' in str(excinfo.value), f"实际错误: {excinfo.value}"
        assert 'prev_close' in str(excinfo.value)
        assert not service.store.load_pending_signals(acct, '2026-07-03'), \
            "缺字段时不得保存任何调仓信号"
        assert not service.store.is_day_processed(acct, '2026-07-02'), \
            "失败当日不得标记为已处理"

    def test_runner_invalid_value_stops(self, service, runner):
        """trend_score 为 NaN（字段无效）：停止生成交易并指明字段与标的。"""
        acct = _make_account(service)
        scores = _scores([
            ('512400.SH', 65.0, float('nan'), 10.0, 1.0, 0.01, 0.10, 1.2, 100),
        ])
        with pytest.raises(ValueError) as excinfo:
            runner.run_daily(acct, '2026-07-02',
                             {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        msg = str(excinfo.value)
        assert '评分字段无效' in msg and 'trend_score' in msg and '512400.SH' in msg

    def test_live_script_missing_field_stops(self):
        """实盘脚本：评分缺 ma20_slope 列时抛出 ValueError（不再有宽松回退）。"""
        from config import build_config
        from live_generate_trade_plan import get_b0_4_signals
        with tempfile.TemporaryDirectory() as d:
            assistant = _live_assistant(d)
            scores = _mock_scores([('512400.SH', 65.0)]).drop(columns=['ma20_slope'])
            histories = {'512400.SH': _market_history('512400.SH')}
            db = _MockDB(scores, histories)
            with pytest.raises(ValueError) as excinfo:
                get_b0_4_signals(assistant, '2026-10-21', build_config(), db=db)
            assert 'ma20_slope' in str(excinfo.value)

    def test_no_total_only_fallback(self, service, runner):
        """被否决的宽松路径不复存在：仅 total_score 的数据必须报错而不是按总分过滤。"""
        acct = _make_account(service)
        scores = pd.DataFrame({'ticker': ['512400.SH'], 'total_score': [65.0]})
        prices = {'512400.SH': 1.0}
        with pytest.raises(ValueError):
            runner.run_daily(acct, '2026-07-02', prices, prices, scores)
        r = runner.run_daily  # 不继续执行，账户无交易
        assert service.store.list_trades(acct, '2026-07-02') == []


# ============== 补2: 三路径一致性（真实生产信号路径） ==============

D1, D2 = '2026-10-20', '2026-10-21'


def _signal_ts(specs):
    """构造 generate_signals 所需的两日期评分时间序列。

    specs: dict(ticker, close_d1, close_d2, ma20, slope, trend, confirm,
                total, momentum, hist)
    d1 行的 close 即 d2 行 prev_close=groupby('ticker')['close'].shift(1) 的来源。
    """
    rows = []
    for spec in specs:
        for date, close in ((D1, spec['close_d1']), (D2, spec['close_d2'])):
            rows.append({
                'ticker': spec['ticker'], 'date': date, 'close': close,
                'ma20': spec['ma20'], 'ma20_slope': spec['slope'],
                'trend_score': spec['trend'], 'confirm_score': spec['confirm'],
                'total_score': spec['total'], 'momentum_20': spec['momentum'],
                'momentum_valid': True, 'history_count': spec.get('hist', 100),
            })
    return pd.DataFrame(rows)


def _cross_section_for_runner(scores_ts):
    """路径 B 输入：d2 横截面 + 数据层富化的 prev_close（= d1 收盘）。"""
    cross = scores_ts[scores_ts['date'] == D2].copy()
    prev = scores_ts[scores_ts['date'] == D1].set_index('ticker')['close']
    cross['prev_close'] = cross['ticker'].map(prev)
    return cross


def _planner_targets(industry_candidates, defense_candidates, prices, cash, cfg):
    from rebalance_planner import plan_rebalance_v2_5
    import config as config_module
    _, state = plan_rebalance_v2_5(
        nav=cash, cash=cash, current_positions={},
        industry_candidates=industry_candidates,
        defense_candidates=defense_candidates,
        prices=dict(prices),
        industry_tickers=set(config_module.ETF_UNIVERSE.keys()),
        defense_tickers=set(config_module.DEFENSE_UNIVERSE.keys()),
        max_industry_holdings=5, max_defense_holdings=2, max_total_holdings=5,
        max_position_per_etf=0.20, max_total_position=1.0,
        commission_rate=0.0003, min_commission=5.0, lot_size=100,
        defense_enabled=True,
    )
    return {t: s for t, s in state['positions'].items() if s > 0}


def _production_path_targets(scores_ts, prices, cash, cfg):
    """路径 A：真实生产信号路径 StrategyEngine.generate_signals（回测同源）。

    generate_signals 已完成全部入场过滤；此处仅做 _rebalance_v2 的池路由，
    并断言其评分二次过滤对本数据为无操作（买入信号已满足更强约束）。
    """
    from strategy import StrategyEngine
    import config as config_module
    engine = StrategyEngine(cfg)
    signals = engine.generate_signals(scores_ts.copy(), bench_df=None)
    buys = signals[(signals['date'] == D2) & (signals['signal_type'] == 'BUY')]
    buys = buys.sort_values('total_score', ascending=False)
    ind, dfn = [], []
    for _, r in buys.iterrows():
        t, s = r['ticker'], float(r['total_score'])
        assert s >= cfg['min_total_score'], \
            f"_rebalance_v2 的行业二次过滤（min_total={cfg['min_total_score']}）应为无操作"
        if t in config_module.DEFENSE_UNIVERSE:
            assert s >= cfg['min_total_score'] - 10, "防御二次过滤应为无操作"
            dfn.append((t, s))
        else:
            ind.append((t, s))
    return _planner_targets(ind, dfn, prices, cash, cfg), buys


def _live_path_targets(scores_ts, prices, cash, cfg):
    """路径 C：实盘建议脚本 get_b0_4_signals（含数据层 prev_close/history_count 富化）。"""
    from live_generate_trade_plan import get_b0_4_signals
    with tempfile.TemporaryDirectory() as d:
        assistant = _live_assistant(d, cash=cash)
        prev = scores_ts[scores_ts['date'] == D1].set_index('ticker')['close']
        today = scores_ts[scores_ts['date'] == D2].set_index('ticker')['close']
        tickers = sorted(scores_ts['ticker'].unique())
        histories = {
            t: _market_history(t, prev_close=float(prev[t]),
                               today_close=float(today[t]), end=D2)
            for t in tickers
        }
        db = _MockDB(scores_ts, histories)
        target, _ = get_b0_4_signals(assistant, D2, cfg, db=db)
        return target


class TestThreePathConsistency:
    def test_good_market_three_paths_identical(self, runner):
        """好市场（动量中位数>0）：三路径目标持仓必须逐一相等。"""
        from config import build_config
        import config as config_module
        ind_pool = sorted(config_module.ETF_UNIVERSE.keys())
        t_a, t_b, t_c = ind_pool[0], ind_pool[1], ind_pool[2]
        specs = [
            dict(ticker=t_a, close_d1=1.2, close_d2=10.0, ma20=1.0, slope=0.01,
                 trend=20.0, confirm=10.0, total=60.0, momentum=0.10),
            dict(ticker=t_b, close_d1=1.2, close_d2=10.0, ma20=1.0, slope=0.01,
                 trend=20.0, confirm=10.0, total=50.0, momentum=0.10),
            # trend 5 < 15：三路径都必须排除
            dict(ticker=t_c, close_d1=1.2, close_d2=10.0, ma20=1.0, slope=0.01,
                 trend=5.0, confirm=10.0, total=50.0, momentum=0.10),
            # 防御：45 分达标（门槛 30）；25 分排除
            dict(ticker='518880.SH', close_d1=101.0, close_d2=100.0, ma20=100.0,
                 slope=0.005, trend=15.0, confirm=5.0, total=45.0, momentum=0.02),
            dict(ticker='511010.SH', close_d1=101.0, close_d2=100.0, ma20=100.0,
                 slope=0.005, trend=15.0, confirm=5.0, total=25.0, momentum=0.02),
        ]
        scores_ts = _signal_ts(specs)
        prices = {t_a: 10.0, t_b: 10.0, t_c: 10.0, '518880.SH': 100.0, '511010.SH': 100.0}
        cash = 1_000_000.0
        cfg = build_config()

        # 路径 A：真实生产信号路径
        targets_a, buys_a = _production_path_targets(scores_ts, prices, cash, cfg)
        assert set(buys_a['ticker']) == {t_a, t_b, '518880.SH'}, \
            f"生产信号应买入 A/B/黄金，实际: {sorted(buys_a['ticker'])}"

        # 路径 B：虚拟盘 runner（共享入场过滤模块）
        cross = _cross_section_for_runner(scores_ts)
        trades_b, _ = runner._run_planner(
            {'cash': cash, 'positions': {}}, cross, dict(prices), cfg=B04_ACCOUNT_CFG)
        targets_b = {}
        for t in trades_b:
            if t['action'] == 'BUY':
                targets_b[t['ticker']] = targets_b.get(t['ticker'], 0) + t['shares']

        # 路径 C：实盘建议脚本
        targets_c = _live_path_targets(scores_ts, prices, cash, cfg)

        assert targets_a, "路径 A 应产生目标持仓"
        assert targets_b == targets_a, \
            f"虚拟盘与生产信号路径目标不一致: runner={targets_b} vs production={targets_a}"
        assert targets_c == targets_a, \
            f"实盘与生产信号路径目标不一致: live={targets_c} vs production={targets_a}"

    def test_weak_market_55_gate_three_paths_identical(self):
        """弱市（核心池动量中位数<0）：行业门槛升至 55 分，三路径一致执行。"""
        from config import build_config
        from strategy import StrategyEngine
        from paper_trading.entry_filter import select_candidates
        import config as config_module
        ind_pool = sorted(config_module.ETF_UNIVERSE.keys())
        t_a, t_b = ind_pool[0], ind_pool[1]
        specs = [
            # total 50：好市场达标，但弱市 55 门槛下必须被排除
            dict(ticker=t_a, close_d1=1.2, close_d2=10.0, ma20=1.0, slope=0.01,
                 trend=20.0, confirm=10.0, total=50.0, momentum=-0.05),
            # total 60：弱市也达标
            dict(ticker=t_b, close_d1=1.2, close_d2=10.0, ma20=1.0, slope=0.01,
                 trend=20.0, confirm=10.0, total=60.0, momentum=-0.05),
            # 防御不受 55 门槛影响（生产规则：55 只加在核心池）
            dict(ticker='518880.SH', close_d1=101.0, close_d2=100.0, ma20=100.0,
                 slope=0.005, trend=15.0, confirm=5.0, total=45.0, momentum=-0.02),
        ]
        scores_ts = _signal_ts(specs)
        cfg = build_config()

        # 生产路径：弱市判定成立，A(50) 被 55 门槛排除
        engine = StrategyEngine(cfg)
        signals = engine.generate_signals(scores_ts.copy(), bench_df=None)
        buys = signals[(signals['date'] == D2) & (signals['signal_type'] == 'BUY')]
        assert set(buys['ticker']) == {t_b, '518880.SH'}, \
            f"弱市 55 门槛应排除 50 分的 {t_a}，实际买入信号: {sorted(buys['ticker'])}"

        # 虚拟盘/实盘共用的入场过滤：候选集合必须与生产路径一致
        cross = _cross_section_for_runner(scores_ts)
        ind, dfn, meta = select_candidates(
            cross,
            industry_tickers=set(config_module.ETF_UNIVERSE.keys()),
            defense_tickers=set(config_module.DEFENSE_UNIVERSE.keys()),
            min_total_score=40, min_trend_score=15, min_confirm_score=4,
        )
        assert meta['market_quality_poor'] is True
        assert meta['effective_min_total'] == 55.0
        prod_ind = sorted(r['ticker'] for _, r in buys.iterrows()
                          if r['ticker'] not in config_module.DEFENSE_UNIVERSE)
        prod_dfn = sorted(r['ticker'] for _, r in buys.iterrows()
                          if r['ticker'] in config_module.DEFENSE_UNIVERSE)
        assert sorted(t for t, _ in ind) == prod_ind
        assert sorted(t for t, _ in dfn) == prod_dfn

    def test_immature_and_below_ma20_excluded_like_production(self):
        """数据未成熟（history_count<51）与前收跌破 MA20：与生产路径一致排除。"""
        from config import build_config
        from strategy import StrategyEngine
        from paper_trading.entry_filter import select_candidates
        import config as config_module
        ind_pool = sorted(config_module.ETF_UNIVERSE.keys())
        t_a, t_b, t_c = ind_pool[0], ind_pool[1], ind_pool[2]
        specs = [
            dict(ticker=t_a, close_d1=1.2, close_d2=10.0, ma20=1.0, slope=0.01,
                 trend=20.0, confirm=10.0, total=60.0, momentum=0.10),
            # 未成熟：history_count=50 < 51
            dict(ticker=t_b, close_d1=1.2, close_d2=10.0, ma20=1.0, slope=0.01,
                 trend=20.0, confirm=10.0, total=60.0, momentum=0.10, hist=50),
            # 前收跌破 MA20：close_d1 0.9 < ma20 1.0
            dict(ticker=t_c, close_d1=0.9, close_d2=10.0, ma20=1.0, slope=0.01,
                 trend=20.0, confirm=10.0, total=60.0, momentum=0.10),
        ]
        scores_ts = _signal_ts(specs)
        cfg = build_config()

        engine = StrategyEngine(cfg)
        signals = engine.generate_signals(scores_ts.copy(), bench_df=None)
        buys = signals[(signals['date'] == D2) & (signals['signal_type'] == 'BUY')]
        assert set(buys['ticker']) == {t_a}, \
            f"只有 {t_a} 应产生买入信号，实际: {sorted(buys['ticker'])}"

        cross = _cross_section_for_runner(scores_ts)
        ind, dfn, _ = select_candidates(
            cross,
            industry_tickers=set(config_module.ETF_UNIVERSE.keys()),
            defense_tickers=set(config_module.DEFENSE_UNIVERSE.keys()),
        )
        assert sorted(t for t, _ in ind) == [t_a]
        assert dfn == []


# ============== 补3: 预设权重兼容性 + 评分来源记录 ==============

class TestPresetWeightCompatibility:
    def test_pipeline_effective_weights_sanity(self):
        """当前管线：动量/波动率因子关闭，有效权重 = 趋势30/确认20/成交量15 归一化。"""
        eff = effective_pipeline_weights()
        assert eff['momentum'] == 0.0 and eff['volatility'] == 0.0
        assert eff['trend'] == pytest.approx(30 / 65, abs=1e-9)
        assert eff['confirm'] == pytest.approx(20 / 65, abs=1e-9)
        assert eff['volume'] == pytest.approx(15 / 65, abs=1e-9)

    def test_compatible_weights_accepted(self, service):
        """v1.0 原始权重（关闭因子置零归一化后与管线一致）必须可创建。"""
        cfg = {**B04_ACCOUNT_CFG, 'weights': {
            'trend': 0.30, 'confirm': 0.20, 'momentum': 0.25,
            'volume': 0.15, 'volatility': 0.10,
        }}
        acct = _make_account(service, account_id='acct-w-ok', strategy_config=cfg)
        assert service.get_account(acct)['account_id'] == 'acct-w-ok'

    def test_incompatible_weights_rejected(self, service):
        """保守型权重（趋势0.40/确认0.30/成交量0.10）与管线不一致 → 拒绝创建。"""
        cfg = {**B04_ACCOUNT_CFG, 'weights': {
            'trend': 0.40, 'confirm': 0.30, 'momentum': 0.15,
            'volume': 0.10, 'volatility': 0.05,
        }}
        request = AccountCreate(
            account_id='acct-w-bad', name='Bad', account_type=AccountType.COMPARISON,
            strategy_name='X', strategy_config=cfg,
            initial_capital=1_000_000, start_mode=StartMode.CASH,
            start_date='2026-06-29',
        )
        with pytest.raises(ValueError) as excinfo:
            service.create_account(request)
        msg = str(excinfo.value)
        assert '不兼容' in msg and 'weights' in msg, f"实际错误: {msg}"
        assert service.store.get_account('acct-w-bad') is None, "被拒绝的账户不得落库"

    def test_score_source_recorded(self, service):
        """创建账户必须记录评分来源（统一评分管线，逐账户权重不生效）。"""
        acct = _make_account(service, account_id='acct-src')
        cfg = json.loads(service.get_account(acct)['config_json'])
        assert 'score_source' in cfg, "账户配置必须记录 score_source"
        assert 'daily_scores' in cfg['score_source']
        assert '逐账户权重不生效' in cfg['score_source']


# ============== 补4: 老账户执行口径确认门闩 ==============

class TestBehaviorAckGate:
    def test_legacy_account_paused_until_confirmed(self, service, runner):
        """老账户（无 config_version）：未确认不得按新行为运行——暂停且不保存状态。"""
        acct = _insert_legacy_account(service)
        scores = _scores([_qualified('512400.SH')])
        r = runner.run_daily(acct, '2026-07-02',
                             {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        assert r.get('paused') is True, f"老账户未确认应暂停，实际: {r}"
        assert '确认' in r['reason']
        assert r['trades'] == []
        assert not service.store.load_pending_signals(acct, '2026-07-03')
        assert not service.store.is_day_processed(acct, '2026-07-02'), \
            "暂停当日不得标记为已处理"

    def test_ui_service_reports_legacy_account_as_skipped(self, service, runner):
        """页面批量运行：未确认老账户归入 skipped 并说明原因，不算成功也不算异常。"""
        acct = _insert_legacy_account(service)
        ui = PaperTradingUIService(service, runner)
        scores = _scores([_qualified('512400.SH')])
        results = ui.run_accounts([acct], '2026-07-02',
                                  {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        assert acct in results['skipped'], f"实际结果: {results}"
        assert '确认' in results['skipped'][acct]

    def test_confirm_records_date_and_unfreezes_running(self, service, runner):
        """确认后：生效日期被记录、config_json 原封不动、账户按确认快照恢复运行。"""
        acct = _insert_legacy_account(service)
        before = service.get_account(acct)['config_json']
        updated = service.confirm_behavior_update(acct, confirm_date='2026-10-22')
        assert updated['behavior_ack_date'] == '2026-10-22', "确认生效日期必须被记录"
        assert updated['config_json'] == before, "确认不得改写不可变的创建配置"
        ack_cfg = json.loads(updated['behavior_ack_config_json'])
        assert ack_cfg['config_version'] == 2
        assert 'industry_tickers' in ack_cfg and 'defense_tickers' in ack_cfg

        scores = _scores([_qualified('512400.SH')])
        r = runner.run_daily(acct, '2026-07-02',
                             {'512400.SH': 1.0}, {'512400.SH': 1.0}, scores)
        assert not r.get('paused'), f"确认后应正常运行，实际: {r.get('reason')}"
        assert service.store.load_pending_signals(acct, '2026-07-03'), \
            "确认后周四应正常产生调仓信号"

    def test_confirm_keeps_existing_frozen_pool(self, service, monkeypatch):
        """上一修复轮创建的账户（已有冻结池、无 config_version）：确认不得覆盖其冻结池。"""
        import config as config_module
        frozen_pool = ['512400.SH', '512480.SH']
        acct = _insert_legacy_account(
            service, account_id='acct-old-frozen',
            config={**B04_ACCOUNT_CFG, 'industry_tickers': frozen_pool,
                    'defense_tickers': ['518880.SH']})
        # 确认时全局池已变化：不得影响该账户确认快照中的既有冻结池
        monkeypatch.setattr(config_module, 'ETF_UNIVERSE',
                            {'159999.SZ': '新ETF'})
        updated = service.confirm_behavior_update(acct, confirm_date='2026-10-22')
        ack_cfg = json.loads(updated['behavior_ack_config_json'])
        assert sorted(ack_cfg['industry_tickers']) == sorted(frozen_pool), \
            "已有冻结池必须保留，不得用当前全局池覆盖"

    def test_double_confirm_rejected(self, service):
        acct = _insert_legacy_account(service)
        service.confirm_behavior_update(acct, confirm_date='2026-10-22')
        with pytest.raises(ValueError):
            service.confirm_behavior_update(acct)

    def test_new_account_needs_no_confirm(self, service):
        acct = _make_account(service, account_id='acct-new')
        with pytest.raises(ValueError):
            service.confirm_behavior_update(acct)

    def test_store_migration_adds_ack_columns(self, service):
        """schema v4：paper_accounts 必须含 behavior_ack_date / behavior_ack_config_json。"""
        with service.store.connect() as conn:
            cols = {row['name'] for row in
                    conn.execute("PRAGMA table_info(paper_accounts)").fetchall()}
        assert 'behavior_ack_date' in cols
        assert 'behavior_ack_config_json' in cols


class TestScoringPriceEnrichmentParity:
    """真实价/评分价双轨下，prev_close 必须与正式评分路径同口径。"""

    @staticmethod
    def _dual_track_history(ticker, end='2026-10-21'):
        hist = _market_history(ticker, prev_close=0.5, today_close=0.5, end=end)
        # 前一交易日真实价 0.5、评分调整价 1.2；若误用真实价，会被 ma20=1.0 排除。
        hist['adj_close'] = [1.2] * (len(hist) - 1) + [0.5]
        return hist

    def test_live_prev_close_uses_scoring_adjusted_price(self):
        from config import build_config, ETF_UNIVERSE
        from live_generate_trade_plan import get_b0_4_signals
        ticker = sorted(ETF_UNIVERSE)[0]
        with tempfile.TemporaryDirectory() as d:
            assistant = _live_assistant(d)
            scores = _mock_scores([(ticker, 60.0)])
            scores['ma20'] = 1.0
            db = _MockDB(scores, {ticker: self._dual_track_history(ticker)})
            target, _ = get_b0_4_signals(
                assistant, '2026-10-21', build_config(), db=db)
            assert ticker in target, (
                '正式评分路径的调整后 prev_close=1.2 > ma20=1.0，应允许入场；'
                '不得误用真实收盘价 0.5 排除')

    def test_app_provider_prev_close_uses_scoring_adjusted_price(self, monkeypatch):
        import app as app_mod
        from config import ETF_UNIVERSE
        ticker = sorted(ETF_UNIVERSE)[0]
        latest = '2026-10-21'
        hist = self._dual_track_history(ticker, end=latest)
        latest_prices = hist.tail(1).copy()
        scores = _mock_scores([(ticker, 60.0)], date=latest)
        scores['ma20'] = 1.0

        class _DB:
            def get_latest_date(self):
                return latest

        def market_data(ticker=None, start_date=None, end_date=None):
            if start_date == latest and end_date == latest:
                return latest_prices.copy()
            return hist.copy()

        monkeypatch.setattr(app_mod, 'get_database', lambda: _DB())
        monkeypatch.setattr(app_mod, 'load_market_data', market_data)
        monkeypatch.setattr(app_mod, 'load_scores', lambda date=None: scores.copy())
        _, _, enriched, _, _ = app_mod._paper_trading_data_provider()
        actual = enriched.set_index('ticker').at[ticker, 'prev_close']
        assert actual == pytest.approx(1.2), (
            f'页面富化必须使用评分调整价 1.2，实际为 {actual}')

    def test_app_provider_marks_unenrichable_scores_invalid(self, monkeypatch):
        import app as app_mod
        from config import ETF_UNIVERSE
        ticker = sorted(ETF_UNIVERSE)[0]
        latest = '2026-10-21'
        latest_prices = self._dual_track_history(ticker, end=latest).tail(1)
        scores = _mock_scores([(ticker, 60.0)], date=latest)

        class _DB:
            def get_latest_date(self):
                return latest

        def market_data(ticker=None, start_date=None, end_date=None):
            if start_date == latest and end_date == latest:
                return latest_prices.copy()
            return pd.DataFrame()

        monkeypatch.setattr(app_mod, 'get_database', lambda: _DB())
        monkeypatch.setattr(app_mod, 'load_market_data', market_data)
        monkeypatch.setattr(app_mod, 'load_scores', lambda date=None: scores.copy())
        _, _, _, _, coverage = app_mod._paper_trading_data_provider()
        invalid = {x['ticker']: x['reason'] for x in coverage['invalid_scores']}
        assert ticker in invalid
        assert '无法富化' in invalid[ticker]
