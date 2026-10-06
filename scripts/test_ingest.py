#!/usr/bin/env python3
"""Tests for the daily ingester's parsing rules. Run: python3 scripts/test_ingest.py"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_daily as ig  # noqa: E402

NOW = dt.datetime(2026, 10, 6, 13, 0, tzinfo=ig.NY)


def nycff_row(**kw):
    a = {"slug": "pop-up", "title": "Free Pop-Up", "start-date": "October 6, 2026", "end-date": "October 6, 2026",
         "start-time": "2:00 PM", "end-time": "5:00 PM", "frequency": "One-time", "recurring-days": "",
         "borough": "Manhattan", "address": "1 Main St, New York, NY 10001, USA", "lat": "40.75", "long": "-73.99",
         "category": "Food", "description": "SECRET COPY TEXT"}
    a.update(kw)
    return "<div " + " ".join(f'{k}="{v}"' for k, v in a.items()) + "></div>"


def parks_item(**kw):
    a = {"title": "Bird Walk", "guid": "123", "link": "http://www.nycgovparks.org/events/x",
         "event:categories": "Nature | Tours", "event:coordinates": "40.70, -73.95", "event:startdate": "2026-10-06",
         "event:enddate": "2026-10-06", "event:starttime": "3:00 pm", "event:endtime": "4:00 pm",
         "event:location": "Prospect Park", "description": "Join us."}
    a.update(kw)
    return "<item>" + "".join(f"<{k}><![CDATA[{v}]]></{k}>" for k, v in a.items()) + "</item>"


class IngestTests(unittest.TestCase):
    def test_nycff_one_time_facts_only(self):
        ev, skipped = ig.nycff_events(nycff_row(), NOW, "2026-10-06")
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual(e["start"], "2026-10-06T14:00:00-04:00")
        self.assertEqual(e["verification"]["status"], "sourced")
        self.assertEqual(e["verification"]["source"], "NYC for FREE")
        self.assertNotIn("SECRET", repr(e), "third-party descriptions must never be copied")
        self.assertTrue(e["sourceURL"].startswith("https://www.nycforfree.co/events/"))

    def test_nycff_skips_ambiguous_and_placeholder(self):
        _, s1 = ig.nycff_events(nycff_row(frequency="Multiple Dates", **{"end-date": "October 9, 2026"}), NOW, "x")
        _, s2 = ig.nycff_events(nycff_row(lat="40.7127753", long="-74.0059728"), NOW, "x")
        _, s3 = ig.nycff_events(nycff_row(**{"start-time": ""}), NOW, "x")
        self.assertEqual((s1, s2, s3), (1, 1, 1))

    def test_nycff_range_and_recurring_days(self):
        ev, _ = ig.nycff_events(nycff_row(**{"end-date": "October 8, 2026"}), NOW, "x")
        self.assertEqual([e["start"][:10] for e in ev], ["2026-10-06", "2026-10-07", "2026-10-08"])
        ev, _ = ig.nycff_events(nycff_row(frequency="Recurrent", **{"recurring-days": "Thu", "end-date": "October 20, 2026"}), NOW, "x")
        self.assertEqual([e["start"][:10] for e in ev], ["2026-10-08", "2026-10-15"])

    def test_nycff_past_dropped(self):
        ev, _ = ig.nycff_events(nycff_row(**{"start-time": "9:00 AM", "end-time": "10:00 AM"}), NOW, "x")
        self.assertEqual(ev, [])

    def test_parks_free_unless_fee_and_skips_classes(self):
        ev, _ = ig.parks_events(parks_item(), NOW, "2026-10-06")
        self.assertEqual(ev[0]["pricing"]["kind"], "free")
        self.assertEqual(ev[0]["sourceURL"][:8], "https://")
        ev, _ = ig.parks_events(parks_item(description="Fee: $10 per person"), NOW, "x")
        self.assertEqual(ev[0]["pricing"]["kind"], "unknown")
        ev, skipped = ig.parks_events(parks_item(**{"event:categories": "Fitness | Shape Up NYC"}), NOW, "x")
        self.assertEqual((ev, skipped), ([], 1))

    def test_parks_open_data(self):
        import json
        rows = [{"title": "Bird Walk", "guid": "9", "link": {"url": "http://www.nycgovparks.org/events/x"},
                 "categories": "Nature | Tours", "coordinates": "40.70, -73.95", "starttime": "2026-10-06T15:00:00.000",
                 "endtime": "2026-10-06T16:00:00.000", "location": "Prospect Park"},
                {"title": "Online Talk", "guid": "10", "categories": "Talks", "coordinates": "40.70, -73.95",
                 "starttime": "2026-10-06T15:00:00.000", "endtime": "2026-10-06T16:00:00.000", "location": "Virtual Event"}]
        ev, skipped = ig.parks_open_data_events(json.dumps(rows), NOW, "2026-10-06")
        self.assertEqual([e["id"] for e in ev], ["nycparks-9"])
        self.assertEqual(ev[0]["start"], "2026-10-06T15:00:00-04:00")
        self.assertEqual(skipped, 1)

    def test_permitted_events_geocoded_and_filtered(self):
        import json
        ig.GEO.enabled = False
        ig.GEO.cache["manhattan|THOMPSON ST|W HOUSTON ST"] = [40.727353, -74.00058]
        rows = [
            {"event_id": "1", "event_name": "ST. ANTHONY FLEA MARKET", "event_type": "Sidewalk Sale",
             "start_date_time": "2026-10-06T14:00:00.000", "end_date_time": "2026-10-06T18:00:00.000",
             "event_borough": "Manhattan", "event_location": "WEST HOUSTON STREET between THOMPSON STREET and MACDOUGAL ST"},
            {"event_id": "2", "event_name": "Celebration", "event_type": "Special Event",
             "start_date_time": "2026-10-06T14:00:00.000", "end_date_time": "2026-10-06T18:00:00.000",
             "event_borough": "Manhattan", "event_location": "Central Park: Summit Rock"},
            {"event_id": "3", "event_name": "Unknown Corner Fair", "event_type": "Single Block Festival",
             "start_date_time": "2026-10-06T14:00:00.000", "end_date_time": "2026-10-06T18:00:00.000",
             "event_borough": "Queens", "event_location": "NOWHERE AVENUE between X STREET and Y STREET"},
        ]
        ev, skipped = ig.permitted_events(json.dumps(rows), NOW, "2026-10-06")
        self.assertEqual([e["id"] for e in ev], ["nycpermit-1-20261006"])
        self.assertEqual(ev[0]["title"], "St. Anthony Flea Market")
        self.assertEqual(ev[0]["coordinate"], {"lat": 40.727353, "lng": -74.00058})
        self.assertEqual(ev[0]["pricing"]["kind"], "unknown")
        self.assertEqual(skipped, 2, "private permits and ungeocodable places are skipped")

    def test_street_names(self):
        self.assertEqual(ig.osm_street_name("WEST   41 STREET"), "West 41st Street")
        self.assertEqual(ig.osm_street_name("EAST 111 STREET"), "East 111th Street")
        self.assertEqual(ig.cscl_name("WEST   41 STREET"), "W 41 ST")
        self.assertEqual(ig.cscl_name("KISSENA BOULEVARD"), "KISSENA BLVD")
        self.assertEqual(ig.cscl_name("MACDOUGAL ST"), "MACDOUGAL ST")
        self.assertEqual(ig.cscl_name("4 AVENUE"), "4 AVE")

    def test_ticketmaster_parsing(self):
        import json
        payload = {"_embedded": {"events": [
            {"id": "A1", "name": "Big Show", "url": "https://www.ticketmaster.com/x",
             "dates": {"start": {"dateTime": "2026-10-06T23:30:00Z"}, "status": {"code": "onsale"}},
             "classifications": [{"segment": {"name": "Music"}}], "priceRanges": [{"min": 45.0, "max": 120.0}],
             "_embedded": {"venues": [{"name": "Madison Square Garden", "location": {"latitude": "40.7505", "longitude": "-73.9934"}}]}},
            {"id": "A2", "name": "Cancelled Thing", "dates": {"start": {"dateTime": "2026-10-07T00:00:00Z"}, "status": {"code": "cancelled"}},
             "_embedded": {"venues": [{"name": "V", "location": {"latitude": "40.75", "longitude": "-73.99"}}]}},
            {"id": "A3", "name": "TBA", "dates": {"start": {"localDate": "2026-10-09", "timeTBA": True}},
             "_embedded": {"venues": [{"name": "V", "location": {"latitude": "40.75", "longitude": "-73.99"}}]}},
        ]}}
        ev, skipped = ig.ticketmaster_events(json.dumps(payload), NOW, "2026-10-06")
        self.assertEqual([e["id"] for e in ev], ["tm-A1"])
        self.assertEqual(ev[0]["start"], "2026-10-06T19:30:00-04:00")
        self.assertEqual(ev[0]["pricing"]["kind"], "ticketed")
        self.assertEqual(ev[0]["pricing"]["minUSD"], 45.0)
        self.assertEqual(ev[0]["verification"]["source"], "Ticketmaster")
        self.assertEqual(skipped, 2)

    def test_ticketmaster_not_configured_without_key(self):
        os.environ.pop("TICKETMASTER_API_KEY", None)
        self.assertIsNone(ig.ticketmaster_url(NOW))

    def test_events_validate(self):
        ev, _ = ig.parks_events(parks_item(), NOW, "2026-10-06")
        cat = {"schemaVersion": 1, "contentVersion": "t", "neighborhoods": [], "places": [], "events": ev, "collections": []}
        self.assertEqual(ig.validate(cat), [])


if __name__ == "__main__":
    unittest.main()
