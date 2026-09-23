from __future__ import annotations

import email.utils
import fcntl
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

USER_AGENT = "ensignwesley-news-feed/1.0 (+https://github.com/ensignwesley/news-feed)"
ITEM_KEYS = ("title", "url", "source", "publishedAt", "fetchedAt")
DATE_TAGS = ("published", "updated", "pubDate", "date", "created")
LINK_TAGS = ("link", "guid")


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(node: ET.Element, names: Iterable[str]) -> str | None:
    wanted = set(names)
    for child in node:
        if local_name(child.tag) in wanted and child.text and child.text.strip():
            return child.text.strip()
    return None


def item_url(node: ET.Element) -> str | None:
    fallback = None
    for child in node:
        if local_name(child.tag) not in LINK_TAGS:
            continue
        href = child.attrib.get("href")
        text = (child.text or "").strip()
        candidate = href or text
        if not candidate:
            continue
        rel = child.attrib.get("rel", "alternate")
        if rel == "alternate":
            return candidate
        fallback = fallback or candidate
    return fallback


def parse_feed(xml_bytes: bytes, source: str, fetched_at: datetime) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML: {exc}") from exc
    nodes = [n for n in root.iter() if local_name(n.tag) in {"item", "entry"}]
    if not nodes:
        raise ValueError("feed contains no RSS items or Atom entries")
    result = []
    for node in nodes:
        title = child_text(node, ("title",))
        url = item_url(node)
        if not title or not url:
            continue
        date_text = child_text(node, DATE_TAGS)
        published = parse_date(date_text)
        # A present but unsupported date is a feed incompatibility, not a missing date.
        if date_text and published is None:
            raise ValueError(f"unsupported item date {date_text!r}")
        result.append({
            "title": " ".join(title.split()),
            "url": url.strip(),
            "source": source,
            "publishedAt": iso(published) if published else None,
            "fetchedAt": iso(fetched_at),
        })
    if not result:
        raise ValueError("feed has no items with both title and URL")
    return result


def fetch_url(url: str, timeout: float = 12.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(8 * 1024 * 1024 + 1)


def fetch_with_retries(url: str, *, timeout: float, retries: int, fetcher: Callable[[str, float], bytes] = fetch_url, sleeper: Callable[[float], None] = time.sleep) -> bytes:
    last = None
    for attempt in range(retries + 1):
        try:
            body = fetcher(url, timeout)
            if len(body) > 8 * 1024 * 1024:
                raise ValueError("feed exceeds 8 MiB limit")
            return body
        except Exception as exc:  # normalized into status; each attempt remains bounded
            last = exc
            if attempt < retries:
                sleeper(min(2 ** attempt, 4))
    raise RuntimeError(f"{type(last).__name__}: {last}") from last


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("refresh already running") from exc
        yield


def choose_items(items: list[dict], now: datetime) -> list[dict]:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=2)
    eligible = []
    for item in items:
        published = parse_date(item.get("publishedAt"))
        if published is not None and published < cutoff:
            continue
        eligible.append(item)
    eligible.sort(key=lambda item: (item["publishedAt"] is not None, item["publishedAt"] or ""), reverse=True)
    return eligible[:5]


def merged_output(cache: dict[str, list[dict]]) -> list[dict]:
    output = [item for items in cache.values() for item in items]
    output.sort(key=lambda item: (item["publishedAt"] is not None, item["publishedAt"] or ""), reverse=True)
    return [{key: item.get(key) for key in ITEM_KEYS} for item in output]


def refresh(*, feeds: dict[str, str], now: datetime, output_dir: Path, timeout: float = 12.0, retries: int = 2, fetcher: Callable[[str, float], bytes] = fetch_url, sleeper: Callable[[float], None] = time.sleep) -> tuple[list[dict], dict]:
    now = now.astimezone(timezone.utc)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "cache.json"
    feed_path = output_dir / "feed.json"
    status_path = output_dir / "status.json"
    lock_path = output_dir / "refresh.lock"
    with exclusive_lock(lock_path):
        cache = read_json(cache_path, {})
        prior_status = read_json(status_path, {"sources": {}})
        if not isinstance(cache, dict):
            raise ValueError("cache.json is not an object")
        sources = {}
        for source, url in feeds.items():
            old = list(cache.get(source, []))
            previous = prior_status.get("sources", {}).get(source, {}) if isinstance(prior_status, dict) else {}
            try:
                body = fetch_with_retries(url, timeout=timeout, retries=retries, fetcher=fetcher, sleeper=sleeper)
                selected = choose_items(parse_feed(body, source, now), now)
                cache[source] = selected
                sources[source] = {
                    "url": url,
                    "lastSuccess": iso(now),
                    "lastAttempt": iso(now),
                    "itemCount": len(selected),
                    "error": None,
                }
            except Exception as exc:
                # Retain last-good items, while still enforcing the global two-day contract.
                cache[source] = choose_items(old, now)
                sources[source] = {
                    "url": url,
                    "lastSuccess": previous.get("lastSuccess"),
                    "lastAttempt": iso(now),
                    "itemCount": len(cache[source]),
                    "error": str(exc),
                }
        # Drop stale sources removed from injected configuration.
        cache = {source: cache.get(source, []) for source in feeds}
        output = merged_output(cache)
        status = {
            "generatedAt": iso(now),
            "healthy": all(value["error"] is None for value in sources.values()),
            "sources": sources,
        }
        # Cache first: interrupted writes can only leave recoverable newer cache behind.
        atomic_json(cache_path, cache)
        atomic_json(feed_path, output)
        atomic_json(status_path, status)
        return output, status
