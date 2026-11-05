"""Offline restore to a new SQLite file, never an existing database."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from settlement.store import restore_to_new_sqlite

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup',type=Path)
    parser.add_argument('new_database',type=Path)
    args=parser.parse_args()
    store=restore_to_new_sqlite(args.backup.read_bytes(),args.new_database)
    print(f'복원 완료: {len(store.rows())}건. 원본 DB는 변경하지 않았습니다.')

if __name__=='__main__': main()
