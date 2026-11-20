# SPDX-License-Identifier: AGPL-3.0-or-later
"""Any Latin or Cyrillic text -> the model's 29-class romanized alphabet.

The model expects uroman's romanization conventions. Three engines are available:

* ``table`` (default): a small context-aware table plus Unicode decomposition. Deterministic
  and dependency-free; `kotlin/Romanizer.kt` implements the same table. It matches uroman on
  Latin-script languages and Russian.
* ``uroman``: the reference convention. Requires the `uroman` package.
* ``icu``: ICU ``Any-Latin; Latin-ASCII``. Requires PyICU. Output can differ between ICU
  versions, so it is provided for comparison only.

Romanization runs per word, after `align.expand_text`, so word indices are unchanged. A word
that romanizes to nothing (e.g. a lone "—") becomes the `*` wildcard (`<unk>`).
"""
from __future__ import annotations

import unicodedata
from functools import lru_cache

from .align import Expansion

# ISO 639-1 -> the ISO 639-3 code uroman takes.
LANG3 = {"en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por",
         "nl": "nld", "pl": "pol", "ru": "rus"}

ALLOWED = set("abcdefghijklmnopqrstuvwxyz'")

# Latin letters whose romanization is not "strip the accent" (uroman's values).
LATIN_SPECIAL = {
    "ä": "ae", "å": "aa", "ö": "oe", "ø": "oe", "ü": "ue", "œ": "oe", "æ": "ae", "ß": "ss",
    "ç": "s", "ş": "sh", "ș": "sh", "ț": "ts", "þ": "th", "ð": "th", "ł": "l", "đ": "d",
    "ı": "i", "ŀ": "l", "ĳ": "ij",
}

# Cyrillic, following uroman for Russian. `е` depends on context: "ye" at word start and after
# а/и/о/у/ъ/ь, otherwise "e"; at word start before е/ё it is "e"; after е it alternates
# ("ее" -> "eye"). Word-final "ий" is "y" ("синий" -> "siny").
RU = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
      "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
      "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
      "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
      "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "w"}
RU_YE_AFTER = set("аиоуъь")


def _ru_e(prev: str, prev_out: str, nxt: str) -> str:
    if not prev:
        return "e" if nxt in ("е", "ё") else "ye"
    if prev == "е":
        return "e" if prev_out == "ye" else "ye"
    return "ye" if prev in RU_YE_AFTER else "e"


APOSTROPHES = {"’": "'", "‘": "'", "ʼ": "'", "`": "'"}


def _fold_latin(ch: str) -> str:
    if ch in LATIN_SPECIAL:
        return LATIN_SPECIAL[ch]
    # strip combining marks (Unicode category Mn)
    base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                   if unicodedata.category(c) != "Mn")
    return base


@lru_cache(maxsize=200_000)
def _table_word(word: str) -> str:
    w = [APOSTROPHES.get(c, c) for c in word.lower()]
    out = []
    prev = ""
    final_iy = len(w) >= 2 and w[-2] == "и" and w[-1] == "й"
    for i, ch in enumerate(w):
        if final_iy and i == len(w) - 2:
            out.append("")                  # word-final "ий" -> "y": the й supplies it
        elif ch == "е":
            out.append(_ru_e(prev, out[-1] if out else "", w[i + 1] if i + 1 < len(w) else ""))
        elif ch in RU:
            out.append(RU[ch])
        else:
            out.append(_fold_latin(ch))
        prev = ch
    return "".join(c for c in "".join(out) if c in ALLOWED)


_UROMAN = None
_ICU = None


def _uroman_word(word: str, lang: str) -> str:
    global _UROMAN
    if _UROMAN is None:
        import uroman
        _UROMAN = uroman.Uroman()
    r = _UROMAN.romanize_string(word, lcode=LANG3.get(lang))
    return "".join(c for c in r.lower() if c in ALLOWED)


def _icu_word(word: str, lang: str) -> str:
    global _ICU
    if _ICU is None:
        import icu
        _ICU = icu.Transliterator.createInstance("Any-Latin; Latin-ASCII")
    return "".join(c for c in _ICU.transliterate(word).lower() if c in ALLOWED)


def romanize_word(word: str, lang: str = "en", engine: str = "table") -> str:
    """One word (no spaces) -> characters of the romanized alphabet. May return ""."""
    if engine == "table":
        return _table_word(word)
    if engine == "uroman":
        return _uroman_word(word, lang)
    if engine == "icu":
        return _icu_word(word, lang)
    raise ValueError(f"unknown romanizer {engine!r}")


def _romanize_piece(piece: str, lang: str, engine: str) -> str:
    """A run of one word's expanded characters, possibly with internal spaces ("eighteen twelve")."""
    parts = [romanize_word(p, lang, engine) for p in piece.split(" ")]
    return " ".join(p for p in parts if p)


def number_readings_lang(digits: str, lang: str) -> list[str]:
    """Spoken forms of a digit run in `lang`, likeliest first (requires num2words).

    For 4-digit values between 1000 and 2100 the year reading comes first where num2words has one.
    Languages num2words does not support keep the digits, which romanize to the `*` wildcard.
    """
    from num2words import num2words
    n = int(digits)
    out = []
    try:
        card = num2words(n, lang=lang)
    except NotImplementedError:
        card = digits
    if len(digits) == 4 and 1000 <= n <= 2100:
        try:
            yr = num2words(n, lang=lang, to="year")
            if yr and yr != card:
                out.append(yr)
        except (NotImplementedError, TypeError, AttributeError):
            pass
    out.append(card)
    return [o.replace("-", " ").replace(",", "") for o in out]


def romanize_expansion(exp: Expansion, lang: str = "en", engine: str = "table") -> Expansion:
    """Map an [Expansion] onto the romanized alphabet, keeping words, word indices and groups.

    Each word is romanized as one unit (romanization is context-dependent) and `char_to_word`
    is rebuilt so every output character still points at its source word.
    """
    src, c2w = exp.text, exp.char_to_word
    gstart = {g[0]: g for g in exp.groups}
    out: list[str] = []
    o_c2w: list[int] = []
    groups: list[tuple[int, int, tuple[str, ...]]] = []

    def put(chars: str, wi: int) -> None:
        out.extend(chars)
        o_c2w.extend([wi] * len(chars))

    i, n = 0, len(src)
    while i < n:
        wi = c2w[i]
        j = i
        while j < n and c2w[j] == wi:
            j += 1
        if wi < 0:
            put(src[i:j], -1)
            i = j
            continue
        start_len = len(out)
        k = i
        while k < j:
            if k in gstart:
                a, b, readings = gstart[k]
                rom = tuple(r for r in (_romanize_piece(x, lang, engine) for x in readings) if r)
                rom = tuple(dict.fromkeys(rom)) or ("*",)
                s0 = len(out)
                put(rom[0], wi)
                if len(rom) > 1:
                    groups.append((s0, len(out), rom))
                k = b
                continue
            nxt = min([g for g in gstart if k < g < j] or [j])
            put(_romanize_piece(src[k:nxt], lang, engine), wi)
            k = nxt
        if len(out) == start_len:
            put("*", wi)                    # unromanizable word: a wildcard, never a hole
        i = j

    return Expansion(text="".join(out), char_to_word=o_c2w, words=list(exp.words), groups=groups)


def prepare_text(text: str, lang: str = "en", engine: str = "table") -> Expansion:
    """Text -> romanized [Expansion] ready for `expansion_to_tokens`."""
    from .align import expand_text
    return romanize_expansion(expand_text(text, lang=lang), lang=lang, engine=engine)
