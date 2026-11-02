"""App admins and account erasure from the command line (run from backend/, e.g. inside the API container).

    python -m manage_users admin grant 98111 22334    give that login the app-admin rights (shared picture library)
    python -m manage_users admin revoke 98111 22334
    python -m manage_users admin list
    python -m manage_users erase-due                  erase the shops whose deletion grace period is over

Admin rights are given only here (and once by migration 0801, for the ADMIN_PHONES of the time), never through
the app or the API. A running API sees the change within USER_CACHE_SECONDS (auth.py). Each change goes to the
audit log as done by the system.
"""
import argparse
import asyncio
import sys

import audit
import auth
import db as db_module
import observability
from common import normalize_in_phone
from routers import account
from settings import check


async def set_admin(db, phone: str, on: bool) -> dict:
    """Give or take app-admin rights; returns the login. Raises LookupError for an unknown number."""
    number = normalize_in_phone(phone)
    user = await db.users.find_one({"phone": number}, {"_id": 0}) if number else None
    if not user:
        raise LookupError(f"no login with phone {phone}")
    if bool(user.get("is_admin")) != on:
        await db.users.update_one({"id": user["id"]}, {"$set": {"is_admin": on}})
        await audit.log(db, None, "admin.grant" if on else "admin.revoke", {"type": "user", "id": user["id"]},
                        {"via": "manage_users"}, business_id=user["business_id"])
    auth.forget_user(user["id"])
    return {**user, "is_admin": on}


async def admins(db) -> list[dict]:
    return await db.users.find({"is_admin": True}, {"_id": 0, "id": 1, "name": 1, "phone": 1, "role": 1}).to_list(100)


async def main(args) -> int:
    db_module.set_appname(f"{check().mongo_appname}-manage")
    db = db_module.get_db()
    try:
        if args.command == "erase-due":
            erased = await account.erase_due(db)
            print(f"erased {len(erased)} shop(s){': ' + ', '.join(erased) if erased else ''}")
        elif args.action == "list":
            for u in await admins(db):
                print(f"{u.get('phone') or '-':<16} {u['role']:<7} {u.get('name', '')}")
        else:
            if not args.phone:
                print("which phone number?", file=sys.stderr)
                return 2
            user = await set_admin(db, " ".join(args.phone), args.action == "grant")
            print(f"{user['phone']} ({user.get('name', '')}): admin {'on' if user['is_admin'] else 'off'}")
        return 0
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        await db_module.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m manage_users")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("admin", help="app-admin rights")
    admin.add_argument("action", choices=["grant", "revoke", "list"])
    admin.add_argument("phone", nargs="*")
    sub.add_parser("erase-due", help="erase shops whose deletion grace period is over")
    observability.setup_logging()
    sys.exit(asyncio.run(main(parser.parse_args())))
