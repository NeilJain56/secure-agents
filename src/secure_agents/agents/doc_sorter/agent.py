"""Document sorting agent.

Scans a user-specified source folder, classifies each document via the
LLM (one fresh call per file — no context leakage between documents),
copies it into the appropriate category subfolder under ``output_root``,
then emits jobs to the three deduplicator agents.

Classification is adaptive: the agent sends a small initial excerpt to
the LLM and stops as soon as it returns a high-confidence answer.  If
the model is uncertain, successively larger excerpts are tried (up to
``_CLASSIFY_MAX_CHARS``).  Most legal documents identify themselves in
the first paragraph, so a single short call is usually sufficient.

This is a **single-run** agent: it processes all files once, emits the
dedup jobs, and stops.  It does not poll.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import structlog

from secure_agents.core.base_agent import BaseAgent
from secure_agents.core.message_builder import MessageBuilder
from secure_agents.core.registry import register_agent
from secure_agents.core.schemas import validate_schema

from .prompts import (
    CATEGORY_FOLDERS,
    CLASSIFY_INSTRUCTION,
    SORT_SCHEMA,
    SYSTEM_PROMPT,
)

logger = structlog.get_logger()

_SUPPORTED_EXTENSIONS = [".pdf", ".docx", ".doc", ".pptx", ".xlsx"]

# Adaptive classification chunk sizes (characters).
# The agent tries the smallest chunk first; if confidence is below the
# threshold it moves to the next larger chunk, up to the maximum.
# Most legal docs are identifiable from their title + first paragraph
# (~500–1 500 chars), so the first attempt usually succeeds.
_CLASSIFY_CHUNKS = [1_500, 3_500, 6_000]

# Stop reading more text once confidence reaches or exceeds this value.
_CONFIDENCE_THRESHOLD = 0.75

# Dedup agent names keyed by category slug
_DEDUP_AGENTS: dict[str, str] = {
    "nda":            "nda_deduplicator",
    "msa_company":    "msa_company_deduplicator",
    "msa_thirdparty": "msa_thirdparty_deduplicator",
}


@register_agent("doc_sorter")
class DocSorterAgent(BaseAgent):
    """Classifies documents from a source folder into three categories."""

    name = "doc_sorter"
    description = "Sort documents into NDA / MSA-company / MSA-thirdparty / Misc"
    version = "0.1.0"
    features = [
        "Classifies PDF, DOCX, DOC, PPTX, XLSX",
        "Adaptive classification — reads only as much text as needed",
        "One fresh LLM context per file (no context bleed)",
        "Copies files into category folders",
        "Emits jobs to deduplicator agents",
    ]

    def __init__(self, tools, provider, config=None, **kwargs):
        super().__init__(tools, provider, config, **kwargs)
        self.source_folder: str = self.config.get("source_folder", "")
        self.output_root: str = self.config.get("output_root", "./ai_generated")

    # ── Main loop (single-run) ───────────────────────────────────────────

    def tick(self) -> None:
        if not self.source_folder:
            logger.error("doc_sorter.no_source_folder",
                         msg="Set 'source_folder' in the agent config")
            self._stop_event.set()
            return

        file_mgr = self.get_tool("file_manager")
        text_ext = self.get_tool("text_extractor")

        # 1. Create category folders
        for folder_name in CATEGORY_FOLDERS.values():
            file_mgr.execute(action="mkdir", path=folder_name)

        # 2. Scan source folder for supported file types
        scan = file_mgr.execute(
            action="scan",
            folder=self.source_folder,
            extensions=_SUPPORTED_EXTENSIONS,
        )
        if "error" in scan:
            logger.error("doc_sorter.scan_failed", error=scan["error"])
            self._stop_event.set()
            return

        files = scan["files"]
        logger.info("doc_sorter.found_files", count=len(files))

        # Track which files land in each category for the dedup payload.
        # A lock protects list.append() so concurrent worker threads don't race.
        category_files: dict[str, list[str]] = {k: [] for k in CATEGORY_FOLDERS}
        category_lock = threading.Lock()

        def _process_file(file_info: dict) -> None:
            """Extract, classify, and copy one file.  Called from a worker thread."""
            src_path = file_info["path"]
            filename = file_info["name"]

            # Extract text (extractor applies its own reasonable page/row limits)
            result = text_ext.execute(file_path=src_path)
            if "error" in result:
                logger.warning("doc_sorter.extract_failed",
                               filename=filename, error=result["error"])
                return

            text = result["text"]
            if not text.strip():
                logger.warning("doc_sorter.empty_text", filename=filename)
                return

            # Adaptive classification
            category = self._classify(text, filename)
            if category is None:
                return

            # Copy file into the category folder
            folder_name = CATEGORY_FOLDERS[category]
            copy_result = file_mgr.execute(
                action="copy",
                src=src_path,
                dest=f"{folder_name}/{filename}",
            )
            if "error" in copy_result:
                logger.error("doc_sorter.copy_failed",
                             filename=filename, error=copy_result["error"])
                return

            with category_lock:
                category_files[category].append(filename)
            logger.info("doc_sorter.classified",
                        filename=filename, category=category)

        # 3. Classify all files in parallel.
        # Each worker independently extracts text, calls the LLM, and copies the
        # file — no shared mutable state except category_files (protected by a lock).
        # Set OLLAMA_NUM_PARALLEL >= sort_workers so Ollama actually services
        # concurrent requests rather than queuing them.
        sort_workers = self.config.get("sort_workers", 4)
        logger.info("doc_sorter.classifying_parallel",
                    total=len(files), workers=sort_workers)

        with ThreadPoolExecutor(max_workers=sort_workers) as executor:
            futures = {executor.submit(_process_file, fi): fi for fi in files}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    logger.exception("doc_sorter.worker_error")

        # 4. Emit jobs to dedup agents (misc has no dedup agent — skip it)
        for cat_key, folder_name in CATEGORY_FOLDERS.items():
            if cat_key not in _DEDUP_AGENTS:
                logger.info("doc_sorter.skipping_dedup",
                            category=cat_key,
                            file_count=len(category_files[cat_key]))
                continue
            dedup_agent = _DEDUP_AGENTS[cat_key]
            self.emit(dedup_agent, {
                "category": cat_key,
                "folder_name": folder_name,
                "output_root": self.output_root,
                "files": category_files[cat_key],
            })
            logger.info("doc_sorter.emitted_dedup",
                        agent=dedup_agent,
                        file_count=len(category_files[cat_key]))

        logger.info("doc_sorter.complete",
                     total=len(files),
                     sorted={k: len(v) for k, v in category_files.items()})

        # Single-run: stop after processing
        self._stop_event.set()

    # ── Classification ───────────────────────────────────────────────────

    def _classify(self, text: str, filename: str) -> str | None:
        """Classify *text* adaptively, reading only as much as needed.

        Tries progressively larger excerpts (``_CLASSIFY_CHUNKS``) until the
        model returns a confidence >= ``_CONFIDENCE_THRESHOLD``.  Returns the
        best category found, or ``None`` if every attempt failed.
        """
        best_category: str | None = None

        prev_chunk_size = 0
        for chunk_size in _CLASSIFY_CHUNKS:
            chunk = text[:chunk_size]

            # Skip this attempt if the document is shorter than the previous
            # chunk (no new text to offer the model).
            if len(chunk) <= prev_chunk_size:
                break
            prev_chunk_size = len(chunk)

            result = self._call_llm(chunk, filename, attempt=chunk_size)
            if result is None:
                continue

            category, confidence = result
            best_category = category

            logger.info("doc_sorter.classified_attempt",
                        filename=filename,
                        category=category,
                        confidence=confidence,
                        chars_used=len(chunk))

            if confidence >= _CONFIDENCE_THRESHOLD:
                break  # Confident enough — no need to read more

        return best_category

    def _call_llm(
        self, text: str, filename: str, attempt: int
    ) -> tuple[str, float] | None:
        """Send *text* to the LLM and return ``(category, confidence)``."""
        messages = (
            MessageBuilder(SYSTEM_PROMPT)
            .add_instruction(CLASSIFY_INSTRUCTION)
            .add_untrusted("document", text)
            .build()
        )

        try:
            response = self.provider.complete(messages, response_schema=SORT_SCHEMA)
        except Exception:
            logger.exception("doc_sorter.llm_error",
                             filename=filename, attempt=attempt)
            return None

        ok, parsed = validate_schema(response.content, SORT_SCHEMA)
        if not ok:
            # Model sometimes returns confidence as a percentage (0-100).
            # Try normalising before giving up.
            try:
                raw = json.loads(response.content)
                if isinstance(raw.get("confidence"), (int, float)) and raw["confidence"] > 1.0:
                    raw["confidence"] = raw["confidence"] / 100.0
                ok, parsed = validate_schema(json.dumps(raw), SORT_SCHEMA)
            except Exception:
                pass
            if not ok:
                logger.warning("doc_sorter.schema_invalid",
                               filename=filename, attempt=attempt, error=parsed)
                return None

        category = parsed["category"]
        confidence = min(max(parsed["confidence"], 0.0), 1.0)
        reasoning = parsed["reasoning"]

        logger.info("doc_sorter.llm_result",
                     filename=filename,
                     category=category,
                     confidence=confidence,
                     chars_used=len(text),
                     reasoning=reasoning[:120])

        return category, confidence
