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

    def test_events_validate(self):
        ev, _ = ig.parks_events(parks_item(), NOW, "2026-10-06")
        cat = {"schemaVersion": 1, "contentVersion": "t", "neighborhoods": [], "places": [], "events": ev, "collections": []}
        self.assertEqual(ig.validate(cat), [])


if __name__ == "__main__":
    unittest.main()
