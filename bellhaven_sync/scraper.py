"""Scrape every Bellhaven community from the public website.

Finds communities two ways so nothing is missed:
  1. the paginated directory (/communities?page=N), until a page adds no new links;
  2. any /communities/<slug> link on the home and About pages (the home page
     announces a new community that the directory does not list).
Then reads each community's own page for name, address, care offerings, phone.
"""
import re
import time
from dataclasses import dataclass, field, asdict
from html import unescape
from html.parser import HTMLParser

import requests

from . import config

SLUG_RE = re.compile(r'href="/communities/([a-z0-9\-]+)"')


@dataclass
class Location:
    slug: str
    url: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list = field(default_factory=list)
    phone: str = ""
    administrator: str = ""
    found_via: str = "directory"

    def to_dict(self):
        return asdict(self)


class _DetailParser(HTMLParser):
    """Reads <h1>, the <dt>/<dd> pairs, and the care badges on a community page."""

    def __init__(self):
        super().__init__()
        self.h1, self.fields, self.badges = "", {}, []
        self._in, self._dt, self._dd_parts, self._in_badge = None, None, [], False

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "dt", "dd"):
            self._in = tag
            if tag == "dd":
                self._dd_parts = []
        elif tag == "br" and self._in == "dd":
            self._dd_parts.append("\n")
        elif tag == "span" and ("class", "badge") in attrs and self._in == "dd":
            self._in_badge = True
            self.badges.append("")

    def handle_endtag(self, tag):
        if tag == "span":
            self._in_badge = False
        if tag == "dd" and self._dt:
            self.fields[self._dt] = "".join(self._dd_parts).strip()
        if tag == self._in:
            self._in = None

    def handle_data(self, data):
        if self._in == "h1":
            self.h1 += data
        elif self._in == "dt":
            self._dt = data.strip()
        elif self._in == "dd":
            self._dd_parts.append(data)
            if self._in_badge:
                self.badges[-1] += data


def _parse_city_line(line):
    # "Maplewood, OH 44280"
    m = re.match(r"\s*(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", line)
    if not m:
        raise ValueError(f"Unrecognized city line: {line!r}")
    return m.group(1).strip(), m.group(2), m.group(3)


def parse_detail(slug, html_text, found_via="directory"):
    p = _DetailParser()
    p.feed(html_text)
    address_lines = [unescape(x).strip() for x in p.fields.get("Address", "").split("\n") if x.strip()]
    if len(address_lines) < 2:
        raise ValueError(f"{slug}: address not found")
    city, state, zip_code = _parse_city_line(address_lines[-1])
    return Location(
        slug=slug,
        url=f"{config.BASE_URL}/communities/{slug}",
        name=unescape(p.h1).strip(),
        street=address_lines[0],
        city=city, state=state, zip=zip_code,
        care_offerings=[unescape(b).strip() for b in p.badges if b.strip()],
        phone=p.fields.get("Phone", "").strip(),
        administrator=p.fields.get("Administrator", "").strip(),
        found_via=found_via,
    )


def scrape(session=None, delay=0.1):
    """Returns (locations, notes). notes holds data-quality observations."""
    s = session or requests.Session()
    base = config.BASE_URL
    notes = []

    directory = []
    page = 1
    while page <= 50:
        html_text = s.get(f"{base}/communities", params={"page": page}, timeout=30).text
        new = [x for x in SLUG_RE.findall(html_text) if x not in directory]
        if page == 1:
            m = re.search(r"(\d+)\s+communities listed", html_text)
            if m:
                notes.append(f"Directory header says {m.group(1)} communities listed.")
        if not new:
            break
        directory.extend(new)
        page += 1
        time.sleep(delay)

    extra = []
    for path in ("/", "/about"):
        html_text = s.get(base + path, timeout=30).text
        m = re.search(r"serve\s+(\d+)\s+communities", html_text)
        if m:
            notes.append(f"Home page says Bellhaven serves {m.group(1)} communities.")
        for slug in SLUG_RE.findall(html_text):
            if slug not in directory and slug not in extra:
                extra.append(slug)
                notes.append(f"'{slug}' is linked from {path} but missing from the directory.")

    locations = []
    for slug, via in [(x, "directory") for x in directory] + [(x, "home/about link") for x in extra]:
        r = s.get(f"{base}/communities/{slug}", timeout=30)
        if r.status_code != 200:
            notes.append(f"Could not read /communities/{slug} (HTTP {r.status_code}).")
            continue
        locations.append(parse_detail(slug, r.text, via))
        time.sleep(delay)
    notes.append(f"Scraped {len(locations)} community pages.")
    return locations, notes
