# SPDX-License-Identifier: AGPL-3.0-or-later
"""Forced alignment of book text to CTC emissions, with skip arcs.

Given a `(T, V)` matrix of per-frame log-probabilities and the text, recover the time span of
every word. Unlike plain CTC forced alignment, the text does not have to match the audio exactly:
the DP can skip audio that is not in the text (announcements, music) and text that is never
spoken (front matter, unread footnotes), each at a penalty.

States are `(t, j, b)`: `t` audio frames consumed, `j` text tokens consumed, and `b` says whether
the path is inside token `j-1` or at a boundary. Arcs:

1. blank       `(t-1, j, *) -> (t, j, boundary)`, scored by the blank column.
2. emit        `(t-1, j-1, *) -> (t, j, inside)`, scored by token `j-1`'s column.
3. extend      `(t-1, j, inside) -> (t, j, inside)`, token `j-1` continues.
4. audio skip  `(t-1, j, *) -> (t, j, boundary)`, a frame of sound not in the text.
5. text skip   `(t, j-1, *) -> (t, j, boundary)`, a token that is never spoken.

Leading text can be skipped at a much lower rate (`free_text_prefix`), up to
`max_prefix_tokens`. Complexity is `O(T * band)` with a band around the expected diagonal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

NEG_INF = float("-inf")
BIG_NEG = -1.0e18

# backpointer arc ids
ARC_BLANK, ARC_EMIT, ARC_AUDIO_SKIP, ARC_TEXT_SKIP, ARC_EXTEND = 0, 1, 2, 3, 4
# bucket ids
BOUNDARY, INSIDE = 0, 1


@dataclass
class Token:
    """One alignable character of the expanded text.

    Space tokens are scored by the blank column (a word gap is silence) and belong to no word.
    """

    text: str
    id: int
    is_space: bool = False


@dataclass
class Expansion:
    """Text rewritten into alignable characters, plus the map back to word indices.

    `char_to_word[i]` is the word index of character `i`, or -1 for an inter-word space.
    `groups` holds `(start, end, readings)` for written forms with several spoken readings
    (numbers), used by [resolve_ambiguous].
    """

    text: str
    char_to_word: list[int]
    words: list[str]
    groups: list[tuple[int, int, tuple[str, ...]]] = field(default_factory=list)


@dataclass
class AlignConfig:
    """DP settings. The defaults match `align_config.json`."""

    blank: int = 0
    log_audio_skip: float = -5.0    # added to the frame's best non-blank log-prob
    log_text_skip: float = -4.0     # per text token never spoken
    band: int = 512                 # half-width in tokens around the band center; 0 = no band
    emit_floor: float = -20.0       # lower bound on emission scores
    pad_frames: int = 0             # widen each token span by this many frames either side
    free_text_prefix: bool = True   # leading text may be skipped at `log_prefix_skip` per token
    log_prefix_skip: float = -0.15
    max_prefix_tokens: int = 6000


@dataclass
class TokenSpan:
    token_index: int
    text: str
    start_frame: int
    end_frame: int          # exclusive
    skipped_text: bool = False
    unmatched: bool = False  # never emitted; the span is inherited from the previous token


@dataclass
class AlignResult:
    spans: list[TokenSpan]
    total_score: float
    frames: int
    audio_skips: int
    text_skips: int
    unmatched: int = 0
    prefix_skipped: int = 0     # leading tokens treated as unspoken front matter
    matched_frames: int = 0     # frames consumed by emit or extend arcs

    @property
    def skipped_audio_ratio(self) -> float:
        return self.audio_skips / max(1, self.frames)

    @property
    def skipped_text_ratio(self) -> float:
        return (self.text_skips + self.unmatched) / max(1, len(self.spans))

    @property
    def spoken_fraction(self) -> float:
        """Fraction of non-space tokens after the prefix that were matched to audio."""
        ev = self._evidence_spans()
        if not ev:
            return 0.0
        return sum(1 for s in ev if not s.unmatched) / len(ev)

    @property
    def prefix_ratio(self) -> float:
        """Fraction of the text treated as unspoken front matter."""
        if not self.spans:
            return 0.0
        return self.prefix_skipped / len(self.spans)

    @property
    def audio_matched_fraction(self) -> float:
        """Fraction of audio frames accounted for by the text."""
        return self.matched_frames / max(1, self.frames)

    @property
    def is_probable_mismatch(self) -> bool:
        """True when the recording most likely does not correspond to the text."""
        return self.prefix_ratio > 0.25 and self.audio_matched_fraction < 0.5

    def _evidence_spans(self) -> list[TokenSpan]:
        return [s for s in self.spans[self.prefix_skipped:] if s.text != " "]

    @property
    def measured_ratio(self) -> float:
        """Fraction of non-space tokens after the prefix whose span was measured from audio."""
        ev = self._evidence_spans()
        if not ev:
            return 0.0
        return sum(1 for s in ev if not s.unmatched) / len(ev)


# --------------------------------------------------------------------------------------
# The DP kernel
# --------------------------------------------------------------------------------------

try:
    from numba import njit

    _HAS_NUMBA = True
except Exception:                                  # pragma: no cover
    _HAS_NUMBA = False

    def njit(*a, **k):
        def deco(f):
            return f
        if a and callable(a[0]):
            return a[0]
        return deco


if _HAS_NUMBA:
    @njit(cache=True)
    def _dp_core(lp, tok_ids, is_space, blank_col, speech_col, lo, hi, ts, asp, J, T, W,
                 init_in, init_bd, bp_in, bp_bd, final_in, final_bd):
        """Two-bucket forward DP (compiled). Writes backpointers into `bp_in` / `bp_bd`.

        `lo`/`hi` (T,): per-frame half-open band over j, both non-decreasing.
        `init_in`/`init_bd` (J+1,): initial DP row. Backpointers are indexed `[t, j - lo[t]]`
        and hold `arc * 2 + source_bucket`. Returns (terminal score, terminal bucket, 0).
        """
        BIG = np.float32(-1.0e18)
        EMIT_FLOOR = np.float32(-20.0)
        prev_in = init_in.copy()
        prev_bd = init_bd.copy()
        cur_in = np.full(J + 1, BIG, np.float32)
        cur_bd = np.full(J + 1, BIG, np.float32)
        prev_a = 0
        prev_b = 0
        start_j = 0
        # Row buffers are swapped, so slots outside the current band are reset to BIG.
        cur_dlo = 0
        cur_dhi = 0
        prev_dlo = 0
        prev_dhi = J + 1

        for t in range(T):
            a = lo[t]
            b = hi[t]
            bt = blank_col[t]
            for j in range(prev_a, a):
                prev_in[j] = BIG
                prev_bd[j] = BIG
            for j in range(b, prev_b):
                prev_in[j] = BIG
                prev_bd[j] = BIG
            prev_a = a
            prev_b = b
            for j in range(cur_dlo, min(a, cur_dhi)):
                cur_in[j] = BIG
                cur_bd[j] = BIG
            for j in range(max(b, cur_dlo), cur_dhi):
                cur_in[j] = BIG
                cur_bd[j] = BIG
            cur_dlo = a
            cur_dhi = b
            for j in range(a, b):
                cur_in[j] = BIG
                cur_bd[j] = BIG
                bp_in[t, j - a] = 255
                bp_bd[t, j - a] = 255

                # boundary bucket: blank, audio skip
                p_in = prev_in[j]
                p_bd = prev_bd[j]
                best = BIG
                choice = np.uint8(255)
                src = np.uint8(0)
                if p_in > BIG / 2.0 or p_bd > BIG / 2.0:
                    if p_in >= p_bd:
                        best = p_in + bt
                        src = np.uint8(1)
                    else:
                        best = p_bd + bt
                        src = np.uint8(0)
                    choice = np.uint8(ARC_BLANK * 2) + src
                    cand_src = np.uint8(1) if p_in >= p_bd else np.uint8(0)
                    cand = (p_in if p_in >= p_bd else p_bd) + speech_col[t] + asp
                    if cand > best:
                        best = cand
                        choice = np.uint8(ARC_AUDIO_SKIP * 2) + cand_src
                cur_bd[j] = best
                bp_bd[t, j - a] = choice

                # inside bucket: emit token j-1, or extend it
                besti = BIG
                choicei = np.uint8(255)
                if j > 0:
                    k = j - 1
                    if is_space[k]:
                        e = bt
                    else:
                        e = lp[t, tok_ids[k]]
                        if e < EMIT_FLOOR:
                            e = EMIT_FLOOR
                    q_in = prev_in[j - 1]
                    q_bd = prev_bd[j - 1]
                    if q_in >= q_bd:
                        cand = q_in + e
                        csrc = np.uint8(1)
                    else:
                        cand = q_bd + e
                        csrc = np.uint8(0)
                    if cand > besti:
                        besti = cand
                        choicei = np.uint8(ARC_EMIT * 2) + csrc
                    cand = p_in + e                      # extend: only from inside
                    if cand > besti:
                        besti = cand
                        choicei = np.uint8(ARC_EXTEND * 2) + np.uint8(1)
                cur_in[j] = besti
                bp_in[t, j - a] = choicei

            # text-skip chain into the boundary bucket (same frame, exclusive scan over j)
            run = BIG
            run_bucket = np.uint8(0)
            for j in range(a, b):
                cand = run + np.float32(j) * ts
                if cand > cur_bd[j]:
                    cur_bd[j] = cand
                    bp_bd[t, j - a] = np.uint8(ARC_TEXT_SKIP * 2) + run_bucket
                v_in = cur_in[j]
                v_bd = cur_bd[j]
                if v_in >= v_bd:
                    v = v_in
                    vb = np.uint8(1)
                else:
                    v = v_bd
                    vb = np.uint8(0)
                v = v - np.float32(j) * ts
                if v > run:
                    run = v
                    run_bucket = vb

            prev_in, cur_in = cur_in, prev_in
            prev_bd, cur_bd = cur_bd, prev_bd
            prev_dlo, cur_dlo = cur_dlo, prev_dlo
            prev_dhi, cur_dhi = cur_dhi, prev_dhi
        for j in range(prev_b, J + 1):
            prev_in[j] = BIG
            prev_bd[j] = BIG
        for j in range(0, min(prev_dlo, J + 1)):
            prev_in[j] = BIG
            prev_bd[j] = BIG
        for j in range(J + 1):
            final_in[j] = prev_in[j]
            final_bd[j] = prev_bd[j]

        # terminal state is (T, J): every text token accounted for, spoken or skipped
        s_in = prev_in[J]
        s_bd = prev_bd[J]
        if s_in > s_bd:
            return float(s_in), 1, int(start_j)
        return float(s_bd), 0, int(start_j)
else:
    _dp_core = None


def _dp_core_py(lp, tok_ids, is_space, blank_col, speech_col, lo, hi, ts, asp, J, T, W,
                init_in, init_bd, bp_in, bp_bd, final_in, final_bd):
    """Same DP as `_dp_core` in numpy, used when numba is not installed."""
    BIG = np.float32(BIG_NEG)
    prev_in = init_in.copy()
    prev_bd = init_bd.copy()
    ts32 = np.float32(ts)
    prev_a, prev_b = 0, 0
    start_j = 0

    for t in range(T):
        a, b = int(lo[t]), int(hi[t])
        if prev_a < a:
            prev_in[prev_a:a] = BIG
            prev_bd[prev_a:a] = BIG
        if b < prev_b:
            prev_in[b:prev_b] = BIG
            prev_bd[b:prev_b] = BIG
        prev_a, prev_b = a, b
        sl = slice(a, b)
        js = np.arange(a, b, dtype=np.int64)
        n = b - a
        p_in = prev_in[js]
        p_bd = prev_bd[js]

        # boundary bucket: blank / audio skip
        p_any = np.maximum(p_in, p_bd)
        p_src = np.where(p_in >= p_bd, 1, 0).astype(np.uint8)
        c_blank = p_any + blank_col[t]
        c_audio = p_any + speech_col[t] + asp
        bd = np.maximum(c_blank, c_audio)
        bd_arc = np.where(c_audio > c_blank, ARC_AUDIO_SKIP, ARC_BLANK).astype(np.uint8)
        bd_choice = (bd_arc * 2 + p_src).astype(np.uint8)

        # inside bucket: emit / extend
        ei = np.full(n, BIG, dtype=np.float32)
        ei_choice = np.full(n, 255, dtype=np.uint8)
        has_prev = js > 0
        if has_prev.any():
            jm = np.where(has_prev, js - 1, 0)
            sp_mask = is_space[jm]
            e = np.where(sp_mask, blank_col[t],
                         np.maximum(lp[t, np.where(sp_mask, 0, tok_ids[jm])], -20.0)
                         ).astype(np.float32)
            q_in = prev_in[jm]
            q_bd = prev_bd[jm]
            q_any = np.maximum(q_in, q_bd)
            q_src = np.where(q_in >= q_bd, 1, 0).astype(np.uint8)
            c_emit = q_any + e
            c_ext = p_in + e
            best = np.maximum(c_emit, c_ext)
            best_choice = np.where(c_ext > c_emit,
                                   ARC_EXTEND * 2 + 1,
                                   ARC_EMIT * 2 + q_src).astype(np.uint8)
            ei = np.where(has_prev, best, BIG)
            ei_choice = np.where(has_prev, best_choice, 255).astype(np.uint8)

        # text-skip exclusive scan, in the same scalar form as the compiled kernel
        bd2 = bd.copy()
        choice2 = bd_choice.copy()
        run = np.float32(BIG)
        run_bucket = np.uint8(0)
        for k in range(n):
            j = k + a
            cand = run + np.float32(j) * ts32
            if cand > bd2[k]:
                bd2[k] = cand
                choice2[k] = np.uint8(ARC_TEXT_SKIP * 2) + run_bucket
            v_in = bd2[k]
            v_i = ei[k]
            if v_i >= v_in:
                v = v_i
                vb = np.uint8(1)
            else:
                v = v_in
                vb = np.uint8(0)
            v_shift = v - np.float32(j) * ts32
            if v_shift > run:
                run = v_shift
                run_bucket = vb

        cur_bd = np.full(J + 1, BIG, dtype=np.float32)
        cur_in = np.full(J + 1, BIG, dtype=np.float32)
        cur_bd[sl] = bd2
        cur_in[sl] = ei
        bp_bd[t, :] = 255
        bp_in[t, :] = 255
        bp_bd[t, 0:n] = choice2
        bp_in[t, 0:n] = ei_choice
        prev_in, prev_bd = cur_in, cur_bd
    if prev_b < J + 1:
        prev_in[prev_b:J + 1] = BIG
        prev_bd[prev_b:J + 1] = BIG
    final_in[:] = prev_in
    final_bd[:] = prev_bd

    if prev_in[J] > prev_bd[J]:
        return float(prev_in[J]), 1, start_j
    return float(prev_bd[J]), 0, start_j


_KERNEL = _dp_core if _dp_core is not None else _dp_core_py


def _band_limits(T: int, J: int, cfg: AlignConfig, band_center) -> tuple[np.ndarray, np.ndarray, int]:
    """Per-frame half-open band [lo, hi) over token index j, non-decreasing in t."""
    if band_center is not None and cfg.band > 0:
        bc = np.asarray(band_center, dtype=np.int64)
        if bc.shape[0] < T:
            bc = np.concatenate([bc, np.full(T - bc.shape[0], bc[-1] if bc.size else 0)])
        bc = bc[:T]
        bc = np.maximum.accumulate(bc)
        lo = np.clip(bc - cfg.band, 0, J).astype(np.int32)
        hi = np.clip(bc + cfg.band + 1, 1, J + 1).astype(np.int32)
        # With a free text prefix the path may start up to `cap` tokens ahead of the diagonal,
        # so the upper edge is widened by that amount, decaying to zero at the last frame.
        if cfg.free_text_prefix:
            cap = max(0, min(cfg.max_prefix_tokens, J))
            frac_left = 1.0 - (np.arange(T, dtype=np.float64) / max(1, T - 1))
            hi = (hi + np.round(cap * frac_left)).astype(np.int32)
            hi = np.minimum(hi, J + 1).astype(np.int32)
    else:
        lo = np.zeros(T, dtype=np.int32)
        hi = np.full(T, J + 1, dtype=np.int32)
    lo = np.maximum.accumulate(lo).astype(np.int32)
    hi = np.maximum.accumulate(hi).astype(np.int32)
    lo[0] = 0
    hi[-1] = J + 1
    lo = np.minimum(lo, J).astype(np.int32)
    hi = np.maximum(hi, lo + 1).astype(np.int32)
    hi = np.minimum(hi, J + 1).astype(np.int32)
    hi_next = np.concatenate([hi[1:], np.array([J + 1], dtype=np.int32)])
    W = int(np.max(hi_next - lo)) + 1
    return lo, hi, W


def _record(starts, ends, k, t):
    """Record that token `k` was spoken at frame `t`."""
    if k < 0:
        return
    if starts[k] < 0 or t < starts[k]:
        starts[k] = t
    if t + 1 > ends[k]:
        ends[k] = t + 1


def _backtrack(bp_in, bp_bd, lo, T, J, bucket, tokens, _start_hint=0):
    """Walk the backpointers from (T, J) and build spans.

    Returns (spans, audio_skips, text_skips, unmatched, prefix_skipped, matched_frames).
    """
    starts = np.full(J, -1, dtype=np.int64)
    ends = np.zeros(J, dtype=np.int64)
    skipped = np.zeros(J, dtype=bool)
    audio_skips = 0
    text_skips = 0
    matched_frames = 0
    t, j, b = T, J, bucket
    prefix_skipped = 0
    guard = (T + J) * 4 + 64
    while (t > 0 or j > 0) and guard > 0:
        guard -= 1
        if t == 0:
            # audio exhausted: the remaining leading text is unspoken front matter
            j -= 1
            if j >= 0:
                prefix_skipped += 1
                skipped[j] = True
            b = BOUNDARY
            continue
        off = j - int(lo[t - 1])
        if off < 0 or off >= bp_in.shape[1]:
            raise IndexError(
                f"backtrack out of band at t={t} j={j} lo={int(lo[t-1])} W={bp_in.shape[1]}")
        raw = int(bp_in[t - 1, off]) if b == INSIDE else int(bp_bd[t - 1, off])
        if raw == 255:
            break
        arc, src = raw // 2, raw % 2
        if arc == ARC_BLANK:
            t -= 1
            b = src
        elif arc == ARC_AUDIO_SKIP:
            t -= 1
            audio_skips += 1
            b = src
        elif arc == ARC_EMIT:
            t -= 1
            j -= 1
            _record(starts, ends, j, t)
            matched_frames += 1
            b = src
        elif arc == ARC_EXTEND:
            t -= 1
            _record(starts, ends, j - 1, t)
            matched_frames += 1
            b = src
        else:  # ARC_TEXT_SKIP
            j -= 1
            text_skips += 1
            if j >= 0:
                skipped[j] = True
            b = src

    spans: list[TokenSpan] = []
    unmatched = 0
    last_end = 0
    for j in range(J):
        if starts[j] >= 0:
            last_end = int(ends[j])
            spans.append(TokenSpan(j, tokens[j].text, int(starts[j]), int(ends[j]),
                                   skipped_text=bool(skipped[j])))
        else:
            unmatched += 1
            spans.append(TokenSpan(j, tokens[j].text, last_end, last_end,
                                   skipped_text=bool(skipped[j]), unmatched=True))
    return spans, audio_skips, text_skips, unmatched, prefix_skipped, matched_frames


def forced_align_dp(
    logp,
    tokens: Sequence[Token],
    cfg: AlignConfig = AlignConfig(),
    band_center=None,
    init_state=None,
):
    """Run the DP and return (result, final_state). `final_state` is the last DP row."""
    lp = np.asarray(logp.detach().cpu().numpy() if hasattr(logp, "detach") else logp,
                    dtype=np.float32)
    if lp.ndim != 2:
        raise ValueError(f"logp must be (T, V), got shape {lp.shape}")
    T, V = lp.shape
    J = len(tokens)
    if T == 0 or J == 0:
        empty = AlignResult([], 0.0, T, 0, 0, J)
        return empty, None

    blank_col = lp[:, cfg.blank].astype(np.float32)
    tok_ids = np.fromiter((tk.id for tk in tokens), dtype=np.int64, count=J)
    is_space = np.fromiter((tk.is_space for tk in tokens), dtype=bool, count=J)
    lo, hi, W = _band_limits(T, J, cfg, band_center)

    if init_state is None:
        init_in = np.full(J + 1, BIG_NEG, dtype=np.float32)
        init_bd = np.full(J + 1, BIG_NEG, dtype=np.float32)
        if cfg.free_text_prefix:
            cap = max(0, min(cfg.max_prefix_tokens, J))
            init_bd[:cap + 1] = (np.arange(cap + 1, dtype=np.float32)
                                 * np.float32(cfg.log_prefix_skip))
        else:
            init_bd[0] = 0.0
    else:
        init_in, init_bd = init_state

    bp_in = np.full((T, W), 255, dtype=np.uint8)
    bp_bd = np.full((T, W), 255, dtype=np.uint8)

    # best non-blank log-prob per frame, which prices the audio-skip arc
    nb = np.delete(lp, cfg.blank, axis=1) if lp.shape[1] > 1 else lp
    speech_col = nb.max(axis=1).astype(np.float32)
    np.maximum(speech_col, np.float32(cfg.emit_floor), out=speech_col)

    final_in = np.full(J + 1, BIG_NEG, dtype=np.float32)
    final_bd = np.full(J + 1, BIG_NEG, dtype=np.float32)
    score, bucket, start_j = _KERNEL(lp, tok_ids, is_space, blank_col, speech_col, lo, hi,
                                     np.float32(cfg.log_text_skip),
                                     np.float32(cfg.log_audio_skip),
                                     J, T, W, init_in, init_bd, bp_in, bp_bd, final_in, final_bd)

    final_state = (final_in, final_bd)
    if score > BIG_NEG / 2:
        spans, audio_skips, text_skips, unmatched, prefix_skipped, matched_frames = _backtrack(
            bp_in, bp_bd, lo, T, J, bucket, tokens, start_j)
        if cfg.pad_frames:
            for sp in spans:
                if not sp.unmatched:
                    sp.start_frame = max(0, sp.start_frame - cfg.pad_frames)
                    sp.end_frame = min(T, sp.end_frame + cfg.pad_frames)
        result = AlignResult(spans=spans, total_score=score, frames=T,
                             audio_skips=audio_skips, text_skips=text_skips,
                             unmatched=unmatched, prefix_skipped=int(prefix_skipped),
                             matched_frames=int(matched_frames))
    else:
        result = AlignResult(
            spans=[TokenSpan(j, tokens[j].text, 0, 0, unmatched=True) for j in range(J)],
            total_score=NEG_INF, frames=T, audio_skips=0, text_skips=0, unmatched=J)
    return result, final_state


def forced_align(logp, tokens: Sequence[Token], cfg: AlignConfig = AlignConfig(),
                 band_center=None) -> AlignResult:
    """One-shot alignment of `logp` against `tokens`."""
    result, _ = forced_align_dp(logp, tokens, cfg, band_center)
    return result


def linear_band_center(T: int, J: int) -> np.ndarray:
    """The straight diagonal from (0, 0) to (T, J), used as the band center."""
    if T <= 1:
        return np.zeros(max(T, 1), dtype=np.int64)
    return np.linspace(0, J, T).astype(np.int64)


# --------------------------------------------------------------------------------------
# Text -> tokens
# --------------------------------------------------------------------------------------

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

def number_to_words(n: int) -> str:
    """Cardinal English for a non-negative integer below one million."""
    if n < 0:
        return "negative " + number_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + ("" if o == 0 else " " + _ONES[o])
    if n < 1000:
        h, r = divmod(n, 100)
        s = _ONES[h] + " hundred"
        return s + ("" if r == 0 else " " + number_to_words(r))
    if n < 1_000_000:
        th, r = divmod(n, 1000)
        s = number_to_words(th) + " thousand"
        return s + ("" if r == 0 else " " + number_to_words(r))
    return str(n)


def number_readings(digits: str, style: str = "auto") -> list[str]:
    """Plausible English spoken forms of a digit run, likeliest first.

    "1812" -> ["eighteen twelve", "one thousand eight hundred twelve", "one eight one two"].
    """
    if not digits or not digits.isdigit():
        return [digits]
    n = int(digits)
    cardinal = number_to_words(n)
    out: list[str] = []
    if len(digits) == 4:
        hi, lo = int(digits[:2]), int(digits[2:])
        if 10 <= hi <= 99:
            if lo == 0:
                out.append(number_to_words(hi) + " hundred")
            elif lo < 10:
                out.append(number_to_words(hi) + " oh " + _ONES[lo])
                out.append(number_to_words(hi) + " " + number_to_words(lo))
            else:
                out.append(number_to_words(hi) + " " + number_to_words(lo))
    if style == "cardinal":
        out = [cardinal] + [o for o in out if o != cardinal]
    elif style == "year":
        out = out + [cardinal] if out else [cardinal]
    else:  # auto: split-year reading for 4 digits that look like a year, cardinal otherwise
        if not (3 <= len(digits) <= 4 and 100 <= n <= 2100 and out):
            out = [cardinal] + [o for o in out if o != cardinal]
    if cardinal not in out:
        out.append(cardinal)
    out.append(" ".join(_ONES[int(d)] for d in digits))
    seen, res = set(), []
    for o in out:
        if o and o not in seen:
            seen.add(o)
            res.append(o)
    return res


# A word is a run of letters/digits, with apostrophe continuations ("don't" is one word,
# "well-known" and "1,000" are two). Word indices in the output refer to this segmentation.
try:
    import regex as _regex_mod

    _WORD_RE = _regex_mod.compile(r"[\p{L}\p{Nd}]+(?:['’][\p{L}]+)*")
except ImportError:                    # pragma: no cover
    import unicodedata as _ud

    class _WordReFallback:
        """Same segmentation as the regex above, without the `regex` package."""

        @staticmethod
        def _is_word_char(ch: str) -> bool:
            cat = _ud.category(ch)
            return cat.startswith("L") or cat == "Nd"

        def finditer(self, text: str):
            out, i, n = [], 0, len(text)
            while i < n:
                while i < n and not self._is_word_char(text[i]):
                    i += 1
                if i >= n:
                    break
                start = i
                while i < n and self._is_word_char(text[i]):
                    i += 1
                while (i + 1 < n and text[i] in "'’"
                       and self._is_word_char(text[i + 1])):
                    j = i + 1
                    while j < n and self._is_word_char(text[j]):
                        j += 1
                    i = j
                out.append((start, i))
            return [_Match(a, b, text) for a, b in out]

    class _Match:
        def __init__(self, a: int, b: int, text: str):
            self._a, self._b, self._t = a, b, text

        def start(self, *_):
            return self._a

        def end(self, *_):
            return self._b

        def group(self, *_):
            return self._t[self._a:self._b]

    _WORD_RE = _WordReFallback()


def _word_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) character offsets of every word."""
    out: list[tuple[int, int]] = []
    for m in _WORD_RE.finditer(text):
        out.append((m.start(), m.end()))
    return out


def _punct_strip(w: str) -> tuple[str, str, str]:
    """Split a word into (leading punctuation, alphanumeric core, trailing punctuation)."""
    i, j = 0, len(w)
    while i < j and not w[i].isalnum():
        i += 1
    while j > i and not w[j - 1].isalnum():
        j -= 1
    return w[:i], w[i:j], w[j:]


def expand_text(text: str, *, number_style: str = "auto", lang: str = "en") -> Expansion:
    """Rewrite text into alignable characters, recording the map back to word indices.

    One space per inter-word gap (word -1); digit runs replaced by their likeliest reading
    (English readings for `lang="en"`, num2words otherwise), with alternatives in `groups`.
    The output keeps the original script; `romanize.romanize_expansion` maps it to the model's
    alphabet.
    """
    out: list[str] = []
    char_to_word: list[int] = []
    words: list[str] = []
    groups: list[tuple[int, int, tuple[str, ...]]] = []

    def put(chars: str, wi: int) -> None:
        for ch in chars:
            out.append(ch)
            char_to_word.append(wi)

    for wi, (ws, we) in enumerate(_word_spans(text)):
        word = text[ws:we]
        words.append(word)
        if out and out[-1] != " ":
            put(" ", -1)

        lead, core, _trail = _punct_strip(word)
        if not core:
            put(word, wi)
            continue

        if core.isdigit():
            if lang == "en":
                readings = number_readings(core, style=number_style)
            else:
                from .romanize import number_readings_lang
                readings = number_readings_lang(core, lang)
            put(lead, wi)
            span_start = len(out)
            put(readings[0], wi)
            if len(readings) > 1:
                groups.append((span_start, len(out), tuple(readings)))
            continue

        put(lead + core, wi)

    return Expansion(text="".join(out), char_to_word=char_to_word, words=words, groups=groups)


def expansion_to_tokens(exp: Expansion, vocab) -> list[Token]:
    """Expanded text -> one [Token] per character, via `vocab.id_for` (case-insensitive)."""
    space_id = vocab.id_for(" ")
    toks: list[Token] = []
    for ch in exp.text:
        if ch == " ":
            toks.append(Token(" ", space_id, is_space=True))
        else:
            toks.append(Token(ch, vocab.id_for(ch)))
    return toks


def resolve_ambiguous(logp, exp: Expansion, vocab, cfg: AlignConfig = AlignConfig(),
                      *, window_sec: float = 4.0, frame_shift: float = 0.02,
                      max_groups: int = 256) -> Expansion:
    """For each ambiguous written form, keep the reading that aligns best to the audio.

    Each candidate reading is aligned against a short window of audio around the word's
    expected position, and the best-scoring one replaces the default.
    """
    if not exp.groups:
        return exp
    lp = np.asarray(logp.detach().cpu().numpy() if hasattr(logp, "detach") else logp,
                    dtype=np.float32)
    T = lp.shape[0]
    win = max(8, int(window_sec / frame_shift))

    text = list(exp.text)
    c2w = list(exp.char_to_word)
    offset = 0
    for (span_start, span_end, readings) in exp.groups[:max_groups]:
        if len(readings) < 2:
            continue
        gs, ge = span_start + offset, span_end + offset
        if gs < 0 or ge > len(text):
            continue
        f_mid = int(np.clip(gs / max(1, len(text)) * T, 0, max(0, T - 1)))
        f_lo = max(0, f_mid - win // 2)
        f_hi = min(T, f_lo + win)
        if f_hi - f_lo < 4:
            continue
        window = lp[f_lo:f_hi]
        best_chars, best_score = None, None
        for reading in readings:
            cand = list(reading)
            local = "".join(text[:gs] + cand + text[ge:])
            sub = Expansion(text=local, char_to_word=[], words=[], groups=[])
            r = forced_align(window, expansion_to_tokens(sub, vocab), cfg)
            if best_score is None or r.total_score > best_score:
                best_score, best_chars = r.total_score, cand
        if best_chars is not None:
            delta = len(best_chars) - (ge - gs)
            text[gs:ge] = best_chars
            wi = c2w[gs] if gs < len(c2w) else (c2w[-1] if c2w else -1)
            c2w[gs:ge] = [wi] * len(best_chars)
            offset += delta
    return Expansion(text="".join(text), char_to_word=c2w, words=exp.words, groups=[])


def spans_to_words(
    text: str,
    spans: Sequence[TokenSpan],
    frame_shift: float,
    *,
    expansion: Expansion | None = None,
    offset_sec: float = 0.0,
    pad_sec: float = 0.0,
    emissions=None,
    snap_ends: bool = True,
    start_offset_sec: float | None = None,
    start_cluster_frames: int = 0,
) -> list[tuple[int, float, float, bool]]:
    """Token spans -> (word_index, start_sec, end_sec, measured) for every word.

    `frame_shift` is the model's frame duration in seconds (0.02 for cavaalign.onnx).
    With `emissions`, word ends are extended into the following pause (see [snap_word_ends]).
    Starts are moved earlier by `start_offset_sec` (default: one frame), which corrects the lag
    between a sound's onset and its character's posterior peak.
    With `start_cluster_frames` > 0, a first letter separated from the rest of its word by more
    than that many frames is not used as the word's start.
    A word is `measured` when more than half of its characters were matched to audio; other words
    inherit the previous word's position. Times never go backwards.
    """
    if expansion is None:
        expansion = expand_text(text)
    shift_sec = frame_shift if start_offset_sec is None else float(start_offset_sec)
    c2w = expansion.char_to_word
    n_words = len(expansion.words)
    if len(spans) != len(c2w):
        raise ValueError(f"spans ({len(spans)}) do not match expansion chars ({len(c2w)})")

    snap_starts = snap_ends_frames = None
    if emissions is not None and snap_ends:
        lp = np.asarray(emissions.detach().cpu().numpy()
                        if hasattr(emissions, "detach") else emissions, dtype=np.float32)
        snap_starts, snap_ends_frames = snap_word_ends(lp, spans, c2w, n_words)

    cluster_start: dict[int, int] = {}
    if start_cluster_frames > 0:
        by_word: dict[int, list] = {}
        for ci, sp in enumerate(spans):
            wi = c2w[ci]
            if wi >= 0 and not sp.unmatched:
                by_word.setdefault(wi, []).append(sp)
        for wi, sps in by_word.items():
            for a, b in zip(sps, sps[1:]):
                if b.start_frame - a.end_frame <= start_cluster_frames:
                    if a is not sps[0]:
                        cluster_start[wi] = a.start_frame
                    break
    starts = [math.inf] * n_words
    ends = [-math.inf] * n_words
    n_chars = [0] * n_words
    n_hit = [0] * n_words
    for ci, sp in enumerate(spans):
        wi = c2w[ci]
        if wi < 0:
            continue
        n_chars[wi] += 1
        if sp.unmatched:
            continue
        n_hit[wi] += 1
        if wi in cluster_start and sp.start_frame < cluster_start[wi]:
            continue
        if snap_starts is not None and snap_starts[wi] >= 0:
            s_frame = snap_starts[wi]
            e_frame = snap_ends_frames[wi]
        else:
            s_frame = sp.start_frame
            e_frame = sp.end_frame
        if wi in cluster_start:
            s_frame = max(s_frame, cluster_start[wi])
        e_rel = e_frame * frame_shift
        s_rel = max(0.0, min(s_frame * frame_shift - shift_sec, e_rel))
        s = s_rel + offset_sec
        e = e_rel + offset_sec
        if s < starts[wi]:
            starts[wi] = s
        if e > ends[wi]:
            ends[wi] = e

    out: list[tuple[int, float, float, bool]] = []
    last_end = 0.0
    for wi in range(n_words):
        measured = n_chars[wi] > 0 and n_hit[wi] * 2 > n_chars[wi]
        s, e = starts[wi], ends[wi]
        if s == math.inf:
            s = last_end
        if e == -math.inf or e < s:
            e = s + frame_shift
        if last_end > 0 and s < last_end - frame_shift:
            s = last_end - frame_shift
        if e < s:
            e = s
        if measured:
            last_end = max(last_end, e)
        out.append((wi, max(0.0, s - pad_sec), e + pad_sec, measured))
    return out


def snap_word_ends(
    lp: np.ndarray,
    spans: Sequence[TokenSpan],
    char_to_word: Sequence[int],
    n_words: int,
    *,
    blank: int = 0,
    max_expand: int = 8,
) -> tuple[list[int], list[int]]:
    """Per-word (starts, ends) in frames, with each end moved to the blank peak that follows.

    A word's last character peaks before the word finishes, so the end is moved forward to the
    frame with the highest blank log-prob within `max_expand` frames, never past the next word's
    start. Starts are left unchanged. Unmeasured words get -1.
    """
    T = lp.shape[0]
    blank_col = lp[:, blank]
    starts: list[int] = [-1] * n_words
    ends: list[int] = [-1] * n_words
    for ci, sp in enumerate(spans):
        if ci >= len(char_to_word):
            break
        wi = char_to_word[ci]
        if wi < 0 or sp.unmatched:
            continue
        if starts[wi] < 0 or sp.start_frame < starts[wi]:
            starts[wi] = sp.start_frame
        if ends[wi] < 0 or sp.end_frame > ends[wi]:
            ends[wi] = sp.end_frame

    for wi in range(n_words):
        if starts[wi] < 0:
            continue
        e = ends[wi]
        next_start = starts[wi + 1] if wi + 1 < n_words else -1
        lo = min(max(e, 0), T)
        hi = T if next_start < 0 else min(T, next_start)
        if hi - lo > 0:
            window = blank_col[lo:min(hi, lo + max_expand)]
            if window.size:
                peak = lo + int(np.argmax(window))
                ends[wi] = max(e, peak)
    return starts, ends


def align_text(
    logp,
    text: str,
    vocab,
    cfg: AlignConfig = AlignConfig(),
    *,
    lang: str = "en",
    frame_shift: float = 0.02,
    offset_sec: float = 0.0,
    pad_sec: float = 0.0,
    band_center=None,
    resolve_numbers: bool = False,
) -> tuple[list[tuple[int, float, float, bool]], AlignResult, Expansion]:
    """Emissions + text -> word timings. Returns (word_timings, diagnostics, expansion).

    `band_center` defaults to the straight diagonal when `cfg.band` > 0.
    """
    from .romanize import prepare_text
    exp = prepare_text(text, lang)
    if resolve_numbers and exp.groups:
        exp = resolve_ambiguous(logp, exp, vocab, cfg, frame_shift=frame_shift)
    toks = expansion_to_tokens(exp, vocab)
    if band_center is None and cfg.band > 0:
        band_center = linear_band_center(len(logp), len(toks))
    result = forced_align(logp, toks, cfg, band_center)
    words = spans_to_words(text, result.spans, frame_shift,
                           expansion=exp, offset_sec=offset_sec, pad_sec=pad_sec,
                           emissions=logp)
    return words, result, exp
