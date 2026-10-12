import argparse

from app.database import SessionLocal
from app.services.memory_governance import backfill


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    if not parser.parse_args().apply:
        print('Read-only: vault metadata backfill requires --apply; no source files are changed')
    else:
        total = 0
        with SessionLocal() as db:
            while True:
                added = backfill(db, 500)
                total += added
                if added < 500:
                    break
        print('Historical memory metadata created:', total)
