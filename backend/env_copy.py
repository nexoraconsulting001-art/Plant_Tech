"""Copy QA's photos into production's own photo folder, and point the copied records at them.

Run by deploy/copy-qa-to-prod.sh inside production's `migrate` container, after QA's database has been restored
into production's. The container's own settings are production's (MONGO_URL, MONGO_DB, STORAGE_PREFIX, R2_*);
QA's photo settings come in as the same names with SRC_ in front (SRC_R2_BUCKET, SRC_STORAGE_PREFIX, ...),
passed by name from the script's environment so no key appears on a command line.

    python -m env_copy photos    copy every object under QA's folder (SRC_STORAGE_PREFIX/) to production's
                                 (STORAGE_PREFIX/), private store to private store, public to public. Objects
                                 already there with the same size are skipped, so it can be run again.
    python -m env_copy rewrite   in production's database, every text that starts with "<QA folder>/" now starts
                                 with "<production folder>/" (photo paths are the only such texts).
    python -m env_copy clear-push  removes the phones registered for notifications: they belong to the QA app,
                                 and the production app registers its own when it opens.

Photo paths are stored whole, folder included (storage_client: `<STORAGE_PREFIX>/shops/<shop>/<id>.jpg`), so a
production that kept QA's paths would read QA's folder, and a photo removed on QA would vanish from production.
"""
import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

from pymongo import UpdateOne

BATCH = 500


# ---- photos ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class R2:
    """One side's photo settings: R2 keys, its stores and its folder."""
    account: str
    key_id: str
    secret: str
    bucket: str
    public_bucket: str
    prefix: str
    endpoint: str = ""  # R2_ENDPOINT_URL: another S3-compatible address (a rehearsal); empty = Cloudflare R2

    @property
    def url(self) -> str:
        return self.endpoint or f"https://{self.account}.r2.cloudflarestorage.com"

    @property
    def stores(self) -> dict[str, str]:
        """{"private": bucket, "public": bucket}; one bucket for both without a separate public one."""
        return {"private": self.bucket, "public": self.public_bucket or self.bucket}

    @property
    def separate_public(self) -> bool:
        return bool(self.public_bucket) and self.public_bucket != self.bucket

    def same_keys(self, other: "R2") -> bool:
        return (self.url, self.key_id) == (other.url, other.key_id)


def r2_from_env(env: dict, pre: str = "") -> R2:
    backend = (env.get(pre + "STORAGE_BACKEND") or "local").lower()
    if backend != "r2":
        raise SystemExit(f"{pre}STORAGE_BACKEND is {backend!r}: this copies photos between R2 folders only")
    missing = [n for n in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
               if not env.get(pre + n)]
    if missing:
        raise SystemExit("missing " + ", ".join(pre + n for n in missing))
    return R2(env[pre + "R2_ACCOUNT_ID"], env[pre + "R2_ACCESS_KEY_ID"], env[pre + "R2_SECRET_ACCESS_KEY"],
              env[pre + "R2_BUCKET"], env.get(pre + "R2_PUBLIC_BUCKET", ""),
              (env.get(pre + "STORAGE_PREFIX") or "nurserypos").strip("/"), env.get(pre + "R2_ENDPOINT_URL", ""))


def client_for(side: R2):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3", endpoint_url=side.url,
        aws_access_key_id=side.key_id, aws_secret_access_key=side.secret, region_name="auto",
        config=Config(max_pool_connections=20, connect_timeout=5, read_timeout=60, signature_version="s3v4",
                      retries={"mode": "standard", "total_max_attempts": 5},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required"))


def plan(src: R2, dst: R2) -> list[tuple[str, str]]:
    """(source bucket, destination bucket) pairs to copy, refusing settings that would share or lose photos."""
    if src.separate_public and not dst.separate_public and dst.public_bucket:
        raise SystemExit("R2_PUBLIC_BUCKET: set it in production as in QA (both separate, or both empty)")
    if dst.separate_public and not src.separate_public:
        raise SystemExit("production has a separate R2_PUBLIC_BUCKET and QA does not: set them the same way")
    pairs = [(src.stores["private"], dst.stores["private"])]
    if src.separate_public:
        pairs.append((src.stores["public"], dst.stores["public"] if dst.separate_public else dst.stores["private"]))
    for s, d in pairs:
        if s == d and src.prefix == dst.prefix:
            raise SystemExit(f"QA and production use the same folder {src.prefix}/ in bucket {s}: give production "
                             "its own STORAGE_PREFIX (e.g. nurserypos) so the two never share photos")
    return pairs


def _size(client, bucket: str, key: str) -> int | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except Exception as e:  # noqa: BLE001 - botocore ClientError 404, or a fake's KeyError
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound") or isinstance(e, KeyError):
            return None
        raise


def copy_photos(src: R2, dst: R2, src_client, dst_client, log=print) -> dict:
    """Copy every object under src.prefix/ to dst.prefix/ (see plan). Server-side when both sides use the same
    R2 keys, else read and write. Returns counts; failed keys are listed (at most 20) and counted."""
    totals = {"copied": 0, "skipped": 0, "failed": 0, "bytes": 0}
    server_side = src.same_keys(dst)
    for s_bucket, d_bucket in plan(src, dst):
        log(f"photos: {s_bucket}/{src.prefix}/ -> {d_bucket}/{dst.prefix}/ ({'server-side' if server_side else 'read and write'})")
        pages = src_client.get_paginator("list_objects_v2").paginate(Bucket=s_bucket, Prefix=src.prefix + "/")
        for page in pages:
            for obj in page.get("Contents", []) or []:
                key, size = obj["Key"], obj["Size"]
                new = dst.prefix + key[len(src.prefix):]
                try:
                    if _size(dst_client, d_bucket, new) == size:
                        totals["skipped"] += 1
                        continue
                    if server_side:
                        dst_client.copy_object(Bucket=d_bucket, Key=new, CopySource={"Bucket": s_bucket, "Key": key},
                                               MetadataDirective="COPY")
                    else:
                        got = src_client.get_object(Bucket=s_bucket, Key=key)
                        dst_client.put_object(Bucket=d_bucket, Key=new, Body=got["Body"].read(),
                                              ContentType=got.get("ContentType") or "application/octet-stream")
                    totals["copied"] += 1
                    totals["bytes"] += size
                except Exception as e:  # noqa: BLE001 - keep going, report at the end
                    totals["failed"] += 1
                    if totals["failed"] <= 20:
                        log(f"  FAILED {key}: {e}")
                done = totals["copied"] + totals["skipped"]
                if done and done % 500 == 0:
                    log(f"  {done} done")
    log("photos: {copied} copied, {skipped} already there, {failed} failed, {mb:.1f} MB".format(
        mb=totals["bytes"] / 1e6, **totals))
    return totals


# ---- photo paths in the database ------------------------------------------------------------------

def moved(value, old: str, new: str):
    """(value with every text starting with `old` re-pointed to `new`, whether anything changed); recursive."""
    if isinstance(value, str):
        return (new + value[len(old):], True) if value.startswith(old) else (value, False)
    if isinstance(value, dict):
        out, changed = {}, False
        for k, v in value.items():
            out[k], c = moved(v, old, new)
            changed |= c
        return out, changed
    if isinstance(value, list):
        out, changed = [], False
        for v in value:
            nv, c = moved(v, old, new)
            out.append(nv)
            changed |= c
        return out, changed
    return value, False


async def rewrite_paths(db, src_prefix: str, dst_prefix: str, log=print) -> dict[str, int]:
    """Re-point every stored text under "<src_prefix>/" to "<dst_prefix>/", in every collection."""
    old, new = src_prefix.strip("/") + "/", dst_prefix.strip("/") + "/"
    if old == new:
        log("paths: same folder name on both sides, nothing to change")
        return {}
    counts: dict[str, int] = {}
    for name in sorted(await db.list_collection_names()):
        if name.startswith("system."):
            continue
        ops, n = [], 0
        async for doc in db[name].find({}):
            fields = {}
            for k, v in doc.items():
                if k == "_id":
                    continue
                nv, changed = moved(v, old, new)
                if changed:
                    fields[k] = nv
            if fields:
                ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": fields}))
                n += 1
            if len(ops) >= BATCH:
                await db[name].bulk_write(ops, ordered=False)
                ops = []
        if ops:
            await db[name].bulk_write(ops, ordered=False)
        if n:
            counts[name] = n
            log(f"paths: {name}: {n} records re-pointed")
    log(f"paths: {sum(counts.values())} records in {len(counts)} collections now use {new}")
    return counts


async def clear_push(db, log=print) -> int:
    n = (await db.push_tokens.delete_many({})).deleted_count
    log(f"notifications: {n} QA-app phones removed (the production app registers its own)")
    return n


# ---- command line ---------------------------------------------------------------------------------

async def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m env_copy")
    p.add_argument("command", choices=["photos", "rewrite", "clear-push"])
    a = p.parse_args(argv)
    env = dict(os.environ)
    if a.command == "photos":
        src, dst = r2_from_env(env, "SRC_"), r2_from_env(env)
        totals = copy_photos(src, dst, client_for(src), client_for(dst))
        return 1 if totals["failed"] else 0
    from pymongo import AsyncMongoClient

    from settings import get_settings

    s = get_settings()
    client = AsyncMongoClient(s.mongo_url, appname="env-copy")
    try:
        database = client[s.mongo_db]
        if a.command == "rewrite":
            src_prefix = (env.get("SRC_STORAGE_PREFIX") or "").strip("/")
            if not src_prefix:
                raise SystemExit("SRC_STORAGE_PREFIX (QA's photo folder) is missing")
            await rewrite_paths(database, src_prefix, (env.get("STORAGE_PREFIX") or "nurserypos").strip("/"))
        else:
            await clear_push(database)
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
