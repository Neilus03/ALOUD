"""Reference lists: split a paper's bibliography into entries and pull out, for each,
its title, first author, year and the best link (alphaXiv for arXiv papers, the DOI,
a URL in the entry, else a Google Scholar search). Then resolve in-text citations
("[26]", "[3, 7]", "(Smith et al., 2020)", "Smith and Jones (2019)") to entries.
"""
import re
from urllib.parse import quote_plus

import pymupdf

HEAD = re.compile(r"^\s*(\d+\.?\s+|[A-Z]\.\s+)?(references|bibliography|works cited|literature cited|literature)\s*$", re.I)
NUM = re.compile(r"^\s*\[(\d{1,3}|[A-Z][A-Za-z+]{0,7}\d{2}[a-z]?)\]\s*")
NUM2 = re.compile(r"^\s*(\d{1,3})\.\s+(?=[A-Z])")
ARXIV = re.compile(r"(?:arxiv[:\s/]*(?:abs/)?|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(v\d+)?", re.I)
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s,;\]]+[^\s,;.\]])", re.I)
URL = re.compile(r"https?://[^\s\]]+[^\s.,;\])]")
YEAR = re.compile(r"\b(19\d\d|20[0-4]\d)[a-z]?\b(?!\.\d)")
OLD_YEAR = re.compile(r"\b(1[6-8]\d\d)[a-z]?\b(?!\.\d)")      # Brandeis 1914 is caught above; 1600s–1800s classics


def _page_lines(page):
    """Lines rebuilt from words on a shared baseline (PDFs often split justified lines)."""
    W = page.rect.width
    words = page.get_text("words")
    if not words:
        return []
    # two columns: hardly any word sits across the middle of the page
    two_col = sum(1 for w in words if w[0] < W * 0.5 < w[2] and not re.fullmatch(r"\d{1,4}", w[4])) < 0.01 * len(words) and \
        sum(1 for w in words if w[0] > W * 0.52) > 0.25 * len(words)
    rows = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        cy = (w[1] + w[3]) / 2
        col = (0 if w[2] < W * 0.52 else 1) if two_col else 0
        for r in rows[-12:]:
            if r["col"] == col and abs(r["cy"] - cy) < 2.5:
                r["w"].append(w)
                break
        else:
            rows.append(dict(col=col, cy=cy, w=[w]))
    out = []
    for r in rows:
        ws = sorted(r["w"], key=lambda w: w[0])
        # margin line numbers of review copies: a lone 3-4 digit number at an edge or between columns
        ws = [w for w in ws if not (re.fullmatch(r"\d{3,4}", w[4]) and (w[0] < W * 0.1 or w[2] > W * 0.9
                                                                       or abs((w[0] + w[2]) / 2 - W / 2) < W * 0.05))]
        if ws:
            out.append((r["col"], r["cy"], ws[0][0], " ".join(w[4] for w in ws), ws[0][3] - ws[0][1]))
    return out


def _heads(doc):
    """Every "References" heading (a paper can have one for the main text and one for an appendix)."""
    out = []
    for pno in range(len(doc)):
        for col, cy, x, t, h in _page_lines(doc[pno]):
            if HEAD.match(t) and len(t) < 40:
                out.append((pno, cy, col))
    return out


def _lines_after_heading(doc, start=None):
    """Text lines of a reference section, in reading order, with their left edge."""
    if start is None:
        hs = _heads(doc)
        start = hs[-1] if hs else None
    if not start:
        return []
    out = []
    for pno in range(start[0], len(doc)):
        page = doc[pno]
        H = page.rect.height
        # on the heading's page: skip what comes before it (earlier columns, or above it in its column)
        lines = [l for l in _page_lines(page) if not (pno == start[0] and (l[0] < start[2] or (l[0] == start[2] and l[1] <= start[1] + 1)))]
        lines = [l for l in lines if not ((l[1] < 0.06 * H or l[1] > 0.95 * H) and re.fullmatch(r"[\divxlc]{1,5}", l[3]))]
        lines.sort(key=lambda l: (l[0], l[1], l[2]))
        for col, cy, x, t, h in lines:
            # the next section after the references (an appendix) ends the list
            if out and len(t) < 70 and (re.match(r"^(appendix|supplementary)", t, re.I)
                                        or (re.match(r"^[A-Z](\.\d+)*\s+[A-Z][a-z]+(\s+[\w-]+){0,6}$", t) and not t.endswith("."))):
                return out
            out.append((col, x, t))
    return out


def parse(pdf_path, max_entries=600):
    """Reference entries: [{n, text, title, author, year, link}]."""
    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return []
    out, seen = [], set()
    for start in _heads(doc)[-4:]:
        for e in _parse_lines(_lines_after_heading(doc, start), max_entries):
            k = e["text"][:120]
            if k not in seen:
                seen.add(k); out.append(e)
    return out[:max_entries]


def _parse_lines(lines, max_entries):
    if not lines:
        return []
    entries = []
    numbered = sum(1 for _, _, t in lines[:40] if NUM.match(t) or NUM2.match(t)) >= 3
    if numbered:
        cur = None
        for _, _, t in lines:
            m = NUM.match(t) or NUM2.match(t)
            if m:
                if cur:
                    entries.append(cur)
                g = m.group(1)
                cur = dict(n=int(g) if g.isdigit() else None, label=None if g.isdigit() else g, text=t[m.end():])
            elif cur:
                cur["text"] = _join(cur["text"], t)
        if cur:
            entries.append(cur)
    else:
        # author-year: a new entry starts at the column's left edge; continuation lines are indented
        lefts = {}
        for col, x, t in lines:
            lefts[col] = min(lefts.get(col, x), x)
        cur = None
        for col, x, t in lines:
            starts = x <= lefts[col] + 2.5 and re.match(r"^[A-ZÀ-ſ][\wÀ-ſ'’\-]+,?\s", t)
            if starts or cur is None:
                if cur:
                    entries.append(cur)
                cur = dict(n=None, text=t)
            else:
                cur["text"] = _join(cur["text"], t)
        if cur:
            entries.append(cur)
    out = []
    for e in entries[:max_entries]:
        e["text"] = " ".join(e["text"].split())
        e.update(_fields(e["text"]))
        out.append(e)
    return out


def _join(a, b):
    if a.endswith("-") and b[:1].islower():
        return a[:-1] + b
    return a + " " + b


def _fields(t):
    year = YEAR.search(t) or OLD_YEAR.search(t)
    title = None
    q = re.search(r"[“\"]([^”\"]{8,300})[”\"]", t)
    if q:
        title = q.group(1)
    if not title:
        m = re.search(r"\(\s*((?:19|20)\d{2}[a-z]?)\s*\)\s*[.,:]?\s*(.+?)(?:\.\s|\?\s|$)", t)     # APA / econ: "(2020). Title."
        if m:
            title = m.group(2)
    if not title:
        m = re.search(r"\b((?:19|20)\d{2}[a-z]?)\.\s+(.+?)(?:\.\s|\?\s|$)", t)                      # "Smith, J. 2020. Title."
        if m and m.start() < 160:
            title = m.group(2)
    if not title:
        m = re.match(r"^[A-ZÀ-ſ][\w'’\-]+,\s*(?:[A-Z]\.\s*)+,\s*(.+?)(?:[:.]\s|,\s|$)", t)      # "Brandeis, L.D., Book title: …"
        if m and len(m.group(1)) > 8:
            title = m.group(1)
    if not title:
        # CS style: "A. Author, B. Author, and C. Author. Title of paper. In Venue, 2020."
        parts = re.split(r"(?<=[a-zà-ſ\)])\.\s+(?=[A-Z0-9“\"])", t)
        if len(parts) >= 2:
            title = parts[1]
    title = (title or t[:120])
    title = re.split(r"\.\s+(?=arXiv|In\s|CoRR|Proc|Advances|Journal|IEEE|ACM|Technical|Working|NBER|Preprint)", title)[0]
    title = title.strip(" .,;:")
    title = re.sub(r"^(in|in:)\s+", "", title, flags=re.I)
    # first author's surname
    first = re.split(r",|\band\b|&|\bet al|(?<=[a-z]{2})\.\s", t, maxsplit=1)[0].strip()
    toks = re.findall(r"[A-ZÀ-ſ][\wÀ-ſ'’\-]+", first)
    if re.match(r"^[A-ZÀ-ſ][\w'’\-]+,\s", t):           # "Surname, F." style
        author = re.match(r"^([A-ZÀ-ſ][\wÀ-ſ'’\-]+(?:\s+[a-z]+\s+[A-Z][\w'’\-]+)?)", t).group(1)
    else:
        author = toks[-1] if toks else first[:30]
    link = None
    a = ARXIV.search(t)
    if a:
        link = f"https://www.alphaxiv.org/abs/{a.group(1)}"
    else:
        d = DOI.search(t)
        if d:
            link = f"https://doi.org/{d.group(1)}"
        else:
            u = URL.search(t)
            link = u.group(0) if u else None
    scholar = "https://scholar.google.com/scholar?q=" + quote_plus(title)
    return dict(title=title, author=author, year=year.group(0) if year else None, link=link or scholar,
                scholar=scholar, arxiv=a.group(1) if a else None)


def resolve(entries, cite):
    """Entries that an in-text citation points to."""
    if not entries:
        return []
    nums = [int(x) for x in re.findall(r"\d{1,3}", cite)] if re.match(r"^\s*\[[\d,\s\-–]+\]\s*$", cite) else []
    if nums:
        rng = re.search(r"(\d+)\s*[-–]\s*(\d+)", cite)
        if rng:
            a, b = int(rng.group(1)), int(rng.group(2))
            nums = sorted(set(nums) | set(range(a, min(b, a + 20) + 1)))
        byn = {e["n"]: e for e in entries if e.get("n") is not None}
        return [byn[n] for n in nums if n in byn]
    labels = re.findall(r"[A-Z][A-Za-z+]{0,7}\d{2}[a-z]?", cite) if cite.strip().startswith("[") else []
    if labels:
        byl = {e.get("label"): e for e in entries if e.get("label")}
        hit = [byl[x] for x in labels if x in byl]
        if hit:
            return hit
    out = []
    # author-year: each "Name ... 2020" chunk (the year may be in parentheses)
    for m in re.finditer(r"([A-Z][\w'’\-]+)[^;]{0,60}?((?:19|20)\d{2}[a-z]?)", cite):
        name, yr = m.group(1), m.group(2)
        same_year = [e for e in entries if e.get("year") and e["year"][:4] == yr[:4]]
        hit = next((e for e in same_year if (e.get("author") or "").lower() == name.lower()), None) or \
            next((e for e in same_year if name.lower() in e["text"][:120].lower()), None)
        if hit and hit not in out:
            out.append(hit)
    return out
