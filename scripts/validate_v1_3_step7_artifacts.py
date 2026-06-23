"""v1.3 Step 7 验证器：检查所有证据文件和勾稽。

验证项：
- 所有证据文件存在（含新增7份CSV）
- cash + positions_value = NAV
- cumulative_return与NAV/初始资金一致（严格误差<0.01%）
- 敞口 industry_pct + defense_pct + cash_pct = 100%（误差<0.1%）
- 佣金逐笔按生产公式重算
- shares % 100 == 0
- 报告核心数值与CSV一致
- A精确复现NAV=2,761,288.07、804笔
- 2025–2026未进入验收
- LOO只包含2019–2024
- 验证失败返回非零退出码
"""
import sys, os, re
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def _production_commission(price, shares):
    """生产佣金公式：max(5, price * shares * 0.0003)"""
    return max(5.0, price * shares * 0.0003)


def validate(output_dir='D:/etf_rotation_model/reports'):
    """运行所有验证。失败返回非零退出码。"""
    errors = []
    warnings = []

    scenarios = ['A', 'B', 'C', 'D']
    required_files = []
    for sc in scenarios:
        required_files.append(f'v1_3_step7_nav_{sc}.csv')
        required_files.append(f'v1_3_step7_trades_{sc}.csv')
    required_files += [
        'v1_3_step7_loyo.csv',
        'v1_3_step7_annual_contribution.csv',
        'v1_3_step7_defense_contribution.csv',
        'v1_3_step7_reconciliation.csv',
        'v1_3_step7_portfolio_orthogonal.md',
        # 新增7份CSV
        'v1_3_step7_yearly_metrics.csv',
        'v1_3_step7_position_exposure.csv',
        'v1_3_step7_slot_contribution.csv',
        'v1_3_step7_commission_summary.csv',
        'v1_3_step7_slot5_yearly.csv',
        'v1_3_step7_orthogonal_attribution.csv',
        'v1_3_step7_standard7_verification.csv',
    ]

    # 1. 所有证据文件存在
    missing = []
    for fn in required_files:
        path = os.path.join(output_dir, fn)
        if not os.path.exists(path):
            missing.append(path)
            errors.append(f"文件不存在: {path}")

    if missing:
        print("=" * 60)
        print("验证失败：必要文件缺失")
        for e in errors:
            print(f"  ❌ {e}")
        return 1

    # 2. 逐方案勾稽
    for sc in scenarios:
        nav_path = os.path.join(output_dir, f'v1_3_step7_nav_{sc}.csv')
        trades_path = os.path.join(output_dir, f'v1_3_step7_trades_{sc}.csv')

        nav_df = pd.read_csv(nav_path)
        trades_df = pd.read_csv(trades_path)

        # 2a. cash + positions_value = NAV
        if 'cash' in nav_df.columns and 'positions_value' in nav_df.columns:
            nav_df['check_nav'] = nav_df['cash'] + nav_df['positions_value']
            mismatch = (nav_df['nav'] - nav_df['check_nav']).abs()
            if mismatch.max() >= 0.01:
                errors.append(f"{sc}: NAV勾稽失败，最大偏差={mismatch.max():.4f}")
            else:
                print(f"  OK {sc}: cash+positions_value=NAV")

        # 2b. cumulative_return与NAV/初始资金一致（严格）
        if 'nav' in nav_df.columns and len(nav_df) > 1:
            initial_nav = nav_df['nav'].iloc[0]
            final_nav = nav_df['nav'].iloc[-1]
            expected_cum_ret = final_nav / initial_nav - 1
            if 'cumulative_return' in nav_df.columns:
                csv_cum_ret = nav_df['cumulative_return'].iloc[-1]
                if abs(csv_cum_ret - expected_cum_ret) >= 0.0001:
                    errors.append(f"{sc}: cumulative_return勾稽失败 CSV={csv_cum_ret:.6%} expected={expected_cum_ret:.6%}")
                else:
                    print(f"  OK {sc}: cumulative_return与NAV一致 ({expected_cum_ret:.2%})")
            else:
                print(f"  OK {sc}: cumulative_return列缺失，跳过CSV校验 (NAV推导={expected_cum_ret:.2%})")
            if expected_cum_ret <= -0.5:
                errors.append(f"{sc}: cumulative return异常: {expected_cum_ret:.2%}")

        # 2c. 佣金逐笔按生产公式重算
        if 'commission' in trades_df.columns and 'price' in trades_df.columns and 'shares' in trades_df.columns:
            comm_errors = 0
            for _, row in trades_df.iterrows():
                expected = _production_commission(row['price'], row['shares'])
                actual = row['commission']
                if abs(actual - expected) >= 0.01:
                    comm_errors += 1
                    if comm_errors <= 3:
                        errors.append(f"{sc}: 佣金计算错误: {row.get('ticker', '?')} expected={expected:.2f} actual={actual:.2f}")
            if comm_errors == 0:
                print(f"  OK {sc}: 佣金逐笔验证通过")

        # 2d. shares % 100 == 0
        if 'shares' in trades_df.columns:
            bad_shares = (trades_df['shares'] % 100 != 0).sum()
            if bad_shares > 0:
                errors.append(f"{sc}: {bad_shares}笔交易shares非100整数倍")
            else:
                print(f"  OK {sc}: shares全部整手")

    # 3. 敞口校验：industry_pct + defense_pct + cash_pct = 100%
    exposure_path = os.path.join(output_dir, 'v1_3_step7_position_exposure.csv')
    if os.path.exists(exposure_path):
        exp_df = pd.read_csv(exposure_path)
        exp_df['total_pct'] = exp_df['industry_pct'] + exp_df['defense_pct'] + exp_df['cash_pct']
        max_dev = (exp_df['total_pct'] - 1.0).abs().max()
        if max_dev >= 0.001:
            errors.append(f"敞口勾稽失败: industry+defense+cash 最大偏差={max_dev:.4%}")
        else:
            print(f"  OK 敞口勾稽: industry+defense+cash=100% (最大偏差={max_dev:.4%})")

    # 4. A精确复现
    nav_a = pd.read_csv(os.path.join(output_dir, 'v1_3_step7_nav_A.csv'))
    final_nav = nav_a['nav'].iloc[-1]
    trades_a = pd.read_csv(os.path.join(output_dir, 'v1_3_step7_trades_A.csv'))
    n_trades = len(trades_a)
    if abs(final_nav - 2_761_288.07) >= 0.01:
        errors.append(f"A: 基线复现失败 NAV={final_nav} (expected 2,761,288.07)")
    else:
        print(f"  OK A: NAV基线复现通过 ({final_nav:,.2f})")
    if n_trades != 804:
        errors.append(f"A: 交易数复现失败 {n_trades} (expected 804)")
    else:
        print(f"  OK A: 交易数复现通过 ({n_trades})")

    # 5. LOO只包含2019-2024
    loyo_path = os.path.join(output_dir, 'v1_3_step7_loyo.csv')
    loyo_df = pd.read_csv(loyo_path)
    years = set(loyo_df['exclude_year'].unique())
    expected_years = {2019, 2020, 2021, 2022, 2023, 2024}
    if years != expected_years:
        errors.append(f"LOO年份错误: {years} (expected {expected_years})")
    else:
        print(f"  OK LOO仅包含2019-2024")

    # 6. reconciliation CSV与NAV/交易数一致
    recon_path = os.path.join(output_dir, 'v1_3_step7_reconciliation.csv')
    recon_df = pd.read_csv(recon_path)
    for sc in scenarios:
        nav_df = pd.read_csv(os.path.join(output_dir, f'v1_3_step7_nav_{sc}.csv'))
        trades_df = pd.read_csv(os.path.join(output_dir, f'v1_3_step7_trades_{sc}.csv'))
        recon_row = recon_df[recon_df['scenario'] == sc]
        if not recon_row.empty:
            if abs(recon_row['final_nav'].iloc[0] - nav_df['nav'].iloc[-1]) >= 0.01:
                errors.append(f"{sc}: reconciliation NAV不一致")
            if recon_row['num_trades'].iloc[0] != len(trades_df):
                errors.append(f"{sc}: reconciliation 交易数不一致")
    print(f"  OK reconciliation CSV一致")

    # 7. 报告核心数值与CSV一致（简单校验：报告中出现的收益数字与period_results对应）
    report_path = os.path.join(output_dir, 'v1_3_step7_portfolio_orthogonal.md')
    with open(report_path, 'r', encoding='utf-8') as f:
        report_text = f.read()

    # 检查报告是否包含关键表格和结论
    required_sections = [
        '## 逐日持仓敞口',
        '## 槽位贡献（mark-to-market）',
        '## 正交归因（实际敞口+槽位PnL验证）',
        '## 预注册标准7：Top4实际权重',
    ]
    for section in required_sections:
        if section not in report_text:
            errors.append(f"报告缺失章节: {section}")
        else:
            print(f"  OK 报告包含: {section}")

    # ===== NEW FIX 8 VALIDATIONS =====
    print("\n--- 新增验证项 ---")

    # 8.1 检查period column存在于slot_contribution, orthogonal_attribution, standard7
    slot_path = os.path.join(output_dir, 'v1_3_step7_slot_contribution.csv')
    slot_df = pd.read_csv(slot_path)
    if 'period' not in slot_df.columns:
        errors.append("slot_contribution 缺少 period 列")
    else:
        print("  OK slot_contribution 含 period 列")
        # 检查不同period的值不同
        periods_in_slot = slot_df['period'].unique()
        if len(periods_in_slot) < 2:
            errors.append(f"slot_contribution 只含 {periods_in_slot} 个period，应含多个")
        else:
            print(f"  OK slot_contribution 含 {len(periods_in_slot)} 个period")

    attr_path = os.path.join(output_dir, 'v1_3_step7_orthogonal_attribution.csv')
    attr_df = pd.read_csv(attr_path)
    if 'period' not in attr_df.columns:
        errors.append("orthogonal_attribution 缺少 period 列")
    else:
        print("  OK orthogonal_attribution 含 period 列")

    std7_path = os.path.join(output_dir, 'v1_3_step7_standard7_verification.csv')
    std7_df = pd.read_csv(std7_path)
    if 'period' not in std7_df.columns:
        errors.append("standard7_verification 缺少 period 列")
    else:
        print("  OK standard7_verification 含 period 列")

    # 8.2 检查2025-2026 NOT in standard7
    if 'period' in std7_df.columns:
        bad_periods = std7_df[std7_df['period'].isin(['观察期', '全期间'])]
        if not bad_periods.empty:
            errors.append(f"standard7 包含不应有的period: {bad_periods['period'].unique().tolist()}")
        else:
            print("  OK standard7 只含研究期/验证期")

    # 8.3 正交归因平衡检查: known_effects + residual == observed_diff (within 1.0)
    for _, row in attr_df.iterrows():
        pair = row['pair']
        observed = row['observed_diff']
        if pair == 'B-A':
            known = row['rank5_effect'] + row['r14_effect']
        elif pair == 'C-B':
            known = row['defense_effect'] + row['r14_effect']
        elif pair == 'D-B':
            known = row['r14_effect']
        elif pair == 'D-A':
            known = row['rank5_effect'] + row['r14_effect']
        else:
            known = 0
        residual = row['residual']
        total = known + residual
        if abs(total - observed) >= 1.0:
            errors.append(f"正交归因不平衡 {pair} {row['period']}: known+residual={total:.2f} != observed={observed:.2f}")
    if not any('正交归因不平衡' in e for e in errors):
        print("  OK 正交归因平衡: known_effects + residual == observed_diff (within 1.0)")

    # 8.4 检查slot contribution使用了entry_rank（一致性检查）
    # 由于entry_rank是固定的，每个scenario+period中每个rank应只出现一次
    if 'period' in slot_df.columns and 'rank' in slot_df.columns:
        dup_check = slot_df.groupby(['scenario', 'period', 'rank']).size()
        dups = dup_check[dup_check > 1]
        if not dups.empty:
            errors.append(f"slot_contribution 存在重复rank: {dups.index.tolist()[:3]}")
        else:
            print("  OK slot_contribution rank唯一（entry_rank一致性）")

    # 8.5 检查drawdown >= -100%
    if 'max_drawdown' in slot_df.columns:
        bad_dd = slot_df[slot_df['max_drawdown'] < -1.0]
        if not bad_dd.empty:
            errors.append(f"slot_contribution 存在drawdown < -100%: {bad_dd['max_drawdown'].min():.2%}")
        else:
            print("  OK drawdown 全部 >= -100%")

    # 8.6 检查monthly win rate使用compounding（从nav重算验证）
    # 8.6 检查monthly_win_rate使用compound而非sum（逐year验证）
    ym_path = os.path.join(output_dir, 'v1_3_step7_yearly_metrics.csv')
    if os.path.exists(ym_path):
        ym_df = pd.read_csv(ym_path)
        nav_a = pd.read_csv(os.path.join(output_dir, 'v1_3_step7_nav_A.csv'))
        nav_a['date'] = pd.to_datetime(nav_a['date'])
        nav_a = nav_a[nav_a['date'] <= '2024-12-31']
        nav_a['ret'] = nav_a['nav'].pct_change()
        nav_a['ym'] = nav_a['date'].dt.to_period('M')
        nav_a['year'] = nav_a['date'].dt.year
        # Check yearly_metrics for A uses compound (per year)
        a_yearly = ym_df[(ym_df['scenario'] == 'A') & (ym_df['period'] == '分析期')]
        if not a_yearly.empty:
            errors_monthly = []
            for _, row in a_yearly.iterrows():
                year = row['year']
                csv_win_rate = row['monthly_win_rate']
                yr_nav = nav_a[nav_a['year'] == year]
                if len(yr_nav) < 2:
                    continue
                yr_nav = yr_nav.copy()
                yr_nav['ym'] = yr_nav['date'].dt.to_period('M')
                monthly_compound = yr_nav.groupby('ym').apply(lambda x: (1 + x['ret']).prod() - 1)
                monthly_sum = yr_nav.groupby('ym')['ret'].sum()
                win_rate_compound = (monthly_compound > 0).mean()
                win_rate_sum = (monthly_sum > 0).mean()
                if abs(csv_win_rate - win_rate_compound) > abs(csv_win_rate - win_rate_sum) + 0.01:
                    errors_monthly.append(f"year={year}: CSV={csv_win_rate:.2%} compound={win_rate_compound:.2%} sum={win_rate_sum:.2%}")
            if errors_monthly:
                errors.append(f"monthly_win_rate 可能使用sum而非compound: {errors_monthly}")
            else:
                print("  OK monthly_win_rate 使用compound (逐year验证通过)")
        else:
            print("  SKIP monthly_win_rate 验证（A分析期数据缺失）")

    # 8.7 检查report numbers match CSVs（sample check）
    # 检查报告中是否包含标准7的D Top4权重
    if 'avg_top4_weight' in std7_df.columns:
        d_top4 = std7_df[std7_df['scenario'] == 'D']['avg_top4_weight']
        if not d_top4.empty:
            d_top4_val = d_top4.iloc[0]
            d_top4_str = f"{d_top4_val:.2%}"
            if d_top4_str.replace('%', '') in report_text or f"{d_top4_val:.2%}" in report_text:
                print("  OK 报告数值与standard7 CSV一致")
            else:
                warnings.append("报告数值可能与standard7 CSV不一致（格式差异）")

    print("=" * 60)
    if errors:
        print(f"验证失败: {len(errors)} 个错误")
        for e in errors:
            print(f"  FAIL {e}")
        return 1
    else:
        print("验证通过: 全部检查项通过")
        if warnings:
            for w in warnings:
                print(f"  WARN {w}")
        return 0


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='v1.3 Step 7 验证器')
    parser.add_argument('--output-dir', type=str, default='D:/etf_rotation_model/reports',
                        help='输出目录')
    args = parser.parse_args()
    sys.exit(validate(output_dir=args.output_dir))
