"""Publishing to S3 + CloudFront and DB/raw backups, against moto's S3."""
from __future__ import annotations

import gzip
import sqlite3
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from capex import paths, settings
from capex.server import backup, publish

REGION = "eu-north-1"
SITE, BACKUPS = "capex-site-test", "capex-backup-test"
WB1 = "[2026.10.29 - 21h05] financials sourcebook.xlsx"
WB2 = "[2026.10.30 - 07h40] financials sourcebook.xlsx"
WB2B = "[2026.10.30 - 07h40] financials sourcebook v2.xlsx"


class FakeCloudFront:
    def __init__(self):
        self.invalidations = []

    def create_invalidation(self, DistributionId, InvalidationBatch):  # noqa: N803
        self.invalidations.append((DistributionId, InvalidationBatch["Paths"]["Items"]))
        return {"Invalidation": {"Id": f"I{len(self.invalidations)}"}}


@pytest.fixture
def aws(capex_db, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (SITE, BACKUPS):
            s3.create_bucket(Bucket=bucket,
                             CreateBucketConfiguration={"LocationConstraint": REGION})
        settings.set("publish.site_bucket", SITE, db=capex_db)
        settings.set("publish.distribution_id", "EDIST", db=capex_db)
        settings.set("backup.bucket", BACKUPS, db=capex_db)
        yield s3


def _site(files: dict[str, bytes]):
    for rel, body in files.items():
        path = paths.site_dir() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def _workbooks(*names):
    paths.workbook_dir().mkdir(parents=True, exist_ok=True)
    for name in names:
        (paths.workbook_dir() / name).write_bytes(f"xlsx {name}".encode())


def _publish(capex_db, s3, cf, **kw):
    return publish.publish_site(db=capex_db, s3=s3, cloudfront=cf, log=lambda _: None, **kw)


# ---- publish --------------------------------------------------------------------------

def test_workbook_keys_and_download_names():
    assert publish.workbook_key(WB1) == "workbooks/20261029-2105.xlsx"
    assert publish.workbook_key(WB2B) == "workbooks/20261030-0740-v2.xlsx"
    assert publish.workbook_key("notes.xlsx") is None
    disposition = publish.content_disposition(WB1)
    assert f'filename="{WB1}"' in disposition
    assert "filename*=UTF-8''%5B2026.10.29%20-%2021h05%5D%20financials" in disposition


def test_first_publish_uploads_everything_with_headers(capex_db, aws):
    _site({"index.html": b"<h1>dash</h1>", "charts/capex.png": b"PNG",
           "cloud.html": b"<p>cloud</p>"})
    _workbooks(WB1, WB2, WB2B)
    cf = FakeCloudFront()
    summary = _publish(capex_db, aws, cf)

    keys = {o["Key"] for o in aws.list_objects_v2(Bucket=SITE)["Contents"]}
    assert keys == {"index.html", "charts/capex.png", "cloud.html", "workbooks.html",
                    "download/latest.xlsx", "workbooks/20261029-2105.xlsx",
                    "workbooks/20261030-0740.xlsx", "workbooks/20261030-0740-v2.xlsx"}
    page = aws.head_object(Bucket=SITE, Key="index.html")
    assert (page["ContentType"], page["CacheControl"]) == ("text/html; charset=utf-8",
                                                           publish.CACHE_SHORT)
    book = aws.head_object(Bucket=SITE, Key="workbooks/20261030-0740-v2.xlsx")
    assert book["CacheControl"] == publish.CACHE_IMMUTABLE
    assert f'filename="{WB2B}"' in book["ContentDisposition"]
    latest = aws.get_object(Bucket=SITE, Key="download/latest.xlsx")
    assert latest["Body"].read() == f"xlsx {WB2B}".encode()       # v2 beats v1 in a minute
    listing = aws.get_object(Bucket=SITE, Key="workbooks.html")["Body"].read().decode()
    assert listing.index(WB2B) < listing.index(WB2) < listing.index(WB1)
    assert cf.invalidations == [("EDIST", ["/*"])] and summary["invalidation"] == "I1"


def test_an_unchanged_site_uploads_nothing(capex_db, aws):
    _site({"index.html": b"<h1>dash</h1>"})
    cf = FakeCloudFront()
    _publish(capex_db, aws, cf)
    again = _publish(capex_db, aws, cf)
    assert again["uploaded"] == [] and again["deleted"] == []
    assert len(cf.invalidations) == 1                                # no second invalidation


def test_changes_and_removals_are_mirrored(capex_db, aws):
    _site({"index.html": b"v1", "old.html": b"gone soon"})
    _workbooks(WB1)
    cf = FakeCloudFront()
    _publish(capex_db, aws, cf)
    (paths.site_dir() / "old.html").unlink()
    (paths.site_dir() / "index.html").write_bytes(b"v2")
    aws.put_object(Bucket=SITE, Key="_doctor/probe.txt", Body=b"x")    # left alone
    summary = _publish(capex_db, aws, cf)
    assert summary["uploaded"] == ["index.html"]
    assert summary["deleted"] == ["old.html"]
    assert aws.get_object(Bucket=SITE, Key="index.html")["Body"].read() == b"v2"
    assert len(cf.invalidations) == 2


def test_header_policy_changes_reupload(capex_db, aws, monkeypatch):
    _site({"index.html": b"same"})
    _publish(capex_db, aws, FakeCloudFront())
    monkeypatch.setattr(publish, "CACHE_SHORT", "public, max-age=60")
    summary = _publish(capex_db, aws, FakeCloudFront())
    assert "index.html" in summary["uploaded"]
    assert aws.head_object(Bucket=SITE, Key="index.html")["CacheControl"] == "public, max-age=60"


def test_mass_deletion_needs_force(capex_db, aws):
    for i in range(12):
        aws.put_object(Bucket=SITE, Key=f"page{i}.html", Body=b"x")
    _site({"index.html": b"new site"})
    with pytest.raises(publish.PublishError):
        _publish(capex_db, aws, FakeCloudFront())
    assert _publish(capex_db, aws, FakeCloudFront(), force=True)["deleted"]


def test_publish_skips_until_configured_and_generated(capex_db, aws):
    assert _publish(capex_db, aws, FakeCloudFront())["status"] == "skipped"   # no index.html
    _site({"index.html": b"x"})
    settings.reset("publish.site_bucket", db=capex_db)
    assert "site_bucket" in _publish(capex_db, aws, FakeCloudFront())["reason"]


def test_dry_run_changes_nothing(capex_db, aws):
    _site({"index.html": b"x"})
    summary = _publish(capex_db, aws, FakeCloudFront(), dry_run=True)
    assert summary["status"] == "dry-run" and "index.html" in summary["uploaded"]
    assert "Contents" not in aws.list_objects_v2(Bucket=SITE)


# ---- backups -------------------------------------------------------------------------

def test_db_backup_is_verified_uploaded_and_rotated(capex_db, aws):
    settings.set("backup.keep_daily", 2, db=capex_db)
    stamps = [datetime(2026, 10, d, 3, 15, tzinfo=timezone.utc) for d in (27, 28, 29)]
    for now in stamps:
        summary = backup.backup_db(db=capex_db, s3=aws, now=now, log=lambda _: None)
    assert summary["integrity"] == "ok"
    assert summary["uploaded"] == ["db/capex-20261029T031500Z.db.gz",
                                   "db/capex-20261029T031500Z.dump.sql.gz"]
    local = sorted(p.name for p in (paths.backups_dir() / "db").iterdir())
    assert local == ["capex-20261028T031500Z.db.gz", "capex-20261028T031500Z.dump.sql.gz",
                     "capex-20261029T031500Z.db.gz", "capex-20261029T031500Z.dump.sql.gz"]
    listed = backup.list_backups(db=capex_db, s3=aws)
    assert len(listed) == 6 and listed[0]["key"].startswith("db/capex-20261029")
    dump = gzip.decompress((paths.backups_dir() / "db" /
                            "capex-20261029T031500Z.dump.sql.gz").read_bytes()).decode()
    assert "CREATE TABLE" in dump and "companies" in dump


def test_restore_round_trip(capex_db, aws, tmp_path):
    backup.backup_db(db=capex_db, s3=aws, now=datetime(2026, 10, 29, tzinfo=timezone.utc),
                     log=lambda _: None)
    target = tmp_path / "restored" / "capex.db"
    backup.restore_db("db/capex-20261029T000000Z.db.gz", to=target, db=capex_db, s3=aws,
                      log=lambda _: None)
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 13
    with pytest.raises(backup.BackupError):                       # won't overwrite silently
        backup.restore_db("db/capex-20261029T000000Z.db.gz", to=target, db=capex_db, s3=aws)


def test_backup_without_a_bucket_keeps_a_local_copy(capex_db):
    summary = backup.backup_db(db=capex_db, log=lambda _: None)
    assert summary["uploaded"] == [] and "local copy only" in summary["upload_skipped"]
    assert (paths.backups_dir() / "db" / summary["db"]).exists()


def test_raw_sync_uploads_only_missing_files(capex_db, aws):
    raw = paths.sources_dir() / "MSFT" / "_raw"
    raw.mkdir(parents=True)
    (raw / "[2026.10.29][MSFT][Q1][10-Q].htm").write_bytes(b"filing")
    (raw / "[2026.10.29][MSFT][Q1][10-Q].htm.fetch.json").write_bytes(b"{}")
    first = backup.sync_raw(db=capex_db, s3=aws, log=lambda _: None)
    assert (first["files"], first["uploaded"]) == (2, 2)
    (raw / "[2026.07.30][MSFT][AR][10-K].htm").write_bytes(b"annual")
    second = backup.sync_raw(db=capex_db, s3=aws, log=lambda _: None)
    assert second["uploaded"] == 1
    assert aws.get_object(Bucket=BACKUPS, Key="raw/MSFT/_raw/[2026.07.30][MSFT][AR][10-K].htm")
