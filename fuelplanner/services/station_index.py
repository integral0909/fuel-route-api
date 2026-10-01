import threading
from dataclasses import dataclass

import numpy as np

from .geo import to_unit_xyz


@dataclass(frozen=True)
class StationIndex:
    records: list
    prices: np.ndarray
    xyz: np.ndarray

    def __len__(self):
        return len(self.records)


_lock = threading.Lock()
_index = None


def get_station_index():
    # Loaded lazily once per process; ~6.6k rows so it's tiny.
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = _load()
    return _index


def reset_station_index():
    global _index
    with _lock:
        _index = None


def _load():
    from fuelplanner.models import FuelStation

    rows = list(
        FuelStation.objects.values(
            "opis_id", "name", "address", "city", "state", "price", "latitude", "longitude"
        )
    )
    for r in rows:
        r["price"] = float(r["price"])

    if not rows:
        return StationIndex([], np.empty(0), np.empty((0, 3)))
    return StationIndex(
        records=rows,
        prices=np.array([r["price"] for r in rows]),
        xyz=to_unit_xyz([r["latitude"] for r in rows], [r["longitude"] for r in rows]),
    )
