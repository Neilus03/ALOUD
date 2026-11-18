"""Read a web page (blog post, news article) like a reader view does: download the
HTML, keep the article (title, headings, paragraphs, lists, quotes, tables) and drop
menus, ads, cookie banners, share buttons and comment sections.

Extraction uses trafilatura, the open-source boilerplate remover used in web-corpus
research. Only what the site sends to any visitor is read; pages behind a login or
paywall come back as the teaser, and we say so instead of reading half an article.
"""
import html as htmlmod
import re
from pathlib import Path
from urllib.parse import urlparse

URL_RE = re.compile(r"^\s*(https?://|www\.)\S+\s*$", re.I)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")

PAYWALL_HINTS = re.compile(
    r"subscribe to (continue|read)|to continue reading|already a subscriber|"
    r"this (article|story|content) is (for|available to) (subscribers|members)|"
    r"sign in to (continue|read)|create a free account to (continue|read)|"
    r"you have reached your (free )?article limit|member-only story", re.I)


BOILER = re.compile(
    r"(follow (topics|us|authors)|sign up (for|to)|subscribe (to|for)|share (this|on)|"
    r"advertisement|most popular|recommended for you|related:|read more:|"
    r"(click|tap) here to|newsletter|supported by|related content|we acknowledge mistakes)\b", re.I)


def is_url(text):
    return bool(URL_RE.match(text or ""))


class PreviewOnly(Exception):
    def __init__(self, msg, signin=None):
        super().__init__(msg)
        self.signin = signin          # a page to open for signing in, when that could help


def _locked(page, words):
    return bool((words < 400 and PAYWALL_HINTS.search(page))
                or (re.search(r'"isAccessibleForFree"\s*:\s*"?(false|False)', page) and words < 700))


def math_to_tex(page):
    """Formulas on web pages (Wikipedia, KaTeX, MathJax) -> their LaTeX source between \\( \\),
    so the article extractor keeps them and ALOUD reads them as math."""
    if not re.search(r"katex|MathJax|math/tex|<math|mwe-math", page):
        return page
    from bs4 import BeautifulSoup, NavigableString
    soup = BeautifulSoup(page, "lxml")

    def put(el, tex, display):
        tex = re.sub(r"^\{\\displaystyle\s*(.*)\}$", r"\1", " ".join(tex.split()), flags=re.S)
        el.replace_with(NavigableString((" \\[ " if display else "\\(") + tex + (" \\] " if display else "\\)")))
    for el in soup.select(".mwe-math-element"):                       # Wikipedia
        m = el.find("math")
        alt = (m.get("alttext") if m else None) or (el.find("img") or {}).get("alt")
        if alt:
            put(el, alt, "display" in " ".join(el.get("class", [])) or bool(m and m.get("display") == "block"))
    for el in soup.select(".katex-display, .katex"):                  # KaTeX
        if el.parent is None:
            continue
        ann = el.find("annotation", attrs={"encoding": "application/x-tex"})
        if ann:
            put(el, ann.get_text(), "katex-display" in el.get("class", []))
    for sc in soup.find_all("script", attrs={"type": re.compile(r"math/tex")}):   # MathJax 2
        prev = sc.find_previous_sibling()
        if prev is not None and re.search(r"MathJax", " ".join(prev.get("class", [])) + (prev.get("id") or "")):
            prev.decompose()
        put(sc, sc.get_text(), "display" in sc.get("type", ""))
    for el in soup.select(".MathJax_Preview, .MathJax_Display, .MathJax, mjx-container"):
        el.decompose()
    import json as _json
    for m in soup.find_all("math"):                                   # MathML that carries its TeX source
        if m.parent is None:
            continue
        tex = m.get("alttext")
        ann = m.find("annotation", attrs={"encoding": re.compile(r"tex", re.I)})
        if not tex and ann:
            tex = ann.get_text()
        if not tex and m.get("data-mw"):                              # Wikipedia's own markup
            try:
                tex = _json.loads(m["data-mw"]).get("body", {}).get("extsrc")
            except ValueError:
                tex = None
        if tex:
            put(m, tex, m.get("display") == "block" or "block" in " ".join(m.get("class", [])))
    return str(soup)


def _extract(page, url):
    import trafilatura
    page = math_to_tex(page)
    meta = trafilatura.extract_metadata(page, default_url=url)
    body = trafilatura.extract(
        page, url=url, output_format="html", favor_recall=True,
        include_comments=False, include_tables=True, include_images=True,
        include_links=False, include_formatting=True, deduplicate=True)
    text = trafilatura.extract(page, url=url, favor_recall=True, include_comments=False) or ""
    return meta, body, len(text.split())


def fetch_article(url, timeout=20, render=None):
    """Return (title, html) for the article at url. `render(url) -> (html, final_url)`
    loads the page in a real (hidden) browser view; it's used when a plain download
    gets refused or the article is built by JavaScript."""
    import requests

    url = url.strip()
    if url.lower().startswith("www."):
        url = "https://" + url
    page, final, blocked = None, url, False
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"})
        if r.status_code in (401, 402, 403, 429):
            blocked = True
        else:
            r.raise_for_status()
            if "pdf" in r.headers.get("content-type", ""):
                return None, r.content          # a link straight to a PDF
            page, final = r.text, r.url
    except requests.RequestException:
        if render is None:
            raise
        blocked = True
    meta, body, words = _extract(page, final) if page else (None, None, 0)
    # a teaser behind a paywall: if you've signed in to this site inside ALOUD,
    # its browser view has your subscription and loads the whole article
    if (not body or words < 150 or (page and _locked(page, words))) and render is not None:
        try:
            page2, final2 = render(url)
        except Exception:
            page2 = None
        if page2:
            m2, b2, w2 = _extract(page2, final2 or url)
            if b2 and w2 > words:
                page, final, meta, body, words = page2, final2 or url, m2, b2, w2
    fetch_article.source = (page, final)      # the page itself, for showing it as it is
    if not body or words < 40:
        raise PreviewOnly("That site blocks downloads by apps (or needs you to be signed in)." if blocked and not body
                          else "Couldn't find an article on that page.", signin=final if blocked else None)
    return _assemble(page, final, meta, body, words)


def article_from_html(page, url):
    """Reader view of a page sent from your own browser (it already has your sign-ins)."""
    meta, body, words = _extract(page, url)
    if not body or words < 40:
        raise PreviewOnly("Couldn't find an article on that page.")
    return _assemble(page, url, meta, body, words, check_paywall=False)


def _assemble(page, final, meta, body, words, check_paywall=True):
    title = (meta.title if meta and meta.title else None) or urlparse(final).netloc
    if check_paywall and _locked(page, words):
        # publishers mark paywalled articles for search engines ("isAccessibleForFree": false)
        raise PreviewOnly(f"Only a preview of “{title}” loaded ({words} words); the rest is for subscribers.",
                          signin=final)

    body = re.sub(r"^\s*<html>\s*<body>|</body>\s*</html>\s*$", "", body.strip())
    body = re.sub(r"</?main[^>]*>|</?doc[^>]*>", "", body)
    site = meta.sitename if meta and meta.sitename else ""
    if site:   # "Speech synthesis - Wikipedia" -> "Speech synthesis"
        title = re.sub(r"\s+[-|–—·:]\s+" + re.escape(site) + r"\s*$", "", title, flags=re.I) or title
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    if h1:   # the page's own headline is usually cleaner than <title> ("X - Wikipedia")
        head = " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", "", h1.group(1))).split())
        if 3 < len(head) <= 200 and title.lower().startswith(head.lower()):
            title = head
    # trafilatura repeats the title as the first heading on many sites
    first = re.match(r"\s*<h[1-3][^>]*>(.*?)</h[1-3]>", body, re.S)
    if first and re.sub(r"\W+", "", htmlmod.unescape(re.sub(r"<[^>]+>", "", first.group(1)))).lower() \
            == re.sub(r"\W+", "", title).lower():
        body = body[first.end():]
    author = meta.author if meta and meta.author and len(meta.author) <= 60 else None
    body = re.sub(r"\[\s*edit(\s*\|\s*edit source)?\s*\]", "", body)          # Wikipedia section links
    # stop at the back matter a reader view leaves out: references, related posts, comments
    end = re.search(r"<h[1-6][^>]*>\s*(references|notes|citations|footnotes|sources|bibliography|"
                    r"external links|further reading|see also|related( posts| articles| stories| content)?|"
                    r"(more )?recent (posts|articles)|more from .{0,40}|you (may|might) also like|"
                    r"read (more|next)|comments?|leave a (reply|comment)|share this( post| article)?)"
                    r"\s*:?\s*</h[1-6]>", body, re.I)
    if end and end.start() > len(body) * 0.3:
        body = body[:end.start()]
    body = re.sub(r"<sup[^>]*>\s*\[?[\w\s,–-]{1,12}\]?\s*</sup>", "", body)   # footnote markers
    byline = " · ".join(x for x in [
        author,
        meta.sitename if meta and meta.sitename else urlparse(final).netloc.removeprefix("www."),
        meta.date if meta else None] if x)
    # the headline repeated inside the article, and newsletter / follow boilerplate
    nt = re.sub(r"\W+", "", title).lower()
    body = re.sub(r"<(p|h[1-6])[^>]*>(.*?)</\1>",
                  lambda m: "" if re.sub(r"\W+", "", htmlmod.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).lower() == nt
                  or BOILER.match(htmlmod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()) else m.group(0),
                  body, flags=re.S)
    esc = htmlmod.escape
    hero = meta.image if meta and getattr(meta, "image", None) else None
    if hero and hero.startswith("http") and "<img" not in body[:3000] and hero.split("?")[0] not in body:
        body = f'<figure class="hero"><img src="{esc(hero)}" alt=""></figure>' + body   # the article's lead picture
    html = (f"<html><head><title>{esc(title)}</title></head><body><h1>{esc(title)}</h1>"
            f"<p><small>{esc(byline)}</small></p>{body}</body></html>")   # shown, not read
    return title, html



def localize_images(html, folder, base_url=""):
    """Download the article's pictures next to it (the reader works offline and lays them out
    itself). Tiny images (icons, avatars, tracking pixels) and failures are dropped."""
    import io
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urljoin
    import requests
    from PIL import Image
    tags = list(re.finditer(r"<img\b[^>]*>", html, re.I))
    if not tags:
        return html
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    def src_of(tag):
        m = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag) or re.search(r'\bdata-src\s*=\s*["\']([^"\']+)["\']', tag)
        return urljoin(base_url, htmlmod.unescape(m.group(1))) if m else None

    def fetch(job):
        k, url = job
        if not url or not url.startswith("http"):
            return k, None
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": UA, "Referer": base_url or url})
            r.raise_for_status()
            if len(r.content) > 15_000_000:
                return k, None
            im = Image.open(io.BytesIO(r.content))
            im.load()
            if im.width < 120 or im.height < 60:
                return k, None                      # icon, avatar or tracking pixel
            if im.width > 1600:
                im = im.resize((1600, round(im.height * 1600 / im.width)))
            alpha = im.mode in ("RGBA", "LA", "P") and "transparency" in im.info or im.mode in ("RGBA", "LA")
            name = f"img{k:03d}." + ("png" if alpha else "jpg")
            if alpha:
                im.convert("RGBA").save(folder / name)
            else:
                im.convert("RGB").save(folder / name, quality=86)
            return k, (name, im.width, im.height)
        except Exception:
            return k, None
    jobs = [(k, src_of(m.group(0))) for k, m in enumerate(tags[:60])]
    with ThreadPoolExecutor(6) as pool:
        got = dict(pool.map(fetch, jobs))
    out, last = [], 0
    for k, m in enumerate(tags):
        out.append(html[last:m.start()])
        res = got.get(k)
        if res:
            name, w, h = res
            width_pt = min(468, round(w * 0.75))             # never blown up past its natural size
            out.append(f'<img src="{folder.name}/{name}" width="{width_pt}">')
        last = m.end()
    out.append(html[last:])
    html = "".join(out)
    html = re.sub(r"<figure[^>]*>\s*</figure>", "", html)       # figures whose picture failed
    return html



OVERLAY_CSS = """
html, body { overflow: auto !important; height: auto !important; position: static !important; }
[id*="cookie" i], [class*="cookie-banner" i], [class*="cookieconsent" i], [id*="consent" i], [class*="consent-banner" i],
[class*="newsletter-popup" i], [class*="modal-backdrop" i], [id*="onetrust" i], [class*="onetrust" i], #CybotCookiebotDialog { display: none !important; }
::highlight(nr-unit) { background-color: rgba(10,132,255,.14); }
::highlight(nr-word) { background-color: #ffd970; color: #1d1d1f; }
::highlight(nr-note) { background-color: rgba(242,183,5,.22); }
"""


def clean_snapshot(html, url, local=""):
    """The page as it looked, made safe to show inside ALOUD: its own scripts and inline
    handlers removed (nothing re-renders or pops up), lazy images given their real address,
    links resolved against the original site. Embedded videos keep working."""
    from bs4 import BeautifulSoup, NavigableString
    soup = BeautifulSoup(html, "lxml")
    has_math = bool(re.search(r"mathjax|katex|math/tex", html, re.I))
    for sc in soup.find_all("script", attrs={"type": re.compile(r"math/tex", re.I)}):   # MathJax 2 keeps TeX in scripts
        disp = "display" in sc.get("type", "")
        sc.replace_with(NavigableString(("\\[" if disp else "\\(") + sc.get_text() + ("\\]" if disp else "\\)")))
    for el in soup.select(".MathJax_Preview, .MathJax, .MathJax_Display, mjx-assistive-mml"):
        el.decompose()
    for t in soup.find_all(["script", "link"]):
        if t.name == "script" or (t.get("rel") and {"preload", "modulepreload", "prefetch"} & set(t.get("rel"))
                                  and t.get("as") == "script"):
            t.decompose()
    for m in soup.find_all("meta"):
        if (m.get("http-equiv") or "").lower() in ("content-security-policy", "refresh", "x-frame-options"):
            m.decompose()
    for t in soup.find_all(True):
        for a in list(t.attrs):
            if a.lower().startswith("on"):
                del t.attrs[a]
        if t.name == "a" and str(t.get("href", "")).lower().startswith("javascript:"):
            del t.attrs["href"]
    for img in soup.find_all(["img", "source"]):
        for k in ("data-src", "data-lazy-src", "data-original", "data-url", "data-hi-res-src"):
            if img.get(k) and (not img.get("src") or img["src"].startswith("data:") or "placeholder" in img.get("src", "")):
                img["src"] = img[k]
        for k in ("data-srcset", "data-lazy-srcset"):
            if img.get(k):
                img["srcset"] = img[k]
        if img.name == "img":
            img.attrs.pop("loading", None)
    for ns in soup.find_all("noscript"):            # picture fallbacks meant for "no JavaScript": that's us
        if ns.find("img") or "<img" in ns.get_text():
            inner = BeautifulSoup(ns.decode_contents() if ns.find("img") else ns.get_text(), "lxml")
            imgs = inner.find_all("img")
            prev = ns.find_previous_sibling("img")
            if imgs and (prev is None or not prev.get("src") or prev["src"].startswith("data:")):
                if prev is not None:
                    prev.decompose()
                ns.replace_with(*imgs)
                continue
        ns.decompose()
    head = soup.head or soup.new_tag("head")
    if soup.head is None:
        (soup.html or soup).insert(0, head)
    for b in head.find_all("base"):
        b.decompose()
    head.insert(0, soup.new_tag("base", href=url))
    head.insert(1, soup.new_tag("meta", attrs={"name": "referrer", "content": "no-referrer"}))
    st = soup.new_tag("style", id="aloud-style")
    st.string = OVERLAY_CSS
    head.append(st)
    if has_math and local:     # the site typeset its formulas with a script: do it with ALOUD's own copy of KaTeX
        head.append(soup.new_tag("link", rel="stylesheet", href=f"{local}/vendor/katex/katex.min.css"))
        for f in ("katex.min.js", "auto-render.min.js"):
            head.append(soup.new_tag("script", src=f"{local}/vendor/katex/{f}"))
        run = soup.new_tag("script")
        run.string = ('document.addEventListener("DOMContentLoaded",function(){try{renderMathInElement(document.body,{delimiters:['
                      '{left:"$$",right:"$$",display:true},{left:"\\\\[",right:"\\\\]",display:true},'
                      '{left:"\\\\(",right:"\\\\)",display:false},{left:"$",right:"$",display:false}],'
                      'throwOnError:false,ignoredTags:["script","noscript","style","textarea","pre","code"]})}catch(e){}});')
        head.append(run)
    return str(soup)
