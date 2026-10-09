"""
tests/test_tableau_client.py

Mocked-HTTP tests for the live Snowflake credential pass-through feature
(specs/snowflakepassthrough.md section 8). No network calls -- every
`requests.post` / `requests.get` is replaced with a fake.

Fake credential values only (never real secrets), per
specs/snowflakepassthrough.md section 7.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tableau_client as tc

FAKE_SERVER = "https://fake.tableau.test"
FAKE_SITE_NAME = "fakesite"
FAKE_SITE_ID = "site-123"
FAKE_TOKEN = "fake-session-token"
FAKE_DS_NAME = "FENIX_SEARCH (RPT_BASE.FENIX_SEARCH) (RPT_BASE)"
FAKE_DS_ID = "ds-123"
FAKE_CONN_ID = "conn-123"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json_data = json_data
        if text is not None:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = ""

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._json_data is None:
            raise ValueError("FakeResponse has no JSON body")
        return self._json_data


class Sequenced:
    """Returns responses from `items` in order; repeats the last one once
    exhausted (so a mock doesn't need to predict exactly how many times a
    cached-friendly call, like list_datasources, will hit the network)."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if len(self.items) > 1:
            return self.items.pop(0)
        return self.items[0]


def _signin_ok(*_args, **_kwargs):
    return FakeResponse(200, {"credentials": {"token": FAKE_TOKEN, "site": {"id": FAKE_SITE_ID}}})


def _datasources_list(*_args, **_kwargs):
    return FakeResponse(
        200,
        {"datasources": {"datasource": [{"id": FAKE_DS_ID, "name": FAKE_DS_NAME, "contentUrl": "fenix"}]}},
    )


def _connections(conn_types):
    def _responder(*_args, **_kwargs):
        return FakeResponse(
            200,
            {
                "connections": {
                    "connection": [
                        {"id": f"{FAKE_CONN_ID}-{i}", "type": t, "serverAddress": "db.example.com"}
                        for i, t in enumerate(conn_types)
                    ]
                }
            },
        )

    return _responder


@pytest.fixture(autouse=True)
def tableau_env(monkeypatch):
    monkeypatch.setenv("TABLEAU_SERVER", FAKE_SERVER)
    monkeypatch.setenv("SITE_NAME", FAKE_SITE_NAME)
    monkeypatch.setenv("TABLEAU_PAT_NAME", "test-pat-name")
    monkeypatch.setenv("TABLEAU_PAT_SECRET", "test-pat-secret")
    monkeypatch.delenv("SNOWFLAKE_CONN_USERNAME", raising=False)
    monkeypatch.delenv("SNOWFLAKE_CONN_PASSWORD", raising=False)

    tc._session_cache["token"] = None
    tc._session_cache["site_id"] = None
    tc._session_cache["server"] = None
    tc._connections_cache.clear()
    yield
    tc._session_cache["token"] = None
    tc._session_cache["site_id"] = None
    tc._session_cache["server"] = None
    tc._connections_cache.clear()


def _get_dispatch(get_mock, connections_responder):
    def _get(url, *args, **kwargs):
        if url.endswith("/connections"):
            return connections_responder(url, *args, **kwargs)
        return _datasources_list(url, *args, **kwargs)

    return _get


def _post_dispatch(post_sequences):
    """post_sequences: dict of URL-suffix -> Sequenced (or plain callable)."""

    def _post(url, *args, **kwargs):
        if url.endswith("/auth/signin"):
            return post_sequences["signin"](url, *args, **kwargs)
        if url.endswith("/read-metadata"):
            return post_sequences["read-metadata"](url, *args, **kwargs)
        if url.endswith("/query-datasource"):
            return post_sequences["query-datasource"](url, *args, **kwargs)
        raise AssertionError(f"Unexpected POST to {url}")

    return _post


# ---------------------------------------------------------------------------
# 1. Snowflake source + creds set -> connections attached to both VDS calls.
# ---------------------------------------------------------------------------

def test_snowflake_creds_attached_to_both_vds_calls(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_CONN_USERNAME", "test-user")
    monkeypatch.setenv("SNOWFLAKE_CONN_PASSWORD", "test-token")

    metadata_resp = Sequenced([
        FakeResponse(200, {"data": [
            {"fieldCaption": "Revenue", "fieldName": "Revenue", "dataType": "REAL"},
        ]})
    ])
    query_resp = Sequenced([FakeResponse(200, {"data": [{"SUM(Revenue)": 42}]})])
    posts = _post_dispatch({
        "signin": Sequenced([_signin_ok()]),
        "read-metadata": metadata_resp,
        "query-datasource": query_resp,
    })
    conns = _connections(["snowflake"])

    with patch("tableau_client.requests.post", side_effect=posts), \
         patch("tableau_client.requests.get", side_effect=_get_dispatch(None, conns)):
        fields = tc.get_datasource_fields(FAKE_DS_NAME)
        assert fields == [{"name": "Revenue", "dataType": "REAL"}]

        result = tc.run_query(FAKE_DS_NAME, [{"name": "Revenue", "aggregation": "SUM"}])
        assert result == [{"SUM(Revenue)": 42}]

    metadata_body = metadata_resp.calls[0][1]["json"]
    query_body = query_resp.calls[0][1]["json"]

    for body in (metadata_body, query_body):
        assert "connections" in body["datasource"]
        entries = body["datasource"]["connections"]
        assert entries == [{"connectionUsername": "test-user", "connectionPassword": "test-token"}]
        # Single connection -> no connectionLuid (matches the proven request).
        assert "connectionLuid" not in entries[0]


# ---------------------------------------------------------------------------
# 2. Snowflake source + creds missing -> no `connections` key; helpful hint.
# ---------------------------------------------------------------------------

def test_snowflake_creds_missing_gives_helpful_hint():
    unauthorized = FakeResponse(401, {"message": "Not authorized"})
    metadata_resp = Sequenced([unauthorized, unauthorized])
    posts = _post_dispatch({
        "signin": Sequenced([_signin_ok(), _signin_ok()]),
        "read-metadata": metadata_resp,
        "query-datasource": Sequenced([FakeResponse(200, {"data": []})]),
    })
    conns = _connections(["snowflake"])

    with patch("tableau_client.requests.post", side_effect=posts), \
         patch("tableau_client.requests.get", side_effect=_get_dispatch(None, conns)):
        with pytest.raises(tc.TableauAPIError) as excinfo:
            tc.get_datasource_fields(FAKE_DS_NAME)

    exc = excinfo.value
    assert exc.status == 401
    assert exc.hint == (
        "This live source needs Snowflake credentials. Set "
        "SNOWFLAKE_CONN_USERNAME/PASSWORD."
    )

    sent_body = metadata_resp.calls[0][1]["json"]
    assert "connections" not in sent_body["datasource"]


# ---------------------------------------------------------------------------
# 3. Extract / non-Snowflake source -> no `connections` key even if creds set.
# ---------------------------------------------------------------------------

def test_extract_datasource_never_gets_credentials(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_CONN_USERNAME", "test-user")
    monkeypatch.setenv("SNOWFLAKE_CONN_PASSWORD", "test-token")

    metadata_resp = Sequenced([FakeResponse(200, {"data": [
        {"fieldCaption": "Orders", "fieldName": "Orders", "dataType": "INTEGER"},
    ]})])
    posts = _post_dispatch({
        "signin": Sequenced([_signin_ok()]),
        "read-metadata": metadata_resp,
        "query-datasource": Sequenced([FakeResponse(200, {"data": []})]),
    })
    conns = _connections(["hyper"])  # a published extract

    with patch("tableau_client.requests.post", side_effect=posts), \
         patch("tableau_client.requests.get", side_effect=_get_dispatch(None, conns)):
        tc.get_datasource_fields(FAKE_DS_NAME)

    sent_body = metadata_resp.calls[0][1]["json"]
    assert "connections" not in sent_body["datasource"]


# ---------------------------------------------------------------------------
# 4. 401 then success after re-sign-in -> returns data, no error shown.
# ---------------------------------------------------------------------------

def test_401_then_success_after_resignin_is_transparent():
    metadata_resp = Sequenced([
        FakeResponse(401, {"message": "Session expired"}),
        FakeResponse(200, {"data": [{"fieldCaption": "Orders", "fieldName": "Orders", "dataType": "INTEGER"}]}),
    ])
    signin_seq = Sequenced([_signin_ok(), _signin_ok()])
    posts = _post_dispatch({
        "signin": signin_seq,
        "read-metadata": metadata_resp,
        "query-datasource": Sequenced([FakeResponse(200, {"data": []})]),
    })
    conns = _connections(["postgres"])

    with patch("tableau_client.requests.post", side_effect=posts), \
         patch("tableau_client.requests.get", side_effect=_get_dispatch(None, conns)):
        fields = tc.get_datasource_fields(FAKE_DS_NAME)

    assert fields == [{"name": "Orders", "dataType": "INTEGER"}]
    assert len(metadata_resp.calls) == 2
    assert len(signin_seq.calls) == 2  # initial sign-in + re-sign-in retry


# ---------------------------------------------------------------------------
# 5. 401 twice -> TableauAPIError with Tableau's real code.
# ---------------------------------------------------------------------------

def test_401_twice_raises_real_tableau_code(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_CONN_USERNAME", "test-user")
    monkeypatch.setenv("SNOWFLAKE_CONN_PASSWORD", "test-token")
    real_error = FakeResponse(401, {"errorCode": "390003", "message": "Snowflake role not authorized for this table"})
    metadata_resp = Sequenced([real_error, real_error])
    posts = _post_dispatch({
        "signin": Sequenced([_signin_ok(), _signin_ok()]),
        "read-metadata": metadata_resp,
        "query-datasource": Sequenced([FakeResponse(200, {"data": []})]),
    })
    conns = _connections(["snowflake"])

    with patch("tableau_client.requests.post", side_effect=posts), \
         patch("tableau_client.requests.get", side_effect=_get_dispatch(None, conns)):
        with pytest.raises(tc.TableauAPIError) as excinfo:
            tc.get_datasource_fields(FAKE_DS_NAME)

    exc = excinfo.value
    assert exc.status == 401
    assert exc.code == "390003"
    assert "Snowflake role not authorized for this table" in str(exc)
    assert exc.hint == "Snowflake role can't read this table."


# ---------------------------------------------------------------------------
# 6. Error text containing a secret -> appears as [REDACTED].
# ---------------------------------------------------------------------------

def test_secret_values_are_redacted_from_error_text(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_CONN_USERNAME", "test-user")
    monkeypatch.setenv("SNOWFLAKE_CONN_PASSWORD", "test-super-secret-token")

    leaky_error = FakeResponse(
        401,
        {"message": "Authentication failed for user test-user with password test-super-secret-token"},
    )
    metadata_resp = Sequenced([leaky_error, leaky_error])
    posts = _post_dispatch({
        "signin": Sequenced([_signin_ok(), _signin_ok()]),
        "read-metadata": metadata_resp,
        "query-datasource": Sequenced([FakeResponse(200, {"data": []})]),
    })
    conns = _connections(["snowflake"])

    with patch("tableau_client.requests.post", side_effect=posts), \
         patch("tableau_client.requests.get", side_effect=_get_dispatch(None, conns)):
        with pytest.raises(tc.TableauAPIError) as excinfo:
            tc.get_datasource_fields(FAKE_DS_NAME)

    text = str(excinfo.value)
    assert "test-super-secret-token" not in text
    assert "[REDACTED]" in text
