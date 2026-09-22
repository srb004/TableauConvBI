# Tableau Conversational Query Agent — Build Spec

## 1. Goal

Build a Streamlit app that lets a user pick any published Tableau data source
they have access to, ask questions about it in plain English, and get back an
answer grounded in a real query against that data — no Tableau Agent, no
premium AI add-on, just Tableau's standard REST API / VizQL Data Service plus
Claude.

## 2. Background — what's already proven

A standalone test script (`test_tableau_access.py`) has already validated the
following against a real Tableau Cloud site. **Reuse this logic rather than
rewriting it from scratch:**

- Signing in via REST API using a Personal Access Token (PAT):
  `POST /api/{version}/auth/signin`
- Listing all data sources visible to that token:
  `GET /api/{version}/sites/{siteId}/datasources`
- Reading field metadata for a chosen data source via VizQL Data Service:
  `POST /api/v1/vizql-data-service/read-metadata`
- Running an aggregation query against a data source via VizQL Data Service:
  `POST /api/v1/vizql-data-service/query-datasource`

### Known environment gotchas — already solved once, carry them forward

- **Corporate SSL inspection** can break `requests`' certificate verification
  on managed Windows machines (`CERTIFICATE_VERIFY_FAILED`). Don't disable
  verification by default — document the `pip-system-certs` (or
  `python-certifi-win32`) fix in the README instead.
- **Windows env vars set via `set VAR="value"` include the literal quote
  characters** in the value. Strip surrounding quotes and whitespace from
  every credential read from the environment (see `_clean()` in the test
  script — port it as-is).
- **The name shown on a data source's tile in the Tableau UI isn't always the
  literal `name` field the API expects** (e.g., underscores rendered as
  spaces in the UI). Any "find by name" logic must fall back to listing
  everything and doing a loose match, not assume an exact filter will hit.
- **A token only sees data its own account has permission on.** A data
  source being visible to a human browsing the site does not guarantee the
  token backing this app can see or query it. Treat an empty or
  shorter-than-expected data source list as a permissions question, not a
  bug to fix in code.
- **Not every field can be aggregated the same way.** Field metadata
  includes a `dataType` (`INTEGER`, `REAL`, `STRING`, `DATE`, etc.) — only
  offer numeric aggregation (SUM/AVG) on numeric fields; treat string/date
  fields as filter or group-by candidates instead.

## 3. Architecture

```
┌──────────────────┐      ┌───────────────────────┐      ┌──────────────────────────┐
│   Streamlit UI    │─────▶│  Orchestrator (app)    │─────▶│   Claude (Messages API)   │
│  picker + chat    │◀─────│   Python backend       │◀─────│   tool use / structured   │
└──────────────────┘      └──────────┬─────────────┘      └──────────────────────────┘
                                       │
                                       ▼
                           ┌───────────────────────┐
                           │  Tableau REST API +    │
                           │  VizQL Data Service    │
                           └───────────────────────┘
```

No MCP server in this first version — call the Tableau REST/VizQL endpoints
directly from Python, same as the test script did. MCP can replace this layer
later as a drop-in without changing the Streamlit UI.

## 4. Functional requirements

### 4.1 Startup / data source selection
- On app load, sign in to Tableau using the PAT from environment variables.
- List all data sources visible to the token.
- Render them in a Streamlit sidebar `st.selectbox`, showing the **literal**
  `name` field returned by the API — not a reformatted/guessed version.
- On selection, call `read-metadata` for that data source and cache the
  field list (name + `dataType`) in `st.session_state`. Refetch only when
  the selected data source changes.

### 4.2 Chat interface
- Standard `st.chat_message` / `st.chat_input` loop.
- Maintain conversation history in `st.session_state.messages`.
- On each user message:
  1. Send the question, the cached field list for the *currently selected*
     data source, and recent chat history to Claude.
  2. Claude decides which field(s), aggregation, and filter(s) answer the
     question, and returns them as structured tool input (see 4.3).
  3. Backend runs that structured query against VizQL Data Service.
  4. Send the raw result back to Claude with the original question, asking
     for a short plain-language answer.
  5. Render Claude's answer in the chat, plus the raw result as a small
     `st.dataframe` underneath for transparency/debugging.

### 4.3 NL → query translation
Use Claude's tool use (function calling) — do not hand-write an NL parser.

- Define one tool, e.g.:
  `query_datasource(fields: list[{name, aggregation}], filters: list[{field, operator, value}])`
- Pass the cached field list (name + dataType) to Claude as context so it
  only ever references real fields in the *currently selected* data source.
- If Claude's chosen aggregation doesn't match the field's dataType (e.g.
  SUM on a STRING field), catch this before calling the API and ask Claude
  to retry rather than sending a request guaranteed to fail.

### 4.4 Error handling
- **401 on sign-in:** show a clear "credentials rejected" message; don't
  retry silently.
- **Empty data source list:** show "this token has no visible data sources —
  check its permissions on the Tableau site," not a blank picker.
- **Query returns zero rows:** tell the user plainly; don't let Claude
  invent an explanation for missing data.
- **VizQL Data Service error response:** surface Tableau's actual error text
  in an `st.expander`, not a generic "something went wrong."

## 5. Non-functional requirements

- **Secrets:** PAT name/secret and the Anthropic API key are loaded from
  environment variables via `python-dotenv` and a local `.env` file (add
  `.env` to `.gitignore`). Never logged, never rendered in the UI, never
  included in any prompt sent to Claude.
- **Reuse, don't rewrite:** port `sign_in`, `find_datasource` (with its
  list-and-match fallback), and `read_metadata` from
  `test_tableau_access.py` into a `tableau_client.py` module with minimal
  changes.
- **Nothing hardcoded to one data source.** The point of this version is
  "connect to any of these datasets" — no data source name, field name, or
  schema assumption should be hardcoded anywhere in the app.
- **Session-scoped only.** No persistent storage of chat history or query
  results across app restarts in this version.

## 6. Suggested file structure

```
tableau-chat-agent/
├── app.py               # Streamlit entry point — UI + orchestration loop
├── tableau_client.py     # sign_in / list_datasources / read_metadata / query_datasource
├── llm_agent.py          # Claude client, tool definition, translation + synthesis calls
├── requirements.txt
├── .env.example          # TABLEAU_SERVER, TABLEAU_SITE, TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET, ANTHROPIC_API_KEY
└── README.md             # setup steps, including the SSL/corporate-proxy fix
```

## 7. Dependencies

```
streamlit
requests
python-dotenv
anthropic
```

## 8. Acceptance criteria

- [ ] App starts, signs in, and populates the data source picker with the
      real literal names (matching what `test_tableau_access.py` already
      printed for this token).
- [ ] Switching the selected data source updates the cached field list
      without a full app restart.
- [ ] Asking a simple aggregate question ("what's the total sales") against
      the Superstore Datasource returns a numerically correct answer.
- [ ] Asking about a field that doesn't exist in the selected data source
      produces a clear "that field isn't available in this dataset"
      response — never a crash or a hallucinated number.
- [ ] Switching to a different data source and re-asking the same question
      either answers using that dataset's real fields or clearly says the
      field doesn't exist there — never silently reuses the previous
      dataset's schema.
- [ ] No secret (PAT or Anthropic key) ever appears in a `print` statement,
      a Streamlit error message, or committed code.

## 9. Explicitly out of scope for this version

- MCP server integration (a planned follow-up, not a blocker here)
- Per-user OAuth / row-level security (current auth is a single shared PAT)
- Chart/visualization rendering of results (table output only for now)
- Deployment or hosting configuration
- Embedding this as a Tableau Dashboard Extension — this version is a
  standalone Streamlit app for validating the concept first