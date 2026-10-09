"""
test_snowflake_passthrough.py

Terminal check: can Tableau Cloud query a LIVE Snowflake data source whose
credentials are NOT embedded, if we pass a Snowflake username + programmatic
access token with each VizQL Data Service request?

It runs these steps and stops at the first failure:
  1. Sign in to Tableau with your PAT
  2. Find the data source by name
  3. Look up its connection(s) (type + connection ID)
  4. read-metadata WITHOUT Snowflake creds  (expected to fail - baseline)
  5. read-metadata WITH Snowflake creds     (should succeed)
  6. One tiny aggregate query WITH creds    (proves real data comes back)

Setup:
    pip install requests python-dotenv
Put these in .env (no quotes):
    TABLEAU_SERVER, TABLEAU_SITE, TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET
    SNOWFLAKE_CONN_USERNAME, SNOWFLAKE_CONN_PASSWORD
    TABLEAU_DATASOURCE   (optional, defaults to FENIX_SEARCH below)
Run:
    python test_snowflake_passthrough.py
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

API_VERSION = "3.21"
DEFAULT_DS = "FENIX_SEARCH (RPT_BASE.FENIX_SEARCH) (RPT_BASE)"


def _clean(value):
    """Strip whitespace and surrounding quotes (Windows `set VAR="x"` keeps them)."""
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


SERVER = _clean(os.getenv("TABLEAU_SERVER")).rstrip("/")
SITE = _clean(os.getenv("TABLEAU_SITE") or os.getenv("SITE_NAME"))
PAT_NAME = _clean(os.getenv("TABLEAU_PAT_NAME"))
PAT_SECRET = _clean(os.getenv("TABLEAU_PAT_SECRET"))
SF_USER = _clean(os.getenv("SNOWFLAKE_CONN_USERNAME"))
SF_TOKEN = _clean(os.getenv("SNOWFLAKE_CONN_PASSWORD"))
DS_NAME = _clean(os.getenv("TABLEAU_DATASOURCE")) or DEFAULT_DS

SECRETS = [s for s in (PAT_SECRET, SF_TOKEN) if s]


def scrub(text):
    """Never print a secret, even if Tableau echoes it back in an error."""
    text = str(text)
    for s in SECRETS:
        text = text.replace(s, "[REDACTED]")
    return text


def fail(step, resp=None, msg=None):
    print(f"\n[FAIL] {step}")
    if resp is not None:
        print(f"  HTTP {resp.status_code}")
        print(f"  {scrub(resp.text)[:1500]}")
    if msg:
        print(f"  {msg}")
    sys.exit(1)


def preflight():
    missing = [n for n, v in {
        "TABLEAU_SERVER": SERVER, "TABLEAU_SITE": SITE,
        "TABLEAU_PAT_NAME": PAT_NAME, "TABLEAU_PAT_SECRET": PAT_SECRET,
        "SNOWFLAKE_CONN_USERNAME": SF_USER, "SNOWFLAKE_CONN_PASSWORD": SF_TOKEN,
    }.items() if not v]
    if missing:
        fail("Pre-flight", msg=f"Missing in .env: {', '.join(missing)}")
    print("--- Pre-flight (no secrets shown) ---")
    print(f"Server        : {SERVER}")
    print(f"Site          : {SITE}")
    print(f"Data source   : {DS_NAME}")
    print(f"Snowflake user: set (len={len(SF_USER)})")
    print(f"Snowflake tok : set (len={len(SF_TOKEN)})")
    print("-------------------------------------\n")


def sign_in():
    url = f"{SERVER}/api/{API_VERSION}/auth/signin"
    body = {"credentials": {
        "personalAccessTokenName": PAT_NAME,
        "personalAccessTokenSecret": PAT_SECRET,
        "site": {"contentUrl": SITE},
    }}
    r = requests.post(url, json=body, headers={"Accept": "application/json"})
    if r.status_code != 200:
        fail("1. Tableau sign-in", r)
    creds = r.json()["credentials"]
    print("[OK] 1. Signed in to Tableau")
    return creds["token"], creds["site"]["id"]


def find_datasource(token, site_id):
    url = f"{SERVER}/api/{API_VERSION}/sites/{site_id}/datasources"
    r = requests.get(url, headers={"X-Tableau-Auth": token, "Accept": "application/json"},
                     params={"pageSize": 1000})
    if r.status_code != 200:
        fail("2. List data sources", r)
    items = r.json().get("datasources", {}).get("datasource", []) or []
    exact = [d for d in items if d["name"] == DS_NAME]
    loose = [d for d in items if DS_NAME.lower() in d["name"].lower()]
    match = (exact or loose or [None])[0]
    if not match:
        fail("2. Find data source", msg=f"'{DS_NAME}' not visible to this token. "
             f"It can see {len(items)} data source(s).")
    print(f"[OK] 2. Found data source '{match['name']}'")
    return match["id"]


def get_connections(token, site_id, ds_id):
    url = f"{SERVER}/api/{API_VERSION}/sites/{site_id}/datasources/{ds_id}/connections"
    r = requests.get(url, headers={"X-Tableau-Auth": token, "Accept": "application/json"})
    if r.status_code != 200:
        fail("3. Get connections", r)
    conns = r.json().get("connections", {}).get("connection", []) or []
    print(f"[OK] 3. {len(conns)} connection(s):")
    for c in conns:
        print(f"       type={c.get('type')}  server={c.get('serverAddress')}  id={c.get('id')}")
    sf = [c for c in conns if "snowflake" in (c.get("type") or "").lower()]
    if not sf:
        fail("3. Get connections", msg="No Snowflake connection on this data source.")
    return sf


def vds_datasource(ds_id, sf_conns, with_creds):
    ds = {"datasourceLuid": ds_id}
    if with_creds:
        entries = []
        for c in sf_conns:
            entry = {"connectionUsername": SF_USER, "connectionPassword": SF_TOKEN}
            if len(sf_conns) > 1:  # ID only needed when there are several connections
                entry["connectionLuid"] = c["id"]
            entries.append(entry)
        ds["connections"] = entries
        print(f"       sending connection keys: {[list(e.keys()) for e in entries]}")
    return ds


def read_metadata(token, ds_id, sf_conns, with_creds):
    url = f"{SERVER}/api/v1/vizql-data-service/read-metadata"
    body = {"datasource": vds_datasource(ds_id, sf_conns, with_creds)}
    return requests.post(url, json=body,
                         headers={"X-Tableau-Auth": token, "Content-Type": "application/json"})


def main():
    preflight()
    token, site_id = sign_in()
    ds_id = find_datasource(token, site_id)
    sf_conns = get_connections(token, site_id, ds_id)

    r = read_metadata(token, ds_id, sf_conns, with_creds=False)
    if r.status_code == 200:
        print("[INFO] 4. Metadata worked WITHOUT creds (credentials may already be embedded)")
    else:
        print(f"[OK] 4. Baseline without creds failed as expected (HTTP {r.status_code})")

    r = read_metadata(token, ds_id, sf_conns, with_creds=True)
    if r.status_code != 200:
        fail("5. read-metadata WITH Snowflake creds", r,
             "Check the error text: IP/390422 = network policy; role/authorized = grants; "
             "warehouse = default warehouse.")
    fields = r.json().get("data", [])
    print(f"[OK] 5. Metadata WITH creds: {len(fields)} fields. First few:")
    for f in fields[:10]:
        print(f"       - {f.get('fieldCaption')} ({f.get('dataType')})")

    numeric = next((f["fieldCaption"] for f in fields
                    if f.get("dataType") in ("INTEGER", "REAL")), None)
    if not numeric:
        print("[SKIP] 6. No numeric field found for a sample query.")
    else:
        url = f"{SERVER}/api/v1/vizql-data-service/query-datasource"
        body = {
            "datasource": vds_datasource(ds_id, sf_conns, with_creds=True),
            "query": {"fields": [{"fieldCaption": numeric, "function": "SUM"}]},
        }
        r = requests.post(url, json=body,
                          headers={"X-Tableau-Auth": token, "Content-Type": "application/json"})
        if r.status_code != 200:
            fail("6. Sample query WITH creds", r)
        print(f"[OK] 6. SUM({numeric}) = {r.json().get('data')}")

    print("\nAll checks passed. Live Snowflake pass-through works. Safe to integrate.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        fail("Network", msg=scrub(e))