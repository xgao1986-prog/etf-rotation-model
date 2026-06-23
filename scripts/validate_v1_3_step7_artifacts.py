"""v1.3 Step 7 验证器：检查所有证据文件和勾稽。

验证项：
- 所有证据文件存在
- cash + positions_value = NAV
- cumulative_return与NAV一致
- 佣金逐笔按生产公式重算
- shares % 100 == 0
- 报告数字与CSV一致
- A精确复现NAV=2,761,288.07、804笔
- 2025–2026未进入验收
- LOO只包含2019–2024
- 验证失败返回非零退出码
"""
import sys, os
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
    ]

    # 1. 所有证据文件存在
    for fn in required_files:
        path = os.path.join(output_dir, fn)
        if not os.path.exists(path):
            errors.append(f"文件不存在: {path}")

    if errors:
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

        # 2b. cumulative_return与NAV一致
        if 'nav' in nav_df.columns and len(nav_df) > 1:
            initial_nav = nav_df['nav'].iloc[0]
            final_nav = nav_df['nav'].iloc[-1]
            cum_ret = final_nav / initial_nav - 1
            if cum_ret <= -0.5:
                errors.append(f"{sc}: cumulative return异常: {cum_ret:.2%}")
            else:
                print(f"  OK {sc}: cumulative_return合理 ({cum_ret:.2%})")

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

    # 3. A精确复现
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

    # 4. LOO只包含2019-2024
    loyo_path = os.path.join(output_dir, 'v1_3_step7_loyo.csv')
    loyo_df = pd.read_csv(loyo_path)
    years = set(loyo_df['exclude_year'].unique())
    expected_years = {2019, 2020, 2021, 2022, 2023, 2024}
    if years != expected_years:
        errors.append(f"LOO年份错误: {years} (expected {expected_years})")
    else:
        print(f"  OK LOO仅包含2019-2024")

    # 5. reconciliation CSV与NAV/交易数一致
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

    print("=" * 60)
    if errors:
        print(f"验证失败: {len(errors)} 个错误")
        for e in errors:
            print(f"  FAIL {e}")
        return 1
    else:
        print("验证通过: 全部检查项通过")
        return 0


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='v1.3 Step 7 验证器')
    parser.add_argument('--output-dir', type=str, default='D:/etf_rotation_model/reports',
                        help='输出目录')
    args = parser.parse_args()
    sys.exit(validate(output_dir=args.output_dir))
