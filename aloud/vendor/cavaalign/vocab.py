# SPDX-License-Identifier: AGPL-3.0-or-later
"""Token set for CavaAlign: a 29-class romanized alphabet.

Character-level, deliberately: characters align straight onto the book's own orthography, which
is the text the reader is looking at, with no per-language lexicon or G2P. Every script reaches
this alphabet through `cavaalign.romanize`, so one model serves every language.
"""
from __future__ import annotations

BLANK = 0

# blank, 26 letters ordered by frequency, apostrophe, and `<unk>` (written `*` in romanized text),
# a wildcard for anything with no romanization. There is no space class: the DP scores spaces from
# the blank column (`align.Token.is_space`), so a space maps onto the blank id.
ROMAN_LETTERS = list("aienoutsrmkldghybpwcvjzf") + ["'"] + list("qx")


class VocabRoman:
    """The 29-class romanized vocab used by cavaalign.onnx."""

    name = "roman29"

    def __init__(self):
        self.chars = list(ROMAN_LETTERS)
        self.specials = ["<unk>"]
        self.tokens = ["<blank>"] + self.chars + self.specials
        self.stoi = {t: i for i, t in enumerate(self.tokens)}
        self.itos = {i: t for t, i in self.stoi.items()}
        self.char_to_id = {c: self.stoi[c] for c in self.chars}
        self.char_to_id["*"] = self.stoi["<unk>"]
        self.unk = self.stoi["<unk>"]

    def id_for(self, ch: str) -> int:
        """Token id for one character, case-folded; a space is the blank id."""
        if ch == " ":
            return BLANK
        return self.char_to_id.get(ch.lower(), self.unk)

    def __len__(self) -> int:
        return len(self.tokens)

    @property
    def vocab_size(self) -> int:
        return len(self.tokens)

    def encode(self, text: str) -> list[int]:
        return [self.id_for(c) for c in text]

    def decode(self, ids: list[int], collapse_repeats: bool = True) -> str:
        out: list[str] = []
        prev = None
        for i in ids:
            if i == BLANK:
                prev = None
                continue
            if collapse_repeats and i == prev:
                continue
            prev = i
            tok = self.itos[i]
            if not tok.startswith("<"):
                out.append(tok)
        return "".join(out)


def vocab_for_tokens(tokens: list[str] | None) -> VocabRoman:
    """The vocab matching a model's `vocab_tokens` metadata.

    Raises rather than guessing: a model whose output ids mean different characters from the ones
    the aligner asks for still runs and still produces timings, and they are wrong.
    """
    v = VocabRoman()
    if tokens is not None and list(tokens) != v.tokens:
        raise RuntimeError(f"unknown vocabulary ({len(tokens)} tokens, e.g. {list(tokens)[1:5]!r})")
    return v
