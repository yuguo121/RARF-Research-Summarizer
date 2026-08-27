# RARF Research Summarizer

Summarizes PDFs from a Zotero folder into the Research Article Review Form (RARF) dimensions. Each paper becomes one row in an Excel overview; theory and methods are filled in separate Cursor Grok 4.6 High runs, then reconciled.

The pipeline never copies or modifies files in the Zotero library. It only reads PDFs recursively.

## Setup

```powershell
cd "C:\Users\HuGoL\OneDrive - HKUST (Guangzhou)\PhD\Zotero\RARF Research Summarizer"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy `.env.example` to `.env` and set `CURSOR_API_KEY`. The file is gitignored.
```

User API keys live at [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations).

`rarf-desk` (or `desk.cmd`) launches the local web desk as a standalone window; it is the same `rarf ui` server with browser auto-open.

Metadata now comes from Zotero, not the LLM. Point `zotero.export_path` in `config/settings.yaml` (or the Settings dialog in the desk) at a Zotero export (`File → Export Library… → Zotero JSON / CSL JSON`), or enable the Zotero Web API (`use_api: true`, `library_id`, and a `ZOTERO_API_KEY` in `.env`). When a PDF matches a Zotero item, author/year/DOI/journal/citation are filled directly; otherwise the PDF's own metadata is used.

Each row keeps its extraction provenance: the Excel overview and the desk show the scan batch (`Scan` column) and metadata source so you can tell which run produced a row.

## Commands

```powershell
# Extract, summarize, and export the default exemplar folder
rarf run

# Summarize only papers whose filename contains a token
rarf summarize --name-contains "Krause, Semadeni, Cannella_2014"

# Local desk: pick folders/PDFs, choose dimensions, edit prompts
rarf-desk
# or
desk.cmd

# Or point at another folder
rarf run "C:\Users\HuGoL\OneDrive - HKUST (Guangzhou)\PhD\Zotero\Corporate Governance\Internal"

# Steps separately
rarf extract PATH
rarf summarize PATH
rarf export
rarf sync-back
```

`summarize` is incremental: papers are cached by PDF hash plus schema, prompt, and model versions. Use `--force` to redo LLM runs. Use `--name-contains "Krause"` or `--limit 1` to summarize a subset of a large folder.

After editing cells in `RARF Overview`, run `rarf sync-back` so those edits are stored as human overrides and survive the next export.

## Output

`output/RARF_Overview.xlsx` contains:

- **RARF Overview** — one paper per row, 23 RARF columns, plus scan/extraction provenance
- **Variable–Measure Map** — conceptual constructs linked to empirical measures
- **Evidence & QA** — page-cited quotations, argument rephrasings, quote-match checks
- **Run Log** — model, agent/run IDs, cache hits, errors
- **Notes** — how to interpret statuses and edit the workbook

## Agent flow

- `Pipeline` discovers PDFs, extracts text, and stamps each row with a `scan_id`.
- `Summarizer` runs a theory session and a method session per paper, then reconciles them; if the reconcile pass fails, the completed theory/method merge is kept instead of dropping the row.
- API calls live in `cursor_runtime` (`CursorSdkBackend` for the local Cursor agent, `ExternalChatBackend` for any OpenAI-compatible API such as 智谱 GLM or DeepSeek); the desk picks the backend via Settings.

## Model

Every LLM session uses Cursor Grok 4.6 High. At startup the catalog from `Cursor.models.list()` is checked; if that model/preset is unavailable, the run stops instead of falling back to another model.
