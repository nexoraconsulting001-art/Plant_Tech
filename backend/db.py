"""MongoDB handle shared by all routes (PyMongo's asyncio client). Tests point it at their own database.

One client per process, created on first use: gunicorn starts each worker before any connection is made. The
client is tied to the event loop that first uses it (a PyMongo rule), so the app closes it on shutdown (`close`).

Connection settings are explicit (settings.py): pool size per process, a 5 s server-selection limit, a 5 s
limit on every operation including its wait for a pooled connection (`timeoutMS`; PyMongo then ignores
waitQueueTimeoutMS, so none is set; `pymongo.timeout()` lifts it for slow jobs such as migrations), retried
writes, majority write concern and an app name that shows in Atlas. Too many requests for the pool are refused
before they get here (overload.py), and a timeout becomes 503 db_busy, not a 500.

`in_transaction(callback)` runs `await callback(session)` as one transaction, retried by PyMongo on
TransientTransactionError and UnknownTransactionCommitResult. Transactions need a replica set; on a standalone
mongod (a laptop) or the in-memory test database it runs the callback once with session=None and logs a warning
once, so code written for transactions still works there. Every write inside must pass `session=session`.
"""
import logging
from types import SimpleNamespace

import pymongo
from pymongo import AsyncMongoClient, ReadPreference
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from settings import get_settings

log = logging.getLogger(__name__)

state = SimpleNamespace(client=None, db=None, txn=None, warned=False, appname=None)
TXN_TIMEOUT_FACTOR = 2  # a whole transaction with its retries: twice MONGO_TIMEOUT_MS (in_transaction)


def client_options(appname: str | None = None) -> dict:
    s = get_settings()
    return {"maxPoolSize": s.mongo_max_pool_size, "minPoolSize": 0, "maxIdleTimeMS": 300_000,
            "serverSelectionTimeoutMS": s.mongo_server_selection_timeout_ms, "connectTimeoutMS": 5000,
            "timeoutMS": s.mongo_timeout_ms, "retryWrites": True, "retryReads": True, "w": "majority",
            "appname": appname or state.appname or s.mongo_appname}


def set_appname(name: str):
    """Call before the first get_db() to name this process's connections (e.g. the worker)."""
    state.appname = name


def get_db():
    if state.db is None:
        s = get_settings()
        state.client = AsyncMongoClient(s.mongo_url, **client_options())
        state.db = state.client[s.mongo_db]
    return state.db


async def close():
    """Close the client this module opened (not a database a test or the dev server put in `state.db`)."""
    if state.client is not None:
        await state.client.close()
        state.client = state.db = None
    state.txn = None


async def ping(db=None, timeout: float = 2.0) -> None:
    """Raises when the database does not answer within `timeout` seconds (/readyz)."""
    db = db if db is not None else get_db()
    if not isinstance(db, AsyncDatabase):
        return  # the in-memory test database is always there
    with pymongo.timeout(timeout):
        await db.command("ping")


async def supports_transactions(db=None) -> bool:
    """True on a replica set or a sharded cluster; checked once per client."""
    db = db if db is not None else get_db()
    if not isinstance(db, AsyncDatabase):
        return False
    if state.txn is None or state.txn[0] is not db.client:
        hello = await db.client.admin.command("hello")
        state.txn = (db.client, bool(hello.get("setName")) or hello.get("msg") == "isdbgrid")
    return state.txn[1]


async def in_transaction(callback, db=None, *, read_concern=ReadConcern("snapshot"),
                         write_concern=WriteConcern("majority"), read_preference=ReadPreference.PRIMARY,
                         max_commit_time_ms: int | None = None, timeout: float | None = None):
    """`await callback(session)` in one transaction (see the module notes); returns what the callback returns.
    The callback may run more than once (retries), so it must not have side effects outside the database.

    The whole transaction, retries included, gets `timeout` seconds (default twice MONGO_TIMEOUT_MS, 10 s: the
    app's own limit on a request, after which nobody waits for the answer): without a pymongo.timeout() around it,
    with_transaction keeps retrying transient errors (write conflicts) for up to 120 s. Over the limit PyMongo
    raises a timeout error (`.timeout` is True), which the API answers with 503 db_busy (overload.py); a bill's
    Idempotency-Key makes the app's retry safe. Inside a longer pymongo.timeout() (a job) the shorter of the two
    applies, as pymongo.timeout() nests."""
    db = db if db is not None else get_db()
    if not await supports_transactions(db):
        if not state.warned:
            state.warned = True
            log.warning("MongoDB is not a replica set: writes run without transactions (dev only)")
        return await callback(None)
    seconds = timeout if timeout is not None else TXN_TIMEOUT_FACTOR * get_settings().mongo_timeout_ms / 1000
    with pymongo.timeout(seconds):
        async with db.client.start_session() as session:
            return await session.with_transaction(callback, read_concern=read_concern, write_concern=write_concern,
                                                  read_preference=read_preference,
                                                  max_commit_time_ms=max_commit_time_ms)
