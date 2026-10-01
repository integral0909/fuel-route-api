# Offline geocoding for "City, ST" inputs, built from the Census 2024
# gazetteer (see build_station_dataset). Saves a Nominatim call per request.
import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

US_STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
STATE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}

CANADIAN_PROVINCES = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}
NON_US_REGIONS = (
    {c.lower() for c in CANADIAN_PROVINCES}
    | {n.lower() for n in CANADIAN_PROVINCES.values()}
    | {"canada", "mexico"}
)

# Census appends the legal type to names: "Big Cabin town", "Laredo city", ...
CENSUS_SUFFIX_RE = re.compile(
    r"\s+(city and borough|unified government|metropolitan government|"
    r"consolidated government|urban county|charter township|city|town|village|"
    r"borough|cdp|municipality|township|plantation)$",
    re.I,
)

CITY_STATE_RE = re.compile(
    r"^\s*(?P<city>[^,]+?)\s*,\s*(?P<state>[A-Za-z .]+?)\s*"
    r"(?:,\s*(?:usa|us|united states( of america)?)\s*)?$",
    re.I,
)


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    lat: float
    lon: float


def normalize_place(name):
    # Match key: "St. Louis" == "Saint Louis", "Mc Calla" == "McCalla", etc.
    s = name.lower().strip()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\bst\.?\s", "saint ", s)
    s = re.sub(r"\bste\.?\s", "sainte ", s)
    s = re.sub(r"\bmt\.?\s", "mount ", s)
    s = re.sub(r"\bft\.?\s", "fort ", s)
    s = re.sub(r"^s\s", "south ", s)
    s = re.sub(r"^n\s", "north ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def census_name_variants(raw):
    name = re.sub(r"\s*\(balance\)\s*$", "", raw.strip(), flags=re.I)
    stripped = CENSUS_SUFFIX_RE.sub("", name)
    variants = [stripped]
    if stripped.lower().endswith(" city"):  # "Boise City city"
        variants.append(stripped[:-5])
    if "-" in stripped and re.search(r"county|government", name, re.I):
        variants.append(stripped.split("-")[0])  # "Athens-Clarke County ..."
    return variants


def state_code(value):
    value = value.strip().rstrip(".")
    if value.upper() in US_STATES:
        return value.upper()
    return STATE_BY_NAME.get(value.lower())


def names_non_us_region(query):
    parts = [p.strip().rstrip(".").lower() for p in query.split(",")]
    return any(p in NON_US_REGIONS for p in parts[1:])


@lru_cache(maxsize=1)
def load_gazetteer():
    path = Path(settings.FUEL_PLANNER["PLACES_CSV"])
    if not path.exists():
        return {}
    index = {}  # (state, key) -> (land area, Place)
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            place = Place(row["name"], row["state"], float(row["lat"]), float(row["lon"]))
            area = float(row["aland_sqmi"] or 0)
            for variant in {row["name"], *census_name_variants(row["census_name"])}:
                key = (row["state"], normalize_place(variant))
                # Same name twice in a state (city + CDP): keep the bigger one.
                if key not in index or area > index[key][0]:
                    index[key] = (area, place)
    return {k: place for k, (_, place) in index.items()}


def lookup_city_state(query):
    m = CITY_STATE_RE.match(query)
    if not m:
        return None
    code = state_code(m.group("state"))
    if not code:
        return None
    return load_gazetteer().get((code, normalize_place(m.group("city"))))
