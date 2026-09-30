"""Container health probe: liveness only, no data. Exit 0 iff the GET returns 2xx."""
import os
import sys
import urllib.request

url = "http://127.0.0.1:%s%s" % (os.environ.get("BRAIN_PORT", "8080"),
                                 os.environ.get("BRAIN_HEALTH_PATH", "/healthz"))
try:
    with urllib.request.urlopen(url, timeout=3) as r:
        sys.exit(0 if 200 <= r.status < 300 else 1)
except Exception:
    sys.exit(1)
