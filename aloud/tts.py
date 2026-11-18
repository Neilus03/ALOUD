"""Kokoro synthesis with word timings, a priority queue and a disk cache."""
import hashlib
import heapq
import io
import itertools
import json
import threading
import traceback
from importlib.util import find_spec
from pathlib import Path

import numpy as np
import soundfile as sf

from .extract_doc import spoken_text
from .paths import SUPPORT

SR = 24000
REPO = "hexgrad/Kokoro-82M"

VOICES = [
    # id, label, grade (from Kokoro's VOICES.md)
    ("af_heart", "Heart · US female", "A"),
    ("af_bella", "Bella · US female", "A-"),
    ("af_lessac", "Lessac · US female, expressive (questions rise)", "P"),
    ("af_nicole", "Nicole · US female, soft", "B-"),
    ("bf_emma", "Emma · UK female", "B-"),
    ("af_kore", "Kore · US female", "C+"),
    ("af_aoede", "Aoede · US female", "C+"),
    ("af_sarah", "Sarah · US female", "C+"),
    ("am_fenrir", "Fenrir · US male", "C+"),
    ("am_michael", "Michael · US male", "C+"),
    ("am_puck", "Puck · US male", "C+"),
    ("bf_isabella", "Isabella · UK female", "C"),
    ("bm_george", "George · UK male", "C"),
    ("bm_fable", "Fable · UK male", "C"),
    ("ef_dora", "Dora · Spanish female", "es"),
    ("em_alex", "Alex · Spanish male", "es"),
    ("ff_siwis", "Siwis · French female", "fr"),
    ("if_sara", "Sara · Italian female", "it"),
    ("im_nicola", "Nicola · Italian male", "it"),
    ("pf_dora", "Dora · Portuguese female", "pt"),
    ("pm_alex", "Alex · Portuguese male", "pt"),
]
LANG_OF_VOICE = {"a": "en", "b": "en", "e": "es", "f": "fr", "i": "it", "p": "pt", "d": "de"}
DEFAULT_VOICE = {"en": "af_heart", "es": "ef_dora", "fr": "ff_siwis", "it": "if_sara", "pt": "pf_dora"}
PIPER_DIR = SUPPORT / "piper"
PIPER_MODELS = {}
if find_spec("piper") is not None:
    for voice, model in {"de_thorsten": "de_DE-thorsten-high", "af_lessac": "en_US-lessac-high"}.items():
        if (PIPER_DIR / f"{model}.onnx").is_file() and (PIPER_DIR / f"{model}.onnx.json").is_file():
            PIPER_MODELS[voice] = model
if "de_thorsten" in PIPER_MODELS:
    VOICES.append(("de_thorsten", "Thorsten · German male", "de"))
    DEFAULT_VOICE["de"] = "de_thorsten"
VOICE_IDS = {voice[0] for voice in VOICES}

# ------------------------------------------------------------------ voice cleanup

def _eq_gain_db(f):
    """Gain curve (dB) aimed at Kokoro's boxy, metallic 'speaking from a can' colour."""
    lf = np.log2(np.maximum(f, 1.0))

    def bell(fc, gain, octaves):
        return gain * np.exp(-0.5 * ((lf - np.log2(fc)) / (octaves / 2.355)) ** 2)

    g = np.zeros_like(f)
    g += np.where(f < 70, -18 * (1 - f / 70), 0)          # rumble
    g += bell(160, 1.5, 1.2)                              # a little warmth / chest
    g += bell(480, -3.5, 1.1)                             # boxy "can" resonance
    g += bell(1000, -1.5, 1.0)                            # nasal honk
    g += bell(3200, 2.0, 1.4)                             # presence / clarity
    g += -6.0 / (1 + np.exp(-(f - 7500) / 700))           # metallic fizz from the vocoder
    return g


def clean_voice(audio):
    if len(audio) < 64:
        return audio
    n = 1 << int(np.ceil(np.log2(len(audio) + 4096)))
    spec = np.fft.rfft(audio, n)
    f = np.fft.rfftfreq(n, 1 / SR)
    spec *= 10 ** (_eq_gain_db(f) / 20)
    out = np.fft.irfft(spec, n)[: len(audio)]
    return out.astype(np.float32)


def finish(audio, clean):
    a = clean_voice(audio) if clean else audio.copy()
    # consistent loudness (speech RMS ~ -20 dBFS), with a peak ceiling
    voiced = a[np.abs(a) > 0.01]
    rms = float(np.sqrt(np.mean(voiced ** 2))) if len(voiced) else 0
    if rms > 1e-4:
        a *= min(0.1 / rms, 0.95 / max(1e-6, float(np.abs(a).max())))
    fade = min(len(a) // 2, 120)
    if fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        a[:fade] *= ramp
        a[-fade:] *= ramp[::-1]
    return np.clip(a, -1, 1)


def wav_bytes(audio):
    buf = io.BytesIO()
    sf.write(buf, audio, SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# ------------------------------------------------------------------ engine

class Engine:
    def __init__(self, cache_dir, device="cpu"):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.device = device
        self._heap = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._jobs = {}       # key -> dict(event, job, result, error)
        self._model = None
        self._pipes = {}
        self.ready = threading.Event()
        threading.Thread(target=self._worker, daemon=True, name="tts").start()

    # -- public
    @staticmethod
    def key_for(text, spans, voice, pause=0.35):
        spoken, _ = spoken_text(text, spans or [])
        ver = "v4p3" if voice in PIPER_MODELS else "v4"   # bump when a voice's timing method changes
        return hashlib.sha1(f"{ver}|{voice}|{spoken}|{pause}".encode()).hexdigest()[:20]

    def cached(self, key):
        meta = self.cache / f"{key}.json"
        if meta.exists() and (self.cache / f"{key}.wav").exists():
            try:
                return json.loads(meta.read_text())
            except ValueError:
                return None
        return None

    def request(self, text, spans, voice, priority, pause=0.35):
        if voice not in VOICE_IDS:
            raise ValueError("Unknown voice")
        key = self.key_for(text, spans, voice, pause)
        if self.cached(key):
            return key
        with self._cv:
            job = self._jobs.get(key)
            if job is None:
                job = self._jobs[key] = dict(event=threading.Event(), prio=priority, error=None,
                                             args=(text, spans, voice, pause))
            elif job["event"].is_set():
                return key
            if priority < job.get("queued_prio", 1e9):
                job["queued_prio"] = priority
                heapq.heappush(self._heap, (priority, next(self._seq), key))
                self._cv.notify()
        return key

    def wait(self, key, timeout=300):
        res = self.cached(key)
        if res:
            return res
        job = self._jobs.get(key)
        if job is None:
            return None
        job["event"].wait(timeout)
        if job["error"]:
            raise RuntimeError(job["error"])
        return self.cached(key)

    def audio(self, key, clean=True):
        data, _ = sf.read(self.cache / f"{key}.wav", dtype="float32")
        return wav_bytes(finish(data, clean))

    def drop_prefetch(self):
        """Forget queued low-priority work (e.g. after the user jumps elsewhere)."""
        with self._cv:
            self._heap = [h for h in self._heap if h[0] <= 0]
            heapq.heapify(self._heap)
            for k, j in list(self._jobs.items()):
                if not j["event"].is_set() and j.get("queued_prio", 0) > 0 and not j.get("running"):
                    j["error"] = "cancelled"
                    j["event"].set()
                    del self._jobs[k]

    # -- worker
    def _load(self):
        import torch
        from kokoro import KModel
        torch.set_num_threads(max(2, (__import__("os").cpu_count() or 4) - 2))
        self._model = KModel(repo_id=REPO).to(self.device).eval()
        # warm up: load the English pronunciation pipeline and run the model once, so the
        # first sentence the user asks for isn't also paying for start-up
        try:
            list(self._pipe("af_heart")("Ready.", voice="af_heart"))
        except Exception:
            traceback.print_exc()
        self.ready.set()

    def _pipe(self, voice):
        from kokoro import KPipeline
        lang = voice[0] if voice[0] in LANG_OF_VOICE else "a"
        if lang not in self._pipes:
            self._pipes[lang] = KPipeline(lang_code=lang, repo_id=REPO, model=self._model)
        return self._pipes[lang]

    def _worker(self):
        try:
            self._load()
        except Exception:
            traceback.print_exc()
        while True:
            with self._cv:
                while not self._heap:
                    self._cv.wait()
                prio, _, key = heapq.heappop(self._heap)
                job = self._jobs.get(key)
                if job is None or job["event"].is_set() or job.get("running"):
                    continue
                if prio > job.get("queued_prio", prio):
                    continue  # stale entry; a higher-priority copy exists
                job["running"] = True
            try:
                self._synth(key, *job["args"])
            except Exception as e:
                traceback.print_exc()
                job["error"] = str(e)
            finally:
                job["event"].set()

    def _piper(self, voice):
        if not hasattr(self, "_pipers"):
            self._pipers = {}
        if voice not in self._pipers:
            from piper import PiperVoice
            self._pipers[voice] = PiperVoice.load(str(PIPER_DIR / f"{PIPER_MODELS[voice]}.onnx"))
        return self._pipers[voice]

    def _synth_piper(self, spoken, voice):
        """Piper voices (German): sentence by sentence; word timings spread over each
        sentence's speech in proportion to its sounds (Piper gives no timings itself)."""
        import re
        pv = self._piper(voice)
        sr = pv.config.sample_rate
        chunks, words, offset = [], [], 0.0
        for m in re.finditer(r"[^.!?;:]+[.!?;:]*[”’\"')\]]*\s*", spoken):
            sent = m.group()
            if not re.search(r"\w", sent):
                continue
            parts = list(pv.synthesize(sent))
            if not parts:
                continue
            a = np.concatenate([c.audio_float_array for c in parts]).astype(np.float32)
            a = np.interp(np.arange(0, len(a), sr / SR), np.arange(len(a)), a).astype(np.float32)   # -> 24 kHz
            phon = "".join("".join(c.phonemes) for c in parts)
            env = np.abs(a)
            loud = np.nonzero(env > 0.03 * max(1e-6, float(env.max())))[0]
            t_start = (loud[0] / SR) if len(loud) else 0.0
            t_end = (loud[-1] / SR) if len(loud) else len(a) / SR
            # weight of each word: its speech sounds; punctuation after a word adds a pause
            groups = [g for g in phon.split(" ") if g]
            ws = [(m.start() + w.start(), m.start() + w.end()) for w in re.finditer(r"\w[\w'’\-]*", sent)]
            def weight(g):
                return sum(1 for ch in g if ch.isalpha() and ch not in "ˈˌː") + 3 * sum(ch in ",;:" for ch in g)
            gw = [weight(g) for g in groups if any(ch.isalpha() for ch in g)]
            if ws:
                if len(gw) != len(ws):
                    gw = [max(1, len(re.sub(r"\W", "", spoken[s:e]))) for s, e in ws]
                total = float(sum(gw)) or 1.0
                t = t_start
                for (s, e), w in zip(ws, gw):
                    d = (t_end - t_start) * w / total
                    words.append([s, e, round(offset + t, 3), round(offset + t + d, 3)])
                    t += d
            chunks.append(a)
            offset += len(a) / SR
        return chunks, words

    def _synth(self, key, text, spans, voice, pause):
        spoken, pieces = spoken_text(text, spans or [])
        chunks, words, offset, cursor = [], [], 0.0, 0
        if voice in PIPER_MODELS:
            if spoken.strip():
                chunks, words = self._synth_piper(spoken, voice)
            spoken = ""                      # skip the Kokoro path below
        else:
            pipe = self._pipe(voice)
        if spoken.strip():
            for r in pipe(spoken, voice=voice, speed=1.0, split_pattern=None):
                if r.audio is None:
                    continue
                a = r.audio.detach().cpu().numpy().astype(np.float32)
                if not r.tokens and r.pred_dur is not None:      # non-English: no token timings
                    est, cursor = _timings_from_durations(r, spoken, cursor)
                    words += [[s, e, round(offset + t0, 3), round(offset + t1, 3)] for s, e, t0, t1 in est]
                for t in r.tokens or []:
                    tt = (t.text or "").strip()
                    if not tt:
                        continue
                    pos = spoken.find(tt, cursor)
                    if pos < 0 or pos - cursor > 200:
                        continue
                    cursor = pos + len(tt)
                    if t.start_ts is None or t.end_ts is None or not any(c.isalnum() for c in tt):
                        continue
                    words.append([pos, pos + len(tt), round(offset + t.start_ts, 3), round(offset + t.end_ts, 3)])
                chunks.append(a)
                offset += len(a) / SR
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
        # trim the model's own padding so pauses between passages are ours and consistent
        lead = tail = 0
        if len(audio):
            env = np.abs(audio)
            loud = np.nonzero(env > 0.02 * max(1e-6, float(env.max())))[0]
            if len(loud):
                lead = max(0, int(loud[0]) - int(0.04 * SR))
                tail = min(len(audio), int(loud[-1]) + int(0.10 * SR))
                audio = audio[lead:tail]
        shift = lead / SR
        words = [[a, b, round(max(0.0, t0 - shift), 3), round(max(0.0, t1 - shift), 3)] for a, b, t0, t1 in words]
        audio = np.concatenate([audio, np.zeros(int(pause * SR), np.float32)])
        sf.write(self.cache / f"{key}.wav", audio, SR, subtype="FLOAT")
        meta = dict(key=key, dur=round(len(audio) / SR, 3), words=words)
        (self.cache / f"{key}.json").write_text(json.dumps(meta))


def _timings_from_durations(r, spoken, cursor):
    """Word timings from the model's per-phoneme durations (units of 1/40 s)."""
    import re
    ph = r.phonemes or ""
    dur = r.pred_dur.detach().cpu().numpy().astype(float)
    cum = np.concatenate([[0.0], np.cumsum(dur)]) / 40.0          # cum[i] = start of input id i
    groups, i = [], 0
    for m in re.finditer(r"\S+", ph):
        if any(c.isalpha() for c in m.group()):
            groups.append((cum[m.start() + 1], cum[m.end() + 1]))     # +1: the start token
    chunk = r.graphemes or ""
    base = spoken.find(chunk[:20], cursor) if chunk else -1
    base = cursor if base < 0 else base
    words = [(base + m.start(), base + m.end()) for m in re.finditer(r"\w[\w'’\-]*", chunk)]
    out = []
    if groups and words:
        G, W = len(groups), len(words)
        for j, (s, e) in enumerate(words):
            a = int(j * G / W) if G != W else j
            b = max(a + 1, int((j + 1) * G / W)) if G != W else j + 1
            out.append([s, e, groups[a][0], groups[min(b, G) - 1][1]])
    return out, base + len(chunk)


def raw_words(words, text, spans):
    """Map [spoken_start, spoken_end, t0, t1] onto this passage's own text."""
    _, pieces = spoken_text(text, spans or [])
    out = []
    for s, e, t0, t1 in words:
        rs, re_ = _to_raw(pieces, s, e)
        if out and out[-1][0] == rs and out[-1][1] == re_:
            out[-1][3] = t1                  # several spoken words for one replaced span ("for example")
        else:
            out.append([rs, re_, t0, t1])
    return out


def _to_raw(pieces, s, e):
    rs = re_ = None
    for ps, pe, raw_s, raw_e in pieces:
        if pe <= s or ps >= e:
            continue
        verbatim = (pe - ps) == (raw_e - raw_s)
        a = raw_s + (max(s, ps) - ps) if verbatim else raw_s
        b = raw_s + (min(e, pe) - ps) if verbatim else raw_e
        rs = a if rs is None else min(rs, a)
        re_ = b if re_ is None else max(re_, b)
    return (rs or 0), (re_ or 0)
