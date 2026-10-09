"""
diagnose.py

CLI: prints one row per Tableau data source visible to this token --

    name | live/extract | connection type | creds source | read-metadata status | Tableau error code

Reuses tableau_client.py (no duplicated request code). Never prints a
secret, a connection username, or a stored credential value.

Usage:
    python diagnose.py
"""

import tableau_client as tc


def _read_metadata_status(ds_name):
    """(status_text, error_code) for one data source's read-metadata call.
    Never includes the raw Tableau message -- just enough to spot a
    problem at a glance; re-run the app itself for full error detail."""
    try:
        fields = tc.get_datasource_fields(ds_name)
        return f"OK ({len(fields)} fields)", ""
    except tc.TableauAPIError as exc:
        return f"HTTP {exc.status}", exc.code or ""
    except tc.VizqlServiceError:
        return "ERROR", ""
    except tc.TableauAuthError:
        return "SESSION REJECTED", ""
    except tc.DatasourceNotFoundError:
        return "NOT FOUND", ""
    except tc.TableauConnectionError:
        return "UNREACHABLE", ""


def _connection_status(ds_id):
    try:
        return tc.describe_connection_status(ds_id)
    except (tc.TableauAuthError, tc.TableauAPIError, tc.TableauConnectionError):
        return {"live": None, "connection_type": None, "creds_source": "none"}


def main():
    try:
        datasources = tc.list_datasources()
    except tc.TableauConnectionError as exc:
        print(exc.message)
        print(f"({exc.details})")
        return

    rows = []
    for ds in datasources:
        status = _connection_status(ds["id"])
        if status["live"] is None:
            live_extract = "unknown"
        else:
            live_extract = "live" if status["live"] else "extract"
        read_status, error_code = _read_metadata_status(ds["name"])
        rows.append((
            ds["name"],
            live_extract,
            status["connection_type"] or "-",
            status["creds_source"],
            read_status,
            error_code,
        ))

    columns = (
        "name",
        "live/extract",
        "connection type",
        "creds source",
        "read-metadata status",
        "Tableau error code",
    )
    widths = [
        max(len(columns[i]), max((len(str(r[i])) for r in rows), default=0))
        for i in range(len(columns))
    ]

    def _format_row(values):
        return " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(values))

    print(_format_row(columns))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(_format_row(row))


if __name__ == "__main__":
    main()
