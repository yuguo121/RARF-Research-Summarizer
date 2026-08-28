# RARF Research Summarizer

Turn a folder of research PDFs into a structured, page-cited review-form workbook.

Each paper becomes one row in an Excel overview. A local desk app lets you pick
folders, toggle dimensions, edit the prompt behind every dimension, re-run a
single cell, and keep human edits across exports. The pipeline never copies or
modifies files in your library — it only reads PDFs.

The review form is **data-driven**: dimensions, prompts, sessions, and group
styling all live in one YAML file. The project ships with the 23-dimension
**Research Article Review Form (RARF)** used in management research, and you can
point `schema_path` at your own YAML to run a completely different form.
See [Custom schemas](#custom-schemas).

## Setup

```powershell
git clone https://github.com/yuguo121/RARF-Research-Summarizer.git
cd RARF-Research-Summarizer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and set `CURSOR_API_KEY` (for the local Cursor
backend) or a provider key such as `DEEPSEEK_API_KEY` / `ZHIPU_API_KEY` (for any
OpenAI-compatible API). `.env` is gitignored. User API keys for the Cursor
backend live at [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations).

`rarf-desk` (or `desk.cmd` on Windows) launches the local web desk as a
standalone window; it is the same `rarf ui` server with browser auto-open.

## Commands

```powershell
# Extract, summarize, and export a folder of PDFs
rarf run "D:\Papers\Corporate Governance"

# Steps separately
rarf extract PATH
rarf summarize PATH
rarf export
rarf sync-back

# Local desk: pick folders/PDFs, choose dimensions, edit prompts
rarf-desk
```

`summarize` is incremental: papers are cached by PDF hash plus schema, prompt,
and model versions. Use `--force` to redo LLM runs, `--name-contains "Krause"`
or `--limit 1` to work on a subset.

After editing cells in the exported workbook, run `rarf sync-back` so those
edits are stored as human overrides and survive the next export.

## Output

`output/RARF_Overview.xlsx` contains:

- **Overview** — one paper per row, one column per schema dimension, plus
  scan/extraction provenance
- **Variable–Measure Map** — conceptual constructs linked to empirical measures
  (only for schemas with `constructs`/`measures` fields, e.g. RARF)
- **Evidence & QA** — page-cited quotations, argument rephrasings, quote-match
  checks
- **Run Log** — model, agent/run IDs, cache hits, errors
- **Notes** — how to interpret statuses and edit the workbook

## Custom schemas

The default form is `config/rarf_schema.yaml`. To use your own review form:

1. Copy `config/examples/quick_review.yaml` (a minimal 5-field template) or the
   RARF schema as a starting point.
2. Edit `fields:` — each field needs an `id`, `label`, `group`, `session`,
   `value_kind`, and the `instruction` the LLM follows.
3. Point `schema_path` in `config/settings.yaml` at your file.

Everything downstream adapts automatically: the desk's dimension list, the
summary grid, group colors, the Excel sheet name and layout, and the session
prompts. Fields with `value_kind: text` work out of the box; `framing`,
`arguments`, `constructs`, and `measures` are built-in enriched types with
structured validation and dedicated Excel sheets.

Sessions group fields into separate LLM passes over different parts of the PDF.
The RARF default uses a `theory` pass (abstract, introduction, discussion…) and
a `method` pass (methods, results, appendix…), then reconciles the two. Define
your own under `sessions:` with the `section_keys` each pass should read.

## Metadata from Zotero (optional)

Paper metadata comes from Zotero, not the LLM. Point `zotero.export_path` in
`config/settings.yaml` (or the Settings dialog in the desk) at a Zotero export
(`File → Export Library… → Zotero JSON / CSL JSON`), or enable the Zotero Web
API (`use_api: true`, `library_id`, and a `ZOTERO_API_KEY` in `.env`). When a
PDF matches a Zotero item, author/year/DOI/journal/citation are filled directly;
otherwise the PDF's own metadata is used.

## Agent flow

- `Pipeline` discovers PDFs, extracts text, and stamps each row with a
  `scan_id`.
- `Summarizer` runs one LLM session per schema session per paper, then
  reconciles them; if the reconcile pass fails, the merged session output is
  kept instead of dropping the row.
- API calls live in `cursor_runtime` (`CursorSdkBackend` for the local Cursor
  agent, `ExternalChatBackend` for any OpenAI-compatible API such as GLM or
  DeepSeek); the desk picks the backend via Settings.

## Model policy

With the local Cursor backend, every LLM session uses Cursor Grok 4.6 High. At
startup the catalog from `Cursor.models.list()` is checked; if that model/preset
is unavailable, the run stops instead of falling back to another model. The
external backend uses whatever model you configure.

---

## 中文快速上手

把一整个文件夹的论文 PDF 变成一张结构化、带页码引用的综述表格：每篇论文一行，
每个维度一列。维度、提问词、会话和配色全部由一个 YAML 文件定义 — 默认内置
管理学常用的 23 维 RARF 表，也可以在 `config/settings.yaml` 里把 `schema_path`
指向你自己的 YAML，换成完全不同的表单（模板见 `config/examples/quick_review.yaml`）。

```powershell
pip install -e ".[dev]"
# 复制 .env.example 为 .env，填入 CURSOR_API_KEY 或 DEEPSEEK_API_KEY 等
rarf-desk   # 打开本地桌面端：选文件夹 → 勾维度 → 抽取并总结 → 导出 Excel
```

总结是增量的（按 PDF 哈希 + schema/提示词/模型版本缓存）；在 Excel 里改过的
格子用 `rarf sync-back` 存为人工覆盖，下次导出不会丢。元数据可来自 Zotero
导出文件或 Web API，不依赖 LLM。

## License

[Apache-2.0](LICENSE)
