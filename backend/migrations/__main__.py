"""python -m migrations up | status  (run from backend/; see migrations/__init__.py)."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
import migrations  # noqa: E402
import observability  # noqa: E402
from settings import check  # noqa: E402


async def main(args) -> int:
    db_module.set_appname(f"{check().mongo_appname}-migrate")
    db = db_module.get_db()
    try:
        if args.command == "status":
            for row in await migrations.status(db):
                when = row["applied_at"].isoformat(timespec="seconds") if row["applied_at"] else "pending"
                print(f"{row['id']:<40} {when:<26} {row['about']}")
            return 0
        ran = await migrations.up(db, wait=args.wait)
        print(f"applied {len(ran)} migration(s){': ' + ', '.join(ran) if ran else ' (all up to date)'}")
        return 0
    finally:
        await db_module.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m migrations")
    parser.add_argument("command", choices=["up", "status"])
    parser.add_argument("--wait", type=float, default=300, help="seconds to wait for another runner's lock")
    observability.setup_logging()
    sys.exit(asyncio.run(main(parser.parse_args())))
