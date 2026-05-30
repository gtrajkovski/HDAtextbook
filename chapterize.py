"""
Chapter-aware extractor + dual-voice renderer for 'Препорачана акција'.

Headings are set at 13pt (body is 9.5pt). Two heading styles:
  - ALL-CAPS, letter-spaced  -> NUMBERED chapters       -> Marija  (female)
  - Mixed-case               -> UNNUMBERED interludes   -> Aleksandar (male)

The renderer:
  - detects every 13pt heading (font-size based, so it catches interludes too),
  - reconstructs heading words from character gaps,
  - strips running headers / page numbers / chapter ordinals (all non-body sizes),
  - segments the book heading-to-heading,
  - renders each segment in its assigned voice (resumable per chunk),
  - writes one MP3 per segment into ./audiobook/.

Usage:
  python chapterize.py                 # list all segments + assigned voice
  python chapterize.py 1               # render segment #1
  python chapterize.py 1 2 3           # render several
  python chapterize.py all             # render everything
"""

import asyncio
import re
import statistics
import sys
from pathlib import Path

import fitz
import edge_tts
from pydub import AudioSegment

PDF = Path("Recommended_Action_Macedonian_9p5pt_RECTO_FIXED.pdf")
OUT_DIR = Path("audiobook")
BODY_SIZE = 9.5
HEAD_SIZE = 13.0
VOICE_NUMBERED = "mk-MK-MarijaNeural"       # ALL-CAPS chapters
VOICE_INTERLUDE = "mk-MK-AleksandarNeural"  # mixed-case interludes
MAX_CHARS = 4000
GAP_MS = 300
MAX_RETRIES = 5


def _is_size(span, size):
    return abs(span["size"] - size) < 0.6


def reconstruct_heading(page):
    """Return heading text for a page (joining wrapped lines), or None."""
    pieces = []
    for b in page.get_text("rawdict")["blocks"]:
        for ln in b.get("lines", []):
            chars = [c for sp in ln["spans"] if _is_size(sp, HEAD_SIZE) for c in sp["chars"]]
            if not chars:
                continue
            chars.sort(key=lambda c: c["bbox"][0])
            gaps = [chars[i + 1]["bbox"][0] - chars[i]["bbox"][2] for i in range(len(chars) - 1)]
            med = statistics.median(gaps) if gaps else 0
            s = chars[0]["c"]
            for i, g in enumerate(gaps):
                if g > max(med * 1.8, 1.5):
                    s += " "
                s += chars[i + 1]["c"]
            pieces.append(s.strip())
    if not pieces:
        return None
    text = " ".join(pieces)
    # If it's letter-spaced caps ("С Л О Ј  З А"), collapse: single space = join,
    # run of 2+ spaces = word break.
    toks = text.split(" ")
    singles = sum(1 for t in toks if len(t) == 1)
    if toks and singles / len(toks) >= 0.6:
        text = re.sub(r" {2,}", "§", text).replace(" ", "").replace("§", " ").strip()
    return text


def voice_for(heading: str) -> str:
    letters = [c for c in heading if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return VOICE_NUMBERED          # ALL-CAPS -> numbered chapter -> Marija
    return VOICE_INTERLUDE             # mixed-case -> interlude -> Aleksandar


def body_text(page) -> str:
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            txt = "".join(sp["text"] for sp in ln["spans"] if _is_size(sp, BODY_SIZE))
            if txt.strip():
                lines.append(txt)
    return "\n".join(lines)


def build_segments():
    doc = fitz.open(PDF)
    heads = []  # (page, heading)
    for pno in range(doc.page_count):
        h = reconstruct_heading(doc[pno])
        if h:
            heads.append((pno, h))

    segments = []
    for i, (start, heading) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else doc.page_count
        raw = "\n".join(body_text(doc[p]) for p in range(start, end))
        raw = re.sub(r"-\n", "", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        segments.append(
            {
                "seq": i + 1,
                "pages": (start, end - 1),
                "heading": heading,
                "voice": voice_for(heading),
                "text": f"{heading}. {raw}",
            }
        )
    doc.close()
    return segments


def split_into_chunks(text, max_chars=MAX_CHARS):
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks, current = [], ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for s in sentences:
        if not s:
            continue
        if len(current) + len(s) + 1 <= max_chars:
            current = f"{current} {s}".strip()
        else:
            flush()
            if len(s) <= max_chars:
                current = s
            else:
                for w in s.split():
                    if len(current) + len(w) + 1 <= max_chars:
                        current = f"{current} {w}".strip()
                    else:
                        flush()
                        current = w
    flush()
    return chunks


async def synth_chunk(text, out_path, voice, idx, total):
    if out_path.exists() and out_path.stat().st_size > 0:
        print(f"    [{idx}/{total}] cached")
        return
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await edge_tts.Communicate(text, voice).save(str(out_path))
            if out_path.stat().st_size > 0:
                print(f"    [{idx}/{total}] ok ({len(text)} chars)")
                return
            raise RuntimeError("empty file")
        except Exception as exc:
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            if attempt == MAX_RETRIES:
                raise
            print(f"    [{idx}/{total}] retry {attempt}: {exc}")
            await asyncio.sleep(delay)
            delay *= 2


async def render(seg, out_dir: Path) -> Path:
    safe = re.sub(r"[^0-9A-Za-zА-Шарс ]+", "", seg["heading"])[:40].strip().replace(" ", "_")
    vshort = "marija" if seg["voice"] == VOICE_NUMBERED else "aleksandar"
    cache = out_dir / f"seg{seg['seq']:02d}_chunks"
    cache.mkdir(parents=True, exist_ok=True)
    chunks = split_into_chunks(seg["text"])
    print(f"Seg {seg['seq']:02d} [{vshort}] {seg['heading']!r}  "
          f"pp{seg['pages'][0]}-{seg['pages'][1]}  ({len(seg['text'])} chars, {len(chunks)} chunks)")
    paths = []
    for j, c in enumerate(chunks, 1):
        p = cache / f"chunk_{j:04d}.mp3"
        await synth_chunk(c, p, seg["voice"], j, len(chunks))
        paths.append(p)
    combined = AudioSegment.empty()
    gap = AudioSegment.silent(duration=GAP_MS)
    for k, p in enumerate(paths):
        s = AudioSegment.from_mp3(p)
        combined += s if k == 0 else gap + s
    out = out_dir / f"seg{seg['seq']:02d}_{vshort}_{safe}.mp3"
    combined.export(out, format="mp3")
    print(f"  -> {out}  ({out.stat().st_size // 1024} KB)")
    return out


def main():
    segs = build_segments()
    OUT_DIR.mkdir(exist_ok=True)
    args = sys.argv[1:]
    if not args:
        for s in segs:
            v = "Marija    " if s["voice"] == VOICE_NUMBERED else "Aleksandar"
            print(f"{s['seq']:2}. [{v}] {s['heading']}  (pp{s['pages'][0]}-{s['pages'][1]}, {len(s['text'])} chars)")
        return
    want = {a for a in args}
    for s in segs:
        if "all" in want or str(s["seq"]) in want:
            asyncio.run(render(s, OUT_DIR))


if __name__ == "__main__":
    main()
