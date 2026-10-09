"""
app.py
======

Streamlit entry point for the Tableau Conversational Query Agent (see
specs/Spec.md section 4, and specs/design.md for the "Field Ledger" visual
design this module implements). This module owns the UI and orchestration
only -- all Tableau REST/VizQL calls live in `tableau_client.py` and all LLM
calls live in `llm_agent.py`. Nothing here talks to `requests` or an LLM SDK
directly, and nothing here is hardcoded to a specific data source or field.

st.session_state keys used by this module:
    tableau_session        -- cached dict from tableau_client.sign_in(),
                               set once per session so sign-in never repeats
                               on a rerun.
    datasources             -- cached list from tableau_client.list_datasources(),
                               set once per session.
    selected_datasource     -- the sidebar st.selectbox's own state (its
                               literal API `name`); Streamlit persists this
                               across reruns because it is the widget key.
    field_metadata_cache    -- dict[str, list[dict]] mapping a data source's
                               literal `name` to its cached
                               get_datasource_fields() result. Refetched only
                               when a name is missing from this dict, so
                               switching back to a previously-seen data
                               source is free.
    messages                -- list of chat turn dicts:
                               {"role": "user"|"assistant", "content": str,
                                "dataframe": Optional[list[dict]],
                                "error_detail": Optional[str],
                                "headline": Optional[str],
                                "elapsed_seconds": Optional[float],
                                "citation": Optional[str],
                                "query_fields": Optional[list[dict]],
                                "datasource": Optional[str]}
                               `dataframe` holds the raw VizQL result rows to
                               redisplay under an assistant answer.
                               `error_detail` holds Tableau's verbatim error
                               text to redisplay inside an st.expander.
                               `headline` holds a pre-formatted large number
                               string for a pure scalar-aggregate result (1
                               row, 1 column); None otherwise.
                               `elapsed_seconds`/`citation` hold this turn's
                               real wall-clock round-trip time and a factual
                               "what was queried" receipt string, built from
                               the structured query already returned by
                               llm_agent.translate_question -- set only on
                               the message that actually carries a
                               `dataframe`.
                               `query_fields` holds the exact
                               `{"name", "aggregation"}` list sent to
                               tableau_client.run_query for this turn, kept
                               so historical re-renders can classify result
                               columns (dimension vs. numeric) correctly.
                               `datasource` holds the data source name this
                               turn was run against, so a later data source
                               switch doesn't break re-rendering of older
                               turns.
    chart_type_<idx>        -- one `st.selectbox` widget per assistant
                               message that has a chartable result, keyed by
                               that message's fixed index in `messages`.
                               This is plain Streamlit widget-key state (not
                               a dict we manage ourselves): Streamlit keeps
                               each entry's chart-type choice independent of
                               every other entry's, including across
                               reruns, because each key is unique per
                               message index.
    current_session_id     -- id (filename stem) of this conversation's
                               local save file under saved_chats/, or None
                               before the first turn. Set the first time a
                               turn is auto-saved (see chat_history.py);
                               every later turn overwrites that same file
                               in place rather than creating a new one, so
                               one conversation = one file. Reset to None
                               by "Clear chat" / "Load" so the next turn
                               starts (or resumes) the right file.

After each turn, the current conversation is auto-saved to a local JSON
file (chat_history.py / saved_chats/) -- no manual save step. The sidebar's
"Chat history" expander lists those files and can load one back into
`messages` or delete it. "Clear chat" just starts a new conversation (the
old one is already saved). Asking a question also scrolls the page to the
newest turn via a small injected script (`st.html(..., unsafe_allow_javascript=True)`)
rather than a boxed/fixed-height chat area, to keep the full-page "ledger"
layout from design.md intact.
"""

import html
import os
import re
import time

import plotly.graph_objects as go
import streamlit as st

import chat_history as chat_archive  # aliased: "chat_history" is already used
# below as a local variable name for the LLM's recent-turns context list.
import llm_agent
import tableau_client

st.set_page_config(page_title="Tableau Conversational Query Agent", page_icon="\U0001F4CA")

MAX_HISTORY_TURNS = 10

# ---------------------------------------------------------------------------
# "Field Ledger" design tokens (specs/design.md section 3).
# ---------------------------------------------------------------------------

PALETTE = {
    "ink": "#161B22",
    "paper": "#F5F6F3",
    "petrol": "#1F6F73",
    "petrol_dim": "#16565A",
    "ochre": "#C08A2E",
    "brick": "#A6443C",
    "slate": "#6B7280",
}

# Primary/secondary chart series colors (design.md 7.1) -- extended with a
# couple more palette-consistent tones so a 3rd/4th numeric series still
# never falls back to Plotly's default categorical cycle.
_SERIES_COLORS = [PALETTE["petrol"], PALETTE["ochre"], PALETTE["petrol_dim"], PALETTE["slate"]]

_NUMERIC_DATA_TYPES = {"INTEGER", "REAL"}
_DIMENSION_DATA_TYPES = {"STRING", "DATE", "DATETIME", "BOOLEAN"}
_ALWAYS_NUMERIC_AGGREGATIONS = {"SUM", "AVG", "COUNT", "COUNTD", "MEDIAN", "STDEV", "VAR"}


def _inject_custom_css():
    # NOTE: everything from <style> to </style> below must stay as one
    # unbroken block of lines (no blank lines in between). Streamlit's
    # Markdown renderer treats a run of raw-HTML-looking lines as a single
    # passthrough block only until the first blank line; a blank line
    # inside what looks like one <style> block can make the renderer treat
    # the remainder as literal Markdown prose (visible CSS text on the
    # page) instead of an actual stylesheet. The blank line right before
    # <style> is deliberate and safe -- it cleanly ends the separate
    # <link> block first, so <style> starts its own fresh block.
    css = f"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap" rel="stylesheet">

<style>
html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {{
    font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}
[data-testid="stAppViewContainer"] {{
    background-color: {PALETTE['paper']};
    color: {PALETTE['ink']};
}}
/* --- Sidebar: ink background, paper/slate text (design.md 5, Sidebar) --- */
[data-testid="stSidebar"] {{
    background-color: {PALETTE['ink']};
}}
[data-testid="stSidebar"] * {{
    color: {PALETTE['paper']};
}}
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {{
    color: {PALETTE['paper']};
    font-size: 0.8125rem;
}}
[data-testid="stSidebar"] hr {{
    border-color: rgba(245, 246, 243, 0.2);
}}
/* Sidebar compact field-list reference: name in sans, dataType in mono+slate. */
table.sidebar-field-list {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
}}
table.sidebar-field-list td {{
    padding: 0.2rem 0;
    border-bottom: 1px solid rgba(245, 246, 243, 0.12);
}}
[data-testid="stSidebar"] table.sidebar-field-list td.field-name {{
    font-family: 'IBM Plex Sans', sans-serif;
    color: {PALETTE['paper']} !important;
    text-align: left;
}}
[data-testid="stSidebar"] table.sidebar-field-list td.field-type {{
    font-family: 'IBM Plex Mono', monospace;
    color: {PALETTE['slate']} !important;
    text-align: right;
    padding-left: 1rem;
    white-space: nowrap;
}}
/* --- Chat thread: ledger entries, not bubbles (design.md 5, Chat thread) --- */
[data-testid="stChatMessage"] {{
    background: transparent;
    border-radius: 0;
    border-left: 2px solid {PALETTE['slate']};
    border-bottom: 1px solid rgba(107, 114, 128, 0.18);
    padding: 0.25rem 0 0.9rem 1rem;
    margin-bottom: 0.9rem;
    box-shadow: none;
}}
/* User/assistant avatar badges (design.md 5, Chat thread) -- small, flat,
   on-brand squares rather than Streamlit's raw circular default. Icon glyph
   color set via `color` since Material Symbols render as a colorable text
   glyph, not an svg fill. */
[data-testid="stChatMessageAvatarUser"],
[data-testid="stChatMessageAvatarAssistant"] {{
    border-radius: 4px;
    width: 1.85rem;
    height: 1.85rem;
    box-shadow: none;
}}
[data-testid="stChatMessageAvatarUser"] {{
    background-color: {PALETTE['slate']} !important;
}}
[data-testid="stChatMessageAvatarAssistant"] {{
    background-color: {PALETTE['petrol_dim']} !important;
}}
[data-testid="stChatMessageAvatarUser"] [data-testid="stIconMaterial"],
[data-testid="stChatMessageAvatarAssistant"] [data-testid="stIconMaterial"] {{
    color: {PALETTE['paper']} !important;
    font-size: 1.05rem;
}}
/* Headline answer number (design.md 4, 5). */
.field-ledger-answer {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 2rem;
    font-weight: 700;
    color: {PALETTE['ink']};
    line-height: 1.15;
    margin-bottom: 0.15rem;
}}
/* Question text / explanatory sentence -- same ~1rem sans-ink treatment. */
.field-ledger-text {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 1rem;
    color: {PALETTE['ink']};
    margin-bottom: 0.4rem;
}}
/* Time-taken / citation receipt lines (design.md 7.2) -- kept as two
   separate small lines, never middot-joined into one string. */
.field-ledger-meta {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.75rem;
    color: {PALETTE['slate']};
    margin-top: 0.3rem;
}}
/* Empty-state invitation (design.md 5, Empty state). */
.field-ledger-empty-state {{
    font-family: 'IBM Plex Sans', sans-serif;
    color: {PALETTE['slate']};
    font-size: 1rem;
    border-left: 2px solid {PALETTE['slate']};
    padding-left: 1rem;
    margin: 1rem 0 1.5rem 0;
}}
/* --- Results table (design.md 5, Results table) --- */
table.field-ledger-table {{
    width: 100%;
    border-collapse: collapse;
    margin: 0.4rem 0 0.6rem 0;
    font-size: 0.875rem;
}}
table.field-ledger-table th {{
    text-align: left;
    font-family: 'IBM Plex Sans', sans-serif;
    font-weight: 500;
    color: {PALETTE['slate']};
    border-bottom: 1px solid rgba(107, 114, 128, 0.35);
    padding: 0.35rem 0.6rem;
}}
table.field-ledger-table th.num,
table.field-ledger-table td.num {{
    text-align: right;
}}
table.field-ledger-table td {{
    padding: 0.3rem 0.6rem;
    border-bottom: 1px solid rgba(107, 114, 128, 0.15);
}}
table.field-ledger-table td.num {{
    font-family: 'IBM Plex Mono', monospace;
    color: {PALETTE['ink']};
}}
table.field-ledger-table td.txt {{
    font-family: 'IBM Plex Sans', sans-serif;
    color: {PALETTE['ink']};
}}
/* Per-result chart-type control (design.md 7.1) -- small, sans, slate,
   sidebar-field-label styling, narrow so it never reads as a global
   setting. Matched by substring since each message gets its own
   st.selectbox key ("chart_type_<idx>") and therefore its own
   "st-key-chart_type_<idx>" wrapper class. */
[class*="st-key-chart_type_"] {{
    max-width: 220px;
}}
[class*="st-key-chart_type_"] [data-testid="stWidgetLabel"] p {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.8125rem;
    color: {PALETTE['slate']};
}}
/* --- Expanders (main-content "Tableau error details" + sidebar "Fields in
   '...'") -- explicit contrast rather than inheriting secondaryBackgroundColor,
   which is a light "secondary surface" tone shared by widgets everywhere,
   not just the sidebar. Main-content default: paper bg, ink text. --- */
[data-testid="stExpander"] {{
    background-color: {PALETTE['paper']} !important;
    border: 1px solid rgba(107, 114, 128, 0.35) !important;
    border-radius: 4px;
}}
[data-testid="stExpander"] summary {{
    color: {PALETTE['ink']} !important;
    font-family: 'IBM Plex Sans', sans-serif;
}}
[data-testid="stExpanderDetails"] {{
    background-color: {PALETTE['paper']} !important;
    color: {PALETTE['ink']} !important;
}}
/* Sidebar expander sits on the ink background -- keep it a dark, inset
   panel (paper text already applies via the blanket sidebar-text rule
   above) instead of inheriting the now-light secondaryBackgroundColor,
   which would otherwise paint a light patch with paper-on-light text. */
[data-testid="stSidebar"] [data-testid="stExpander"] {{
    background-color: rgba(245, 246, 243, 0.05) !important;
    border: 1px solid rgba(245, 246, 243, 0.25) !important;
}}
/* The global [data-testid="stExpander"] summary / stExpanderDetails rules
   above force ink text (correct for the main-content error-details
   expander, which sits on paper) -- both have higher specificity than the
   blanket sidebar paper-text rule, so without this explicit re-override
   the sidebar's "Fields in '...'" expander header (and any future content
   in its body besides the field table, which already has its own
   dedicated override) would render ink-on-ink against this dark panel. */
[data-testid="stSidebar"] [data-testid="stExpander"] summary {{
    color: {PALETTE['paper']} !important;
}}
[data-testid="stSidebar"] [data-testid="stExpanderDetails"] {{
    background-color: transparent !important;
    color: {PALETTE['paper']} !important;
}}
/* --- st.code blocks (sanitized Tableau error text) -- explicit light
   surface + ink text + mono, independent of secondaryBackgroundColor
   (kept equal to the config.toml value here so it stays visually
   consistent with any Streamlit-default secondary surfaces this CSS
   doesn't otherwise touch). --- */
[data-testid="stCode"] {{
    background-color: #E6E8E1 !important;
    border: 1px solid rgba(107, 114, 128, 0.3) !important;
    border-radius: 4px;
}}
[data-testid="stCode"] code,
[data-testid="stCode"] pre,
[data-testid="stCode"] * {{
    color: {PALETTE['ink']} !important;
    font-family: 'IBM Plex Mono', monospace !important;
    background-color: transparent !important;
}}
/* --- Alerts (st.error / st.warning) -- each call site is wrapped in its
   own keyed st.container(key=...) in app.py so it gets a stable
   "st-key-<key>" wrapper class; st.error and st.warning otherwise share the
   same data-testid ("stAlert"), so a plain kind-based CSS selector isn't
   reliable. Opaque paper background + a colored left rule + colored icon --
   never a translucent tint, since these alerts can sit on either the paper
   main background or the ink sidebar background, and a translucent tint
   would read differently (and sometimes unreadably) on each. --- */
[class*="st-key-ledger-alert-critical-error"] [data-testid="stAlert"],
[class*="st-key-ledger-alert-sidebar-error"] [data-testid="stAlert"] {{
    background-color: {PALETTE['paper']} !important;
    border: 1px solid {PALETTE['brick']} !important;
    border-left: 3px solid {PALETTE['brick']} !important;
    border-radius: 2px;
}}
[class*="st-key-ledger-alert-critical-error"] [data-testid="stAlert"] *,
[class*="st-key-ledger-alert-sidebar-error"] [data-testid="stAlert"] * {{
    color: {PALETTE['ink']} !important;
}}
[class*="st-key-ledger-alert-critical-error"] [data-testid="stAlertDynamicIcon"] *,
[class*="st-key-ledger-alert-sidebar-error"] [data-testid="stAlertDynamicIcon"] * {{
    color: {PALETTE['brick']} !important;
}}
[class*="st-key-ledger-alert-sidebar-warning"] [data-testid="stAlert"],
[class*="st-key-ledger-alert-noquery-warning"] [data-testid="stAlert"] {{
    background-color: {PALETTE['paper']} !important;
    border: 1px solid {PALETTE['ochre']} !important;
    border-left: 3px solid {PALETTE['ochre']} !important;
    border-radius: 2px;
}}
[class*="st-key-ledger-alert-sidebar-warning"] [data-testid="stAlert"] *,
[class*="st-key-ledger-alert-noquery-warning"] [data-testid="stAlert"] * {{
    color: {PALETTE['ink']} !important;
}}
[class*="st-key-ledger-alert-sidebar-warning"] [data-testid="stAlertDynamicIcon"] *,
[class*="st-key-ledger-alert-noquery-warning"] [data-testid="stAlertDynamicIcon"] * {{
    color: {PALETTE['ochre']} !important;
}}
/* --- Sidebar data-source st.selectbox -- the closed control's fill comes
   from secondaryBackgroundColor (now a light tone), which would otherwise
   sit under the blanket paper-text sidebar rule as light-on-light. Reset
   every descendant to transparent and paint one deliberate dark fill on
   the control itself so it reads as part of the ink sidebar, not a light
   patch inside it. --- */
[data-testid="stSidebar"] [data-testid="stSelectbox"] * {{
    background-color: transparent !important;
}}
[data-testid="stSidebar"] [data-testid="stSelectbox"] {{
    background-color: rgba(245, 246, 243, 0.08) !important;
    border: 1px solid rgba(245, 246, 243, 0.3) !important;
    border-radius: 4px;
}}
/* The selectbox's dropdown popover is a floating element portalled to the
   document root, not nested under [data-testid="stSidebar"] -- it needs
   its own top-level selector rather than a "stSidebar *" descendant rule.
   Styled to match the ink/paper/petrol brand rather than left as
   Streamlit's default light popover. */
[data-testid="stSelectboxVirtualDropdown"] {{
    background-color: {PALETTE['ink']} !important;
    border: 1px solid rgba(245, 246, 243, 0.2) !important;
    border-radius: 4px;
}}
[data-testid="stSelectboxVirtualDropdown"] * {{
    color: {PALETTE['paper']} !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
    background-color: transparent !important;
}}
[data-testid="stSelectboxVirtualDropdown"] [role="option"]:hover,
[data-testid="stSelectboxVirtualDropdown"] [role="option"][data-hovered="true"],
[data-testid="stSelectboxVirtualDropdown"] [role="option"][aria-selected="true"] {{
    background-color: {PALETTE['petrol_dim']} !important;
}}
/* --- st.chat_input -- deliberate on-brand styling: paper surface, ink
   text, petrol focus ring, instead of Streamlit's fully default chrome. --- */
[data-testid="stChatInput"] {{
    background-color: {PALETTE['paper']} !important;
    border: 1px solid {PALETTE['slate']} !important;
    border-radius: 6px;
}}
[data-testid="stChatInput"]:focus-within {{
    border-color: {PALETTE['petrol']} !important;
    box-shadow: 0 0 0 1px {PALETTE['petrol']} !important;
}}
[data-testid="stChatInputTextArea"] {{
    color: {PALETTE['ink']} !important;
    background-color: transparent !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
}}
[data-testid="stChatInputSubmitButton"] [data-testid="stIconMaterial"] {{
    color: {PALETTE['petrol']} !important;
}}
/* --- Sidebar buttons ("Clear chat", history row actions) -- same fix as
   the selectbox above: a secondary button's default fill is now the light
   secondaryBackgroundColor, which would sit under the blanket sidebar
   paper-text rule as near-invisible light-on-light. Reset to a deliberate
   translucent-paper fill on ink, petrol on hover -- same treatment as the
   selectbox so every sidebar control reads as one family. --- */
[data-testid="stSidebar"] [data-testid^="stBaseButton-secondary"] {{
    background-color: rgba(245, 246, 243, 0.08) !important;
    border: 1px solid rgba(245, 246, 243, 0.3) !important;
    color: {PALETTE['paper']} !important;
}}
[data-testid="stSidebar"] [data-testid^="stBaseButton-secondary"]:hover {{
    border-color: {PALETTE['petrol']} !important;
    color: {PALETTE['petrol']} !important;
}}
[data-testid="stSidebar"] [data-testid^="stBaseButton-secondary"] p {{
    color: inherit !important;
}}
/* Saved-chat history rows (sidebar "Chat history" expander). */
.ledger-history-meta {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem;
    color: rgba(245, 246, 243, 0.55);
}}
.ledger-history-preview {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.8125rem;
    color: {PALETTE['paper']};
    margin: 0.1rem 0 0.35rem 0;
    overflow-wrap: anywhere;
}}
</style>
"""
    st.markdown(css, unsafe_allow_html=True)


_inject_custom_css()


# ---------------------------------------------------------------------------
# Defensive secret sanitization
# ---------------------------------------------------------------------------
# tableau_client / llm_agent are documented to raise clean error text that
# never embeds a secret, but this is a last line of defense: if any
# exception string happens to contain the literal PAT secret, PAT name, or
# Azure API key currently loaded in the environment, redact it before it
# can ever reach st.error / st.expander / st.chat_message.

def _current_secret_values():
    values = []
    for var in (
        "TABLEAU_PAT_SECRET",
        "TABLEAU_PAT_NAME",
        "AZURE_OPENAI_API_KEY",
        "SNOWFLAKE_CONN_USERNAME",
        "SNOWFLAKE_CONN_PASSWORD",
    ):
        raw = os.getenv(var)
        if not raw:
            continue
        cleaned = raw.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("'", '"'):
            cleaned = cleaned[1:-1].strip()
        if cleaned:
            values.append(cleaned)
    return values


def sanitize(text) -> str:
    text = str(text)
    for secret in _current_secret_values():
        if secret and secret in text:
            text = text.replace(secret, "[REDACTED]")
    return text


# ---------------------------------------------------------------------------
# Startup: sign in once per session, then load the data source list once.
# ---------------------------------------------------------------------------

def ensure_signed_in():
    if "tableau_session" in st.session_state:
        return
    try:
        st.session_state["tableau_session"] = tableau_client.sign_in()
    except tableau_client.TableauConnectionError as exc:
        with st.container(key="ledger-alert-critical-error"):
            st.error(sanitize(exc.message))
        with st.expander("Technical details"):
            st.code(sanitize(exc.details))
        st.stop()
    except tableau_client.TableauAuthError:
        with st.container(key="ledger-alert-critical-error"):
            st.error(
                "Tableau didn't accept these credentials.  \n"
                "Check TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET, and SITE_NAME in "
                "your .env file, then restart the app. (Not retrying "
                "automatically.)"
            )
        st.stop()
    except tableau_client.TableauAPIError as exc:
        with st.container(key="ledger-alert-critical-error"):
            st.error("Tableau sign-in failed.")
        with st.expander("Tableau error details"):
            st.code(sanitize(exc))
        st.stop()
    except ValueError as exc:
        # Raised by tableau_client._get_env for a missing/blank env var.
        with st.container(key="ledger-alert-critical-error"):
            st.error(f"Configuration problem: {sanitize(exc)}")
        st.stop()


def ensure_datasources_loaded():
    if "datasources" in st.session_state:
        return
    try:
        st.session_state["datasources"] = tableau_client.list_datasources()
    except tableau_client.TableauConnectionError as exc:
        with st.container(key="ledger-alert-critical-error"):
            st.error(sanitize(exc.message))
        with st.expander("Technical details"):
            st.code(sanitize(exc.details))
        st.stop()
    except tableau_client.NoDatasourcesError:
        with st.container(key="ledger-alert-critical-error"):
            st.error(
                "This token can't see any data sources on this site.  \n"
                "This is almost always a permissions problem on the token "
                "owner's account (missing Explore/Connect permission on the "
                "relevant projects in Tableau), not a bug in this client."
            )
        st.stop()
    except tableau_client.TableauAuthError:
        with st.container(key="ledger-alert-critical-error"):
            st.error(
                "Tableau rejected the session while listing data sources. "
                "Check your credentials and restart the app."
            )
        st.stop()
    except tableau_client.TableauAPIError as exc:
        with st.container(key="ledger-alert-critical-error"):
            st.error("Could not list Tableau data sources.")
        with st.expander("Tableau error details"):
            st.code(sanitize(exc))
        st.stop()


ensure_signed_in()
ensure_datasources_loaded()

if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "field_metadata_cache" not in st.session_state:
    st.session_state["field_metadata_cache"] = {}
if "current_session_id" not in st.session_state:
    st.session_state["current_session_id"] = None


def _start_new_chat():
    """Reset to a blank conversation. Nothing to archive first -- the
    outgoing conversation (if any) is already saved turn-by-turn (see
    current_session_id / the end of the "if question:" block below)."""
    st.session_state["messages"] = []
    st.session_state["current_session_id"] = None
    for key in [k for k in st.session_state if k.startswith("chart_type_")]:
        del st.session_state[key]


# ---------------------------------------------------------------------------
# Sidebar: "New chat" pinned at the very top, above the data source picker,
# so starting fresh is always one click away without scrolling.
# ---------------------------------------------------------------------------

if st.sidebar.button(
    "New chat",
    icon=":material/add_comment:",
    type="primary",
    width="stretch",
    disabled=not st.session_state["messages"],
    key="new_chat_button_top",
    help="Starts a new conversation. This one is already saved to Chat history.",
):
    _start_new_chat()

st.sidebar.divider()


# ---------------------------------------------------------------------------
# Sidebar: data source picker + field metadata cache.
# ---------------------------------------------------------------------------

datasource_names = [ds["name"] for ds in st.session_state["datasources"]]

st.sidebar.header("Data source")
selected_datasource = st.sidebar.selectbox(
    "Choose a Tableau data source",
    datasource_names,
    key="selected_datasource",
)


def _vds_error_display(exc):
    """(headline, detail_text) for a VDS-level error (TableauAPIError or
    VizqlServiceError), per specs/snowflakepassthrough.md 4.3/5: `exc.hint`
    (when set) is the headline; Tableau's real status/code/message always
    goes in the detail, for an st.expander."""
    headline = getattr(exc, "hint", None) or "Tableau returned an error for this data source."
    detail_parts = []
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if status is not None:
        detail_parts.append(f"HTTP {status}")
    if code:
        detail_parts.append(f"Tableau code: {code}")
    detail_parts.append(sanitize(exc))
    return headline, "\n".join(detail_parts)


_selected_ds_record = next(
    (ds for ds in st.session_state["datasources"] if ds["name"] == selected_datasource), None
)
if _selected_ds_record is not None:
    try:
        _conn_status = tableau_client.describe_connection_status(_selected_ds_record["id"])
        if not _conn_status["live"]:
            _status_text = "Extract"
        elif _conn_status["creds_source"] == "app":
            _status_text = "Live · Snowflake · credentials: from app"
        else:
            _status_text = "Live · credentials embedded"
        st.sidebar.caption(_status_text)
    except (
        tableau_client.TableauConnectionError,
        tableau_client.TableauAuthError,
        tableau_client.TableauAPIError,
        tableau_client.VizqlServiceError,
    ):
        pass  # the fields-load error below already surfaces the real problem

fields_available = False
if selected_datasource not in st.session_state["field_metadata_cache"]:
    try:
        fields = tableau_client.get_datasource_fields(selected_datasource)
        st.session_state["field_metadata_cache"][selected_datasource] = fields
        fields_available = True
    except tableau_client.TableauConnectionError as exc:
        with st.sidebar.container(key="ledger-alert-sidebar-error"):
            st.error(sanitize(exc.message))
        with st.sidebar.expander("Technical details"):
            st.code(sanitize(exc.details))
    except tableau_client.DatasourceNotFoundError as exc:
        with st.sidebar.container(key="ledger-alert-sidebar-error"):
            st.error(sanitize(exc))
    except (tableau_client.VizqlServiceError, tableau_client.TableauAPIError) as exc:
        headline, detail = _vds_error_display(exc)
        with st.sidebar.container(key="ledger-alert-sidebar-error"):
            st.error(headline)
        with st.sidebar.expander("Tableau error details"):
            st.code(detail)
    except tableau_client.TableauAuthError:
        with st.sidebar.container(key="ledger-alert-sidebar-error"):
            st.error(
                "Tableau rejected the session while fetching field metadata. "
                "Restart the app to sign in again."
            )
else:
    fields_available = True

if fields_available:
    field_metadata = st.session_state["field_metadata_cache"][selected_datasource]
    rows_html = "".join(
        f'<tr><td class="field-name">{html.escape(f.get("name") or "")}</td>'
        f'<td class="field-type">{html.escape((f.get("dataType") or "").upper())}</td></tr>'
        for f in field_metadata
    )
    with st.sidebar.expander(f"Fields in '{selected_datasource}'", expanded=False):
        st.markdown(
            f'<table class="sidebar-field-list"><tbody>{rows_html}</tbody></table>',
            unsafe_allow_html=True,
        )
else:
    field_metadata = []
    with st.sidebar.container(key="ledger-alert-sidebar-warning"):
        st.warning("No field metadata is available for this data source yet.")


# ---------------------------------------------------------------------------
# Sidebar: clear chat + locally saved chat history (saved_chats/ on disk,
# see chat_history.py). Every turn auto-saves (see the end of the "if
# question:" block below), so there's nothing left to archive here --
# "Clear chat" just starts a new conversation.
# ---------------------------------------------------------------------------

st.sidebar.divider()
st.sidebar.header("Chat")

if st.sidebar.button(
    "Clear chat",
    icon=":material/delete_sweep:",
    width="stretch",
    disabled=not st.session_state["messages"],
    key="clear_chat_button",
    help="Starts a new conversation. This one is already saved to Chat history.",
):
    _start_new_chat()

with st.sidebar.expander("Chat history", icon=":material/history:"):
    saved_sessions = chat_archive.list_sessions()
    if not saved_sessions:
        st.caption("No saved conversations yet.")
    for session in saved_sessions:
        preview = session["preview"] or "(no question recorded)"
        if len(preview) > 80:
            preview = preview[:77] + "..."
        st.markdown(
            f'<div class="ledger-history-meta">{html.escape(session["saved_at"])}'
            f' &middot; {html.escape(session["datasource"])}'
            f' &middot; {session["message_count"]} messages</div>'
            f'<div class="ledger-history-preview">{html.escape(preview)}</div>',
            unsafe_allow_html=True,
        )
        with st.container(horizontal=True, wrap=False):
            if st.button("Load", icon=":material/open_in_new:", key=f"load_hist_{session['id']}"):
                loaded = chat_archive.load_session(session["id"])
                if loaded is not None:
                    # The active conversation (if any) is already saved
                    # under its own current_session_id -- nothing to
                    # archive, just switch. Keep this session's id so
                    # further turns continue updating this same file.
                    st.session_state["messages"] = loaded.get("messages") or []
                    st.session_state["current_session_id"] = session["id"]
            if st.button("Delete", icon=":material/delete:", key=f"delete_hist_{session['id']}"):
                chat_archive.delete_session(session["id"])
                if st.session_state.get("current_session_id") == session["id"]:
                    st.session_state["current_session_id"] = None
                st.rerun()
        st.divider()


# ---------------------------------------------------------------------------
# Formatting / classification helpers for results (design.md 5 "Results
# table" and 7.1 "Chart rendering").
# ---------------------------------------------------------------------------

def _sentence_case(label: str) -> str:
    """"REVENUE" -> "Revenue"; leaves already mixed-case labels (e.g. a
    fieldCaption like "SUM(Sales)") alone rather than mangling them."""
    if label and label.isupper():
        return label.capitalize()
    return label


def _format_cell(value, numeric: bool) -> str:
    if value is None:
        return "—"  # em dash
    if numeric and isinstance(value, bool):
        return str(value)
    if numeric and isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int):
            return f"{value:,}"
        return f"{value:,.2f}"
    return str(value)


def _format_headline(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return _format_cell(value, numeric=True)
    return str(value)


def _headline_for(dataframe):
    """A single scalar-aggregate result (1 row, 1 column) gets a large
    headline number (design.md 5, Chat thread). Anything else (multiple
    rows and/or multiple columns) has no single "the answer" value, so no
    headline is shown -- just the explanatory sentence and the table."""
    if dataframe and len(dataframe) == 1 and len(dataframe[0]) == 1:
        only_value = next(iter(dataframe[0].values()))
        return _format_headline(only_value)
    return None


def _is_numeric_column(column_key, field_metadata_for_col):
    """Best-effort numeric/text classification for a raw VDS result column
    key, matched against cached field metadata. VDS's exact column-key
    convention for an aggregated field ("Sales" vs. "SUM(Sales)") isn't
    pinned down anywhere in tableau_client's docstrings, so this tries an
    exact fieldCaption match, then an "AGG(fieldCaption)" convention, then
    falls back to sampling the actual Python value type."""
    type_by_name = {f["name"]: (f.get("dataType") or "").upper() for f in field_metadata_for_col}
    if column_key in type_by_name:
        return type_by_name[column_key] in _NUMERIC_DATA_TYPES
    for name, dtype in type_by_name.items():
        if not name:
            continue
        if column_key == name:
            return dtype in _NUMERIC_DATA_TYPES
        match = re.match(r"^([A-Za-z_]+)\((.+)\)$", column_key)
        if match and match.group(2) == name:
            agg = match.group(1).upper()
            return agg in _ALWAYS_NUMERIC_AGGREGATIONS or dtype in _NUMERIC_DATA_TYPES
    return None  # unknown -- caller should fall back to sampling the value


def _render_result_table(rows, field_metadata_for_result):
    if not rows:
        st.markdown(
            '<div class="field-ledger-text" style="color:#6B7280;">'
            "(zero rows)</div>",
            unsafe_allow_html=True,
        )
        return

    columns = list(rows[0].keys())

    def col_is_numeric(col):
        verdict = _is_numeric_column(col, field_metadata_for_result)
        if verdict is not None:
            return verdict
        sample = rows[0].get(col)
        return isinstance(sample, (int, float)) and not isinstance(sample, bool)

    numeric_flags = {c: col_is_numeric(c) for c in columns}

    header_cells = "".join(
        f'<th class="{"num" if numeric_flags[c] else "txt"}">{html.escape(_sentence_case(c))}</th>'
        for c in columns
    )
    body_rows = []
    for row in rows:
        cells = "".join(
            f'<td class="{"num" if numeric_flags[c] else "txt"}">'
            f'{html.escape(_format_cell(row.get(c), numeric_flags[c]))}</td>'
            for c in columns
        )
        body_rows.append(f"<tr>{cells}</tr>")

    table_html = (
        '<table class="field-ledger-table"><thead><tr>'
        f"{header_cells}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _classify_columns(rows, fields_spec, field_metadata_for_result):
    """Split a result's column keys into (dimension_keys, numeric_keys)
    using the cached field metadata's dataType per design.md 7.1 ("...
    from the field metadata already cached ..., not by guessing from the
    raw value types alone"). `fields_spec` is the exact
    {"name", "aggregation"} list this turn sent to run_query -- used (by
    name, then by an "AGG(Name)" convention, then positionally) to resolve
    a raw result column key back to its source field/aggregation, since
    VDS's exact column-naming convention isn't guaranteed."""
    if not rows:
        return [], []
    keys = list(rows[0].keys())
    fields_spec = fields_spec or []

    def spec_for(key, index):
        for spec in fields_spec:
            if spec.get("name") == key:
                return spec
        for spec in fields_spec:
            name = spec.get("name")
            agg = (spec.get("aggregation") or "").upper()
            if name and agg and agg != "NONE" and key == f"{agg}({name})":
                return spec
        if index < len(fields_spec):
            return fields_spec[index]
        return None

    type_by_name = {f["name"]: (f.get("dataType") or "").upper() for f in field_metadata_for_result}

    dimension_keys, numeric_keys = [], []
    for i, key in enumerate(keys):
        spec = spec_for(key, i) or {}
        name = spec.get("name")
        agg = (spec.get("aggregation") or "NONE").upper()
        dtype = type_by_name.get(name, "")
        if agg in _ALWAYS_NUMERIC_AGGREGATIONS:
            numeric_keys.append(key)
        elif dtype in _NUMERIC_DATA_TYPES:
            numeric_keys.append(key)
        elif dtype in _DIMENSION_DATA_TYPES:
            dimension_keys.append(key)
        else:
            sample = rows[0].get(key)
            if isinstance(sample, bool):
                dimension_keys.append(key)
            elif isinstance(sample, (int, float)):
                numeric_keys.append(key)
            else:
                dimension_keys.append(key)
    return dimension_keys, numeric_keys


def _build_chart(rows, dimension_keys, numeric_keys, chart_type):
    """A Plotly figure styled to the Field Ledger palette (design.md 7.1):
    paper background, ink/slate axis text and light gridlines, petrol as
    the primary series and ochre as the secondary -- never Plotly's default
    categorical color cycle."""
    x_key = dimension_keys[0]
    x_values = [row.get(x_key) for row in rows]

    fig = go.Figure()
    if chart_type == "Pie":
        y_key = numeric_keys[0]
        y_values = [row.get(y_key) for row in rows]
        colors = [_SERIES_COLORS[i % len(_SERIES_COLORS)] for i in range(len(x_values))]
        fig.add_trace(
            go.Pie(
                labels=x_values,
                values=y_values,
                marker=dict(colors=colors, line=dict(color=PALETTE["paper"], width=1)),
                textfont=dict(color=PALETTE["ink"]),
            )
        )
    else:
        for i, y_key in enumerate(numeric_keys):
            y_values = [row.get(y_key) for row in rows]
            color = _SERIES_COLORS[i % len(_SERIES_COLORS)]
            if chart_type == "Line":
                fig.add_trace(
                    go.Scatter(
                        x=x_values, y=y_values, mode="lines+markers", name=y_key,
                        line=dict(color=color), marker=dict(color=color),
                    )
                )
            elif chart_type == "Scatter":
                fig.add_trace(
                    go.Scatter(
                        x=x_values, y=y_values, mode="markers", name=y_key,
                        marker=dict(color=color, size=9),
                    )
                )
            else:  # "Bar" (default)
                fig.add_trace(go.Bar(x=x_values, y=y_values, name=y_key, marker=dict(color=color)))
        if chart_type == "Bar":
            fig.update_layout(barmode="group")

    fig.update_layout(
        paper_bgcolor=PALETTE["paper"],
        plot_bgcolor=PALETTE["paper"],
        font=dict(family="IBM Plex Sans, sans-serif", color=PALETTE["ink"], size=12),
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", font=dict(color=PALETTE["slate"]), bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(gridcolor="#DEE0DA", zerolinecolor="#DEE0DA", color=PALETTE["slate"], linecolor=PALETTE["slate"]),
        yaxis=dict(gridcolor="#DEE0DA", zerolinecolor="#DEE0DA", color=PALETTE["slate"], linecolor=PALETTE["slate"]),
    )
    return fig


# ---------------------------------------------------------------------------
# Citation (design.md 7.2) -- built only from the structured query app.py
# already has in hand, never reworded by the LLM and never inventing a
# field/filter that wasn't actually sent.
# ---------------------------------------------------------------------------

def _build_citation(datasource_name, translation):
    if not translation:
        return None
    fields = translation.get("fields") or []
    filters = translation.get("filters") or []

    aggregated, grouped = [], []
    for f in fields:
        name = f.get("name")
        if not name:
            continue
        agg = (f.get("aggregation") or "NONE").upper()
        if agg and agg != "NONE":
            aggregated.append(f"{agg}({name})")
        else:
            grouped.append(name)

    if not aggregated and not grouped:
        return f"{datasource_name} — (no fields)"

    body_parts = []
    if aggregated:
        body_parts.append(", ".join(aggregated))
    if grouped:
        body_parts.append(("by " if aggregated else "") + ", ".join(grouped))
    citation = f"{datasource_name} — " + " ".join(body_parts)

    if filters:
        filter_bits = []
        for flt in filters:
            fname = flt.get("field")
            op = flt.get("operator")
            value = flt.get("value")
            value_str = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
            filter_bits.append(f"{fname} {op} {value_str}")
        citation += " where " + "; ".join(filter_bits)

    return citation


# ---------------------------------------------------------------------------
# "Field doesn't exist" error humanization (design.md 5, Error states).
# ---------------------------------------------------------------------------

_FIELD_NOT_FOUND_RE = re.compile(r"^(?:Field|Filter field) '([^']+)' does not exist")


def _humanize_field_not_found(exc, field_metadata_for_lookup):
    """If `exc` is tableau_client's "field does not exist" ValueError,
    return design.md's exact-copy message plus 2-3 close field names found
    via simple substring/case-insensitive matching against the cached
    metadata. Returns None for any other ValueError so the caller's
    original, more general message is used instead."""
    match = _FIELD_NOT_FOUND_RE.match(str(exc))
    if not match:
        return None
    missing = match.group(1)
    lower_missing = missing.lower()
    candidates = [f.get("name") for f in field_metadata_for_lookup if f.get("name")]

    close = [name for name in candidates if lower_missing in name.lower() or name.lower() in lower_missing]
    if not close:
        tokens = [t for t in re.split(r"[\s_-]+", lower_missing) if t]
        close = [name for name in candidates if any(t and t in name.lower() for t in tokens)]
    close = close[:3]

    message = f"'{missing}' isn't a field in this data source."
    if close:
        message += f" Did you mean: {', '.join(close)}?"
    return message


# ---------------------------------------------------------------------------
# Empty-state invitation (design.md 5, Empty state) -- grounded in the
# actual selected data source's real fields, never a generic placeholder.
# ---------------------------------------------------------------------------

def _build_empty_state_invitation(datasource_name, field_metadata_for_ds):
    numeric_field = next(
        (f["name"] for f in field_metadata_for_ds if (f.get("dataType") or "").upper() in _NUMERIC_DATA_TYPES and f.get("name")),
        None,
    )
    non_numeric_field = next(
        (
            f["name"]
            for f in field_metadata_for_ds
            if (f.get("dataType") or "").upper() in _DIMENSION_DATA_TYPES
            and f.get("name")
            and f["name"] != numeric_field
        ),
        None,
    )

    if numeric_field and non_numeric_field:
        example = f"What's the total {numeric_field} by {non_numeric_field}?"
    elif numeric_field:
        example = f"What's the total {numeric_field}?"
    elif non_numeric_field:
        example = f"How many rows are there by {non_numeric_field}?"
    else:
        example = "What would you like to know?"

    return f"Ask something about '{datasource_name}.' Try: “{example}”"


# ---------------------------------------------------------------------------
# Chat history rendering.
# ---------------------------------------------------------------------------

st.title("Tableau Conversational Query Agent")
st.caption(f"Currently querying: **{selected_datasource}**")


# Restrained, professional chat-message avatars (design.md 5, Chat thread --
# this reverses the brief's original "no avatar" direction). Material
# Symbols icons rather than emoji or an image: a plain person icon for the
# user, an analysis/querying icon (not a cutesy bot) for the assistant.
_AVATARS = {
    "user": ":material/person:",
    "assistant": ":material/query_stats:",
}


def _render_message(message, idx, field_metadata_default):
    with st.chat_message(message["role"], avatar=_AVATARS.get(message["role"])):
        headline = message.get("headline")
        if message["role"] == "assistant" and headline:
            st.markdown(f'<div class="field-ledger-answer">{html.escape(headline)}</div>', unsafe_allow_html=True)

        content = message.get("content") or ""
        st.markdown(f'<div class="field-ledger-text">{html.escape(content)}</div>', unsafe_allow_html=True)

        dataframe = message.get("dataframe")
        if dataframe is not None:
            msg_field_metadata = st.session_state["field_metadata_cache"].get(
                message.get("datasource"), field_metadata_default
            )
            _render_result_table(dataframe, msg_field_metadata)

            if len(dataframe) >= 2:
                dimension_keys, numeric_keys = _classify_columns(
                    dataframe, message.get("query_fields") or [], msg_field_metadata
                )
                if dimension_keys and numeric_keys:
                    chart_type = st.selectbox(
                        "Chart type",
                        ["Bar", "Line", "Scatter", "Pie"],
                        key=f"chart_type_{idx}",
                    )
                    fig = _build_chart(dataframe, dimension_keys, numeric_keys, chart_type)
                    st.plotly_chart(fig, width="stretch", key=f"chart_{idx}")

        if message.get("error_detail"):
            with st.expander("Tableau error details"):
                st.code(message["error_detail"])

        # Time-taken + citation (design.md 7.2) -- two separate small lines,
        # never middot-joined, shown below this entry's result every time
        # it's redisplayed.
        elapsed_seconds = message.get("elapsed_seconds")
        if elapsed_seconds is not None:
            st.markdown(
                f'<div class="field-ledger-meta">Time taken: {elapsed_seconds:.2f}s</div>',
                unsafe_allow_html=True,
            )
        citation = message.get("citation")
        if citation:
            st.markdown(
                f'<div class="field-ledger-meta">Source: {html.escape(citation)}</div>',
                unsafe_allow_html=True,
            )


for idx, message in enumerate(st.session_state["messages"]):
    _render_message(message, idx, field_metadata)

if not st.session_state["messages"] and fields_available:
    st.markdown(
        f'<div class="field-ledger-empty-state">{html.escape(_build_empty_state_invitation(selected_datasource, field_metadata))}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Helpers for the chat turn below.
# ---------------------------------------------------------------------------

def _build_chat_history():
    """Recent conversation turns as OpenAI-style {"role", "content"} dicts,
    excluding the just-appended current user question."""
    history = []
    for m in st.session_state["messages"][:-1]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            history.append({"role": m["role"], "content": m["content"]})
    return history[-MAX_HISTORY_TURNS:]


def _append_and_render(
    role,
    content,
    field_metadata_for_render,
    dataframe=None,
    error_detail=None,
    headline=None,
    elapsed_seconds=None,
    citation=None,
    query_fields=None,
    datasource=None,
):
    entry = {
        "role": role,
        "content": content,
        "dataframe": dataframe,
        "error_detail": error_detail,
        "headline": headline,
        "elapsed_seconds": elapsed_seconds,
        "citation": citation,
        "query_fields": query_fields,
        "datasource": datasource,
    }
    st.session_state["messages"].append(entry)
    idx = len(st.session_state["messages"]) - 1
    _render_message(entry, idx, field_metadata_for_render)


# ---------------------------------------------------------------------------
# Chat input + orchestration loop (spec 4.2 / 4.3 / 4.4).
# ---------------------------------------------------------------------------

question = st.chat_input(
    "Ask a question about this data source..."
    if fields_available
    else "No field metadata available -- fix the sidebar error above first"
)

if question:
    if not fields_available:
        with st.container(key="ledger-alert-noquery-warning"):
            st.warning("Can't answer questions until field metadata for this data source loads.")
        st.stop()

    _append_and_render("user", question, field_metadata)

    chat_history = _build_chat_history()
    turn_start = time.perf_counter()

    with st.spinner("Querying Tableau…"):
        # --- Step 1: translate the question into a structured query. ---
        translation = None
        try:
            translation = llm_agent.translate_question(question, chat_history, field_metadata)
        except llm_agent.MissingCredentialError as exc:
            _append_and_render(
                "assistant",
                f"The assistant can't reach the LLM right now: {sanitize(exc)}",
                field_metadata,
            )
        except llm_agent.ToolCallValidationError as exc:
            _append_and_render(
                "assistant",
                "I couldn't turn that question into a valid query against "
                f"'{selected_datasource}'. {sanitize(exc)}",
                field_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort guard, never crash the UI
            _append_and_render(
                "assistant",
                f"Something went wrong translating that question: {sanitize(exc)}",
                field_metadata,
            )

        # --- Step 2: run the query against Tableau. ---
        query_result = None
        if translation is not None:
            run_fields = translation.get("fields", [])
            run_filters = translation.get("filters") or []
            try:
                query_result = tableau_client.run_query(selected_datasource, run_fields, run_filters)
            except ValueError as exc:
                humanized = _humanize_field_not_found(exc, field_metadata)
                if humanized:
                    _append_and_render("assistant", sanitize(humanized), field_metadata)
                else:
                    _append_and_render(
                        "assistant",
                        f"That question needs a field/aggregation combination this data source "
                        f"doesn't support: {sanitize(exc)}",
                        field_metadata,
                    )
            except tableau_client.DatasourceNotFoundError as exc:
                _append_and_render("assistant", sanitize(exc), field_metadata)
            except tableau_client.TableauConnectionError as exc:
                _append_and_render(
                    "assistant",
                    sanitize(exc.message),
                    field_metadata,
                    error_detail=sanitize(exc.details),
                )
            except tableau_client.TableauAuthError:
                _append_and_render(
                    "assistant",
                    "Tableau rejected the session while running this query. Restart the app to sign in again.",
                    field_metadata,
                )
            except (tableau_client.VizqlServiceError, tableau_client.TableauAPIError) as exc:
                headline, detail = _vds_error_display(exc)
                _append_and_render(
                    "assistant",
                    headline,
                    field_metadata,
                    error_detail=detail,
                )
            except Exception as exc:  # noqa: BLE001
                _append_and_render(
                    "assistant",
                    f"Something went wrong running that query: {sanitize(exc)}",
                    field_metadata,
                )

        # --- Step 3: zero rows -> say so plainly, never let the LLM invent an explanation. ---
        if query_result is not None and len(query_result) == 0:
            elapsed = time.perf_counter() - turn_start
            citation = _build_citation(selected_datasource, translation)
            _append_and_render(
                "assistant",
                "That query returned no rows. There's no matching data "
                f"in '{selected_datasource}' for this question.",
                field_metadata,
                dataframe=[],
                elapsed_seconds=elapsed,
                citation=citation,
                query_fields=translation.get("fields") if translation else None,
                datasource=selected_datasource,
            )
            query_result = None  # already handled, skip synthesis below

        # --- Step 4: synthesize a plain-language answer + show the raw result. ---
        if query_result:
            run_fields_for_meta = translation.get("fields") if translation else None
            try:
                answer = llm_agent.synthesize_answer(question, query_result)
                elapsed = time.perf_counter() - turn_start
                citation = _build_citation(selected_datasource, translation)
                _append_and_render(
                    "assistant",
                    answer,
                    field_metadata,
                    dataframe=query_result,
                    headline=_headline_for(query_result),
                    elapsed_seconds=elapsed,
                    citation=citation,
                    query_fields=run_fields_for_meta,
                    datasource=selected_datasource,
                )
            except llm_agent.MissingCredentialError as exc:
                elapsed = time.perf_counter() - turn_start
                citation = _build_citation(selected_datasource, translation)
                _append_and_render(
                    "assistant",
                    f"The query worked, but the assistant can't reach the LLM to summarize it: {sanitize(exc)}",
                    field_metadata,
                    dataframe=query_result,
                    headline=_headline_for(query_result),
                    elapsed_seconds=elapsed,
                    citation=citation,
                    query_fields=run_fields_for_meta,
                    datasource=selected_datasource,
                )
            except Exception as exc:  # noqa: BLE001
                elapsed = time.perf_counter() - turn_start
                citation = _build_citation(selected_datasource, translation)
                _append_and_render(
                    "assistant",
                    f"The query worked, but summarizing the result failed: {sanitize(exc)}",
                    field_metadata,
                    dataframe=query_result,
                    headline=_headline_for(query_result),
                    elapsed_seconds=elapsed,
                    citation=citation,
                    query_fields=run_fields_for_meta,
                    datasource=selected_datasource,
                )

    # --- Auto-save this turn locally (no manual "save" step -- see
    # chat_history.py / saved_chats/ and current_session_id above). ---
    st.session_state["current_session_id"] = chat_archive.save_session(
        st.session_state["messages"],
        selected_datasource,
        st.session_state["current_session_id"],
    )

    # --- Scroll the page to this turn's answer. The full-page "ledger"
    # layout (design.md) has no boxed/fixed-height chat area to auto-scroll
    # natively, so a tiny injected script does it instead; scoped to this
    # "if question:" branch so it only fires right after a new turn, never
    # on an unrelated rerun (switching a chart type, Clear chat, Load). ---
    st.html(
        '<div id="ledger-scroll-anchor"></div>'
        "<script>"
        "(function () {"
        "  var el = document.getElementById('ledger-scroll-anchor');"
        "  if (el) { el.scrollIntoView({behavior: 'smooth', block: 'end'}); }"
        "})();"
        "</script>",
        unsafe_allow_javascript=True,
    )
