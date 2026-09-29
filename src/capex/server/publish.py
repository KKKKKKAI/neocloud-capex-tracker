"""Publish the public site to S3 behind CloudFront.

    capex server publish [--dry-run] [--force]   (and the `publish` job)

What goes up; the bucket mirrors it, and other keys are deleted:
- site/**: the dashboard, chart pages and PNGs;
- workbooks/<YYYYMMDD-HHMM[-vN]>.xlsx: every workbook under a URL-safe
  key, downloaded under its real name
  ("[2026.08.14 - 11h03] financials sourcebook.xlsx");
- download/latest.xlsx: the newest workbook;
- workbooks.html: a list of all of them.

Only changed files are uploaded: a file's MD5 is compared with the
object's ETag, and its headers with what the last publish sent. Pages,
charts and download/latest.xlsx are cached 5 minutes; a timestamped
workbook never changes, so it is cached for a year. After any change,
one CloudFront invalidation of /* (a single path; 1,000 a month are free).

Safety: nothing is published before site/index.html exists, and a
publish that would delete more than half of a non-trivial bucket stops
unless forced.
"""
from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .. import paths, settings
from ..db import Database
from ..exporters.excel import parse_workbook_name

CACHE_SHORT = "public, max-age=300"
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HTML_TYPE = "text/html; charset=utf-8"
KEEP_PREFIXES = ("_doctor/",)       # the doctor's round-trip probes
RESERVED = ("workbooks/", "download/", "workbooks.html")
MAX_DELETE_SHARE = 0.5
MIN_KEYS_FOR_GUARD = 10


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    key: str
    content_type: str
    cache_control: str
    disposition: str | None = None
    source: Path | None = None
    body: bytes | None = field(default=None, repr=False)

    def data(self) -> bytes:
        if self.body is not None:
            return self.body
        assert self.source is not None
        return self.source.read_bytes()

    @cached_property
    def md5(self) -> str:
        return hashlib.md5(self.data(), usedforsecurity=False).hexdigest()

    def signature(self) -> str:
        return "|".join((self.md5, self.content_type, self.cache_control,
                         self.disposition or ""))


def workbook_key(name: str) -> str | None:
    """`workbooks/20260814-1103.xlsx` (` v2` → `-v2`) for a workbook name."""
    parsed = parse_workbook_name(name)
    if parsed is None:
        return None
    ts, version = parsed
    return f"workbooks/{ts:%Y%m%d-%H%M}{'' if version == 1 else f'-v{version}'}.xlsx"


def content_disposition(filename: str) -> str:
    """Download under `filename`: quoted for old clients, RFC 5987 for the rest."""
    plain = filename.replace('"', "'")
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(filename)}"


def _content_type(path: Path) -> str:
    if path.suffix.lower() in (".html", ".htm"):
        return HTML_TYPE
    ctype, _ = mimetypes.guess_type(path.name)
    if ctype and (ctype.startswith("text/") or ctype in ("application/javascript",
                                                        "application/json")):
        ctype += "; charset=utf-8"
    return ctype or "application/octet-stream"


def workbooks_page(entries: list[tuple[str, str, int]]) -> bytes:
    """The workbooks list: (file name, key, bytes), newest first."""
    rows = "\n".join(
        f"<li><a href=\"{html.escape(key)}\" download>{html.escape(name)}</a>"
        f" <span>{size / 1024:,.0f} KB</span></li>"
        for name, key, size in entries
    ) or "<li>No workbooks yet.</li>"
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workbooks · neocloud capex tracker</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; background: #f6f8fa; color: #1f2328; }}
main {{ max-width: 760px; margin: 0 auto; padding: 24px 16px; }}
a {{ color: #0969da; }}
.latest {{ display: inline-block; padding: 8px 14px; background: #1f883d; color: #fff;
          border-radius: 6px; text-decoration: none; font-weight: 600; }}
ol {{ padding-left: 1.4em; line-height: 1.9; }}
span {{ color: #57606a; font-size: 13px; }}
</style></head><body><main>
<p><a href="index.html">&larr; Dashboard</a></p>
<h1>Excel workbooks</h1>
<p><a class="latest" href="download/latest.xlsx">Download the latest workbook</a></p>
<p>Every value is in USD; each cell's comment cites its SEC or HKEX source.
Newest first.</p>
<ol>
{rows}
</ol>
</main></body></html>
"""
    return page.encode("utf-8")


def build_items(site_dir: Path | None = None, workbook_dir: Path | None = None) -> list[Item]:
    """Everything the bucket should hold."""
    site = site_dir or paths.site_dir()
    items: list[Item] = []
    for path in sorted(p for p in site.rglob("*") if p.is_file()):
        rel = path.relative_to(site).as_posix()
        if any(part.startswith(".") for part in rel.split("/")) or rel.startswith(RESERVED):
            continue
        items.append(Item(rel, _content_type(path), CACHE_SHORT, source=path))
    ranked = sorted(
        ((key, p) for p in (workbook_dir or paths.workbook_dir()).glob("*.xlsx")
         if (key := parse_workbook_name(p.name)) is not None),
        reverse=True,
    )
    listing = []
    for _, path in ranked:
        key = workbook_key(path.name)
        assert key is not None
        items.append(Item(key, XLSX_TYPE, CACHE_IMMUTABLE, content_disposition(path.name),
                          source=path))
        listing.append((path.name, key, path.stat().st_size))
    if ranked:
        newest = ranked[0][1]
        items.append(Item("download/latest.xlsx", XLSX_TYPE, CACHE_SHORT,
                          content_disposition(newest.name), source=newest))
    items.append(Item("workbooks.html", HTML_TYPE, CACHE_SHORT, body=workbooks_page(listing)))
    return items


def _state_path() -> Path:
    return paths.run_dir() / "publish-state.json"


def _load_state() -> dict[str, str]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def remote_etags(s3: Any, bucket: str) -> dict[str, str]:
    etags = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            etags[obj["Key"]] = obj["ETag"].strip('"')
    return etags


def publish_site(
    *,
    db: Database | None = None,
    dry_run: bool = False,
    force: bool = False,
    s3: Any = None,
    cloudfront: Any = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Mirror the site into the bucket; returns a summary (see module doc)."""
    db = db or Database()
    bucket = settings.get("publish.site_bucket", db)
    distribution = settings.get("publish.distribution_id", db)
    if not bucket:
        return {"status": "skipped", "reason": "publish.site_bucket is not set"}
    if not (paths.site_dir() / "index.html").exists():
        return {"status": "skipped",
                "reason": "site/index.html does not exist yet: run regenerate_outputs"}
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    items = build_items()
    remote = remote_etags(s3, bucket)
    state = _load_state()
    uploads = [i for i in items if remote.get(i.key) != i.md5
               or state.get(i.key) != i.signature()]
    wanted = {i.key for i in items}
    deletes = sorted(k for k in remote if k not in wanted and not k.startswith(KEEP_PREFIXES))
    summary: dict[str, Any] = {
        "status": "dry-run" if dry_run else "published", "bucket": bucket,
        "objects": len(items), "uploaded": [i.key for i in uploads], "deleted": deletes,
        "unchanged": len(items) - len(uploads), "invalidation": None,
    }
    if (not force and len(remote) >= MIN_KEYS_FOR_GUARD
            and len(deletes) > MAX_DELETE_SHARE * len(remote)):
        raise PublishError(
            f"refusing to delete {len(deletes)} of {len(remote)} objects in {bucket}; "
            "publish with force if that is intended")
    if dry_run:
        return summary
    for item in uploads:
        extra = {"ContentType": item.content_type, "CacheControl": item.cache_control}
        if item.disposition:
            extra["ContentDisposition"] = item.disposition
        s3.put_object(Bucket=bucket, Key=item.key, Body=item.data(), **extra)
        state[item.key] = item.signature()
        log(f"  put {item.key}")
    for start in range(0, len(deletes), 1000):
        chunk = deletes[start:start + 1000]
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk],
                                                 "Quiet": True})
    for key in deletes:
        state.pop(key, None)
        log(f"  delete {key}")
    _save_state(state)
    if (uploads or deletes) and distribution:
        if cloudfront is None:
            import boto3
            cloudfront = boto3.client("cloudfront")
        response = cloudfront.create_invalidation(
            DistributionId=distribution,
            InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]},
                               "CallerReference": f"capex-{time.time_ns()}"},
        )
        summary["invalidation"] = response["Invalidation"]["Id"]
    log(f"published {len(uploads)} changed, {len(deletes)} deleted, "
        f"{summary['unchanged']} unchanged"
        + (f"; invalidation {summary['invalidation']}" if summary["invalidation"] else ""))
    return summary
