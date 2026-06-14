# iFinD 批量下载脚本
# 用法：在有iFinD客户端的环境中运行此脚本
# 需要安装：pip install iFinDPy

import os
import pandas as pd
from datetime import datetime

# 尝试导入iFinD API
try:
    from iFinDPy import *
except ImportError:
    print("请先安装 iFinDPy: pip install iFinDPy")
    print("并确保iFinD客户端已登录")
    exit(1)

# 配置
OUTPUT_DIR = r'D:\etf_rotation_model\database'
os.makedirs(OUTPUT_DIR, exist_ok=True)

START_DATE = '2019-06-03'
END_DATE = datetime.now().strftime('%Y-%m-%d')

# 15只待下载ETF（588200和159869已用AKShare获取）
ETFS_TO_DOWNLOAD = {
    '516510.SH': '云计算ETF',
    '562500.SH': '机器人ETF',
    '159740.SZ': '碳中和ETF',
    '515050.SH': '5GETF',
    '512690.SH': '白酒ETF',
    '515170.SH': '食品饮料ETF',
    '159766.SZ': '旅游ETF',
    '159992.SZ': '创新药ETF',
    '159898.SZ': '医疗器械ETF',
    '515790.SH': '光伏ETF',
    '159566.SZ': '储能电池ETF',
    '515220.SH': '煤炭ETF',
    '513160.SH': '港股科技30ETF',
    '510880.SH': '红利ETF',
    '560700.SH': '央企改革ETF',
}

def download_etf_data(code, name):
    """使用iFinD下载单只ETF数据"""
    print(f"\n下载 {code} ({name})...")
    
    # iFinD代码格式
    if code.endswith('.SH'):
        if code.startswith('51'):
            ifind_code = code.replace('.SH', '.SH')  # 沪市ETF
        else:
            ifind_code = code.replace('.SH', '.SH')
    else:
        ifind_code = code.replace('.SZ', '.SZ')
    
    # iFinD字段
    indicators = "ths_open_price_etf;ths_high_price_etf;ths_low_price_etf;ths_close_price_etf;ths_trading_volume_etf"
    
    try:
        # 使用iFinD API获取数据
        # 注意：需要iFinD客户端已登录
        df = THS_HistoryQuotes(
            ifind_code,
            indicators,
            'Days:Tradeday,Fill:Previous,Interval:D',
            START_DATE,
            END_DATE
        )
        
        if df is not None and not df.empty:
            # 重命名列
            df = df.rename(columns={
                'OPEN': 'open',
                'HIGH': 'high',
                'LOW': 'low',
                'CLOSE': 'close',
                'VOLUME': 'volume',
            })
            df['ticker'] = code
            
            # 保存CSV
            output_file = os.path.join(OUTPUT_DIR, f"{code.replace('.', '_')}_history.csv")
            df.to_csv(output_file, index=False, encoding='utf-8-sig')
            
            print(f"  ✅ 成功: {len(df)} 条, {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
            return True
        else:
            print(f"  ❌ 无数据")
            return False
            
    except Exception as e:
        print(f"  ❌ 失败: {e}")
        return False

def main():
    print("=" * 80)
    print("iFinD 批量下载15只新增ETF")
    print("=" * 80)
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"日期范围: {START_DATE} ~ {END_DATE}")
    print("=" * 80)
    
    success_count = 0
    for code, name in ETFS_TO_DOWNLOAD.items():
        if download_etf_data(code, name):
            success_count += 1
    
    print(f"\n{'='*80}")
    print(f"下载完成: {success_count}/{len(ETFS_TO_DOWNLOAD)} 只成功")
    print(f"{'='*80}")
    
    if success_count < len(ETFS_TO_DOWNLOAD):
        print("\n失败的ETF需要手动下载:")
        for code, name in ETFS_TO_DOWNLOAD.items():
            output_file = os.path.join(OUTPUT_DIR, f"{code.replace('.', '_')}_history.csv")
            if not os.path.exists(output_file):
                print(f"  {code} {name}")

if __name__ == '__main__':
    main()
