"""
PDF -> Macedonian audiobook (single MP3), 100% local, free, no API key.

Pipeline:
  1. Extract text from the PDF with PyMuPDF (fitz).
  2. If nothing is extractable, stop and report it's a scanned PDF needing OCR.
  3. Clean: fix hyphenated line breaks, drop bare page-number lines, collapse whitespace.
  4. Split into sentence-aware chunks of ~4000 characters.
  5. Synthesize each chunk with edge-tts (Microsoft free neural voices), with
     retries and on-disk caching so an interrupted run resumes.
  6. Stitch the chunks into one MP3 with pydub, written next to the PDF.

Requires: pymupdf, pydub, edge-tts, and ffmpeg on PATH.
"""

import asyncio
import os
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
import edge_tts
from pydub import AudioSegment

# ----------------------------------------------------------------------------
# CONFIG  -- change the voice here.
# Macedonian neural voices:
#   "mk-MK-MarijaNeural"      (female)
#   "mk-MK-AleksandarNeural"  (male)
# ----------------------------------------------------------------------------
VOICE = "mk-MK-MarijaNeural"

PDF_PATH = Path(
    r"C:\Users\gpt30\OneDrive\Desktop\FICTION\RECOMMENDED ACTION\macedonian\Recommended_Action_Macedonian_9p5pt_RECTO_FIXED.pdf"
)

MAX_CHARS = 4000          # target chunk size (sentence-aware)
GAP_MS = 300              # silence inserted between chunks when stitching
MAX_RETRIES = 5           # synthesis attempts per chunk
RETRY_BACKOFF = 2.0       # seconds; doubles each retry


# ----------------------------------------------------------------------------
# 1 + 2. Extract text
# ----------------------------------------------------------------------------
def extract_text(pdf_path: Path) -> str:
    doc = fitz.open(pdf_path)
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return "\n".join(pages)


# ----------------------------------------------------------------------------
# 3. Clean text
# ----------------------------------------------------------------------------
def clean_text(text: str) -> str:
    # Join words split across a line by a hyphen:  "разго-\nвор" -> "разговор"
    text = re.sub(r"-\n", "", text)
    # Drop lines that are only a page number (optionally surrounded by spaces).
    text = re.sub(r"(?m)^\s*\d+\s*$\n?", "", text)
    # Collapse all runs of whitespace (incl. newlines) into single spaces.
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ----------------------------------------------------------------------------
# 4. Sentence-aware chunking
# ----------------------------------------------------------------------------
def split_into_chunks(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    # Split after sentence terminators (handles . ! ? and the ellipsis …).
    sentences = re.split(r"(?<=[.!?…])\s+", text)

    chunks: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for sentence in sentences:
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 <= max_chars:
            current = f"{current} {sentence}".strip()
        else:
            flush()
            if len(sentence) <= max_chars:
                current = sentence
            else:
                # A single sentence longer than max_chars: hard-split on words.
                for word in sentence.split():
                    if len(current) + len(word) + 1 <= max_chars:
                        current = f"{current} {word}".strip()
                    else:
                        flush()
                        current = word
    flush()
    return chunks


# ----------------------------------------------------------------------------
# 5. Synthesize one chunk with edge-tts (async, retrying, cached)
# ----------------------------------------------------------------------------
async def synth_chunk(text: str, out_path: Path, index: int, total: int) -> None:
    # Resume support: skip chunks already rendered to a non-empty file.
    if out_path.exists() and out_path.stat().st_size > 0:
        print(f"  [{index}/{total}] cached, skipping -> {out_path.name}")
        return

    delay = RETRY_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            communicate = edge_tts.Communicate(text, VOICE)
            await communicate.save(str(out_path))
            if out_path.exists() and out_path.stat().st_size > 0:
                print(f"  [{index}/{total}] rendered {len(text)} chars -> {out_path.name}")
                return
            raise RuntimeError("edge-tts produced an empty file")
        except Exception as exc:  # noqa: BLE001 - retry on any synthesis error
            # Don't leave a partial/empty file behind for the resume check.
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            if attempt == MAX_RETRIES:
                print(f"  [{index}/{total}] FAILED after {MAX_RETRIES} attempts: {exc}")
                raise
            print(f"  [{index}/{total}] attempt {attempt} failed ({exc}); retrying in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay *= 2


async def synth_all(chunks: list[str], cache_dir: Path) -> list[Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        out_path = cache_dir / f"chunk_{i:04d}.mp3"
        await synth_chunk(chunk, out_path, i, total)
        paths.append(out_path)
    return paths


# ----------------------------------------------------------------------------
# 6. Stitch into one MP3
# ----------------------------------------------------------------------------
def stitch(chunk_paths: list[Path], out_path: Path) -> None:
    print(f"Stitching {len(chunk_paths)} chunks -> {out_path.name}")
    combined = AudioSegment.empty()
    gap = AudioSegment.silent(duration=GAP_MS)
    for i, path in enumerate(chunk_paths):
        segment = AudioSegment.from_mp3(path)
        combined += segment if i == 0 else (gap + segment)
    combined.export(out_path, format="mp3")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    if not PDF_PATH.exists():
        print(f"PDF not found: {PDF_PATH}")
        return 1

    print(f"Reading: {PDF_PATH.name}")
    raw = extract_text(PDF_PATH)
    text = clean_text(raw)

    if len(text) < 20:
        print(
            "No extractable text found. This looks like a scanned (image-only) PDF.\n"
            "You'll need OCR first (e.g. ocrmypdf / Tesseract with the 'mkd' language "
            "pack) to produce a text layer, then re-run this script."
        )
        return 2

    chunks = split_into_chunks(text)
    print(f"Extracted {len(text)} characters -> {len(chunks)} chunk(s). Voice: {VOICE}")

    cache_dir = PDF_PATH.with_name(PDF_PATH.stem + "_chunks")
    chunk_paths = asyncio.run(synth_all(chunks, cache_dir))

    out_path = PDF_PATH.with_suffix(".mp3")
    stitch(chunk_paths, out_path)

    print(f"Done. Audiobook written to:\n  {out_path}")
    print(f"(Chunk cache kept in {cache_dir.name}\\ — delete it to free space or to force a re-render.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
