"""Register the KETS Pesapal IPN endpoint and print the IPN ID.

Run on Render shell or locally with the same environment variables as KETS:
PESAPAL_CONSUMER_KEY, PESAPAL_CONSUMER_SECRET, PESAPAL_ENVIRONMENT,
KETS_PUBLIC_URL.
"""
import os
import requests


env = os.environ.get("PESAPAL_ENVIRONMENT", "live").lower().strip()
base = "https://cybqa.pesapal.com/pesapalv3/api" if env in {"sandbox", "demo", "test"} else "https://pay.pesapal.com/v3/api"
public = os.environ.get("KETS_PUBLIC_URL", "https://kets.onrender.com").rstrip("/")
ipn_url = f"{public}/api/payments/ipn"

key = os.environ.get("PESAPAL_CONSUMER_KEY", "").strip()
secret = os.environ.get("PESAPAL_CONSUMER_SECRET", "").strip()
if not key or not secret:
    raise SystemExit("Missing PESAPAL_CONSUMER_KEY or PESAPAL_CONSUMER_SECRET")

r = requests.post(
    f"{base}/Auth/RequestToken",
    headers={"Accept": "application/json", "Content-Type": "application/json"},
    json={"consumer_key": key, "consumer_secret": secret},
    timeout=30,
)
r.raise_for_status()
token = r.json().get("token")
if not token:
    raise SystemExit(f"Pesapal authentication failed: {r.text}")

r = requests.post(
    f"{base}/URLSetup/RegisterIPN",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    },
    json={"url": ipn_url, "ipn_notification_type": "POST"},
    timeout=30,
)
print(r.text)
r.raise_for_status()
data = r.json()
print("\nKETS PESAPAL IPN URL:", ipn_url)
print("KETS PESAPAL IPN ID:", data.get("ipn_id"))
print("Save that IPN ID as the Render environment variable: PESAPAL_IPN_ID")
