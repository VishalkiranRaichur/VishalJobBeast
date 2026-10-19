"""Shared test configuration.

Tests that build a real resume PDF (anything driving prepare_handoff /
handoff_batch) need tectonic and poppler. Without them they are skipped with a
clear reason instead of failing, so a fresh clone runs green out of the box.
"""
from __future__ import annotations

import inspect
import shutil

import pytest

PDF_TOOLS = ("tectonic", "pdfinfo", "pdftotext", "pdftoppm")
MISSING_PDF_TOOLS = [tool for tool in PDF_TOOLS if shutil.which(tool) is None]
_PDF_BUILDERS = ("prepare_handoff(", "handoff_batch(")


def _builds_pdf(item) -> bool:
    try:
        source = inspect.getsource(item.function)
    except (OSError, TypeError):
        return False
    return any(marker in source for marker in _PDF_BUILDERS)


def pytest_collection_modifyitems(config, items):
    if not MISSING_PDF_TOOLS:
        return
    skip = pytest.mark.skip(reason=f"needs PDF toolchain; missing: {', '.join(MISSING_PDF_TOOLS)}")
    for item in items:
        if _builds_pdf(item):
            item.add_marker(skip)
