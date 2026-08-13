#!/usr/bin/env python3
"""Build an iCalendar feed from Visit Arizona's public event pages."""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from icalendar import Calendar, Event

BASE_URL = "https://www.visitarizona.com"
EVENTS_URL = f"{BASE_URL}/events"
OUTPUT = Path(__file__).with_name("visitarizona.ics")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VisitArizonaCalendar/1.0)"}
TIMEOUT = 30


def fetch(session: requests.Session, url: str) -> str:
    response = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def sitemap_urls(session: requests.Session, url: str, seen: set[str] | None = None) -> set[str]:
    """Read either a sitemap or sitemap index recursively."""
    seen = seen or set()
    if url in seen:
        return set()
    seen.add(url)
    try:
        root = ElementTree.fromstring(fetch(session, url))
    except Exception as exc:
        print(f"WARNING: could not read {url}: {exc}")
        return set()
    locations = {node.text.strip() for node in root.iter() if node.tag.endswith("loc") and node.text}
    if root.tag.endswith("sitemapindex"):
        result: set[str] = set()
        for child in sorted(locations):
            result.update(sitemap_urls(session, child, seen))
        return result
    return locations


def discover_event_urls(session: requests.Session) -> list[str]:
    urls = sitemap_urls(session, f"{BASE_URL}/sitemap.xml")
    # Also inspect the calendar page in case a new event appears before the sitemap refreshes.
    try:
        soup = BeautifulSoup(fetch(session, EVENTS_URL), "html.parser")
        urls.update(urljoin(BASE_URL, a["href"]) for a in soup.select("a[href]") if "/events/" in a["href"])
    except Exception as exc:
        print(f"WARNING: could not inspect event index: {exc}")
    clean = {
        u.split("#", 1)[0].rstrip("/")
        for u in urls
        if urlparse(u).netloc.endswith("visitarizona.com")
        and re.search(r"/events/[^/?#]+$", urlparse(u).path.rstrip("/"))
    }
    return sorted(clean)


def compact(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date_parser.parse(str(value), fuzzy=False).date()
    except (ValueError, TypeError, OverflowError):
        return None


def json_ld_events(soup: BeautifulSoup) -> list[dict]:
    found: list[dict] = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if item.get("@type") == "Event" or "Event" in (item.get("@type") or []):
                    found.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
    return found


def location_from_json(value: object) -> str:
    if isinstance(value, str):
        return compact(value)
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    if value.get("name"):
        parts.append(compact(str(value["name"])))
    address = value.get("address")
    if isinstance(address, str):
        parts.append(compact(address))
    elif isinstance(address, dict):
        parts.extend(compact(str(address.get(k, ""))) for k in
                     ("streetAddress", "addressLocality", "addressRegion", "postalCode") if address.get(k))
    return ", ".join(dict.fromkeys(p for p in parts if p and p.lower() != "undefined"))


def text_after_heading(soup: BeautifulSoup, label: str) -> str:
    heading = soup.find(lambda tag: tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
                        and label.lower() in compact(tag.get_text()).lower())
    if not heading:
        return ""
    for node in heading.find_all_next(limit=8):
        if node is heading or node.name in {"script", "style"}:
            continue
        value = compact(node.get_text(" ", strip=True))
        if value and label.lower() not in value.lower():
            return value
    return ""


def page_text_dates(soup: BeautifulSoup) -> tuple[date | None, date | None]:
    text = compact(soup.get_text(" ", strip=True))
    marker = re.search(r"SAVE THE DATE\s+(.{0,80})", text, re.I)
    segment = marker.group(1) if marker else text
    values = re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b", segment)
    parsed = [parse_date(v) for v in values[:2]]
    return (parsed[0] if parsed else None, parsed[1] if len(parsed) > 1 else None)


def parse_event_page(page_html: str, url: str) -> dict | None:
    soup = BeautifulSoup(page_html, "html.parser")
    structured = json_ld_events(soup)
    data = structured[0] if structured else {}
    title = compact(str(data.get("name", "")))
    if not title:
        title = compact(soup.h1.get_text(" ", strip=True)) if soup.h1 else ""
    if not title:
        return None

    start = parse_date(data.get("startDate"))
    end = parse_date(data.get("endDate"))
    if not start:
        start, fallback_end = page_text_dates(soup)
        end = end or fallback_end
    if not start:
        return None
    end = end or start
    # A few source records have an end date earlier than the start date. Treat them as one-day events.
    if end < start:
        print(f"WARNING: reversed dates on {url}; using {start} as both start and end")
        end = start

    description = compact(str(data.get("description", "")))
    if not description:
        meta = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = compact(meta.get("content", "")) if meta else ""
    location = location_from_json(data.get("location"))
    if not location:
        location = text_after_heading(soup, "Location")
    if location.lower() == "undefined":
        location = ""
    external = ""
    for link in soup.select('a[href]'):
        href = urljoin(url, link.get("href", ""))
        if compact(link.get_text()).lower() == "website" and urlparse(href).scheme in {"http", "https"}:
            external = href
            break
    return {
        "title": title,
        "start": start,
        "end": end,
        "description": description,
        "location": location,
        "url": url,
        "external": external,
    }


def deduplicate(events: list[dict]) -> list[dict]:
    unique: dict[tuple[str, date], dict] = {}
    for event in events:
        key = (re.sub(r"\W+", " ", event["title"].lower()).strip(), event["start"])
        current = unique.get(key)
        if not current or sum(bool(event[k]) for k in ("location", "description", "external")) > sum(
            bool(current[k]) for k in ("location", "description", "external")
        ):
            unique[key] = event
    return sorted(unique.values(), key=lambda item: (item["start"], item["title"].lower()))


def write_calendar(events: list[dict]) -> None:
    calendar = Calendar()
    calendar.add("prodid", "-//Visit Arizona Events Calendar//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("x-wr-calname", "Visit Arizona Events")
    calendar.add("x-wr-caldesc", "Events collected from VisitArizona.com")
    stamp = datetime.now(timezone.utc)
    for item in events:
        event = Event()
        event.add("uid", f"{hashlib.sha256(item['url'].encode()).hexdigest()[:24]}@visitarizona-calendar")
        event.add("summary", item["title"])
        event.add("dtstart", item["start"])
        event.add("dtend", item["end"] + timedelta(days=1))  # all-day DTEND is exclusive
        event.add("dtstamp", stamp)
        event.add("last-modified", stamp)
        if item["location"]:
            event.add("location", item["location"])
        details = item["description"]
        if item["external"]:
            details = f"{details}\n\nEvent website: {item['external']}".strip()
        details = f"{details}\n\nVisit Arizona listing: {item['url']}".strip()
        event.add("description", details)
        event.add("url", item["url"])
        calendar.add_component(event)
    OUTPUT.write_bytes(calendar.to_ical())


def main() -> None:
    session = requests.Session()
    urls = discover_event_urls(session)
    print(f"Visit Arizona discovered {len(urls)} event pages")
    events: list[dict] = []
    today = date.today()
    for index, url in enumerate(urls, 1):
        try:
            item = parse_event_page(fetch(session, url), url)
            if item and item["end"] >= today - timedelta(days=7):
                events.append(item)
        except Exception as exc:
            print(f"WARNING: {url}: {exc}")
        if index < len(urls):
            time.sleep(0.15)
    events = deduplicate(events)
    if not events:
        raise RuntimeError("No current or future events were parsed; existing calendar was not replaced")
    write_calendar(events)
    print(f"Visit Arizona: wrote {len(events)} events to {OUTPUT.name}")


if __name__ == "__main__":
    main()
