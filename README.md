# City Passport catalog

Content for the City Passport iOS app: curated NYC places plus events refreshed every morning.

- Published catalog: https://snoweyking1.github.io/city-passport-catalog/catalog.json
- Build report (sources, counts, errors): https://snoweyking1.github.io/city-passport-catalog/manifest.json

## How it updates

`.github/workflows/daily.yml` runs at about 5 AM New York time (and on any change to the catalog or scripts):

1. Runs the ingester tests.
2. `scripts/ingest_daily.py` merges `Catalog/catalog.source.json` (hand-curated places) with:
   - **NYC Parks** public events RSS (official, next 14 days).
   - **NYC for FREE** events listing (https://www.nycforfree.co/events): one request per day.
3. Validates the result (`scripts/catalog_tool.py` rules). An invalid catalog is never published.
4. Publishes `catalog.json` + `manifest.json` to GitHub Pages. The app checks for it on launch, on foreground (at most every 4 hours) and in iOS background refresh.

If a source fails, yesterday's still-upcoming events from that source are kept and the failure is recorded in `manifest.json`.

## Content rules

- From third-party listings we keep facts only (title, date, time, place, coordinates, category) and link back to the source page. Descriptions and images are not copied.
- Events are marked `sourced` with the source name and date. The app shows "Listed by … Confirm with the organizer". Nothing from a listing is presented as verified.
- Ambiguous listings are skipped: no concrete days or times, placeholder "all of NYC" locations, online events, past occurrences.
- Recurring classes and member programs from NYC Parks (fitness classes, rec-center programs) are excluded.

## Editing places

Edit `Catalog/catalog.source.json`. Keep IDs stable: users' saves and visits refer to them. Then run:

```bash
python3 scripts/catalog_tool.py validate
python3 scripts/test_ingest.py
python3 scripts/ingest_daily.py   # local build into Catalog/dist/
```

Pushing to `main` republishes automatically. To run the job by hand: Actions → Daily catalog → Run workflow.
