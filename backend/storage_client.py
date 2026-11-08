"""Photo storage: Cloudflare R2 (S3-compatible) or local disk, in two stores.

- private (R2_BUCKET, or LOCAL_STORAGE_DIR): supplier-bill scans and every photo not known to be public yet.
  Served only through the API (a short-lived presigned link, or the API itself on local disk).
- public (R2_PUBLIC_BUCKET, or LOCAL_PUBLIC_STORAGE_DIR): product photos, shop logos, library pictures.
  Meant to sit behind the CDN at PUBLIC_IMAGE_BASE_URL (an R2 custom domain), so the API never reads them.
  Without R2_PUBLIC_BUCKET both stores are the one R2_BUCKET (as before WP-7) and nothing goes to the CDN.

Keys are the same in both stores (`<STORAGE_PREFIX>/shops/<shop>/<id>.jpg`), so a photo keeps its path when it
is copied to the public store. get_object() without a store looks in the private store, then the public one,
so code that only knows a path (AI bill pages, library contributions, the bill logo) keeps working.

The R2 client: one per process, at most 50 pooled connections, 5 s connect / 30 s read timeouts, and botocore's
"standard" retry mode (3 attempts with backoff).
"""
import os
import shutil
from pathlib import Path

PRIVATE, PUBLIC = "private", "public"

STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local").lower()  # "r2" | "local"
LOCAL_DIR = Path(os.environ.get("LOCAL_STORAGE_DIR", "/data/uploads"))
# Inside LOCAL_DIR by default so one volume keeps both; keys start with STORAGE_PREFIX, never "_public".
LOCAL_PUBLIC_DIR = Path(os.environ.get("LOCAL_PUBLIC_STORAGE_DIR") or LOCAL_DIR / "_public")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_PUBLIC_BUCKET = os.environ.get("R2_PUBLIC_BUCKET", "")
APP_NAME = os.environ.get("STORAGE_PREFIX", "nurserypos")  # folder in the bucket; staging uses its own
PUBLIC_CACHE_CONTROL = "public, max-age=31536000, immutable"  # a key never gets other content
PRIVATE_CACHE_CONTROL = "private, max-age=3600"

_s3 = None


class NotFound(Exception):
    """No object under that key (in the store asked for)."""


def init_storage():
    """Call once at startup. Idempotent."""
    global _s3
    if STORAGE_BACKEND == "r2":
        if _s3 is None:
            import boto3
            from botocore.config import Config

            _s3 = boto3.client(
                "s3",
                # R2_ENDPOINT_URL: another S3-compatible address, for a rehearsal on a laptop; unset on the VM
                endpoint_url=os.environ.get("R2_ENDPOINT_URL")
                or f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
                config=Config(
                    max_pool_connections=50, connect_timeout=5, read_timeout=30,
                    retries={"mode": "standard", "total_max_attempts": 3}, signature_version="s3v4",
                    # Cloudflare's R2 + boto3 guidance: checksums only where S3 requires one.
                    request_checksum_calculation="when_required", response_checksum_validation="when_required",
                ),
            )
        return _s3
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    return None


def public_store_separate() -> bool:
    """Public photos live apart from private ones: always on local disk, on R2 only with R2_PUBLIC_BUCKET."""
    return STORAGE_BACKEND != "r2" or bool(R2_PUBLIC_BUCKET and R2_PUBLIC_BUCKET != R2_BUCKET)


def can_presign() -> bool:
    return STORAGE_BACKEND == "r2"


def _bucket(store: str) -> str:
    return R2_PUBLIC_BUCKET if store == PUBLIC and R2_PUBLIC_BUCKET else R2_BUCKET


def _local_path(path: str, store: str = PRIVATE) -> Path:
    root = (LOCAL_PUBLIC_DIR if store == PUBLIC else LOCAL_DIR).resolve()
    p = (root / path).resolve()
    if root not in p.parents:
        raise ValueError("Invalid path")
    return p


def _is_missing(e: Exception) -> bool:
    code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
    return code in ("NoSuchKey", "404", "NotFound")


def put_object(path: str, data: bytes, content_type: str, store: str = PRIVATE) -> dict:
    s3 = init_storage()
    if s3 is not None:
        s3.put_object(Bucket=_bucket(store), Key=path, Body=data, ContentType=content_type,
                      CacheControl=PUBLIC_CACHE_CONTROL if store == PUBLIC else PRIVATE_CACHE_CONTROL)
    else:
        p = _local_path(path, store)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return {"path": path, "size": len(data)}


def _get(path: str, store: str) -> tuple[bytes, str]:
    s3 = init_storage()
    if s3 is not None:
        try:
            obj = s3.get_object(Bucket=_bucket(store), Key=path)
        except Exception as e:
            if _is_missing(e):
                raise NotFound(path) from e
            raise
        return obj["Body"].read(), obj.get("ContentType", "application/octet-stream")
    p = _local_path(path, store)
    if not p.is_file():
        raise NotFound(path)
    ctype = {".png": "image/png", ".webp": "image/webp"}.get(p.suffix.lower(), "image/jpeg")
    return p.read_bytes(), ctype


def get_object(path: str, store: str | None = None) -> tuple[bytes, str]:
    """The bytes and content type. No store given: the private store first, then the public one."""
    if store is not None:
        return _get(path, store)
    try:
        return _get(path, PRIVATE)
    except NotFound:
        if not public_store_separate():
            raise
        return _get(path, PUBLIC)


def object_size(path: str, store: str = PRIVATE) -> int:
    """Size in bytes; NotFound if there is no such object."""
    s3 = init_storage()
    if s3 is not None:
        try:
            return int(s3.head_object(Bucket=_bucket(store), Key=path)["ContentLength"])
        except Exception as e:
            if _is_missing(e):
                raise NotFound(path) from e
            raise
    p = _local_path(path, store)
    if not p.is_file():
        raise NotFound(path)
    return p.stat().st_size


def copy_to_public(path: str, content_type: str = "image/jpeg"):
    """Copy a private object to the public store under the same key (server-side on R2: no bytes through us)."""
    if not public_store_separate():
        return
    s3 = init_storage()
    if s3 is not None:
        try:
            s3.copy_object(Bucket=_bucket(PUBLIC), Key=path, CopySource={"Bucket": _bucket(PRIVATE), "Key": path},
                           MetadataDirective="REPLACE", ContentType=content_type, CacheControl=PUBLIC_CACHE_CONTROL)
        except Exception as e:
            if _is_missing(e):
                raise NotFound(path) from e
            raise
        return
    src, dst = _local_path(path, PRIVATE), _local_path(path, PUBLIC)
    if not src.is_file():
        raise NotFound(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def delete_object(path: str, store: str = PRIVATE):
    """Remove one object. Used only for raw direct uploads once processed; saved photos are never deleted here."""
    s3 = init_storage()
    if s3 is not None:
        s3.delete_object(Bucket=_bucket(store), Key=path)
        return
    _local_path(path, store).unlink(missing_ok=True)


def delete_many(keys: list[str], store: str = PRIVATE):
    """Remove these objects from one store (a shop being erased: its photos and thumbnails). Missing ones are
    skipped; R2 takes them 1000 per DeleteObjects call."""
    s3 = init_storage()
    if s3 is not None:
        for i in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=_bucket(store), Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]],
                                                             "Quiet": True})
        return
    for k in keys:
        try:
            _local_path(k, store).unlink(missing_ok=True)  # refuses paths outside the photo folder
        except (OSError, ValueError):
            pass


def presigned_get(path: str, store: str, seconds: int) -> str:
    """A link anyone can open for `seconds` (R2 only)."""
    return init_storage().generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(store), "Key": path}, ExpiresIn=seconds)


def presigned_put(path: str, content_type: str, size: int, seconds: int) -> str:
    """A link the phone can PUT exactly `size` bytes of `content_type` to, for `seconds` (R2, private store)."""
    return init_storage().generate_presigned_url(
        "put_object", Params={"Bucket": _bucket(PRIVATE), "Key": path, "ContentType": content_type,
                              "ContentLength": size}, ExpiresIn=seconds)
