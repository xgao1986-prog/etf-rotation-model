import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
import sys

def backup_database(db_path='database/etf_model.db', backup_dir='database/backups'):
    """
    备份数据库，按时间戳命名
    
    使用方式:
    python backup_database.py          # 手动备份
    python backup_database.py --auto   # 自动备份（用于脚本中调用）
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    
    if not db_path.exists():
        print(f"ERROR: 数据库不存在: {db_path}")
        return False
    
    # 创建备份目录
    backup_dir.mkdir(exist_ok=True, parents=True)
    
    # 生成备份文件名: etf_model_20260616_103000.db
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = backup_dir / f'etf_model_{timestamp}.db'
    
    # 复制数据库
    shutil.copy2(db_path, backup_path)
    
    # 记录备份信息
    info_path = backup_dir / f'etf_model_{timestamp}.info'
    db_size = db_path.stat().st_size
    
    # 获取数据库基本统计
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM market_data')
    row_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(DISTINCT ticker) FROM market_data')
    ticker_count = cursor.fetchone()[0]
    cursor.execute('SELECT MAX(date) FROM market_data')
    latest_date = cursor.fetchone()[0]
    conn.close()
    
    info_content = f"""Backup Info
=============
Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Original: {db_path}
Backup: {backup_path}
Size: {db_size:,} bytes

Database Stats:
- Rows: {row_count}
- Tickers: {ticker_count}
- Latest Date: {latest_date}

Backup by: {sys.argv[0] if len(sys.argv) > 1 else 'manual'}
"""
    info_path.write_text(info_content, encoding='utf-8')
    
    print(f"Database backed up to: {backup_path}")
    print(f"Backup info: {info_path}")
    print(f"Database stats: {row_count} rows, {ticker_count} tickers, latest: {latest_date}")
    
    return True

def list_backups(backup_dir='database/backups'):
    """列出所有备份"""
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        print(f"No backup directory: {backup_dir}")
        return
    
    backups = sorted(backup_dir.glob('etf_model_*.db'))
    print(f"Found {len(backups)} backups in {backup_dir}:")
    for bp in backups:
        size = bp.stat().st_size
        info_file = bp.with_suffix('.info')
        if info_file.exists():
            print(f"  {bp.name} ({size:,} bytes) - has info")
        else:
            print(f"  {bp.name} ({size:,} bytes) - no info")

def restore_database(backup_name, db_path='database/etf_model.db', backup_dir='database/backups'):
    """从备份恢复数据库"""
    backup_dir = Path(backup_dir)
    backup_path = backup_dir / backup_name
    
    if not backup_path.exists():
        print(f"ERROR: Backup not found: {backup_path}")
        return False
    
    # 先备份当前数据库
    print("Backing up current database before restore...")
    backup_database(db_path, backup_dir)
    
    # 恢复
    shutil.copy2(backup_path, db_path)
    print(f"Restored {backup_path} to {db_path}")
    return True

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--list':
        list_backups()
    elif len(sys.argv) > 2 and sys.argv[1] == '--restore':
        restore_database(sys.argv[2])
    else:
        backup_database()
