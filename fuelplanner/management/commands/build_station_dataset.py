# One-off: turn the raw OPIS price file into data/fuel_stations.csv.
#
# The price file has no coordinates, so stations get their city's location from
# the Census 2024 gazetteer (places, then county subdivisions), with Nominatim
# for the ~150 hamlets the gazetteer doesn't have. Also writes data/us_places.csv,
# which the API uses to geocode "City, ST" input without a network call.
#
# Outputs are committed; only re-run this if the price file changes.

import csv
import io
import json
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from fuelplanner.services.places import US_STATES, census_name_variants, normalize_place

GAZETTEER_URLS = {
    "place": "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_place_national.zip",
    "cousub": "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_cousubs_national.zip",
}

# Names in the price file that neither the gazetteer nor Nominatim recognise.
CITY_ALIASES = {
    ("AR", "Hot Springs National Park"): "Hot Springs",
    ("NM", "Pueblo Of Acoma"): "Acomita Lake",  # Sky City, I-40 exit 102
    ("FL", "Mc Alpin"): "McAlpin",
    ("FL", "Saint Johns"): "St. Johns",
}

STATION_FIELDS = [
    "opis_id",
    "name",
    "address",
    "city",
    "state",
    "rack_id",
    "price",
    "price_samples",
    "lat",
    "lon",
    "geocode_source",
]


class Command(BaseCommand):
    help = "Geocode the OPIS price file into data/fuel_stations.csv"

    def add_arguments(self, parser):
        cfg = settings.FUEL_PLANNER
        parser.add_argument("--input", default=str(cfg["RAW_PRICES_CSV"]))
        parser.add_argument("--output", default=str(cfg["STATIONS_CSV"]))
        parser.add_argument("--places-output", default=str(cfg["PLACES_CSV"]))
        parser.add_argument("--cache-dir", default=str(settings.DATA_DIR / ".cache"))
        parser.add_argument(
            "--no-nominatim",
            action="store_true",
            help="Skip the Nominatim fallback (unmatched stations are dropped).",
        )

    def handle(self, *args, **opts):
        cache_dir = Path(opts["cache_dir"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        session = requests.Session()
        session.headers["User-Agent"] = settings.FUEL_PLANNER["HTTP_USER_AGENT"]

        places = self._read_gazetteer(session, cache_dir, "place")
        cousubs = self._read_gazetteer(session, cache_dir, "cousub")
        self._write_places(places, Path(opts["places_output"]))

        index = {}
        for source, rows in (("census_place", places), ("census_cousub", cousubs)):
            for row in rows:
                for variant in census_name_variants(row["NAME"]):
                    key = (row["USPS"], normalize_place(variant))
                    index.setdefault(key, (float(row["INTPTLAT"]), float(row["INTPTLONG"]), source))

        stations = self._aggregate(Path(opts["input"]))
        nominatim_cache_path = cache_dir / "nominatim.json"
        nominatim_cache = (
            json.loads(nominatim_cache_path.read_text()) if nominatim_cache_path.exists() else {}
        )

        written, dropped = [], []
        for st in stations:
            city = CITY_ALIASES.get((st["state"], st["city"]), st["city"])
            hit = index.get((st["state"], normalize_place(city)))
            if hit is None and not opts["no_nominatim"]:
                hit = self._nominatim(session, city, st["state"], nominatim_cache)
                nominatim_cache_path.write_text(json.dumps(nominatim_cache, indent=1))
            if hit is None:
                dropped.append(st)
                continue
            st["lat"], st["lon"], st["geocode_source"] = round(hit[0], 6), round(hit[1], 6), hit[2]
            written.append(st)

        out = Path(opts["output"])
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=STATION_FIELDS)
            writer.writeheader()
            writer.writerows(written)

        self.stdout.write(self.style.SUCCESS(f"Wrote {len(written)} stations to {out}"))
        for st in dropped:
            self.stdout.write(
                self.style.WARNING(
                    f"  dropped (not geocoded): {st['opis_id']} {st['city']}, {st['state']}"
                )
            )

    def _read_gazetteer(self, session, cache_dir, kind):
        zpath = cache_dir / f"gazetteer_{kind}.zip"
        if not zpath.exists():
            self.stdout.write(f"Downloading Census {kind} gazetteer...")
            resp = session.get(GAZETTEER_URLS[kind], timeout=120)
            resp.raise_for_status()
            zpath.write_bytes(resp.content)
        with zipfile.ZipFile(zpath) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".txt"))
            text = zf.read(name).decode("latin-1")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in reader]

    def _write_places(self, places, path):
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["state", "name", "census_name", "lat", "lon", "aland_sqmi"])
            for row in places:
                if row["USPS"] not in US_STATES:
                    continue  # PR, territories
                writer.writerow(
                    [
                        row["USPS"],
                        census_name_variants(row["NAME"])[0],
                        row["NAME"],
                        round(float(row["INTPTLAT"]), 5),
                        round(float(row["INTPTLONG"]), 5),
                        row["ALAND_SQMI"],
                    ]
                )
        self.stdout.write(f"Wrote offline gazetteer to {path}")

    def _aggregate(self, path):
        grouped = defaultdict(list)
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                if row["State"] not in US_STATES:
                    continue  # Canadian rows; USA only
                grouped[int(row["OPIS Truckstop ID"])].append(row)

        stations = []
        for opis_id, rows in sorted(grouped.items()):
            best = min(rows, key=lambda r: float(r["Retail Price"]))
            stations.append(
                {
                    "opis_id": opis_id,
                    "name": best["Truckstop Name"],
                    "address": best["Address"],
                    "city": best["City"],
                    "state": best["State"],
                    "rack_id": best["Rack ID"] or "",
                    "price": f"{float(best['Retail Price']):.5f}",
                    "price_samples": len(rows),
                }
            )
        return stations

    def _nominatim(self, session, city, state, cache):
        key = f"{city}|{state}"
        if cache.get(key) is None:
            # structured search misses a lot of unincorporated places; free-text finds them
            attempts = [
                {"city": city, "state": US_STATES[state], "country": "USA"},
                {"q": f"{city}, {US_STATES[state]}", "countrycodes": "us"},
            ]
            cache[key] = None
            for params in attempts:
                time.sleep(1.05)  # Nominatim allows 1 req/s
                resp = session.get(
                    f"{settings.FUEL_PLANNER['NOMINATIM_URL']}/search",
                    params={**params, "format": "jsonv2", "limit": 1},
                    timeout=30,
                )
                resp.raise_for_status()
                results = resp.json()
                if results:
                    cache[key] = [float(results[0]["lat"]), float(results[0]["lon"])]
                    break
            self.stdout.write(f"  nominatim {city}, {state}: {cache[key]}")
        hit = cache[key]
        return (hit[0], hit[1], "nominatim") if hit else None
