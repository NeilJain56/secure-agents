# Document Sorting & Deduplication Pipeline

A multi-agent pipeline that takes a folder of mixed legal documents, classifies each one into a category, and finds near-duplicate files within each category — all running locally with no data leaving your machine.

---

## Table of Contents

- [Overview](#overview)
- [Pipeline Architecture](#pipeline-architecture)
- [How It Works](#how-it-works)
  - [Stage 1 — Document Classification](#stage-1--document-classification)
  - [Stage 2 — Near-Duplicate Detection](#stage-2--near-duplicate-detection)
- [Performance](#performance)
- [Testing](#testing)
- [Setup](#setup)
  - [Prerequisites](#prerequisites)
  - [Install](#install)
  - [Configure](#configure)
  - [Run](#run)
- [Configuration Reference](#configuration-reference)
- [Output Files](#output-files)
- [Model Recommendations](#model-recommendations)

---

## Overview

Legal teams accumulate document repositories that are hard to navigate: NDAs mixed with vendor contracts, purchase agreements, DPAs, and spreadsheets — often with multiple copies of the same agreement in different formats or versions. This pipeline solves two problems automatically:

1. **Sort** — Classify every document into one of four categories (NDA, MSA on company paper, MSA on third-party paper, or miscellaneous) and copy it into a labelled subfolder.
2. **Deduplicate** — Within each category, identify near-identical files (e.g. the same signed agreement saved as both a PDF and a DOCX, or two copies of the same executed contract).

The pipeline outputs a `duplicates.csv` for each category with file pairs, confidence scores, and reasoning — ready to review and act on.

**Everything runs locally.** The framework enforces `local_only = True` on every LLM provider; cloud egress is structurally impossible.

---

## Pipeline Architecture

The pipeline is a 4-agent system with a serial-then-parallel topology, coordinated through a shared SQLite job queue:

```
 source_folder/  (PDF, DOCX, DOC, PPTX, XLSX)
       │
       ▼
 ┌─────────────┐  Stage 1 — serial
 │  doc_sorter │  Classifies every file with one LLM call each.
 │             │  Copies files into category subfolders.
 └──────┬──────┘  Emits one job per category to the dedup agents.
        │ emit() × 3
        ▼
 ┌──────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
 │  nda_dedup       │  │  msa_company_dedup   │  │  msa_thirdparty_dedup│
 │                  │  │                      │  │                      │
 │  NDAs/           │  │  MSAs (company)/     │  │  MSAs (3rd party)/   │
 │  duplicates.csv  │  │  duplicates.csv      │  │  duplicates.csv      │
 └──────────────────┘  └──────────────────────┘  └──────────────────────┘
         Stage 2 — all three run in parallel
```

**Stage 1 (serial):** `doc_sorter` must finish before the dedup agents can start — it needs to create the category folders and emit the list of files for each agent to process.

**Stage 2 (parallel):** The three dedup agents are independent of each other and run simultaneously. Each one processes its own category folder in isolation.

**Job queue coordination:** `doc_sorter` calls `self.emit("nda_deduplicator", {...})` (and similarly for the other two) when sorting is complete. The framework enqueues these jobs to a shared SQLite database. Each dedup agent calls `self.job_queue.dequeue(self.name)` in its tick loop and begins work as soon as its job arrives.

**Single-run agents:** All four agents are single-run. They process all their work in one pass, then call `self._stop_event.set()` to exit cleanly. You start the pipeline once; it runs to completion and stops.

---

## How It Works

### Stage 1 — Document Classification

**Agent:** `doc_sorter`  
**Tools:** `text_extractor`, `file_manager`

#### Classification categories

| Category | What it captures |
|---|---|
| `nda` | Non-Disclosure Agreements, Mutual NDAs, Confidentiality Agreements |
| `msa_company` | Master Service Agreements drafted on the *company's own* paper/template |
| `msa_thirdparty` | MSAs, Purchase Agreements, SaaS Agreements drafted on a *vendor or client's* template |
| `misc` | Data Processing Addenda, security questionnaires, audit reports, spreadsheets, pitch decks, anything non-contractual |

The LLM prompt includes explicit guidance to distinguish edge cases: DPAs and security questionnaires look like NDAs but are classified as `misc`; the company-vs-third-party distinction for MSAs is inferred from which party's branding and terms dominate the document.

#### Adaptive text chunking

Rather than sending full document text to the LLM on every call, `doc_sorter` uses an adaptive strategy that stops as soon as it has enough signal:

```
Attempt 1: send first 1,500 chars  → model returns confidence
If confidence ≥ 0.75  ──────────────────────────────► done
Attempt 2: send first 3,500 chars  → model returns confidence
If confidence ≥ 0.75  ──────────────────────────────► done
Attempt 3: send first 6,000 chars  → model returns confidence
                                    ────────────────► done (best effort)
```

In practice, well-formatted legal documents identify themselves in the first paragraph — most files classify at the 1,500-char threshold with no further retries. Ambiguous files (e.g. a dense agreement with no title page) progress to larger excerpts.

#### Parallel processing

`doc_sorter` processes files with a `ThreadPoolExecutor` using `sort_workers` threads (default: 4). Since each LLM call is independent (no shared state, no context bleed between documents), parallel workers are safe and reduce wall-clock time proportionally.

> **Note for Ollama users:** Set `OLLAMA_NUM_PARALLEL` to at least `sort_workers` so Ollama actually services concurrent requests instead of queueing them. Example: `OLLAMA_NUM_PARALLEL=4 secure-agents start doc_sort_pipeline`

#### Structured output

Every LLM call sends a JSON Schema that constrains the model to return exactly:

```json
{
  "category": "nda | msa_company | msa_thirdparty | misc",
  "confidence": 0.0–1.0,
  "reasoning": "1–3 sentence explanation"
}
```

The schema is forwarded to the provider's native structured-output mechanism (Ollama `format`, llama.cpp `json_schema`, OpenAI-compatible `response_format`). The framework re-validates the returned JSON as an independent check. A malformed or incomplete response triggers a retry at the next chunk size.

---

### Stage 2 — Near-Duplicate Detection

**Agents:** `nda_deduplicator`, `msa_company_deduplicator`, `msa_thirdparty_deduplicator`  
**Tools:** `text_extractor`, `file_manager`

Each dedup agent operates independently on its own category folder using a two-phase approach designed to minimize LLM calls without sacrificing accuracy.

#### Phase 1 — Jaccard pre-filter (instant, zero LLM calls)

For N files in a category, there are N×(N−1)÷2 possible pairs to compare. Sending all of them to the LLM would be slow. The pre-filter eliminates obviously dissimilar pairs with a word-set similarity score:

```
word_set(doc) = lowercase words − stop_words

jaccard(A, B) = |word_set(A) ∩ word_set(B)|
                ─────────────────────────────
                |word_set(A) ∪ word_set(B)|
```

Only pairs with Jaccard ≥ **0.95** are sent to the LLM. This threshold was chosen to retain:
- The same document in two formats (PDF + DOCX): typically Jaccard ≈ 0.97–0.99
- Scanned image + digital copy (OCR text may vary): Jaccard ≈ 0.95–0.98

And filter out:
- Different versions of the same template: Jaccard ≈ 0.85–0.92 (different parties, dates, pricing)
- Truly different agreements: Jaccard < 0.80

Stop words (54 common English function words) are removed before computing similarity so boilerplate clauses ("shall not", "in the event that") do not inflate scores between unrelated documents.

#### Phase 2 — LLM comparison (candidate pairs only)

For each pair that passes the pre-filter, the agent sends the first **4,000 characters** of each document to the LLM side-by-side and asks it to make a binary judgment:

```json
{
  "is_similar": true | false,
  "confidence": 0.0–1.0,
  "reasoning": "1–3 sentence explanation"
}
```

The LLM prompt instructs the model to be **strict**: return `true` only for true duplicates (same document, different format or cosmetic differences). Different versions of a negotiation, amendments, and agreements for different parties are explicitly NOT duplicates, even if they share most of their text.

The 4,000-character truncation was benchmarked against the 109-pair MSA-thirdparty workload in our test dataset:
- At **5,000 chars**: ~45 minutes total (too slow for practical use)
- At **4,000 chars**: ~16 minutes total (~8–9s per LLM call) ✓

4,000 characters is typically one to two pages of dense legal text — enough to capture the parties, date, governing law, and key commercial terms that distinguish true duplicates from different-version pairs.

#### What counts as a duplicate

| Classified as duplicate | Not classified as duplicate |
|---|---|
| Same PDF and DOCX of identical content | Version 1 vs version 2 of a negotiation |
| Scanned copy + digital original | Clean copy vs redlined copy |
| Two identical signed copies | Same template, different counterparty |
| Cosmetic differences only (font, headers) | Amendments or renewals |

---

## Performance

Measured on a real-world dataset of **154 legal documents** (mixed formats: PDF, DOCX, XLSX) spanning NDAs, vendor MSAs, purchase agreements, DPAs, and operational documents.

### Classification (Stage 1)

| Metric | Value |
|---|---|
| Files processed | 154 |
| Supported formats | PDF, DOCX, DOC, PPTX, XLSX |
| Parallel workers | 4 (default) |
| Avg. LLM calls per file | 1.0–1.3 (adaptive — most stop at first chunk) |
| Confidence threshold to stop | 0.75 |
| Typical first-chunk size used | 1,500 chars (well-labelled docs) |
| LLM call latency (Ollama + llama3.1:8b) | ~7–9s |
| **Total sort time** | **~20–25 min** for 154 files at 4 workers |

Classification accuracy on the test dataset: the model correctly identified NDAs, company-paper MSAs, third-party MSAs, and non-contractual documents (spreadsheets, security questionnaires, DPAs) with high consistency. The explicit prompt guidance on DPAs and security questionnaires was critical — without it, those documents would have been misclassified as NDAs.

### Deduplication (Stage 2 — parallel)

Results from the MSA-thirdparty category (largest in the test set):

| Metric | Value |
|---|---|
| Files in category | ~60–70 (varies by run) |
| Total possible pairs | ~2,100 |
| Pairs after Jaccard pre-filter (threshold 0.95) | ~109 |
| Pre-filter reduction | **~95%** of pairs eliminated before any LLM call |
| LLM call latency per pair | ~8–9s (4,000 chars each) |
| **Total dedup time (one category)** | **~15–16 min** |

All three dedup agents run in parallel, so the total Stage 2 wall-clock time equals the time for the *slowest* category — not the sum of all three.

---

## Testing

The pipeline was developed and tested against a corpus of **154 real legal documents** from an active startup's legal repository, including:

- Executed NDAs (mutual and unilateral)
- Vendor purchase agreements and SaaS MSAs (on third-party paper)
- Internal service agreements (on company paper)
- Data Processing Addenda
- Security questionnaires and vendor assessments
- Financial/operational spreadsheets

### What we validated

**Classification accuracy**
- NDAs were consistently identified, including mutual NDAs, unilateral NDAs, and confidentiality agreements with non-standard titles
- DPAs and security questionnaires were correctly routed to `misc` after prompt refinement (initial runs without the explicit exclusion rules misclassified some)
- The company-paper vs. third-party-paper distinction for MSAs proved reliable — the model correctly infers this from which party's branding and terms dominate
- XLSX files (spreadsheets) were correctly classified as `misc` without ambiguity

**Deduplication precision**
- True duplicates identified: same agreements saved as PDF + DOCX, and scanned copies alongside digital originals
- No false positives observed on the test set: different-version pairs (e.g. draft v1 vs. executed v2 of the same deal) were correctly classified as NOT duplicates
- The 0.95 Jaccard threshold cleanly separated true duplicates from different-version pairs, which tended to cluster at 0.85–0.92

**Pipeline reliability**
- Job queue coordination worked correctly: the three dedup agents each received exactly one job from `doc_sorter`, processed it, and exited cleanly
- The pipeline was run multiple times against the dataset with consistent results
- Progress tracking in the dashboard reflected the serial → parallel execution correctly (25% while sorting, advancing to 100% as each dedup agent completes)

### Known edge cases

- **Very short documents** (< 1,500 chars) classify at confidence 0.6–0.7, which is below the 0.75 threshold. The agent falls through all three chunk sizes and uses the best available result.
- **Scanned PDFs** with poor OCR may produce noisy text; Jaccard similarity drops and these pairs may miss the pre-filter even when they are true duplicates. The `text_extractor` tool uses `pdfplumber` which handles most modern PDFs well.
- **Mixed-language documents** (English + another language) may classify as `misc` when the English content is insufficient to signal the category.

---

## Setup

### Prerequisites

| Requirement | Notes |
|---|---|
| macOS or Linux | macOS uses Keychain automatically; Linux uses the encrypted file backend |
| Python 3.11+ | Managed by `setup.sh` if not present |
| [Ollama](https://ollama.com) | Or any OpenAI-compatible local server |
| A local LLM model | `llama3.1:8b` recommended for legal classification |
| Docker | Required for sandbox mode (default: enabled) |

> **Model recommendation:** `llama3.1:8b` or larger. Smaller models (3B) tend to produce valid JSON but with lower classification accuracy on ambiguous documents. The `llama3.2:3b` model works for clearly-labelled documents but struggles with documents that lack a title page.

### Install

```bash
# Clone the repo
git clone https://github.com/NeilJain56/secure-agents.git
cd secure-agents

# Run the one-command setup (macOS — installs Python, Ollama, venv, pulls model)
bash setup.sh

# Or install manually
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Pull the recommended model:

```bash
ollama pull llama3.1:8b
```

> **For best throughput:** enable parallel requests in Ollama so multiple workers can run at once:
> ```bash
> OLLAMA_NUM_PARALLEL=4 ollama serve
> ```

### Configure

Copy the example config and edit it:

```bash
cp config.example.yaml config.yaml
```

Set the two required fields under `agents`:

```yaml
agents:
  doc_sorter:
    enabled: true
    source_folder: /path/to/your/unsorted/documents   # ← point this at your files
    output_root: /path/to/your/unsorted/documents     # ← usually same as source_folder

  nda_deduplicator:
    enabled: true
    output_root: /path/to/your/unsorted/documents     # ← must match doc_sorter output_root

  msa_company_deduplicator:
    enabled: true
    output_root: /path/to/your/unsorted/documents

  msa_thirdparty_deduplicator:
    enabled: true
    output_root: /path/to/your/unsorted/documents
```

Setting `output_root` to the same path as `source_folder` means the category subfolders appear alongside your original files — making it easy to review the sorted output without switching directories.

Optionally tune performance:

```yaml
agents:
  doc_sorter:
    sort_workers: 4    # parallel LLM calls (set OLLAMA_NUM_PARALLEL to match)

  msa_thirdparty_deduplicator:
    dedup_workers: 1   # LLM comparison workers (>1 only with batching backends)
```

Set the active LLM provider:

```yaml
provider:
  active: ollama
  ollama:
    host: http://localhost:11434
    model: llama3.1:8b    # recommended for legal document classification
    temperature: 0.1
```

### Run

Validate your configuration first:

```bash
secure-agents validate
```

Start the pipeline with a single command:

```bash
secure-agents start doc_sort_pipeline
```

That's it. The CLI expands `doc_sort_pipeline` into the four constituent agents and starts them in the correct order.

**With Ollama parallel requests enabled (recommended):**

```bash
OLLAMA_NUM_PARALLEL=4 ollama serve &
secure-agents start doc_sort_pipeline
```

**Monitor progress in the dashboard:**

```bash
# In a second terminal
secure-agents ui
```

Open `http://localhost:8420`. The pipeline tile shows:
- A progress bar (0% → 25% while sorting → 50–100% as each dedup agent finishes → 100%)
- The serial → parallel stage layout with per-agent running indicators
- Metrics tab: tick counts and latency for each agent
- Outputs tab: `duplicates.csv` files for each category once dedup completes
- Audit log: every classification decision and dedup comparison, with confidence scores

**What to expect:**

1. The dashboard shows `doc_sorter` running (Stage 1). Category subfolders appear in `output_root` as files are classified.
2. When sorting completes, `doc_sorter` emits jobs and exits. The three dedup agents start simultaneously (Stage 2).
3. Each dedup agent extracts text, runs the Jaccard pre-filter, sends candidate pairs to the LLM, and writes `duplicates.csv`.
4. When all three finish, the pipeline is complete. All agents have exited cleanly.

---

## Configuration Reference

### `doc_sorter`

| Key | Default | Description |
|---|---|---|
| `source_folder` | *(required)* | Absolute path to the folder of unsorted documents |
| `output_root` | `./ai_generated` | Root folder where category subfolders are created |
| `sort_workers` | `4` | Number of parallel threads for LLM classification calls |
| `tools` | `[text_extractor, file_manager]` | Required tools — do not change |
| `enabled` | `true` | Set to `false` to skip sorting |

### `nda_deduplicator` / `msa_company_deduplicator` / `msa_thirdparty_deduplicator`

| Key | Default | Description |
|---|---|---|
| `output_root` | `./ai_generated` | Must match `doc_sorter.output_root` |
| `dedup_workers` | `1` | Parallel LLM comparison threads. Increase only if your backend supports batching (vLLM, multi-GPU Ollama) |
| `tools` | `[text_extractor, file_manager]` | Required tools — do not change |
| `enabled` | `true` | Set to `false` to skip a category |

### Pipeline definition

```yaml
pipelines:
  doc_sort_pipeline:
    description: Sort and deduplicate legal documents
    agents:
      - doc_sorter
      - nda_deduplicator
      - msa_company_deduplicator
      - msa_thirdparty_deduplicator
    stages:
      - [doc_sorter]                                                    # serial
      - [nda_deduplicator, msa_company_deduplicator, msa_thirdparty_deduplicator]  # parallel
```

---

## Output Files

After the pipeline runs, `output_root` contains:

```
output_root/
├── Non-disclosure agreements/
│   ├── <sorted NDAs...>
│   └── duplicates.csv
├── MSAs (on company paper)/
│   ├── <sorted MSAs...>
│   └── duplicates.csv
├── MSAs (on third party paper)/
│   ├── <sorted MSAs...>
│   └── duplicates.csv
└── Miscellaneous/
    └── <sorted non-contractual files...>
```

Each `duplicates.csv` has four columns:

| Column | Description |
|---|---|
| `file_a` | First file in the duplicate pair |
| `file_b` | Second file in the duplicate pair |
| `confidence` | LLM confidence score (0.0–1.0) |
| `reasoning` | One-sentence explanation of why the pair was flagged |

Example output:

```csv
file_a,file_b,confidence,reasoning
Acme_NDA_2024.pdf,Acme_NDA_2024.docx,0.98,"Same executed NDA saved in two formats; identical parties, dates, and terms."
NDA_Signed_v2.pdf,NDA_Final_Executed.pdf,0.95,"Both are the same fully-executed NDA; the file names differ but content is identical."
```

If no duplicates are found in a category, the `duplicates.csv` is written with headers only (empty body). The Miscellaneous category does not get a dedup pass — only the three contractual categories are deduplicated.

---

## Model Recommendations

| Model | Classification | Deduplication | Notes |
|---|---|---|---|
| `llama3.1:8b` | ✓ Excellent | ✓ Excellent | Recommended. Handles edge cases, ambiguous titles, and mixed-language docs well. |
| `llama3.2:3b` | ✓ Good | ✓ Good | Faster; works well for clearly-labelled documents. May misclassify ambiguous files. |
| `mistral:7b` | ✓ Good | ✓ Good | Comparable to llama3.1:8b for classification; slightly lower accuracy on dedup strictness. |
| Any `openai_compat` | Varies | Varies | Works with vLLM, LM Studio, LocalAI. Performance depends on the underlying model. |

Set the model in `config.yaml`:

```yaml
provider:
  active: ollama
  ollama:
    model: llama3.1:8b
    temperature: 0.1    # low temperature for deterministic classification
```

Use `temperature: 0.1` (not 0.0) to avoid degenerate outputs from models that collapse to a single token at zero temperature.

---

*Part of the [Secure Agents](../README.md) framework — local-only AI agent automation for sensitive document workflows.*
