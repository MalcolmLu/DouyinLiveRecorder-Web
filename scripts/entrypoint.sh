#!/bin/sh
set -eu
python /app/scripts/setup.py init
exec "$@"
