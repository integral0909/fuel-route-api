# Fuel Route API

A Django 6.1 API that takes a start and a finish location in the USA and returns:

- the driving route, as GeoJSON plus an interactive map page
- the most cost-effective fuel stops along that route, for a vehicle with a 500-mile range
- the total fuel cost at 10 miles per gallon

A typical request makes **one** external call (to the OSRM routing API) and returns in about 0.1–0.6 s. Repeated requests come from cache in a few milliseconds and make no external calls.

## Quick start

```bash
make install        # venv + requirements-dev.txt (Python 3.12+)
make setup          # migrate + load data/fuel_stations.csv (6,626 US stations)
make run
```

Or with Docker: `docker compose up --build` (migrations and the station load run on start-up).

| URL | What |
|-----|------|
| `/api/route-plan/?start=New York, NY&finish=Los Angeles, CA` | the API |
| `/map/?start=New York, NY&finish=Los Angeles, CA` | interactive map of a plan |
| `/api/docs/` | Swagger UI (OpenAPI schema at `/api/schema/`) |
| `/healthz` | liveness + data check, used by the container health check |

No API keys are needed. A Postman collection with assertions on every request is in
`docs/fuel-route-api.postman_collection.json`.

## Development

```bash
make test        # 31 tests, HTTP mocked, no network needed
make coverage
make lint        # ruff check + format check
make check       # lint + tests + migration drift + OpenAPI schema validation (what CI runs)
```

CI (`.github/workflows/ci.yml`) runs lint, the test suite on Python 3.12 and 3.13, and builds
the Docker image and smoke-tests `/healthz`.

### Configuration

All settings come from the environment; see `.env.example`. The ones that matter in production:

| Variable | Default | Notes |
|----------|---------|-------|
| `DJANGO_SECRET_KEY` | dev key | required when `DJANGO_DEBUG=0`, the app refuses to start otherwise |
| `DJANGO_DEBUG` | `1` | `0` in the Docker image; turns on secure cookies and HTTPS redirect |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | localhost is always allowed for the health check |
| `OSRM_BASE_URL`, `NOMINATIM_URL` | public servers | point at self-hosted instances for real traffic |
| `ROUTE_PLAN_RATE` | `60/min` | per-client limit, shared by the API and the map page |

## API

`GET /api/route-plan/` with query parameters, or `POST /api/route-plan/` with a JSON body.

| Parameter        | Required | Default | Description |
|------------------|----------|---------|-------------|
| `start`          | yes      |         | `"City, ST"`, `"City, State"`, a street address, or `"lat,lon"` |
| `finish`         | yes      |         | Same formats as `start` |
| `start_fuel`     | no       | `0`     | Fraction of a full tank at departure (0 = empty, 1 = full) |
| `corridor_miles` | no       | `5`     | Maximum distance of a station from the route (0.5–25) |
| `stop_penalty`   | no       | `2`     | Dollar cost assigned to each extra stop. `0` gives the strictly cheapest plan (see below) |

```bash
curl "http://127.0.0.1:8000/api/route-plan/?start=Chicago,%20IL&finish=Houston,%20TX"

curl -X POST http://127.0.0.1:8000/api/route-plan/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "New York, NY", "finish": "Los Angeles, CA", "start_fuel": 0.5}'
```

### Response (abridged, New York → Los Angeles)

```jsonc
{
  "start":  {"query": "New York, NY", "label": "New York, NY", "lat": 40.66, "lon": -73.94, "source": "gazetteer"},
  "finish": {"query": "Los Angeles, CA", "label": "Los Angeles, CA", "lat": 34.02, "lon": -118.41, "source": "gazetteer"},
  "route":  {"distance_miles": 2810.4, "duration_hours": 50.31},
  "fuel_stops": [
    {"sequence": 1, "opis_id": 72087, "name": "DELTA", "city": "Jersey City", "state": "NJ",
     "lat": 40.71, "lon": -74.06, "price_per_gallon": 3.239, "route_mile": 8.8,
     "distance_from_route_miles": 1.5, "fuel_on_arrival_gallons": 0.0,
     "gallons": 6.11, "cost": 19.79},
    // ... 9 stops in total
  ],
  "summary": {
    "total_fuel_cost": 859.48,
    "gallons_purchased": 280.16,
    "gallons_used": 281.04,
    "number_of_stops": 9,
    "average_price_paid": 3.0678,
    "stations_considered": 349,
    "cheapest_possible": {"total_fuel_cost": 856.71, "number_of_stops": 18}
  },
  "assumptions": {"mpg": 10.0, "tank_range_miles": 500.0, "tank_capacity_gallons": 50.0,
                  "start_fuel_gallons": 0.88, "arrival_fuel_gallons": 0.0,
                  "corridor_miles": 5.0, "stop_penalty_usd": 2.0, "notes": ["..."]},
  "map": {
    "geojson": {"type": "FeatureCollection", "features": ["route LineString", "start", "finish", "one Point per fuel stop"]},
    "html_url": "http://127.0.0.1:8000/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA&start_fuel=0.0"
  },
  "meta": {"cache": "miss", "external_api_calls": 1, "external_api_call_names": ["osrm"], "elapsed_ms": 616.8}
}
```

The map is returned in two forms:
- `map.geojson` can be drawn by any client (Leaflet, Mapbox, geojson.io).
- `map.html_url` opens a ready-made Leaflet/OpenStreetMap page for the same plan. That page is served from cache, so it makes no new external calls.

### Errors

| Status | `error`                                     | When |
|--------|---------------------------------------------|------|
| 400    | `invalid_request`                           | A parameter is missing or out of range |
| 400    | `location_not_found`, `location_outside_usa` | The location can't be found, or it is outside the USA (for example `"Toronto, ON"` or `"51.5,-0.1"`) |
| 422    | `no_route`, `no_feasible_fuel_plan`         | No drivable route, or a gap longer than 500 miles with no station in the corridor |
| 429    | (DRF throttle)                              | More than `ROUTE_PLAN_RATE` requests from one client |
| 502    | `routing_unavailable`                       | The routing or geocoding API is down |

## How it works

```
start/finish ──► geocode ──► OSRM route (1 call) ──► stations near route ──► optimizer ──► response + cache
                 (offline)                           (KD-tree, in memory)    (exact DP)
```

1. **Geocoding without external calls.** `"City, ST"` inputs are resolved from a bundled copy of the US Census 2024 Gazetteer (`data/us_places.csv`). `"lat,lon"` inputs are used directly. Only other free-text input, such as a street address, falls back to Nominatim (OpenStreetMap), and the result is cached.
2. **Routing.** One request to the free public OSRM server gets the full route geometry. The base URL can be changed with `OSRM_BASE_URL`, for example to a self-hosted OSRM.
3. **Stations near the route.** When a worker process starts, all stations are loaded into memory as numpy arrays. For each request, the route is resampled every 0.25 miles and indexed in a `scipy` KD-tree. A single vectorised query then finds every station within `corridor_miles` of the route, along with its mile marker and its distance from the route. This step takes a few milliseconds.
4. **Choosing the stops.** This is the classic "gas station problem". A result by Khuller, Malekian and Mestre (*To fill or not to fill*, 2007) shows that an optimal plan only ever does one of two things at a stop: fill the tank, or buy just enough to reach the next stop. So the fuel on arrival at a stop can only be empty, what's left after filling up at an earlier stop, or the starting fuel. `optimizer.py` runs an exact dynamic programme over those few states. It takes about 20 ms for a coast-to-coast route with about 350 candidate stations.
   - **Why there is a stop penalty.** The strictly cheapest plan makes silly stops, such as buying 0.2 gallons at a station 10 miles ahead because it is 0.5¢ cheaper. The DP therefore minimises `fuel cost + stop_penalty × number of stops`. With the default of $2 per stop, New York → Los Angeles goes from 18 stops to 9 for $2.77 more (+0.3%). The response always includes the strictly cheapest plan in `summary.cheapest_possible`, and `stop_penalty=0` returns it directly.
   - **How it is tested.** With `stop_penalty=0`, the optimizer is checked against a linear-programming solver (`scipy.optimize.linprog`) on hundreds of random routes, and the costs match exactly.
5. **Caching.** Geocodes, OSRM routes and whole plans are cached in Django's cache for 24 hours. It is local memory by default; switch `CACHES` to Redis for multi-process deployments.

### Preparing the station data

The price file has no coordinates. `python manage.py build_station_dataset` prepares it once, offline:

1. It removes non-US rows (Canadian provinces).
2. It merges duplicate OPIS ids, keeping the lowest listed price.
3. It geocodes each station's city: first from the Census gazetteer's places and county subdivisions (96% match), then from Nominatim for the remaining ~150 hamlets, at 1 request per second with results cached on disk.

The output, `data/fuel_stations.csv`, is committed, so you don't need to run this command.

## Assumptions and limitations

- **Start fuel.** By default the vehicle departs empty. It is assumed to have just enough fuel to reach the first station in the corridor, and `assumptions.start_fuel_gallons` reports how much that is. `total_fuel_cost` is the money spent at the recommended stops. Pass `start_fuel=1` to depart with a full tank.
- **Station positions are approximate.** Stations are placed at their city's centre point, because the file gives addresses like "I-44, EXIT 283" rather than coordinates. `route_mile` and `distance_from_route_miles` are therefore approximate. Widen `corridor_miles` if the route passes near a big city whose centre is far from the highway.
- **Station coverage.** Coverage follows the price file. California, for example, has only 16 stations, so a trip that starts in California may have its first station hundreds of miles away. The empty-start assumption then covers that stretch, and `start_fuel_gallons` shows by how much.
- **Detours.** The cost of driving off the route to reach a station is not added. The corridor limit keeps these detours small.
- **Public APIs.** The public OSRM and Nominatim servers are free but rate-limited. For production, point `OSRM_BASE_URL` and `NOMINATIM_URL` at self-hosted instances.

## Project layout

```
config/                       settings (env-driven), urls, wsgi
fuelplanner/
  views.py, serializers.py    API, map page, health check, OpenAPI annotations
  throttling.py               rate limit shared by API + map page
  services/
    planner.py                orchestration, response shaping, caching
    optimizer.py              exact fuel-stop DP
    corridor.py, geo.py       KD-tree route/station matching, spherical maths
    station_index.py          in-memory station arrays
    geocoding.py, places.py   offline gazetteer + Nominatim fallback (throttled)
    osrm.py, http.py          routing client, shared HTTP session with retries
  management/commands/        build_station_dataset, load_stations
  templates/fuelplanner/      Leaflet map page
  tests/                      optimizer (LP cross-check), API, endpoints, corridor, places
data/                         raw price file, geocoded stations, US gazetteer
docker/, Dockerfile           gunicorn image, entrypoint runs migrate + load
docs/                         Postman collection
```
