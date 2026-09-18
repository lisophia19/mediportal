#!/bin/sh
# Runs pending migrations, then serves with gunicorn (not Flask's dev
# server) -- used by the production image only; local dev still uses
# `flask run` directly per the README.
set -e
flask db upgrade
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --access-logfile - --access-logformat '%(t)s %(m)s %(U)s -> %(s)s' wsgi:app
