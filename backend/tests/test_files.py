"""Photo files (WP-7): thumbnails, public and private stores, redirects, direct uploads, limits, unique paths."""
import base64
import importlib
import io
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.exceptions import ClientError
from PIL import Image

import jobs
import storage_client
from conftest import Shop, photo_b64, run
from db import get_db
from routers import files
from settings import get_settings

CDN = "https://img.test"


@pytest.fixture(autouse=True)
def fresh_caches():
    files._unknown.clear()
    files._queued.clear()
    yield
    files._unknown.clear()
    files._queued.clear()


@pytest.fixture
def cdn(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_image_base_url", CDN)
    return CDN


class FakeR2:
    """Just enough of boto3's S3 client for storage_client, in memory; counts reads of photo bytes."""

    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}
        self.reads = 0

    @staticmethod
    def _missing(op):
        return ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, op)

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl=None):
        self.objects[(Bucket, Key)] = {"body": Body, "type": ContentType, "cache": CacheControl}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self._missing("GetObject")
        self.reads += 1
        o = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(o["body"]), "ContentType": o["type"]}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[(Bucket, Key)]["body"])}

    def copy_object(self, Bucket, Key, CopySource, MetadataDirective, ContentType, CacheControl):
        src = (CopySource["Bucket"], CopySource["Key"])
        if src not in self.objects:
            raise self._missing("CopyObject")
        self.objects[(Bucket, Key)] = {**self.objects[src], "type": ContentType, "cache": CacheControl}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def head_bucket(self, Bucket):
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://r2.test/{Params['Bucket']}/{Params['Key']}?op={op}&X-Amz-Expires={ExpiresIn}"


@pytest.fixture
def r2(monkeypatch):
    fake = FakeR2()
    monkeypatch.setattr(storage_client, "STORAGE_BACKEND", "r2")
    monkeypatch.setattr(storage_client, "R2_BUCKET", "priv")
    monkeypatch.setattr(storage_client, "R2_PUBLIC_BUCKET", "pub")
    monkeypatch.setattr(storage_client, "_s3", fake)
    return fake


def shop_id(shop) -> str:
    return shop.get("/api/auth/me").json()["business"]["id"]


def upload(shop, kind=None, **photo):
    body = {"image_base64": photo_b64(**photo)}
    if kind:
        body["kind"] = kind
    r = shop.post("/api/upload-base64", body)
    assert r.status_code == 200, r.text
    return r.json()["path"]


def record(path):
    return run(get_db().uploads.find_one({"path": path}, {"_id": 0}))


def settle():
    run(jobs.drain())


def test_upload_makes_a_webp_thumbnail_and_counts_storage(client):
    shop = Shop(client)
    path = upload(shop, size=(3000, 2000))
    up = record(path)
    assert up["thumb"] == path[:-4] + ".w256.webp" and up["visibility"] is None and up["store"] == "private"
    full = client.get(f"/api/files/{path}")
    assert full.status_code == 200 and full.headers["content-type"] == "image/jpeg"
    small = client.get(f"/api/files/{path}", params={"w": 256})
    assert small.status_code == 200 and small.headers["content-type"] == "image/webp"
    img = Image.open(io.BytesIO(small.content))
    assert img.format == "WEBP" and img.size == (256, 171)
    assert client.get(f"/api/files/{path}", params={"w": 800}).headers["content-type"] == "image/jpeg"
    usage = run(get_db().storage_usage.find_one({"_id": shop_id(shop)}))
    assert usage["files"] == 1 and usage["bytes"] == up["size"] == len(full.content) + len(small.content)
    assert client.get("/api/files/nurserypos/shops/x/missing.jpg").status_code == 404


def test_product_photo_goes_to_the_public_store_and_the_cdn(client, cdn):
    shop = Shop(client)
    path = upload(shop, kind="product")
    up = record(path)
    assert up["visibility"] == "public" and up["store"] == "public"
    assert (storage_client.LOCAL_PUBLIC_DIR / path).is_file() and not (storage_client.LOCAL_DIR / path).exists()
    r = client.get(f"/api/files/{path}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"{cdn}/{path}"
    assert "public" in r.headers["cache-control"]
    r = client.get(f"/api/files/{path}", params={"w": 256}, follow_redirects=False)
    assert r.headers["location"] == f"{cdn}/{up['thumb']}"
    # code that only knows the path still reads it (AI pages, library contributions, the bill logo)
    assert storage_client.get_object(path)[1] == "image/jpeg"


def test_without_a_cdn_photos_come_through_the_api_as_before(client):
    shop = Shop(client)
    path = upload(shop, kind="product")
    r = client.get(f"/api/files/{path}", follow_redirects=False)
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000"


def test_older_apps_product_photo_is_published_once_its_use_is_known(client, cdn):
    """1.1.0 sends no kind: the photo starts private and moves to the CDN once a product uses it."""
    shop = Shop(client)
    path = upload(shop)
    assert client.get(f"/api/files/{path}", follow_redirects=False).status_code == 200  # not used yet: as before
    lib = shop.get("/api/products/suggest", params={"q": "money"}).json()["library"][0]
    shop.post("/api/products", {"name": "Money Plant", "library_id": lib["id"], "selling_price": 250,
                                "photo_path": path})
    files._unknown.clear()  # the "not used yet" answer is remembered for a few minutes
    r = client.get(f"/api/files/{path}", follow_redirects=False)
    assert r.status_code == 200  # classified now; the copy to the public store runs in the background
    settle()
    assert record(path)["visibility"] == "public" and record(path)["store"] == "public"
    assert (storage_client.LOCAL_PUBLIC_DIR / path).is_file()
    r = client.get(f"/api/files/{path}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"{cdn}/{path}"
    # the shared library picture this photo became (no shop) is public too, and gets a thumbnail on first ask
    picture = run(get_db().library.find_one({"id": lib["id"]}))["image_path"]
    assert record(picture)["business_id"] is None and record(picture).get("thumb") is None
    client.get(f"/api/files/{picture}", params={"w": 256})
    settle()
    assert record(picture)["store"] == "public" and record(picture)["thumb"]
    r = client.get(f"/api/files/{picture}", params={"w": 256}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"{cdn}/{record(picture)['thumb']}"


def test_private_bill_pages_are_refused_to_other_shops(client, cdn, monkeypatch):
    a, b = Shop(client), Shop(client, phone="9811155667")
    page = upload(a, kind="bill")
    old_page = upload(a)  # 1.1.0: no kind; private once a supplier bill uses it
    r = a.post("/api/purchases", {"supplier": "Green Farms", "image_paths": [old_page],
                                  "items": [{"name": "Snake Plant", "quantity": 2, "purchase_price": 100}]})
    assert r.status_code == 200, r.text
    for path in (page, old_page):
        assert client.get(f"/api/files/{path}", headers=b.h).status_code == 404  # another shop's login
        assert client.get(f"/api/files/{path}", headers=a.h).status_code == 200
        assert client.get(f"/api/files/{path}", follow_redirects=False).status_code == 200  # 1.1.0: no login
        assert b.get(f"/api/file-links/{path}").status_code == 404
    assert record(old_page)["visibility"] == "private" and record(page)["store"] == "private"
    assert not (storage_client.LOCAL_PUBLIC_DIR / page).exists()
    r = client.get(f"/api/files/{page}", headers=a.h, follow_redirects=False)
    assert r.status_code == 200 and r.headers["cache-control"].startswith("private")  # never to the CDN

    monkeypatch.setattr(files, "PRIVATE_NEED_LOGIN", True)  # once 1.1.0 and 1.2.0 are retired
    assert client.get(f"/api/files/{page}").status_code == 404
    link = a.get(f"/api/file-links/{page}").json()
    assert link["expires_in"] == files.LINK_SECONDS
    url = urlsplit(link["url"])
    assert url.path == f"/api/files/{page}"
    assert client.get(f"{url.path}?{url.query}").status_code == 200
    q = parse_qs(url.query)
    forged = ("B" if q["sig"][0][0] == "A" else "A") + q["sig"][0][1:]
    assert client.get(url.path, params={"exp": q["exp"][0], "sig": forged}).status_code == 404
    assert client.get(url.path, params={"exp": int(q["exp"][0]) + 1, "sig": q["sig"][0]}).status_code == 404
    assert client.get(url.path, params={"exp": 1000, "sig": files._sign(page, 1000)}).status_code == 404  # expired
    assert client.get(f"/api/files/{page}", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_r2_serves_by_redirect_and_never_reads_photos(client, r2, cdn, monkeypatch):
    shop = Shop(client)
    product, bill, unknown = upload(shop, kind="product"), upload(shop, kind="bill"), upload(shop)
    assert ("pub", product) in r2.objects and ("priv", product) not in r2.objects
    assert r2.objects[("pub", product)]["cache"] == storage_client.PUBLIC_CACHE_CONTROL
    assert ("priv", bill) in r2.objects and ("priv", files.thumb_key(bill)) in r2.objects

    r = client.get(f"/api/files/{product}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"{cdn}/{product}"
    r = client.get(f"/api/files/{bill}", headers=shop.h, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(f"https://r2.test/priv/{bill}?op=get_object")
    assert r.headers["cache-control"] == "private, no-store"
    r = client.get(f"/api/files/{unknown}", params={"w": 256}, follow_redirects=False)
    assert r.headers["location"].startswith(f"https://r2.test/priv/{files.thumb_key(unknown)}?")
    assert r2.reads == 0  # the API itself read no photo bytes

    monkeypatch.setattr(get_settings(), "public_image_base_url", "")  # no CDN yet: presigned from the public bucket
    r = client.get(f"/api/files/{product}", follow_redirects=False)
    assert r.headers["location"].startswith(f"https://r2.test/pub/{product}?")

    monkeypatch.setattr(files, "FILES_SERVE", "proxy")  # the switch for trouble: bytes through the API again
    r = client.get(f"/api/files/{product}", follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_r2_older_photo_is_copied_to_the_public_bucket(client, r2, cdn):
    shop = Shop(client)
    path = upload(shop)
    assert client.put("/api/business", headers=shop.h, json={"logo_path": path}).status_code == 200  # 1.1.0 logo
    client.get(f"/api/files/{path}", follow_redirects=False)
    settle()
    assert ("pub", path) in r2.objects and ("pub", files.thumb_key(path)) in r2.objects
    assert r2.objects[("pub", path)]["cache"] == storage_client.PUBLIC_CACHE_CONTROL
    r = client.get(f"/api/files/{path}", follow_redirects=False)
    assert r.headers["location"] == f"{cdn}/{path}"


def test_r2_client_settings(monkeypatch):
    for k, v in {"R2_ACCOUNT_ID": "acc", "R2_ACCESS_KEY_ID": "key", "R2_SECRET_ACCESS_KEY": "secret"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(storage_client, "STORAGE_BACKEND", "r2")
    monkeypatch.setattr(storage_client, "R2_BUCKET", "priv")
    monkeypatch.setattr(storage_client, "_s3", None)
    s3 = storage_client.init_storage()
    cfg = s3.meta.config
    assert cfg.max_pool_connections == 50 and cfg.connect_timeout == 5 and cfg.read_timeout == 30
    assert cfg.retries == {"mode": "standard", "total_max_attempts": 3}
    assert s3.meta.endpoint_url == "https://acc.r2.cloudflarestorage.com"
    url = storage_client.presigned_put("p/incoming/b/1", "image/jpeg", 1234, 600)  # signed offline
    assert "X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost" in url and "X-Amz-Expires=600" in url


def test_direct_upload(client, r2):
    shop = Shop(client)
    r = shop.post("/api/uploads/presign", {"content_type": "image/jpeg", "size": 5000, "kind": "product"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["method"] == "PUT" and d["headers"] == {"Content-Type": "image/jpeg"} and "op=put_object" in d["url"]
    key = urlsplit(d["url"]).path.split("/priv/", 1)[1]
    assert key.startswith(f"nurserypos/incoming/{shop_id(shop)}/")
    assert shop.post(f"/api/uploads/{d['upload_id']}/complete").status_code == 400  # nothing PUT yet
    r2.objects[("priv", key)] = {"body": base64.b64decode(photo_b64((2000, 1500))), "type": "image/jpeg"}
    done = shop.post(f"/api/uploads/{d['upload_id']}/complete")
    assert done.status_code == 200, done.text
    path = done.json()["path"]
    assert ("pub", path) in r2.objects and ("priv", key) not in r2.objects  # checked copy kept, raw removed
    assert record(path)["visibility"] == "public" and record(path)["thumb"]
    assert shop.post(f"/api/uploads/{d['upload_id']}/complete").json()["path"] == path  # repeat: same answer
    other = Shop(client, phone="9811155667")
    assert other.post(f"/api/uploads/{d['upload_id']}/complete").status_code == 404

    bad = shop.post("/api/uploads/presign", {"size": 100}).json()
    bad_key = urlsplit(bad["url"]).path.split("/priv/", 1)[1]
    r2.objects[("priv", bad_key)] = {"body": b"<html>not a photo</html>", "type": "image/jpeg"}
    assert shop.post(f"/api/uploads/{bad['upload_id']}/complete").status_code == 400
    assert ("priv", bad_key) not in r2.objects
    assert shop.post(f"/api/uploads/{bad['upload_id']}/complete").status_code == 404  # start again
    assert shop.post("/api/uploads/presign", {"size": files.MAX_IMAGE_BYTES + 1}).status_code == 422
    assert client.post("/api/uploads/presign", json={"size": 100}).status_code == 401


def test_direct_upload_needs_r2(client):
    shop = Shop(client)
    r = shop.post("/api/uploads/presign", {"size": 100})
    assert r.status_code == 409 and r.json()["code"] == "direct_upload_unavailable"


def test_upload_rate_limit(client, monkeypatch):
    monkeypatch.setattr(files, "UPLOAD_RATE_USER", "2/minute")
    shop = Shop(client)
    upload(shop)
    upload(shop)
    r = shop.post("/api/upload-base64", {"image_base64": photo_b64()})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and "Retry-After" in r.headers


def test_storage_quota(client, monkeypatch):
    monkeypatch.setattr(files, "UPLOAD_QUOTA_MB", 1)
    shop = Shop(client)
    bid = shop_id(shop)
    upload(shop)
    run(get_db().storage_usage.update_one({"_id": bid}, {"$set": {"bytes": 1024 * 1024 - 100}}))
    r = shop.post("/api/upload-base64", {"image_base64": photo_b64()})
    assert r.status_code == 403
    assert r.json()["code"] == "storage_full" and r.json()["params"] == {"limit": 1}
    assert run(get_db().storage_usage.find_one({"_id": bid}))["bytes"] == 1024 * 1024 - 100
    assert run(get_db().uploads.count_documents({"business_id": bid})) == 1
    monkeypatch.setattr(files, "UPLOAD_QUOTA_MB", 0)  # 0 = no limit
    upload(shop)


def test_uploads_path_unique_migration(client):
    migration = importlib.import_module("migrations.0701_uploads_unique")
    db = get_db()
    run(db.uploads.drop_indexes())
    run(db.uploads.create_index("path"))
    run(db.uploads.insert_many([{"path": "p/a.jpg", "business_id": "b", "created_at": "2026-01-02"},
                                {"path": "p/a.jpg", "business_id": "b", "created_at": "2026-01-01", "first": True},
                                {"path": "p/b.jpg", "business_id": "b", "created_at": "2026-01-03"}]))
    run(migration.up(db))
    run(migration.up(db))  # twice: no change
    left = run(db.uploads.find({}, {"_id": 0}).to_list(10))
    assert sorted(d["path"] for d in left) == ["p/a.jpg", "p/b.jpg"]
    assert next(d for d in left if d["path"] == "p/a.jpg").get("first") is True  # the oldest record kept
    info = run(db.uploads.index_information())
    assert [v.get("unique") for v in info.values() if v["key"] == [("path", 1)]] == [True]
    from pymongo.errors import DuplicateKeyError
    with pytest.raises(DuplicateKeyError):
        run(db.uploads.insert_one({"path": "p/b.jpg"}))
