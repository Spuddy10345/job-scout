"""Candidate profile = CV text and/or the editable markdown profile, per the chosen mode."""

from __future__ import annotations

import io
from pathlib import Path

from .config import AppSettings
from .prompts import DEFAULT_PERSONA


def extract_cv_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if ext == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()
    if ext in {".md", ".txt", ".markdown"}:
        return data.decode("utf-8", errors="replace").strip()
    raise ValueError("CV must be a PDF, DOCX, Markdown or text file")


def profile_text(settings: AppSettings) -> str:
    mode = settings.profile_mode
    parts = []
    if mode in ("both", "profile") and settings.profile_md.strip():
        parts.append("Profile notes (written by the candidate, most up to date):\n" + settings.profile_md.strip())
    if mode in ("both", "cv") and settings.cv_text.strip():
        parts.append("CV:\n" + settings.cv_text.strip()[:15000])
    if not parts:  # fall back to whichever exists
        parts.append(settings.profile_md.strip() or settings.cv_text.strip() or f"Candidate: {DEFAULT_PERSONA}.")
    weights = ", ".join(f"{k} {v:+d}" for k, v in settings.filters.category_weights.items())
    parts.append(f"Candidate's category preferences (applied separately, for context only): {weights}")
    return "\n\n".join(parts)
