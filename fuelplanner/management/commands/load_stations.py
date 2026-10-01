import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelplanner.models import FuelStation
from fuelplanner.services.station_index import reset_station_index


class Command(BaseCommand):
    help = "Load data/fuel_stations.csv into the database, replacing what's there."

    def add_arguments(self, parser):
        parser.add_argument("--path", default=str(settings.FUEL_PLANNER["STATIONS_CSV"]))
        parser.add_argument(
            "--if-empty",
            action="store_true",
            help="Do nothing if stations are already loaded (for container start-up).",
        )

    def handle(self, *args, **opts):
        if opts["if_empty"] and FuelStation.objects.exists():
            self.stdout.write("Stations already loaded, skipping.")
            return

        path = Path(opts["path"])
        if not path.exists():
            raise CommandError(f"{path} not found. Run build_station_dataset first.")
        with path.open(newline="", encoding="utf-8") as fh:
            stations = [
                FuelStation(
                    opis_id=int(r["opis_id"]),
                    name=r["name"],
                    address=r["address"],
                    city=r["city"],
                    state=r["state"],
                    rack_id=int(r["rack_id"]) if r["rack_id"] else None,
                    price=Decimal(r["price"]),
                    price_samples=int(r["price_samples"]),
                    latitude=float(r["lat"]),
                    longitude=float(r["lon"]),
                    geocode_source=r["geocode_source"],
                )
                for r in csv.DictReader(fh)
            ]
        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=1000)
        reset_station_index()
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(stations)} fuel stations."))
