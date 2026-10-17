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

def build(source, url, output):
    found = {}
    # Pagination for Sedona is /P24/, /P48/, ...; stop on repeated pages.
    pages = [url]
    if "visitsedona.com" in url:
        pages += [url.rstrip("/") + "/P" + str(n) + "/" for n in range(24, 241, 24)]
    for page in pages:
        try: soup = get(page)
        except requests.RequestException as exc:
            print(f"WARNING {source}: {page}: {exc}")
            continue
        for obj in event_jsonld(soup):
            title = compact(obj.get("name"))
            start = parse_date(obj.get("startDate"))
            if not title or not start or not TODAY <= start <= END: continue
            end = parse_date(obj.get("endDate")) or start
            if end < start: end = start
            link = obj.get("url") or page
            if isinstance(link, dict): link = link.get("@id", page)
            link = urljoin(page, str(link))
            if urlparse(link).scheme not in ("https", "http"): continue
            location = safe_location(obj)
            description = compact(obj.get("description"))[:1200]
            key = (title.casefold(), start)
            item = (title, start, end, link, location, description)
            if key not in found or (location and not found[key][4]): found[key] = item
    print(f"{source}: {len(found)} future structured events")
    path = Path(__file__).with_name(output)
    if not found:
        if path.exists():
            print(f"WARNING {source}: preserving existing {output}; no verified new events")
            return
        print(f"WARNING {source}: no verified events; feed not created")
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
    for source, url, output in SOURCES.values(): build(source, url, output)
