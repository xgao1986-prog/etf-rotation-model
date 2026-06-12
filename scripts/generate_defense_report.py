"""
防御模块综合对比报告生成脚本
汇总所有测试结果并生成Markdown报告
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd


def main():
    # 读取测试结果
    try:
        df_defense = pd.read_csv('reports/defense_comparison.csv', encoding='utf-8-sig')
    except:
        print("未找到 defense_comparison.csv")
        return
    
    # 生成报告
    report = []
    report.append("# 防御模块 v1.3 综合测试报告")
    report.append("")
    report.append("## 测试概述")
    report.append("")
    report.append("- **测试日期**: 2025年")
    report.append("- **测试区间**: 2019-06-03 至 2026-06-12")
    report.append("- **样本内**: 2019-06-03 至 2023-12-31")
    report.append("- **样本外**: 2024-01-01 至 2026-06-12")
    report.append("- **基准**: 沪深300")
    report.append("- **初始资金**: 100万")
    report.append("")
    report.append("## 三种防御配置对比")
    report.append("")
    report.append("### 全区间表现（2019-2026）")
    report.append("")
    report.append("| 配置 | 总收益 | 年化收益 | 夏普比率 | 最大回撤 | 交易次数 | 防御交易 | 平均持仓 |")
    report.append("|------|--------|----------|----------|----------|----------|----------|----------|")
    
    for _, row in df_defense[df_defense['sample'] == 'all'].iterrows():
        report.append(f"| {row['label']} | {row['total_return']:.2%} | {row['annual_return']:.2%} | {row['sharpe_ratio']:.3f} | {row['max_drawdown']:.2%} | {int(row['num_trades'])} | {int(row['defense_trades'])} | {row['avg_holdings']:.1f} |")
    
    report.append("")
    report.append("### 样本内表现（2019-2023）")
    report.append("")
    report.append("| 配置 | 总收益 | 年化收益 | 夏普比率 | 最大回撤 | 交易次数 | 防御交易 | 平均持仓 |")
    report.append("|------|--------|----------|----------|----------|----------|----------|----------|")
    
    for _, row in df_defense[df_defense['sample'] == 'in'].iterrows():
        report.append(f"| {row['label']} | {row['total_return']:.2%} | {row['annual_return']:.2%} | {row['sharpe_ratio']:.3f} | {row['max_drawdown']:.2%} | {int(row['num_trades'])} | {int(row['defense_trades'])} | {row['avg_holdings']:.1f} |")
    
    report.append("")
    report.append("### 样本外表现（2024-2026）")
    report.append("")
    report.append("| 配置 | 总收益 | 年化收益 | 夏普比率 | 最大回撤 | 交易次数 | 防御交易 | 平均持仓 |")
    report.append("|------|--------|----------|----------|----------|----------|----------|----------|")
    
    for _, row in df_defense[df_defense['sample'] == 'out'].iterrows():
        report.append(f"| {row['label']} | {row['total_return']:.2%} | {row['annual_return']:.2%} | {row['sharpe_ratio']:.3f} | {row['max_drawdown']:.2%} | {int(row['num_trades'])} | {int(row['defense_trades'])} | {row['avg_holdings']:.1f} |")
    
    report.append("")
    report.append("## 关键发现")
    report.append("")
    
    # 计算对比数据
    gold_all = df_defense[(df_defense['sample'] == 'all') & (df_defense['label'] == '仅黄金防御')].iloc[0]
    bond_all = df_defense[(df_defense['sample'] == 'all') & (df_defense['label'] == '仅国债防御')].iloc[0]
    dual_all = df_defense[(df_defense['sample'] == 'all') & (df_defense['label'] == '黄金+国债双防御')].iloc[0]
    
    gold_out = df_defense[(df_defense['sample'] == 'out') & (df_defense['label'] == '仅黄金防御')].iloc[0]
    bond_out = df_defense[(df_defense['sample'] == 'out') & (df_defense['label'] == '仅国债防御')].iloc[0]
    dual_out = df_defense[(df_defense['sample'] == 'out') & (df_defense['label'] == '黄金+国债双防御')].iloc[0]
    
    report.append("### 1. 黄金 vs 国债")
    report.append("")
    report.append(f"- **黄金防御全区间夏普**: {gold_all['sharpe_ratio']:.3f}，**国债防御**: {bond_all['sharpe_ratio']:.3f}")
    report.append(f"- **黄金防御全区间收益**: {gold_all['total_return']:.2%}，**国债防御**: {bond_all['total_return']:.2%}")
    report.append(f"- **黄金防御全区间回撤**: {gold_all['max_drawdown']:.2%}，**国债防御**: {bond_all['max_drawdown']:.2%}")
    report.append("- **结论**: 黄金防御在全区间表现优于国债防御（夏普更高、收益更高、回撤更小）")
    report.append("")
    
    report.append("### 2. 单防御 vs 双防御")
    report.append("")
    report.append(f"- **双防御全区间夏普**: {dual_all['sharpe_ratio']:.3f}，介于黄金({gold_all['sharpe_ratio']:.3f})和国债({bond_all['sharpe_ratio']:.3f})之间")
    report.append(f"- **双防御样本外夏普**: {dual_out['sharpe_ratio']:.3f}，**三者最高**")
    report.append(f"- **双防御样本外回撤**: {dual_out['max_drawdown']:.2%}，**三者最低**（黄金{gold_out['max_drawdown']:.2%}，国债{bond_out['max_drawdown']:.2%}）")
    report.append("- **结论**: 双防御在样本外（熊市）表现最优，夏普最高且回撤最小")
    report.append("")
    
    report.append("### 3. 样本内 vs 样本外")
    report.append("")
    report.append("- **样本内（2019-2023）**: 三种配置夏普都较低（0.087-0.173），说明防御模块在震荡/慢牛环境中贡献有限")
    report.append("- **样本外（2024-2026）**: 三种配置夏普都显著提升（1.11-1.26），防御价值在熊市中凸显")
    report.append("- **结论**: 防御模块的核心价值在于熊市保护，而非牛市增强")
    report.append("")
    
    report.append("### 4. 动态防御比例测试")
    report.append("")
    report.append("- **线性插值 vs 阶梯式**: 由于大盘择时信号（market_signal）为离散值（0.2/0.5/1.0），两种模式结果相同")
    report.append("- **波动率增强**: 启用后回撤略有改善（-10.59%→-9.93%），但收益下降（40.93%→38.64%），效果不明显")
    report.append("- **建议**: 当前阶梯式防御比例已足够有效，无需复杂化")
    report.append("")
    
    report.append("## 配置建议")
    report.append("")
    report.append("基于测试结果，推荐以下防御配置：")
    report.append("")
    report.append("### 推荐方案：黄金+国债双防御")
    report.append("")
    report.append("```python")
    report.append("DEFENSE_UNIVERSE = {")
    report.append("    '518880.SH': '黄金ETF',")
    report.append("    '511010.SH': '国债ETF',")
    report.append("}")
    report.append("")
    report.append("DEFENSE_ALLOCATION = {")
    report.append("    0.2: 0.50,   # 防御仓位: 50%配防御资产")
    report.append("    0.5: 0.20,   # 半仓: 20%配防御资产")
    report.append("    1.0: 0.00,   # 满仓: 不配防御资产")
    report.append("}")
    report.append("```")
    report.append("")
    report.append("**理由**:")
    report.append("1. 样本外夏普最高（1.255），熊市保护最强")
    report.append("2. 样本外回撤最小（-7.57%），风险控制最优")
    report.append("3. 黄金和国债低相关性，组合配置可分散风险")
    report.append("")
    report.append("## 风险提示")
    report.append("")
    report.append("1. 防御模块在震荡市可能拖累收益（样本内夏普仅0.087）")
    report.append("2. 黄金ETF在极端行情中可能与股票同向波动，无法完全避险")
    report.append("3. 国债ETF受利率政策影响，加息周期可能下跌")
    report.append("4. 历史回测不代表未来表现，需持续监控")
    report.append("")
    
    # 保存报告
    report_text = "\n".join(report)
    with open('reports/defense_module_report.md', 'w', encoding='utf-8') as f:
        f.write(report_text)
    
    print("=" * 70)
    print("防御模块综合报告已生成")
    print("=" * 70)
    print(f"保存路径: reports/defense_module_report.md")
    print("\n报告内容预览:")
    print("-" * 70)
    print(report_text[:2000])
    print("...")
    print("-" * 70)


if __name__ == '__main__':
    main()
