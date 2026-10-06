#!/usr/bin/env python3
"""City Passport catalog tool.

Usage:
  scripts/catalog_tool.py validate [path]   Validate a catalog JSON file (default: Catalog/catalog.source.json)
  scripts/catalog_tool.py build             Validate the source, copy it into the app bundle, and write a
                                            publishable snapshot + manifest to Catalog/dist/

The app bundles CityPassport/Resources/catalog.json for offline first launch. A snapshot in Catalog/dist/
can be uploaded to any static HTTPS host; set CatalogEndpointURL in project.yml (Info.plist) to its URL to
let the app refresh from it. The app applies the same validation rules before accepting a refresh.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "Catalog", "catalog.source.json")
BUNDLED = os.path.join(ROOT, "CityPassport", "Resources", "catalog.json")
DIST = os.path.join(ROOT, "Catalog", "dist")

BOROUGHS = {"manhattan", "brooklyn", "queens", "bronx", "staten-island"}
INTERESTS = {"food", "museums", "outdoors", "culture", "cafes", "architecture", "hiddenGems"}
PRICING = {"free", "conditionalFree", "ticketed", "suggestedDonation", "variableSpend", "unknown"}
HOURS = {"alwaysOpen", "weekly", "unknown"}
RESERVATION = {"notRequired", "recommended", "required", "unknown"}
EVENT_STATUS = {"scheduled", "cancelled", "soldOut", "postponed"}
DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Generous NYC bounding box.
LAT = (40.49, 40.92)
LNG = (-74.27, -73.68)


def validate(cat):
    errors = []

    def err(msg):
        errors.append(msg)

    if cat.get("schemaVersion") != 1:
        err("schemaVersion must be 1")
    if not cat.get("contentVersion"):
        err("contentVersion is required")
    nb_ids = set()
    for n in cat.get("neighborhoods", []):
        if not ID_RE.match(n.get("id", "")):
            err(f"neighborhood id invalid: {n.get('id')}")
        if n["id"] in nb_ids:
            err(f"duplicate neighborhood id {n['id']}")
        nb_ids.add(n["id"])
        if n.get("borough") not in BOROUGHS:
            err(f"{n['id']}: bad borough")
    place_ids = set()
    for p in cat.get("places", []):
        pid = p.get("id", "")
        where = f"place {pid}"
        if not ID_RE.match(pid):
            err(f"{where}: id must be kebab-case")
        if pid in place_ids:
            err(f"{where}: duplicate id")
        place_ids.add(pid)
        for f in ("name", "address", "summary", "sourceURL"):
            if not p.get(f):
                err(f"{where}: {f} required")
        if not str(p.get("sourceURL", "")).startswith("https://"):
            err(f"{where}: sourceURL must be https")
        if p.get("borough") not in BOROUGHS:
            err(f"{where}: bad borough")
        if p.get("neighborhoodID") not in nb_ids:
            err(f"{where}: unknown neighborhoodID {p.get('neighborhoodID')}")
        c = p.get("coordinate", {})
        if not (LAT[0] <= c.get("lat", 0) <= LAT[1] and LNG[0] <= c.get("lng", 0) <= LNG[1]):
            err(f"{where}: coordinate outside NYC")
        if not p.get("categories") or not set(p["categories"]) <= INTERESTS:
            err(f"{where}: categories must be a non-empty subset of {sorted(INTERESTS)}")
        v = p.get("visitDuration", {})
        if not (0 < v.get("minMinutes", 0) <= v.get("maxMinutes", 0)):
            err(f"{where}: visitDuration invalid")
        pr = p.get("pricing", {})
        if pr.get("kind") not in PRICING:
            err(f"{where}: pricing.kind invalid")
        if pr.get("kind") == "free" and (pr.get("maxUSD") not in (0, None)):
            err(f"{where}: free pricing cannot have a max price")
        h = p.get("hours", {})
        if h.get("kind") not in HOURS:
            err(f"{where}: hours.kind invalid")
        if h.get("kind") == "weekly":
            for day, ranges in (h.get("weekly") or {}).items():
                if day not in DAYS:
                    err(f"{where}: bad weekday {day}")
                for r in ranges:
                    if not (TIME_RE.match(r.get("open", "")) and TIME_RE.match(r.get("close", ""))):
                        err(f"{where}: bad time range {r}")
        if p.get("reservation") not in RESERVATION:
            err(f"{where}: reservation invalid")
        ver = p.get("verification", {})
        if ver.get("status") not in {"verified", "unverified", "sourced"}:
            err(f"{where}: verification.status invalid")
        if ver.get("status") == "verified" and not ver.get("checkedOn"):
            err(f"{where}: verified records need checkedOn")
        img = p.get("image")
        if img and not (img.get("url", "").startswith("https://") and img.get("attribution") and img.get("license")):
            err(f"{where}: images need https url, attribution and license")
    for e in cat.get("events", []):
        where = f"event {e.get('id')}"
        for f in ("id", "seriesID", "title", "start", "end", "timeZone", "sourceURL"):
            if not e.get(f):
                err(f"{where}: {f} required")
        if e.get("timeZone") != "America/New_York":
            err(f"{where}: timeZone must be America/New_York")
        for f in ("start", "end"):
            try:
                d = datetime.datetime.fromisoformat(e[f])
                if d.tzinfo is None:
                    err(f"{where}: {f} needs a UTC offset")
            except Exception:
                err(f"{where}: {f} must be ISO 8601")
        if e.get("status") not in EVENT_STATUS:
            err(f"{where}: status invalid")
        if e.get("placeID") and e["placeID"] not in place_ids:
            err(f"{where}: unknown placeID")
        c = e.get("coordinate", {})
        if not (LAT[0] <= c.get("lat", 0) <= LAT[1] and LNG[0] <= c.get("lng", 0) <= LNG[1]):
            err(f"{where}: coordinate outside NYC")
        ev = e.get("verification", {})
        if ev.get("status") not in {"verified", "sourced"} or not ev.get("checkedOn"):
            err(f"{where}: events need verified/sourced verification with checkedOn")
        if ev.get("status") == "sourced" and not ev.get("source"):
            err(f"{where}: sourced events must name their source")
        if not str(e.get("sourceURL", "")).startswith("https://"):
            err(f"{where}: sourceURL must be https")
    for c in cat.get("collections", []):
        rule = c.get("rule", {})
        if rule.get("type") == "places":
            missing = [i for i in rule.get("placeIDs", []) if i not in place_ids]
            if missing:
                err(f"collection {c.get('id')}: unknown places {missing}")
            if not 0 < rule.get("required", 0) <= len(rule.get("placeIDs", [])):
                err(f"collection {c.get('id')}: required out of range")
        elif rule.get("type") == "boroughs":
            if not 0 < rule.get("required", 0) <= 5:
                err(f"collection {c.get('id')}: required out of range")
        else:
            err(f"collection {c.get('id')}: unknown rule type")
    return errors


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "validate"
    if cmd == "validate":
        path = sys.argv[2] if len(sys.argv) > 2 else SOURCE
        errors = validate(load(path))
        for e in errors:
            print("ERROR", e)
        cat = load(path)
        print(f"{len(cat['places'])} places, {len(cat['neighborhoods'])} neighborhoods, "
              f"{len(cat['events'])} events, {len(cat['collections'])} collections — "
              f"{'INVALID' if errors else 'valid'}")
        sys.exit(1 if errors else 0)
    if cmd == "build":
        cat = load(SOURCE)
        errors = validate(cat)
        if errors:
            for e in errors:
                print("ERROR", e)
            sys.exit(1)
        os.makedirs(os.path.dirname(BUNDLED), exist_ok=True)
        shutil.copyfile(SOURCE, BUNDLED)
        os.makedirs(DIST, exist_ok=True)
        data = open(SOURCE, "rb").read()
        name = f"catalog-{cat['contentVersion']}.json"
        open(os.path.join(DIST, name), "wb").write(data)
        manifest = {
            "contentVersion": cat["contentVersion"],
            "schemaVersion": cat["schemaVersion"],
            "file": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        json.dump(manifest, open(os.path.join(DIST, "manifest.json"), "w"), indent=2)
        print(f"Bundled {BUNDLED}\nSnapshot {os.path.join(DIST, name)} sha256={manifest['sha256'][:12]}…")
        return
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
