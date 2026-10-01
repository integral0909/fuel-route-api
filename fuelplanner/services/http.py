import threading
import time
from functools import lru_cache

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@lru_cache(maxsize=1)
def get_session():
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = settings.FUEL_PLANNER["HTTP_USER_AGENT"]
    return session


class CallCounter:
    """Records outbound API calls made while serving one request."""

    def __init__(self):
        self.calls = []

    def record(self, name):
        self.calls.append(name)

    def __len__(self):
        return len(self.calls)


class Throttle:
    """Minimum spacing between calls, shared by all threads in the process."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()
