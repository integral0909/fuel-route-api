#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py load_stations --if-empty

exec "$@"
