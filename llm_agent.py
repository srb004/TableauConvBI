"""
llm_agent.py
============

Owns all interaction with the LLM (Azure OpenAI) for the Tableau
Conversational Query Agent.

Two responsibilities, both using OpenAI-style function/tool calling on the
Chat Completions API (never a hand-written NL parser):

1. ``translate_question`` — turn a natural-language question (plus recent
   chat history and the field metadata of the *currently selected* Tableau
   data source) into structured query arguments via a single
   ``query_datasource`` tool call. The model's tool-call output is validated
   against the real field metadata before it is trusted; an invalid
   field/aggregation pairing triggers an automatic corrective retry.

2. ``synthesize_answer`` — turn a raw query result (list of row dicts) plus
   the original question into a short, plain-language answer via a normal
   (tool-free) chat completion.

Credentials
-----------
All Azure OpenAI credentials are read from environment variables via
``python-dotenv`` / ``os.getenv``. The env var names actually present in
this project's ``.env`` (confirmed by direct inspection) are:

    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_DEPLOYMENT          (note: no "_NAME" suffix, unlike the
                                       spec's original naming)
    AZURE_OPENAI_API_VERSION

No credential is ever hardcoded, logged, or included in any prompt sent to
the model. If any of the four variables above is missing or blank, this
module fails fast with a ``MissingCredentialError`` naming the exact
variable that is missing — it never guesses a default or falls back to a
different provider/deployment.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration / client bootstrap
# ---------------------------------------------------------------------------

# The exact env var names present in this project's .env file.
_ENV_API_KEY = "AZURE_OPENAI_API_KEY"
_ENV_ENDPOINT = "AZURE_OPENAI_ENDPOINT"
_ENV_DEPLOYMENT = "AZURE_OPENAI_DEPLOYMENT"  # no "_NAME" suffix in this project
_ENV_API_VERSION = "AZURE_OPENAI_API_VERSION"

_REQUIRED_ENV_VARS = (_ENV_API_KEY, _ENV_ENDPOINT, _ENV_DEPLOYMENT, _ENV_API_VERSION)

# Numeric Tableau field dataTypes that legitimately support SUM/AVG.
_NUMERIC_DATA_TYPES = {"INTEGER", "REAL"}
_NUMERIC_AGGREGATIONS = {"SUM", "AVG"}

# How many times we'll ask the model to correct an invalid tool call before
# giving up.
_MAX_TRANSLATION_ATTEMPTS = 3


class MissingCredentialError(EnvironmentError):
    """Raised when a required Azure OpenAI env var is missing or blank."""


class ToolCallValidationError(RuntimeError):
    """Raised when the model cannot produce a valid tool call after retries."""


def _clean(value: Optional[str]) -> Optional[str]:
    """Strip surrounding whitespace and matching quote characters.

    Windows `set VAR="value"` leaves literal quote characters in the env
    value; this mirrors the same defensive cleanup used elsewhere in this
    project for credentials read from the environment.
    """
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def _get_config() -> Dict[str, str]:
    """Read and validate all required Azure OpenAI env vars.

    Fails fast, naming the specific missing variable, rather than guessing
    a default or silently falling back to a different provider.
    """
    values: Dict[str, Optional[str]] = {name: _clean(os.getenv(name)) for name in _REQUIRED_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise MissingCredentialError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in .env (see specs/Spec.md / .env.example)."
        )
    return values  # type: ignore[return-value]


_client: Optional[AzureOpenAI] = None
_deployment: Optional[str] = None


def _get_client() -> "tuple[AzureOpenAI, str]":
    """Lazily build (and cache) the AzureOpenAI client + deployment name."""
    global _client, _deployment
    if _client is None:
        config = _get_config()
        _client = AzureOpenAI(
            api_key=config[_ENV_API_KEY],
            azure_endpoint=config[_ENV_ENDPOINT],
            api_version=config[_ENV_API_VERSION],
        )
        _deployment = config[_ENV_DEPLOYMENT]
    return _client, _deployment  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

_QUERY_DATASOURCE_TOOL = {
    "type": "function",
    "function": {
        "name": "query_datasource",
        "description": (
            "Run an aggregation query against the currently selected Tableau "
            "data source. Only ever reference field names that appear in the "
            "field metadata provided in the system message — never a field "
            "from a different data source, and never an invented field."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "description": (
                        "The fields to return, each with an optional aggregation. "
                        "Must be non-empty."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Exact field name, copied verbatim from the field metadata.",
                            },
                            "aggregation": {
                                "type": "string",
                                "enum": ["SUM", "AVG", "COUNT", "MIN", "MAX", "NONE"],
                                "description": (
                                    "Aggregation to apply. Use SUM/AVG only on INTEGER or REAL "
                                    "fields. Use NONE for group-by / non-aggregated fields."
                                ),
                            },
                        },
                        "required": ["name", "aggregation"],
                    },
                },
                "filters": {
                    "type": "array",
                    "description": "Optional filters to narrow the query. Omit or leave empty if none apply.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "description": "Exact field name, copied verbatim from the field metadata.",
                            },
                            "operator": {
                                "type": "string",
                                "enum": ["=", "!=", ">", "<", ">=", "<=", "IN", "CONTAINS"],
                            },
                            "value": {
                                "description": "The value(s) to filter on (string, number, or list for IN).",
                            },
                        },
                        "required": ["field", "operator", "value"],
                    },
                },
            },
            "required": ["fields"],
        },
    },
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_tool_args(args: Dict[str, Any], field_metadata: Sequence[Dict[str, str]]) -> List[str]:
    """Return a list of human-readable problems with ``args``; empty = valid.

    Two checks, per the brief:
      - every field/filter name referenced must actually exist in
        ``field_metadata`` for the currently selected data source;
      - SUM/AVG may only be applied to a field whose dataType is
        INTEGER or REAL.
    """
    errors: List[str] = []
    field_types = {f["name"]: (f.get("dataType") or "").upper() for f in field_metadata}

    fields = args.get("fields")
    if not isinstance(fields, list) or len(fields) == 0:
        errors.append("`fields` must be a non-empty list.")
        fields = []

    for item in fields:
        if not isinstance(item, dict):
            errors.append(f"Field entry {item!r} is not an object with name/aggregation.")
            continue
        name = item.get("name")
        aggregation = (item.get("aggregation") or "NONE").upper()
        if name not in field_types:
            errors.append(f"Field '{name}' does not exist in the currently selected data source.")
            continue
        if aggregation in _NUMERIC_AGGREGATIONS and field_types[name] not in _NUMERIC_DATA_TYPES:
            errors.append(
                f"Aggregation '{aggregation}' is not valid on field '{name}' "
                f"(dataType={field_types[name] or 'UNKNOWN'}); SUM/AVG require an "
                "INTEGER or REAL field."
            )

    for flt in args.get("filters") or []:
        if not isinstance(flt, dict):
            errors.append(f"Filter entry {flt!r} is not an object with field/operator/value.")
            continue
        fname = flt.get("field")
        if fname not in field_types:
            errors.append(f"Filter references field '{fname}', which does not exist in this data source.")

    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def translate_question(
    question: str,
    chat_history: Optional[Sequence[Dict[str, str]]],
    field_metadata: Sequence[Dict[str, str]],
) -> Dict[str, Any]:
    """Translate a natural-language question into structured query arguments.

    Parameters
    ----------
    question:
        The user's latest natural-language question.
    chat_history:
        Recent conversation turns as OpenAI-style dicts, e.g.
        ``[{"role": "user"/"assistant", "content": "..."}, ...]``. May be
        ``None`` or empty.
    field_metadata:
        ``[{"name": str, "dataType": str}, ...]`` for the *currently
        selected* data source — the exact shape produced by
        ``tableau_client.get_datasource_fields()``. Only fields in this
        list may ever be referenced.

    Returns
    -------
    dict
        The validated tool-call arguments, shaped exactly like the
        ``query_datasource`` tool's parameters, e.g.::

            {
                "fields": [{"name": "Sales", "aggregation": "SUM"}],
                "filters": [{"field": "Region", "operator": "=", "value": "West"}],
            }

    Raises
    ------
    MissingCredentialError
        If a required Azure OpenAI env var is missing.
    ToolCallValidationError
        If the model does not produce a valid tool call (field exists in
        this data source, SUM/AVG only on INTEGER/REAL) within
        ``_MAX_TRANSLATION_ATTEMPTS`` attempts.
    """
    client, deployment = _get_client()

    system_content = (
        "You translate a user's natural-language question about a Tableau "
        "data source into a single call to the `query_datasource` tool.\n\n"
        "The CURRENTLY SELECTED data source has exactly these fields "
        "(name and dataType). You must ONLY reference fields from this list "
        "— never a field from a different data source, and never an "
        "invented field name:\n"
        f"{json.dumps(list(field_metadata))}\n\n"
        "Rules:\n"
        "- Only apply SUM or AVG aggregation to a field whose dataType is "
        "INTEGER or REAL.\n"
        "- Use NONE as the aggregation for fields used only for grouping or "
        "filtering.\n"
        "- If the question refers to a field that is not in the list above, "
        "still make your best-effort tool call using the closest real "
        "field(s) available; do not invent a field.\n"
        "- Always respond by calling the query_datasource tool — never with "
        "plain text."
    )

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
    for turn in chat_history or []:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    last_errors: List[str] = []
    for attempt in range(1, _MAX_TRANSLATION_ATTEMPTS + 1):
        response = client.chat.completions.create(
            model=deployment,
            messages=messages,
            tools=[_QUERY_DATASOURCE_TOOL],
            tool_choice={"type": "function", "function": {"name": "query_datasource"}},
        )
        message = response.choices[0].message
        tool_calls = message.tool_calls or []

        if not tool_calls:
            last_errors = ["Model did not produce a query_datasource tool call."]
            messages.append({"role": "assistant", "content": message.content or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "You must respond by calling the query_datasource tool, not with plain text.",
                }
            )
            continue

        tool_call = tool_calls[0]
        try:
            args = json.loads(tool_call.function.arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            last_errors = [f"Tool call arguments were not valid JSON: {exc}"]
            args = {}

        errors = _validate_tool_args(args, field_metadata) if args else last_errors
        if not errors:
            return {
                "fields": args.get("fields", []),
                "filters": args.get("filters", []),
            }

        last_errors = errors

        if attempt == _MAX_TRANSLATION_ATTEMPTS:
            break

        # Append the assistant's (invalid) tool call, then a tool result
        # explaining what was wrong, and let the model try again.
        messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": (
                    "That tool call was invalid: "
                    + "; ".join(errors)
                    + ". Call query_datasource again using only the fields listed "
                    "in the system message, and only use SUM/AVG on INTEGER or "
                    "REAL fields."
                ),
            }
        )

    raise ToolCallValidationError(
        f"Model failed to produce a valid query_datasource call after "
        f"{_MAX_TRANSLATION_ATTEMPTS} attempts. Last error(s): {'; '.join(last_errors)}"
    )


def synthesize_answer(question: str, query_result: Sequence[Dict[str, Any]]) -> str:
    """Ask the model for a short, plain-language answer given the query result.

    Parameters
    ----------
    question:
        The original natural-language question.
    query_result:
        The raw result rows returned by the Tableau VizQL query, as a list
        of dicts.

    Returns
    -------
    str
        A short, plain-language answer. Does not use tool calling.
    """
    client, deployment = _get_client()

    messages = [
        {
            "role": "system",
            "content": (
                "You answer questions about Tableau query results in one or two "
                "short, plain-language sentences. Base your answer strictly on "
                "the provided query result — never invent numbers or explanations "
                "for data that isn't there. If the result is empty, say plainly "
                "that no data was returned."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Query result (raw rows): {json.dumps(list(query_result), default=str)}\n\n"
                "Give a short, plain-language answer."
            ),
        },
    ]

    response = client.chat.completions.create(
        model=deployment,
        messages=messages,
    )
    return (response.choices[0].message.content or "").strip()
