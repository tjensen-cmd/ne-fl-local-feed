#!/usr/bin/env python3
"""Daily collector for the NE Florida local news feed.

Reads sources.yaml, fetches each active feed (at most once per day per
source), stores headline/link/date/source/county/excerpt in data/articles.jsonl,
and regenerates docs/data.json with just the last 14 days for the page to load.

A broken or blocked feed is logged to logs/fetch_log.txt and skipped; it never
stops the run.
"""

import json
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import yaml

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.yaml"
ARTICLES_FILE = ROOT / "data" / "articles.jsonl"
LAST_FETCH_FILE = ROOT / "data" / "last_fetch.json"
LOG_FILE = ROOT / "logs" / "fetch_log.txt"
OUTPUT_FILE = ROOT / "docs" / "data.json"

USER_AGENT = "NEFLLocalFeedBot/0.1 (personal local-news RSS reader; one request/day)"
REQUEST_TIMEOUT = 15
EXCERPT_MAX_CHARS = 300
DAYS_TO_KEEP_ON_PAGE = 14

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def log(message):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{timestamp} {message}\n")


def load_sources():
    with SOURCES_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_last_fetch():
    if LAST_FETCH_FILE.exists():
        with LAST_FETCH_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_last_fetch(state):
    LAST_FETCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LAST_FETCH_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_existing_links():
    links = set()
    if ARTICLES_FILE.exists():
        with ARTICLES_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    links.add(json.loads(line)["link"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return links


def clean_excerpt(raw_html):
    if not raw_html:
        return ""
    text = HTML_TAG_RE.sub(" ", raw_html)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > EXCERPT_MAX_CHARS:
        text = text[: EXCERPT_MAX_CHARS - 1].rstrip() + "…"
    return text


def entry_published_iso(entry):
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_feed_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def process_source(source, existing_links):
    name = source["name"]
    county = source["county"]
    feed_url = source["feed_url"]
    keywords = source.get("county_filter_keywords")

    raw = fetch_feed_bytes(feed_url)
    parsed = feedparser.parse(raw)

    new_items = []
    for entry in parsed.entries:
        link = getattr(entry, "link", None)
        headline = getattr(entry, "title", None)
        if not link or not headline:
            continue
        if link in existing_links:
            continue
        if keywords and not any(k.lower() in link.lower() or k.lower() in headline.lower() for k in keywords):
            continue

        summary = getattr(entry, "summary", "") or ""
        new_items.append(
            {
                "headline": headline.strip(),
                "link": link,
                "published": entry_published_iso(entry),
                "source": name,
                "county": county,
                "excerpt": clean_excerpt(summary),
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tags": [],
            }
        )
        existing_links.add(link)

    return new_items


def regenerate_page_data():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_TO_KEEP_ON_PAGE)
    recent = []
    if ARTICLES_FILE.exists():
        with ARTICLES_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    article = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    published = datetime.strptime(article["published"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                except (KeyError, ValueError):
                    continue
                if published >= cutoff:
                    recent.append(article)

    recent.sort(key=lambda a: a["published"], reverse=True)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "articles": recent,
            },
            f,
            indent=2,
        )
    return len(recent)


def main():
    sources = load_sources()
    last_fetch = load_last_fetch()
    existing_links = load_existing_links()
    today = datetime.now().strftime("%Y-%m-%d")

    total_new = 0
    ARTICLES_FILE.parent.mkdir(parents=True, exist_ok=True)

    for source in sources:
        name = source["name"]
        if source.get("status") != "active":
            continue

        if last_fetch.get(name) == today:
            log(f"skip source={name!r} reason=already_fetched_today")
            continue

        try:
            new_items = process_source(source, existing_links)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            log(f"error source={name!r} error={exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - a bad feed must never crash the run
            log(f"error source={name!r} error=unexpected:{exc}")
            continue

        if new_items:
            with ARTICLES_FILE.open("a", encoding="utf-8") as f:
                for item in new_items:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")

        total_new += len(new_items)
        last_fetch[name] = today
        log(f"ok source={name!r} new_items={len(new_items)}")

    save_last_fetch(last_fetch)
    kept = regenerate_page_data()
    log(f"run_complete new_items_total={total_new} articles_on_page={kept}")
    print(f"Done. {total_new} new article(s) collected. {kept} article(s) now on the page (last {DAYS_TO_KEEP_ON_PAGE} days).")


if __name__ == "__main__":
    main()
