"""
Chapter-aware extractor + dual-voice renderer for 'Препорачана акција'.

Layout facts learned from the PDF:
  - Body text   : 9.5pt NotoSerif
  - Chapter head: 13pt   (ALL-CAPS letter-spaced = NUMBERED chapters -> Marija;
                          mixed-case = UNNUMBERED interludes        -> Aleksandar)
  - Part divider: 28pt 'ДЕЛ' + 9.5pt 'ПРВ/ВТОР/ТРЕТ' on its own page
  - Chapter no. : 12pt (dropped)
  - Page numbers: 9.5pt bare-digit line at top of every page (MUST be dropped)
  - Running head: 7pt spaced author/title line (dropped by font filter)
  - Screen code : 6.5pt DejaVuSansMono (replaced by a spoken marker)

Narration rules:
  - Never read page numbers, running headers/footers, or chapter ordinals.
  - Announce 'Прв дел / Втор дел / Трет дел' before the chapter that opens a part.
  - Read all numbers as natural Macedonian words (years, counts, times, IP, …).
  - Spell ID codes/hex; transliterate Latin terms to Cyrillic.

Usage:
  python chapterize.py            # list segments + voice
  python chapterize.py 1 2 3      # render some
  python chapterize.py all        # render everything
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
MONO_FONT = "DejaVuSansMono"
MONO_MARKER = "На екранот се прикажува извадок код."

VOICE_NUMBERED = "mk-MK-MarijaNeural"       # ALL-CAPS chapters
VOICE_INTERLUDE = "mk-MK-AleksandarNeural"  # mixed-case interludes
RATE = {VOICE_NUMBERED: "-8%", VOICE_INTERLUDE: "+0%"}

MAX_CHARS = 4000
GAP_MS = 300
MAX_RETRIES = 5

PART_LABEL = {"ПРВ": "Прв дел", "ВТОР": "Втор дел", "ТРЕТ": "Трет дел"}

# ===========================================================================
# Macedonian number-to-words
# ===========================================================================
_ONES = {0: "нула", 1: "еден", 2: "два", 3: "три", 4: "четири", 5: "пет",
         6: "шест", 7: "седум", 8: "осум", 9: "девет"}
_TEENS = {10: "десет", 11: "единаесет", 12: "дванаесет", 13: "тринаесет",
          14: "четиринаесет", 15: "петнаесет", 16: "шеснаесет",
          17: "седумнаесет", 18: "осумнаесет", 19: "деветнаесет"}
_TENS = {2: "дваесет", 3: "триесет", 4: "четириесет", 5: "педесет",
         6: "шеесет", 7: "седумдесет", 8: "осумдесет", 9: "деведесет"}
_HUND = {1: "сто", 2: "двесте", 3: "триста", 4: "четиристотини",
         5: "петстотини", 6: "шестстотини", 7: "седумстотини",
         8: "осумстотини", 9: "деветстотини"}


def _under_100(n: int) -> str:
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n]
    t = _TENS[n // 10]
    u = n % 10
    return t if u == 0 else f"{t} и {_ONES[u]}"


def _under_1000_parts(n: int) -> list[str]:
    parts = []
    if n // 100:
        parts.append(_HUND[n // 100])
    if n % 100:
        parts.append(_under_100(n % 100))
    return parts


def cardinal(n: int) -> str:
    if n == 0:
        return "нула"
    seq = []
    th, rest = divmod(n, 1000)
    if th:
        seq.append("илјада" if th == 1 else ("две илјади" if th == 2 else f"{cardinal(th)} илјади"))
    seq += _under_1000_parts(rest)
    if len(seq) == 1:
        return seq[0]
    last = seq[-1]
    head = " ".join(seq[:-1])
    return f"{head} {last}" if " и " in last else f"{head} и {last}"


# Feminine ordinal forms (for reading a year, which agrees with «година»).
_CARD2ORD = {
    "еден": "прва", "два": "втора", "три": "трета", "четири": "четврта",
    "пет": "петта", "шест": "шеста", "седум": "седма", "осум": "осма",
    "девет": "деветта", "десет": "десетта",
    "единаесет": "единаесетта", "дванаесет": "дванаесетта", "тринаесет": "тринаесетта",
    "четиринаесет": "четиринаесетта", "петнаесет": "петнаесетта", "шеснаесет": "шеснаесетта",
    "седумнаесет": "седумнаесетта", "осумнаесет": "осумнаесетта", "деветнаесет": "деветнаесетта",
    "дваесет": "дваесетта", "триесет": "триесетта", "четириесет": "четириесетта",
    "педесет": "педесетта", "шеесет": "шеесетта", "седумдесет": "седумдесетта",
    "осумдесет": "осумдесетта", "деведесет": "деведесетта",
    "сто": "стота", "двесте": "двестота", "триста": "тристота",
}


def year_words(n: int) -> str:
    """Read a year as a feminine ordinal: 1995 -> ...деведесет и петта."""
    words = cardinal(n).split(" ")
    words[-1] = _CARD2ORD.get(words[-1], words[-1] + "та")
    return " ".join(words)


def _digits_words(s: str) -> str:
    return "-".join(_ONES[int(d)] for d in s)


def _say_time(m) -> str:
    h, mm = int(m.group(1)), int(m.group(2))
    return f"{cardinal(h)} часот" if mm == 0 else f"{cardinal(h)} и {cardinal(mm)}"


def _say_ip(m) -> str:
    return " точка ".join(cardinal(int(o)) for o in m.group(0).split("."))


def _say_decimal(m) -> str:
    return f"{cardinal(int(m.group(1)))} запирка {_digits_words(m.group(2))}"


def _say_thousands(m) -> str:
    return cardinal(int(m.group(0).replace(".", "")))


# ===========================================================================
# Speech normalization (codes, Latin, numbers)
# ===========================================================================
TRANSLIT = {
    "Vanguard": "Вангард", "Medical": "Медикал", "Billing": "Билинг",
    "Canton": "Кантон", "MERA": "МЕРА", "VoIP": "Воип", "IP": "Ај-Пи",
    "sandbox": "сандбокс", "rollback": "ролбек", "commit": "комит",
    "portmirroring": "порт-мирроринг", "verdigris": "вердигрис",
    "enter": "ентер", "II": "Втор", "USB": "У-Ес-Бе",
}
TRANSLIT_PHRASES = {"keep-alive": "кип-алајв"}
LAT2CYR = {
    "A": "А", "B": "Б", "C": "Ц", "D": "Д", "E": "Е", "F": "Ф", "G": "Г",
    "H": "Х", "I": "И", "J": "Ј", "K": "К", "L": "Л", "M": "М", "N": "Н",
    "O": "О", "P": "П", "Q": "К", "R": "Р", "S": "С", "T": "Т", "U": "У",
    "V": "В", "W": "В", "X": "Х", "Y": "Ј", "Z": "З",
}


def _num_group(num: str) -> str:
    if len(num) >= 2 and num[0] == "0":
        return _digits_words(num)
    return cardinal(int(num))


def _spell_code(token: str) -> str:
    out = []
    for part in re.split(r"[-–]", token):
        for run in re.findall(r"[A-Za-z]+|\d+", part):
            if run[0].isdigit():
                out.append(_num_group(run))
            else:
                out.append("-".join(LAT2CYR.get(c.upper(), c) for c in run))
    return ", ".join(out)


def _spell_hex(m) -> str:
    spoken = [_ONES[int(c)] if c.isdigit() else LAT2CYR.get(c.upper(), c) for c in m.group(1)]
    return "нула-икс-" + "-".join(spoken)


def normalize_for_speech(text: str) -> str:
    # numbers that must be recognised before generic integer handling
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", _say_ip, text)          # IP
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", _say_time, text)               # time
    text = re.sub(r"\b0x([0-9A-Fa-f]+)\b", _spell_hex, text)              # hex
    # ID codes (>=1 letter so numeric ranges are left for cardinal handling)
    text = re.sub(r"\b[A-Z0-9]+(?:[-–][A-Z0-9]+)+\b",
                  lambda m: _spell_code(m.group(0)) if re.search(r"[A-Za-z]", m.group(0)) else m.group(0),
                  text)
    text = re.sub(r"\b[A-Z]{2,}\d{2,}\b", lambda m: _spell_code(m.group(0)), text)  # MAM095
    text = re.sub(r"\b\d{1,3}(?:\.\d{3})+\b", _say_thousands, text)        # 10.000
    text = re.sub(r"\b(\d+),(\d+)\b", _say_decimal, text)                  # 0,5
    text = re.sub(r"\b(?:19|20)\d{2}\b",                                   # years -> ordinal
                  lambda m: year_words(int(m.group(0))), text)
    text = re.sub(r"\d+", lambda m: cardinal(int(m.group(0))), text)       # remaining integers
    # Latin terms
    for k, v in TRANSLIT_PHRASES.items():
        text = re.sub(re.escape(k), v, text, flags=re.IGNORECASE)

    def _w(m):
        w = m.group(0)
        rep = TRANSLIT.get(w, TRANSLIT.get(w.capitalize(), w))
        nxt = m.string[m.end():m.end() + 1]
        if nxt and "Ѐ" <= nxt <= "ӿ":
            rep += "-"
        return rep
    text = re.sub(r"[A-Za-z]{2,}", _w, text)
    text = text.replace(" · ", ", ")
    return text


# ===========================================================================
# PDF structure
# ===========================================================================
def _is_size(span, size):
    return abs(span["size"] - size) < 0.6


def reconstruct_heading(page):
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
    toks = text.split(" ")
    if toks and sum(1 for t in toks if len(t) == 1) / len(toks) >= 0.6:
        text = re.sub(r" {2,}", "§", text).replace(" ", "").replace("§", " ").strip()
    return text


def find_part_dividers(doc):
    """Return {page_no: 'Прв дел'} for the 28pt 'ДЕЛ' divider pages."""
    res = {}
    for pno in range(doc.page_count):
        has_del = ordinal = None
        for b in doc[pno].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    flat = sp["text"].replace(" ", "").strip()
                    if sp["size"] >= 24 and "ДЕЛ" in flat:
                        has_del = True
                    if flat in PART_LABEL:
                        ordinal = flat
        if has_del and ordinal:
            res[pno] = PART_LABEL[ordinal]
    return res


def voice_for(heading: str) -> str:
    letters = [c for c in heading if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return VOICE_NUMBERED
    return VOICE_INTERLUDE


def body_text(page) -> str:
    """9.5pt body only; drop bare page-number lines; mark code blocks."""
    lines = []
    for b in page.get_text("dict")["blocks"]:
        has_mono = False
        for ln in b.get("lines", []):
            txt = "".join(sp["text"] for sp in ln["spans"] if _is_size(sp, BODY_SIZE))
            if txt.strip() and not re.fullmatch(r"\s*\d+\s*", txt):   # skip page numbers
                lines.append(txt)
            if any(sp["font"] == MONO_FONT for sp in ln["spans"]):
                has_mono = True
        if has_mono:
            lines.append(MONO_MARKER)
    return "\n".join(lines)


def build_segments():
    doc = fitz.open(PDF)
    dividers = find_part_dividers(doc)
    heads = [(p, reconstruct_heading(doc[p])) for p in range(doc.page_count)]
    heads = [(p, h) for p, h in heads if h]

    segments = []
    for i, (start, heading) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else doc.page_count
        body = "\n".join(body_text(doc[p]) for p in range(start, end) if p not in dividers)
        body = re.sub(r"-\n", "", body)
        body = re.sub(r"\s+", " ", body).strip()
        segments.append({
            "seq": i + 1, "pages": (start, end - 1), "heading": heading,
            "voice": voice_for(heading), "_body": body, "_part": None,
        })

    # Attach each part label to the first chapter that starts after its divider.
    for dp, label in sorted(dividers.items()):
        for s in segments:
            if s["pages"][0] > dp:
                s["_part"] = label
                break

    for s in segments:
        prefix = f"{s['_part']}. " if s["_part"] else ""
        s["text"] = normalize_for_speech(f"{prefix}{s['heading']}. {s['_body']}")
    doc.close()
    return segments


# ===========================================================================
# Render
# ===========================================================================
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
    safe = re.sub(r"[^0-9A-Za-zЀ-ӿ ]+", " ", seg["heading"])[:40].strip().replace(" ", "_")
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
            part = f"  <{s['_part']}>" if s["_part"] else ""
            print(f"{s['seq']:2}. [{v}] {s['heading']}  (pp{s['pages'][0]}-{s['pages'][1]}, {len(s['text'])} chars){part}")
        return
    want = {a for a in args}
    for s in segs:
        if "all" in want or str(s["seq"]) in want:
            asyncio.run(render(s, OUT_DIR))


if __name__ == "__main__":
    main()
