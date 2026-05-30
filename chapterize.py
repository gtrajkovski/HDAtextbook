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
# Per-voice speaking rate (edge-tts). Marija reads a touch slower.
RATE = {
    VOICE_NUMBERED: "-8%",
    VOICE_INTERLUDE: "+0%",
}
MAX_CHARS = 4000
GAP_MS = 300
MAX_RETRIES = 5


MONO_FONT = "DejaVuSansMono"
MONO_MARKER = "На екранот се прикажува извадок код."

# --- speech normalization -------------------------------------------------
# Latin words / terms -> Cyrillic (both the Macedonian gloss and this are read).
TRANSLIT = {
    "Vanguard": "Вангард", "Medical": "Медикал", "Billing": "Билинг",
    "Canton": "Кантон", "MERA": "МЕРА", "VoIP": "Воип", "IP": "Ај-Пи",
    "sandbox": "сандбокс", "rollback": "ролбек", "commit": "комит",
    "portmirroring": "порт-мирроринг", "verdigris": "вердигрис",
    "enter": "ентер", "II": "Втор", "USB": "У-Ес-Бе",
}
# Multi-token / hyphenated terms handled before single-word substitution.
TRANSLIT_PHRASES = {
    "keep-alive": "кип-алајв",
}
# Latin letter -> Cyrillic letter for spelling out ID codes.
LAT2CYR = {
    "A": "А", "B": "Б", "C": "Ц", "D": "Д", "E": "Е", "F": "Ф", "G": "Г",
    "H": "Х", "I": "И", "J": "Ј", "K": "К", "L": "Л", "M": "М", "N": "Н",
    "O": "О", "P": "П", "Q": "К", "R": "Р", "S": "С", "T": "Т", "U": "У",
    "V": "В", "W": "В", "X": "Х", "Y": "Ј", "Z": "З",
}
_ONES = ["нула", "еден", "два", "три", "четири", "пет", "шест", "седум", "осум", "девет"]
_TEENS = {10: "десет", 11: "единаесет", 12: "дванаесет", 13: "тринаесет",
          14: "четиринаесет", 15: "петнаесет", 16: "шеснаесет",
          17: "седумнаесет", 18: "осумнаесет", 19: "деветнаесет"}
_TENS = {2: "дваесет", 3: "триесет", 4: "четириесет", 5: "педесет",
         6: "шеесет", 7: "седумдесет", 8: "осумдесет", 9: "деведесет"}


def _digits(num: str) -> str:
    return "-".join(_ONES[int(d)] for d in num)


def _num_group(num: str) -> str:
    # Leading zero -> read digit by digit (e.g. 04 -> нула-четири).
    if len(num) >= 2 and num[0] == "0":
        return _digits(num)
    n = int(num)
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n]
    if n < 100:
        t = _TENS[n // 10]
        return t if n % 10 == 0 else f"{t} и {_ONES[n % 10]}"
    return _digits(num)  # 3+ digits: spell digits


def _spell_code(token: str) -> str:
    parts = re.split(r"[-–]", token)
    out = []
    for p in parts:
        # split a part into letter-runs and digit-runs (e.g. MAM095, 95)
        for run in re.findall(r"[A-Za-z]+|\d+", p):
            if run[0].isdigit():
                out.append(_num_group(run))
            else:
                out.append("-".join(LAT2CYR.get(c.upper(), c) for c in run))
    return ", ".join(out)


def _spell_hex(m) -> str:
    digits = m.group(1)
    spoken = []
    for c in digits:
        spoken.append(_ONES[int(c)] if c.isdigit() else LAT2CYR.get(c.upper(), c))
    return "нула-икс-" + "-".join(spoken)


def normalize_for_speech(text: str) -> str:
    # 1) hex literals: 0x28 -> нула-икс-два-осум
    text = re.sub(r"\b0x([0-9A-Fa-f]+)\b", _spell_hex, text)
    # 2) ID codes with hyphens: INST-SK-LOG-95-04, E-95-21 -> spelled out.
    #    Require >=1 letter so plain numeric ranges (e.g. 1994-1995) are left alone.
    def _code(m):
        tok = m.group(0)
        return _spell_code(tok) if re.search(r"[A-Za-z]", tok) else tok
    text = re.sub(r"\b[A-Z0-9]+(?:[-–][A-Z0-9]+)+\b", _code, text)
    # 3) model codes: MAM095 -> М-А-М, нула-девет-пет
    text = re.sub(r"\b[A-Z]{2,}\d{2,}\b",
                  lambda m: _spell_code(m.group(0)), text)
    # 4) hyphenated English phrases
    for k, v in TRANSLIT_PHRASES.items():
        text = re.sub(re.escape(k), v, text, flags=re.IGNORECASE)
    # 5) Latin words/runs -> Cyrillic (maximal Latin run, even when fused to
    #    Cyrillic like 'USBпорт'; add a separator before trailing Cyrillic).
    def _w(m):
        w = m.group(0)
        rep = TRANSLIT.get(w, TRANSLIT.get(w.capitalize(), w))
        nxt = m.string[m.end():m.end() + 1]
        if nxt and "Ѐ" <= nxt <= "ӿ":
            rep += "-"
        return rep
    text = re.sub(r"[A-Za-z]{2,}", _w, text)
    # 6) tidy heading separators: "1995 · II: Границата" reads cleanly
    text = text.replace(" · ", ", ")
    return text


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
        block_has_mono = False
        block_lines = []
        for ln in b.get("lines", []):
            txt = "".join(sp["text"] for sp in ln["spans"] if _is_size(sp, BODY_SIZE))
            if txt.strip():
                block_lines.append(txt)
            if any(sp["font"] == MONO_FONT for sp in ln["spans"]):
                block_has_mono = True
        lines.extend(block_lines)
        if block_has_mono:
            lines.append(MONO_MARKER)  # screen/code excerpt cue, in reading order
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
                "text": normalize_for_speech(f"{heading}. {raw}"),
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
            await edge_tts.Communicate(text, voice, rate=RATE.get(voice, "+0%")).save(str(out_path))
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
