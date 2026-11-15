"""Read native PDF text in an isolated subprocess."""

import json
import sys

import pymupdf

MAX_PAGES = 32
MAX_TEXT_CHARS = 100_000


class DocumentError(ValueError):
    """The document cannot be extracted within the supported input contract."""


def read_pages(pdf_bytes: bytes) -> list[str]:
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
            if document.needs_pass:
                raise DocumentError("pdf_encrypted")
            if not 1 <= document.page_count <= MAX_PAGES:
                raise DocumentError("pdf_page_limit")
            pages = []
            total = 0
            for page in document:
                text = page.get_text("text", sort=True).strip()
                if not text:
                    raise DocumentError("pdf_page_has_no_text")
                total += len(text)
                if total > MAX_TEXT_CHARS:
                    raise DocumentError("pdf_text_limit")
                pages.append(text)
            return pages
    except (pymupdf.FileDataError, RuntimeError):
        raise DocumentError("pdf_unreadable") from None


def main() -> None:
    try:
        result = {"pages": read_pages(sys.stdin.buffer.read())}
    except DocumentError as exc:
        result = {"error_code": str(exc)}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
