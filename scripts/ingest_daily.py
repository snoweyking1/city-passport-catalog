#!/usr/bin/env python3
"""Daily City Passport catalog build.

Merges the curated place catalog (Catalog/catalog.source.json) with fresh event occurrences from:
  * NYC Parks public events RSS (official, next 14 days)
  * NYC for FREE events listing (https://www.nycforfree.co/events), facts only
  * NYC Permitted Events (NYC Open Data tvpp-9vvx): street fairs, parades, flea markets, Greenmarkets,
    plaza events. "A between B and C" is placed at the A/B intersection using the city's street
    centerline (NYC Open Data inkn-q76z), cached in geocache.json.
  * Ticketmaster Discovery API (official) when TICKETMASTER_API_KEY is set

and writes a validated snapshot the app downloads:
  Catalog/dist/catalog.json   (published at CatalogEndpointURL)
  Catalog/dist/manifest.json  (version, hash, per-source counts and errors)

Usage:
  scripts/ingest_daily.py              build the snapshot
  scripts/ingest_daily.py --bundle     also copy it into the app bundle (CityPassport/Resources/catalog.json)
  scripts/ingest_daily.py --offline    rebuild from the previous snapshot's events without fetching

Content policy (enforced here, not just documented):
  * Only facts are kept from third-party listings: title, date, time, place, coordinates, category, link.
    Descriptions and images are never copied; each event links back to its source page.
  * Events are marked "sourced" (listed by a named source on a date), never "verified".
  * Anything ambiguous is skipped rather than guessed: listings without concrete dates/times,
    placeholder "all of NYC" coordinates, and past occurrences.
  * One polite request per source per run, with an identifying User-Agent.
  * If a source fails, its still-upcoming events from the previous snapshot are kept and the failure
    is recorded in the manifest; a run never publishes a catalog that fails validation.
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from catalog_tool import validate  # noqa: E402

NY = ZoneInfo("America/New_York")
SOURCE = os.path.join(ROOT, "Catalog", "catalog.source.json")
DIST = os.path.join(ROOT, "Catalog", "dist")
OUT = os.path.join(DIST, "catalog.json")
BUNDLED = os.path.join(ROOT, "CityPassport", "Resources", "catalog.json")
UA = "CityPassportCatalogBot/1.0 (daily, one request per source; +https://github.com/snoweyking1/city-passport-catalog)"
WINDOW_DAYS = 14

PARKS_URL = "https://www.nycgovparks.org/xml/events_300_rss.xml"
# Same NYC Parks feed via NYC Open Data; reachable from cloud runners (the RSS host blocks them).
PARKS_OPEN_DATA_URL = "https://data.cityofnewyork.us/resource/w3wp-dpdi.json?$limit=5000"
NYCFF_URL = "https://www.nycforfree.co/events"
NYC_CENTROID = (40.7127753, -74.0059728)  # placeholder used for "multiple locations"


def fetch(url, encoding="utf-8"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode(encoding, errors="replace")


def iso(d):
    return d.isoformat(timespec="seconds")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "event"


def parse_clock(s):
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", s.strip().lower())
    if not m:
        return None
    h, mi = int(m.group(1)) % 12, int(m.group(2))
    if m.group(3) == "p":
        h += 12
    return dt.time(h, mi)


def free_pricing(source_name, note):
    return {"kind": "free", "freeByNature": False, "minUSD": 0, "maxUSD": 0, "note": note}


def unknown_pricing(note):
    return {"kind": "unknown", "freeByNature": False, "minUSD": None, "maxUSD": None, "note": note}


def make_event(eid, series, title, start, end, lat, lng, venue, cats, summary, pricing, url, source, checked):
    return {
        "id": eid, "seriesID": series, "title": title, "placeID": None, "venueName": venue,
        "coordinate": {"lat": round(lat, 6), "lng": round(lng, 6)},
        "start": iso(start), "end": iso(end), "timeZone": "America/New_York", "status": "scheduled",
        "categories": cats, "summary": summary, "pricing": pricing, "reservation": "unknown",
        "sourceURL": url,
        "verification": {"status": "sourced", "checkedOn": checked, "note": None, "source": source},
    }


# --- NYC Parks -------------------------------------------------------------------------------

PARKS_SKIP = {"Recreation Center Programming", "Shape Up NYC", "Sports Camps", "Exercise Classes",
              "Seniors", "Yoga & Pilates Classes", "Dance Classes", "Basketball/Netball"}
PARKS_MAP = [
    ({"Art", "Arts & Crafts", "Film", "Movies Under the Stars", "Free Summer Movies", "Concerts", "Dance",
      "Talks", "History", "Festivals", "Fall Festivals", "Halloween", "Open House New York", "Education",
      "Workshops"}, "culture"),
    ({"Nature", "Wildlife", "Tours", "Walking", "Gardening", "Waterfront", "Urban Park Rangers",
      "Running/Jogging", "Outdoor Fitness", "Sports"}, "outdoors"),
    ({"Food"}, "food"),
]


def parks_event(raw, now, checked):
    """Normalize one NYC Parks event (fields: title, guid, link, categories, coordinates, start, end,
    location, parknames, description). Returns an event dict or None to skip."""
    cats = [c.strip() for c in raw["categories"].split("|") if c.strip()]
    title = re.sub(r"\s+", " ", html.unescape(raw["title"])).strip()
    # Recurring classes and member programs aren't "things to do today" for a visitor.
    if not cats or set(cats) & PARKS_SKIP or set(cats) <= {"Fitness", "Best for Kids", "Games"} \
            or "open call" in title.lower():
        return None
    try:
        lat, lng = [float(x) for x in raw["coordinates"].split(",")[:2]]
    except Exception:
        return None
    start, end = raw["start"], raw["end"]
    if not (start and end) or end <= start or end <= now or start.date() != end.date():
        return None
    if not (40.49 <= lat <= 40.92 and -74.27 <= lng <= -73.68):
        return None
    venue = html.unescape(raw.get("location") or raw.get("parknames") or "NYC park")
    if re.search(r"virtual|online|zoom", venue, flags=re.I):
        return None  # not a place you can go
    interests = []
    for group, interest in PARKS_MAP:
        if group & set(cats) and interest not in interests:
            interests.append(interest)
    fee = re.search(r"\$\s?\d", raw.get("description") or "")
    pricing = unknown_pricing("NYC Parks mentions a fee for this event; check the listing.") if fee else \
        free_pricing("NYC Parks", "NYC Parks public event; no fee listed. Some need registration.")
    url = (raw.get("link") or "https://www.nycgovparks.org/events").replace("http://", "https://")
    return make_event(f"nycparks-{raw['guid']}", f"nycparks-{slug(title)}", title, start, end, lat, lng, venue,
                      interests or ["outdoors"], f"Public NYC Parks event at {venue}. " + ", ".join(cats[:3]) + ".",
                      pricing, url, "NYC Parks", checked)


def parks_open_data_events(text, now, checked):
    out, skipped = [], 0
    for r in json.loads(text):
        try:
            raw = {"title": r.get("title", ""), "guid": r["guid"], "link": (r.get("link") or {}).get("url"),
                   "categories": r.get("categories", ""), "coordinates": r.get("coordinates", ""),
                   "start": dt.datetime.fromisoformat(r["starttime"]).replace(tzinfo=NY),
                   "end": dt.datetime.fromisoformat(r["endtime"]).replace(tzinfo=NY),
                   "location": r.get("location"), "parknames": r.get("parknames"), "description": r.get("description")}
        except Exception:
            skipped += 1
            continue
        e = parks_event(raw, now, checked)
        if e:
            out.append(e)
        else:
            skipped += 1
    return out, skipped


def parks_events(text, now, checked):
    """NYC Parks RSS (fallback when Open Data is unavailable)."""
    out, skipped = [], 0
    for it in re.findall(r"<item>(.*?)</item>", text, flags=re.S):
        def tag(name):
            m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (name, name), it, flags=re.S)
            return html.unescape(m.group(1)).strip() if m else ""
        try:
            d0 = dt.date.fromisoformat(tag("event:startdate"))
            d1 = dt.date.fromisoformat(tag("event:enddate") or tag("event:startdate"))
            t0, t1 = parse_clock(tag("event:starttime")), parse_clock(tag("event:endtime"))
            raw = {"title": tag("title"), "guid": tag("guid"), "link": tag("link"), "categories": tag("event:categories"),
                   "coordinates": tag("event:coordinates"),
                   "start": dt.datetime.combine(d0, t0, NY) if t0 else None,
                   "end": dt.datetime.combine(d1, t1, NY) if t1 else None,
                   "location": tag("event:location"), "parknames": tag("event:parknames"), "description": tag("description")}
        except Exception:
            skipped += 1
            continue
        e = parks_event(raw, now, checked)
        if e:
            out.append(e)
        else:
            skipped += 1
    return out, skipped


# --- NYC for FREE ------------------------------------------------------------------------------

NYCFF_MAP = {"Food": "food", "Drink": "food", "TV/Movies": "culture", "Music": "culture",
             "Performance/Dance": "culture", "Community": "culture", "Holiday": "culture",
             "Halloween": "culture", "Kids": "culture", "Sports/Fitness": "outdoors", "Travel": "outdoors"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def nycff_events(text, now, checked):
    out, skipped = [], 0
    rows = [dict((k, html.unescape(v)) for k, v in re.findall(r'([a-z-]+)="([^"]*)"', s))
            for s in re.findall(r"<div ([^>]*slug=\"[^\"]+\"[^>]*)>", text)]
    horizon = (now + dt.timedelta(days=WINDOW_DAYS)).date()
    for r in rows:
        try:
            title = re.sub(r"\s+", " ", r["title"]).strip()
            d0 = dt.datetime.strptime(r["start-date"], "%B %d, %Y").date()
            d1 = dt.datetime.strptime(r.get("end-date") or r["start-date"], "%B %d, %Y").date()
            t0, t1 = parse_clock(r.get("start-time", "")), parse_clock(r.get("end-time", ""))
            lat, lng = float(r["lat"]), float(r["long"])
        except Exception:
            skipped += 1
            continue
        freq = r.get("frequency", "")
        days = [d.strip()[:3] for d in r.get("recurring-days", "").split(",") if d.strip()]
        placeholder = abs(lat - NYC_CENTROID[0]) < 1e-4 and abs(lng - NYC_CENTROID[1]) < 1e-4
        if not (t0 and t1) or placeholder or r.get("borough") == "N/A" or (freq == "Multiple Dates" and not days):
            skipped += 1  # no concrete place or no concrete days: don't guess
            continue
        if not (40.49 <= lat <= 40.92 and -74.27 <= lng <= -73.68):
            skipped += 1
            continue
        address = r.get("address", "").replace(", USA", "")
        cat = r.get("category", "Other")
        interest = NYCFF_MAP.get(cat, "hiddenGems")
        url = f"https://www.nycforfree.co/events/{r['slug']}"
        day = max(d0, now.date())
        while day <= min(d1, horizon):
            if not days or DAYS[day.weekday()] in days:
                start = dt.datetime.combine(day, t0, NY)
                end = dt.datetime.combine(day, t1, NY)
                if end <= start:
                    end += dt.timedelta(days=1)
                if end > now:
                    out.append(make_event(
                        f"nycff-{r['slug']}-{day:%Y%m%d}", f"nycff-{r['slug']}", title, start, end, lat, lng,
                        address.split(",")[0] or "See listing", [interest],
                        f"{cat} listing from NYC for FREE" + (f", running through {d1:%b %-d}" if d1 > d0 else "") + ".",
                        free_pricing("NYC for FREE", "Listed as free by NYC for FREE. Some need an RSVP or have conditions — check the listing."),
                        url, "NYC for FREE", checked))
            day += dt.timedelta(days=1)
    return out, skipped



# --- Geocoding street intersections (NYC street centerline on Open Data, cached) -----------------

GEOCACHE = os.path.join(DIST, "geocache.json")
CENTERLINE = "https://data.cityofnewyork.us/resource/inkn-q76z.json"
BORO_CODE = {"manhattan": "1", "bronx": "2", "brooklyn": "3", "queens": "4", "staten island": "5"}
ABBR = [("NORTH", "N"), ("SOUTH", "S"), ("EAST", "E"), ("WEST", "W"), ("STREET", "ST"), ("AVENUE", "AVE"),
        ("BOULEVARD", "BLVD"), ("ROAD", "RD"), ("PLACE", "PL"), ("DRIVE", "DR"), ("PARKWAY", "PKWY"),
        ("LANE", "LN"), ("TERRACE", "TER"), ("COURT", "CT"), ("SAINT", "ST"), ("EXPRESSWAY", "EXPY")]


def cscl_name(s):
    """'WEST   41 STREET' -> 'W 41 ST' (city centerline spelling, single-spaced)."""
    words = re.sub(r"[^A-Z0-9 ]", " ", s.upper()).split()
    words = [dict(ABBR).get(w, w) for w in words]
    words = [re.sub(r"^(\d+)(ST|ND|RD|TH)$", r"\1", w) for w in words]  # 41ST -> 41
    return " ".join(words)


def osm_street_name(s):
    """Human-readable form for labels: 'WEST   41 STREET' -> 'West 41st Street'."""
    s = re.sub(r"\s+", " ", s).strip().title()
    def ordinal(m):
        n = int(m.group(1))
        suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suf}{m.group(2)}"
    return re.sub(r"\b(\d+)( (?:Street|Avenue|Road|Place|Drive|Terrace|Lane))\b", ordinal, s)


class Geocoder:
    """Finds where two named streets meet using the city's street centerline geometry."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.cache = {}
        if os.path.exists(GEOCACHE):
            try:
                self.cache = json.load(open(GEOCACHE))
            except Exception:
                self.cache = {}
        self.streets = {}
        self.lookups = 0
        self.max_lookups = 300      # street fetches per run
        self.failures = 0           # circuit breaker: stop after repeated service errors

    def street_points(self, name, boro):
        key = f"{boro}|{name}"
        if key in self.streets:
            return self.streets[key]
        if not self.enabled or self.lookups >= self.max_lookups or self.failures >= 3:
            return None
        self.lookups += 1
        import urllib.parse
        variants = {name, re.sub(r"^([NSEW]) ", r"\1  ", name)}  # dataset sometimes double-spaces after a direction
        where = f"boroughcode='{boro}' AND full_street_name in(" + ",".join("'" + v.replace("'", "''") + "'" for v in variants) + ")"
        url = CENTERLINE + "?" + urllib.parse.urlencode({"$select": "the_geom", "$where": where, "$limit": "2000"})
        try:
            rows = json.loads(fetch(url))
        except Exception:
            self.failures += 1
            return None
        pts = []
        for r in rows:
            g = r.get("the_geom") or {}
            lines = g.get("coordinates", [])
            if g.get("type") == "LineString":
                lines = [lines]
            for line in lines:
                pts.extend((c[1], c[0]) for c in line)
        self.streets[key] = pts
        return pts

    def intersection(self, a, b, borough):
        a, b = cscl_name(a), cscl_name(b)
        boro = BORO_CODE.get(borough)
        key = f"{borough}|{min(a, b)}|{max(a, b)}"
        if key in self.cache:
            return self.cache[key]
        if not boro:
            return None
        pa, pb = self.street_points(a, boro), self.street_points(b, boro)
        if pa is None or pb is None:
            return None  # service unavailable: don't cache, try again tomorrow
        best = None
        # Grid-bucket street B's vertices so the nearest-vertex search stays fast.
        grid = {}
        for p in pb:
            grid.setdefault((round(p[0], 3), round(p[1], 3)), []).append(p)
        for p in pa:
            for dy in (-0.001, 0, 0.001):
                for dx in (-0.001, 0, 0.001):
                    for q in grid.get((round(p[0] + dy, 3), round(p[1] + dx, 3)), []):
                        d = ((p[0] - q[0]) * 111_000) ** 2 + ((p[1] - q[1]) * 84_000) ** 2
                        if best is None or d < best[0]:
                            best = (d, p)
        val = [round(best[1][0], 6), round(best[1][1], 6)] if best and best[0] < 30 ** 2 else None
        self.cache[key] = val  # cache misses too (streets that don't meet)
        return val

    def save(self):
        os.makedirs(DIST, exist_ok=True)
        json.dump(self.cache, open(GEOCACHE, "w"), separators=(",", ":"))


GEO = Geocoder()

# --- NYC Permitted Events (Open Data) ------------------------------------------------------------

PERMITTED_URL = "https://data.cityofnewyork.us/resource/tvpp-9vvx.json"
PERMITTED_TYPES = {
    "Single Block Festival": ("culture", "Street fair"),
    "Street Festival": ("culture", "Street fair"),
    "Parade": ("culture", "Parade"),
    "Sidewalk Sale": ("hiddenGems", "Sidewalk sale / flea market"),
    "Farmers Market": ("food", "Farmers market"),
    "Plaza Event": ("culture", "Plaza event"),
    "Athletic Race / Tour": ("outdoors", "Race (spectators welcome)"),
}


def permitted_url(now):
    s = now.strftime("%Y-%m-%dT00:00:00")
    e = (now + dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT00:00:00")
    types = ",".join("'" + t.replace("'", "''") + "'" for t in PERMITTED_TYPES)
    import urllib.parse
    where = f"start_date_time between '{s}' and '{e}' AND event_type in({types})"
    return PERMITTED_URL + "?" + urllib.parse.urlencode({"$limit": "5000", "$where": where})


def permitted_location(loc, borough):
    """Return (lat, lng, label) for 'A between B and C' or a named plaza, else None."""
    first = re.split(r",(?![^()]*\))", loc)[0].strip()
    first = first.split(":")[-1].strip()  # drop "Avenue C Plaza:" style prefixes
    m = re.match(r"([A-Z0-9 .'-]+?)\s+between\s+([A-Z0-9 .'-]+?)\s+and\s+([A-Z0-9 .'-]+)$", first, flags=re.I)
    if m:
        street_raw = re.sub(r"^\d+\s+(?=\d+\s|[A-Z])", "", m.group(1).strip()) if re.match(r"^\d+\s+\d+\s", m.group(1).strip()) else m.group(1).strip()
        cross_raw = m.group(2).strip()
        pt = GEO.intersection(street_raw, cross_raw, borough)
        if pt:
            return pt[0], pt[1], f"{osm_street_name(street_raw)} at {osm_street_name(cross_raw)}"
    return None


def permitted_events(text, now, checked):
    out, skipped = [], 0
    for r in json.loads(text):
        etype = r.get("event_type", "")
        if etype not in PERMITTED_TYPES:
            skipped += 1
            continue
        try:
            start = dt.datetime.fromisoformat(r["start_date_time"]).replace(tzinfo=NY)
            end = dt.datetime.fromisoformat(r["end_date_time"]).replace(tzinfo=NY)
        except Exception:
            skipped += 1
            continue
        if end <= now or end <= start or (end - start) > dt.timedelta(hours=16):
            skipped += 1
            continue
        borough = (r.get("event_borough") or "").lower()
        loc = permitted_location(r.get("event_location", ""), borough)
        if not loc:
            skipped += 1  # can't place it on a map precisely: skip rather than guess
            continue
        lat, lng, label = loc
        if not (40.49 <= lat <= 40.92 and -74.27 <= lng <= -73.68):
            skipped += 1
            continue
        interest, kind = PERMITTED_TYPES[etype]
        title = re.sub(r"\s+", " ", r.get("event_name", "")).strip().title() if r.get("event_name", "").isupper() \
            else re.sub(r"\s+", " ", r.get("event_name", "")).strip()
        if not title or title.lower() in {"celebration", "event"}:
            skipped += 1
            continue
        out.append(make_event(
            f"nycpermit-{r['event_id']}-{start:%Y%m%d}", f"nycpermit-{slug(title)}", title, start, end, lat, lng,
            label, [interest], f"{kind} with a city permit, {r.get('event_borough', '')}. Permit hours can include setup time.",
            unknown_pricing("Free to walk through; vendors and entry fees vary."),
            "https://data.cityofnewyork.us/City-Government/NYC-Permitted-Event-Information/tvpp-9vvx",
            "NYC permitted events", checked))
    return out, skipped


# --- Ticketmaster Discovery API (official; needs TICKETMASTER_API_KEY) --------------------------

def ticketmaster_url(now):
    key = os.environ.get("TICKETMASTER_API_KEY", "").strip()
    if not key:
        return None
    import urllib.parse
    params = {"apikey": key, "latlong": "40.7128,-74.0060", "radius": "15", "unit": "miles", "size": "200",
              "sort": "date,asc", "locale": "*",
              "startDateTime": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "endDateTime": (now + dt.timedelta(days=WINDOW_DAYS)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return "https://app.ticketmaster.com/discovery/v2/events.json?" + urllib.parse.urlencode(params)


TM_MAP = {"Music": "culture", "Arts & Theatre": "culture", "Film": "culture", "Sports": "outdoors",
          "Miscellaneous": "hiddenGems", "Family": "culture"}


def ticketmaster_events(text, now, checked):
    out, skipped = [], 0
    for e in (json.loads(text).get("_embedded") or {}).get("events", []):
        try:
            st = e["dates"]["start"]
            if st.get("dateTBD") or st.get("timeTBA") or "dateTime" not in st:
                raise ValueError("no time")
            start = dt.datetime.fromisoformat(st["dateTime"].replace("Z", "+00:00")).astimezone(NY)
            v = e["_embedded"]["venues"][0]
            lat, lng = float(v["location"]["latitude"]), float(v["location"]["longitude"])
        except Exception:
            skipped += 1
            continue
        status = (e["dates"].get("status") or {}).get("code", "onsale")
        if status in ("cancelled", "postponed", "rescheduled"):
            skipped += 1
            continue
        # Discovery gives no end time; use a conservative 2-hour block and say so.
        end = start + dt.timedelta(hours=2)
        if end <= now or not (40.49 <= lat <= 40.92 and -74.27 <= lng <= -73.68):
            skipped += 1
            continue
        seg = ((e.get("classifications") or [{}])[0].get("segment") or {}).get("name", "")
        pr = (e.get("priceRanges") or [{}])[0]
        pricing = {"kind": "ticketed", "freeByNature": False,
                   "minUSD": pr.get("min"), "maxUSD": pr.get("max"),
                   "note": "Ticket prices from Ticketmaster; fees may apply." if pr else "Ticketed; see Ticketmaster for prices."}
        ev = make_event(f"tm-{e['id']}", f"tm-{slug(e['name'])}", e["name"], start, end, lat, lng, v.get("name", "Venue"),
                        [TM_MAP.get(seg, "culture")], f"{seg or 'Event'} at {v.get('name', 'the venue')}. End time is an estimate.",
                        pricing, e.get("url", "https://www.ticketmaster.com"), "Ticketmaster", checked)
        if status == "offsale":
            ev["status"] = "soldOut"
        ev["reservation"] = "required"
        out.append(ev)
    return out, skipped


SOURCES = [
    # (id prefix, display name, [(url, encoding, parser), ...fallbacks])
    ("nycparks", "NYC Parks", [(PARKS_OPEN_DATA_URL, "utf-8", parks_open_data_events),
                               (PARKS_URL, "iso-8859-1", parks_events)]),
    ("nycff", "NYC for FREE", [(NYCFF_URL, "utf-8", nycff_events)]),
    ("nycpermit", "NYC permitted events", [(permitted_url, "utf-8", permitted_events)]),
    ("tm", "Ticketmaster", [(ticketmaster_url, "utf-8", ticketmaster_events)]),
]


def main():
    args = set(sys.argv[1:])
    now = dt.datetime.now(NY)
    GEO.enabled = "--offline" not in args
    checked = now.date().isoformat()
    cat = json.load(open(SOURCE, encoding="utf-8"))
    previous = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {"events": []}

    events, report = [], {}
    for key, name, endpoints in SOURCES:
        prior = [e for e in previous.get("events", []) if e["id"].startswith(key + "-")
                 and dt.datetime.fromisoformat(e["end"]) > now]
        if "--offline" in args:
            events += prior
            report[name] = {"status": "offline", "events": len(prior)}
            continue
        errors = []
        endpoints = [(u(now) if callable(u) else u, enc, p) for u, enc, p in endpoints]
        if all(u is None for u, _, _ in endpoints):
            report[name] = {"status": "not configured"}
            continue
        for url, enc, parser in endpoints:
            try:
                got, skipped = parser(fetch(url, enc), now, checked)
                if not got:
                    raise ValueError("no usable events")
                events += got
                report[name] = {"status": "ok", "events": len(got), "skipped": skipped, "url": url.split("?")[0]}
                if errors:
                    report[name]["fallbackFrom"] = errors
                break
            except Exception as e:
                errors.append(f"{url.split('?')[0]}: {re.sub(r'apikey=[^&]+', 'apikey=***', str(e))[:160]}")
        else:  # every endpoint failed: keep yesterday's still-upcoming events for this source
            events += prior
            report[name] = {"status": "failed", "errors": errors, "keptFromPrevious": len(prior)}

    # De-duplicate the same thing listed twice (same title, same start).
    seen, unique = set(), []
    for e in sorted(events, key=lambda e: (e["start"], e["id"])):
        k = (re.sub(r"[^a-z0-9]", "", e["title"].lower()), e["start"])
        if k not in seen:
            seen.add(k)
            unique.append(e)

    cat["events"] = unique
    cat["contentVersion"] = now.strftime("%Y.%m.%d-%H%M")
    cat["generatedAt"] = iso(now.replace(microsecond=0))
    cat["provenance"] = (cat.get("provenance", "").split(" Events:")[0] +
                         f" Events: refreshed {now:%b %-d, %Y} from " +
                         ", ".join(f"{n} ({r.get('events', r.get('keptFromPrevious', 0))})" for n, r in report.items()) +
                         ". Event facts only; descriptions stay with the source.")

    errors = validate(cat)
    if errors:
        for e in errors[:20]:
            print("ERROR", e)
        print("Validation failed; nothing published.")
        sys.exit(1)

    os.makedirs(DIST, exist_ok=True)
    data = json.dumps(cat, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(OUT, "wb") as f:
        f.write(data)
    manifest = {"contentVersion": cat["contentVersion"], "schemaVersion": cat["schemaVersion"], "file": "catalog.json",
                "sha256": hashlib.sha256(data).hexdigest(), "builtAt": iso(now.replace(microsecond=0)),
                "places": len(cat["places"]), "events": len(unique), "sources": report}
    json.dump(manifest, open(os.path.join(DIST, "manifest.json"), "w"), indent=2)
    GEO.save()
    manifest["geocoder"] = {"cached": len(GEO.cache), "lookupsThisRun": GEO.lookups}
    json.dump(manifest, open(os.path.join(DIST, "manifest.json"), "w"), indent=2)
    if "--bundle" in args:
        with open(BUNDLED, "wb") as f:
            f.write(data)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
