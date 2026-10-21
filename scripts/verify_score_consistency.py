# -*- coding: utf-8 -*-
"""main.py 信号路径 vs 页面 _recalculate_scores 评分一致性验证（ROUND3 意见一验证）。

方法：在正式库临时副本上，分别用两条代码路径计算全量评分，
逐 ticker+date 比对全部评分列。配置均为 STRATEGY_CONFIG 默认值（同源）。
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, 'src')
sys.path.insert(0, os.getcwd())

import database as db_mod
from config import DB_PATH, STRATEGY_CONFIG

SCORE_COLS = ['ma20', 'ma50', 'ma20_slope', 'above_ma20_days',
              'volatility_20', 'momentum_20', 'volume_ratio',
              'trend_score', 'confirm_score', 'momentum_rank',
              'volume_score', 'vol_score', 'total_score']


def run_main_path(tmp_db):
    """执行 main.py cmd_signal 的评分段（含保存捕获）。"""
    import main as main_mod

    db_mod.DB_PATH = tmp_db  # ETFDatabase() 默认路径指向临时副本
    captured = {}
    orig_save = db_mod.ETFDatabase.save_scores

    def capture_save(self, df):
        captured['df'] = df.copy()
        return orig_save(self, df)

    db_mod.ETFDatabase.save_scores = capture_save
    orig_update = main_mod.update_latest_data
    main_mod.update_latest_data = lambda db=None: 0  # 禁止触网更新
    try:
        class A:  # argparse 命名空间占位（cmd_signal 仅用 args.save）
            save = False
        main_mod.cmd_signal(A())
    finally:
        db_mod.ETFDatabase.save_scores = orig_save
        main_mod.update_latest_data = orig_update
        db_mod.DB_PATH = DB_PATH
    return captured['df']


def run_page_path(tmp_db):
    """执行 app.py _recalculate_scores（cfg 用 STRATEGY_CONFIG 默认值）。"""
    import app
    db = db_mod.ETFDatabase(tmp_db)
    cfg = STRATEGY_CONFIG.copy()
    df = app._recalculate_scores(db, cfg, st_info=None, st_success=None, st_error=None)
    return df


def compare(a, b):
    ka = a[['ticker', 'date']].astype(str)
    kb = b[['ticker', 'date']].astype(str)
    key = ['ticker', 'date']
    aa = a.copy(); aa[key] = aa[key].astype(str)
    bb = b.copy(); bb[key] = bb[key].astype(str)
    merged = aa.merge(bb, on=key, how='outer', suffixes=('_main', '_page'), indicator=True)
    issues = []
    only_main = int((merged['_merge'] == 'left_only').sum())
    only_page = int((merged['_merge'] == 'right_only').sum())
    if only_main or only_page:
        issues.append(f'行集不一致: 仅main={only_main} 仅page={only_page}')
    both = merged[merged['_merge'] == 'both']
    for col in SCORE_COLS:
        ca, cb = f'{col}_main', f'{col}_page'
        if ca not in both.columns or cb not in both.columns:
            issues.append(f'缺列: {col}')
            continue
        x = pd.to_numeric(both[ca], errors='coerce')
        y = pd.to_numeric(both[cb], errors='coerce')
        diff = (x - y).abs()
        bad = diff[(diff > 1e-9) & ~(x.isna() & y.isna()) & ~(x.isna() | y.isna())]
        if len(bad):
            issues.append(f'{col}: {len(bad)} 行不一致, max diff={bad.max():.6g}')
        nan_mismatch = int((x.isna() != y.isna()).sum())
        if nan_mismatch:
            issues.append(f'{col}: {nan_mismatch} 行 NaN 模式不一致')
    return issues, len(both)


if __name__ == '__main__':
    import pandas as pd  # noqa: 延迟导入，compare 内已用

    tmpdir = tempfile.mkdtemp(prefix='score_consistency_')
    try:
        results = []
        for name, fn in (('main', run_main_path), ('page', run_page_path)):
            tmp_db = os.path.join(tmpdir, f'{name}.db')
            shutil.copy(DB_PATH, tmp_db)
            df = fn(tmp_db)
            print(f'[{name}] 评分行数: {len(df)}')
            results.append(df)
        issues, n_both = compare(results[0], results[1])
        print(f'比对行数（交集）: {n_both}')
        if issues:
            print('❌ 不一致:')
            for i in issues:
                print('  -', i)
            sys.exit(1)
        print('✅ 两路径评分完全一致（逐 ticker+date 逐列）')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
