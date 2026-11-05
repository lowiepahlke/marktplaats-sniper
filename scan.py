"""One-shot scan: grabs EVERY ad currently matching your searches (not just the
newest) and saves them to a spreadsheet (CSV) you can sort by price or distance.

    python scan.py                       all searches from settings.toml
    python scan.py "nintendo switch"     a quick one-off search (uses your postcode,
                                         whole country unless you add --km)
    python scan.py "lego" --km 20 --max 50 --out lego

Results land in the "scans" folder. Open the .csv with Excel or Google Sheets.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time

import mp_core
from mp_core import Ad, Search, SettingsError

PAGE_SIZE = 100
SCANS_DIR = mp_core.HERE / "scans"


def fetch_all(session, search: Search, postcode: str, cap: int) -> list[Ad]:
    ads: list[Ad] = []
    while len(ads) < cap:
        batch = mp_core.fetch_ads(session, search, postcode, limit=PAGE_SIZE, offset=len(ads))
        ads.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(0.5)  # polite
    return ads[:cap]


def row(ad: Ad, search: Search) -> dict:
    return {
        "search": search.name,
        "title": ad.title,
        "price": ad.price_cents / 100,
        "price_type": ad.price_label,
        "km": ad.distance_km if ad.distance_km is not None else "",
        "city": ad.city,
        "condition": ad.condition,
        "date": ad.date,
        "seller": ad.seller,
        "shop": "yes" if ad.is_shop else "",
        "highlights": ", ".join(mp_core.found_words(ad.text, search.highlight_words)),
        "url": ad.url,
        "description": ad.description.replace("\n", " ")[:400],
    }


def main() -> None:
    mp_core.fix_console()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", nargs="?", help="one-off search; leave out to use settings.toml")
    p.add_argument("--km", type=int, default=0, help="distance from your postcode (0 = whole country)")
    p.add_argument("--min", type=int, default=0, help="minimum price in euros")
    p.add_argument("--max", type=int, default=0, help="maximum price in euros")
    p.add_argument("--cap", type=int, default=1000, help="max ads per search")
    p.add_argument("--out", default="scan", help="file name (without .csv)")
    a = p.parse_args()

    try:
        settings = mp_core.load_settings()
        postcode, searches = settings.postcode, settings.searches
    except SettingsError as err:
        if not a.query:
            sys.exit(f"Problem with settings.toml:\n  {err}")
        postcode, searches = "", []  # a one-off search can still run without valid settings
    if a.query:
        if a.km and not mp_core.POSTCODE_RE.match(postcode):
            sys.exit('--km needs your postcode in settings.toml (postcode = "1234AB").')
        searches = [Search(name=a.query, query=a.query, min_price=a.min, max_price=a.max,
                           distance_km=a.km, exclude_words=mp_core.DEFAULT_EXCLUDE)]

    session = mp_core.new_session()
    rows: dict[str, dict] = {}
    skipped = 0
    for search in searches:
        t = time.time()
        ads = fetch_all(session, search, postcode, a.cap)
        for ad in ads:
            if mp_core.skip_reason(ad, search):
                skipped += 1
            else:
                rows.setdefault(ad.id, row(ad, search))
        print(f"{search.name}: {len(ads)} ads in {time.time() - t:.1f}s")

    keep = sorted(rows.values(), key=lambda r: (r["km"] if r["km"] != "" else 9999, r["price"]))
    print(f"{len(keep)} kept, {skipped} skipped by your filters")
    if not keep:
        return
    SCANS_DIR.mkdir(exist_ok=True)
    out = SCANS_DIR / f"{a.out}.csv"
    # utf-8-sig + ';' so Excel with Dutch regional settings opens it in proper columns.
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(keep[0]), delimiter=";")
        w.writeheader()
        for r in keep:
            w.writerow({**r, "price": f"{r['price']:.2f}".replace(".", ",")})
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
