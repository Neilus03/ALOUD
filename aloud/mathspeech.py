"""Speak mathematics the way a person reads it aloud.

LaTeX -> MathML (latex2mathml) -> words (Speech Rule Engine, the open-source engine
behind MathJax's and many screen readers' math speech, ClearSpeak style), followed by
a few fixes for notation common in papers (transpose, expectations, argmin, norms).

The engine is JavaScript; it runs inside macOS's own JavaScriptCore, on one private
thread, with no browser or Node.js needed.
"""
import json
import queue
import re
import threading
from pathlib import Path

VENDOR = Path(__file__).parent / "vendor" / "sre"
SRE_LOCALES = {"en", "de", "es", "fr", "it"}

_BOOT = r"""
var global = this, window = undefined;
var process = { env: { SRE_JSON_PATH: "/sre/" }, versions: {} };
var console = { log: function(){}, warn: function(){}, error: function(){}, info: function(){} };
var __timers = [];
function setTimeout(f, ms) { __timers.push(f); return __timers.length; }
function clearTimeout(id) {}
function __pump() { var n = 0; while (__timers.length && n < 10000) { var f = __timers.shift(); try { f(); } catch (e) {} n++; } }
var __modules = {}, __cache = {};
function __define(name, fn) { __modules[name] = fn; }
function require(name) {
  if (name.indexOf("./") === 0) name = "xmldom/" + name.slice(2).replace(/\.js$/, "");
  if (name === "@xmldom/xmldom") name = "xmldom/index";
  if (name === "process") return process;
  if (name === "commander" || name === "readline") return {};
  if (name === "fs") return { readFileSync: function(p) { return __readLocale(String(p)); },
                              readFile: function(p, o, cb) { var r; try { r = __readLocale(String(p)); } catch (e) { return cb(e); } cb(null, r); },
                              existsSync: function() { return true; } };
  if (__cache[name]) return __cache[name].exports;
  var m = { exports: {} }; __cache[name] = m;
  if (!__modules[name]) throw new Error("no module " + name);
  __modules[name](m, m.exports, require);
  return m.exports;
}
"""


class _Engine:
    """Speech Rule Engine on a private thread (a JSContext must stay on one thread)."""

    def __init__(self):
        self.q = queue.Queue()
        self.ready = threading.Event()
        self.error = None
        threading.Thread(target=self._run, daemon=True, name="sre").start()
        self.ready.wait(60)
        if self.error:
            raise RuntimeError(self.error)

    def _run(self):
        try:
            from JavaScriptCore import JSContext
            ctx = JSContext.alloc().init()
            ctx.evaluateScript_(_BOOT)
            maps = VENDOR / "mathmaps"
            locales = {f.name: f.read_text(encoding="utf-8") for f in maps.glob("*.json")}
            ctx.evaluateScript_("var __locales = " + json.dumps(locales) + ";\n"
                                "function __readLocale(p) { var b = String(p).split('/').pop(); "
                                "if (!(b in __locales)) throw new Error('no locale ' + b); return __locales[b]; }")
            for f in sorted((VENDOR / "xmldom").glob("*.js")):
                ctx.evaluateScript_(f"__define({json.dumps('xmldom/' + f.stem)}, function(module, exports, require) {{\n"
                                    + f.read_text(encoding="utf-8") + "\n});")
            ctx.evaluateScript_('__define("wicked-good-xpath", function(module, exports, require) {\n'
                                + (VENDOR / "wgxpath.install-node.js").read_text(encoding="utf-8") + "\n});")
            ctx.evaluateScript_("var module = { exports: {} }, exports = module.exports;\n"
                                + (VENDOR / "sre.js").read_text(encoding="utf-8")
                                + "\nvar SRE = module.exports;")
            exc = ctx.exception()
            if exc is not None:
                raise RuntimeError(str(exc))
            self.ctx = ctx
            self.setup = None
            self.ready.set()
        except Exception as e:     # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            self.ready.set()
            return
        while True:
            items, locale, style, out = self.q.get()
            try:
                out.put(self._speak(items, locale, style))
            except Exception as e:  # noqa: BLE001
                out.put(e)

    def _speak(self, mmls, locale, style):
        ctx = self.ctx
        if self.setup != (locale, style):
            ctx.evaluateScript_(
                "var __ok = null; SRE.setupEngine(" + json.dumps(dict(
                    locale=locale, domain=style.split(":", 1)[0] if style.startswith("mathspeak:")
                    else ("clearspeak" if locale == "en" else "mathspeak"),
                    style=style.split(":", 1)[1] if style.startswith("mathspeak:") else style,
                    modality="speech", markup="none")) + ").then(function(){ return SRE.engineReady(); })"
                ".then(function(){ __ok = true; }, function(e){ __ok = String(e); });")
            for _ in range(50):
                ctx.evaluateScript_("__pump()")
                if ctx.evaluateScript_("__ok").toObject() is not None:
                    break
            ok = ctx.evaluateScript_("__ok").toObject()
            if ok is not True:
                raise RuntimeError(f"math speech setup failed: {ok}")
            self.setup = (locale, style)
        ctx.setObject_forKeyedSubscript_(list(mmls), "__in")
        res = ctx.evaluateScript_(
            "__in.map(function(m){ try { return SRE.toSpeech(m); } catch (e) { return null; } })").toObject()
        return [None if r is None else str(r) for r in res]

    def speak(self, mmls, locale="en", style="default"):
        out = queue.Queue()
        self.q.put((mmls, locale, style, out))
        r = out.get(timeout=120)
        if isinstance(r, Exception):
            raise r
        return r


_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def engine():
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = _Engine()
    return _ENGINE


CLEARSPEAK = "VerticalLine_Given:TriangleSymbol_Delta:SetMemberSymbol_Element"

# function names written upright in LaTeX: how to say them
FUNCS = {
    "Var": "the variance of", "var": "the variance of", "Cov": "the covariance of", "cov": "the covariance of",
    "Corr": "the correlation of", "argmin": "arg min", "argmax": "arg max", "arg min": "arg min", "arg max": "arg max",
    "softmax": "soft max", "ReLU": "ReLU", "relu": "ReLU", "diag": "diag", "tr": "trace", "Tr": "trace",
    "rank": "rank", "sgn": "sign", "sign": "sign", "KL": "K L", "MSE": "M S E", "Pr": "Pr", "logit": "logit",
}


def _prep_latex(tex):
    t = tex.strip()
    t = re.sub(r"\\\\(?=[()\[\]])", r"\\", t)                 # "\\(" copied from chat apps
    t = re.sub(r"\\(displaystyle|textstyle|scriptstyle|limits|nolimits|left|right|big|Big|bigg|Bigg)[lr]?\b", "", t)
    t = re.sub(r"\\(,|;|:|!|quad|qquad)", " ", t)
    t = re.sub(r"\\(label|tag)\{[^{}]*\}", "", t)
    t = re.sub(r"\\(R|N|Z|Q|C)(?![A-Za-z])", r"\\mathbb{\1}", t)        # common shorthand macros (Wikipedia)
    t = re.sub(r"\\(reals|Reals)\b", r"\\mathbb{R}", t)
    t = re.sub(r"\\(naturals|integers)\b", lambda m: r"\mathbb{N}" if m.group(1) == "naturals" else r"\mathbb{Z}", t)
    t = re.sub(r"\\arg\s*\\?(min|max)", r"\\operatorname{arg\1}", t)
    t = re.sub(r"\\(small)?setminus\b|∖", r"\\operatorname{SETMINUS}", t)
    t = re.sub(r"\\(mathbb|mathds|mathrm|operatorname)\{E\}", r"\\operatorname{EXPECT}", t)
    t = re.sub(r"(?<![\\\w])E\s*(?=\[)", r"\\operatorname{EXPECT}", t)      # E[X]: an expectation
    def upright(m):            # \mathrm{where\;head}, \mathrm{h e a d} (spaced letters from OCR)
        body = re.sub(r"\\[,;:!]|~|\\q?quad", " ", m.group(2)).strip()
        if re.fullmatch(r"([A-Za-z] )+[A-Za-z]", body):
            body = body.replace(" ", "")
        if re.fullmatch(r"[A-Za-z]{2,}", body):
            return r"\operatorname{" + body + "}"
        if re.fullmatch(r"[A-Za-z]+( [A-Za-z]+)+", body):
            return r"\text{ " + body + " }"
        return m.group(0)
    t = re.sub(r"\\(operatorname\*?|mathrm|text|textrm|mathit|textit|mathsf)\{([^{}]+)\}", upright, t)
    t = re.sub(r"\^\s*\{?\s*\\(top|intercal|mathsf\{T\}|mathrm\{T\})\s*\}?", r"^{\\TRANSPOSE}", t)
    t = t.replace(r"\TRANSPOSE", r"\operatorname{TRANSPOSE}")
    t = re.sub(r"\^\s*\{\s*\(([^{}()]{1,14})\)\s*\}", r"^{\\operatorname{SUPIDX}\1}", t)   # x^{(i)}: an index, not a power
    # on a capital (a matrix), ^T is a transpose and another capital is a label: W^Q is "W super Q"
    t = re.sub(r"(?<![A-Za-z\\])([A-Z]\}?(?:_\{[^{}]*\}|_\w)?)\^\s*(?:\{\s*T\s*\}|T(?![A-Za-z]))", r"\1^{\\operatorname{TRANSPOSE}}", t)
    t = re.sub(r"(?<![A-Za-z\\])([A-Z]\}?(?:_\{[^{}]*\}|_\w)?)\^\s*(?:\{\s*([A-SU-Z])\s*\}|([A-SU-Z])(?![A-Za-z]))",
               lambda m: m.group(1) + r"^{\operatorname{SUPIDX}" + (m.group(2) or m.group(3)) + "}", t)
    t = re.sub(r"\\begin\{array\}\{[^{}]*\}|\\end\{array\}", "", t)
    t = re.sub(r"\\(begin|end)\{(equation|align|aligned|gather|gathered|multline|eqnarray|split)\*?\}", "", t)
    t = re.sub(r"D_\{\s*K\s*L\s*\}|D_\{\\mathrm\{KL\}\}|\\mathrm\{KL\}|D_\{\\operatorname\{KL\}\}", r"\\operatorname{KLDIV}", t)
    t = re.sub(r"\|\||\\\|(?=\s*[A-Za-z\\])", r"\\operatorname{DIVFROM}", t) if "KLDIV" in t else t
    t = t.replace("&", " ").replace(r"\\", " , ")
    return t


def _fix_mathml(mml):
    # the converter splits upright words into letters: <mi mathvariant="normal">s</mi><mi ...>o</mi> ...
    def merge(m):
        letters = re.findall(r">([A-Za-z])<", m.group(0))
        return f"<mi>{''.join(letters)}</mi>"
    mml = re.sub(r'(?:<mi mathvariant="normal">[A-Za-z]</mi>){2,}', merge, mml)
    mml = re.sub(r"<mo>([A-Za-z]{2,})</mo>", r"<mi>\1</mi>", mml)   # \operatorname{softmax} is a name, not an operator
    mml = mml.replace("&#x02206;", "&#x00394;")
    mml = _implied_times(mml)              # increment sign -> capital Delta
    # a known function followed by "(": mark function application so it is read "f of x"
    names = "|".join(sorted(map(re.escape, list(FUNCS) + ["EXPECT"]), key=len, reverse=True))
    mml = re.sub(rf"(<mi>(?:{names})</mi>)(\s*<mo[^>]*>(?:[(\[]|&#x00028;|&#x0005B;)</mo>)", r"\1<mo>&#x02061;</mo>\2", mml)
    return mml


def _implied_times(mml):
    """Adjacent single letters in a row multiply ("y_{ft}" is y sub f t, not "f of t")."""
    import xml.etree.ElementTree as ET
    ns = "http://www.w3.org/1998/Math/MathML"
    try:
        root = ET.fromstring(mml)
    except ET.ParseError:
        return mml
    rows = {f"{{{ns}}}{t}" for t in ("math", "mrow", "mstyle", "mtd")}
    for el in root.iter():
        if el.tag not in rows:
            continue
        kids = list(el)
        for k in range(len(kids) - 1, 0, -1):
            a, b = kids[k - 1], kids[k]
            if (a.tag == f"{{{ns}}}mi" and b.tag == f"{{{ns}}}mi" and len((a.text or "").strip()) == 1
                    and len((b.text or "").strip()) == 1):
                mo = ET.Element(f"{{{ns}}}mo")
                mo.text = "\u2062"
                el.insert(k, mo)
    ET.register_namespace("", ns)
    return ET.tostring(root, encoding="unicode")


_POST = [
    (r"(raised )?to the TRANSPOSE(-th)? power", "transpose"),
    (r"\bEXPECT of open bracket (.+?) close bracket", r"the expected value of \1,"),
    (r"\b(the )?probability of open paren (.+?) close paren", r"the probability that \2,"),
    (r"^(\w+) colon (.+?) right arrow (.+)$", r"\1, from \2 to \3"),
    (r"\barg (min|max) sub\b", r"arg \1 over"),
    (r"\b([rRcCnNzZqQ])-(\w+)\b", lambda m: m.group(1).upper() + " " + m.group(2)),
    (r"(raised )?to the (asterisk|star) power", "star"),
    (r"(raised )?to the prime power", "prime"),
    (r"\bEXPECT\b( of)?", "the expected value of"),
    (r"\b(normal )?E of,? open bracket (.+?) close bracket", r"the expected value of \2"),
    (r"\blim over (.+?) right arrow (.+?) of\b", r"the limit as \1 approaches \2 of"),
    (r"\blim over (.+?) right arrow (\S+)", r"the limit as \1 approaches \2,"),
    (r"\bnabla\b", "the gradient of"),
    (r"\bpartial differential\b", "partial"),
    (r"\bthe metric of\b", "the norm of"),
    (r"\b(\w)-th power\b", r"\1th power"),
    (r"\bopen paren (\w+( sub \w+)?) close paren\b", r"\1"),
    (r"\bnormal\b ", ""),
    (r"\bbackslash (cap )?", ""),
    # "f of open paren z given x close paren" -> "f of z given x" when the argument is simple
    (r"\bof open paren ((?:(?!open paren|close paren|open bracket|\bplus\b|\bminus\b|divided|\btimes\b|\bover\b|equals).){1,48}?)"
     r" close paren(?! (raised |to the |squared|cubed))", r"of \1"),
    (r"\b(raised )?to the SUPIDX (.+?)(-th)? power", r"super \2"),
    (r"\bSUPIDX\b ?", ""),
    (r"\bR (raised )?to the (\w+?)(-th|st|nd|rd|th)? power", r"R \2"),
    (r"\bthe n ary (union|intersection) (of|over)\b", r"the \1 over"),
    (r"\bn ary ", ""),
    (r"\s*\bSETMINUS\b\s*", " set minus "),
    (r"\bKLDIV( of)?", "the K L divergence of"),
    (r"\s*\bDIVFROM\b\s*", " from "),
    (r"\b(bold italic|bold|italic|monospace|double struck|German|sans serif) (?=\S)", ""),
    (r"\basymptotically equals\b", "approximately equals"),
    (r"\bthe expected value of sub (.+?) (times )?open bracket (.+?) close bracket", r"the expected value under \1, of \3"),
    (r"\bthe expected value of sub\b", "the expected value under"),
    (r"^\d+ lines Line 1: ", ""),
    (r"\bLine \d+: ", ""),
]


def _post(s):
    s = " ".join("".join(c for c in s if not 0xFE00 <= ord(c) <= 0xFE0F).split())
    for name, say in FUNCS.items():
        if " " not in name:
            s = re.sub(rf"\b{re.escape(name)}\b", say, s)
    s = s.replace("the variance of of", "the variance of").replace("the covariance of of", "the covariance of")
    for pat, rep in _POST:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\bof of\b", "of", s)
    s = re.sub(r",(\s*,)+", ",", s)
    # a short pause before the relation makes "x sub t plus 1, equals ..." unambiguous
    m = re.search(r" (equals|is less than|is greater than|is approximately|is proportional to)\b", s)
    if m:
        left = s[:m.start()].split()
        tail = " ".join(left[-4:])
        if (len(left) >= 3 and not s[:m.start()].endswith(",")
                and re.search(r"\b(sub|squared|cubed|power|plus|minus|paren|transpose|hat|bar|star|prime)\b", tail)
                and not re.search(r"\b(from|that|as|over|for)\b", " ".join(left[-3:]))
                and left[-1] not in ("approximately", "asymptotically", "not", "identically")
                and not re.match(r" \w+ \w+ to\b| \w+ to\b", s[m.end():])):     # "sub i equals 1 to n": a range
            s = s[:m.start()] + "," + s[m.start():]
    s = s.replace(" and denominator", ", and denominator")
    s = re.sub(r"\b[Aa]\b(?! (member|fraction|function)\b)", "ay", s)   # the variable A, not the article
    return " ".join(s.split()).strip(" ,")


_CACHE = {}


def speak_latex(items, lang="en"):
    """Words for each LaTeX formula (None where it can't be read)."""
    from latex2mathml.converter import convert
    locale = lang if lang in SRE_LOCALES else "en"
    todo, mmls = [], []
    out = [None] * len(items)
    for i, tex in enumerate(items):
        key = (locale, tex)
        if key in _CACHE:
            out[i] = _CACHE[key]
            continue
        try:
            tex_clean = "".join(c for c in tex if not 0xFE00 <= ord(c) <= 0xFE0F)
            mmls.append(_fix_mathml(convert(_prep_latex(tex_clean))))
            todo.append(i)
        except Exception:   # noqa: BLE001
            _CACHE[key] = None
    if todo:
        try:
            said = engine().speak(mmls, locale, CLEARSPEAK if locale == "en" else "default")
        except Exception:   # noqa: BLE001
            said = [None] * len(todo)
        for i, s in zip(todo, said):
            s = _post(s) if s and locale == "en" else (" ".join(s.split()) if s else None)
            _CACHE[(locale, items[i])] = s or None
            out[i] = s or None
    return out


# ---------------------------------------------------------------- finding LaTeX in text
_DELIMS = [
    re.compile(r"\\{1,2}\[(.{1,1500}?)\\{1,2}\]", re.S),          # \[ ... \]  (also \\[ ... \\] from chat apps)
    re.compile(r"\$\$(.{1,1500}?)\$\$", re.S),                     # $$ ... $$
    re.compile(r"\\{1,2}\((.{1,500}?)\\{1,2}\)", re.S),            # \( ... \)
    # $...$ only when it can't be money: no space just inside, no digit right after the
    # opening $ or after the closing one, and something mathematical inside
    re.compile(r"(?<![$\\\w])\$(?![\s\d$])([^$\n]{1,300}?)(?<![\s\\])\$(?![\w$])"),
    # "$ x^{(i)} = [ ... ] $" with spaces inside: only when it's unmistakably LaTeX
    re.compile(r"(?<![$\\\w])\$\s((?=[^$\n]*[\\^_{])[^$\n]{1,300}?)\s?\$(?![\w$])"),
]
_MATHY = re.compile(r"[\\^_=<>+]|^[A-Za-z](_\w+)?$|^[A-Za-z]'*$")


def latex_segments(text):
    """[(start, end, latex)] for formulas written in LaTeX inside plain text."""
    if "\\" not in text and "$" not in text:
        return []
    out, taken = [], []
    for rx in _DELIMS:
        for m in rx.finditer(text):
            a, b = m.span()
            if any(a < y and x < b for x, y in taken):
                continue
            body = m.group(1).strip()
            if not body or (rx in (_DELIMS[3], _DELIMS[4]) and not _MATHY.search(body)):
                continue
            taken.append((a, b))
            out.append((a, b, body))
    return sorted(out)
