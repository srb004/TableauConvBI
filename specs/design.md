# Design Brief — "Field Ledger"

For the Tableau Conversational Query Agent. This is a styling pass on top
of the already-working app — it changes `app.py`'s presentation and adds
`.streamlit/config.toml`. It must not touch the logic in
`tableau_client.py` or `llm_agent.py`.

## 1. Concept

This is an internal instrument for analysts who already trust their
Tableau dashboards and want the same numbers verified in plain language
mid-conversation — not a general-purpose AI assistant skin. The whole
design should read like a shared working ledger between a person and the
data: calm, legible, numerically confident. Every answer shows its
receipt (the raw query result) instead of asking to be trusted blindly.

## 2. Explicitly avoid

- Rounded chat bubbles or speech-bubble shapes
- An animated "thinking..." flourish
- A cartoonish or "cute" bot mascot avatar (a restrained, minimal
  user/assistant icon pairing is used instead -- see section 5, Chat
  thread -- this reverses this brief's original "no avatar" direction)
- Warm cream + terracotta (the generic "AI assistant" default palette)
- Drop-shadowed cards with the same border-radius on everything
- ALL CAPS section labels, middot-joined meta text, arrow-suffixed buttons

## 3. Color tokens

| Token | Hex | Use |
|---|---|---|
| ink | `#161B22` | Sidebar / header chrome (near-black graphite, not pure black) |
| paper | `#F5F6F3` | Main content background (cool, not the warm-cream default) |
| petrol | `#1F6F73` | Primary accent — selected data source, active states, links |
| petrol-dim | `#16565A` | Hover/pressed state of primary elements |
| ochre | `#C08A2E` | Secondary accent — highlighted values, flags. Used sparingly |
| brick | `#A6443C` | Errors and negative values only — never decorative |
| slate | `#6B7280` | Borders, secondary text, dividers |

Contrast check: ink on paper is very high contrast (safe for all body
text). petrol on paper is solid for large/bold text (the headline number,
buttons) but shouldn't be used for small body text — keep small text in
ink or slate.

## 4. Typography

- UI text: a precise, technical sans — IBM Plex Sans. Fall back to the
  system sans if the custom font isn't worth the setup cost right now.
- Data values only (the headline answer, table cells that are numbers): a
  monospace with tabular figures — IBM Plex Mono. This is functional, not
  decorative: it's what makes a column of numbers actually align in a tool
  whose entire job is showing correct numbers.

Type scale:

- Headline answer number: ~2rem, bold, mono, ink or petrol
- User question text: ~1rem, regular, sans, ink
- Assistant explanatory sentence: ~1rem, regular, sans, ink
- Table cells: ~0.875rem — mono + right-aligned for numbers, sans + left-aligned for text
- Sidebar field labels: ~0.8125rem, sans, slate

## 5. Component-level direction

### Sidebar

ink background, paper/slate text. Below the data source picker, show the
selected data source's field list as a compact reference — field name in
sans, its dataType in mono + slate, right-aligned like a small legend.
This does double duty: it's a status readout of what the picker selected,
and a hint for what's askable.

### Chat thread

No bubbles. Each exchange is a ledger entry, not a message:

- A small, restrained avatar badge marks who's speaking — a person icon
  for the user, an icon evoking analysis/querying (not a cutesy "bot")
  for the assistant. Flat, on-brand colors from the palette, no shadow,
  a soft (not fully circular) corner radius consistent with the rest of
  the ledger's shapes. This is a deliberate reversal of an earlier
  "no avatar" direction in this brief.
- User's question: sans, ink, with a thin left border in slate — like an
  entry heading, not a speech bubble.
- Assistant's answer: the headline number first, large and bold in mono,
  then one short explanatory sentence in sans beneath it, then the raw
  result table.
- Separate entries with a single hairline rule (1px, a light tint of
  slate) — not card boundaries, not extra whitespace alone.

### Results table

Plain, hairline row dividers, no zebra striping, no shadow. Numeric
columns right-aligned in mono; text columns left-aligned in sans. Header
row in sentence case (e.g. "Revenue," not "REVENUE") in slate.

### Empty state (before the first question)

Not a blank chat window. Show an invitation grounded in the actual
selected data source — e.g. "Ask something about '[data source name].'
Try: 'What's the total [a real field name] by [another real field]?'" —
built from that data source's real fields, never a generic placeholder
question.

### Error states

State what happened and what to check — no apology, no vagueness:

- Sign-in failure → "Tableau didn't accept these credentials."
- Empty data source list → "This token can't see any data sources on this site."
- Zero rows returned → "That query returned no rows."
- Field doesn't exist → "'[X]' isn't a field in this data source." — list
  2–3 close field names from the metadata if any are available.

### Loading state

A plain `st.spinner` with neutral copy — "Querying Tableau…" — no cute
copy, no character.

## 6. Streamlit implementation notes

Base theme — `.streamlit/config.toml`:

```toml
[theme]
base = "light"
backgroundColor = "#F5F6F3"
secondaryBackgroundColor = "#E6E8E1"
primaryColor = "#1F6F73"
textColor = "#161B22"
font = "sans serif"
```

Note: `secondaryBackgroundColor` must stay a light tone distinct from both
`paper` and `ink` -- it is Streamlit's background for the sidebar *and*
most widgets elsewhere (`st.expander`, `st.code`, `st.selectbox`, etc.),
so setting it equal to `ink`/`textColor` makes any of those widgets
outside the sidebar render ink-on-ink text (invisible). The sidebar's own
ink look is achieved independently via the explicit
`[data-testid="stSidebar"]` background-color override below, not via this
token.

Custom CSS for the ledger-entry look: Streamlit's `st.chat_message`
renders a default avatar + rounded bubble that needs overriding to get
the flat, hairline-divided look this brief calls for. The selectors below
are a starting point, not guaranteed exact for whatever Streamlit version
is installed — Streamlit's internal `data-testid` attributes shift
between releases, so inspect the actually-rendered page (e.g. browser dev
tools) and adjust selectors to match before assuming this CSS works
as-is:

```python
st.markdown("""
<style>
/* Remove the default chat bubble look */
[data-testid="stChatMessage"] {
    background: transparent;
    border-radius: 0;
    border-left: 2px solid #6B7280;
    padding-left: 1rem;
    box-shadow: none;
}
/* Restyle the default avatar badges (fed via st.chat_message(..., avatar=...))
   into small, flat, on-brand squares rather than leaving Streamlit's raw
   circular default -- see section 5, Chat thread, for the reversal of this
   brief's original "hide the avatar" direction. */
[data-testid="stChatMessageAvatarUser"],
[data-testid="stChatMessageAvatarAssistant"] {
    border-radius: 4px;
    box-shadow: none;
}
/* Headline number styling -- wrap the answer value in this class in app.py */
.field-ledger-answer {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 2rem;
    font-weight: 700;
    color: #161B22;
}
</style>
""", unsafe_allow_html=True)
```

If IBM Plex is worth the extra setup, load it via a Google Fonts `<link>`
in the same `st.markdown` block; otherwise ship without it for this pass
— the layout and color changes carry most of the effect on their own.

## 7. Additions beyond the original visual brief (same request, same pass)

These are functional additions, not purely visual, but ship in the same
styling pass since they were requested alongside it:

### 7.1 Chart rendering (Plotly)

- Add a chart of the raw query result underneath the result table (or
  interleaved with it), rendered with Plotly, for any result that
  actually has something chartable — a categorical/date dimension column
  plus one or more numeric columns, across 2+ rows. A single-row, single
  numeric-value result (a pure scalar aggregate, which is already shown
  as the large headline number) should not force a chart on the user;
  skip chart rendering for that case rather than plotting one meaningless
  point.
- Let the user change the chart type per result (e.g. bar / line /
  scatter / pie — table-only is always available since the raw table is
  always shown regardless). Use a small, unobtrusive control (a narrow
  `st.selectbox` or `st.radio`, sidebar-field-label styling: small, sans,
  slate) placed right above or beside that result's chart, not a global
  setting — each ledger entry keeps its own chart-type choice
  independently, including on rerun (this means the chart type choice
  needs a session_state key scoped per message/turn, not one shared
  widget key for the whole app).
- Determine axes/series from the field metadata already cached for the
  selected data source (dataType per column), not by guessing from the
  raw value types alone — a numeric column is a value/series, a
  STRING/DATE/DATETIME/BOOLEAN column is a category/x-axis candidate.
- Style the chart to match the palette and type in this brief: paper
  background, ink/slate axis text and gridlines (light, minimal — no
  default Plotly gridline heaviness), petrol as the primary series color,
  ochre as a secondary series color if there's more than one numeric
  series, brick reserved for negative-value emphasis only if that's a
  natural fit (not forced). IBM Plex Sans (or the same fallback as the
  rest of the UI) for chart text if practical via Plotly's font config.
  No default Plotly categorical color cycle (the default blues/oranges
  clash with this palette).

### 7.2 Response metadata: time taken + citation

Below each assistant ledger entry's result (table/chart), below the
bottom corner of that entry, show two small pieces of receipt-style
metadata in slate, small text (consistent with the "sidebar field
labels" scale, ~0.8125rem or smaller) — but per section 2's explicit
"avoid" list, do NOT middot-join these into one line:

- **Time taken**: wall-clock duration of that turn's full round trip
  (translate → query → synthesize), e.g. "0.84s". Measure this for real
  in `app.py` around the existing orchestration calls — don't fabricate a
  number.
- **Citation**: a short, factual receipt of what was actually queried —
  the data source name plus the fields/aggregations/filters that were
  sent to Tableau for this turn (derived directly from the structured
  query `app.py` already has in hand from `llm_agent.translate_question`,
  not reworded by the LLM and not invented). E.g. something like
  `Object Count — SUM(Sales) by Region`.

Keep these as two small, separate lines (or otherwise visually distinct
without a middot), not a decorative footer.
