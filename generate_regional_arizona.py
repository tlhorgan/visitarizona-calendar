#!/usr/bin/env python3
"""Build independent Arizona regional ICS feeds from public event listings.

Never geocode generic city text as a street address. Missing event locations
are intentionally omitted from LOCATION so the map can suppress those pins.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dates
from icalendar import Calendar, Event

SOURCES = {
    "sedona": ("Visit Sedona", "https://visitsedona.com/events-calendar/", "sedona-events.ics"),
    "mesa": ("Visit Mesa", "https://www.visitmesa.com/events/", "mesa-events.ics"),
    "flagstaff": ("Flagstaff Events", "https://www.flagstaff.az.gov/calendar.aspx", "flagstaff-events.ics"),
}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RegionalEventsCalendar/1.0)"}
TODAY = date.today()
END = TODAY + timedelta(days=120)

def compact(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()

def get(url):
    response = requests.get(url, headers=HEADERS, timeout=35)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def event_jsonld(soup):
    for tag in soup.select('script[type="application/ld+json"]'):
        try: data = json.loads(tag.string or tag.get_text())
        except (ValueError, TypeError): continue
        stack = [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list): stack.extend(obj)
            elif isinstance(obj, dict):
                types = obj.get("@type", [])
                if types == "Event" or (isinstance(types, list) and "Event" in types):
                    yield obj
                for key in ("@graph", "itemListElement"):
                    if key in obj: stack.append(obj[key])

def safe_location(obj):
    location = obj.get("location", {})
    if isinstance(location, list): location = location[0] if location else {}
    if not isinstance(location, dict): return ""
    address = location.get("address", {})
    if isinstance(address, str):
        return compact(address)[:160] if len(compact(address)) < 160 else ""
    if not isinstance(address, dict): return ""
    fields = [compact(address.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
    # No usable city or street: do not produce an ambiguous map location.
    if not (fields[0] or fields[1]): return ""
    return ", ".join(x for x in fields if x)[:180]

def parse_date(value):
    try: return dates.parse(str(value)).date()
    except (ValueError, TypeError, OverflowError): return None

def listing_links(soup, page, slug):
    """Discover detail pages, not just JSON-LD on calendar index pages."""
    prefix = "/events-calendar/" if slug == "sedona" else "/events/"
    out = set()
    for a in soup.select("a[href]"):
        href = urljoin(page, a.get("href", "")).split("#")[0]
        parsed = urlparse(href)
        if not parsed.path.startswith(prefix) or parsed.path.rstrip("/") == prefix.rstrip("/"):
            continue
        if slug == "sedona" and re.search(r"/P\d+/?$", parsed.path, re.I):
            continue
        if slug == "mesa" and ("/annual-events/" in parsed.path or
                               "/featured-events-calendar/" in parsed.path or
                               "/event-newsletter/" in parsed.path):
            continue
        if parsed.netloc.replace("www.", "") == urlparse(page).netloc.replace("www.", ""):
            out.add(href)
    return out


def detail_event(soup, url, slug):
    """Read human-visible event details when pages omit Event JSON-LD."""
    results = []
    for obj in event_jsonld(soup):
        title, start = compact(obj.get("name")), parse_date(obj.get("startDate"))
        if title and start:
            results.append((title, start, parse_date(obj.get("endDate")) or start,
                            url, safe_location(obj), compact(obj.get("description"))[:1200]))
    if results:
        return results
    h1 = soup.find("h1")
    title = compact(h1.get_text(" ", strip=True)) if h1 else ""
    if not title: return []
    text = compact(soup.get_text(" ", strip=True))
    # Sedona detail pages explicitly label the date, before the event text.
    if slug == "sedona":
        date_match = re.search(r"\bDate:\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+([A-Za-z]+\s+\d{1,2},?\s+20\d{2})", text, re.I)
        if not date_match:
            date_match = re.search(r"\bDate:\s*([A-Za-z]+\s+\d{1,2},?\s+20\d{2})", text, re.I)
        start = parse_date(date_match.group(1)) if date_match else None
        directions = soup.find(string=re.compile(r"^\s*Directions\s*$", re.I))
        location = ""
        if directions:
            section = directions.find_parent(["section", "div"])
            if section:
                for item in section.stripped_strings:
                    value = compact(item)
                    if re.search(r"\d+\s+.{2,80}\b(?:Sedona|Village of Oak Creek|Cottonwood)\b", value, re.I):
                        location = value[:180]
                        break
    else:
        # Mesa event pages show date ribbons and offer an iCal link.
        # The calendar link, when available, is the authoritative date source.
        start = None
        date_match = re.search(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?,?\s+20\d{2}", text, re.I)
        if date_match: start = parse_date(date_match.group(0).split("–")[0].split("-")[0])
        if not start:
            # Visit Mesa detail pages show a numeric MM DD date badge immediately
            # before the heading, often without a year in the date text.
            heading = soup.find("h1")
            nearby = compact(" ".join(heading.find_all_previous(string=True, limit=14)[::-1])) if heading else ""
            badge = re.search(r"\\b(1[0-2]|0?[1-9])\\s+(3[01]|[12]\\d|0?[1-9])\\b", nearby)
            if badge:
                month, day = map(int, badge.groups())
                try:
                    candidate = date(TODAY.year, month, day)
                    if candidate < TODAY - timedelta(days=30):
                        candidate = date(TODAY.year + 1, month, day)
                    start = candidate
                except ValueError:
                    pass
        location = ""
        address = re.search(r"\b\d{1,6}\s+[\w.\- ]{3,85}?,?\s+(?:Mesa|Gilbert|Tempe|Phoenix|Queen Creek|Chandler|Scottsdale),?\s+(?:AZ|Arizona)?\s*\d{5}\b", text, re.I)
        if address: location = compact(address.group(0))[:180]
    if not start: return []
    summary = soup.select_one('meta[name="description"],meta[property="og:description"]')
    desc = compact(summary.get("content", "")) if summary else ""
    return [(title, start, start, url, location, desc[:1200])]


def mesa_newsletter_entries(soup, page):
    """Parse Visit Mesa's server-rendered Upcoming Events cards.

    Their listing has dates immediately before linked event titles, while
    individual event detail pages may omit machine-readable date metadata.
    """
    lines = [compact(s) for s in soup.get_text("\n", strip=True).splitlines()]
    lines = [s for s in lines if s]
    start_marker = next((i for i, s in enumerate(lines) if s == "Upcoming Events"), None)
    if start_marker is None:
        print("WARNING Visit Mesa: Upcoming Events section not found")
        return []
    lines = lines[start_marker + 1:]
    stop = next((i for i, s in enumerate(lines) if s in ("Email Newsletter", "Visitors Guide")), len(lines))
    lines = lines[:stop]
    title_links = {}
    for a in soup.select('a[href]'):
        href = urljoin(page, a["href"])
        if re.search(r"/events/[^/?#]+/?$", urlparse(href).path):
            title_links.setdefault(compact(a.get_text(" ", strip=True)).casefold(), href)
    month = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    date_pattern = re.compile(
        rf"^({month}\s+\d{{1,2}})(?:\s+to\s+({month}\s+\d{{1,2}}(?:,\s*20\d{{2}})?))?(?:,\s*(20\d{{2}}))?$",
        re.I,
    )
    out = []
    for idx, line in enumerate(lines):
        match = date_pattern.fullmatch(line.replace("\xa0", " "))
        if not match or idx + 1 >= len(lines):
            continue
        title = lines[idx + 1]
        link = title_links.get(title.casefold())
        if not link or title.lower() in ("learn more", "website", "save"):
            continue
        year = int(match.group(3) or TODAY.year)
        start = parse_date(f"{match.group(1)}, {year}")
        if not start:
            continue
        finish = start
        if match.group(2):
            specified_year = re.search(r"20\d{2}$", match.group(2))
            end_year = int(specified_year.group()) if specified_year else year
            finish = parse_date(f"{match.group(2)}, {end_year}") or start
            if finish < start and not specified_year:
                finish = parse_date(f"{match.group(2)}, {year + 1}") or start
        # Restrict to upcoming or ongoing dates; do not misassign past events.
        if finish < TODAY or start > END:
            continue
        nearby = lines[idx + 2:next(
            (j for j in range(idx + 2, min(idx + 10, len(lines)))
             if date_pattern.fullmatch(lines[j].replace("\xa0", " "))),
            min(idx + 10, len(lines)),
        )]
        venue_text = " ".join(x for x in nearby[:3] if x not in ("Website", "Learn More", "Save"))
        # Require a recognizable street number and Mesa-area locality.
        street = re.search(
            r"\b\d{1,6}\s+.{3,100}?\b(?:Mesa|Gilbert|Tempe|Phoenix|Queen Creek|Chandler|Scottsdale)\s*,?\s*(?:AZ|Arizona)\s*\d{5}\b",
            venue_text, re.I,
        )
        location = compact(street.group(0))[:180] if street else ""
        out.append((title, max(start, TODAY), finish, link, location, venue_text[:500]))
    print(f"Visit Mesa: {len(out)} dated entries from newsletter listing")
    return out


def flagstaff_entries(soup, page):
    """Flagstaff CivicEngage publishes events in list markup, not JSON-LD."""
    results = []
    text = compact(soup.get_text(" ", strip=True))
    # Limit matches to event listing headings followed by dated detail text.
    for header in soup.select("h3, h4"):
        title = compact(header.get_text(" ", strip=True))
        if not title or len(title) > 160: continue
        node = header.find_parent("li") or header.find_parent(class_=re.compile("event", re.I))
        excerpt = compact(node.get_text(" ", strip=True))[:900] if node else ""
        if not excerpt: continue
        m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+20\d{2}", excerpt, re.I)
        if not m: continue
        start = parse_date(m.group(0))
        if start:
            a = header.find("a", href=True)
            link = urljoin(page, a["href"]) if a else page
            results.append((title, start, start, link, "", excerpt[:500]))
    return results


def build(source, url, output, slug):
    found = {}
    pages = [url]
    if slug == "sedona":
        pages += [url.rstrip("/") + "/P" + str(n) + "/" for n in range(12, 241, 12)]
    elif slug == "mesa":
        # The main events calendar is JavaScript-rendered; the newsletter page
        # exposes real event links in server-rendered HTML.
        pages = ["https://www.visitmesa.com/events/event-newsletter/"]
    elif slug == "flagstaff":
        pages = [url + "?CID=0&view=list&month=" + str((TODAY.month - 1 + n) % 12 + 1)
                 + "&year=" + str(TODAY.year + (TODAY.month - 1 + n) // 12)
                 for n in range(5)]
    links = set()
    entries = []
    for page in pages:
        try: soup = get(page)
        except requests.RequestException as exc:
            print(f"WARNING {source}: {page}: {exc}")
            continue
        if slug == "flagstaff":
            entries.extend(flagstaff_entries(soup, page))
        elif slug == "mesa":
            entries.extend(mesa_newsletter_entries(soup, page))
        else:
            links.update(listing_links(soup, page, slug))
            for obj in event_jsonld(soup):
                title, start = compact(obj.get("name")), parse_date(obj.get("startDate"))
                if title and start:
                    link = urljoin(page, str(obj.get("url") or page))
                    entries.append((title, start, parse_date(obj.get("endDate")) or start,
                                    link, safe_location(obj), compact(obj.get("description"))[:1200]))
    # Avoid long jobs and rate limits; always work from real event detail links.
    print(f"{source}: {len(links)} detail links discovered")
    for link in sorted(links)[:300]:
        try: entries.extend(detail_event(get(link), link, slug))
        except requests.RequestException as exc: print(f"WARNING {link}: {exc}")
    for item in entries:
        title, start, end, link, location, desc = item
        if not TODAY <= start <= END: continue
        if end < start: end = start
        key = (title.casefold(), start)
        if key not in found or (location and not found[key][4]):
            found[key] = (title, start, end, link, location, desc)
    print(f"{source}: {len(found)} future events parsed")
    path = Path(__file__).with_name(output)
    if not found:
        if path.exists():
            print(f"WARNING {source}: keeping existing {output} because extraction found no events")
        else:
            print(f"WARNING {source}: no events; output not created")
        return
    cal = Calendar()
    cal.add("prodid", "-//Arizona Regional Events//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", source)
    for title, start, end, link, location, description in sorted(found.values(), key=lambda x: x[1]):
        e = Event()
        e.add("uid", hashlib.sha256((source+link+str(start)).encode()).hexdigest()[:24] + "@arizona-regional")
        e.add("summary", title)
        e.add("dtstart", start)
        e.add("dtend", end + timedelta(days=1))
        e.add("dtstamp", datetime.now(timezone.utc))
        e.add("url", link)
        if location: e.add("location", location)
        e.add("description", (description + "\n\nSource: " + link).strip())
        cal.add_component(e)
    path.write_bytes(cal.to_ical())

if __name__ == "__main__":
    for slug, (source, url, output) in SOURCES.items(): build(source, url, output, slug)
