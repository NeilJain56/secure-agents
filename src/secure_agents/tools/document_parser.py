"""Document parser tool - extracts text from PDF and DOCX files securely."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from secure_agents.core.base_tool import BaseTool
from secure_agents.core.registry import register_tool
from secure_agents.core.security import validate_file

logger = structlog.get_logger()


@register_tool("document_parser")
class DocumentParserTool(BaseTool):
    """Extracts text content from PDF and DOCX documents.

    Validates files before parsing. Returns structured text with metadata.
    """

    name = "document_parser"
    description = "Parse PDF and DOCX files to extract text"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.max_file_size_mb = self.config.get("max_file_size_mb", 50)
        self.allowed_types = self.config.get("allowed_file_types", [".pdf", ".docx", ".doc"])

    def execute(self, **kwargs: Any) -> dict:
        """Parse a document and extract text.

        kwargs:
            file_path: Path to the document (required)

        Returns:
            {"text": str, "metadata": dict, "page_count": int, "file_type": str}
            or {"error": str} on failure.
        """
        file_path = kwargs.get("file_path", "")
        if not file_path:
            return {"error": "No file_path provided"}

        path = Path(file_path)

        # Validate before parsing
        is_valid, reason = validate_file(path, self.allowed_types, self.max_file_size_mb)
        if not is_valid:
            logger.warning("document_parser.invalid", path=str(path), reason=reason)
            return {"error": reason}

        suffix = path.suffix.lower()
        try:
            if suffix == ".pdf":
                return self._parse_pdf(path)
            elif suffix in (".docx", ".doc"):
                return self._parse_docx(path)
            else:
                return {"error": f"Unsupported file type: {suffix}"}
        except Exception as e:
            logger.error("document_parser.error", path=str(path), error=str(e))
            return {"error": f"Parse failed: {str(e)}"}

    def _parse_pdf(self, path: Path) -> dict:
        """Extract text from a PDF file using pdfplumber."""
        import pdfplumber

        text_parts = []
        page_count = 0

        with pdfplumber.open(path) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

        text = "\n\n".join(text_parts)
        logger.info("document_parser.pdf", path=str(path), pages=page_count, chars=len(text))

        return {
            "text": text,
            "metadata": {"filename": path.name, "size_bytes": path.stat().st_size},
            "page_count": page_count,
            "file_type": "pdf",
        }

    def _parse_docx(self, path: Path) -> dict:
        """Extract text from a DOCX file using python-docx."""
        import docx

        doc = docx.Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        text = "\n\n".join(paragraphs)
        page_count = max(1, len(text) // 3000)  # Rough estimate

        logger.info("document_parser.docx", path=str(path), paragraphs=len(paragraphs), chars=len(text))

        return {
            "text": text,
            "metadata": {"filename": path.name, "size_bytes": path.stat().st_size},
            "page_count": page_count,
            "file_type": "docx",
        }
