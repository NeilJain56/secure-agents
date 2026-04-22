"""Deduplicator agents for the document sorting pipeline.

One base class, three registrations — one per document category.  Each
agent reads all files in its category folder, pre-filters with Jaccard
word-set similarity (stdlib only, instant), then sends candidate pairs
to the LLM for detailed comparison.  Results are written to a CSV.

The agents are triggered by jobs emitted from :class:`DocSorterAgent`.
They are **single-run**: process once, write the CSV, and stop.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import structlog

from secure_agents.core.base_agent import BaseAgent
from secure_agents.core.message_builder import MessageBuilder
from secure_agents.core.registry import register_agent
from secure_agents.core.schemas import validate_schema

from .prompts import COMPARE_INSTRUCTION, DEDUP_SCHEMA, SYSTEM_PROMPT

logger = structlog.get_logger()

_SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".pptx", ".xlsx"}

# Pre-filter: pairs below this Jaccard similarity are not sent to the LLM.
# True duplicates (same doc in PDF + DOCX, or two copies) have Jaccard ≥ 0.95.
# Negotiation-version clusters (different drafts of the same deal) typically
# sit in the 0.85–0.92 range — raising the threshold filters them WITHOUT
# LLM calls, keeping the comparison queue small.
_JACCARD_THRESHOLD = 0.95

# Maximum characters of document text sent to the LLM per document.
# Full text is used for Jaccard pre-filtering; only this prefix goes to the
# LLM so that large files (e.g. 20 MB XLSX exports) don't stall the model.
# Benchmarked sweet spot: 4 000 chars catches all confirmed true-duplicate
# pairs (including PDF/DOCX variants whose opening sections differ slightly)
# while staying fast enough for the 109-pair MSA-3p workload (~8-9s/call,
# ~16 min for 109 pairs vs the original ~45 min at 5 000 chars / 150 tokens).
_MAX_CHARS_FOR_LLM = 4_000

# Common English stop words removed during Jaccard pre-filter so that
# boilerplate does not inflate similarity scores.  This is a small,
# conservative list — enough to knock down noise without a dependency.
_STOP_WORDS = frozenset(
    "a an and are as at be but by for from has have he her his i if in "
    "into is it its my no not of on or our she so than that the their "
    "them then there these they this to was we were what when which who "
    "will with you your".split()
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _word_set(text: str) -> set[str]:
    """Normalise *text* into a set of lowercased words minus stop words."""
    words = set(_WORD_RE.findall(text.lower()))
    return words - _STOP_WORDS


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class _BaseDeduplicator(BaseAgent):
    """Shared logic for all three deduplicator agents.

    Concrete subclasses set ``name``, ``description``, and
    ``category_key`` then get registered via ``@register_agent``.
    """

    category_key: str = ""  # e.g. "nda"
    version = "0.1.0"
    features = [
        "Jaccard pre-filter skips obviously-dissimilar pairs",
        "LLM comparison on candidate pairs",
        "Writes duplicates.csv with confidence scores",
    ]

    def __init__(self, tools, provider, config=None, **kwargs):
        super().__init__(tools, provider, config, **kwargs)
        self.output_root: str = self.config.get("output_root", "./ai_generated")

    # ── Main loop (single-run, triggered by job) ─────────────────────────

    def tick(self) -> None:
        """Process one dedup job from the queue, or stop if none."""
        if self.job_queue is None:
            logger.error(f"{self.name}.no_job_queue")
            self._stop_event.set()
            return

        job = self.job_queue.dequeue(self.name)
        if job is None:
            # No work yet — wait and check again (or stop if sorter is done)
            self._stop_event.wait(5)
            return

        try:
            payload = job.payload
            folder_name = payload.get("folder_name", "")
            file_list = payload.get("files", [])

            if not folder_name:
                logger.error(f"{self.name}.missing_folder_name")
                self.job_queue.fail(job.id, "Missing folder_name in payload")
                self._stop_event.set()
                return

            folder_path = Path(self.output_root).resolve() / folder_name
            if not folder_path.is_dir():
                logger.error(f"{self.name}.folder_not_found", path=str(folder_path))
                self.job_queue.fail(job.id, f"Folder not found: {folder_path}")
                self._stop_event.set()
                return

            pairs = self._deduplicate(folder_path, file_list)
            self._write_csv(folder_path, pairs)

            self.job_queue.complete(job.id)
            logger.info(f"{self.name}.complete",
                        folder=folder_name, pairs_found=len(pairs))
        except Exception:
            logger.exception(f"{self.name}.error")
            self.job_queue.fail(job.id, "Unhandled error during dedup")

        # Single-run
        self._stop_event.set()

    # ── Deduplication logic ──────────────────────────────────────────────

    def _deduplicate(
        self,
        folder: Path,
        file_names: list[str],
    ) -> list[dict]:
        """Return a list of similar-pair dicts for the CSV."""
        text_ext = self.get_tool("text_extractor")

        # 1. Extract text from every file
        docs: list[tuple[str, str, set[str]]] = []  # (filename, text, word_set)
        for fname in file_names:
            fpath = folder / fname
            if not fpath.is_file():
                logger.warning(f"{self.name}.file_missing", filename=fname)
                continue
            result = text_ext.execute(file_path=str(fpath))
            if "error" in result:
                logger.warning(f"{self.name}.extract_failed",
                               filename=fname, error=result["error"])
                continue
            text = result["text"]
            docs.append((fname, text, _word_set(text)))

        logger.info(f"{self.name}.extracted", count=len(docs))

        # 2. Pre-filter with Jaccard
        candidates: list[tuple[int, int, float]] = []
        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                sim = _jaccard(docs[i][2], docs[j][2])
                if sim >= _JACCARD_THRESHOLD:
                    candidates.append((i, j, sim))

        logger.info(f"{self.name}.prefilter",
                     total_pairs=len(docs) * (len(docs) - 1) // 2,
                     candidates=len(candidates))

        # 3. LLM comparison on candidate pairs — run in parallel.
        # Truncate text for the LLM call so large documents don't stall the model.
        # Full text is still used for Jaccard pre-filtering above.
        # _compare() is stateless (pure HTTP calls to Ollama), so ThreadPoolExecutor
        # is safe here.  With a single Ollama instance (no GPU batching), Ollama
        # serialises all requests anyway — extra workers just pile up in its queue and
        # risk hitting the httpx 60 s timeout.  Default to 1 (sequential) which is
        # safe; set dedup_workers > 1 in config only when using a batching backend.
        workers = self.config.get("dedup_workers", 1)

        def _do_compare(idx_pair: tuple[int, int, float]) -> tuple[str, str, dict | None]:
            i, j, _ = idx_pair
            name_a, text_a, _ = docs[i]
            name_b, text_b, _ = docs[j]
            return name_a, name_b, self._compare(
                name_a, text_a[:_MAX_CHARS_FOR_LLM],
                name_b, text_b[:_MAX_CHARS_FOR_LLM],
            )

        pairs: list[dict] = []
        logger.info(f"{self.name}.comparing_parallel",
                    candidates=len(candidates), workers=workers)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_do_compare, idx_pair): idx_pair
                for idx_pair in candidates
            }
            for future in as_completed(futures):
                try:
                    name_a, name_b, llm_result = future.result()
                except Exception:
                    logger.exception(f"{self.name}.compare_worker_error")
                    continue
                if llm_result is None:
                    continue
                if llm_result["is_similar"]:
                    pairs.append({
                        "file_a": name_a,
                        "file_b": name_b,
                        "confidence": llm_result["confidence"],
                        "reasoning": llm_result["reasoning"],
                    })

        return pairs

    def _compare(
        self, name_a: str, text_a: str, name_b: str, text_b: str
    ) -> dict | None:
        """Ask the LLM whether two documents are substantially the same."""
        messages = (
            MessageBuilder(SYSTEM_PROMPT)
            .add_instruction(COMPARE_INSTRUCTION)
            .add_untrusted("document_a", f"[File: {name_a}]\n{text_a}")
            .add_untrusted("document_b", f"[File: {name_b}]\n{text_b}")
            .build()
        )

        try:
            response = self.provider.complete(messages, response_schema=DEDUP_SCHEMA)
        except Exception:
            logger.exception(f"{self.name}.llm_error",
                             file_a=name_a, file_b=name_b)
            return None

        ok, parsed = validate_schema(response.content, DEDUP_SCHEMA)
        if not ok:
            # Model sometimes returns confidence as a percentage (0-100).
            # Try normalising before giving up.
            try:
                import json
                raw = json.loads(response.content)
                if isinstance(raw.get("confidence"), (int, float)) and raw["confidence"] > 1.0:
                    raw["confidence"] = raw["confidence"] / 100.0
                ok, parsed = validate_schema(json.dumps(raw), DEDUP_SCHEMA)
            except Exception:
                pass
            if not ok:
                logger.warning(f"{self.name}.schema_invalid",
                               file_a=name_a, file_b=name_b, error=parsed)
                return None

        # Clamp confidence to [0, 1] in case of floating-point edge cases
        parsed["confidence"] = min(max(parsed["confidence"], 0.0), 1.0)
        return parsed

    # ── CSV output ───────────────────────────────────────────────────────

    def _write_csv(self, folder: Path, pairs: list[dict]) -> None:
        """Write duplicates.csv via the file_manager tool."""
        file_mgr = self.get_tool("file_manager")

        # Path relative to output_root
        relative_folder = folder.name
        csv_path = f"{relative_folder}/duplicates.csv"

        rows = [
            [p["file_a"], p["file_b"], f'{p["confidence"]:.2f}', p["reasoning"]]
            for p in pairs
        ]

        result = file_mgr.execute(
            action="write_csv",
            path=csv_path,
            headers=["file_a", "file_b", "confidence", "reasoning"],
            rows=rows,
        )

        if "error" in result:
            logger.error(f"{self.name}.csv_error", error=result["error"])
        else:
            logger.info(f"{self.name}.csv_written",
                        path=result["path"], rows=len(rows))


# ── Three concrete registrations ─────────────────────────────────────────────

@register_agent("nda_deduplicator")
class NDADeduplicator(_BaseDeduplicator):
    name = "nda_deduplicator"
    description = "De-duplicate Non-Disclosure Agreements"
    category_key = "nda"


@register_agent("msa_company_deduplicator")
class MSACompanyDeduplicator(_BaseDeduplicator):
    name = "msa_company_deduplicator"
    description = "De-duplicate MSAs on company paper"
    category_key = "msa_company"


@register_agent("msa_thirdparty_deduplicator")
class MSAThirdpartyDeduplicator(_BaseDeduplicator):
    name = "msa_thirdparty_deduplicator"
    description = "De-duplicate MSAs on third-party paper"
    category_key = "msa_thirdparty"
