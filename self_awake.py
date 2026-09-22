"""One-shot KETS wake/health probe.

The production schedule is driven by an external scheduler (the bundled
GitHub Actions workflow), not by a permanent process inside Render. This
module remains useful for a manual health check or an external scheduler.
"""

import os
import requests

KETS_URL = os.getenv("KETS_PUBLIC_URL", "https://kets.onrender.com").rstrip("/")

def ping() -> None:
    response = requests.get(f"{KETS_URL}/api/health", timeout=30)
    response.raise_for_status()
    print(f"KETS health OK: {response.status_code} {response.text[:200]}")

if __name__ == "__main__":
    ping()
