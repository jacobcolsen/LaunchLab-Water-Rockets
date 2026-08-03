#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [ "$#" -gt 0 ]; then
  exec "$@"
else
  exec daphne -b 0.0.0.0 -p 8000 launchlab.asgi:application
fi
