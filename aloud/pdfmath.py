"""Display equations in PDFs -> LaTeX -> speech.

A PDF only has positioned glyphs: a fraction is three lines, a sum's limits are separate
fragments. So we group the math fragments the layout pass set aside into equation
boxes, render each box as an image and read it with an image-to-LaTeX model
(LaTeX-OCR / pix2tex, run through onnxruntime). The model can invent things when the
image isn't a formula, so its answer is kept only when its letters and digits agree
with the characters the PDF itself has in that box.
"""
import re
import threading
from collections import Counter
from pathlib import Path

import pymupdf

from .paths import SUPPORT

MODELS = SUPPORT / "latex-ocr"
MATH_FONT = re.compile(r"^(CM(MI|SY|EX|R|BX|MIB|BSY|SS|TT)?\d*|MSBM|MSAM|LMMath|LMRoman|.*Math|rsfs|eufm|eusm|"
                       r"txsy|txex|pxsy|pxex|esint|wasy|stmary|Type3|STIX)", re.I)
MATH_CHAR = re.compile(r"[=<>≤≥≈≃∼∝∈∉⊂⊆∪∩∑∏∫∂∇√∞±×·÷→←⇒⇔∀∃αβγδεϵζηθϑικλμνξπρσςτυφϕχψωΓΔΘΛΞΠΣΦΨΩ∥⊤⊙⊗]")
EQ_NUM = re.compile(r"^\(\s?[A-Z]?\d+(\.\d+)?[a-z]?\s?\)$")

_OCR = None
_OCR_LOCK = threading.Lock()


def available():
    try:
        import rapid_latex_ocr  # noqa: F401
    except Exception:   # noqa: BLE001
        return False
    return all((MODELS / f).exists() for f in ("image_resizer.onnx", "encoder.onnx", "decoder.onnx", "tokenizer.json"))


def _ocr():
    global _OCR
    with _OCR_LOCK:
        if _OCR is None:
            import numpy as np
            from rapid_latex_ocr import LaTeXOCR

            class _NP2(LaTeXOCR):      # the package's resizer loop predates NumPy 2
                def loop_image_resizer(self, img):
                    from PIL import Image
                    pad_img = self.pre_pro.pad(Image.fromarray(img))
                    input_image = self.pre_pro.minmax_size(pad_img).convert("RGB")
                    r, w, h = 1, input_image.size[0], input_image.size[1]
                    for _ in range(10):
                        h = int(h * r)
                        final_img, pad_img = self.pre_process(input_image, r, w, h)
                        res = self.image_resizer([final_img.astype(np.float32)])[0]
                        w = (int(np.argmax(res, axis=-1).reshape(-1)[0]) + 1) * 32
                        if w == pad_img.size[0]:
                            break
                        r = w / pad_img.size[0]
                    return final_img
            _OCR = _NP2(image_resizer_path=str(MODELS / "image_resizer.onnx"),
                            encoder_path=str(MODELS / "encoder.onnx"), decoder_path=str(MODELS / "decoder.onnx"),
                            tokenizer_json=str(MODELS / "tokenizer.json"))
    return _OCR


_DICTS = {}


def page_dict(doc, pg):
    """Text-only layout of a page (no images: much faster), cached per document."""
    key = (doc.name, pg)
    if key not in _DICTS:
        if len(_DICTS) > 400:
            _DICTS.clear()
        flags = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_MEDIABOX_CLIP
        _DICTS[key] = doc[pg].get_text("dict", flags=flags)
    return _DICTS[key]


def _inside(bb, r):
    """Is most of span box bb inside rect r? (plain numbers: called very often)"""
    w = min(bb[2], r[2]) - max(bb[0], r[0])
    h = min(bb[3], r[3]) - max(bb[1], r[1])
    if w <= 0 or h <= 0:
        return False
    return w * h > 0.5 * max(1e-6, (bb[2] - bb[0]) * (bb[3] - bb[1]))


def _math_share(page, rect):
    r = tuple(rect)
    spans = [s for b in page_dict(page.parent, page.number)["blocks"] for l in b.get("lines", [])
             if l["bbox"][3] >= r[1] and l["bbox"][1] <= r[3]
             for s in l["spans"] if s["text"].strip() and _inside(s["bbox"], r)]
    tot = sum(len(s["text"].strip()) for s in spans)
    if not tot:
        return 0.0, ""
    m = sum(len(s["text"].strip()) for s in spans if MATH_FONT.match(s["font"]) or MATH_CHAR.search(s["text"]))
    return m / tot, " ".join(s["text"] for s in spans)


def find_equations(doc, paras):
    """Group skipped math fragments into equation boxes.
    Returns [dict(page, rect, paras=[indices], number)] in reading order."""
    in_fig = {}
    for p in paras:                 # labels inside pictures and diagrams are never equations
        if p.get("kind") == "fig":
            pg, *r = p["fig"]
            in_fig.setdefault(pg, []).append(pymupdf.Rect(r))
    frags = []
    for i, p in enumerate(paras):
        if p["kind"] != "skip" or not p.get("rects") or re.match(r"(Figure|Fig\.|Table|Algorithm)\s", p["text"]):
            continue
        byp = {}
        for pg, *r in p["rects"]:
            byp.setdefault(pg, pymupdf.Rect(r))
            byp[pg] |= pymupdf.Rect(r)
        for pg, r in byp.items():
            if any(abs(r & f) > 0.5 * abs(r) for f in in_fig.get(pg, [])):
                continue
            share, _ = _math_share(doc[pg], r)
            frags.append(dict(i=i, page=pg, rect=r, math=share >= 0.5 or bool(MATH_CHAR.search(p["text"]))))
    # union fragments that touch (sum limits, fraction lines, multi-line equations)
    parent = list(range(len(frags)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    by_page = {}
    for k, f in enumerate(frags):
        by_page.setdefault(f["page"], []).append(k)
    pairs = ((a, b) for ks in by_page.values() for x, a in enumerate(ks) for b in ks[x + 1:])
    for a, b in pairs:
        fa, fb = frags[a], frags[b]
        if True:
            if not (fa["math"] or fb["math"]):
                continue
            ra, rb = fa["rect"], fb["rect"]
            vgap = max(rb.y0 - ra.y1, ra.y0 - rb.y1)
            hgap = max(rb.x0 - ra.x1, ra.x0 - rb.x1)
            if (vgap < 3.5 and hgap < 12) or (vgap < 0 and hgap < 40):
                parent[find(a)] = find(b)
    groups = {}
    for k, f in enumerate(frags):
        groups.setdefault(find(k), []).append(f)
    out = []
    for g in groups.values():
        if not any(f["math"] for f in g):
            continue
        page = doc[g[0]["page"]]
        rect = pymupdf.Rect(g[0]["rect"])
        for f in g[1:]:
            rect |= f["rect"]
        words = page.get_text("words", clip=rect + (-1, -1, 1, 1))
        number = None
        body = [w for w in words if not (EQ_NUM.match(w[4]) and w[0] > rect.x0 + 0.6 * rect.width)]
        if len(body) < len(words):
            number = next(w[4] for w in words if EQ_NUM.match(w[4]) and w[0] > rect.x0 + 0.6 * rect.width).strip("() ")
        if not body:
            continue
        crop = pymupdf.Rect(min(w[0] for w in body), rect.y0, max(w[2] for w in body), rect.y1)
        share, text = _math_share(page, crop)
        if share < 0.55 or crop.width < 25 or crop.height < 7:
            continue
        if not re.search(r"[=<>≤≥≈≃∼∝∈←→⇒]", text) and not re.search(r"[∑∏∫]", text):
            continue        # a lone symbol or a table cell, not an equation
        out.append(dict(page=g[0]["page"], rect=crop, paras=sorted({f["i"] for f in g}), number=number,
                        chars="".join(w[4] for w in body)))
    out.sort(key=lambda e: (e["page"], e["rect"].y0))
    return out


GREEK = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
         "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
         "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
         "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
         "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω", "ell": "l"}
WORDS = {"log", "exp", "sin", "cos", "tan", "max", "min", "lim", "sup", "inf", "det", "arg", "tanh", "ln"}


def _letters(s):
    s = s.replace("ϵ", "ε").replace("ϕ", "φ").replace("ϑ", "θ").replace("ℓ", "l")
    return Counter(c for c in s if c.isalnum())


def _latex_letters(tex):
    t = re.sub(r"\\(operatorname|mathrm|mathbf|mathcal|mathbb|mathit|text|textrm|boldsymbol|bm|mathsf)\s*\{", "{", tex)
    t = re.sub(r"\\([A-Za-z]+)", lambda m: GREEK.get(m.group(1), m.group(1) if m.group(1) in WORDS else " "), t)
    return _letters(t)


def agreement(tex, chars):
    a, b = _latex_letters(tex), _letters(chars)
    common = sum((a & b).values())
    tot = sum(a.values()) + sum(b.values())
    return 2 * common / tot if tot else 0.0


def clean_latex(tex):
    t = re.sub(r"\\(tag|label)\{[^{}]*\}", "", tex)
    t = re.sub(r"\\q?quad\s*\(\s*\d+[a-z]?\s*\)\s*$", "", t)
    t = re.sub(r"[\s.,;~\\]*$", "", t)                    # sentence punctuation after the equation
    t = t.replace(r"\mathrm{log}", r"\log").replace(r"\mathrm{exp}", r"\exp")
    return t.strip()


SUSPICIOUS = re.compile(r"\\(L|tt|mathtt|texttt|Im|Re|nonumber|underbrace|overbrace|stackrel|overset|underset|"
                        r"rfloor|lfloor|hbar|smile|frown|Therefore|scriptsize|tiny)\b|\{\{\{\{|(\\qquad\s*){3,}")


def plausible(tex, chars):
    """Is the model's LaTeX a faithful reading of this box?"""
    if not tex:
        return 0.0, False
    a = agreement(tex, chars)
    if len(tex) > 6 * max(20, len(chars)):
        return a, False                                   # runaway output
    return a, a >= 0.86 or (a >= 0.72 and not SUSPICIOUS.search(tex))


def fix_confusions(tex, chars):
    """Letters the model tends to confuse, checked against the PDF's own characters."""
    if "Z" not in chars and "L" in chars:
        tex = re.sub(r"\\(mathcal|mathbb|cal|mathscr)\s*\{?\s*Z\s*\}?", r"\\mathcal{L}", tex)
    return tex


def read_equations(pdf_path, eqs, progress=None, min_agree=0.6):
    """Fill eq['latex'] (or None) for each equation box."""
    if not eqs or not available():
        return eqs
    doc = pymupdf.open(pdf_path)
    model = _ocr()
    for k, e in enumerate(eqs):
        if progress:
            progress(k, len(eqs))
        try:
            pix = doc[e["page"]].get_pixmap(clip=e["rect"] + (-2, -2, 2, 2), dpi=200)
            res = model(pix.tobytes("png"))
            tex = clean_latex(res[0] if isinstance(res, tuple) else res)
        except Exception:   # noqa: BLE001
            tex = ""
        tex = fix_confusions(tex, e["chars"])
        a, ok = plausible(tex, e["chars"])
        e["agree"] = round(a, 2)
        e["latex"] = tex if ok else None
    return eqs


PDF_LOCK = threading.Lock()      # the app swaps in its own: MuPDF must not be used from two threads at once


def ocr_one(pdf_path, e):
    """LaTeX for one equation box (or None)."""
    try:
        with PDF_LOCK:
            png = pymupdf.open(pdf_path)[e["page"]].get_pixmap(
                clip=pymupdf.Rect(e["rect"]) + (-2, -2, 2, 2), dpi=200).tobytes("png")
        res = _ocr()(png)
        tex = clean_latex(res[0] if isinstance(res, tuple) else res)
    except Exception:   # noqa: BLE001
        return None
    tex = fix_confusions(tex, e["chars"])
    a, ok = plausible(tex, e["chars"])
    return tex if ok else None


def attach_equations(pdf_path, paras):
    """Turn each display equation's skipped fragments into one spoken paragraph whose
    words are filled in later by resolve() (reading equations takes a second or two each)."""
    doc = pymupdf.open(pdf_path)
    names = {re.sub(r"^[A-Z]{6}\+", "", f[3]) for pg in doc.pages(0, min(len(doc), 40)) for f in pg.get_fonts()}
    if not any(MATH_FONT.match(n) and not n.startswith("Type3") for n in names):
        return paras
    eqs = find_equations(doc, paras)
    if not eqs:
        return paras
    first, drop = {}, set()
    for e in eqs:
        first[e["paras"][0]] = e
        drop.update(e["paras"][1:])
    out = []
    for i, p in enumerate(paras):
        if i in first:
            e = first[i]
            r = e["rect"]
            text = " ".join(" ".join(paras[j]["text"] for j in e["paras"]).split())
            out.append(dict(kind="p", page=e["page"] + 1, text=text,
                            eq=dict(page=e["page"], rect=[round(v, 1) for v in r], chars=e["chars"],
                                    number=e["number"], latex=None, done=False),
                            boxes=[[0, len(text), e["page"], round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]]))
        elif i not in drop:
            out.append(p)
    return out


_URGENT = [0]


def busy():
    """Is playback waiting for an equation? (background reading should step aside)"""
    return _URGENT[0] > 0


def resolve(pdf_path, p, lang="en", urgent=False):
    """Read one equation paragraph (once): set its LaTeX and spoken words."""
    eq = p.get("eq")
    if not eq or eq.get("done"):
        return
    if urgent:
        _URGENT[0] += 1
    try:
        _resolve(pdf_path, p, lang)
    finally:
        if urgent:
            _URGENT[0] -= 1


def _resolve(pdf_path, p, lang):
    from .mathspeech import speak_latex
    eq = p["eq"]
    with _READ_LOCK:
        if eq.get("done"):
            return
        tex = ocr_one(pdf_path, eq)
        said = speak_latex([tex], lang)[0] if tex else None
        if said and re.search(r"backslash|with .{1,30} over it|under ⌣", said):
            said = None
        eq["latex"] = tex if said else None
        p["spans"] = [[0, len(p["text"]), said]]
        eq["done"] = True


_READ_LOCK = threading.Lock()


# ---------------------------------------------------------------- inline math (from the glyphs)
STRONG = re.compile(r"^(CMMI|CMSY|CMEX|CMMIB|CMBSY|MSBM|MSAM|LMMathItalic|LMMathSymbols|LMMathExtension|"
                    r"txmi|txsy|pxmi|pxsy|rsfs|eufm|eusm|stmary|esint|wasy|.*Math(Italic|Symbol|Pi)|CambriaMath|STIXMath|"
                    r"STIXGeneral-Italic|STIXTwoMath)", re.I)
WEAK = re.compile(r"^(CMR|CMBX|CMTI|CMSS|LMRoman|LMSans|STIXGeneral)", re.I)
SYM = {"|": r"\mid ", "∼": r"\sim ", "−": "-", "·": r"\cdot ", "{": r"\{", "}": r"\}", "∥": r"\| ", "⊤": r"\top ",
       "∇": r"\nabla ", "∂": r"\partial ", "∑": r"\sum ", "∫": r"\int ", "∏": r"\prod ", "≈": r"\approx ",
       "≃": r"\simeq ", "∝": r"\propto ", "→": r"\to ", "←": r"\leftarrow ", "∞": r"\infty ", "⊙": r"\odot ",
       "⊗": r"\otimes ", "×": r"\times ", "±": r"\pm ", "≤": r"\le ", "≥": r"\ge ", "≠": r"\ne ", "∈": r"\in ",
       "∉": r"\notin ", "⊂": r"\subset ", "⊆": r"\subseteq ", "∪": r"\cup ", "∩": r"\cap ", "∀": r"\forall ",
       "∃": r"\exists ", "′": "'", "ℓ": r"\ell ", "ϵ": r"\epsilon ", "ε": r"\varepsilon ", "ϕ": r"\phi ",
       "φ": r"\varphi ", "µ": r"\mu ", "ϱ": r"\varrho ", "\\": r"\setminus ", "∖": r"\setminus ", "ϑ": r"\vartheta ", "ϖ": r"\varpi ", "ς": r"\varsigma ", "∗": "^{*}", "⋆": "^{*}", "�": ""}
FUNC_WORDS = {"log", "exp", "ln", "sin", "cos", "tan", "tanh", "sinh", "cosh", "max", "min", "sup", "inf", "lim",
              "det", "arg", "dim", "ker", "deg", "gcd", "Pr"}
GREEK_CH = {v: k for k, v in GREEK.items() if len(v) == 1 and k not in ("varepsilon", "varphi", "vartheta", "ell")}
TEXTY = re.compile(r"[A-Za-z]{4,}")


def _tex_char(c):
    if c in SYM:
        return SYM[c]
    if c in GREEK_CH:
        return "\\" + GREEK_CH[c] + " "
    if c in "#$%&_^\\~":
        return ""
    return c


def _line_runs(spans, body, body_is_cm):
    """Math runs in one line: [(raw_chars, latex)]."""
    base0 = Counter(round(s["origin"][1], 1) for s in spans
                    if s["size"] >= body * 0.9 and s["text"].strip()[:1] not in "√∑∏∫⋃⋂").most_common(1)
    base0 = base0[0][0] if base0 else None

    def kind(s):
        t, f = s["text"].strip(), re.sub(r"^[A-Z]{6}\+", "", s["font"])
        if not t:
            return "space"
        if (base0 is not None and s["size"] < body * 0.85 and abs(s["origin"][1] - base0) > 0.05 * body
                and len(t) <= 8 and not re.search(r"[A-Za-z]{2,}\s+[A-Za-z]", t)):
            return "math"       # a sub/superscript, even a word like "model" in d_model
        if STRONG.match(f) or MATH_CHAR.search(t) and not TEXTY.search(t):
            return "math"
        if WEAK.match(f) and (not body_is_cm or s["size"] < body * 0.85):
            return "math" if not TEXTY.search(t) or not body_is_cm else "text"
        if s["size"] < body * 0.85 and len(t) <= 3 and not TEXTY.search(t):
            return "math"
        if WEAK.match(f) and len(t) <= 3 and not re.search(r"[A-Za-z]{2}", t):
            return "bridge"
        return "text"
    ks = [kind(s) for s in spans]
    base = Counter(round(s["origin"][1], 1) for s in spans
                   if s["size"] >= body * 0.9 and s["text"].strip()[:1] not in "√∑∏∫⋃⋂").most_common(1)
    base = base[0][0] if base else None
    runs, i = [], 0
    while i < len(spans):
        if ks[i] != "math":
            i += 1
            continue
        j = i
        while j + 1 < len(spans) and (ks[j + 1] in ("math", "space") or
                                      (ks[j + 1] == "bridge" and any(k == "math" for k in ks[j + 2:j + 4]))):
            j += 1
        while j > i and ks[j] in ("space", "bridge"):
            j -= 1
        run = spans[i:j + 1]
        if base is None:
            i = j + 1
            continue
        tex, raw, mode, roots, radicand = "", "", None, 0, False
        for s in run:
            t = "".join(c for c in s["text"].strip() if not 0xFE00 <= ord(c) <= 0xFE0F)
            if not t:
                continue
            small = s["size"] < body * 0.85
            dy = s["origin"][1] - base
            m = "sup" if small and dy < -0.12 * body else ("sub" if small and dy > 0.1 * body else None)
            if not tex and m:
                m = None                                  # nothing to attach a script to
            if t[:1] in "√∑∏∫⋃⋂":
                m = None                                  # big operators sit off the baseline; they aren't scripts
            if roots and radicand and m is None:          # a radicand ends where the next full-size glyph starts
                if mode:
                    tex += "}"
                    mode = None
                tex += "}" * roots
                roots, radicand = 0, False
            if m != mode:
                if mode:
                    tex += "}"
                if m:
                    tex += "^{" if m == "sup" else "_{"
                mode = m
            for tok in re.findall(r"[A-Za-z]{2,}|.", t):
                if tok.isspace():
                    continue
                if len(tok) > 1:                          # an upright word: a function name or a label
                    tex += ("\\" + tok + " ") if tok in FUNC_WORDS else ("\\mathrm{" + tok + "}")
                elif tok == "√":
                    tex += "\\sqrt{"
                    roots += 1
                    continue
                else:
                    tex += _tex_char(tok)
                if roots and m is None:
                    radicand = True
            raw += "".join(c for c in t if not c.isspace())
        if mode:
            tex += "}"
        tex += "}" * roots
        tex = re.sub(r"[.,;:]+$", "", tex.strip())
        raw = re.sub(r"[.,;:]+$", "", raw)
        bad = (re.search(r"\^\{[^{}]*[(=→][^{}]*\}", tex)                     # a stacked fraction read as a power
               or re.search(r"\\(sum|prod)\s*(?![_^ ]|\s*[_^])", tex)          # limits we couldn't place
               or re.search(r"\\(sum|prod)\s*\^\{", tex) and not re.search(r"\\(sum|prod)\s*_\{", tex))
        if raw and re.search(r"[A-Za-zͰ-Ͽ∇∂∑∫]", raw) and not bad:
            first = next(s for s in run if s["text"].strip())
            runs.append((raw, tex, first["bbox"][0]))
        i = j + 1
    return runs


def inline_math(pdf_path, paras):
    """Find inline formulas in spoken paragraphs: p['math'] = [[start, end, latex]]."""
    doc = pymupdf.open(pdf_path)
    mathy = {pg for pg in range(len(doc))
             if any(STRONG.match(re.sub(r"^[A-Z]{6}\+", "", f[3])) for f in doc[pg].get_fonts())}
    if not mathy:
        return                                  # no math fonts anywhere: nothing to do
    sizes, fonts = Counter(), Counter()
    page_lines = {}

    def lines_of(pg):
        if pg not in page_lines:
            page_lines[pg] = [l for b in page_dict(doc, pg)["blocks"] for l in b.get("lines", [])]
            for l in page_lines[pg]:
                for s in l["spans"]:
                    n = len(s["text"].strip())
                    sizes[round(s["size"], 1)] += n
                    fonts[re.sub(r"^[A-Z]{6}\+", "", s["font"])] += n
        return page_lines[pg]
    for pg in range(min(len(doc), 6)):
        lines_of(pg)
    if not sizes:
        return
    body = sizes.most_common(1)[0][0]
    body_font = fonts.most_common(1)[0][0]
    body_is_cm = bool(WEAK.match(body_font) or STRONG.match(body_font))
    mlines = {}

    def math_lines(pg):
        """Only lines with something mathematical in them."""
        if pg not in mlines:
            mlines[pg] = [l for l in lines_of(pg) if any(
                STRONG.match(re.sub(r"^[A-Z]{6}\+", "", s["font"])) or MATH_CHAR.search(s["text"])
                or (not body_is_cm and WEAK.match(re.sub(r"^[A-Z]{6}\+", "", s["font"]))) for s in l["spans"])]
        return mlines[pg]
    for p in paras:
        if p.get("kind") != "p" or p.get("eq") or not p.get("boxes"):
            continue
        found = []
        for pg in sorted({b[2] for b in p["boxes"]} & mathy):
            boxes = [b for b in p["boxes"] if b[2] == pg]
            for l in math_lines(pg):
                x0, y0, x1, y1 = l["bbox"]
                hits = [b for b in boxes if b[3] < x1 and b[5] > x0 and b[4] < y1 and b[6] > y0]
                if not hits:
                    continue
                spans = sorted(l["spans"], key=lambda s: s["bbox"][0])
                for raw, tex, rx0 in _line_runs(spans, body, body_is_cm):
                    word = [b for b in hits if b[3] - 1.5 <= rx0 <= b[5] + 0.5]
                    if word:
                        found.append((min(b[0] for b in word), raw, tex, True))
                    else:
                        found.append((min(b[0] for b in hits), raw, tex, False))
        if not found:
            continue
        out, text = [], p["text"]
        for start, raw, tex, exact in sorted(found, key=lambda f: f[0]):
            def first(rx):
                # whole tokens only ("i" must not match inside "industry"), starting at the run's own word
                for m in re.compile(rx).finditer(text, start if exact else max(0, start - 40)):
                    if exact and m.start() > start + 3:
                        return None
                    if m.end() - m.start() < 1:
                        continue
                    if m.start() > 0 and text[m.start() - 1].isalnum() and text[m.start()].isalnum():
                        continue
                    if m.end() < len(text) and text[m.end()].isalnum() and text[m.end() - 1].isalnum():
                        continue
                    return m
                return None
            m = first(r"[\s\ufe00-\ufe0f]?".join(re.escape(c) for c in raw))
            if not m:      # small raised digits may have been dropped from the text as footnote marks
                m = first(r"\s?".join(f"(?:{re.escape(c)})?" if c.isdigit() else re.escape(c) for c in raw))
                if m and m.end() - m.start() < 2:
                    m = None
            if m:
                a, b = m.start(), m.end()
                while b > a and text[b - 1].isspace():
                    b -= 1
                if not any(x < b and a < y for x, y, _ in out):
                    out.append([a, b, tex])
        out.sort()
        merged = []
        for a, b, tex in out:          # pieces split by "=", "+", a big operator, a subscript line ...
            if merged:
                pa, pb, ptex = merged[-1]
                gap = text[pb:a]
                if re.fullmatch(r"[\s\ufe00-\ufe0f]*([=+\-−<>≤≥≈∼|�][\s\ufe00-\ufe0f]*){0,2}", gap) and a - pb <= 7:
                    j = "".join((r"\int " if re.search(r"d\s?[a-zA-Z\\]", tex) else "") if c == "�" else _tex_char(c)
                                for c in gap if not c.isspace() and not 0xFE00 <= ord(c) <= 0xFE0F)
                    merged[-1] = [pa, b, ptex + j + tex]
                    continue
            merged.append([a, b, tex])
        for m in merged:
            if "�" in text[m[0]:m[1]] and r"\int" not in m[2] and re.search(r"d\s?[a-zA-Z\\]\S*$", m[2]):
                m[2] = re.sub(r"(=)\s*", r"\1\\int ", m[2], count=1)

        def whole(a, b, tex):
            """Only read formulas we have completely; a partial one is left to the skipper."""
            t = re.sub(r"\\(left|right|big|Big|bigg|Bigg)[lr]?", "", tex)
            if t.count("(") != t.count(")") or t.count("[") != t.count("]") or t.count("{") != t.count("}"):
                return False
            if re.search(r"[(\[=+−\-/^_]$", text[a:b]):
                return False                                   # ends on an operator: the rest is elsewhere
            nxt = text[b:b + 1]
            if nxt and (nxt in "/)]^_" or nxt.isalnum() and text[b - 1].isalnum()):
                return False                                   # glued to more formula
            if re.match(r"\s?[=+−/×·]\s?[A-Za-z(\d\u0370-\u03ff]", text[b:b + 5]) and not re.match(
                    r"\s?[=+−/×·]\s?[\d.,]+\s+[a-z]{2,}", text[b:b + 30]):
                return False                                   # the expression goes on
            return True
        merged = [m for m in merged if whole(*m)]
        if merged:
            p["math"] = merged
