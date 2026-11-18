"""Turn non-PDF documents (and pasted text) into a PDF, so every format goes through
the same reading pipeline and gets a page view.

- EPUB / MOBI / FB2 / XPS / CBZ: laid out by MuPDF itself.
- Word (.docx/.doc), RTF, OpenDocument, Pages-exported HTML, web archives: macOS `textutil` -> HTML.
- Markdown: markdown-it -> HTML.
- Plain text / pasted text: paragraphs -> HTML.
HTML is typeset into pages with pymupdf's Story, recording headings as a table of
contents (which the extractor uses to find section titles).
"""
import hashlib
import html as htmlmod
import re
import subprocess
from pathlib import Path

import pymupdf

NATIVE = {".mobi", ".fb2", ".xps", ".oxps", ".cbz"}
TEXTUTIL = {".docx", ".doc", ".rtf", ".rtfd", ".odt", ".wordml", ".webarchive"}
HTML = {".html", ".htm", ".xhtml"}
MARKDOWN = {".md", ".markdown", ".mdown"}
EPUB = {".epub"}
TEXT = {".txt", ".text"}
SUPPORTED = {".pdf"} | EPUB | NATIVE | TEXTUTIL | HTML | MARKDOWN | TEXT

CSS = """
body { font-family: serif; font-size: 11pt; line-height: 1.5; }
p { margin: 0 0 11pt 0; }
h1 { font-family: sans-serif; font-size: 17pt; margin: 14pt 0 8pt 0; }
h2 { font-family: sans-serif; font-size: 14pt; margin: 12pt 0 6pt 0; }
h3, h4, h5, h6 { font-family: sans-serif; font-size: 12pt; margin: 10pt 0 5pt 0; }
li { margin-bottom: 3pt; }
pre, code { font-family: monospace; font-size: 9pt; }
img { display: none; }
small { font-family: sans-serif; font-size: 8.5pt; color: #666; }
"""


def _key(path):
    p = Path(path)
    st = p.stat()
    return hashlib.sha1(f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}|c4".encode()).hexdigest()[:16]


def to_pdf(path, cache_dir, progress=None):
    """Return (pdf_path, title) for any supported document. PDFs are returned as-is,
    unless they are scans: then a copy with recognised text is made (once, cached)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        out = Path(cache_dir) / f"{_key(path)}-ocr.pdf"
        if out.exists():
            return out, None
        try:
            pages = needs_ocr(path)
        except Exception:
            pages = []
        if not pages:
            return path, None
        try:
            import Vision  # noqa: F401
        except ImportError:
            return path, None
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part")
        ocr_pdf(path, tmp, pages, progress)
        tmp.replace(out)
        return out, None
    if ext not in SUPPORTED:
        raise ValueError(f"Unsupported file type {ext or '(none)'}. ALOUD reads: "
                         + ", ".join(sorted(e.lstrip('.') for e in SUPPORTED)))
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{_key(path)}.pdf"
    title = None
    archive = None
    if ext in NATIVE:
        src = pymupdf.open(path)
        title = (src.metadata or {}).get("title") or None
        if not out.exists():
            if src.is_reflowable:
                src.layout(width=468 + 144, height=648 + 144, fontsize=11)
            toc = src.get_toc()
            pdf = pymupdf.open("pdf", src.convert_to_pdf())
            if toc:
                try:
                    pdf.set_toc(toc)
                except Exception:
                    pass
            pdf.save(out)
        return out, title
    if ext in TEXTUTIL:
        html = subprocess.run(["/usr/bin/textutil", "-convert", "html", "-stdout", str(path)],
                              capture_output=True, check=True).stdout.decode("utf-8", "replace")
    elif ext in HTML:
        html = path.read_text(encoding="utf-8", errors="replace")
        if (path.parent / f"{path.stem}_files").is_dir():
            archive = path.parent                    # an article saved with its pictures
    elif ext in EPUB:
        html, title = epub_to_html(path)
    elif ext in MARKDOWN:
        html = _markdown(path.read_text(encoding="utf-8", errors="replace"))
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
        if looks_like_markdown(raw):     # e.g. text copied from ChatGPT / Claude / GitHub
            html = _markdown(raw)
        else:
            html = text_to_html(raw)
        h = re.match(r"<html><body><h1>(.*?)</h1>", html)
        if h:
            title = htmlmod.unescape(h.group(1))
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m and m.group(1).strip():
        title = htmlmod.unescape(m.group(1).strip())
    if not title:
        h = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S) or re.search(r"<h[23][^>]*>(.*?)</h[23]>", html, re.I | re.S)
        if h and re.sub(r"<[^>]+>", "", h.group(1)).strip():
            title = htmlmod.unescape(re.sub(r"<[^>]+>", "", h.group(1)).strip())
    if not out.exists():
        clean = _clean_html(html)
        try:
            html_to_pdf(clean, out, archive)
        except RuntimeError:
            html_to_pdf(_flatten_layout_tables(clean, everything=True), out, archive)
    return out, title


def epub_to_html(path):
    """Chapters in reading (spine) order, as one HTML body."""
    import posixpath
    import zipfile
    from bs4 import BeautifulSoup
    z = zipfile.ZipFile(path)
    container = BeautifulSoup(z.read("META-INF/container.xml"), "xml")
    opf_path = container.find("rootfile")["full-path"]
    opf = BeautifulSoup(z.read(opf_path), "xml")
    base = posixpath.dirname(opf_path)
    title_el = opf.find("dc:title") or opf.find("title")
    title = title_el.get_text(strip=True) if title_el else None
    items = {i.get("id"): i.get("href") for i in opf.find_all("item")}
    bodies = []
    for ref in opf.find_all("itemref"):
        href = items.get(ref.get("idref"))
        if not href:
            continue
        name = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
        try:
            doc = BeautifulSoup(z.read(name), "html.parser")
        except KeyError:
            continue
        body = doc.body or doc
        for t in body.find_all(["script", "style", "img", "svg", "image"]):
            t.decompose()
        bodies.append(body.decode_contents())
    return "<html><body>" + "\n".join(bodies) + "</body></html>", title


def looks_like_markdown(text):
    signs = [
        r"(?m)^#{1,6} \S",                 # headings
        r"\*\*[^*\n]+\*\*|__[^_\n]+__",   # bold
        r"(?m)^\s*[-*+] \S",               # bullet lists
        r"(?m)^\s*\d+[.)] \S",             # numbered lists
        r"(?m)^> \S",                      # quotes
        r"\[[^\]\n]+\]\([^)\s]+\)",        # links
        r"(?m)^```",                        # code
        r"(?m)^\|.+\|\s*$",                # tables
    ]
    return sum(bool(re.search(x, text)) for x in signs) >= 2 or len(re.findall(r"\*\*[^*\n]+\*\*", text)) >= 2


def text_to_html(text, title=None):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if re.search(r"\n\s*\n", text):
        blocks = [re.sub(r"\s*\n\s*", " ", b) for b in re.split(r"\n\s*\n", text)]
    else:
        blocks = text.split("\n")
    blocks = [b.strip() for b in blocks if b.strip()]
    if not title and len(blocks) > 1 and len(blocks[0]) <= 90 and not re.search(r"[.!?,;:]$", blocks[0]):
        title = blocks.pop(0)     # a short first line is a title
    parts = [f"<h1>{htmlmod.escape(title)}</h1>"] if title else []
    parts += [f"<p>{htmlmod.escape(b)}</p>" for b in blocks]
    return "<html><body>" + "\n".join(parts) + "</body></html>"


def _markdown(raw):
    """Markdown -> HTML, keeping LaTeX formulas intact (Markdown would eat their _ and *)."""
    from markdown_it import MarkdownIt
    from .mathspeech import latex_segments
    keep = []

    def hold(seg_text):
        keep.append(seg_text)
        return f"MATHHOLD{len(keep) - 1}X"
    out, last = [], 0
    for a, b, body in latex_segments(raw):
        display = raw[a:b].startswith(("$$", "\\[", "\\\\["))
        out.append(raw[last:a])
        out.append(hold(("\\[ " if display else "\\(") + " ".join(body.split()) + (" \\]" if display else "\\)")))
        last = b
    out.append(raw[last:])
    html = MarkdownIt("commonmark").enable("table").render("".join(out))
    return re.sub(r"MATHHOLD(\d+)X", lambda m: htmlmod.escape(keep[int(m.group(1))]), html)


def _clean_html(html):
    # drop scripts/styles/navigation clutter that Story would otherwise print
    html = re.sub(r"<(script|style|noscript|nav|header|footer|svg)\b.*?</\1>", "", html, flags=re.I | re.S)
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.I | re.S)
    body = m.group(1) if m else html
    if "<table" in body.lower():
        body = _flatten_layout_tables(body)
    return f"<html><body>{body}</body></html>"


def _flatten_layout_tables(body, everything=False):
    """Old-style sites (and some converters) lay the whole page out in a table. A cell
    taller than a page can't be typeset, so tables that hold prose become plain blocks;
    real data tables (short cells) are kept."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "lxml")
    blocks = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "pre", "table", "div"]
    for t in reversed(soup.find_all("table")):           # innermost first
        cells = t.find_all(["td", "th"])
        if everything or len(t.get_text()) > 2500 or any(len(c.get_text()) > 600 or c.find(blocks) for c in cells):
            for c in cells:
                c.name = "div"
            for tag in t.find_all(["tr", "tbody", "thead", "tfoot", "colgroup", "col", "caption"]):
                tag.unwrap()
            t.name = "div"
    b = soup.body
    return b.decode_contents() if b else str(soup)


IMG_CSS = """
img { display: block; margin: 12pt auto 6pt; }
figure { margin: 14pt 0; }
figure.hero { margin: 4pt 0 18pt; }
figcaption { font-family: sans-serif; font-size: 9pt; color: #6b6b6b; text-align: center; margin-top: 2pt; }
"""


def html_to_pdf(html, out, archive=None):
    if archive is not None:
        story = pymupdf.Story(html=html, user_css=CSS.replace("img { display: none; }", "") + IMG_CSS,
                              archive=pymupdf.Archive(str(archive)))
    else:
        story = pymupdf.Story(html=html, user_css=CSS)
    mediabox = pymupdf.paper_rect("letter")
    where = mediabox + (72, 72, -72, -72)
    toc = []

    def record(pos):
        if pos.heading and (pos.open_close & 1) and pos.text and pos.text.strip():
            toc.append([pos.heading, pos.text.strip(), pos.page_num + 1])

    writer = pymupdf.DocumentWriter(str(out))
    more, page = 1, 0
    while more:
        if page > 3000:     # something can't be broken across pages; caller retries without tables
            writer.close()
            raise RuntimeError("layout did not converge")
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.element_positions(record, {"page_num": page})
        story.draw(dev)
        writer.end_page()
        page += 1
    writer.close()
    if toc:
        d = pymupdf.open(out)
        lvl, fixed = 0, []
        for l, t, p in toc:          # set_toc needs levels that step down one at a time
            l = min(l, lvl + 1)
            fixed.append([l, t, p])
            lvl = l
        d.set_toc(fixed)
        d.saveIncr()


# ---------------------------------------------------------------- OCR (scanned PDFs)

def _vision_words(png):
    """On-device text recognition (macOS Vision). Returns [(word, x0, y0, x1, y1)] in 0..1, top-left origin."""
    import Vision
    from Foundation import NSData, NSMakeRange
    data = NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    if req.respondsToSelector_("setAutomaticallyDetectsLanguage:"):
        req.setAutomaticallyDetectsLanguage_(True)
    ok, _err = handler.performRequests_error_([req], None)
    out = []
    for obs in (req.results() or []) if ok else []:
        cand = obs.topCandidates_(1)[0]
        text = cand.string()
        lb = obs.boundingBox()
        line_h = lb.size.height
        line_bottom = 1 - lb.origin.y
        for m in re.finditer(r"\S+", text):
            box, _e = cand.boundingBoxForRange_error_(NSMakeRange(m.start(), m.end() - m.start()), None)
            if box is None:
                continue
            bb = box.boundingBox()
            x0, x1 = bb.origin.x, bb.origin.x + bb.size.width
            y1 = 1 - bb.origin.y
            y0 = y1 - bb.size.height
            out.append((m.group(), x0, y0, x1, y1, line_h, line_bottom))
    return out


def needs_ocr(pdf_path):
    d = pymupdf.open(pdf_path)
    empty = [p.number for p in d if len(p.get_text().strip()) < 40 and (p.get_images() or p.get_drawings())]
    # only real scans: a normal PDF with a few full-page figures is left alone
    return empty if len(empty) >= max(1, 0.5 * len(d)) else []


def ocr_pdf(src, out, pages, progress=None):
    """Copy of `src` with invisible, positioned text on the given (scanned) pages."""
    from concurrent.futures import ThreadPoolExecutor
    d = pymupdf.open(src)
    done = 0
    pool = ThreadPoolExecutor(max_workers=4)
    for start in range(0, len(pages), 8):
        batch = pages[start:start + 8]
        pngs = [d[pno].get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False).tobytes("png") for pno in batch]
        results = list(pool.map(_vision_words, pngs))
        for pno, found in zip(batch, results):
            _place_words(d[pno], found)
            done += 1
            if progress:
                progress(done, len(pages))
    pool.shutdown()
    d.save(out, garbage=3, deflate=True)


def _place_words(page, found):
    W, H = page.rect.width, page.rect.height
    if not found:
        return
    if True:
        import statistics
        typical = statistics.median([f[5] for f in found])
        for word, x0, y0, x1, y1, line_h, line_bottom in found:
            bx0, by0, bx1, by1 = x0 * W, y0 * H, x1 * W, y1 * H
            w = bx1 - bx0
            if w <= 0:
                continue
            # one whole-point size per line: the extractor uses font sizes to find the body text
            # (lines within ±30% of the page's typical height share one body size)
            h_eff = typical if abs(line_h - typical) < 0.3 * typical else line_h
            fs = max(3.0, float(round(h_eff * H * 0.8)))
            try:
                # all words of a line share its baseline; a trailing space keeps words apart
                page.insert_text((bx0, line_bottom * H - line_h * H * 0.22), word + " ", fontsize=fs,
                                 fontname="helv", render_mode=3)
            except Exception:
                pass
