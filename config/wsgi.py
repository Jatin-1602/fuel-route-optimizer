"""WSGI entry point (``gunicorn config.wsgi``).

Set ``WARM_ON_STARTUP=1`` to load the station index and offline gazetteer when the module
is imported. Combined with ``gunicorn --preload`` this happens once in the master
process and the forked workers share the memory (copy-on-write), so no request ever
pays the ~1 s first-load cost.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

if os.environ.get("WARM_ON_STARTUP", "").lower() in ("1", "true", "yes"):
    from django.db import connections

    from apps.routing.services.corridor import get_station_index
    from apps.routing.services.geocoding import get_us_geography

    get_station_index()
    get_us_geography()
    connections.close_all()  # never share a DB connection across forked workers
