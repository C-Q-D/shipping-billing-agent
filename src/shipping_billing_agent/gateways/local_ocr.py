"""Run offline OCR on individual images and multi-frame TIFF files."""

from __future__ import annotations

from io import BytesIO
from statistics import fmean
from threading import Lock
from typing import TYPE_CHECKING

from PIL import Image, ImageSequence

if TYPE_CHECKING:
    from rapidocr_onnxruntime import RapidOCR


class LocalOcrError(RuntimeError):
    pass


class RapidOcrFallback:
    def __init__(self) -> None:
        self._engine: RapidOCR | None = None
        self._engine_lock = Lock()

    def _recognize_page(self, image: Image.Image, page: int) -> tuple[str, list[float]]:
        if image.width == 0 or image.height == 0:
            raise LocalOcrError(f"第 {page} 页图像为空")

        try:
            mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
            with image.convert(mode) as prepared, self._engine_lock:
                if self._engine is None:
                    from rapidocr_onnxruntime import RapidOCR

                    self._engine = RapidOCR()
                rows, _ = self._engine(prepared)
        except Exception as exc:
            raise LocalOcrError(f"第 {page} 页本地 OCR 失败（{type(exc).__name__}）") from exc

        lines = [f"[Page {page}]"]
        scores: list[float] = []
        for _, text, score in rows or []:
            if text.strip():
                lines.append(text)
                scores.append(float(score))
        return "\n".join(lines), scores

    def recognize_pil_image(self, image: Image.Image, page: int = 1) -> tuple[str, float]:
        text, scores = self._recognize_page(image, page)
        if not scores:
            raise LocalOcrError(f"第 {page} 页未识别到文字")
        return text, fmean(scores)

    def recognize_image_bytes(self, content: bytes, filename: str) -> tuple[str, float]:
        pages: list[str] = []
        scores: list[float] = []
        try:
            with BytesIO(content) as buffer, Image.open(buffer) as image:
                for page, frame in enumerate(ImageSequence.Iterator(image), start=1):
                    text, frame_scores = self._recognize_page(frame, page)
                    pages.append(text)
                    scores.extend(frame_scores)
        except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
            raise LocalOcrError(f"无法读取图像 {filename!r}") from exc
        if not scores:
            raise LocalOcrError(f"图像 {filename!r} 未识别到文字")
        return "\n\n".join(pages), fmean(scores)
