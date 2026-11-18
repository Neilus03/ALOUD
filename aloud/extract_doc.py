"""Turn a document into readable, positioned paragraphs.

Each paragraph keeps its original text for display plus "spans" describing how
it is spoken: ranges that are skipped (citations) or replaced (e.g. "e.g." ->
"for example"). Paragraphs that should not be read at all (footnotes,
equations, tables, page furniture) are kept with kind="skip" so the reader can
still show them, dimmed.
"""
import hashlib
import re
import unicodedata
import statistics
from collections import Counter
from pathlib import Path

import pymupdf

PREFIX_WORDS = {
    "well", "self", "non", "co", "pre", "post", "pro", "anti", "multi", "cross", "long",
    "short", "high", "low", "so", "half", "full", "large", "small", "two", "three",
    "first", "second", "time", "firm", "industry", "market", "state", "real", "short",
}

# ---------------------------------------------------------------- helpers

def doc_id(path):
    p = Path(path)
    st = p.stat()
    return hashlib.sha1(f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}|v3".encode()).hexdigest()[:16]


def _clean_spaces(s):
    s = s.replace("­", "")
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" ([,.;:!?%)\]’”])", r"\1", s)
    s = re.sub(r"([(\[“‘]) ", r"\1", s)
    s = re.sub(r"(\w) ’ (\w)", r"\1’\2", s)
    return s.strip()


def _alpha_ratio(s):
    words = re.findall(r"\S+", s)
    if not words:
        return 0.0
    good = [w for w in words if re.fullmatch(r"[A-Za-z][a-z’'\-]{2,}[,.;:)]?", w)]
    return len(good) / len(words)


VOCAB = set()   # words seen inside lines of the current document (for de-hyphenation)


def _dehyphen(prefix, rest):
    """True to join 'pro-' + 'cessing' -> 'processing'; False to keep 'class-agnostic'."""
    p, r = prefix.lower(), re.match(r"[A-Za-z]*", rest).group().lower()
    if VOCAB:
        if p + r in VOCAB:
            return True
        if f"{p}-{r}" in VOCAB:
            return False
        if len(r) >= 3 and p in VOCAB and r in VOCAB:
            return False
        return rest[:1].islower()
    return rest[:1].islower() and p not in PREFIX_WORDS


def _join_lines(lines):
    out = ""
    for ln in lines:
        if not out:
            out = ln
            continue
        m = re.search(r"([A-Za-z]+)-$", out)
        if m and ln[:1].isalpha() and (ln[:1].islower() or (VOCAB and _dehyphen(m.group(1), ln))):
            if not _dehyphen(m.group(1), ln):
                out = out + ln
            else:
                out = out[:-1] + ln
        else:
            out = out + " " + ln
    return _clean_spaces(out)


# ---------------------------------------------------------------- speech spans

CITE_PAREN = re.compile(r" ?\((?=[^()]*[A-Z][^()]*,\s*(?:1[89]|20)\d\d[a-z]?\b|[^()]*\bforthcoming\b)[^()]{3,400}\)")
YEAR_PAREN = re.compile(r"(?<=[A-Za-z.]) ?\(\s*(?:1[89]|20)\d\d[a-z]?(?:\s*[,;]\s*(?:1[89]|20)\d\d[a-z]?)*\s*\)")
MARKUP = re.compile(r"\*{1,3}(?=\S)|(?<=\S)\*{1,3}|^#{1,6} |(?<![\w])__?(?=\w)|(?<=\w)__?(?![\w])|`+|^> ")
CITE_NUM = re.compile(r" ?\[\s*\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*\s*\]")
REPLACEMENTS = [
    (re.compile(r"\be\.g\.,?"), "for example,"),
    (re.compile(r"\bi\.e\.,?"), "that is,"),
    (re.compile(r"\bet al\.(?=\s*[a-z,;])"), "et al"),
    (re.compile(r"\bet al\."), "et al."),
    (re.compile(r"\bcf\."), "compare"),
    (re.compile(r"\bvs\."), "versus"),
    (re.compile(r"\bFig\.(?= ?\d)"), "Figure"),
    (re.compile(r"\bSec\.(?= ?\d)"), "Section"),
    (re.compile(r"\bTab\.(?= ?\d)"), "Table"),
    (re.compile(r"\bTabs\.(?= ?\d)"), "Tables"),
    (re.compile(r"\bFigs\.(?= ?\d)"), "Figures"),
    (re.compile(r"\bEq\.(?= ?\(?\d)"), "Equation"),
    (re.compile(r"\$(\d[\d.,]*)\s?(K|k)\b"), lambda m: f"${m.group(1)} thousand"),
    (re.compile(r"\$(\d[\d.,]*)\s?(M|m|mn|MM)\b"), lambda m: f"${m.group(1)} million"),
    (re.compile(r"\$(\d[\d.,]*)\s?(B|bn|b)\b"), lambda m: f"${m.group(1)} billion"),
    (re.compile(r"\$(\d[\d.,]*)\s?(T|tn)\b"), lambda m: f"${m.group(1)} trillion"),
    # money ranges: "$75,000–$190,000" -> "$75,000 to $190,000"
    (re.compile(r"(?<=\d)\s?[–—-]\s?(?=\$\d)"), " to "),
    (re.compile(r"(\d{4})-(\d{4})"), lambda m: f"{m.group(1)} to {m.group(2)}"),
    # number ranges: "4.2–5.9", "10-20%" -> "4.2 to 5.9"
    (re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(\s*[–—]\s*|-)(?=\d+(?:\.\d+)?%?(?![\w.]*[A-Za-z]))"), lambda m: f"{m.group(1)} to "),
    # magnitudes: "15.99M parameters", "2.1B tokens", "40K"
    (re.compile(r"(?<![$\w.])(\d+(?:\.\d+)?)\s?([KMB])\b(?!\.\w)"),
     lambda m: f"{m.group(1)} " + {"K": "thousand", "M": "million", "B": "billion"}[m.group(2)]),
    (re.compile(r"\bpp\.\s?(?=\d)"), "pages "),
    (re.compile(r"(?<![\w.])p\.\s?(?=\d)"), "page "),
    (re.compile(r"\bEqs\.(?= ?\(?\d)"), "Equations"),
    (re.compile(r"\bSecs?\.(?= ?\d)"), "Section"),
    (re.compile(r"\bRefs?\.(?= ?\[?\d)"), "reference"),
    (re.compile(r"\bApp\.(?= ?[A-Z\d])"), "Appendix"),
    (re.compile(r"\bapprox\."), "approximately"),
    (re.compile(r"\bresp\."), "respectively"),
    (re.compile(r"\bw\.r\.t\."), "with respect to"),
    (re.compile(r"\bi\.i\.d\."), "I I D"),
    (re.compile(r"\betc\.(?=\s+[a-z])"), "et cetera"),
    (re.compile(r"\betc\."), "et cetera."),
    (re.compile(r"\bviz\."), "namely"),
    (re.compile(r"\bNo\.(?= ?\d)"), "number"),
    (re.compile(r"\s?∈\s?"), " in "), (re.compile(r"\s?∉\s?"), " not in "),
    (re.compile(r"\s?×\s?(?=\d)"), " times "), (re.compile(r"\s?±\s?"), " plus or minus "),
    (re.compile(r"(?<=\d)×(?!\s?\d)"), " times"),
    (re.compile(r"\s?≈\s?"), " approximately "), (re.compile(r"(?<!\w)~\s?(?=\d)"), "about "),
    (re.compile(r"\s?≠\s?"), " not equal to "), (re.compile(r"\s?→\s?"), " to "),
    (re.compile(r"[ﬁ]"), "fi"), (re.compile(r"[ﬂ]"), "fl"), (re.compile(r"ﬀ"), "ff"),
]


# web links / e-mails are named, not spelled out; DOIs and arXiv ids are skipped
URL = re.compile(r"\(?\b(?:https?://|www\.)[^\s)]+[^\s.,;:)\]]\)?")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\w")
NOISE = re.compile(r"\s?(?:\(?\b(?:doi:\s?|https?://doi\.org/)10\.\d{4,9}/[^\s)]+[^\s.,;:)\]]\)?"
                   r"|\(?\b10\.\d{4,9}/[^\s)]+[^\s.,;:)\]]\)?"
                   r"|\(?\barXiv:\s?\d{4}\.\d{4,5}(?:v\d)?\)?)")


NAMES = {
    "en": dict(link="the link", email="the email address", doi="the DOI", arxiv="the arXiv preprint"),
    "es": dict(link="el enlace", email="la dirección de correo", doi="el DOI", arxiv="el preprint de arXiv"),
    "fr": dict(link="le lien", email="l'adresse e-mail", doi="le DOI", arxiv="la prépublication arXiv"),
    "it": dict(link="il link", email="l'indirizzo email", doi="il DOI", arxiv="il preprint arXiv"),
    "pt": dict(link="o link", email="o endereço de e-mail", doi="o DOI", arxiv="o preprint do arXiv"),
    "de": dict(link="dem Link", email="die E-Mail-Adresse", doi="die DOI", arxiv="der arXiv-Preprint"),
}
# ---- inline formulas: skipped, except simple facts like "N = 5" or "p < 0.05"
MATH_OP = re.compile(r"[=≤≥<>∈∉⊂⊆⊇∪∩⋃⋂∑∏∫→↦≈≠±×·∥⊤∇∂∞^\\|]")
COMMON = set("""a an the of in on at to is are was were be been and or but for as by with from that this these those
than then so if it its we our you they their there here not no all any each per can may will has have had do does
which who what when where how such into over under after before between both either neither more most less least
one two three four five six seven eight nine ten first second also only very via while about up down out""".split())


def _symbolic(tok):
    t = tok.strip(".,;:()[]{}")
    if not t:
        return True
    if MATH_OP.search(t) or re.search(r"[α-ωΑ-Ωϕϵℓ]", t):
        return True
    if len(t) == 1 and t.isupper():
        return True                                   # "A", "B", "R" are variables, not the article
    if t.lower() in COMMON or len(t) > 6:
        return False
    # variable-shaped: "k", "Kv", "Rv,k", "sij", "x2", "h'" (not ordinary words), numbers, maths functions
    return bool(re.fullmatch(r"[A-Za-z]{1,2}(?:[,_][a-z0-9]{1,2}|\d)?['′*]?|\d+[a-z]{1,2}|[\d.,]+|[+−\-/*]"
                             r"|exp|log|max|min|argmax|argmin|sup|inf|lim|softmax", t))


def formula_spans(text):
    """Character ranges of inline formulas worth skipping."""
    toks = [(m.start(), m.end(), m.group()) for m in re.finditer(r"\S+", text)]
    out, i = [], 0
    while i < len(toks):
        if not MATH_OP.search(toks[i][2].strip(".,;:")):
            i += 1
            continue
        a = b = i
        while a > 0 and _symbolic(toks[a - 1][2]) and not re.search(r"[.!?:;]$", toks[a - 1][2]):
            a -= 1
        while b + 1 < len(toks) and _symbolic(toks[b + 1][2]) and not re.search(r"[.!?;]$", toks[b][2]):
            b += 1
        run = " ".join(t[2] for t in toks[a:b + 1]).strip(".,;:")
        simple = (re.fullmatch(r"\(?[A-Za-z]\w{0,2}\s?[=<>≤≥≈]\s?-?[\d.,]+%?\)?", run)          # N = 5, p < 0.05
                  or re.fullmatch(r"\(?[A-Za-z]\s?[<>≤≥]\s?[A-Za-z]\)?", run)                       # A > B
                  or not re.search(r"[A-Za-zα-ωΑ-Ω]", run))                                        # 72 → 128, 6.8×
        dims = re.fullmatch(r"[\d.,]+\s?[×x]\s?[\d.,]+", run)
        if not simple and not dims and not re.fullmatch(r"[\d.,%]+", run):
            s0, e0 = toks[a][0], toks[b][1]
            while e0 > s0 and text[e0 - 1] in ".,;:":
                e0 -= 1                                  # keep the sentence's own punctuation
            if s0 > 0 and text[s0 - 1] == " ":
                s0 -= 1
            if s0 > 0 and text[s0 - 1] == "," and e0 < len(text) and text[e0] in ".;:":
                s0 -= 1                                  # "outside it, Kv \ R." -> "outside it."
            out.append((s0, e0))
        i = b + 1
    return out


# ---- pronunciation fixes for the voice engine
_GOLD = None


def _gold():
    """Words the voice engine's high-confidence dictionary knows."""
    global _GOLD
    if _GOLD is None:
        try:
            import json as _json
            from importlib.resources import files
            _GOLD = set(_json.loads((files("misaki") / "data" / "us_gold.json").read_text()))
        except Exception:
            _GOLD = set()
    return _GOLD


PREFIXED = re.compile(r"\b([Pp]re|[Pp]ost|[Ff]ine|[Uu]p|[Dd]own|[Ss]ub|[Mm]ulti|[Rr]e|[Pp]oint)([a-z]{4,})\b")


def _hyphenate(m):
    """'pretraining' -> 'pre-training' when the engine would otherwise guess (and guess wrong)."""
    word, pre, rest = m.group(0), m.group(1), m.group(2)
    g = _gold()

    def in_dict(w):
        w = w.lower()
        return w in g or any(w.endswith(sf) and (w[:-len(sf)] in g or w[:-len(sf)] + "e" in g or
                                                 (len(w) > len(sf) + 2 and w[-len(sf) - 1] == w[-len(sf) - 2] and w[:-len(sf) - 1] in g))
                             for sf in ("ing", "ed", "es", "s", "er", "ers", "ly", "ally", "ations", "ation"))
    if not g or in_dict(word):
        return None                      # the engine knows this word (or a form of it): leave it alone
    if word[0].isupper() and not re.search(r"(ing|ed|tion)$", rest):
        return None                      # capitalised: only clear verb forms, never names like "Reuters"
    return f"{pre}-{rest}" if in_dict(rest) else None


# characters the voice can't pronounce (it would spell out their code): skip them
UNSPEAKABLE = re.compile("[\ufe00-\ufe0f\u02b0-\u02ff\u2190-\u21ff\u2300-\u23ff\u25a0-\u25ff\u2600-\u27bf\u2b00-\u2bff"
                         "\U0001F000-\U0001FAFF†‡§¶•◦▪■□▶︎✓✔✗✘]")

def _r(pat, say):
    return (re.compile(pat), say)


LANG_REPLACEMENTS = {
    "de": [_r(r"\bz\.\s?B\.", "zum Beispiel"), _r(r"\bd\.\s?h\.", "das heißt"), _r(r"\bu\.\s?a\.", "unter anderem"),
           _r(r"\bbzw\.", "beziehungsweise"), _r(r"\bvgl\.", "vergleiche"), _r(r"\bVgl\.", "Vergleiche"),
           _r(r"\bca\.", "circa"), _r(r"\bS\.\s?(?=\d)", "Seite "), _r(r"\bAbb\.\s?(?=\d)", "Abbildung "),
           _r(r"\bTab\.\s?(?=\d)", "Tabelle "), _r(r"\bKap\.\s?(?=\d)", "Kapitel "), _r(r"\bNr\.\s?(?=\d)", "Nummer "),
           _r(r"\busw\.", "und so weiter"), _r(r"\bggf\.", "gegebenenfalls"), _r(r"\bsog\.", "sogenannte"),
           _r(r"\bet al\.", "et al."), _r(r"\bi\.\s?d\.\s?R\.", "in der Regel")],
    "es": [_r(r"\bp\.\s?ej\.", "por ejemplo"), _r(r"\bpág\.\s?(?=\d)", "página "), _r(r"\bet al\.", "et al."),
           _r(r"\bEE\.\s?UU\.", "Estados Unidos"), _r(r"\bfig\.\s?(?=\d)", "figura ")],
    "fr": [_r(r"\bp\.\s?ex\.", "par exemple"), _r(r"\bc\.-à-d\.", "c'est-à-dire"), _r(r"\bet al\.", "et al."),
           _r(r"\bp\.\s?(?=\d)", "page "), _r(r"\bfig\.\s?(?=\d)", "figure ")],
    "it": [_r(r"\bad es\.", "ad esempio"), _r(r"\bpag\.\s?(?=\d)", "pagina "), _r(r"\bet al\.", "et al.")],
    "pt": [_r(r"\bp\.\s?ex\.", "por exemplo"), _r(r"\bpág\.\s?(?=\d)", "página "), _r(r"\bet al\.", "et al.")],
}

UNIVERSAL = [r for r in REPLACEMENTS if r[1] in ("fi", "fl", "ff")]


def speech_spans(text, skip_citations=True, lang="en", lexicon=None, math=None):
    """Return list of [start, end, spoken] covering text; spoken None = skip, str = replace.
    Ranges not listed are spoken as-is."""
    edits = []
    taken = [False] * (len(text) + 1)

    def add(s, e, val):
        if any(taken[s:e]):
            return
        for i in range(s, e):
            taken[i] = True
        edits.append([s, e, val])

    for word, say in (lexicon or {}).items():          # the user's own pronunciations come first
        if not word.strip():
            continue
        flags = re.I if word == word.lower() else 0
        for m in re.finditer(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, flags):
            add(m.start(), m.end(), say if say.strip() else None)
    if math:                                           # inline formulas rebuilt from the PDF's glyphs
        try:
            from .mathspeech import speak_latex
            for (a, b, _), said in zip(math, speak_latex([m[2] for m in math], lang)):
                if said and not re.search(r"backslash", said):
                    add(a, b, said)
        except Exception:
            pass
    try:                                               # formulas written in LaTeX: read them as math
        from .mathspeech import latex_segments, speak_latex
        segs = latex_segments(text)
        if segs:
            for (a, b, _), said in zip(segs, speak_latex([g[2] for g in segs], lang)):
                add(a, b, said)
    except Exception:
        pass
    if skip_citations:
        for rx in (CITE_PAREN, YEAR_PAREN, CITE_NUM):
            for m in rx.finditer(text):
                add(m.start(), m.end(), None)
    for m in NOISE.finditer(text):
        g = m.group().strip()
        add(m.start(), m.end(), None if g.startswith("(") else " " + NAMES[lang if lang in NAMES else "en"]["arxiv" if "arXiv" in g else "doi"])
    for m in URL.finditer(text):
        paren = m.group().startswith("(")
        add(m.start() - (1 if paren and m.start() and text[m.start() - 1] == " " else 0), m.end(), None if paren else NAMES[lang if lang in NAMES else "en"]["link"])
    for m in EMAIL.finditer(text):
        add(m.start(), m.end(), NAMES[lang if lang in NAMES else "en"]["email"])
    for a, b in formula_spans(text):
        add(a, b, None)
    if lang == "en":
        for m in PREFIXED.finditer(text):
            r = _hyphenate(m)
            if r:
                add(m.start(), m.end(), r)
    for m in re.finditer("ℓ", text):
        add(m.start(), m.end(), "L")
    for m in MARKUP.finditer(text):
        add(m.start(), m.end(), None)
    for rx, rep in (REPLACEMENTS if lang == "en" else LANG_REPLACEMENTS.get(lang, []) + UNIVERSAL):
        for m in rx.finditer(text):
            add(m.start(), m.end(), rep(m) if callable(rep) else rep)
    for m in UNSPEAKABLE.finditer(text):
        add(m.start(), m.end(), None)
    # "(1)" after a word refers to an equation: say "equation 1" (not just "one"); list
    # numbering after ":", ";", "," or "and/or" and "Equation (3)" are left as they are
    if lang == "en":
        for m in re.finditer(r"(?<=[A-Za-z’'\)]) \((\d{1,2}[a-z]?)\)(?=[\s,.;:])", text):
            before = text[max(0, m.start() - 12):m.start()].lower().strip()
            if not re.search(r"(\b(and|or|eqs?\.?|equations?)|\))$", before):
                add(m.start(), m.end(), f" equation {m.group(1)}")
    edits.sort()
    return edits


# ---------------------------------------------------------------- PDF

def _big_title(d):
    """The title printed on the first page: the largest text in its upper part."""
    for pg in d.pages(0, min(2, len(d))):
        H = pg.rect.height
        lines = []
        for b in pg.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                if l.get("dir", (1, 0))[1] != 0:
                    continue                                   # rotated margin text (arXiv stamp)
                t = " ".join("".join(s["text"] for s in l["spans"]).split())
                if (l["bbox"][1] > (0.85 if pg.number == 0 else 0.55) * H or len(re.findall(r"[A-Za-z]{2,}", t)) < 1
                        or re.match(r"(table of )?contents$", t, re.I)):
                    continue
                size = max(s["size"] for s in l["spans"] if s["text"].strip())
                lines.append((size, l["bbox"][1], l["bbox"][3], t))
        if not lines:
            continue
        sizes = sorted({round(x[0], 1) for x in lines}, reverse=True)
        weight = Counter()
        for size, _, _, t in lines:
            weight[round(size, 1)] += len(t)
        body = weight.most_common(1)[0][0]
        top = sizes[0]
        if top < body * 1.2:
            continue                                           # nothing stands out as a title
        picked = sorted([x for x in lines if round(x[0], 1) >= top - 0.6], key=lambda x: x[1])
        out, last = [], None
        for size, y0, y1, t in picked:
            if last is not None and y0 - last > 1.6 * size:
                break                                          # only the first block of title lines
            out.append(t)
            last = y1
        title = " ".join(out).strip(" *†‡∗")
        if 3 <= len(title) <= 250 and not re.match(r"(abstract|contents|preface)$", title, re.I):
            return title
    return ""


def _norm_title(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


SKIP_SECTION = re.compile(
    r"(references|bibliography|works cited|literature cited|acknowledge?ments?|funding|"
    r"conflicts? of interest|competing interests?|declaration of (competing )?interests?|"
    r"author contributions?|data availability( statement)?|code availability|ethics statement|"
    r"credit authorship contribution statement)\b\.?$", re.I)
SKIP_LINE = re.compile(r"(keywords?|key words|jel( classification)?( codes?)?|index terms|msc( 2020)?)\s*[:.—–-]", re.I)

FIGURE_NUMBER = r"(?:[a-z]\.?)?\d+(?:\.\d+)*"
FIGURE_CAPTION = re.compile(rf"^(?:figure\s+|fig\.\s*)({FIGURE_NUMBER})(?!\.\d)(?=\s|[.:—–-]|$)(?P<separator>\s*[.:—–-])?", re.I)
FIGURE_TARGET = rf"(?P<number>{FIGURE_NUMBER})(?P<panel>[a-z]\b|\s*\([a-z]\))?(?!\w|\.\d)"
FIGURE_REFERENCE = re.compile(rf"\b(?P<name>figures?\s+|figs?\.\s*){FIGURE_TARGET}", re.I)
FIGURE_LIST_ITEM = re.compile(rf"\s*(?:,\s*(?:and\s+)?|and\s+|[—–-]\s*){FIGURE_TARGET}", re.I)
PANEL_LABEL = re.compile(r"\(?([a-z])\)", re.I)
CROP_PADDING = 3


def extract_pdf(path, converted=False):
    d = pymupdf.open(path)
    flags = pymupdf.TEXT_DEHYPHENATE & 0  # we dehyphenate ourselves
    toc = d.get_toc()
    title = (d.metadata or {}).get("title") or ""
    if len(title) < 4 or title.lower().startswith(("microsoft", "untitled")) or title.endswith((".dvi", ".pdf", ".doc", ".docx")):
        title = ""

    # pass 0: margin line numbers (review copies put them at the same x on every page)
    dicts = [page.get_text("dict", flags=flags) for page in d]
    num_pos = Counter()
    for dct in dicts:
        for b in dct["blocks"]:
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    if re.fullmatch(r"\s*\d{1,4}\s*", sp["text"]):
                        num_pos[round(sp["bbox"][0] / 4)] += 1
    linenum = {k for k, c in num_pos.items() if c >= max(25, 3 * len(d))}

    def is_linenum(sp):
        return (re.fullmatch(r"\s*\d{1,4}\s*", sp["text"]) is not None
                and round(sp["bbox"][0] / 4) in linenum)

    # collect lines
    raw_pages = []
    page_words, page_sizes, furn, page_figs = [], [], [], []
    size_count = Counter()
    for pno, page in enumerate(d):
        W, H = page.rect.width, page.rect.height
        page_sizes.append([round(W, 1), round(H, 1)])
        page_words.append(page.get_text("words"))
        furn.append([])
        # figure areas: raster images and clusters of vector drawings
        # (an image covering most of the page is a scan or a background, not a figure)
        figs = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()
                if pymupdf.Rect(i["bbox"]).height > 3 and abs(pymupdf.Rect(i["bbox"]) & page.rect) < 0.6 * abs(page.rect)]
        try:
            if not converted:   # in documents we typeset, drawings are only rules and code boxes
                figs += [r for r in page.cluster_drawings() if r.width > 40 and r.height > 40
                         and abs(r & page.rect) < 0.6 * abs(page.rect)]      # not a page background
        except Exception:
            pass
        page_figs.append(figs)
        lines = []
        for b in dicts[pno]["blocks"]:
            for l in b.get("lines", []):
                allspans = [sp for sp in l["spans"] if sp["text"].strip()]
                for sp in allspans:
                    if is_linenum(sp):
                        furn[pno].append([round(v, 1) for v in sp["bbox"]])
                lspans = [sp for sp in l["spans"] if not is_linenum(sp)]
                spans = [sp for sp in lspans if sp["text"].strip()]
                if not spans:
                    continue
                big = max(sp["size"] for sp in spans)
                kept = [sp for sp in lspans if not (
                    sp["size"] < big * 0.8 and re.fullmatch(r"\s*[\d,*†‡§]+\s*", sp["text"]) and len(spans) > 1)]
                spans = [sp for sp in kept if sp["text"].strip()] or spans
                text = "".join(sp["text"] for sp in kept) or "".join(sp["text"] for sp in spans)
                sizes = Counter()
                for sp in spans:
                    sizes[round(sp["size"], 1)] += len(sp["text"].strip())
                size = sizes.most_common(1)[0][0]
                bold = sum(len(sp["text"]) for sp in spans if sp["flags"] & 16) > 0.6 * sum(len(sp["text"]) for sp in spans)
                x0 = min(sp["bbox"][0] for sp in spans); x1 = max(sp["bbox"][2] for sp in spans)
                y0 = min(sp["bbox"][1] for sp in spans); y1 = max(sp["bbox"][3] for sp in spans)
                lines.append(dict(text=text, size=size, bold=bold, x0=x0, x1=x1, y0=y0, y1=y1, page=pno))
                size_count[size] += len(text)
        raw_pages.append((W, H, lines))

    body = size_count.most_common(1)[0][0]

    # two-column layout?
    bl = [(r, W) for W, H, lines in raw_pages for r in lines if r["size"] == body and len(r["text"]) > 25]
    left = sum(1 for r, W in bl if r["x1"] < W * 0.53)
    right = sum(1 for r, W in bl if r["x0"] > W * 0.47)
    two_col = bool(bl) and left > 0.2 * len(bl) and right > 0.2 * len(bl)

    def col_of(r, W):
        if not two_col:
            return 0
        mid = W / 2
        if r["x0"] < mid - 40 and r["x1"] > mid + 40:
            return 0
        return 1 if (r["x0"] + r["x1"]) / 2 < mid else 2

    pages = []
    for pno, (W, H, lines) in enumerate(raw_pages):
        for r in lines:
            r["col"] = col_of(r, W)
        # the short last line of a full-width block (e.g. a caption) stays with that block
        if two_col:
            fulls = []
            for r in sorted(lines, key=lambda q: q["y0"]):
                if r["col"] == 0:
                    fulls.append(r)
                elif any(abs(q["x0"] - r["x0"]) < 4 and 0 < r["y0"] - q["y0"] < 1.7 * q["size"] for q in fulls[-4:]):
                    r["col"] = 0
                    fulls.append(r)
            # pieces of a full-width line that pymupdf split apart
            for _ in range(3):
                for r in lines:
                    if r["col"] and any(q["col"] == 0 and abs(q["y1"] - r["y1"]) < 3 and max(q["x0"] - r["x1"], r["x0"] - q["x1"]) < 25 for q in lines):
                        r["col"] = 0
        for r in lines:
            c = pymupdf.Point((r["x0"] + r["x1"]) / 2, (r["y0"] + r["y1"]) / 2)
            nwords = len(r["text"].split())
            r["fig"] = False
            for f in page_figs[pno]:
                inside = (f + (-2, -2, 2, 2)).contains(c)
                near = (r["x1"] > f.x0 and r["x0"] < f.x1 and min(abs(r["y1"] - f.y0), abs(r["y0"] - f.y1)) < 22)
                if (inside and (nwords <= 6 or f.height * f.width > 0.08 * W * H)) or (near and nwords <= 4 and not re.search(r"[.:]$", r["text"].strip())):
                    r["fig"] = True
                    break
        # merge fragments that share a baseline within a column (pymupdf splits lines at big gaps)
        lines.sort(key=lambda r: (r["col"], r["y1"]))
        groups = []
        for r in lines:
            if groups and groups[-1][0]["col"] == r["col"] and abs(groups[-1][0]["y1"] - r["y1"]) < 3:
                groups[-1].append(r)
            else:
                groups.append([r])
        merged = []
        for g in groups:
            g.sort(key=lambda r: r["x0"])
            cur = dict(g[0])
            for r in g[1:]:
                gap = r["x0"] - cur["x1"]
                if r["x0"] >= cur["x1"] - 2 and (gap < 15 or (gap < 60 and r["fig"] == cur["fig"])):
                    cur["fig"] = cur["fig"] and r["fig"]
                    cur["text"] += " " + r["text"]
                    cur["x1"] = r["x1"]
                    cur["y0"] = min(cur["y0"], r["y0"])
                    if r["size"] > cur["size"] and len(r["text"]) > len(cur["text"]) // 2:
                        cur["size"] = r["size"]
                else:
                    merged.append(cur)
                    cur = dict(r)
            merged.append(cur)
        # reading order: full-width lines split the page into bands; in each band, left column then right
        merged.sort(key=lambda r: (r["y0"], r["x0"]))
        ordered, band = [], []
        for r in merged:
            if r["col"] == 0 and not r["fig"]:
                ordered += sorted(band, key=lambda q: (q["col"], q["y0"], q["x0"])); band = []
                ordered.append(r)
            else:
                band.append(r)
        ordered += sorted(band, key=lambda q: (q["col"], q["y0"], q["x0"]))
        pages.append((H, ordered))

    # vocabulary of whole words (skip each line's first word and a hyphenated last word)
    VOCAB.clear()
    for _, lines in pages:
        for r in lines:
            toks = re.findall(r"[A-Za-z][A-Za-z\-]*[A-Za-z]-?", r["text"])
            if toks and toks[-1].endswith("-"):
                toks = toks[:-1]
            for tk in toks[1:]:
                VOCAB.add(tk.lower())
                VOCAB.update(x.lower() for x in tk.split("-") if len(x) > 1)

    # repeated header/footer lines
    edge_text = Counter()
    for H, lines in pages:
        for r in lines:
            if r["y0"] < 0.08 * H or r["y1"] > 0.92 * H:
                edge_text[re.sub(r"\d+", "#", _clean_spaces(r["text"]))] += 1
    repeated = {t for t, c in edge_text.items() if c >= max(3, len(pages) // 4)}

    toc_by_page = {}
    for lvl, t, pg in toc:
        toc_by_page.setdefault(pg - 1, []).append((lvl, t))

    # line spacing within paragraphs, and each column's left/right edges
    gaps = []
    for H, lines in pages:
        for a, b in zip(lines, lines[1:]):
            if a["size"] == body and b["size"] == body and a["col"] == b["col"] and 0 < b["y0"] - a["y0"] < 3 * body:
                gaps.append(b["y0"] - a["y0"])
    lead = statistics.median(gaps) if gaps else body * 1.5
    if converted:
        lead = body * 1.5      # we typeset it: line-height 1.5, paragraphs spaced further apart
    col_left, col_right = {}, {}
    for c in (0, 1, 2):
        xs = [r for _, lines in pages for r in lines if r["size"] == body and r["col"] == c and not r["fig"]]
        if len(xs) >= 10:
            col_left[c] = statistics.median([r["x0"] for r in xs])
            col_right[c] = statistics.quantiles([r["x1"] for r in xs], n=10)[8]
    for c in (0, 1, 2):
        col_left.setdefault(c, col_left.get(1, 72) if c == 0 else 72)
        col_right.setdefault(c, col_right.get(2, 540) if c == 0 else 540)
    if two_col and 0 in col_left and 1 in col_left:
        col_left[0] = min(col_left[0], col_left[1])

    paras = []  # dict(kind, text, page, level)

    def flush(buf, kind):
        if not buf:
            return
        text = _join_lines([_clean_spaces(r["text"]) for r in buf])
        if not text:
            return
        paras.append(dict(kind=kind, text=text, page=buf[0]["page"] + 1, lines=list(buf)))

    for pno, (H, lines) in enumerate(pages):
        pending_toc = list(toc_by_page.get(pno, []))
        buf, buf_kind, prev = [], None, None
        for r in lines:
            t = _clean_spaces(r["text"])
            key = re.sub(r"\d+", "#", t)
            # page furniture
            if (r["y0"] < 0.08 * H or r["y1"] > 0.92 * H) and (key in repeated or re.fullmatch(r"[\divxlc]+", t)):
                furn[pno].append([round(r["x0"], 1), round(r["y0"], 1), round(r["x1"], 1), round(r["y1"], 1)])
                continue
            # headings from TOC
            nt = _norm_title(re.sub(r"^[\dA-Z]?(\.\d+)*\.?\s+", "", t))
            hit = None
            for i, (lvl, tt) in enumerate(pending_toc):
                ntt = _norm_title(re.sub(r"^[\dA-Z]?(\.\d+)*\.?\s+", "", tt))
                if ntt and (nt == ntt or (nt.endswith(ntt) and len(nt) - len(ntt) <= 4)):
                    hit = (i, lvl, tt)
                    break
            if hit is not None:
                flush(buf, buf_kind); buf, prev = [], None
                pending_toc.pop(hit[0])
                paras.append(dict(kind="h", level=hit[1], text=hit[2], page=pno + 1, lines=[r]))
                continue
            if hit is None and not r["fig"] and re.fullmatch(r"(Abstract|Acknowledge?ments?|Appendix|Supplementary Material)", t, re.I):
                flush(buf, buf_kind); buf, prev = [], None
                paras.append(dict(kind="h", level=1, text=t, page=pno + 1, lines=[r]))
                continue
            if re.fullmatch(r"(References|Bibliography|Works Cited|Literature Cited)", t, re.I):
                flush(buf, buf_kind); buf, prev = [], None
                paras.append(dict(kind="h", level=1, text=t, page=pno + 1, refs=True, lines=[r]))
                continue
            cl, cr = col_left[r["col"]], col_right[r["col"]]
            pr = col_right[prev["col"]] if prev else cr
            full_width = r["x0"] < cl + 15 and r["x1"] > cr - 25
            digits = sum(c.isdigit() for c in t) / max(1, len(t))
            if re.match(r"(Figure|Fig\.|Table|Algorithm)\s+[A-Z]?\d+[.:]", t):
                kind = "cap"
            elif buf_kind == "cap" and prev is not None and 0 < r["y0"] - prev["y0"] <= lead * 1.35 and abs(r["x0"] - prev["x0"]) < 30:
                kind = "cap"    # caption continues
            elif r["fig"]:
                kind = "skip"   # labels inside / next to figures
            elif r["size"] < body * 0.88:
                kind = "skip"   # footnotes, small captions
            elif not converted and r["size"] < body - 0.4 and r["y0"] > 0.62 * H:
                kind = "skip"   # footnotes only slightly smaller than the text, at the foot of the page
            elif (buf_kind == "p" and prev is not None and 0 < r["y0"] - prev["y0"] <= lead * 1.35
                  and abs(r["x0"] - prev["x0"]) < 4 and len(t.split()) <= 3 and re.search(r"[.!?,;:)”]$", t)):
                kind = "p"      # short last line of a paragraph ("Llama 3.1 405B.")
            elif len(re.findall(r"(?<![\w.])[−-]?\d*\.\d+\**(?![\w.])", t)) >= 3:
                kind = "math"   # a regression-table row: "-.015*** -.014*** -.0034"
            elif digits > 0.3 or (not converted and _alpha_ratio(t) < 0.35 and not full_width and len(t) < 160):
                kind = "math"   # equations, table rows
            else:
                kind = "p"
            new_para = (
                prev is None or kind != buf_kind
                or (r["col"] != prev["col"] and kind != "cap")
                or r["y0"] - prev["y0"] > lead * 1.35
                or r["y0"] < prev["y0"] - 2  # new column / jumped up
                or (kind == "p" and r["x0"] > cl + 8 and prev["x1"] < pr - 40)
                or (kind == "cap" and re.match(r"(Figure|Fig\.|Table|Algorithm)\s", t))
            )
            # a short previous line usually ends a paragraph
            if not new_para and kind == "p" and prev["x1"] < pr - 60 and re.search(r"[.!?:”)]$", _clean_spaces(prev["text"])):
                new_para = True
            if new_para:
                flush(buf, buf_kind)
                buf, buf_kind = [], kind
            buf.append(r)
            prev = r
        flush(buf, buf_kind)

    # one-line fragments wedged against table rows are table rows too
    for k, p in enumerate(paras):
        if p["kind"] == "p" and len(p["lines"]) <= 2 and len(p["text"]) < 60 and not re.search(r"[.!?:”]$", p["text"]):
            if any(0 <= k + o < len(paras) and paras[k + o]["kind"] == "math" and len(paras[k + o]["text"].strip()) >= 6
                   and not re.search(r"\(\d+[a-z]?\)\s*$", paras[k + o]["text"])
                   for o in (-1, 1)):    # a real table row: not an equation, not the "1" of an inline fraction
                p["kind"] = "math"

    # back-matter sections nobody wants read aloud are treated like the reference list
    for p in paras:
        if p["kind"] == "h" and SKIP_SECTION.match(re.sub(r"^[\dA-Z]?(\.\d+)*\.?\s+", "", p["text"].strip())):
            p["refs"] = True
        elif p["kind"] == "p" and SKIP_LINE.match(p["text"]):
            p["kind"] = "skip"

    # merge paragraphs split across pages / around skipped blocks
    out = []
    in_refs = False
    for p in paras:
        if p["kind"] == "h":
            in_refs = bool(p.get("refs"))
        elif in_refs:
            p["kind"] = "skip"
        if p["kind"] in ("math", "cap"):
            p["kind"] = "skip"
        if (not converted and p["kind"] == "p" and len(p["lines"]) == 1 and len(p["text"].split()) <= 3
                and not p["text"][:1].islower()
                and (not re.search(r"[.!?]$", p["text"]) or len(p["text"]) < 16)):
            p["kind"] = "skip"
        if p["kind"] == "p" and out:
            # find last spoken paragraph, ignoring footnotes in between
            j = len(out) - 1
            while j >= 0 and out[j]["kind"] == "skip" and len(out) - j < 25:
                j -= 1
            last = out[j] if j >= 0 else None
            if last and last["kind"] == "p" and not re.search(r"[.!?:”]$", last["text"]) and p["text"][:1].islower():
                last["text"] = _join_lines([last["text"], p["text"]])
                last["lines"] += p["lines"]
                continue
        out.append(p)

    if not converted and (not title or re.fullmatch(r"[\w\-.]+", title)):
        big = _big_title(d)                    # Prefer the printed title over a file label.
        if big and (not title or " " in big):
            title = big
    if not title:
        first = next((p for p in out if p["kind"] in ("p", "h")), None)
        title = first["text"][:80] if first else Path(path).stem
    title = title.rstrip("*† ")

    # where every word sits on the page, so the PDF itself can be highlighted
    for p in out:
        lines = p.pop("lines", [])
        if p["kind"] == "skip":
            p["rects"] = [[r["page"], round(r["x0"], 1), round(r["y0"], 1), round(r["x1"], 1), round(r["y1"], 1)] for r in lines]
        else:
            p["boxes"] = _word_boxes(p["text"], lines, page_words)
    out = _place_figures(out, page_figs, page_sizes)
    if not converted:
        _link_figure_captions(out, _figure_captions(dicts), page_figs, page_words, page_sizes)
    return title, out, dict(pages=page_sizes, furn=furn)


def _norm(s):
    return "".join(c for c in unicodedata.normalize("NFKC", s).lower() if c.isalnum())


def _place_figures(paras, page_figs, page_sizes):
    """Pictures and diagrams become 'fig' paragraphs at their place in the reading order,
    so the text view shows them (they're never read aloud)."""
    figs = []
    for pg, rects in enumerate(page_figs):
        W, H = page_sizes[pg]
        # panels of one composite figure (a grid of images, a diagram's parts) become one figure
        page_rect = pymupdf.Rect(0, 0, W, H)
        rects = [pymupdf.Rect(r) & page_rect for r in rects if r.width * r.height < 0.85 * W * H]
        rects = [r for r in rects if not r.is_empty]
        merged = True
        while merged:
            merged = False
            for a in range(len(rects)):
                for b in range(a + 1, len(rects)):
                    ra, rb = rects[a], rects[b]
                    gap_x = max(rb.x0 - ra.x1, ra.x0 - rb.x1, 0)
                    gap_y = max(rb.y0 - ra.y1, ra.y0 - rb.y1, 0)
                    if gap_x < 26 and gap_y < 26:
                        rects[a] = ra | rb
                        del rects[b]
                        merged = True
                        break
                if merged:
                    break
        keep = []
        for r in sorted(rects, key=lambda r: -r.width * r.height):
            if r.width < 90 or r.height < 50 or r.width * r.height > 0.85 * W * H:
                continue
            if any((r & k).get_area() > 0.4 * min(r.get_area(), k.get_area()) for k in keep):
                continue
            keep.append(r)
        figs += [(pg, r) for r in keep]
    if not figs:
        return paras

    def where(p):
        if p.get("boxes"):
            b = p["boxes"][0]
            return b[2], b[4], b[3], b[5]
        if p.get("rects"):
            r = p["rects"][0]
            return r[0], r[2], r[1], r[3]
        return None
    pos = [where(p) for p in paras]
    inserts = {}
    for pg, r in figs:
        at = None
        # the first paragraph on that page, in the figure's column, that starts below the figure
        for i, w in enumerate(pos):
            if w is None or w[0] < pg:
                continue
            if w[0] > pg:
                at = i
                break
            _, y0, x0, x1 = w
            if y0 >= r.y1 - 4 and x0 < r.x1 and x1 > r.x0:
                at = i
                break
        if at is None:
            at = len(paras)
        W, H = page_sizes[pg]
        rr = (r + (-6, -14, 6, 10)) & pymupdf.Rect(0, 0, W, H)      # take in labels just above / below
        inserts.setdefault(at, []).append(dict(kind="fig", page=pg + 1, text="",
                                               fig=[pg, round(rr.x0, 1), round(rr.y0, 1), round(rr.x1, 1), round(rr.y1, 1)]))
    out = []
    for i, p in enumerate(paras):
        out += sorted(inserts.get(i, []), key=lambda f: (f["fig"][0], f["fig"][2]))
        out.append(p)
    out += inserts.get(len(paras), [])
    return out


def _figure_captions(page_dicts):
    """Use the original blocks, before reading order can merge a caption with another column."""
    captions = {}
    for pg, page in enumerate(page_dicts):
        for block in page["blocks"]:
            lines = block.get("lines")
            if not lines:
                continue
            spans = lines[0]["spans"]
            first = _clean_spaces("".join(s["text"] for s in spans))
            match = FIGURE_CAPTION.match(first)
            leading_span = next((s for s in spans if s["text"].strip()), None)
            if not match or not (match["separator"] or leading_span["flags"] & 16):
                continue
            text = _join_lines(["".join(s["text"] for s in line["spans"]) for line in lines])
            rect = pymupdf.Rect(block["bbox"])
            captions.setdefault(pg, []).append((rect, match[1].upper(), text[match.end():].strip()))
    return captions


def _link_figure_captions(paras, captions, page_figs, page_words, page_sizes):
    """Pair each numbered caption with an adjacent crop on the same page."""
    matches = []
    for i, p in enumerate(paras):
        if p["kind"] != "fig":
            continue
        pg, *bounds = p["fig"]
        crop = pymupdf.Rect(bounds)
        for j, (cap, _, _) in enumerate(captions.get(pg, [])):
            overlap = min(cap.x1, crop.x1) - max(cap.x0, crop.x0)
            above = (cap.y0 + cap.y1) < (crop.y0 + crop.y1)
            gap = crop.y0 - cap.y1 if above else cap.y0 - crop.y1
            if overlap >= 0.5 * min(cap.width, crop.width) and -min(44, crop.height * 0.3) <= gap <= 44:
                matches.append((abs(gap), i, pg, j, above))
    used = set()
    for _, i, pg, j, above in sorted(matches):
        p = paras[i]
        if "figure" in p or (pg, j) in used:
            continue
        used.add((pg, j))
        cap, number, caption = captions[pg][j]
        r = pymupdf.Rect(p["fig"][1:])
        if above:
            r.y0 = max(r.y0 - 8, cap.y1 + CROP_PADDING)
        else:
            r.y1 = min(r.y1 + 8, cap.y0 - CROP_PADDING)
        bounds = [round(v, 1) for v in r]
        pad = CROP_PADDING
        crop = (pymupdf.Rect(bounds) + (-pad, -pad, pad, pad)) & pymupdf.Rect(0, 0, *page_sizes[pg])
        p["figure"] = dict(number=number, caption=caption, crop=[pg, *bounds],
                           panels=_figure_panels(crop, page_figs[pg], page_words[pg]))


def _figure_panels(crop, regions, words):
    """Use distinct PDF components when possible, otherwise outline the label."""
    labels = {}
    for x0, y0, x1, y1, word, *_ in words:
        match = PANEL_LABEL.fullmatch(word)
        if match:
            r = pymupdf.Rect(x0, y0, x1, y1)
            if crop.contains(r):
                labels.setdefault(match[1].lower(), []).append(r)
    if not labels:
        return {}

    components = [r & crop for r in regions]
    components = [r for r in components if min(r.width, r.height) >= 30
                  and 0.06 <= r.get_area() / crop.get_area() <= 0.83]

    def near(component, label):
        x, y = (label.x0 + label.x1) / 2, (label.y0 + label.y1) / 2
        return component.x0 - 2 <= x <= component.x1 + 2 and component.y0 - 18 <= y <= component.y1 + 18

    panels = {}
    for letter, boxes in labels.items():
        if len(boxes) != 1:
            continue
        label = boxes[0]
        candidates = [r for r in components if near(r, label) and
                      not any(near(r, other) for key, markers in labels.items() if key != letter for other in markers)]
        r = (label | candidates[0]) if len(candidates) == 1 else label
        r = (r + (-2, -2, 2, 2)) & crop
        panels[letter] = [round((r.x0 - crop.x0) / crop.width, 5), round((r.y0 - crop.y0) / crop.height, 5),
                          round(r.width / crop.width, 5), round(r.height / crop.height, 5)]
    return panels


def _index_figure_references(paras):
    figures = {}
    for i, p in enumerate(paras):
        if "figure" in p:
            figures.setdefault(p["figure"]["number"], []).append(i)
    if not figures:
        return
    for i, p in enumerate(paras):
        if p["kind"] not in ("p", "h") or p.get("eq"):
            continue
        refs = []
        text = p["text"]
        for first in FIGURE_REFERENCE.finditer(text):
            plural = first["name"].strip().rstrip(".").lower().endswith("s")
            match = first
            while match:
                targets = figures.get(match["number"].upper())
                if targets:
                    j = min(targets, key=lambda j: (abs(paras[j]["page"] - p["page"]), abs(j - i)))
                    panel = (match["panel"] or "").strip().strip("()").lower()
                    refs.append([match.start() if match is first else match.start("number"), match.end(), j, panel])
                match = FIGURE_LIST_ITEM.match(text, match.end()) if plural else None
        if refs:
            p["figure_refs"] = refs


def _word_boxes(text, lines, page_words):
    """Align the paragraph's words with the PDF's words -> [start, end, page, x0, y0, x1, y1]."""
    # each PDF word goes to the line whose vertical centre is nearest (lines with
    # sub/superscripts have tall boxes that overlap their neighbours)
    rows = [[] for _ in lines]
    for pg in sorted({ln["page"] for ln in lines}):
        idxs = [k for k, ln in enumerate(lines) if ln["page"] == pg]
        for w in page_words[pg]:
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            best, bd = None, 1e9
            for k in idxs:
                ln = lines[k]
                if ln["x0"] - 2 <= cx <= ln["x1"] + 2 and ln["y0"] - 1.5 <= cy <= ln["y1"] + 1.5:
                    dist = abs(cy - (ln["y0"] + ln["y1"]) / 2)
                    if dist < bd:
                        best, bd = k, dist
            if best is not None:
                rows[best].append(w)
    cand = []
    for k, row in enumerate(rows):
        row.sort(key=lambda w: w[0])
        cand += [(lines[k]["page"], w) for w in row]
    A, Aw = [], []
    for j, (_, w) in enumerate(cand):
        for c in _norm(w[4]):
            A.append(c); Aw.append(j)
    toks = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    B, Bt = [], []
    for t, (s, e) in enumerate(toks):
        for c in _norm(text[s:e]):
            B.append(c); Bt.append(t)
    A, B = "".join(A), "".join(B)
    match = {}
    i = j = 0
    K = 4
    while i < len(B) and j < len(A):
        if B[i] == A[j]:
            lst = match.setdefault(Bt[i], [])
            if not lst or lst[-1] != Aw[j]:
                lst.append(Aw[j])
            i += 1; j += 1
            continue
        dj = next((d for d in range(1, 400) if j + d < len(A) and A[j + d:j + d + K] == B[i:i + K]), None)
        di = next((d for d in range(1, 400) if i + d < len(B) and B[i + d:i + d + K] == A[j:j + K]), None)
        if dj is not None and (di is None or dj <= di):
            j += dj
        elif di is not None:
            i += di
        else:
            i += 1; j += 1
    boxes = []
    for t, js in sorted(match.items()):
        s, e = toks[t]
        for jj in js:
            pg, w = cand[jj]
            boxes.append([s, e, pg, round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[3], 1)])
    return boxes


def load_document(path, skip_citations=True, cache_dir=None, progress=None, lexicon=None):
    from .convert import to_pdf
    from .paths import CACHE
    path = Path(path)
    cache_dir = cache_dir or CACHE / "converted"
    pdf, conv_title = to_pdf(path, cache_dir, progress)
    title, paras, layout = extract_pdf(pdf, converted=path.suffix.lower() != ".pdf")
    if path.suffix.lower() == ".pdf" and pdf != path and (not title or title == Path(pdf).stem):
        title = path.stem
    if conv_title:
        title = conv_title
    elif pdf != path and path.suffix.lower() != ".pdf":
        title = re.sub(r"^\d{4}-\d\d-\d\d \d\d\.\d\d\.\d\d ", "", path.stem)   # pasted-text files
    layout["pdf"] = str(pdf)
    if path.suffix.lower() == ".pdf":
        try:                                   # display equations: read aloud (filled in lazily)
            from . import pdfmath
            pdfmath.inline_math(str(pdf), paras)
            if pdfmath.available():
                paras = pdfmath.attach_equations(str(pdf), paras)
        except Exception:
            import traceback
            traceback.print_exc()
    lang = detect_lang(" ".join(p["text"] for p in paras if p["kind"] == "p")[:20000])
    inline = [m[2] for p in paras for m in p.get("math", [])]
    if inline:
        try:                                   # one batch for every inline formula (fills the cache)
            from .mathspeech import speak_latex
            speak_latex(sorted(set(inline)), lang)
        except Exception:
            pass
    sections = []
    for i, p in enumerate(paras):
        p["id"] = i
        if p["kind"] == "h":
            sections.append(dict(title=p["text"], level=p.get("level", 1), para=i))
        if p.get("eq"):
            p["spans"] = [[0, len(p["text"]), "…"]]        # placeholder: words come from pdfmath.resolve()
        elif p["kind"] in ("p", "h"):
            p["spans"] = speech_spans(p["text"], skip_citations, lang, lexicon, math=p.pop("math", None))
            _prosody(p)
    units = []
    for i, p in enumerate(paras):
        if p["kind"] in ("p", "h"):
            pieces = split_units(p["text"], p["spans"]) if p["kind"] == "p" and not p.get("eq") else [(0, len(p["text"]))]
            p["units"] = []
            for k, (s, e) in enumerate(pieces):
                p["units"].append(len(units))
                units.append(dict(p=i, s=s, e=e, last=k == len(pieces) - 1))
    _index_figure_references(paras)
    return dict(id=doc_id(path), path=str(path), title=title, sections=sections, paras=paras, units=units,
                lang=lang, **layout)


STOP = {
    "en": "the of and to in is that for it with as was on are by this be from or which an".split(),
    "es": "el la de que y en los del se las por un para con una es al lo como más pero sus".split(),
    "fr": "le la les de des et est un une du que dans pour qui pas au sur se plus par ne".split(),
    "it": "il di che la per un è non una sono del della con gli le si da al nel anche".split(),
    "pt": "o a de que e do da em um para é com não uma os no se na por mais as dos".split(),
    "de": "der die und den das ist nicht mit sich des auf für im dem ein eine auch als zu".split(),
}


def detect_lang(text):
    words = re.findall(r"[a-zà-ÿ]+", text.lower())[:6000]
    if len(words) < 20:
        return "en"
    counts = Counter(words)
    score = {lang: sum(counts[w] for w in ws) / len(words) for lang, ws in STOP.items()}
    best = max(score, key=score.get)
    return best if score[best] > 0.08 else "en"


HEAD_NUM = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.\d+(?:\.\d+)*\.?|[IVX]+\.)\s+(?=\S)")


def _prosody(p):
    """Small edits that give the voice the right intonation."""
    text, spans = p["text"], p["spans"]
    if p["kind"] == "h":
        m = HEAD_NUM.match(text)
        if m and not any(a < m.end() and b > 0 for a, b, _ in spans):
            spans.insert(0, [0, m.end(), None])          # "3.1 Method" -> "Method"
    t = text.rstrip()
    covered = any(a < len(t) <= b for a, b, _ in spans)
    if t and not covered and not re.search(r"[.!?:;…”’\"')\]]$", t):
        spans.append([len(t), len(t), "."])              # falling tone at the end
    spans.sort()


def spoken_text(text, spans):
    """Build the spoken string and a list of pieces (spoken_start, spoken_end, raw_start, raw_end)."""
    out, pieces, pos = [], [], 0
    cur = 0

    def emit(s, raw_s, raw_e):
        nonlocal cur
        if not s:
            return
        out.append(s)
        pieces.append((cur, cur + len(s), raw_s, raw_e))
        cur += len(s)

    for s, e, val in spans:
        if s > pos:
            emit(text[pos:s], pos, s)
        if val is not None:
            emit(val, s, e)
        pos = e
    if pos < len(text):
        emit(text[pos:], pos, len(text))
    spoken = "".join(out)
    return spoken, pieces


SENT_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bal)(?<!\bU\.S)(?<!\bvs)(?<!\bDr)(?<!\bMr)(?<!\bSt)(?<!\bNo)(?<!\bFig)[.?!][”’)\]]*\s+(?=[“‘(\[]?[A-Z0-9])")


def split_units(text, spans, first_limit=170, limit=330):
    """Split a paragraph into runs of whole sentences so synthesis can start quickly."""
    blocked = [False] * (len(text) + 1)
    for s, e, _ in spans:
        for i in range(s, min(e + 1, len(blocked))):
            blocked[i] = True
    cuts = [m.end() for m in SENT_END.finditer(text) if not blocked[m.start()] and not blocked[m.end() - 1]]
    units, start, last = [], 0, 0
    for c in cuts + [len(text)]:
        lim = first_limit if not units else limit
        if c - start > lim and last > start:
            units.append((start, last))
            start = last
        last = c
    if start < len(text):
        units.append((start, len(text)))
    # avoid a tiny trailing unit
    if len(units) > 1 and units[-1][1] - units[-1][0] < 60:
        units[-2] = (units[-2][0], units[-1][1])
        units.pop()
    return units


def unit_text(doc, u):
    unit = doc["units"][u]
    p = doc["paras"][unit["p"]]
    s, e = unit["s"], unit["e"]
    spans = [[max(a, s) - s, min(b, e) - s, v] for a, b, v in p.get("spans", []) if a < e and b > s]
    return p["text"][s:e], spans


if __name__ == "__main__":
    import sys
    doc = load_document(sys.argv[1])
    print(doc["title"], len(doc["paras"]), "paras")
    for s in doc["sections"][:60]:
        print("  " * s["level"], s["title"], "->", s["para"])
    for p in doc["paras"][: int(sys.argv[2]) if len(sys.argv) > 2 else 40]:
        sp = spoken_text(p["text"], p.get("spans", []))[0] if p["kind"] != "skip" else ""
        print(f"[{p['kind']} p{p['page']}] {p['text'][:300]}")
        if sp and sp != p["text"]:
            print(f"   SPOKEN: {sp[:300]}")
