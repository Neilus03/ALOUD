"""ALOUD: read PDFs aloud with the text following the voice.

Runs a small local HTTP server (127.0.0.1 only) and shows it in a native window.
"""
import json
import re
import os
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HERE = Path(__file__).resolve().parent
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from .extract_doc import CROP_PADDING, doc_id, load_document, speech_spans, unit_text  # noqa: E402
from .paths import CACHE, NOTES, SUPPORT  # noqa: E402
from .tts import DEFAULT_VOICE, VOICES, Engine, raw_words  # noqa: E402

SUPPORT.mkdir(mode=0o700, parents=True, exist_ok=True)
STATE_FILE = SUPPORT / "state.json"

DEFAULT_SETTINGS = dict(voice="af_heart", rate=1.0, clean=True, skip_citations=True,
                        show_skipped=True, font=19, theme="auto", follow=True,
                        view="split", web_view="pdf", pdf_zoom=1.0, lang_voices={}, lexicon={},
                        focus=False, sweep=True, ambient=True, sounds=True, figure_previews=True)
SAMPLES = {
    "en": ("Lobbying and campaign finance are essential elements of modern democracy. "
           "On the positive side, they can help elected officials gather the information they need."),
    "es": "La competencia mantiene los precios bajos. Cuando dos empresas se fusionan, los precios pueden subir.",
    "fr": "La concurrence maintient les prix bas. Quand deux entreprises fusionnent, les prix peuvent augmenter.",
    "it": "La concorrenza mantiene bassi i prezzi. Quando due imprese si fondono, i prezzi possono salire.",
    "pt": "A concorrência mantém os preços baixos. Quando duas empresas se fundem, os preços podem subir.",
    "de": "Wettbewerb hält die Preise niedrig. Wenn zwei Unternehmen fusionieren, können die Preise steigen.",
}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.data = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("settings", {})
        self.data["settings"] = {**DEFAULT_SETTINGS, **self.data["settings"]}
        available = {voice[0] for voice in VOICES}
        if self.data["settings"]["voice"] not in available:
            self.data["settings"]["voice"] = DEFAULT_SETTINGS["voice"]
        voices = self.data["settings"]["lang_voices"] or {}
        self.data["settings"]["lang_voices"] = {lang: voice for lang, voice in voices.items() if voice in available}
        self.data.setdefault("docs", {})

    def save(self):
        with self.lock:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.replace(STATE_FILE)


STATE = State()
ENGINE = Engine(CACHE / "audio", device="cpu")
DOCS = {}          # id -> doc
DOC_LOCK = threading.Lock()
STATUS = {"msg": ""}       # what a slow open is doing (shown in the player bar)
DOC_CACHE = CACHE / "docs"
DOC_CACHE.mkdir(parents=True, exist_ok=True)
# extracted documents are cached on disk; any change to the extraction code invalidates them
import hashlib as _hl
EXTRACT_VERSION = _hl.sha1(b"".join((HERE / f).read_bytes() for f in ("extract_doc.py", "convert.py"))).hexdigest()[:10]
PDF_LOCK = threading.Lock()
PDFS = {}          # path -> open pymupdf document (for page images)
PAGE_CACHE = CACHE / "pages"
PAGE_CACHE.mkdir(parents=True, exist_ok=True)


def crop_png(doc, n, r, width):
    """A figure from a page, rendered crisply for the text view (cached)."""
    import hashlib as _h
    width = max(300, min(2400, int(width)))
    key = _h.sha1(f"{doc['id']}|{n}|{r}|{width}".encode()).hexdigest()[:20]
    f = PAGE_CACHE / f"crop_{key}.png"
    if f.exists():
        return f.read_bytes()
    import pymupdf
    with PDF_LOCK:
        src = doc.get("pdf") or doc["path"]
        pdf = PDFS.get(src)
        if pdf is None:
            pdf = PDFS[src] = pymupdf.open(src)
        pad = CROP_PADDING
        rect = pymupdf.Rect(r) + (-pad, -pad, pad, pad)
        z = min(4.0, width / max(1.0, rect.width))
        png = pdf[n].get_pixmap(matrix=pymupdf.Matrix(z, z), clip=rect, alpha=False).tobytes("png")
    PAGE_CACHE.mkdir(parents=True, exist_ok=True)
    f.write_bytes(png)
    return png


def cover_png(did):
    """First page of a document, small, for the library (cached)."""
    info = STATE.data["docs"].get(did)
    if not info or not Path(info.get("path", "")).exists():
        return None
    f = CACHE / "covers" / f"{did}.png"
    if f.exists():
        return f.read_bytes()
    try:
        import pymupdf
        from .convert import to_pdf
        src = info["path"]
        with PDF_LOCK:
            pdf, _ = to_pdf(src, CACHE / "converted") if not src.lower().endswith(".pdf") else (src, None)
            page = pymupdf.open(pdf)[0]
            z = 300 / page.rect.width
            png = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False,
                                  clip=pymupdf.Rect(0, 0, page.rect.width, min(page.rect.height, page.rect.width * 1.32))).tobytes("png")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(png)
        return png
    except Exception:
        traceback.print_exc()
        return None


def page_png(doc, n, width):
    """Render page n (0-based) of the document about `width` pixels wide."""
    width = max(400, min(3000, int(width)))
    f = PAGE_CACHE / f"{doc['id']}_{n}_{width}.png"
    if f.exists():
        return f.read_bytes()
    import pymupdf
    with PDF_LOCK:
        src = doc.get("pdf") or doc["path"]
        pdf = PDFS.get(src)
        if pdf is None:
            pdf = PDFS[src] = pymupdf.open(src)
        page = pdf[n]
        z = width / page.rect.width
        png = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False).tobytes("png")
    f.write_bytes(png)
    return png


def variant():
    """Reading options that change how a document is spoken (part of its cache key)."""
    st = STATE.data["settings"]
    lex = json.dumps(st.get("lexicon") or {}, sort_keys=True)
    return f"{int(st['skip_citations'])}-{_hl.sha1(lex.encode()).hexdigest()[:8]}"


def get_doc(path=None, did=None):
    if did and did in DOCS and DOCS[did].get("_variant") == variant():
        return DOCS[did]
    if did and not path:
        info = STATE.data["docs"].get(did)
        if not info:
            raise KeyError(did)
        path = info["path"]
    path = str(Path(path).expanduser())
    with DOC_LOCK:
        did2 = doc_id(path)
        skip = STATE.data["settings"]["skip_citations"]
        var = variant()
        if did2 in DOCS and DOCS[did2].get("_variant") == var:
            return DOCS[did2]
        def progress(done, total):
            STATUS["msg"] = f"Recognising text in scanned pages… {done} of {total}"
        STATUS["msg"] = "Opening…"
        cached = DOC_CACHE / f"{did2}-{var}-{EXTRACT_VERSION}.json"
        try:
            doc = json.loads(cached.read_text()) if cached.exists() else None
        except (OSError, ValueError):
            doc = None
        if doc is None:
            try:
                doc = load_document(path, skip_citations=skip, cache_dir=CACHE / "converted", progress=progress,
                                    lexicon=STATE.data["settings"].get("lexicon") or {})
            finally:
                STATUS["msg"] = ""
            try:
                cached.write_text(json.dumps(doc))
            except OSError:
                pass
        doc["_variant"] = var
        DOCS[doc["id"]] = doc
        if any(p.get("eq") and not p["eq"].get("done") for p in doc["paras"]):
            threading.Thread(target=equation_worker, args=(doc,), daemon=True).start()
        if did and did != doc["id"]:
            DOCS[did] = doc
    import time
    info = STATE.data["docs"].setdefault(doc["id"], {})
    info.update(path=path, title=doc["title"], opened=time.time(), units=len(doc["units"]),
                chars=sum(u["e"] - u["s"] for u in doc["units"]), pages=len(doc.get("pages") or []))
    STATE.save()
    return doc


def save_source(article_file, html, url):
    """Keep the web page itself next to its article, to show it as it is."""
    base = Path(article_file)
    base.with_name(base.stem + ".source.html").write_text(html, encoding="utf-8")
    base.with_name(base.stem + ".source.json").write_text(json.dumps({"url": url}), encoding="utf-8")


def web_info(doc):
    p = Path(doc.get("path", ""))
    meta = p.with_name(p.stem + ".source.json")
    if p.suffix.lower() == ".html" and meta.exists() and p.with_name(p.stem + ".source.html").exists():
        try:
            return json.loads(meta.read_text())
        except ValueError:
            return None
    return None


def save_text(text):
    """Store text to read (pasted or sent from another app) and return its file."""
    import re as _re
    import time as _time
    first = next((l.strip() for l in text.splitlines() if l.strip()), "Selected text")
    title = first if len(first) <= 80 else first[:60].rsplit(" ", 1)[0] + "…"
    folder = SUPPORT / "pasted"
    folder.mkdir(exist_ok=True)
    safe = _re.sub(r"[^\w\- ]+", "", title)[:40].strip() or "Selected text"
    f = folder / f"{_time.strftime('%Y-%m-%d %H.%M.%S')} {safe}.txt"
    f.write_text(text, encoding="utf-8")
    return f


def bring_to_front(on=True):
    if not on:
        return
    try:
        from AppKit import NSApplication
        from PyObjCTools import AppHelper
        AppHelper.callAfter(lambda: NSApplication.sharedApplication().activateIgnoringOtherApps_(True))
    except Exception:
        pass


NOTES_DIR = NOTES


def notes_file(info):
    import re as _re
    safe = _re.sub(r"\s*:\s*", " - ", info.get("title") or "Untitled")
    safe = " ".join(_re.sub(r'[\\/*?"<>|\n\r\t]+', " ", safe).split()).strip(" .")[:80] or "Untitled"
    return NOTES_DIR / f"{safe}.md"


def write_notes(did):
    """Your marks and notes for one document as Markdown, each linking back to its spot."""
    import time as _time
    info = STATE.data["docs"].get(did) or {}
    notes = info.get("notes") or []
    f = notes_file(info)
    if not notes:
        if f.exists():
            f.unlink()
        return None
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    out = [f"# {info.get('title') or 'Untitled'}", "",
           f"*Notes from ALOUD · source: `{info.get('path', '')}`*", ""]
    section = None
    for n in sorted(notes, key=lambda n: (n.get("unit", 0), n.get("created", 0))):
        if n.get("section") != section:
            section = n.get("section")
            out += ["", f"## {section or 'Beginning'}", ""]
        link = f"http://127.0.0.1:{SEND_PORT}/open?doc={did}&unit={n.get('unit', 0)}"
        where = f"page {n['page']}" if n.get("page") else "jump there"
        quote = " ".join((n.get("quote") or "").split())
        if n.get("kind") == "mark":
            out.append(f"- ⭐ “{quote}” — [{where}]({link})")
        else:
            out.append(f"- 📝 **{n.get('text', '').strip()}**")
            if quote:
                out.append(f"  > “{quote}” — [{where}]({link})")
            else:
                out.append(f"  — [{where}]({link})")
    out += ["", f"*Updated {_time.strftime('%Y-%m-%d %H:%M')}*", ""]
    f.write_text("\n".join(out), encoding="utf-8")
    return str(f)


_POWER = {"t": 0, "ok": True}


def ensure_equation(doc, pi, urgent=True):
    """An equation paragraph gets its words the first time they're needed."""
    p = doc["paras"][pi]
    if p.get("eq") and not p["eq"].get("done"):
        from . import pdfmath
        pdfmath.PDF_LOCK = PDF_LOCK
        pdfmath.resolve(doc["pdf"], p, doc.get("lang", "en"), urgent=urgent)
        EQ_DIRTY.add(doc["id"])


EQ_DIRTY = set()


def equation_worker(doc):
    """Read a document's equations in the background, in reading order, then save them."""
    import time
    from . import pdfmath
    while True:
        pending = [i for i, p in enumerate(doc["paras"]) if p.get("eq") and not p["eq"].get("done")]
        if not pending or DOCS.get(doc["id"]) is not doc:
            break                                         # all read, or the document was closed
        while pdfmath.busy():
            time.sleep(0.05)                              # playback needs an equation right now
        here = doc.get("_cursor", 0)                      # read ahead of the listener first
        ahead = [i for i in pending if i >= here]
        ensure_equation(doc, (ahead or pending)[0], urgent=False)
        time.sleep(0.02)
    if doc["id"] in EQ_DIRTY:
        save_doc_cache(doc)


def save_doc_cache(doc):
    try:
        path = DOC_CACHE / f"{doc['id']}-{doc['_variant']}-{EXTRACT_VERSION}.json"
        path.write_text(json.dumps({k: v for k, v in doc.items() if not k.startswith("_")}))
        EQ_DIRTY.discard(doc["id"])
    except (OSError, KeyError):
        pass


def para_job(doc, u, voice, prio):
    unit = doc["units"][u]
    if prio <= 5:
        doc["_cursor"] = unit["p"]
    ensure_equation(doc, unit["p"])
    kind = doc["paras"][unit["p"]]["kind"]
    nxt = doc["paras"][doc["units"][u + 1]["p"]]["kind"] if u + 1 < len(doc["units"]) else "p"
    # natural pauses: between sentences < between paragraphs < around headings
    pause = 0.75 if kind == "h" else (0.85 if unit["last"] and nxt == "h" else (0.6 if unit["last"] else 0.38))
    text, spans = unit_text(doc, u)
    return ENGINE.request(text, spans, voice, prio, pause)


# ---------------------------------------------------------------- audiobook export
EXPORT = {"id": None, "done": 0, "total": 0, "folder": None, "error": None, "running": False, "cancel": False}


def _safe_name(s, n=60):
    import re as _re
    s = _re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", s).strip(" .")
    return (s[:n].rsplit(" ", 1)[0] if len(s) > n else s) or "Untitled"


def export_audiobook(doc, voice, clean):
    """One AAC file per section in ~/Downloads/<title> (audio)/, synthesised at low priority."""
    import subprocess
    import tempfile
    import numpy as np
    import soundfile as sf
    from .tts import SR, finish
    folder = Path.home() / "Downloads" / f"{_safe_name(doc['title'])} (audio)"
    folder.mkdir(parents=True, exist_ok=True)
    EXPORT.update(id=doc["id"], done=0, total=len(doc["units"]), folder=str(folder), error=None, running=True, cancel=False)
    # group units by section
    starts = [s["para"] for s in doc["sections"]]
    groups, names = [], []
    for u, unit in enumerate(doc["units"]):
        k = sum(1 for sp in starts if sp <= unit["p"])
        name = doc["sections"][k - 1]["title"] if k else doc["title"]
        if not groups or names[-1] != (k, name):
            groups.append([]); names.append((k, name))
        groups[-1].append(u)
    try:
        for gi, (units, (_, name)) in enumerate(zip(groups, names)):
            pcm = []
            for u in units:
                while True:
                    if EXPORT["cancel"]:
                        raise InterruptedError
                    key = para_job(doc, u, voice, 50)
                    try:
                        meta = ENGINE.wait(key, timeout=600)
                        break
                    except RuntimeError:      # cancelled by a jump in live reading: ask again
                        continue
                data, _ = sf.read(ENGINE.cache / f"{key}.wav", dtype="float32")
                pcm.append(finish(data, clean))
                EXPORT["done"] += 1
            if not pcm:
                continue
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                sf.write(tmp.name, np.concatenate(pcm), SR, subtype="PCM_16")
            out = folder / f"{gi + 1:02d} {_safe_name(name)}.m4a"
            subprocess.run(["/usr/bin/afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", tmp.name, str(out)],
                           check=True, capture_output=True)
            os.unlink(tmp.name)
    except InterruptedError:
        EXPORT["error"] = "cancelled"
    except Exception as e:
        traceback.print_exc()
        EXPORT["error"] = str(e)
    finally:
        EXPORT["running"] = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_address[1]}":
            return self._send(403, {"error": "invalid host"})
        try:
            self._get()
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": str(e)})

    def do_POST(self):
        host = f"127.0.0.1:{self.server.server_address[1]}"
        origin = self.headers.get("Origin")
        if (self.headers.get("Host") != host or origin not in (None, f"http://{host}") or
                self.headers.get_content_type() != "application/json"):
            return self._send(403, {"error": "request must come from ALOUD"})
        try:
            self._post()
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": str(e)})

    def _get(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            return self._send(200, (HERE / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path.startswith("/vendor/"):
            f = (HERE / "web" / u.path.lstrip("/")).resolve()
            if not f.is_relative_to(HERE / "web" / "vendor") or not f.is_file():
                return self._send(404, {"error": "not found"})
            ctype = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff",
                     ".ttf": "font/ttf"}.get(f.suffix, "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype)
        if u.path == "/api/cite":
            doc = DOCS.get(q.get("doc", ""))
            if not doc:
                return self._send(404, {"error": "no document"})
            if "_refs" not in doc:
                from . import refs as _refs
                with PDF_LOCK:
                    doc["_refs"] = _refs.parse(doc.get("pdf") or doc["path"])
            from . import refs as _refs
            found = _refs.resolve(doc["_refs"], q.get("q", ""))
            out = []
            for e in found[:6]:
                before = e["text"][:max(0, e["text"].find(e["title"][:20]))] if e.get("title") else e["text"][:80]
                many = bool(re.search(r"\band\b|&|et al|,.*,", before))
                out.append(dict(n=e.get("n") or e.get("label"), title=e["title"], author=e["author"] + (" et al." if many else ""),
                                year=e.get("year"), link=e["link"], scholar=e["scholar"],
                                alphaxiv=f"https://www.alphaxiv.org/abs/{e['arxiv']}" if e.get("arxiv") else None))
            return self._send(200, {"refs": out, "have_list": bool(doc["_refs"])})
        if u.path == "/api/notes":
            info = STATE.data["docs"].get(q.get("doc", "")) or {}
            f = notes_file(info) if info else None
            return self._send(200, {"notes": info.get("notes") or [], "file": str(f) if f and f.exists() else None})
        if u.path == "/api/eqs":
            doc = DOCS.get(q.get("doc", ""))
            out = {i: p["eq"]["latex"] for i, p in enumerate(doc["paras"]) if p.get("eq") and p["eq"].get("latex")} if doc else {}
            return self._send(200, out)
        if u.path == "/help.html":
            return self._send(200, (HERE / "web" / "help.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/delight.js":
            return self._send(200, (HERE / "web" / "delight.js").read_bytes(), "text/javascript; charset=utf-8")
        if u.path == "/app.js":
            return self._send(200, (HERE / "web" / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        if u.path == "/api/state":
            for k, v in STATE.data["docs"].items():          # older entries: length from the cached analysis
                if "chars" not in v:
                    hit = next(iter(DOC_CACHE.glob(f"{k}-*.json")), None)
                    try:
                        d = json.loads(hit.read_text()) if hit else None
                    except (OSError, ValueError):
                        d = None
                    v["chars"] = sum(x["e"] - x["s"] for x in d["units"]) if d else 0
                    v.setdefault("pages", len(d.get("pages") or []) if d else 0)
            docs = sorted(({"id": k, **v} for k, v in STATE.data["docs"].items()
                           if Path(v.get("path", "")).exists()),
                          key=lambda d: -d.get("opened", 0))
            return self._send(200, dict(settings=STATE.data["settings"], docs=docs,
                                        voices=VOICES, ready=ENGINE.ready.is_set(), default_voice=DEFAULT_VOICE,
                                        last=STATE.data.get("last"),
                                        queue=[d for d in STATE.data.get("queue", []) if d in STATE.data["docs"]],
                                        folders=STATE.data.get("folders", {})))
        if u.path == "/api/bookmarklet":
            return self._send(200, {"port": SEND_PORT, "key": bridge_key()})
        if u.path == "/api/crop":
            doc = get_doc(did=q["doc"])
            png = crop_png(doc, int(q["n"]), [float(v) for v in q["r"].split(",")], int(q.get("w", 1400)))
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(png)
            return
        if u.path == "/api/cover":
            png = cover_png(q.get("id", ""))
            if not png:
                return self._send(404, {"error": "no cover"})
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(png)
            return
        if u.path == "/api/status":
            return self._send(200, dict(STATUS, export=EXPORT))
        if u.path == "/api/webpage":
            doc = get_doc(did=q["doc"])
            info = web_info(doc)
            if not info:
                return self._send(404, {"error": "no page"})
            from .webpage import clean_snapshot
            p = Path(doc["path"])
            html = clean_snapshot(p.with_name(p.stem + ".source.html").read_text(encoding="utf-8", errors="replace"), info["url"],
                                  f"http://127.0.0.1:{self.server.server_address[1]}")
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/doc":
            doc = get_doc(path=q.get("path"), did=q.get("id"))
            STATE.data["last"] = doc["id"]
            STATE.save()
            pos = STATE.data["docs"].get(doc["id"], {}).get("pos", {})
            body = {k: v for k, v in doc.items() if not k.startswith("_")}
            return self._send(200, {**body, "pos": pos, "web": web_info(doc)})
        if u.path == "/api/para":
            doc = get_doc(did=q["doc"])
            i = int(q["i"])
            voice = q.get("voice") or STATE.data["settings"]["voice"]
            prefetch = q.get("prefetch")
            key = para_job(doc, i, voice, int(prefetch) if prefetch else int(q.get("prio", 0)))
            if prefetch:
                return self._send(200, {"key": key, "queued": True})
            meta = ENGINE.wait(key)
            if not meta:
                return self._send(503, {"error": "synthesis timed out"})
            off = doc["units"][i]["s"]
            text, spans = unit_text(doc, i)
            meta = dict(meta, words=[[a + off, b + off, t0, t1] for a, b, t0, t1 in raw_words(meta["words"], text, spans)])
            return self._send(200, meta)
        if u.path == "/api/figure-caption":
            doc = get_doc(did=q["doc"])
            i = int(q["i"])
            figure = doc["paras"][i].get("figure") if 0 <= i < len(doc["paras"]) else None
            if not figure or not figure["caption"]:
                return self._send(404, {"error": "No caption found"})
            st = STATE.data["settings"]
            text = f"Figure {figure['number']}. {figure['caption']}"
            spans = speech_spans(text, st["skip_citations"], doc["lang"], st["lexicon"])
            key = ENGINE.request(text, spans, q.get("voice") or st["voice"], 0, 0.18)
            meta = ENGINE.wait(key)
            if not meta:
                return self._send(503, {"error": "Caption synthesis timed out"})
            return self._send(200, meta)
        if u.path == "/api/page":
            doc = get_doc(did=q["doc"])
            n = int(q["n"])
            if not 0 <= n < len(doc.get("pages", [])):
                return self._send(404, {"error": "no such page"})
            body = page_png(doc, n, int(q.get("w", 1400)))
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/sample":
            voice = q.get("voice", "af_heart")
            from .tts import LANG_OF_VOICE
            key = ENGINE.request(SAMPLES[LANG_OF_VOICE.get(voice[0], "en")], [], voice, -1, 0.1)
            meta = ENGINE.wait(key)
            return self._send(200, meta)
        if u.path.startswith("/audio/"):
            key = Path(u.path).stem
            if not key.isalnum():
                return self._send(404, {"error": "bad key"})
            clean = q.get("clean", "1") == "1"
            return self._send(200, ENGINE.audio(key, clean), "audio/wav")
        self._send(404, {"error": "not found"})

    def _post(self):
        u = urlparse(self.path)
        body = self._json()
        if u.path == "/api/settings":
            old = variant()
            STATE.data["settings"].update({k: v for k, v in body.items() if k in DEFAULT_SETTINGS})
            STATE.save()
            return self._send(200, {"ok": True, "reload": old != variant()})
        if u.path == "/api/progress":
            info = STATE.data["docs"].get(body["id"])
            if info is not None:
                info["pos"] = dict(para=body.get("para", 0), unit=body.get("unit", 0), t=body.get("t", 0))
                STATE.save()
            return self._send(200, {"ok": True})
        if u.path == "/api/folder":
            import time as _time
            folders = STATE.data.setdefault("folders", {})
            op = body.get("op")
            fid = body.get("id")

            def parent_ok(child, parent):           # a folder can't go inside itself or its own subfolder
                while parent:
                    if parent == child:
                        return False
                    parent = folders.get(parent, {}).get("parent")
                return True
            if op == "create":
                name = (body.get("name") or "").strip()[:80] or "New folder"
                parent = body.get("parent") if body.get("parent") in folders else None
                fid = f"f{int(_time.time() * 1000)}"
                folders[fid] = {"name": name, "parent": parent, "created": _time.time()}
            elif op == "style" and fid in folders:             # Finder-style colour and an emoji
                if "color" in body:
                    folders[fid]["color"] = body.get("color") or None
                if "emoji" in body:
                    folders[fid]["emoji"] = (body.get("emoji") or "").strip()[:4] or None
            elif op == "rename" and fid in folders:
                folders[fid]["name"] = (body.get("name") or "").strip()[:80] or folders[fid]["name"]
            elif op == "delete" and fid in folders:
                up = folders[fid].get("parent")
                for f in folders.values():          # contents move up one level; nothing is lost
                    if f.get("parent") == fid:
                        f["parent"] = up
                for d in STATE.data["docs"].values():
                    if d.get("folder") == fid:
                        d["folder"] = up
                del folders[fid]
            elif op == "move":
                to = body.get("to") if body.get("to") in folders else None
                for did in body.get("docs") or []:
                    if did in STATE.data["docs"]:
                        STATE.data["docs"][did]["folder"] = to
                for f in body.get("folders") or []:
                    if f in folders and f != to and parent_ok(f, to):
                        folders[f]["parent"] = to
            STATE.save()
            return self._send(200, {"folders": folders, "id": fid})
        if u.path == "/api/queue":
            qd = [d for d in STATE.data.get("queue", []) if d in STATE.data["docs"]]
            op, did = body.get("op"), body.get("id")
            if op == "add" and did and did not in qd:
                qd.append(did)
            elif op == "remove":
                qd = [d for d in qd if d != did]
            elif op == "move" and did in qd:
                qd.remove(did)
                qd.insert(max(0, min(len(qd), int(body.get("to", 0)))), did)
            elif op == "clear":
                qd = []
            STATE.data["queue"] = qd
            STATE.save()
            return self._send(200, {"queue": qd})
        if u.path == "/api/note":
            import time as _time
            info = STATE.data["docs"].get(body.get("doc"))
            if not info:
                return self._send(404, {"error": "no document"})
            notes = info.setdefault("notes", [])
            if body.get("delete"):
                info["notes"] = notes = [n for n in notes if n.get("id") != body["delete"]]
            else:
                n = {k: body.get(k) for k in ("unit", "para", "s", "e", "quote", "text", "kind", "section", "page")}
                n.update(id=f"n{int(_time.time() * 1000)}", created=_time.time())
                notes.append(n)
            STATE.save()
            path = write_notes(body["doc"])
            return self._send(200, {"notes": notes, "file": path})
        if u.path == "/api/forget":
            STATE.data["docs"].pop(body.get("id"), None)
            STATE.save()
            return self._send(200, {"ok": True})
        if u.path == "/api/paste":
            import re as _re
            import time
            from .convert import text_to_html
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, {"error": "Nothing to read"})
            from .webpage import PreviewOnly, fetch_article, is_url
            if is_url(text):
                STATUS["msg"] = "Fetching the page…"
                try:
                    title, page = fetch_article(text, render=render_page)
                except PreviewOnly as e:
                    return self._send(422, {"error": str(e), "signin": e.signin})
                except Exception as e:
                    return self._send(422, {"error": f"Couldn't load that page ({type(e).__name__})."})
                finally:
                    STATUS["msg"] = ""
                folder = SUPPORT / "web"
                folder.mkdir(exist_ok=True)
                safe = _re.sub(r"[^\w\- ]+", "", title or "Web page")[:60].strip() or "Web page"
                f = folder / f"{time.strftime('%Y-%m-%d')} {safe}{'.pdf' if title is None else '.html'}"
                if title is None:
                    f.write_bytes(page)
                else:
                    from .webpage import localize_images
                    STATUS["msg"] = "Fetching the pictures…"
                    try:
                        page = localize_images(page, f.parent / f"{f.stem}_files", text.strip())
                    finally:
                        STATUS["msg"] = ""
                    f.write_text(page, encoding="utf-8")
                    src, final = getattr(fetch_article, "source", (None, None))
                    if src:
                        save_source(f, src, final or text.strip())
                return self._send(200, {"path": str(f), "title": title})
            first = next((l.strip() for l in text.splitlines() if l.strip()), "Pasted text")
            title = first if len(first) <= 80 else first[:60].rsplit(" ", 1)[0] + "…"
            folder = SUPPORT / "pasted"
            folder.mkdir(exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H.%M.%S")
            safe = _re.sub(r"[^\w\- ]+", "", title)[:40].strip() or "Pasted text"
            f = folder / f"{stamp} {safe}.txt"
            f.write_text(text, encoding="utf-8")
            return self._send(200, {"path": str(f), "title": title})
        if u.path == "/api/export":
            if body.get("cancel"):
                EXPORT["cancel"] = True
                return self._send(200, {"ok": True})
            if EXPORT["running"]:
                return self._send(409, {"error": "An export is already running"})
            doc = get_doc(did=body["id"])
            threading.Thread(target=export_audiobook, args=(doc, body.get("voice") or STATE.data["settings"]["voice"],
                             STATE.data["settings"]["clean"]), daemon=True).start()
            return self._send(200, {"ok": True})
        if u.path == "/api/drop_prefetch":
            ENGINE.drop_prefetch()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


class JsApi:
    """Exposed to the page as window.pywebview.api."""
    window = None

    def choose_file(self):
        import webview
        res = self.window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=False,
            file_types=("Documents (*.pdf;*.epub;*.mobi;*.fb2;*.docx;*.doc;*.rtf;*.odt;*.html;*.htm;*.md;*.markdown;*.txt;*.xps)", "All files (*.*)"))
        return res[0] if res else None

    def now_playing(self, title, album, playing):
        """Tell macOS what's playing (Control Centre, media keys, AirPods)."""
        set_driver(self.window, bool(playing))
        update_menubar(title or "ALOUD", bool(playing))
        try:
            from MediaPlayer import (MPMediaItemPropertyAlbumTitle, MPMediaItemPropertyArtist,
                                     MPMediaItemPropertyTitle, MPNowPlayingInfoCenter)
            c = MPNowPlayingInfoCenter.defaultCenter()
            c.setNowPlayingInfo_({MPMediaItemPropertyTitle: title or "ALOUD",
                                  MPMediaItemPropertyArtist: album or "",
                                  MPMediaItemPropertyAlbumTitle: "ALOUD"})
            c.setPlaybackState_(1 if playing else 2)      # playing / paused
        except Exception:
            pass

    def compact(self, on):
        """A slim floating player strip that stays on top while you work (or back to normal)."""
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_compact_window, bool(on))
        except Exception:
            traceback.print_exc()
        return True

    def dock_progress(self, frac, playing=False):
        """A small progress ring on the Dock icon while reading (None clears it)."""
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_draw_dock, frac, bool(playing))
        except Exception:
            pass
        return True

    def open_url(self, url):
        """Open a link in the user's normal browser."""
        if not str(url).startswith(("https://", "http://")):
            return False
        import subprocess
        subprocess.Popen(["/usr/bin/open", url])
        return True

    def open_signin(self, url):
        """A normal browser window inside ALOUD, to sign in to a site you subscribe to.
        It shares ALOUD's cookies, so afterwards its articles load in full."""
        import webview
        from urllib.parse import urlparse
        webview.create_window(f"Sign in — {urlparse(url).netloc.removeprefix('www.')}", url,
                              width=1100, height=850)
        return True

    def open_file(self, path):
        """Open a file (your notes) in its usual app."""
        import subprocess
        if str(path).startswith(str(NOTES_DIR)):
            subprocess.Popen(["/usr/bin/open", path])
        return True

    def reveal(self, path):
        import subprocess
        subprocess.run(["open", "-R", path])


_ACTIVITY = None
_DRIVER = {"on": False, "thread": None}


def set_driver(win, on):
    """While audio plays, nudge the page's highlight from here. WebKit throttles a hidden
    window's own timers (seconds apart); this process isn't throttled, so the highlight and
    the gapless hand-off between passages keep their timing when the window is covered."""
    _DRIVER["on"] = on
    if on and not (_DRIVER["thread"] and _DRIVER["thread"].is_alive()):
        def loop():
            while _DRIVER["on"]:
                try:
                    win.evaluate_js("document.hidden && typeof step === 'function' && !audio.paused && step()")
                except Exception:
                    pass
                time.sleep(0.05)
        _DRIVER["thread"] = threading.Thread(target=loop, daemon=True, name="highlight-driver")
        _DRIVER["thread"].start()
_REMOTE = []


def register_media_keys(win):
    """Play/pause and next/previous from the keyboard, AirPods and Control Centre."""
    try:
        from MediaPlayer import MPRemoteCommandCenter
    except ImportError:
        return
    if _REMOTE:
        return                                           # page reloads must not register twice
    cc = MPRemoteCommandCenter.sharedCommandCenter()

    def bind(cmd, js):
        def handler(_event):
            threading.Thread(target=lambda: win.evaluate_js(js), daemon=True).start()
            return 0                                     # MPRemoteCommandHandlerStatusSuccess
        cmd.setEnabled_(True)
        _REMOTE.append((cmd, cmd.addTargetWithHandler_(handler), handler))
    bind(cc.togglePlayPauseCommand(), "toggle()")
    bind(cc.playCommand(), "isBusy() || toggle()")
    bind(cc.pauseCommand(), "pause()")
    bind(cc.nextTrackCommand(), "sentence(1)")
    bind(cc.previousTrackCommand(), "sentence(-1)")


def keep_awake_while_reading(win):
    """Stop App Nap and WebKit's background throttling, which stall the highlight."""
    global _ACTIVITY
    try:
        from Foundation import NSProcessInfo
        # user-initiated (no App Nap) but still lets the Mac sleep when idle; latency-critical timers
        _ACTIVITY = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            0x00EFFFFF | 0xFF00000000, "Reading aloud with the text highlighted")
    except Exception:
        traceback.print_exc()
    try:
        from PyObjCTools import AppHelper

        def tweak():
            view = win.native.contentView()
            stack = [view]
            while stack:
                v = stack.pop()
                if v.respondsToSelector_("configuration"):
                    prefs = v.configuration().preferences()
                    if prefs.respondsToSelector_("setInactiveSchedulingPolicy:"):
                        prefs.setInactiveSchedulingPolicy_(2)   # WKInactiveSchedulingPolicyNone
                    return
                stack += list(v.subviews())
        AppHelper.callAfter(tweak)
    except Exception:
        traceback.print_exc()


def set_mac_identity():
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "ALOUD"
        app = NSApplication.sharedApplication()
        icon = HERE / "assets" / "icon.png"
        if icon.exists():
            app.setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icon)))
    except Exception:
        traceback.print_exc()


SEND_PORT = 47812          # fixed, so the "Send to ALOUD" browser button can find us
WINDOW = {"win": None}

def bridge_key():
    """Pair local integrations with this installation."""
    import tempfile
    path = SUPPORT / "bridge-key"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        with tempfile.NamedTemporaryFile(mode="w", dir=SUPPORT) as draft:
            draft.write(secrets.token_urlsafe(32))
            draft.flush()
            try:
                os.link(draft.name, path)
            except FileExistsError:
                pass
        return path.read_text().strip()


class SendHandler(BaseHTTPRequestHandler):
    """Receives pages from the browser button: the page as you see it, sign-ins and all."""

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_GET(self):
        """Links in your notes file: open ALOUD at that spot."""
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        doc = q.get("doc", "")
        if urlparse(self.path).path != "/open" or doc not in STATE.data["docs"]:
            return self._reply(404, {"error": "not found"})
        if self.headers.get("Sec-Fetch-Mode") not in (None, "navigate"):
            return self._reply(403, {"error": "open note links in a browser tab"})
        try:
            unit = int(q.get("unit", 0))
        except ValueError:
            return self._reply(400, {"error": "invalid reading position"})
        win = WINDOW["win"]
        if win is not None:
            win.evaluate_js(f"window.aloudJump({json.dumps(doc)}, {unit})")
            try:
                from AppKit import NSApplication
                from PyObjCTools import AppHelper
                AppHelper.callAfter(lambda: NSApplication.sharedApplication().activateIgnoringOtherApps_(True))
            except Exception:
                pass
        body = ("<!doctype html><meta charset=utf-8><title>ALOUD</title><body style='font:15px -apple-system;"
                "display:grid;place-items:center;height:90vh;color:#555'>Opened in ALOUD. You can close this tab."
                "<script>setTimeout(()=>window.close(),400)</script>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        import re as _re
        import time as _time
        route = urlparse(self.path).path
        if self.headers.get("Host") != f"127.0.0.1:{SEND_PORT}" or route not in ("/read", "/send", "/api/send"):
            return self._reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 30_000_000:
                return self._reply(413, {"error": "invalid request size"})
            req = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            return self._reply(400, {"error": "invalid request"})
        key = req.get("key") if isinstance(req, dict) else None
        if not isinstance(key, str) or not secrets.compare_digest(key.encode(), bridge_key().encode()):
            return self._reply(403, {"error": "Set up Send to ALOUD again in ALOUD settings"})
        if route == "/read":
            text = req.get("text", "")
            text = text.strip() if isinstance(text, str) else ""
            if not text:
                return self._reply(400, {"error": "nothing selected"})
            f = save_text(text)
            win = WINDOW["win"]
            if win is not None:
                win.evaluate_js(f"window.aloudRead({json.dumps(str(f))})")
                bring_to_front(req.get("front", True))
            return self._reply(200, {"ok": True})
        try:
            from .webpage import PreviewOnly, article_from_html
            try:
                title, page = article_from_html(req.get("html") or "", req.get("url") or "")
            except PreviewOnly as e:
                return self._reply(422, {"error": str(e)})
            folder = SUPPORT / "web"
            folder.mkdir(exist_ok=True)
            safe = _re.sub(r"[^\w\- ]+", "", title or "Web page")[:60].strip() or "Web page"
            f = folder / f"{_time.strftime('%Y-%m-%d')} {safe}.html"
            from .webpage import localize_images
            page = localize_images(page, f.parent / f"{f.stem}_files", req.get("url") or "")
            f.write_text(page, encoding="utf-8")
            save_source(f, req.get("html") or "", req.get("url") or "")
            win = WINDOW["win"]
            if win is not None:
                win.evaluate_js(f"window.aloudOpen({json.dumps(str(f))})")
                try:
                    from AppKit import NSApplication
                    from PyObjCTools import AppHelper
                    AppHelper.callAfter(lambda: NSApplication.sharedApplication().activateIgnoringOtherApps_(True))
                except Exception:
                    pass
            return self._reply(200, {"ok": True, "title": title})
        except Exception as e:
            traceback.print_exc()
            return self._reply(500, {"error": str(e)})


MENU = {"item": None, "target": None, "title": "", "playing": False}


def _js(code):
    """Run page JavaScript from any thread without blocking the UI thread."""
    win = WINDOW["win"]
    if win is not None:
        threading.Thread(target=lambda: win.evaluate_js(code), daemon=True).start()


def setup_menubar():
    """A small player in the menu bar: what's playing, play/pause, skip, read the clipboard."""
    try:
        import objc
        from AppKit import NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength, NSImage, NSPasteboard
        from Foundation import NSObject
    except Exception:
        return

    class MenuTarget(NSObject):
        def playPause_(self, sender):
            _js("toggle()")

        def nextSentence_(self, sender):
            _js("sentence(1)")

        def prevSentence_(self, sender):
            _js("sentence(-1)")

        def readClipboard_(self, sender):
            t = NSPasteboard.generalPasteboard().stringForType_("public.utf8-plain-text")
            if t and t.strip():
                f = save_text(t)
                _js(f"window.aloudRead({json.dumps(str(f))})")

        def showWindow_(self, sender):
            bring_to_front(True)

        def menuNeedsUpdate_(self, menu):
            update_menubar()

    target = MenuTarget.alloc().init()
    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_("book.pages", "ALOUD") or \
        NSImage.imageWithSystemSymbolName_accessibilityDescription_("book", "ALOUD")
    if img is not None:
        img.setTemplate_(True)
        item.button().setImage_(img)
    else:
        item.button().setTitle_("ALOUD")
    menu = NSMenu.alloc().init()
    menu.setAutoenablesItems_(False)
    menu.setDelegate_(target)

    def add(title, sel, key=""):
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key)
        mi.setTarget_(target)
        menu.addItem_(mi)
        return mi
    now = add("Nothing playing", None)
    now.setEnabled_(False)
    menu.addItem_(NSMenuItem.separatorItem())
    play = add("Play", "playPause:")
    add("Previous sentence", "prevSentence:")
    add("Next sentence", "nextSentence:")
    menu.addItem_(NSMenuItem.separatorItem())
    add("Read clipboard", "readClipboard:")
    add("Show ALOUD", "showWindow:")
    item.setMenu_(menu)
    MENU.update(item=item, target=target, now=now, play=play)


def update_menubar(title=None, playing=None):
    if title is not None:
        MENU["title"] = title
    if playing is not None:
        MENU["playing"] = playing
    if not MENU.get("item"):
        return

    def apply():
        t = MENU["title"] or "Nothing playing"
        MENU["now"].setTitle_(("▶︎  " if MENU["playing"] else "") + (t if len(t) < 60 else t[:57] + "…"))
        MENU["play"].setTitle_("Pause" if MENU["playing"] else "Play")
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(apply)
    except Exception:
        pass


_DOCK = {"base": None, "last": None}


_COMPACT = {}


def _main_nswindow():
    from AppKit import NSApplication
    try:                                   # pywebview's own handle on the main window
        nw = getattr(WINDOW.get("win"), "native", None)    # the NSWindow itself
        if nw is not None and hasattr(nw, "setContentMinSize_"):
            return nw
    except Exception:
        pass
    app = NSApplication.sharedApplication()
    wins = [w for w in app.windows() if w.isVisible() and not str(w.title() or "").startswith(("ALOUD page", "Sign in"))]
    return max(wins, key=lambda w: w.frame().size.width * w.frame().size.height) if wins else app.mainWindow()


def _compact_window(on):
    """Shrink the main window to a floating strip in the screen's lower-right corner, or restore it."""
    from AppKit import NSMakeRect, NSMakeSize
    w = _main_nswindow()
    if w is None:
        return
    if on:
        if not _COMPACT:
            _COMPACT.update(frame=w.frame(), minsize=w.contentMinSize(), level=w.level(), behavior=w.collectionBehavior())
        w.setContentMinSize_(NSMakeSize(420, 84))
        w.setLevel_(3)                                            # floating: stays above other apps' windows
        w.setCollectionBehavior_(w.collectionBehavior() | 1 | 256)  # follow to every Space, and over full-screen apps
        vf = (w.screen() or w).visibleFrame() if w.screen() else w.frame()
        cw, ch = 620, 96
        fr = w.frameRectForContentRect_(NSMakeRect(0, 0, cw, ch))
        x = vf.origin.x + vf.size.width - fr.size.width - 20
        y = vf.origin.y + 20
        w.setFrame_display_animate_(NSMakeRect(x, y, fr.size.width, fr.size.height), True, True)
    else:
        if not _COMPACT:
            return
        w.setLevel_(_COMPACT["level"])
        w.setCollectionBehavior_(_COMPACT["behavior"])
        w.setFrame_display_animate_(_COMPACT["frame"], True, True)
        w.setContentMinSize_(_COMPACT["minsize"])
        _COMPACT.clear()


def _draw_dock(frac, playing):
    from AppKit import (NSApplication, NSBezierPath, NSColor, NSImage, NSMakeRect, NSMakePoint,
                        NSCompositingOperationSourceOver)
    app = NSApplication.sharedApplication()
    key = None if frac is None else (round(float(frac), 2), playing)
    if key == _DOCK["last"]:
        return
    _DOCK["last"] = key
    if _DOCK["base"] is None:
        icon = app.applicationIconImage()
        _DOCK["base"] = icon.copy() if icon is not None else None
    base = _DOCK["base"]
    if base is None:
        return
    if frac is None:
        app.setApplicationIconImage_(base)
        return
    size = base.size()
    img = NSImage.alloc().initWithSize_(size)
    img.lockFocus()
    base.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, size.width, size.height), NSMakeRect(0, 0, 0, 0),
                                                 NSCompositingOperationSourceOver, 1.0)
    r = size.width * 0.17
    cx, cy = size.width - r * 1.15, r * 1.15
    disc = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - r, cy - r, 2 * r, 2 * r))
    NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.82).setFill()
    disc.fill()
    track = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - r * .72, cy - r * .72, r * 1.44, r * 1.44))
    track.setLineWidth_(r * .26)
    NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.25).setStroke()
    track.stroke()
    arc = NSBezierPath.bezierPath()
    arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        NSMakePoint(cx, cy), r * .72, 90, 90 - 360 * max(0.02, min(1.0, float(frac))), True)
    arc.setLineWidth_(r * .26)
    arc.setLineCapStyle_(1)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.04, 0.52, 1.0, 1.0).setStroke()
    arc.stroke()
    img.unlockFocus()
    app.setApplicationIconImage_(img)


def start_send_listener():
    bridge_key()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", SEND_PORT), SendHandler)
    except OSError:
        return                      # another ALOUD already listens
    threading.Thread(target=srv.serve_forever, daemon=True).start()


RENDER_LOCK = threading.Lock()
GUI = {"on": False}


def render_page(url, timeout=25):
    """Load a page in a hidden web view, like a normal visit, and return its HTML once
    the text stops changing (for articles built by JavaScript or sites that refuse apps)."""
    import time
    import webview
    if not GUI["on"]:
        return None, None
    with RENDER_LOCK:
        loaded = threading.Event()
        w = webview.create_window("ALOUD page", url, hidden=True, width=1100, height=900)
        w.events.loaded += lambda: loaded.set()
        try:
            loaded.wait(timeout)
            t0, last, stable = time.time(), -1, 0
            while time.time() - t0 < 12:
                n = w.evaluate_js("document.body ? document.body.innerText.length : 0") or 0
                stable = stable + 1 if (n == last and n > 300) else 0
                if stable >= 3:
                    break
                last = n
                time.sleep(0.5)
            return w.evaluate_js("document.documentElement.outerHTML"), w.evaluate_js("location.href")
        finally:
            try:
                w.destroy()
            except Exception:
                pass


def main(document=None):
    import webview
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    if document is not None:
        url += "#open=" + quote(str(document.expanduser().resolve()))
    api = JsApi()
    win = webview.create_window("ALOUD", url, js_api=api, width=1280, height=860,
                                min_size=(720, 520), text_select=True)
    api.window = win
    WINDOW["win"] = win
    start_send_listener()

    def on_loaded():
        # files dropped onto the window: the page can't see their path, pywebview can
        try:
            from webview.dom import DOMEventHandler

            def on_drop(e):
                files = (e.get("dataTransfer") or {}).get("files") or []
                full = files[0].get("pywebviewFullPath") if files else None
                if full:
                    win.evaluate_js(f"window.aloudOpen({json.dumps(full)})")
            win.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:
            traceback.print_exc()
        keep_awake_while_reading(win)
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(setup_menubar)
            AppHelper.callAfter(register_media_keys, win)
        except Exception:
            traceback.print_exc()
    win.events.loaded += on_loaded
    set_mac_identity()
    GUI["on"] = True
    webview.start(private_mode=False, storage_path=str(SUPPORT / "webview"))
