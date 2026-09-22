"""
tableau_client.py

Thin, dependency-isolating wrapper around the Tableau REST API and the
VizQL Data Service (VDS). This is the only module in the project that is
allowed to touch `requests`, raw Tableau HTTP payloads, or Tableau
credentials directly. Every other module should import the functions
below and work with plain Python dicts/lists -- never a `requests`
Response object.

Credentials are read from environment variables via python-dotenv /
os.getenv (see `.env` in the project root):

    TABLEAU_SERVER        e.g. https://prod-useast-b.online.tableau.com
    SITE_NAME             the site's content URL / site name (NOT the
                           friendly display name -- e.g. "TABLEAU_SITE"
                           from an older example .env is wrong; this
                           project's actual env var is SITE_NAME)
    TABLEAU_PAT_NAME      Personal Access Token name
    TABLEAU_PAT_SECRET    Personal Access Token secret
    TABLEAU_DATASOURCE    optional convenience default, used by callers

Known environment gotchas handled here:
- Windows `set VAR="value"` leaves the literal quote characters in the
  value, so every credential is stripped of whitespace AND surrounding
  quote characters before use (`_clean`).
- The name shown on a data source's tile in the Tableau UI is not always
  the literal `name` field the API returns, so data source lookups always
  list-and-match with a loose/case-insensitive fallback rather than
  trusting an exact server-side filter (`find_datasource`).
- An empty data source list is treated as a permissions problem on the
  token's account, not a bug -- it is surfaced via `NoDatasourcesError`
  rather than silently returning an empty picker.
- Only SUM/AVG-style numeric aggregations are permitted on fields whose
  metadata `dataType` is INTEGER or REAL; anything else raises a
  `ValueError` before ever hitting the network.
"""

import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

# A current stable REST API version. Bump here if Tableau deprecates it.
REST_API_VERSION = "3.24"
VDS_BASE_PATH = "/api/v1/vizql-data-service"

REQUEST_TIMEOUT_SECONDS = 60

# dataType values (per VDS metadata) that support numeric aggregation.
NUMERIC_DATA_TYPES = {"INTEGER", "REAL"}

# Aggregation functions that only make sense on numeric fields. Other
# VDS functions (COUNT, COUNTD, MIN, MAX, YEAR, ...) are valid on
# non-numeric fields too and are intentionally NOT restricted here.
NUMERIC_ONLY_AGGREGATIONS = {"SUM", "AVG", "MEDIAN", "STDEV", "VAR"}

# Matches VDS's "already an aggregation" error, e.g.:
#   "The formula for calculation [avg:Calculation_1368249927221915648:qk]
#    is invalid: ... Argument to AVG (an aggregate function) is already
#    an aggregation, and cannot be further aggregated."
# Group 1 captures Tableau's *internal* fieldName (not fieldCaption) of
# the offending field -- there is no way to know ahead of time from
# read-metadata that a calculated field is already an aggregate; VDS only
# reveals it via this error when a query actually tries to aggregate it
# again.
_REDUNDANT_AGGREGATION_RE = re.compile(
    r"\[[a-z]+:([A-Za-z0-9_]+):qk\][^.]*already an aggregation", re.IGNORECASE
)


class TableauAuthError(Exception):
    """Raised when Tableau rejects sign-in credentials (HTTP 401), or a
    previously-issued session token is later rejected. The caller should
    show a clear "credentials rejected" message and must not silently
    retry."""


class TableauAPIError(Exception):
    """Raised for any other non-2xx response from the Tableau REST API
    (sign-in, list-datasources)."""


class VizqlServiceError(Exception):
    """Raised when the VizQL Data Service (read-metadata / query-datasource)
    returns an error. `str(exc)` carries Tableau's actual error text
    verbatim so the caller can display it (e.g. in an st.expander)."""


class NoDatasourcesError(Exception):
    """Raised when sign-in succeeds but the token's account has zero
    visible data sources. This is a permissions problem on the token's
    account/site role, not a bug in this client, so it is surfaced as its
    own condition rather than an empty list or a generic exception."""


class DatasourceNotFoundError(Exception):
    """Raised when a data source name can't be matched -- exactly, case-
    insensitively, or loosely -- against anything visible to this token."""


def _clean(value):
    """Strip whitespace and surrounding quote characters left behind by
    Windows `set VAR="value"` (or a `.env` file written the same way)."""
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def _get_env(name, required=True):
    cleaned = _clean(os.getenv(name))
    if required and not cleaned:
        raise ValueError(
            f"Environment variable {name} is not set or empty. "
            f"Check your .env file."
        )
    return cleaned


# Process-local session cache so repeated calls don't re-authenticate on
# every single call. sign_in() can always be called again explicitly to
# force a fresh session.
_session_cache = {"token": None, "site_id": None, "server": None}


def sign_in():
    """
    Sign in to Tableau Server/Cloud using a Personal Access Token.

    Reads TABLEAU_SERVER, SITE_NAME, TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET
    from the environment (via python-dotenv).

    Returns:
        dict: {"token": str, "site_id": str, "server": str}

    Raises:
        TableauAuthError: HTTP 401 -- bad PAT name/secret/site.
        TableauAPIError: any other non-2xx response, or an unexpected
            response shape.
    """
    server = _get_env("TABLEAU_SERVER").rstrip("/")
    site_name = _get_env("SITE_NAME")
    pat_name = _get_env("TABLEAU_PAT_NAME")
    pat_secret = _get_env("TABLEAU_PAT_SECRET")

    url = f"{server}/api/{REST_API_VERSION}/auth/signin"
    body = {
        "credentials": {
            "personalAccessTokenName": pat_name,
            "personalAccessTokenSecret": pat_secret,
            "site": {"contentUrl": site_name},
        }
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    resp = requests.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

    if resp.status_code == 401:
        raise TableauAuthError(
            "Tableau rejected the sign-in credentials (HTTP 401). Check "
            "TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET, and SITE_NAME."
        )
    if not resp.ok:
        raise TableauAPIError(f"Sign-in failed: HTTP {resp.status_code} - {resp.text}")

    payload = resp.json()
    try:
        creds = payload["credentials"]
        token = creds["token"]
        site_id = creds["site"]["id"]
    except (KeyError, TypeError) as exc:
        raise TableauAPIError(f"Unexpected sign-in response shape: {payload}") from exc

    _session_cache["token"] = token
    _session_cache["site_id"] = site_id
    _session_cache["server"] = server

    return {"token": token, "site_id": site_id, "server": server}


def _ensure_session():
    if not _session_cache["token"]:
        sign_in()
    return _session_cache


def list_datasources():
    """
    List all data sources visible to the signed-in token.

    Signs in automatically (via sign_in()) if no session exists yet.

    Returns:
        list[dict]: [{"id": str, "name": str, "contentUrl": str}, ...]
        `name` is the literal `name` field returned by the API -- render
        this as-is in any picker, don't reformat/guess it.

    Raises:
        NoDatasourcesError: the list came back empty -- treat as a
            permissions problem on the token's account, not a bug.
        TableauAuthError: the session token was rejected.
        TableauAPIError: any other non-2xx response.
    """
    session = _ensure_session()
    url = f"{session['server']}/api/{REST_API_VERSION}/sites/{session['site_id']}/datasources"
    headers = {"X-Tableau-Auth": session["token"], "Accept": "application/json"}
    params = {"pageSize": 1000}

    resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)

    if resp.status_code == 401:
        raise TableauAuthError("Session token was rejected while listing data sources.")
    if not resp.ok:
        raise TableauAPIError(f"List datasources failed: HTTP {resp.status_code} - {resp.text}")

    payload = resp.json()
    raw = (payload.get("datasources") or {}).get("datasource") or []

    datasources = [
        {"id": ds.get("id"), "name": ds.get("name"), "contentUrl": ds.get("contentUrl")}
        for ds in raw
    ]

    if not datasources:
        raise NoDatasourcesError(
            "This token has no visible data sources. This is almost always "
            "a permissions problem on the token owner's account (missing "
            "Explore/Connect permission on the relevant projects in "
            "Tableau), not a bug in this client."
        )

    return datasources


def find_datasource(name):
    """
    Resolve a data source name to its full record. List-and-match, never
    assume an exact server-side filter would have hit -- the name on a
    tile in the Tableau UI doesn't always match the literal `name` field
    (e.g. underscores rendered as spaces).

    Match order: exact -> case-insensitive exact -> alnum-normalized
    (ignoring case/spaces/underscores/punctuation) -> substring.

    Returns:
        dict: {"id": str, "name": str, "contentUrl": str}

    Raises:
        DatasourceNotFoundError: nothing matched, even loosely.
    """
    datasources = list_datasources()

    for ds in datasources:
        if ds["name"] == name:
            return ds

    lname = name.strip().lower()
    for ds in datasources:
        if (ds["name"] or "").strip().lower() == lname:
            return ds

    def _normalize(s):
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    target = _normalize(name)
    for ds in datasources:
        if target and _normalize(ds["name"]) == target:
            return ds

    for ds in datasources:
        if target and target in _normalize(ds["name"]):
            return ds

    available = ", ".join(ds["name"] for ds in datasources)
    raise DatasourceNotFoundError(
        f"No data source matching '{name}' was found among the "
        f"{len(datasources)} visible to this token: {available}"
    )


def _vds_headers(session):
    return {
        "X-Tableau-Auth": session["token"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post_vds(session, path, body):
    """POST to a VizQL Data Service endpoint and return the parsed JSON
    payload, raising the appropriate exception on any error condition
    (HTTP-level or an `error` key in an otherwise-200 payload)."""
    url = f"{session['server']}{path}"
    resp = requests.post(url, json=body, headers=_vds_headers(session), timeout=REQUEST_TIMEOUT_SECONDS)

    if resp.status_code == 401:
        raise TableauAuthError("Session token was rejected by VizQL Data Service.")

    try:
        payload = resp.json()
    except ValueError:
        payload = None

    if not resp.ok:
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error") or payload
        else:
            message = resp.text
        raise VizqlServiceError(f"HTTP {resp.status_code}: {message}")

    if payload is None:
        raise VizqlServiceError(f"VizQL Data Service returned a non-JSON response: {resp.text}")

    if isinstance(payload, dict) and payload.get("error"):
        raise VizqlServiceError(str(payload["error"]))

    return payload


def _read_metadata_rows(ds, session):
    """Internal: raw read-metadata rows for a resolved datasource dict,
    keeping both the public-facing `fieldCaption` (== `name` everywhere
    else in this module) and Tableau's internal `fieldName`. The internal
    name matters because VizQL Data Service's own error text (e.g. a
    pre-aggregated-calculation error) references fields by `fieldName`,
    not `fieldCaption` -- see `run_query`'s retry-on-redundant-aggregation
    handling."""
    body = {"datasource": {"datasourceLuid": ds["id"]}}
    payload = _post_vds(session, f"{VDS_BASE_PATH}/read-metadata", body)
    return payload.get("data") or []


def get_datasource_fields(name):
    """
    Fetch field metadata for the named data source via VizQL Data
    Service `read-metadata`.

    Args:
        name: data source name (resolved via find_datasource's
            list-and-match logic, so UI-tile-name vs literal-name
            mismatches are tolerated).

    Returns:
        list[dict]: [{"name": str, "dataType": str}, ...]
        `name` here is the field's `fieldCaption` -- this is exactly what
        must be passed back into `run_query`'s `fields`/`filters`.
        `dataType` is one of INTEGER, REAL, STRING, DATETIME, BOOLEAN,
        DATE, SPATIAL, UNKNOWN (Tableau's own vocabulary, passed through
        unchanged).

    Raises:
        DatasourceNotFoundError: `name` couldn't be resolved.
        VizqlServiceError: VDS returned an error (verbatim Tableau text).
    """
    ds = find_datasource(name)
    session = _ensure_session()

    raw_fields = _read_metadata_rows(ds, session)
    return [
        {"name": f.get("fieldCaption") or f.get("fieldName"), "dataType": f.get("dataType")}
        for f in raw_fields
    ]


def run_query(name, fields, filters=None):
    """
    Run an aggregation query against the named data source via VizQL
    Data Service `query-datasource`.

    Args:
        name: data source name (see find_datasource).
        fields: list[dict], each {"name": str, "aggregation": Optional[str]}.
            `name` must be a field returned by get_datasource_fields()
            for this same data source. `aggregation` (e.g. "SUM", "AVG",
            "MEDIAN", "COUNT", "COUNTD", "MIN", "MAX", ...) is optional --
            omit/None to use the field as a plain group-by/select column.
            SUM/AVG/MEDIAN/STDEV/VAR are only permitted when the field's
            dataType is INTEGER or REAL.
        filters: optional list[dict], each
            {"field": str, "operator": str, "value": Any}.
            operator "=" / "==" / "in"      -> SET filter (include values)
            operator "!=" / "not in"        -> SET filter (exclude values)
            operator "contains" / "like"    -> MATCH filter (contains)
            operator ">" / ">="             -> QUANTITATIVE_NUMERICAL (MIN,
                                               inclusive -- VDS has no
                                               strict-inequality bound)
            operator "<" / "<="             -> QUANTITATIVE_NUMERICAL (MAX,
                                               inclusive, same caveat)
            `value` may be a scalar or a list (for "in"/"not in"); for the
            comparison operators it must be numeric and the field's
            dataType must be INTEGER or REAL.

    Returns:
        list[dict]: the `data` rows exactly as VDS returns them -- a list
        of {fieldCaption-or-alias: value} objects, one per result row.
        An empty list means the query legitimately returned zero rows;
        callers should say so plainly rather than inventing an answer.

    Raises:
        ValueError: an unknown field/filter field name for this data
            source, an unsupported filter operator, or a numeric
            aggregation (SUM/AVG/...) requested on a non-numeric field --
            raised before any network call is made.
        DatasourceNotFoundError: `name` couldn't be resolved.
        VizqlServiceError: VDS returned an error (verbatim Tableau text).

    Note:
        Some calculated fields in a Tableau data source already contain
        an aggregation in their own formula (e.g. a ratio calc built from
        AVG()s) -- VDS has no way to expose this ahead of time via
        read-metadata, and only reveals it when a query tries to
        aggregate such a field again ("... is already an aggregation,
        and cannot be further aggregated"). This function recognizes that
        specific error and transparently retries once with the
        aggregation dropped for the offending field.
    """
    ds = find_datasource(name)
    session = _ensure_session()

    metadata_rows = _read_metadata_rows(ds, session)
    metadata = {}
    internal_name_by_caption = {}
    for row in metadata_rows:
        caption = row.get("fieldCaption") or row.get("fieldName")
        metadata[caption] = row.get("dataType")
        internal_name_by_caption[caption] = row.get("fieldName")

    query_fields = []
    for field in fields:
        field_name = field.get("name")
        aggregation = field.get("aggregation")

        if field_name not in metadata:
            raise ValueError(
                f"Field '{field_name}' does not exist in data source "
                f"'{ds['name']}'. Available fields: {', '.join(metadata.keys())}"
            )

        if aggregation and aggregation.strip().upper() == "NONE":
            # llm_agent's query_datasource tool schema makes `aggregation`
            # required and uses the literal string "NONE" as its
            # no-aggregation sentinel (since a JSON schema enum can't
            # express "omit this key"). Treat it the same as omitted/None.
            aggregation = None

        if aggregation:
            aggregation = aggregation.upper()
            if aggregation in NUMERIC_ONLY_AGGREGATIONS and metadata[field_name] not in NUMERIC_DATA_TYPES:
                raise ValueError(
                    f"Cannot apply aggregation '{aggregation}' to field "
                    f"'{field_name}' -- its dataType is "
                    f"'{metadata[field_name]}', not INTEGER/REAL. Use it as "
                    f"a filter or group-by field instead."
                )
            query_fields.append({"fieldCaption": field_name, "function": aggregation})
        else:
            query_fields.append({"fieldCaption": field_name})

    query_filters = []
    for f in filters or []:
        field_name = f.get("field")
        if field_name not in metadata:
            raise ValueError(
                f"Filter field '{field_name}' does not exist in data "
                f"source '{ds['name']}'. Available fields: "
                f"{', '.join(metadata.keys())}"
            )
        operator = (f.get("operator") or "=").strip().lower()
        value = f.get("value")
        values = value if isinstance(value, list) else [value]

        if operator in ("=", "==", "in"):
            query_filters.append({
                "field": {"fieldCaption": field_name},
                "filterType": "SET",
                "values": values,
                "exclude": False,
            })
        elif operator in ("!=", "not in"):
            query_filters.append({
                "field": {"fieldCaption": field_name},
                "filterType": "SET",
                "values": values,
                "exclude": True,
            })
        elif operator in ("contains", "like"):
            query_filters.append({
                "field": {"fieldCaption": field_name},
                "filterType": "MATCH",
                "contains": str(value),
            })
        elif operator in (">", ">=", "<", "<="):
            if metadata[field_name] not in NUMERIC_DATA_TYPES:
                raise ValueError(
                    f"Cannot apply operator '{operator}' to field '{field_name}' "
                    f"-- its dataType is '{metadata[field_name]}', not INTEGER/REAL."
                )
            try:
                bound = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Filter value for '{field_name}' {operator} must be numeric, got {value!r}."
                ) from exc
            # VDS's QUANTITATIVE_NUMERICAL filter only supports inclusive
            # bounds (MIN/MAX) -- there is no strict-inequality variant, so
            # ">" is treated the same as ">=", and "<" the same as "<=".
            if operator in (">", ">="):
                query_filters.append({
                    "field": {"fieldCaption": field_name},
                    "filterType": "QUANTITATIVE_NUMERICAL",
                    "quantitativeFilterType": "MIN",
                    "min": bound,
                })
            else:
                query_filters.append({
                    "field": {"fieldCaption": field_name},
                    "filterType": "QUANTITATIVE_NUMERICAL",
                    "quantitativeFilterType": "MAX",
                    "max": bound,
                })
        else:
            raise ValueError(f"Unsupported filter operator: {f.get('operator')!r}")

    body = {
        "datasource": {"datasourceLuid": ds["id"]},
        "query": {"fields": query_fields},
    }
    if query_filters:
        body["query"]["filters"] = query_filters

    try:
        payload = _post_vds(session, f"{VDS_BASE_PATH}/query-datasource", body)
    except VizqlServiceError as exc:
        retried = _retry_without_redundant_aggregation(str(exc), query_fields, internal_name_by_caption)
        if retried is None:
            raise
        body["query"]["fields"] = retried
        payload = _post_vds(session, f"{VDS_BASE_PATH}/query-datasource", body)

    return payload.get("data") or []


def _retry_without_redundant_aggregation(error_text, query_fields, internal_name_by_caption):
    """If `error_text` is VDS's "already an aggregation" error for one of
    our query fields, return a new `query_fields` list with that field's
    `function` dropped (it's already an aggregate calculation internally,
    so it must be queried like any other pre-aggregated column). Returns
    None if the error doesn't match, or doesn't match any field actually
    in this query -- callers should re-raise the original error in that
    case rather than retrying blindly."""
    match = _REDUNDANT_AGGREGATION_RE.search(error_text)
    if not match:
        return None
    offending_internal_name = match.group(1)

    caption_by_internal_name = {v: k for k, v in internal_name_by_caption.items()}
    offending_caption = caption_by_internal_name.get(offending_internal_name)
    if offending_caption is None:
        return None

    new_fields = []
    matched = False
    for f in query_fields:
        if f.get("fieldCaption") == offending_caption and "function" in f:
            new_fields.append({"fieldCaption": f["fieldCaption"]})
            matched = True
        else:
            new_fields.append(f)

    return new_fields if matched else None
