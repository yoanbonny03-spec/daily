"""
Helper script to discover AmoCRM field IDs, pipeline IDs and status IDs.

Run once to fill in the .env file:
    python helper_find_field_ids.py
"""

import os
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import requests

DOMAIN = os.environ["AMO_DOMAIN"]
TOKEN = os.environ["AMO_ACCESS_TOKEN"]
BASE = f"https://{DOMAIN}.amocrm.ru/api/v4"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def get(path):
    r = requests.get(BASE + path, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def main():
    print("=" * 60)
    print("CUSTOM FIELDS (leads)")
    print("=" * 60)
    data = get("/leads/custom_fields?limit=250")
    for f in data.get("_embedded", {}).get("custom_fields", []):
        print(f"  ID={f['id']:>8}  type={f.get('type',''):15}  name={f['name']}")

    print()
    print("=" * 60)
    print("PIPELINES & STATUSES")
    print("=" * 60)
    data = get("/leads/pipelines?limit=250")
    for p in data.get("_embedded", {}).get("pipelines", []):
        print(f"\n  Pipeline ID={p['id']}  name={p['name']}")
        for s in p.get("_embedded", {}).get("statuses", []):
            print(f"    Status ID={s['id']:>8}  name={s['name']}")


if __name__ == "__main__":
    main()
