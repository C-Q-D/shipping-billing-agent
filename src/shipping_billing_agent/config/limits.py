"""Resource budgets and the supported upload extensions."""

from dataclasses import dataclass

ALLOWED_UPLOAD_EXTENSIONS = frozenset(
    {".eml", ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xls", ".xlsx", ".doc", ".docx"}
)


@dataclass(frozen=True)
class ResourceLimits:
    max_batch_files: int = 50
    max_expanded_documents: int = 120
    max_file_bytes: int = 50 * 1024 * 1024
    max_eml_recursion_depth: int = 3
    max_doc_self_correction_rounds: int = 1
    max_concurrency: int = 4
    max_llm_calls_per_batch: int = 100
