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
    SNOWFLAKE_CONN_USERNAME, SNOWFLAKE_CONN_PASSWORD
                          optional -- Snowflake pass-through credentials
                          for live data sources whose Snowflake connection
                          is not embedded in Tableau. Both are optional:
                          if either is missing the feature is off and
                          behavior is unchanged. See _build_vds_datasource.

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
    (sign-in, list-datasources), and for a VizQL Data Service 401 that
    persists after one re-sign-in-and-retry -- i.e. a 401 that is proven
    to NOT be an expired session, since a brand-new session token hit it
    too. Carries Tableau's real `status`, `code` (parsed from the JSON
    error body when present), `message`, and the `endpoint` that failed,
    so callers can show the genuine error instead of guessing "session
    expired"."""

    def __init__(self, message, status=None, code=None, endpoint=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.endpoint = endpoint
        self.hint = None


class VizqlServiceError(Exception):
    """Raised when the VizQL Data Service (read-metadata / query-datasource)
    returns an error that is not a 401 (see TableauAPIError for that case).
    `str(exc)` carries Tableau's actual error text verbatim so the caller
    can display it (e.g. in an st.expander). `hint`, when set, is a short
    human-readable guess at the cause (see `_hint_for_error`)."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message
        self.hint = None


class NoDatasourcesError(Exception):
    """Raised when sign-in succeeds but the token's account has zero
    visible data sources. This is a permissions problem on the token's
    account/site role, not a bug in this client, so it is surfaced as its
    own condition rather than an empty list or a generic exception."""


class DatasourceNotFoundError(Exception):
    """Raised when a data source name can't be matched -- exactly, case-
    insensitively, or loosely -- against anything visible to this token."""


class TableauConnectionError(Exception):
    """Raised when a Tableau call never got a response at all -- DNS
    failure, connection refused, or a timed-out connect/read -- i.e.
    Tableau Cloud isn't reachable from this machine/network, as opposed to
    Tableau rejecting the request (that's TableauAuthError/TableauAPIError/
    VizqlServiceError, which always have a real HTTP response behind them).
    `message` is the user-facing sentence; `details` carries the original
    requests exception text for an expander, never a raw traceback."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details


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

# get_connections() results, keyed by datasource_id, for the life of the
# current Tableau session -- cleared on every sign_in() (a fresh session
# is the natural point to assume connections may have changed).
_connections_cache = {}


def _secret_values():
    """Every credential value currently configured, for redaction. Tableau
    (or Snowflake, via Tableau) can echo a submitted credential back in an
    error body -- e.g. a bad connectionPassword showing up in its own
    rejection message -- so every outgoing error message is scrubbed of
    these before it's ever raised, logged, or shown."""
    values = []
    for var in ("TABLEAU_PAT_SECRET", "TABLEAU_PAT_NAME", "SNOWFLAKE_CONN_USERNAME", "SNOWFLAKE_CONN_PASSWORD"):
        value = _get_env(var, required=False)
        if value:
            values.append(value)
    return values


def _redact(text):
    """Replace any configured secret value found verbatim in `text` with
    `[REDACTED]`. Always call this on response text / error bodies before
    they reach an exception message."""
    text = str(text)
    for secret in _secret_values():
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _snowflake_credentials():
    """Optional Snowflake pass-through credentials from the environment.
    Both are optional -- the feature is off unless both are set."""
    return _get_env("SNOWFLAKE_CONN_USERNAME", required=False), _get_env(
        "SNOWFLAKE_CONN_PASSWORD", required=False
    )


_UNREACHABLE_MESSAGE = (
    "Can't reach Tableau Cloud from this computer. Check internet/VPN, or "
    "set HTTPS_PROXY if your network uses a proxy."
)


def _request(method, url, **kwargs):
    """Thin wrapper around requests.post/requests.get that turns a
    network-level failure (DNS, connection refused, timed-out connect/
    read) into TableauConnectionError with a clear, non-technical message.
    Every requests call in this module goes through here so none of them
    can surface a raw traceback for an unreachable server -- the original
    exception text is kept on `.details` for an expander.

    Dispatches to `requests.post`/`requests.get` (rather than the generic
    `requests.request`) so tests can keep patching those two names
    directly, same as before this wrapper existed."""
    call = requests.post if method.upper() == "POST" else requests.get
    try:
        return call(url, **kwargs)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise TableauConnectionError(_UNREACHABLE_MESSAGE, details=str(exc)) from exc


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

    resp = _request("POST", url, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

    if resp.status_code == 401:
        raise TableauAuthError(
            "Tableau rejected the sign-in credentials (HTTP 401). Check "
            "TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET, and SITE_NAME."
        )
    if not resp.ok:
        raise TableauAPIError(f"Sign-in failed: HTTP {resp.status_code} - {_redact(resp.text)}")

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
    _connections_cache.clear()

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

    resp = _request("GET", url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)

    if resp.status_code == 401:
        raise TableauAuthError("Session token was rejected while listing data sources.")
    if not resp.ok:
        raise TableauAPIError(f"List datasources failed: HTTP {resp.status_code} - {_redact(resp.text)}")

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


def get_connections(datasource_id):
    """
    List a data source's underlying connection(s) via the REST API.

    Cached per `datasource_id` for the life of the current Tableau session
    (cleared on the next sign_in()).

    Args:
        datasource_id: the data source's `id` (see find_datasource).

    Returns:
        list[dict]: [{"id": str, "type": str, "server_address": str}, ...]
        Never includes the connection's stored username -- callers must
        not log or display it either.

    Raises:
        TableauAuthError: the session token was rejected.
        TableauAPIError: any other non-2xx response.
    """
    if datasource_id in _connections_cache:
        return _connections_cache[datasource_id]

    session = _ensure_session()
    url = (
        f"{session['server']}/api/{REST_API_VERSION}/sites/"
        f"{session['site_id']}/datasources/{datasource_id}/connections"
    )
    headers = {"X-Tableau-Auth": session["token"], "Accept": "application/json"}

    resp = _request("GET", url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

    if resp.status_code == 401:
        raise TableauAuthError("Session token was rejected while fetching data source connections.")
    if not resp.ok:
        raise TableauAPIError(
            f"Get connections failed: HTTP {resp.status_code} - {_redact(resp.text)}",
            status=resp.status_code,
            endpoint="connections",
        )

    payload = resp.json()
    raw = (payload.get("connections") or {}).get("connection") or []
    connections = [
        {"id": c.get("id"), "type": c.get("type"), "server_address": c.get("serverAddress")}
        for c in raw
    ]
    _connections_cache[datasource_id] = connections
    return connections


def _build_vds_datasource(datasource_id):
    """
    Build the `datasource` object for a VDS `read-metadata` /
    `query-datasource` request, attaching Snowflake pass-through
    credentials when (and only when) they apply.

    Credentials are attached only if BOTH are true:
      1. SNOWFLAKE_CONN_USERNAME and SNOWFLAKE_CONN_PASSWORD are both set.
      2. The data source has at least one connection whose `type` contains
         "snowflake" (case-insensitive).
    One entry is added per Snowflake connection. `connectionLuid` is
    included only when there is more than one Snowflake connection -- the
    proven single-connection request (test_snowflake_passthrough.py)
    omits it.

    Returns:
        tuple[dict, bool, bool]: (datasource object, has_snowflake,
        creds_configured) -- the extra flags let callers compute an
        accurate error hint if the request later fails.
    """
    ds_object = {"datasourceLuid": datasource_id}

    sf_user, sf_password = _snowflake_credentials()
    creds_configured = bool(sf_user and sf_password)

    connections = get_connections(datasource_id)
    sf_connections = [c for c in connections if "snowflake" in (c.get("type") or "").lower()]
    has_snowflake = bool(sf_connections)

    if creds_configured and sf_connections:
        entries = []
        for c in sf_connections:
            entry = {"connectionUsername": sf_user, "connectionPassword": sf_password}
            if len(sf_connections) > 1:
                entry["connectionLuid"] = c["id"]
            entries.append(entry)
        ds_object["connections"] = entries

    return ds_object, has_snowflake, creds_configured


def describe_connection_status(datasource_id):
    """
    Human-facing summary of a data source's connection, for display only
    (e.g. the sidebar status line / diagnose.py) -- never includes a
    connection's stored username or any secret.

    Returns:
        dict: {
            "live": bool,             # False for a published extract
            "connection_type": str | None,   # Tableau's raw `type`
            "creds_source": "app" | "embedded" | "none",
        }
        "creds_source" is "app" only when this module actually attaches
        Snowflake pass-through credentials to VDS requests for this data
        source (see _build_vds_datasource). Any other live connection is
        reported as "embedded" -- there is no API signal that distinguishes
        "Tableau already holds working credentials" from "this will 401
        when queried"; a wrong assumption here still surfaces as a real
        Tableau error at query time, it just isn't flagged in advance.
    """
    connections = get_connections(datasource_id)
    if not connections:
        return {"live": False, "connection_type": None, "creds_source": "none"}

    types = {(c.get("type") or "").lower() for c in connections}
    if types <= {"hyper"}:
        return {"live": False, "connection_type": "hyper", "creds_source": "none"}

    _, has_snowflake, creds_configured = _build_vds_datasource(datasource_id)
    connection_type = next((c.get("type") for c in connections if c.get("type")), None)
    creds_source = "app" if (has_snowflake and creds_configured) else "embedded"
    return {"live": True, "connection_type": connection_type, "creds_source": creds_source}


def _hint_for_error(status, text, has_snowflake, creds_configured):
    """Short, human-readable guess at the cause of a VDS error, per
    specs/snowflakepassthrough.md section 4.3. Returns None when nothing
    matches -- callers should fall back to showing Tableau's raw text."""
    text = text or ""
    lower = text.lower()

    if status == 401 and has_snowflake and not creds_configured:
        return (
            "This live source needs Snowflake credentials. Set "
            "SNOWFLAKE_CONN_USERNAME/PASSWORD."
        )
    if "403800" in text:
        return "Token's Tableau user lacks API Access on this data source."
    if "390422" in text or ("ip" in lower and "not allowed" in lower):
        return "Snowflake network policy is blocking Tableau Cloud."
    if "role" in lower and ("not authorized" in lower or "does not exist" in lower):
        return "Snowflake role can't read this table."
    if "warehouse" in lower:
        return "Snowflake user has no usable default warehouse."
    return None


def _vds_headers(session):
    return {
        "X-Tableau-Auth": session["token"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_error_code(payload):
    """Best-effort pull of Tableau's own error code out of a VDS error
    body. Shapes vary (`{"error": {"code": ...}}`, `{"errorCode": ...}`,
    `{"code": ...}`) so this tries each rather than assuming one."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code"):
        return error.get("code")
    return payload.get("code") or payload.get("errorCode")


def _extract_error_message(payload, fallback_text):
    if not isinstance(payload, dict):
        return fallback_text
    error = payload.get("error")
    if isinstance(error, dict):
        parts = [str(p) for p in (error.get("summary"), error.get("detail")) if p]
        if parts:
            return " - ".join(parts)
    if isinstance(error, str) and error:
        return error
    return payload.get("message") or fallback_text


def _post_vds(session, path, body, error_context=None, _allow_retry=True):
    """POST to a VizQL Data Service endpoint and return the parsed JSON
    payload, raising the appropriate exception on any error condition
    (HTTP-level or an `error` key in an otherwise-200 payload).

    On a 401, re-signs in once and retries the same call once with the
    fresh session before giving up. This is what lets a Snowflake-level
    401 (bad/missing pass-through creds, blocked network policy, ...) be
    told apart from a genuinely expired Tableau session: if sign_in()
    itself fails, that's a real session/credentials problem
    (TableauAuthError, "session rejected" wording is accurate). If
    sign_in() succeeds but the retried call still 401s, the session is
    proven fine -- that's Tableau's real error, raised as TableauAPIError
    with its actual status/code/message, never "session expired".

    `error_context`, when given, is the `(has_snowflake, creds_configured)`
    tuple from `_build_vds_datasource` -- used only to compute a more
    specific `.hint` on failure.
    """
    has_snowflake, creds_configured = error_context or (False, False)
    url = f"{session['server']}{path}"
    resp = _request("POST", url, json=body, headers=_vds_headers(session), timeout=REQUEST_TIMEOUT_SECONDS)

    if resp.status_code == 401:
        if _allow_retry:
            new_session = sign_in()
            return _post_vds(new_session, path, body, error_context=error_context, _allow_retry=False)
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        code = _extract_error_code(payload)
        message = _redact(_extract_error_message(payload, resp.text))
        exc = TableauAPIError(message, status=401, code=code, endpoint=path)
        exc.hint = _hint_for_error(401, f"{code or ''} {message}", has_snowflake, creds_configured)
        raise exc

    try:
        payload = resp.json()
    except ValueError:
        payload = None

    if not resp.ok:
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error") or payload
        else:
            message = resp.text
        message = _redact(message)
        exc = VizqlServiceError(f"HTTP {resp.status_code}: {message}")
        exc.hint = _hint_for_error(resp.status_code, str(message), has_snowflake, creds_configured)
        raise exc

    if payload is None:
        raise VizqlServiceError(f"VizQL Data Service returned a non-JSON response: {_redact(resp.text)}")

    if isinstance(payload, dict) and payload.get("error"):
        exc = VizqlServiceError(_redact(str(payload["error"])))
        exc.hint = _hint_for_error(resp.status_code, str(payload["error"]), has_snowflake, creds_configured)
        raise exc

    return payload


def _read_metadata_rows(ds, session):
    """Internal: raw read-metadata rows for a resolved datasource dict,
    keeping both the public-facing `fieldCaption` (== `name` everywhere
    else in this module) and Tableau's internal `fieldName`. The internal
    name matters because VizQL Data Service's own error text (e.g. a
    pre-aggregated-calculation error) references fields by `fieldName`,
    not `fieldCaption` -- see `run_query`'s retry-on-redundant-aggregation
    handling."""
    ds_object, has_snowflake, creds_configured = _build_vds_datasource(ds["id"])
    body = {"datasource": ds_object}
    payload = _post_vds(
        session,
        f"{VDS_BASE_PATH}/read-metadata",
        body,
        error_context=(has_snowflake, creds_configured),
    )
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

    ds_object, has_snowflake, creds_configured = _build_vds_datasource(ds["id"])
    error_context = (has_snowflake, creds_configured)
    body = {
        "datasource": ds_object,
        "query": {"fields": query_fields},
    }
    if query_filters:
        body["query"]["filters"] = query_filters

    try:
        payload = _post_vds(session, f"{VDS_BASE_PATH}/query-datasource", body, error_context=error_context)
    except VizqlServiceError as exc:
        retried = _retry_without_redundant_aggregation(str(exc), query_fields, internal_name_by_caption)
        if retried is None:
            raise
        body["query"]["fields"] = retried
        payload = _post_vds(session, f"{VDS_BASE_PATH}/query-datasource", body, error_context=error_context)

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
