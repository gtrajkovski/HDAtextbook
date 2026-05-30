"""
Build the intro (00) and outro (last) tracks around the supplied credits music.

Intro : music opens -> voice (title/authors) enters over a ducked bed -> music plays out.
Outro : closing line over the music tail -> music resolves and fades.
"""

import asyncio
from pathlib import Path
import edge_tts
from pydub import AudioSegment

VOICE = "mk-MK-AleksandarNeural"
RATE = "-5%"
MUSIC = "opening_credits.mp3"
OUT = Path("audiobook_latin"); OUT.mkdir(exist_ok=True)

INTRO_TXT = ("Препорачана акција. Роман од Горан Трајковски и Алан Шукарт. "
             "Аудиоиздание на македонски јазик.")
OUTRO_TXT = ("Ова беше „Препорачана акција“, роман од Горан Трајковски и Алан Шукарт. "
             "Ви благодариме што слушавте.")

DUCK_DB = -11  # how much to lower music under the voice


async def _tts(text, path):
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(path)


def _voice(text):
    asyncio.run(_tts(text, "/tmp/v.mp3"))
    return AudioSegment.from_mp3("/tmp/v.mp3")


def _duck_and_overlay(music, voice, lead_in):
    before = music[:lead_in]
    during = music[lead_in:lead_in + len(voice)].apply_gain(DUCK_DB)
    after = music[lead_in + len(voice):]
    bed = before + during + after
    return bed.overlay(voice, position=lead_in)


def build_intro(out_path):
    music = AudioSegment.from_mp3(MUSIC)
    voice = _voice(INTRO_TXT)
    lead_in = 5000                      # music alone for 5s before the voice
    mixed = _duck_and_overlay(music, voice, lead_in).fade_in(1200).fade_out(3500)
    mixed.export(out_path, format="mp3")
    print(f"intro -> {out_path} ({len(mixed)//1000}s, {Path(out_path).stat().st_size//1024} KB)")


def build_outro(out_path):
    music = AudioSegment.from_mp3(MUSIC)
    voice = _voice(OUTRO_TXT)
    lead_in = 1800
    need = lead_in + len(voice) + 3500
    music_out = music[max(0, len(music) - need):]   # use the resolving tail
    mixed = _duck_and_overlay(music_out, voice, lead_in).fade_in(800).fade_out(3500)
    mixed.export(out_path, format="mp3")
    print(f"outro -> {out_path} ({len(mixed)//1000}s, {Path(out_path).stat().st_size//1024} KB)")


if __name__ == "__main__":
    build_intro(str(OUT / "00_Aleksandar_Voved.mp3"))
    build_outro(str(OUT / "39_Aleksandar_Zavrshetok.mp3"))
