import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def to_unit_xyz(lat, lon):
    lat = np.radians(np.asarray(lat, dtype=float))
    lon = np.radians(np.asarray(lon, dtype=float))
    return np.column_stack((np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)))


# The KD-tree works in 3D on the unit sphere, so distances are chord lengths.
def miles_to_chord(miles):
    return 2 * np.sin(miles / (2 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2, 0, 1))


def cumulative_miles(lat, lon):
    seg = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    return np.concatenate(([0.0], np.cumsum(seg)))


def resample_polyline(lat, lon, step_miles):
    # OSRM leaves long straight stretches (I-80 in Nebraska etc.) with very few
    # vertices, so densify before doing nearest-point lookups.
    lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
    cum = cumulative_miles(lat, lon)
    marks = np.append(np.arange(0, cum[-1], step_miles), cum[-1])
    return np.interp(marks, cum, lat), np.interp(marks, cum, lon), marks
