"""env_copy: QA's photos into production's folder, the copied records re-pointed, QA's phones removed."""
import io

import pytest

import db as db_module
import env_copy
from env_copy import R2, copy_photos, moved, plan, r2_from_env
from conftest import run

QA = R2("acct", "key-qa", "s-qa", "photos", "", "nurserypos-staging")
PROD = R2("acct", "key-qa", "s-qa", "photos", "", "nurserypos")


class FakeS3:
    """The few S3 calls env_copy makes, over {bucket: {key: (bytes, content type)}}."""

    def __init__(self, buckets=None, page=2):
        self.buckets = buckets if buckets is not None else {}
        self.page = page
        self.calls: list[str] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in s3.buckets.get(Bucket, {}) if k.startswith(Prefix))
                for i in range(0, len(keys), s3.page):
                    yield {"Contents": [{"Key": k, "Size": len(s3.buckets[Bucket][k][0])} for k in keys[i:i + s3.page]]}
                if not keys:
                    yield {}
        return P()

    def head_object(self, Bucket, Key):
        self.calls.append("head")
        return {"ContentLength": len(self.buckets[Bucket][Key][0])}  # KeyError = not there

    def copy_object(self, Bucket, Key, CopySource, MetadataDirective):
        self.calls.append("copy")
        self.buckets.setdefault(Bucket, {})[Key] = self.buckets[CopySource["Bucket"]][CopySource["Key"]]

    def get_object(self, Bucket, Key):
        self.calls.append("get")
        data, ctype = self.buckets[Bucket][Key]
        return {"Body": io.BytesIO(data), "ContentType": ctype}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.calls.append("put")
        self.buckets.setdefault(Bucket, {})[Key] = (Body, ContentType)


def _photos():
    return {"photos": {
        "nurserypos-staging/shops/s1/a.jpg": (b"aaa", "image/jpeg"),
        "nurserypos-staging/shops/s1/b.jpg": (b"bbbb", "image/jpeg"),
        "nurserypos-staging/library/rose.jpg": (b"rr", "image/jpeg"),
        "nurserypos-stagingX/shops/s9/not-ours.jpg": (b"x", "image/jpeg"),  # another folder that only starts alike
        "nurserypos/shops/old/kept.jpg": (b"k", "image/jpeg"),
    }}


def test_photos_are_copied_server_side_into_productions_folder_and_a_rerun_skips_them():
    s3 = FakeS3(_photos())
    log = []
    totals = copy_photos(QA, PROD, s3, s3, log.append)
    assert totals == {"copied": 3, "skipped": 0, "failed": 0, "bytes": 9}
    b = s3.buckets["photos"]
    assert b["nurserypos/shops/s1/a.jpg"] == (b"aaa", "image/jpeg")
    assert b["nurserypos/library/rose.jpg"] == (b"rr", "image/jpeg")
    assert "nurserypos/shops/s9/not-ours.jpg" not in b and "nurseryposX/shops/s9/not-ours.jpg" not in b
    assert b["nurserypos-staging/shops/s1/a.jpg"] == (b"aaa", "image/jpeg")  # QA keeps its own
    assert "get" not in s3.calls and s3.calls.count("copy") == 3  # same R2 keys: copied inside R2
    again = copy_photos(QA, PROD, s3, s3, log.append)
    assert again["copied"] == 0 and again["skipped"] == 3


def test_photos_between_two_accounts_are_read_and_written():
    src, dst = FakeS3(_photos()), FakeS3({})
    other = R2("acct2", "key-prod", "s-prod", "prod-photos", "", "nurserypos")
    totals = copy_photos(QA, other, src, dst, lambda _: None)
    assert totals["copied"] == 3 and "copy" not in dst.calls
    assert dst.buckets["prod-photos"]["nurserypos/shops/s1/b.jpg"] == (b"bbbb", "image/jpeg")


def test_a_failed_photo_is_counted_and_the_rest_go_on():
    s3 = FakeS3(_photos())
    real_copy = s3.copy_object

    def flaky(Bucket, Key, CopySource, MetadataDirective):
        if Key.endswith("a.jpg"):
            raise RuntimeError("R2 said no")
        real_copy(Bucket, Key, CopySource, MetadataDirective)
    s3.copy_object = flaky
    log = []
    totals = copy_photos(QA, PROD, s3, s3, log.append)
    assert totals["failed"] == 1 and totals["copied"] == 2
    assert any("FAILED nurserypos-staging/shops/s1/a.jpg" in line for line in log)


def test_public_store_goes_to_the_public_store():
    qa = R2("acct", "k", "s", "private", "public", "nurserypos-staging")
    prod = R2("acct", "k", "s", "private", "public", "nurserypos")
    assert plan(qa, prod) == [("private", "private"), ("public", "public")]
    s3 = FakeS3({"private": {"nurserypos-staging/shops/s/bill.jpg": (b"b", "image/jpeg")},
                 "public": {"nurserypos-staging/shops/s/p.jpg": (b"p", "image/jpeg")}})
    copy_photos(qa, prod, s3, s3, lambda _: None)
    assert "nurserypos/shops/s/bill.jpg" in s3.buckets["private"] and "nurserypos/shops/s/p.jpg" in s3.buckets["public"]


def test_settings_that_would_share_or_lose_photos_are_refused():
    with pytest.raises(SystemExit, match="same folder"):
        plan(QA, R2("acct", "k", "s", "photos", "", "nurserypos-staging"))
    assert plan(QA, R2("acct", "k", "s", "other-bucket", "", "nurserypos-staging")) == [("photos", "other-bucket")]
    with pytest.raises(SystemExit, match="R2_PUBLIC_BUCKET"):
        plan(QA, R2("acct", "k", "s", "photos", "public", "nurserypos"))
    qa_public = R2("acct", "k", "s", "photos", "public", "nurserypos-staging")
    with pytest.raises(SystemExit, match="R2_PUBLIC_BUCKET"):  # a public bucket named like the private one
        plan(qa_public, R2("acct", "k", "s", "photos", "photos", "nurserypos"))
    # QA's public photos into production's only bucket (production then serves them itself)
    assert plan(qa_public, PROD) == [("photos", "photos"), ("public", "photos")]
    with pytest.raises(SystemExit, match="STORAGE_BACKEND"):
        r2_from_env({"SRC_STORAGE_BACKEND": "local"}, "SRC_")
    with pytest.raises(SystemExit, match="SRC_R2_BUCKET"):
        r2_from_env({"SRC_STORAGE_BACKEND": "r2", "SRC_R2_ACCOUNT_ID": "a", "SRC_R2_ACCESS_KEY_ID": "k",
                     "SRC_R2_SECRET_ACCESS_KEY": "s"}, "SRC_")
    side = r2_from_env({"STORAGE_BACKEND": "r2", "R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k",
                        "R2_SECRET_ACCESS_KEY": "s", "R2_BUCKET": "b"})
    assert side.prefix == "nurserypos" and side.stores == {"private": "b", "public": "b"}


def test_moved_repoints_only_texts_under_the_old_folder():
    old, new = "nurserypos-staging/", "nurserypos/"
    doc = {"image_path": "nurserypos-staging/shops/s/a.jpg", "name": "Rose", "n": 3, "none": None,
           "pages": ["nurserypos-staging/shops/s/p1.jpg", "elsewhere/p2.jpg"],
           "nested": {"logo": "nurserypos-staging/shops/s/logo.png", "other": "nurserypos-stagingX/z.jpg"},
           "rows": [{"photo": "nurserypos-staging/library/r.jpg"}], "mine": "nurserypos/shops/s/new.jpg"}
    out, changed = moved(doc, old, new)
    assert changed
    assert out["image_path"] == "nurserypos/shops/s/a.jpg"
    assert out["pages"] == ["nurserypos/shops/s/p1.jpg", "elsewhere/p2.jpg"]
    assert out["nested"] == {"logo": "nurserypos/shops/s/logo.png", "other": "nurserypos-stagingX/z.jpg"}
    assert out["rows"] == [{"photo": "nurserypos/library/r.jpg"}]
    assert out["mine"] == "nurserypos/shops/s/new.jpg" and out["n"] == 3 and out["none"] is None
    assert moved({"name": "Rose"}, old, new) == ({"name": "Rose"}, False)


def test_rewrite_paths_and_clear_push_in_the_database(client):
    async def scenario():
        db = db_module.get_db()
        await db.products.insert_many([
            {"id": "p1", "name": "Rose", "image_path": "nurserypos-staging/shops/s/a.jpg"},
            {"id": "p2", "name": "Pot", "image_path": None},
        ])
        await db.purchases.insert_one({"id": "b1", "pages": ["nurserypos-staging/shops/s/bill1.jpg"]})
        await db.push_tokens.insert_many([{"token": "ExponentPushToken[qa1]"}, {"token": "ExponentPushToken[qa2]"}])
        counts = await env_copy.rewrite_paths(db, "nurserypos-staging", "nurserypos", log=lambda _: None)
        removed = await env_copy.clear_push(db, log=lambda _: None)
        p1 = await db.products.find_one({"id": "p1"})
        p2 = await db.products.find_one({"id": "p2"})
        b1 = await db.purchases.find_one({"id": "b1"})
        again = await env_copy.rewrite_paths(db, "nurserypos-staging", "nurserypos", log=lambda _: None)
        return counts, removed, p1, p2, b1, again, await db.push_tokens.count_documents({})

    counts, removed, p1, p2, b1, again, left = run(scenario())
    assert counts == {"products": 1, "purchases": 1}
    assert p1["image_path"] == "nurserypos/shops/s/a.jpg" and p2["image_path"] is None
    assert b1["pages"] == ["nurserypos/shops/s/bill1.jpg"]
    assert again == {} and removed == 2 and left == 0
