# Spec — Live Snowflake Credential Pass-Through (Option B)

## 1. Context

The app (`app.py`, `tableau_client.py`, `llm_agent.py`) queries Tableau
published data sources through VizQL Data Service (VDS). It works for
extracts but fails on **live Snowflake data sources whose credentials are
not embedded** in Tableau (e.g. `FENIX_SEARCH (RPT_BASE.FENIX_SEARCH)
(RPT_BASE)`, whose Connections tab shows "Not embedded in connection").

Tableau returns a 401 for these, which the app currently mislabels as
"Tableau rejected the session ... restart the app to sign in again". The
session is actually fine; Tableau just cannot log into Snowflake.

VDS supports passing database credentials per request. Inside the
`datasource` object of both `read-metadata` and `query-datasource`:

```json
"datasource": {
  "datasourceLuid": "<luid>",
  "connections": [
    {
      "connectionUsername": "<snowflake username>",
      "connectionPassword": "<snowflake password or token>"
    }
  ]
}
```

Include `"connectionLuid": "<connection id>"` in each entry **only when the
data source has more than one connection**. The connection ID comes from the
REST method
`GET /api/{version}/sites/{site_id}/datasources/{datasource_id}/connections`.

`test_snowflake_passthrough.py` in the project root has already proven this
end to end against FENIX_SEARCH (32 fields read, a SUM query returned real
data). **Port its logic exactly; don't redesign it.**

## 2. Goal

Live Snowflake data sources with non-embedded credentials load their fields
and answer questions, using a Snowflake username + programmatic access token
from `.env`. Extracts and data sources with embedded credentials keep
working exactly as before.

## 3. Configuration

New `.env` keys (add both to `.env.example` with empty values):

```
SNOWFLAKE_CONN_USERNAME=
SNOWFLAKE_CONN_PASSWORD=
```

- Read via `python-dotenv`, cleaned with the existing `_clean()` helper
  (strips whitespace and surrounding quotes).
- Both are optional. If either is missing, the feature is off and the app
  behaves exactly as today, except with better error messages (section 6).
- `SNOWFLAKE_CONN_PASSWORD` currently holds a Snowflake password (POC only).
  It may later hold a programmatic access token, once Snowflake's network
  policy requirement for tokens is met. The code must not care which. Treat
  it as a secret everywhere.

## 4. Changes to `tableau_client.py`

### 4.1 `get_connections(datasource_id) -> list[dict]`
- Calls the REST connections endpoint above.
- Returns plain dicts: `{"id", "type", "server_address"}`. Do not return or
  log the connection's stored username.
- Cache results per `datasource_id` for the life of the Tableau session.

### 4.2 Credential attachment
- Add a private helper `_build_vds_datasource(datasource_id)` that returns
  the `datasource` object for VDS calls.
- Attach `connections` **only** when:
  1. both Snowflake env values are set, and
  2. the data source has at least one connection whose `type` contains
     `snowflake` (case-insensitive).
- Add one entry per Snowflake connection. Include `connectionLuid` only when
  the data source has more than one connection (the proven single-connection
  request omits it). Never add credentials to non-Snowflake connections.
- Use this helper in **both** `read-metadata` and `query-datasource`.

### 4.3 Real error handling
Replace the blanket "401 = session rejected" mapping:

- Add `TableauAPIError(Exception)` carrying `status`, `code` (Tableau's error
  code from the JSON body if present), `message`, and `endpoint`.
- On a 401 from a VDS call:
  1. Re-sign in once and retry the same call once.
  2. If it still fails, raise `TableauAPIError` with Tableau's real code and
     message. Only use the "session expired" wording if the retry after
     re-sign-in succeeded.
- Map known cases to a short human hint (keep Tableau's raw text too):

| Signal in response | Hint shown to the user |
|---|---|
| `403800` | Token's Tableau user lacks API Access on this data source |
| `390422` or "IP" + "not allowed" | Snowflake network policy is blocking Tableau Cloud |
| "role" + "not authorized" / "does not exist" | Snowflake role can't read this table |
| "warehouse" | Snowflake user has no usable default warehouse |
| 401 with no Snowflake creds configured on a Snowflake source | This live source needs Snowflake credentials. Set SNOWFLAKE_CONN_USERNAME/PASSWORD. |

- Every message passes through the existing secret sanitizer, which must
  now also redact `SNOWFLAKE_CONN_PASSWORD` and `SNOWFLAKE_CONN_USERNAME`.

## 5. Changes to `app.py`

- In the sidebar, under the data source picker, show a small status line for
  the selected source: `Live · Snowflake · credentials: from app` /
  `Live · credentials embedded` / `Extract`. Use the connection type and
  whether credentials were attached. No usernames, no secrets.
- Replace the current generic error box with: the hint (section 4.3) as the
  headline, and Tableau's real code + message inside an `st.expander`.
- Remove the "Restart the app to sign in again" text unless the error really
  is an expired session.
- No change to `llm_agent.py`. It keeps receiving the same field metadata
  shape.

## 6. `diagnose.py`

Add a CLI script (reuse `tableau_client.py`, don't duplicate requests code)
that prints one row per visible data source:

`name | live/extract | connection type | creds source (embedded/app/none) | read-metadata status | Tableau error code`

No secrets, no usernames in output.

## 7. Security rules (non-negotiable)

- Snowflake values only ever come from environment variables.
- Never print, log, render, or include them in any LLM prompt.
- Never hardcode them, including in tests (use fake values like `test-user` /
  `test-token`).
- Confirm `.env` is still in `.gitignore`.

## 8. Tests

Add `tests/test_tableau_client.py` using mocked HTTP (`responses` or
`unittest.mock`), no network:

1. Snowflake source + creds set → `connections` attached to both VDS calls.
2. Snowflake source + creds missing → no `connections` key; helpful 401 hint.
3. Extract / non-Snowflake source → no `connections` key even if creds set.
4. 401 then success after re-sign-in → returns data, no error shown.
5. 401 twice → `TableauAPIError` with Tableau's real code.
6. Error text containing the token → token appears as `[REDACTED]`.

## 9. Acceptance criteria

- [ ] `python test_snowflake_passthrough.py` passes (already true before this
      change; confirms the environment).
- [ ] In the app, FENIX_SEARCH loads its fields and answers a simple total.
- [ ] A question by a field not on any dashboard (e.g. by customer/segment, if
      present in the field list) returns a correct breakdown.
- [ ] The Revenue extract data source still works unchanged.
- [ ] Removing the two Snowflake env values makes FENIX_SEARCH show the
      "needs Snowflake credentials" hint, not "session rejected".
- [ ] All tests in section 8 pass.
- [ ] `git diff` shows no secret values anywhere.

## 10. Out of scope

- Per-user Snowflake credentials (one shared POC token for now).
- Publishing workbook-embedded data sources.
- Dashboard extension work.