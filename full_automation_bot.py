import asyncio
from datetime import datetime, timezone
import html as htmllib
import json
import logging
import os
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from bs4 import BeautifulSoup, Comment, NavigableString
from dotenv import load_dotenv
from g4f.client import Client
from github import Github, Auth, GithubException, UnknownObjectException
import requests
from telegram import Bot

# Load environment variables from .env file if available
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ================= CONFIGURATION =================
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN .env फाइल में नहीं मिला!")

REPO_NAME = os.getenv("GITHUB_REPO", "bgsarkariresult/bgsarkariresult")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bglarenup")

DEFAULT_FALLBACK_IMAGE = "https://images.unsplash.com/photo-1454165804606-c3d57bc86b40?q=80&w=1000&auto=format&fit=crop"
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://bgsarkariresult.github.io/bgsarkariresult")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

client = Client()

# Kitni baar retry karna hai jab GitHub API par transient glitch aaye
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

# Jab koi detail source me na mile to page par yahi dikhega
COMING_SOON = "Coming Soon"

# AI ke liye models (ek ke baad ek try hote hain)
AI_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"]

FIELD_KEYS = [
    "total_vacancies", "qualification", "age_limit", "last_date", "start_date",
    "salary", "fee", "selection_process", "department", "post_name",
]

# ---------- Links: kin sites ke link hamare page par KABHI nahi aane chahiye ----------
# Host ke naam me ye keywords mile to link source / aggregator site ka maana jayega.
# Naya aggregator site mile to bas yahan naam jod dein.
BLOCKED_SITE_KEYWORDS = [
    "freejobalert", "sarkariresult", "indiafreejobalert", "sarkariexam", "sarkarinaukri",
    "rojgarresult", "jobalert", "sarkarijobfind", "sarkariprep", "jobsarkari",
    "freshersnow", "adda247", "govtjobsalert", "jagranjosh", "testbook", "oliveboard",
]

# Social / app / news links kabhi "Official Website" nahi ban sakte
SOCIAL_DOMAINS = [
    "t.me", "telegram.me", "telegram.org", "whatsapp.com", "wa.me", "facebook.com", "fb.com",
    "fb.me", "instagram.com", "youtube.com", "youtu.be", "twitter.com", "x.com", "linkedin.com",
    "pinterest.com", "play.google.com", "apps.apple.com", "news.google.com", "g.co",
]

# Short links: inhe redirect follow karke asli destination nikalte hain
SHORTENER_DOMAINS = ["bit.ly", "tinyurl.com", "goo.gl", "cutt.ly", "rb.gy", "is.gd", "t.ly", "shorturl.at", "ow.ly"]

OFFICIAL_SUFFIXES = (".gov.in", ".nic.in", ".ac.in", ".org.in", ".res.in", ".edu.in", ".gov", ".edu")

REDIRECT_PARAM_KEYS = ["url", "u", "link", "redirect", "redirect_to", "to", "target", "goto", "go", "dest", "destination", "r"]

SITE_NAME_RE = re.compile(
    r"(?i)\b(?:www\.)?(?:india\s*free\s*job\s*alert|free\s*job\s*alert|sarkari\s*result|sarkari\s*exam|sarkari\s*naukri|rojgar\s*result)"
    r"(?:\.com(?:\.cm)?|\.in|\.net|\.org)?\b"
)

# Source page ki aisi lines jo page par / AI ko nahi jani chahiye (promo, social, source site ka naam)
JUNK_LINE_RE = re.compile(
    r"(?i)(telegram|whatsapp|join\s+(?:us|our|now)|follow\s+us|subscribe|youtube\s+channel|facebook|instagram|"
    r"free\s*job\s*alert|freejobalert|sarkari\s*result|sarkariresult|sarkari\s*exam|download\s+(?:our\s+)?app|"
    r"share\s+this|click\s+here\s+to|copyright|all\s+rights\s+reserved|disclaimer\s*:|"
    r"job\s+alerts?|never\s+miss|register\s+for|daily\s+quiz|mock\s+test|take\s+today|"
    r"you\s+may\s+also|also\s+read|read\s+more|preferred\s+source|add\s+us\s+on)"
)

# In values ka matlab "jaankari abhi nahi hai" -> Coming Soon
EMPTY_VALUES = {
    "n/a", "na", "not available", "not mentioned", "various", "various posts", "tba", "tbd",
    "to be announced", "coming soon", "update soon", "updating soon", "-", "--", "notify later",
    "will be updated here soon", "will be updated soon", "to be notified", "soon",
    "click here", "check here", "download here", "click here to download", "apply now", "download", "here", "link",
}
NOT_STATED_RE = re.compile(r"(?i)\b(?:not|no)\b[^.]{0,40}\b(?:mentioned|specified|declared|available|notified|given|stated)\b")
WEAK_RE = re.compile(
    r"(?i)^\s*(?:as\s+per|refer|check|see|read)\b.{0,60}\b(?:notification|advertis\w*|advt|cen|official|detail\w*|rules?|norms?)\b"
)

HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
INLINE_TAGS = {"a", "strong", "b", "em", "i", "span", "u", "small", "mark", "font", "sup", "sub", "br", "code", "abbr", "label"}
BLOCK_CONTAINERS = {"div", "section", "article", "main", "center", "figure", "details", "summary", "blockquote",
                    "dl", "dd", "dt", "tbody", "thead", "header"}
SKIP_TAGS = {"script", "style", "noscript", "iframe", "img", "svg", "picture", "button", "input", "select", "textarea", "nav", "footer", "aside"}

# Section heading -> kind. Upar wale rule pehle check hote hain.
SECTION_RULES = [
    ("stop", r"related posts|latest posts|other posts|you might|latest updates|about the author|connect with us|leave a (?:reply|comment)|"
             r"disclaimer|most viewed|job notifications|jobs by|free mock|last date reminder|have a question"),
    ("pattern", r"exam pattern|physical standard|physical|exam scheme"),
    ("skip", r"\bjoin\b|follow |telegram|whatsapp|download .*app|important links|useful (?:important )?links|useful links|notification pdf|"
             r"syllabus|quiz|register|alert|discuss|report an error|share"),
    ("dates", r"\bdates?\b|schedule|timeline"),
    ("fee", r"\bfee\b|\bfees\b|payment"),
    ("age", r"\bage\b"),
    ("eligibility", r"eligib|qualification|education"),
    ("selection", r"selection|select candidates|how will"),
    ("documents", r"document"),
    ("apply", r"how to (?:apply|fill|check|download)|steps to"),
    ("vacancy", r"vacanc|post wise|posts? details|total post|category wise|reservation"),
    ("pay", r"salary|pay scale|stipend|pay level|pay matrix"),
    ("keypoints", r"key points|highlights"),
    ("overview", r"overview|quick summary|short details|summary|details"),
    ("other", r"other (?:points|conditions|important)|\bnotes?\b|instruction|conditions|relaxation"),
]

SECTION_ORDER = [
    ("overview", "Overview"),
    ("dates", "Important Dates"),
    ("vacancy", "Vacancy Details"),
    ("eligibility", "Eligibility / Qualification"),
    ("age", "Age Limit"),
    ("fee", "Application Fee"),
    ("pay", "Salary / Pay Scale"),
    ("selection", "Selection Process"),
    ("documents", "Documents Required"),
    ("pattern", "Exam Pattern & Other Details"),
    ("other", "Other Important Points"),
]

TABLE_STYLE = 'border="1" style="width:100%; border-collapse:collapse; margin:20px 0; font-size:0.95rem;"'

_REDIRECT_CACHE = {}
_REDIRECT_BUDGET = {"left": 25}


def get_github_repo():
    auth = Auth.Token(GITHUB_TOKEN)
    g = Github(auth=auth)
    return g.get_repo(REPO_NAME)


def slugify(text):
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[\s_-]+', '-', text).strip('-')


def _esc(value, quote=True):
    return htmllib.escape(str(value if value is not None else ""), quote=quote)


def _html_text(value):
    return _esc(value).replace("\n", "<br>")


def _cs(value):
    """Khaali value ki jagah 'Coming Soon' dikhane ke liye."""
    value = (str(value).strip() if value is not None else "")
    return value if value else COMING_SOON


def _clean_value(value):
    if value is None:
        return ""
    value = re.sub(r"\s+", " ", str(value)).strip()
    value = re.sub(r"^[\s:|\-–—]+", "", value)
    value = re.sub(r"(?<!/)[\s:|\-–—]+$", "", value)
    if not value or value.lower() in EMPTY_VALUES:
        return ""
    if len(value) <= 120 and NOT_STATED_RE.search(value):
        return ""
    return value


def _is_weak(value):
    return bool(value) and len(value) < 120 and bool(WEAK_RE.search(value))


def _display_cell(text, cs=True):
    t = (text or "").strip()
    if not t:
        return COMING_SOON if cs else "-"
    if cs and not _clean_value(t):
        return COMING_SOON
    return t


def _first_match(text, patterns, max_len=300):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = _clean_value(match.group(1))
            if value and len(value) <= max_len:
                return value
    return ""


def _normalize_url(base_url, href):
    return urljoin(base_url, href.strip()) if href else ""


# ================= URL / LINK HELPERS =================
def _host(url):
    try:
        h = urlparse(url or "").netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _host_matches(url, domains):
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in domains)


def _is_source_site_url(url):
    h = _host(url)
    return any(k in h for k in BLOCKED_SITE_KEYWORDS)


def _is_social_url(url):
    return _host_matches(url, SOCIAL_DOMAINS)


def _is_shortener(url):
    return _host_matches(url, SHORTENER_DOMAINS)


def _is_blocked_url(url):
    """Source / aggregator / social link? To hamare page par nahi aana chahiye."""
    return _is_source_site_url(url) or _is_social_url(url)


def _is_official_domain(url):
    h = _host(url)
    return h.endswith(OFFICIAL_SUFFIXES) or h in ("nic.in", "gov.in")


def _is_source_article_url(href, source_url):
    if not href:
        return True
    try:
        h = urlparse(href)
        s = urlparse(source_url)
        return h.netloc.lower().replace("www.", "") == s.netloc.lower().replace("www.", "")
    except Exception:
        return False


def _is_likely_official_url(href):
    if not href or not re.match(r"^https?://", href, re.IGNORECASE):
        return False
    return not _is_blocked_url(href) and not _is_shortener(href)


def _strip_site_names(text):
    text = SITE_NAME_RE.sub("", text or "")
    return re.sub(r"[ \t]{2,}", " ", text)


def _extract_explicit_urls(text):
    return re.findall(r'https?://[^\s<>"\']+', text or "", flags=re.IGNORECASE)


def _safe_text(t):
    """Page par daalne layak text. Junk / source-site wali line ho to khaali string."""
    t = re.sub(r"[ \t]+", " ", t or "").strip()
    if not t or JUNK_LINE_RE.search(t):
        return ""
    if any(_is_blocked_url(u) for u in _extract_explicit_urls(t)):
        return ""
    return _strip_site_names(t).strip()


def _unwrap_redirect_param(href):
    try:
        qs = parse_qs(urlparse(href).query)
    except Exception:
        return ""
    for key in REDIRECT_PARAM_KEYS:
        for val in qs.get(key, []):
            val = unquote(val).strip()
            if re.match(r"^https?://", val, re.IGNORECASE):
                return val
    return ""


def _resolve_redirect(href, session=None):
    """Source site ya short link ko follow karke asli (official) destination nikalta hai."""
    if not href or not re.match(r"^https?://", href, re.IGNORECASE):
        return href
    if not (_is_source_site_url(href) or _is_shortener(href)):
        return href
    if href in _REDIRECT_CACHE:
        return _REDIRECT_CACHE[href]

    final = _unwrap_redirect_param(href) or href
    if (_is_source_site_url(final) or _is_shortener(final)) and _REDIRECT_BUDGET["left"] > 0:
        _REDIRECT_BUDGET["left"] -= 1
        try:
            s = session or requests
            r = s.head(final, allow_redirects=True, timeout=10, headers=HEADERS)
            if r.status_code >= 400 or r.url == final:
                r = s.get(final, allow_redirects=True, timeout=15, headers=HEADERS, stream=True)
                r.close()
            final = r.url or final
        except Exception as e:
            logging.warning(f"Redirect resolve failed for {href}: {e}")
    _REDIRECT_CACHE[href] = final
    return final


def _kind_from_label(label):
    """Link ke label / row ke naam se pata lagata hai ki yeh kis type ka link hai."""
    t = re.sub(r"\s+", " ", (label or "")).lower()
    if not t:
        return ""
    if any(x in t for x in ["admit card", "hall ticket", "call letter", "city intimation", "exam city"]):
        return "admit"
    if any(x in t for x in ["answer key", "response sheet", "objection"]):
        return "answer_key"
    if any(x in t for x in ["result", "scorecard", "score card", "merit list", "selection list", "cut off", "cutoff"]):
        return "result"
    if any(x in t for x in ["notification", "advertisement", "advt", "brochure", "download pdf"]):
        return "notification"
    if any(x in t for x in ["notice", "corrigendum", "circular"]):
        return "notice"
    if any(x in t for x in ["syllabus", "exam pattern"]):
        return "syllabus"
    if any(x in t for x in ["apply online", "online application", "apply now", "application form",
                            "registration", "apply link", "login", "apply"]):
        return "apply"
    if any(x in t for x in ["official website", "official site", "official portal", "official web",
                            "website", "home page", "homepage"]):
        return "website"
    return ""


def _classify_link(label, href, source_url=""):
    if not re.match(r"^https?://", href or "", re.IGNORECASE):
        return ""
    if _is_blocked_url(href) or _is_shortener(href):
        return ""
    if source_url and _is_source_article_url(href, source_url):
        return ""
    kind = _kind_from_label(label)
    if not kind:
        path = href.lower().split("?")[0]
        if path.endswith(".pdf"):
            kind = "notice"
        elif _is_official_domain(href):
            kind = "website"
    return kind


def _collect_link_candidates(scope, base_url):
    """Link candidates. Table me row ka pehla cell label banta hai ("Click Here" ke bajaay)."""
    cands = []
    seen = set()

    def ok_href(h):
        return h and not h.strip().lower().startswith(("#", "mailto:", "javascript:", "tel:"))

    for tr in scope.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        row_label = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True))
        first_cell_anchors = cells[0].find_all("a", href=True)
        for a in tr.find_all("a", href=True):
            if not ok_href(a.get("href")):
                continue
            a_text = a.get_text(" ", strip=True)
            in_first = any(a is x for x in first_cell_anchors)
            cands.append({
                "label": a_text if in_first else f"{row_label} {a_text}",
                "title": a_text if in_first else row_label,
                "href": _normalize_url(base_url, a.get("href")),
            })
            seen.add(id(a))

    for a in scope.find_all("a", href=True):
        if id(a) in seen or not ok_href(a.get("href")):
            continue
        a_text = a.get_text(" ", strip=True)
        label = a_text
        parent = a.parent
        ptxt = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)) if parent else ""
        if ptxt and len(ptxt) <= 120:
            label = ptxt
        cands.append({"label": label, "title": a_text or label, "href": _normalize_url(base_url, a.get("href"))})
    return cands


def _first(items):
    return items[0] if items else ""


GENERIC_LINK_TITLES = {
    "notice": "Official Notice", "syllabus": "Syllabus & Exam Pattern", "notification": "Official Notification",
    "apply": "Apply Online", "result": "Result", "admit": "Admit Card", "answer_key": "Answer Key",
}


def _link_title(title, kind):
    t = _strip_site_names(re.sub(r"\s+", " ", title or "")).strip(" :-|")
    if t.lower() in ("", "click here", "here", "download", "link", "check here", "click here to download"):
        return GENERIC_LINK_TITLES.get(kind, "Official Link")
    return t[:120]


def _extract_links(scope, base_url, source_url, category, session=None):
    kinds = ("notification", "notice", "syllabus", "apply", "result", "admit", "answer_key", "website")
    found = {k: [] for k in kinds}
    resolved = []
    for cand in _collect_link_candidates(scope, base_url):
        label, href = cand["label"], cand["href"]
        if _kind_from_label(label) or _is_shortener(href):
            href = _resolve_redirect(href, session)
        kind = _classify_link(label, href, source_url)
        if kind:
            found[kind].append(href)
            resolved.append((kind, cand["title"], href))

    expected = {"Result": "result", "Admit Card": "admit", "Answer Key": "answer_key"}.get(category, "apply")
    pdf_url = _first(found["notification"])
    if not pdf_url:
        for h in found["notice"]:
            if re.search(r"notification|advt|advertis", h.lower()):
                pdf_url = h
                break
    links = {
        "pdf_url": pdf_url,
        "apply_url": _first(found[expected]),
        "official_site": _first(found["website"]),
    }

    # Official site clear nahi mili, par apply/notification link kisi official domain par hai
    # to us domain ka home page official site maana jayega (actual link se, andaaze se nahi).
    if not links["official_site"]:
        for u in (links["apply_url"], links["pdf_url"]):
            if u and _is_official_domain(u):
                p = urlparse(u)
                links["official_site"] = f"{p.scheme}://{p.netloc}/"
                break

    # Baaki official notices / PDFs (corrigendum, roster, syllabus PDF, ...) -> extra rows
    used = {v for v in links.values() if v}
    extras, seen_hrefs = [], set()
    for kind, title, href in resolved:
        if kind == "website" or href in used or href in seen_hrefs:
            continue
        path = href.lower().split("?")[0]
        if not (_is_official_domain(href) or path.endswith(".pdf")):
            continue
        seen_hrefs.add(href)
        extras.append((_link_title(title, kind), href))
        if len(extras) >= 8:
            break
    links["extra_links"] = extras
    return links


# ================= FIELD EXTRACTION =================
def _assign_field(fields, key, value):
    key = re.sub(r"\s+", " ", (key or "").lower()).strip(" :-|")
    value = _clean_value(value)
    if not key or not value or len(key) > 60 or len(value) > 600:
        return
    if value.lower() in {"eligibility criteria", "details", "particulars", "description", "date", "link", "event"}:
        return

    if any(x in key for x in ["total vacanc", "total post", "no. of vacanc", "no of vacanc", "number of vacanc",
                              "no. of post", "no of post", "number of post", "vacancies", "vacancy"]):
        name = "total_vacancies"
    elif any(x in key for x in ["qualification", "eligibility", "education"]):
        name = "qualification"
    elif re.search(r"\bage\b", key):
        name = "age_limit"
    elif any(x in key for x in ["last date", "closing date", "end date", "last day", "apply last", "application end"]):
        name = "last_date"
    elif any(x in key for x in ["start date", "starting date", "application start", "application begin", "apply start",
                                "begin date", "date of start", "opening date", "open date", "application opens",
                                "starts from", "starting from"]):
        name = "start_date"
    elif any(x in key for x in ["salary", "pay scale", "pay level", "pay matrix", "stipend"]) or re.match(r"^pay\b", key):
        name = "salary"
    elif any(x in key for x in ["application fee", "exam fee", "fee"]):
        name = "fee"
    elif any(x in key for x in ["selection process", "selection procedure", "mode of selection"]):
        name = "selection_process"
    elif any(x in key for x in ["department", "organization", "organisation", "recruiting body", "conducting body"]):
        name = "department"
    elif any(x in key for x in ["post name", "name of post", "posts"]):
        name = "post_name"
    else:
        return

    if re.search(r"\bnotice\b|\bnotification\b|\blink\b", key) and name != "total_vacancies":
        return
    if name in ("last_date", "start_date") and re.search(r"fee|payment|correction|modif|edit|withdraw", key):
        return
    if name == "total_vacancies" and (not re.search(r"\d", value) or len(value) > 80):
        return
    if name in ("last_date", "start_date") and len(value) > 200:
        return

    cur = fields[name]
    if not cur or (_is_weak(cur) and not _is_weak(value)):
        fields[name] = value


def _bold_ratio(tag):
    text = re.sub(r"\W", "", tag.get_text("", strip=True))
    if not text:
        return 0.0
    bold = re.sub(r"\W", "", "".join(b.get_text("", strip=True) for b in tag.find_all(["strong", "b"])))
    return len(bold) / len(text)


def _is_bold_only(tag):
    return _bold_ratio(tag) >= 0.9


def _cell_is_bold(c):
    return c.name == "th" or _is_bold_only(c)


def _extract_structured_fields(scope, text, title=""):
    fields = {k: "" for k in FIELD_KEYS}

    # 1) Table rows: key | value (header rows chhod kar)
    for table in scope.find_all("table"):
        for idx, tr in enumerate(table.find_all("tr")):
            cell_tags = tr.find_all(["th", "td"])
            if len(cell_tags) < 2:
                continue
            if idx == 0 and all(_cell_is_bold(c) for c in cell_tags):
                continue
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in cell_tags]
            _assign_field(fields, cells[0], " ".join(cells[1:]))

    # 2) List / paragraph lines: "Label: value"
    for tag in scope.find_all(["li", "p", "dd"]):
        t = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
        if ":" not in t or not (5 < len(t) < 500):
            continue
        label, _, value = t.partition(":")
        if len(label) <= 50:
            _assign_field(fields, label, value)

    # 3) Plain-text regex fallback
    head = f"{title}\n{text[:1500]}"
    fields["total_vacancies"] = fields["total_vacancies"] or _first_match(text, [
        r"(?:total\s+(?:number\s+of\s+)?vacanc(?:y|ies)|total\s+posts?|no\.?\s*of\s*(?:posts?|vacanc(?:y|ies))|"
        r"number\s+of\s+(?:posts?|vacanc(?:y|ies)))\s*[:\-]?\s*([0-9][0-9,]*)",
    ]) or _first_match(head, [
        # Loose pattern sirf title / shuru ke text me (galat number uthane se bachne ke liye)
        r"\b([0-9][0-9,]{1,})\s*\+?\s*(?:vacanc(?:y|ies)|posts?)\b",
    ])
    fields["qualification"] = fields["qualification"] or _first_match(text, [r"(?:educational\s+)?qualification\s*[:\-]\s*([^\n]+)"])
    fields["age_limit"] = fields["age_limit"] or _first_match(text, [r"age\s+limit\s*[:\-]\s*([^\n]+)"])
    fields["last_date"] = fields["last_date"] or _first_match(text, [r"(?:last\s+date|closing\s+date|application\s+last\s+date)\s*[:\-]\s*([^\n]+)"])
    fields["start_date"] = fields["start_date"] or _first_match(text, [r"(?:start\s+date|starting\s+date|application\s+start\s+date)\s*[:\-]\s*([^\n]+)"])
    # Salary: sirf ":" se (nahi to "Pay\nLevel-6" me "Level-" kat jata tha)
    # Date ke baad agla sentence (jaise ". Total Vacancy: 12") value me na ghuse
    for _k in ("last_date", "start_date"):
        if fields[_k]:
            fields[_k] = _clean_value(re.split(r"\.\s+(?=[A-Z])", fields[_k])[0])
    fields["salary"] = fields["salary"] or _first_match(text, [r"(?:salary|pay\s+scale)\s*:\s*([^\n]+)"])
    fields["fee"] = fields["fee"] or _first_match(text, [r"(?:application\s+fee|exam\s+fee)\s*[:\-]\s*([^\n]+)"])
    return fields


def _clean_raw_text(text):
    """Promo / social / source-site wali lines hata deta hai."""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or JUNK_LINE_RE.search(s):
            continue
        if any(_is_blocked_url(u) for u in _extract_explicit_urls(s)):
            continue
        out.append(s)
    return "\n".join(out)


def _clean_title(raw):
    t = re.sub(r"\s+", " ", raw or "").strip()
    t = re.sub(r"\s*[-|–—:]\s*(?:www\.)?(?:free\s*job\s*alert|sarkari\s*result)[^|]*$", "", t, flags=re.IGNORECASE)
    t = _strip_site_names(t)
    t = re.sub(r"\s*\(\s*short\s+notice\s*\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[-|–—:]\s*$", "", t)
    t = re.sub(r"^\s*[-|–—:]\s*", "", t)
    return t.strip()


def _short_title(title):
    return re.split(r"\s+[-–—|]\s+", title or "", maxsplit=1)[0].strip() or (title or "")


def _detect_category(title):
    title_lower = (title or "").lower()
    if any(k in title_lower for k in ["admit card", "hall ticket", "call letter", "city intimation"]):
        return "Admit Card"
    if any(k in title_lower for k in ["result", "scorecard", "merit list", "selection list", "cut off"]):
        return "Result"
    if any(k in title_lower for k in ["answer key", "response sheet"]):
        return "Answer Key"
    return "Latest Jobs"


# ================= SECTION-BASED EXTRACTION =================
def _list_items(ul):
    """List ke items (nested items alag line ban jate hain)."""
    items = []
    for li in ul.find_all("li", recursive=False):
        own = []
        nested = []
        for ch in li.children:
            if isinstance(ch, NavigableString):
                own.append(str(ch))
            elif ch.name in ("ul", "ol"):
                nested.append(ch)
            else:
                own.append(ch.get_text(" ", strip=True))
        t = re.sub(r"\s+", " ", " ".join(own)).strip()
        if t:
            items.append(t)
        for n in nested:
            items.extend(_list_items(n))
    return items


def _table_blocks(table):
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = tr.find_all(["th", "td"], recursive=False)
        row = []
        for c in cells:
            for br in c.find_all("br"):
                br.replace_with("\n")
            for li in c.find_all("li"):
                li.insert(0, "\n")
            lines = [re.sub(r"\s+", " ", l).strip() for l in c.get_text(" ").split("\n")]
            row.append({"t": "\n".join(l for l in lines if l), "b": _cell_is_bold(c)})
        if row and any(x["t"] for x in row):
            rows.append(row)
    if not rows:
        return []

    ncols = [len(r) for r in rows]
    blocks = []
    if all(n == 1 for n in ncols):
        start = 0
        first = rows[0][0]
        if len(rows) > 1 and (first["b"] or len(first["t"]) <= 120) and "\n" not in first["t"]:
            blocks.append(("heading", first["t"]))
            start = 1
        for r in rows[start:]:
            lines = r[0]["t"].split("\n")
            blocks.append(("list", lines) if len(lines) > 1 else ("para", lines[0]))
        return blocks

    if len(rows) > 1 and ncols[0] == 1 and all(n >= 2 for n in ncols[1:]) and "\n" not in rows[0][0]["t"]:
        blocks.append(("heading", rows[0][0]["t"]))
        rows = rows[1:]
    blocks.append(("table", rows))
    return blocks


def _collect_blocks(node):
    """Content ko (heading / list / table / para) blocks me todta hai, document ke order me."""
    blocks = []
    buf = []  # (text, is_link)

    def flush():
        pieces = [(t, l) for t, l in buf if t.strip()]
        buf.clear()
        if not pieces or all(l for _, l in pieces):
            return  # sirf link text (jaise "Join Now") -> chhod do
        t = re.sub(r"\s+", " ", " ".join(t for t, _ in pieces)).strip()
        if t:
            blocks.append(("para", t))

    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            buf.append((str(child), False))
            continue
        name = child.name or ""
        if name in SKIP_TAGS:
            continue
        if name in INLINE_TAGS:
            buf.append((child.get_text(" ", strip=True), name == "a"))
            continue
        flush()
        if name in HEADINGS:
            t = re.sub(r"\s+", " ", child.get_text(" ", strip=True))
            if t:
                blocks.append(("heading", t))
        elif name in ("ul", "ol"):
            items = _list_items(child)
            if items:
                blocks.append(("list", items))
        elif name == "table":
            blocks.extend(_table_blocks(child))
        elif name == "p":
            t = re.sub(r"\s+", " ", child.get_text(" ", strip=True))
            if not t:
                continue
            if _is_bold_only(child) and len(t) <= 90 and not t.endswith("."):
                blocks.append(("heading", t.rstrip(": ")))
            elif not all(ch.name == "a" for ch in child.find_all(True, recursive=False)) or len(child.find_all(True, recursive=False)) == 0:
                blocks.append(("para", t))
        elif name in BLOCK_CONTAINERS:
            blocks.extend(_collect_blocks(child))
        else:
            t = re.sub(r"\s+", " ", child.get_text(" ", strip=True))
            if t:
                blocks.append(("para", t))
    flush()
    return blocks


def _drop_faq_blocks(blocks):
    """Source ke FAQ / Q&A blocks hata deta hai (FAQ hum apne facts se khud banate hain)."""
    out = []
    in_faq = False
    for b in blocks:
        if b[0] == "heading":
            t = b[1]
            if re.search(r"(?i)\bfaqs?\b|frequently asked|important question", t):
                in_faq = True
                continue
            if re.match(r"(?i)^\s*q\s*\d+\s*[.:)\-]", t) or (in_faq and t.rstrip().endswith("?")):
                in_faq = True
                continue
            in_faq = False
            out.append(b)
            continue
        if in_faq:
            continue
        text = "\n".join(b[1]) if b[0] == "list" else (b[1] if b[0] == "para" else "")
        if text and re.match(r"(?is)^\s*(?:question|q\d*)\s*[:.\-)]", text):
            continue
        out.append(b)
    return out


def _classify_section(title):
    t = (title or "").lower()
    for kind, pattern in SECTION_RULES:
        if re.search(pattern, t):
            return kind
    return "unknown"


def _build_sections(blocks):
    """{kind: [blocks_list, ...]}. Sirf pehchane hue sections rakhe jate hain."""
    sections = {}
    kind = "intro"
    current = []

    def commit():
        if current and kind not in ("skip", "stop", "unknown", "intro"):
            sections.setdefault(kind, []).append(list(current))

    for b in blocks:
        if b[0] == "heading":
            commit()
            current = []
            kind = _classify_section(b[1])
            if kind == "stop":
                break
        else:
            current.append(b)
    else:
        commit()
    return sections


def _section_lines(blocks):
    lines = []
    for b in blocks:
        if b[0] == "list":
            lines.extend(b[1])
        elif b[0] == "para":
            lines.append(b[1])
        elif b[0] == "table":
            for row in b[1]:
                lines.append(" : ".join(c["t"].replace("\n", " ") for c in row))
    return [l for l in (_safe_text(x) for x in lines) if l]


def _split_kv(item):
    if ":" not in item:
        return None
    label, _, value = item.partition(":")
    label, value = label.strip(), value.strip()
    if 0 < len(label) <= 70 and value:
        return label, value
    return None


def _kv_pairs(blocks):
    pairs = []
    for b in blocks:
        if b[0] == "list":
            for it in b[1]:
                kv = _split_kv(it)
                if kv:
                    pairs.append(kv)
        elif b[0] == "para":
            kv = _split_kv(b[1])
            if kv:
                pairs.append(kv)
        elif b[0] == "table":
            rows = b[1]
            start = 1 if (len(rows) > 1 and len(rows[0]) >= 2 and all(c["b"] for c in rows[0])) else 0
            for row in rows[start:]:
                if len(row) >= 2:
                    pairs.append((row[0]["t"].replace("\n", " "), " ".join(c["t"].replace("\n", " ") for c in row[1:])))
    return pairs


def _fill_from_sections(fields, sections):
    for blocks in sections.get("dates", []):
        for label, value in _kv_pairs(blocks):
            _assign_field(fields, label, value)

    for kind in ("eligibility", "overview", "pay"):
        for blocks in sections.get(kind, []):
            for label, value in _kv_pairs(blocks):
                _assign_field(fields, label, value)

    if not fields["total_vacancies"]:
        for blocks in sections.get("vacancy", []):
            for line in _section_lines(blocks):
                m = re.search(r"\b([0-9][0-9,]{1,})\s*(?:posts?|vacanc(?:y|ies))\b", line, re.IGNORECASE)
                if m:
                    fields["total_vacancies"] = m.group(1)
                    break
            if fields["total_vacancies"]:
                break

    if not fields["fee"]:
        picks = []
        for blocks in sections.get("fee", []):
            for l in _section_lines(blocks):
                if re.search(r"\d|\bnil\b|\bfree\b|exempt", l, re.IGNORECASE) and not re.search(r"payment mode", l, re.IGNORECASE):
                    picks.append(l)
        val = _clean_value("; ".join(picks[:3]))
        if val and len(val) <= 300:
            fields["fee"] = val

    age_lines = [l for blocks in sections.get("age", []) for l in _section_lines(blocks)
                 if re.search(r"\d", l) and len(l) <= 160]
    if age_lines:
        val = _clean_value("; ".join(age_lines[:4]))
        cur = fields["age_limit"]
        if val and len(val) <= 300 and not _is_weak(val) and (not cur or _is_weak(cur) or len(val) > len(cur)):
            fields["age_limit"] = val

    if not fields["selection_process"]:
        lines = [l for blocks in sections.get("selection", []) for l in _section_lines(blocks) if len(l) <= 80]
        val = _clean_value(" → ".join(lines[:8]))
        if val and len(val) <= 300:
            fields["selection_process"] = val

    if not fields["qualification"] or _is_weak(fields["qualification"]):
        for blocks in sections.get("eligibility", []):
            if any(b[0] == "table" and len(b[1]) >= 3 for b in blocks) and not fields["qualification"]:
                fields["qualification"] = "Post-wise eligibility (see details below)"
                break


CONTENT_SELECTORS = [".entry-content", ".post-content", ".td-post-content", ".article-content",
                     ".single-content", ".post-body", "article", "main"]


def _find_content_node(soup):
    for sel in CONTENT_SELECTORS:
        try:
            node = soup.select_one(sel)
        except Exception:
            node = None
        if node and len(node.get_text(" ", strip=True)) > 300:
            return node
    return soup.body or soup


def scrape_job_details(url):
    data = {
        "title": "", "slug": "", "post_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_vacancies": "", "qualification": "", "age_limit": "", "last_date": "",
        "start_date": "", "salary": "", "fee": "", "selection_process": "",
        "department": "", "post_name": "", "image": DEFAULT_FALLBACK_IMAGE,
        "raw_text": "", "source_url": url, "apply_url": "", "pdf_url": "",
        "official_site": "", "category": "Latest Jobs", "extra_links": [], "sections": {},
    }

    _REDIRECT_BUDGET["left"] = 25
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        res = session.get(url, timeout=30, allow_redirects=True)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        # ---- Title (site ka naam hata kar) ----
        title_candidates = []
        h1 = soup.find("h1")
        if h1:
            title_candidates.append(h1.get_text(" ", strip=True))
        h2 = soup.find("h2", {"class": "entry-title"})
        if h2:
            title_candidates.append(h2.get_text(" ", strip=True))
        og = soup.find("meta", {"property": "og:title"})
        if og and og.get("content"):
            title_candidates.append(og.get("content"))
        if soup.title and soup.title.string:
            title_candidates.append(soup.title.string)
        for cand in title_candidates:
            cleaned = _clean_title(cand)
            if len(cleaned) >= 8:
                data["title"] = cleaned
                data["slug"] = slugify(cleaned)
                break

        data["category"] = _detect_category(data["title"])

        # ---- Content ----
        entry_content = _find_content_node(soup)
        for tag in entry_content.find_all(["script", "style", "nav", "footer", "aside", "noscript", "iframe"]):
            tag.decompose()
        data["raw_text"] = _clean_raw_text(entry_content.get_text(separator="\n", strip=True))

        # ---- Fields ----
        fields = _extract_structured_fields(entry_content, data["raw_text"], data["title"])
        if sum(1 for v in fields.values() if v) < 3 and entry_content is not soup:
            extra = _extract_structured_fields(soup, data["raw_text"], data["title"])
            for k, v in extra.items():
                fields[k] = fields[k] or v

        # ---- Image: source site ki image nahi, sirf kisi aur domain ki ----
        image = entry_content.find("img")
        if image:
            src = image.get("src") or image.get("data-src") or ""
            img_url = _normalize_url(res.url, src)
            if re.match(r"^https?://", img_url, re.IGNORECASE) and not _is_blocked_url(img_url) \
                    and not _is_source_article_url(img_url, url):
                data["image"] = img_url

        # ---- Links: sirf asli official links ----
        links = _extract_links(entry_content, res.url, url, data["category"], session)
        if not any(links.get(k) for k in ("pdf_url", "apply_url", "official_site")) and entry_content is not soup:
            links = _extract_links(soup, res.url, url, data["category"], session)
        data["extra_links"] = links.pop("extra_links", [])
        data.update({k: v for k, v in links.items() if v})

        # ---- Sections (dates, fee, age, eligibility, selection, documents ...) ----
        # NOTE: yeh tree me <br> / <li> badalta hai, isliye links aur fields ke BAAD chalta hai
        blocks = _drop_faq_blocks(_collect_blocks(entry_content))
        sections = _build_sections(blocks)
        _fill_from_sections(fields, sections)
        data["sections"] = sections
        data.update(fields)

        # Kuch pages me official links sirf plain text me hote hain
        category_keywords = {
            "Result": ["result", "score"],
            "Admit Card": ["admit", "hall", "call"],
            "Answer Key": ["answerkey", "answer-key", "answer_key", "response", "objection"],
        }.get(data["category"], ["authentication.aspx", "apply", "registration", "recruit"])
        for href in _extract_explicit_urls(data["raw_text"]):
            href = href.rstrip(").,;")
            if _is_blocked_url(href) or _is_shortener(href) or _is_source_article_url(href, url):
                continue
            low = href.lower()
            if (".pdf" in low or "notificationpdf" in low or "notification/" in low) and not data["pdf_url"]:
                data["pdf_url"] = href
            elif any(k in low for k in category_keywords) and not data["apply_url"]:
                data["apply_url"] = href
            elif _is_likely_official_url(href) and _is_official_domain(href) and not data["official_site"]:
                data["official_site"] = href

        logging.info("Extracted facts: vacancies=%r qualification=%r age=%r start=%r last=%r salary=%r fee=%r selection=%r notification=%r apply=%r sections=%s",
                     data["total_vacancies"], data["qualification"], data["age_limit"], data["start_date"],
                     data["last_date"], data["salary"], data["fee"], data["selection_process"],
                     data["pdf_url"], data["apply_url"], sorted(sections.keys()))

    except Exception as e:
        logging.error(f"Scraping Error: {e}")
        raise RuntimeError(f"Source page scrape failed: {e}") from e

    if not data["title"]:
        raise RuntimeError("Source page se job title nahi mila; publish roka gaya.")

    return data


# ================= AI HELPERS =================
def get_ai_response(prompt, rounds=3):
    """g4f kabhi-kabhi fail hota hai, isliye kai models aur kai round try karte hain."""
    for attempt in range(1, rounds + 1):
        for model_name in AI_MODELS:
            try:
                res = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}]
                )
                content = res.choices[0].message.content
                if content and len(content.strip()) > 20:
                    return content
            except Exception as e:
                logging.warning(f"AI call failed (round {attempt}, model {model_name}): {e}")
                continue
        if attempt < rounds:
            time.sleep(3 * attempt)
    return ""


def _parse_json_object(text):
    s = re.sub(r"```(?:json)?", "", text or "", flags=re.IGNORECASE)
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def ai_fill_missing_fields(job):
    """Regex se jo fields nahi mile, unhe AI se source text me se nikalwata hai (sirf text me maujood facts)."""
    missing = [k for k in FIELD_KEYS if not job.get(k)]
    if not missing or not job.get("raw_text"):
        return

    prompt = f"""Extract recruitment facts from the notification text below.
Return ONLY one JSON object (no markdown, no explanation) with exactly these keys:
{json.dumps(FIELD_KEYS)}

Rules:
- Use short plain text copied or condensed from the text. Keep dates and numbers exactly as written.
- If a fact is not clearly present in the text, use an empty string "". NEVER guess or invent.
- total_vacancies must be the total number of posts if the text states it.
- Do not mention any website name.

Title: {job['title']}
Text:
{job['raw_text'][:9000]}
"""
    reply = get_ai_response(prompt, rounds=2)
    data = _parse_json_object(reply)
    if not data:
        logging.warning("AI field extraction ne koi usable JSON nahi diya.")
        return

    raw = job["raw_text"]
    filled = []
    for key in missing:
        value = _clean_value(data.get(key, ""))
        if not value or len(value) > 300:
            continue
        # Grounding check: value ke bade numbers (3+ digit) source text me hone chahiye
        tokens = re.findall(r"\d[\d,]*", value)
        if any(len(t.replace(",", "")) >= 3 and t not in raw for t in tokens):
            continue
        job[key] = value
        filled.append(key)
    if filled:
        logging.info(f"AI ne ye missing fields bhare: {filled}")


def ai_intro(job):
    """Chhota Hinglish intro paragraph (sirf diye gaye facts se). Fail ho to khaali string."""
    facts = {
        "Title": job["title"], "Category": job["category"],
        "Organisation": _cs(job["department"]), "Post": _cs(job["post_name"]),
        "Total vacancies": _cs(job["total_vacancies"]), "Qualification": _cs(job["qualification"]),
        "Start date": _cs(job["start_date"]), "Last date": _cs(job["last_date"]),
        "Age limit": _cs(job["age_limit"]), "Fee": _cs(job["fee"]),
        "Selection": _cs(job["selection_process"]),
    }
    facts_txt = "\n".join(f"{k}: {v}" for k, v in facts.items())
    prompt = f"""Write ONE short paragraph (70-110 words) in simple Hinglish for an Indian government job website about this notification.
Use ONLY the facts below. Do not invent anything. Do not mention any other website. No links, no HTML, no bullet points, no headings.
If a fact says "{COMING_SOON}", you may say that the detail will be updated soon.

{facts_txt}
"""
    txt = get_ai_response(prompt, rounds=2)
    txt = re.sub(r"<[^>]+>", " ", txt or "")
    txt = re.sub(r"[`*#]", "", txt)
    txt = _strip_site_names(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if re.search(r"https?://|www\.", txt, re.IGNORECASE):
        return ""
    return txt if 80 <= len(txt) <= 1200 else ""


def validate_job_data(job):
    problems = []
    if not job.get("title"):
        problems.append("title missing")
    if not job.get("raw_text"):
        problems.append("source article text missing")

    forbidden = {
        "total_vacancies": ["Various Posts", "Various"],
        "qualification": ["10th / 12th / Graduate"],
        "age_limit": ["18 to 35 Years"],
        "last_date": ["Check Official Portal"]
    }
    for field, values in forbidden.items():
        if job.get(field) in values:
            problems.append(f"fabricated fallback in {field}")

    # Source / aggregator / social link kabhi publish nahi hoga (chupchaap hata diya jata hai)
    for key in ("pdf_url", "apply_url", "official_site"):
        val = job.get(key)
        if val and (_is_blocked_url(val) or _is_shortener(val)):
            logging.warning(f"{key} source/aggregator link tha, hata diya: {val}")
            job[key] = ""
    job["extra_links"] = [(t, h) for t, h in job.get("extra_links", []) if not _is_blocked_url(h) and not _is_shortener(h)]

    if job.get("category") == "Latest Jobs" and not job.get("sections") and not any(
        job.get(x) for x in ["total_vacancies", "qualification", "last_date", "age_limit", "salary",
                             "start_date", "fee", "selection_process"]
    ):
        problems.append("no structured recruitment facts extracted")

    if problems:
        raise RuntimeError("Publish validation failed: " + "; ".join(problems))


# ================= PAGE BUILDING =================
def _date_label(category):
    if category in ("Result", "Answer Key"):
        return "Released"
    if category == "Admit Card":
        return "Available from"
    return "Last Date"


def _simple_table(rows, header=("Particulars", "Details")):
    head = ""
    if header:
        head = (f'<tr style="background:#f1f5f9;"><th style="padding:10px;text-align:left;">{_esc(header[0])}</th>'
                f'<th style="padding:10px;text-align:left;">{_esc(header[1])}</th></tr>')
    body = "".join(
        f'<tr><td style="padding:10px;"><b>{_esc(a)}</b></td><td style="padding:10px;">{_html_text(b)}</td></tr>'
        for a, b in rows
    )
    return f"<table {TABLE_STYLE}>{head}{body}</table>"


def _render_table(rows, cs=False):
    clean = []
    for r in rows:
        if any(JUNK_LINE_RE.search(c["t"]) or any(_is_blocked_url(u) for u in _extract_explicit_urls(c["t"])) for c in r):
            continue
        clean.append([{"t": _strip_site_names(c["t"]), "b": c["b"]} for c in r])
    if not clean:
        return ""
    ncols = max(len(r) for r in clean)
    for r in clean:
        while len(r) < ncols:
            r.append({"t": "", "b": False})

    has_header = len(clean) > 1 and ncols >= 2 and all(c["b"] for c in clean[0])
    out = [f"<table {TABLE_STYLE}>"]
    body_rows = clean
    if has_header:
        out.append('<tr style="background:#f1f5f9;">' +
                   "".join(f'<th style="padding:10px;text-align:left;">{_html_text(c["t"])}</th>' for c in clean[0]) + "</tr>")
        body_rows = clean[1:]
    for r in body_rows:
        cells = []
        for i, c in enumerate(r):
            txt = _html_text(_display_cell(c["t"], cs))
            if i == 0 and ncols == 2 and not has_header:
                txt = f"<b>{txt}</b>"
            cells.append(f'<td style="padding:10px;vertical-align:top;">{txt}</td>')
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _render_kv_list(items):
    kv, others = [], []
    for it in items:
        pair = _split_kv(it)
        (kv if pair else others).append(pair or it)
    html_out = ""
    if kv:
        html_out += _simple_table([(a, _display_cell(b, True)) for a, b in kv], header=None)
    if others:
        html_out += "<ul>" + "".join(f"<li>{_html_text(o)}</li>" for o in others) + "</ul>"
    return html_out


def _render_blocks(blocks, kind):
    kv_kinds = {"dates", "fee", "age", "pay"}
    cs_kinds = {"dates", "overview", "fee", "age", "pay"}
    out = []
    for b in blocks:
        if b[0] == "table":
            out.append(_render_table(b[1], cs=kind in cs_kinds))
        elif b[0] == "list":
            items = [i for i in (_safe_text(x) for x in b[1]) if i]
            if not items:
                continue
            if kind in kv_kinds:
                out.append(_render_kv_list(items))
            else:
                tag = "ol" if kind == "selection" else "ul"
                out.append(f"<{tag}>" + "".join(f"<li>{_html_text(i)}</li>" for i in items) + f"</{tag}>")
        elif b[0] == "para":
            t = _safe_text(b[1])
            if not t:
                continue
            if kind == "vacancy" and re.fullmatch(r"[0-9][0-9,]*\s*(?:posts?|vacanc(?:y|ies))", t, re.IGNORECASE):
                out.append(f"<p><b>Total Posts:</b> {_esc(t)}</p>")
            elif len(t) <= (250 if kind == "other" else 400):
                out.append(f"<p>{_html_text(t)}</p>")
    return "".join(out)


def _apply_steps(job):
    cat = job["category"]
    last = job["last_date"]
    if cat == "Result":
        title, steps = "How to Check Result", [
            "Official result portal kholein (link neeche Important Links me diya gaya hai).",
            "Result / Score Card wale link par click karein.",
            "Roll number / Registration number aur zaroori details bharein.",
            "Result dekhein aur PDF download ya print karke rakh lein.",
        ]
    elif cat == "Admit Card":
        title, steps = "How to Download Admit Card", [
            "Official website kholein (link neeche Important Links me diya gaya hai).",
            "Admit Card / Hall Ticket wale link par click karein.",
            "Registration number / Roll number aur password ya date of birth bharein.",
            "Admit Card download karein aur exam ke din print copy saath le jayein.",
        ]
    elif cat == "Answer Key":
        title, steps = "How to Check Answer Key", [
            "Official website kholein (link neeche Important Links me diya gaya hai).",
            "Answer Key / Response Sheet wale link par click karein.",
            "Login details bharein aur answer key download karein.",
            "Agar objection ka option ho to last date se pehle apna objection darj karein.",
        ]
    else:
        title, steps = "How to Apply", [
            "Official website kholein (link neeche Important Links me diya gaya hai).",
            "Official notification dhyan se padhein aur eligibility check karein.",
            "Registration / login karke online application form bharein.",
            "Zaroori documents, photo aur signature upload karein.",
            "Agar fee lagti hai to online fee jama karein (fee details upar di gayi hain).",
            "Form submit karein aur print ya PDF copy save karke rakhein.",
        ]
    html_out = f"<h2>{_esc(title)}</h2><ol>" + "".join(f"<li>{_esc(s)}</li>" for s in steps) + "</ol>"
    if last:
        html_out += f"<p><b>{_esc(_date_label(cat))}:</b> {_esc(last)}</p>"
    return html_out


def build_faqs(job):
    """Sirf maujood facts se FAQ (Coming Soon wali cheezein shaamil nahi)."""
    name = _short_title(job["title"])
    faqs = []

    def add(q, a):
        faqs.append((q, a))

    cat = job["category"]
    if job["last_date"]:
        add(f"{name} ki last date kya hai?", f"{name} ke liye {_date_label(cat).lower()} {job['last_date']} hai.")
    if job["start_date"]:
        add(f"{name} ka form kab se bhara jayega?", f"Online application {job['start_date']} se shuru hogi.")
    if job["total_vacancies"]:
        add(f"{name} me kitni vacancy hain?", f"Is bharti me total {job['total_vacancies']} posts hain.")
    if job["qualification"] and not _is_weak(job["qualification"]):
        add(f"{name} ke liye qualification kya chahiye?", f"Qualification: {job['qualification']}.")
    if job["age_limit"]:
        add(f"{name} ki age limit kya hai?", f"Age limit: {job['age_limit']}.")
    if job["fee"]:
        add(f"{name} ki application fee kitni hai?", f"Application fee: {job['fee']}.")
    if job["salary"]:
        add(f"{name} me salary kitni milegi?", f"Salary / Pay: {job['salary']}.")
    if job["selection_process"]:
        add(f"{name} me selection kaise hoga?", f"Selection process: {job['selection_process']}.")
    if cat == "Latest Jobs":
        add(f"{name} ke liye apply kaise karein?",
            "Official website par jaakar online form bharein. Official link is page ke Important Links table me diya gaya hai.")
    return faqs[:10]


def build_page_body(job, intro):
    """Source ki jaankari se poora page body: intro, summary, sections, apply steps, FAQ."""
    parts = []
    parts.append(f"<p>{_esc(intro)}</p>")

    summary_rows = [
        ("Organisation", _cs(job["department"])),
        ("Post Name", _cs(job["post_name"])),
        ("Total Vacancies", _cs(job["total_vacancies"])),
        ("Qualification", _cs(job["qualification"])),
        ("Start Date", _cs(job["start_date"])),
        (_date_label(job["category"]) if job["category"] != "Latest Jobs" else "Last Date", _cs(job["last_date"])),
    ]
    parts.append("<h2>Quick Summary</h2>" + _simple_table(summary_rows))

    sections = job.get("sections", {})
    rendered = 0
    for kind, heading in SECTION_ORDER:
        blocks_list = sections.get(kind, [])
        if kind == "overview" and not blocks_list:
            blocks_list = sections.get("keypoints", [])
        body = "".join(_render_blocks(blocks, kind) for blocks in blocks_list)
        if body.strip():
            parts.append(f"<h2>{_esc(heading)}</h2>{body}")
            rendered += 1

    if rendered == 0:
        lines = _summary_lines(job["raw_text"])
        if lines:
            parts.append("<h2>Important Highlights</h2><ul>" + "".join(f"<li>{_esc(l)}</li>" for l in lines) + "</ul>")

    parts.append(_apply_steps(job))

    faqs = build_faqs(job)
    if faqs:
        parts.append("<h2>Frequently Asked Questions</h2>" + "".join(
            f"<h3>{_esc(q)}</h3><p>{_esc(a)}</p>" for q, a in faqs))
    return "".join(parts), faqs


def default_intro(job):
    parts = [f"{job['title']} ki poori jaankari is page par di gayi hai."]
    if job["department"]:
        parts.append(f"Yeh notification {job['department']} ki taraf se hai.")
    if job["total_vacancies"]:
        parts.append(f"Total vacancy: {job['total_vacancies']}.")
    if job["last_date"]:
        parts.append(f"{_date_label(job['category'])}: {job['last_date']}.")
    if job["qualification"] and not _is_weak(job["qualification"]):
        parts.append(f"Qualification: {job['qualification']}.")
    parts.append("Neeche important dates, eligibility, fee, selection process aur official links diye gaye hain. "
                 f"Jis detail ki jaankari abhi uplabdh nahi hai, wahan '{COMING_SOON}' likha gaya hai.")
    return " ".join(parts)


def _summary_lines(raw_text, limit=8):
    keys = re.compile(r"(?i)(vacanc|qualification|eligib|age limit|last date|closing date|start date|selection|fee|salary|pay scale|exam date)")
    seen, out = set(), []
    for line in (raw_text or "").splitlines():
        s = _safe_text(re.sub(r"\s+", " ", line))
        if 25 <= len(s) <= 220 and keys.search(s) and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _build_links_table(job, link1_label, link2_label):
    def row(label, href, text=None):
        if href and re.match(r"^https?://", href, re.IGNORECASE) and not _is_blocked_url(href) and not _is_shortener(href):
            return (
                f'<tr><td style="padding:10px;"><b>{_esc(label)}</b></td>'
                f'<td style="padding:10px;"><a href="{_esc(href)}" target="_blank" rel="noopener noreferrer">{_esc(text or "Check Here")}</a></td></tr>'
            )
        return (
            f'<tr><td style="padding:10px;"><b>{_esc(label)}</b></td>'
            f'<td style="padding:10px;">{COMING_SOON}</td></tr>'
        )

    official_site = job.get("official_site", "")
    domain = official_site.replace("https://", "").replace("http://", "").rstrip("/") if official_site else ""
    rows = [
        row("Official Website", official_site, domain or None),
        row(link1_label, job.get("pdf_url", "")),
        row(link2_label, job.get("apply_url", "")),
    ]
    for title, href in job.get("extra_links", []):
        if href and not _is_blocked_url(href):
            rows.append(row(title, href))
    return f"""
<table border="1" style="width:100%; border-collapse:collapse; margin:28px 0; font-size:0.95rem;">
  <tr style="background:#f1f5f9;">
    <th style="padding:10px; text-align:left;">Link</th>
    <th style="padding:10px; text-align:left;">Details</th>
  </tr>
  {''.join(rows)}
</table>
"""


def _meta_description(job):
    bits = []
    if job["last_date"]:
        bits.append(f"{_date_label(job['category'])} {job['last_date']}")
    if job["total_vacancies"]:
        bits.append(f"{job['total_vacancies']} vacancies")
    if job["qualification"] and not _is_weak(job["qualification"]):
        bits.append(f"qualification {job['qualification']}")
    facts = (" – " + ", ".join(bits) + ".") if bits else " –"
    desc = f"{job['title']}{facts} Complete details, important dates, fee, selection process and official links. Updated {job['post_date']}."
    return re.sub(r"\s+", " ", desc)[:300]


def generate_full_html_page(job, ai_content, faqs=None):
    page_title = f"{job['title']} | Download Link & Details 2026 - BG Sarkari Result"
    meta_desc = _meta_description(job)
    canonical_url = f"{SITE_BASE_URL}/jobs/{job['slug']}.html"

    if job['category'] == 'Result':
        link1_label = "Result / Notice PDF"
        link2_label = "Official Result Portal"
    elif job['category'] == 'Admit Card':
        link1_label = "Admit Card Notice PDF"
        link2_label = "Download Hall Ticket Portal"
    elif job['category'] == 'Answer Key':
        link1_label = "Official Answer Key PDF"
        link2_label = "Raise Objection / Response Sheet"
    else:
        link1_label = "Official Notification PDF"
        link2_label = "Apply Online Link"
    meta_label = _date_label(job['category'])

    links_table = _build_links_table(job, link1_label, link2_label)

    ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": job["title"],
        "description": meta_desc,
        "datePublished": job["post_date"],
        "dateModified": job["post_date"],
        "author": {"@type": "Organization", "name": "BG Sarkari Result"},
        "publisher": {
            "@type": "Organization",
            "name": "BG Sarkari Result",
            "logo": {"@type": "ImageObject", "url": f"{SITE_BASE_URL}/images/logo.png"},
        },
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url},
    }
    json_ld = (
        '<script type="application/ld+json">\n'
        + json.dumps(ld, ensure_ascii=False, indent=2).replace("</", "<\\/")
        + "\n</script>"
    )
    if faqs:
        faq_ld = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                for q, a in faqs
            ],
        }
        json_ld += (
            '\n<script type="application/ld+json">\n'
            + json.dumps(faq_ld, ensure_ascii=False, indent=2).replace("</", "<\\/")
            + "\n</script>"
        )

    keywords = f"{job['title']}, {job['category']}, download, official, notification, BG Sarkari Result"

    e_title = _esc(job['title'])
    e_page_title = _esc(page_title)
    e_desc = _esc(meta_desc)
    e_image = _esc(job['image'])

    full_page = f"""<!DOCTYPE html>
<html lang="hi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{e_page_title}</title>
  <meta name="description" content="{e_desc}">
  <meta name="keywords" content="{_esc(keywords)}">
  <meta name="robots" content="index, follow">
  <meta name="author" content="BG Sarkari Result">
  <link rel="canonical" href="{canonical_url}">

  <!-- Open Graph -->
  <meta property="og:type" content="article">
  <meta property="og:title" content="{e_page_title}">
  <meta property="og:description" content="{e_desc}">
  <meta property="og:image" content="{e_image}">
  <meta property="og:url" content="{canonical_url}">
  <meta property="og:site_name" content="BG Sarkari Result">

  <!-- Twitter -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{e_page_title}">
  <meta name="twitter:description" content="{e_desc}">

  <link rel="stylesheet" href="../css/style.css">
  {json_ld}
</head>
<body>

  <header>
    <div class="header-inner">
      <a href="../index.html" class="logo">
        BG <span>Sarkari</span> Result
        <span class="tag">LIVE</span>
      </a>
      <div class="controls">
        <input type="search" id="searchInput" placeholder="Search Jobs, Results, Admit Card..." aria-label="Search">
        <button id="themeToggle" aria-label="Toggle dark mode">🌙</button>
      </div>
    </div>
  </header>

  <nav>
    <div class="nav-inner">
      <a href="../index.html">Home</a>
      <a href="../jobs.html">Jobs</a>
      <a href="../results.html">Results</a>
      <a href="../admit-card.html" class="{'active' if job['category']=='Admit Card' else ''}">Admit Card</a>
      <a href="../answer-key.html">Answer Key</a>
      <a href="../syllabus.html">Syllabus</a>
      <a href="../about.html">About</a>
    </div>
  </nav>

  <main class="container">
    <h1 class="page-title">{e_title}</h1>
    <p class="page-desc">
      Category: <strong>{_esc(job['category'])}</strong> • Updated: {_esc(job['post_date'])}
    </p>

    <div class="notice" style="margin-bottom:24px;">
      📌 <strong>{meta_label}:</strong> {_esc(_cs(job['last_date']))} &nbsp;|&nbsp;
      <strong>Vacancies:</strong> {_esc(_cs(job['total_vacancies']))} &nbsp;|&nbsp;
      <strong>Qualification:</strong> {_esc(_cs(job['qualification']))}
    </div>

    <div style="text-align:center; margin-bottom:28px;">
      <img src="{e_image}" alt="{e_title} Download Link"
           width="800" height="450"
           style="max-width:100%; height:auto; border-radius:12px; max-height:380px; box-shadow:0 4px 16px rgba(0,0,0,0.1);">
    </div>

    <article class="post-content" style="line-height:1.8;">
      {ai_content}
    </article>

    <!-- Important Links Table (sirf direct official links, warna Coming Soon) -->
    <h2 style="margin-top:32px;">Important Links</h2>
    {links_table}

    <div class="notice">
      ⚠️ <strong>Disclaimer:</strong> Yeh information sirf educational purpose ke liye hai.
      Hamesha official website se verify karke download / apply karein.
    </div>
  </main>

  <footer>
    <div class="footer-links">
      <a href="../index.html">Home</a>
      <a href="../jobs.html">Jobs</a>
      <a href="../results.html">Results</a>
      <a href="../admit-card.html">Admit Card</a>
      <a href="../answer-key.html">Answer Key</a>
      <a href="../about.html">About</a>
    </div>
    <p>© 2026 BG Sarkari Result • All Rights Reserved</p>
    <p style="margin-top:6px; font-size:0.85rem;">
      Disclaimer: This website provides information for educational purposes. Always verify from official sources.
    </p>
  </footer>

  <script src="../js/script.js"></script>
</body>
</html>"""
    return full_page


# ================= GITHUB =================
def _get_existing_file(repo, file_path):
    """
    file_path GitHub par maujood hai ya nahi, yeh saaf tarike se check karta hai.
    Sirf genuine 404 (file not found) par None deta hai.
    Kisi aur error (network glitch, rate limit, etc.) par exception aage bhejta hai
    taaki caller retry kar sake — galti se "file exists nahi" na maan le.
    """
    try:
        return repo.get_contents(file_path)
    except UnknownObjectException:
        return None  # Genuinely file nahi hai


def push_to_github(file_path, content, commit_message):
    """Success par True, saari retries fail hone par False."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            repo = get_github_repo()
            existing_file = _get_existing_file(repo, file_path)

            if existing_file is not None:
                repo.update_file(existing_file.path, commit_message, content, existing_file.sha)
                logging.info(f"Updated File in Repo: {file_path}")
            else:
                try:
                    repo.create_file(file_path, commit_message, content)
                    logging.info(f"Created File in Repo: {file_path}")
                except GithubException as ce:
                    # Race condition: file abhi-abhi ban gayi (ya humein galat pata chala
                    # ki woh nahi hai). Agar GitHub keh raha hai "sha wasn't supplied",
                    # to iska matlab file asal me maujood hai — update karo.
                    if ce.status == 422:
                        logging.warning(f"Create failed (file likely exists) for {file_path}, retrying as update...")
                        fresh = repo.get_contents(file_path)
                        repo.update_file(fresh.path, commit_message, content, fresh.sha)
                        logging.info(f"Updated File in Repo (after race-condition retry): {file_path}")
                    else:
                        raise
            return True  # Success — retry loop se bahar nikal jao

        except Exception as e:
            logging.error(f"GitHub Commit Error (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                logging.error(f"GitHub Commit permanently failed for {file_path} after {MAX_RETRIES} attempts.")
    return False


def update_jobs_json(new_job):
    file_path = "data/jobs.json"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            repo = get_github_repo()
            existing_file = _get_existing_file(repo, file_path)

            if existing_file is not None:
                raw_content = existing_file.decoded_content.decode('utf-8').strip()
                if not raw_content:
                    # Fail khaali/corrupt mil gayi (jaise pehle kabhi partial write ho gayi ho).
                    # Crash hone ya bekaar retry karne ki bajaay, saaf warning ke saath
                    # ek nayi khaali list se shuru karte hain — taaki naya job save ho sake.
                    logging.warning(f"{file_path} khaali/corrupt mili — nayi list se shuru kar rahe hain.")
                    jobs_list = []
                else:
                    try:
                        jobs_list = json.loads(raw_content)
                        if not isinstance(jobs_list, list):
                            raise ValueError("jobs.json root ek list honi chahiye")
                    except (json.JSONDecodeError, ValueError) as parse_err:
                        logging.warning(f"{file_path} me invalid JSON mila ({parse_err}) — nayi list se shuru kar rahe hain.")
                        jobs_list = []
            else:
                jobs_list = []

            # Remove duplicate slug if exists
            jobs_list = [j for j in jobs_list if j.get("slug") != new_job["slug"]]
            jobs_list.insert(0, new_job)

            updated_json_str = json.dumps(jobs_list, indent=2, ensure_ascii=False)

            if existing_file is not None:
                repo.update_file(file_path, f"Update jobs.json for {new_job['slug']}", updated_json_str, existing_file.sha)
            else:
                try:
                    repo.create_file(file_path, f"Initialize jobs.json with {new_job['slug']}", updated_json_str)
                except GithubException as ce:
                    if ce.status == 422:
                        # File asal me maujood hai (race condition) — sha ke saath update karo
                        logging.warning("Create failed for jobs.json (file likely exists), retrying as update...")
                        fresh = repo.get_contents(file_path)
                        fresh_list = json.loads(fresh.decoded_content.decode('utf-8'))
                        fresh_list = [j for j in fresh_list if j.get("slug") != new_job["slug"]]
                        fresh_list.insert(0, new_job)
                        merged_json_str = json.dumps(fresh_list, indent=2, ensure_ascii=False)
                        repo.update_file(file_path, f"Update jobs.json for {new_job['slug']}", merged_json_str, fresh.sha)
                    else:
                        raise

            logging.info(f"jobs.json updated for: {new_job['slug']}")
            return  # Success

        except Exception as e:
            logging.error(f"JSON Sync Error (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                logging.error(f"jobs.json update permanently failed after {MAX_RETRIES} attempts.")


def update_sitemap(new_url):
    file_path = "sitemap.xml"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            repo = get_github_repo()
            existing_file = _get_existing_file(repo, file_path)

            if existing_file is not None:
                xml_str = existing_file.decoded_content.decode('utf-8')
            else:
                xml_str = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
</urlset>'''

            if new_url in xml_str:
                return  # Already present, kuch karne ki zaroorat nahi

            url_node = f"""  <url>
    <loc>{new_url}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>"""
            updated_xml = xml_str.replace("</urlset>", url_node)

            if existing_file is not None:
                repo.update_file(file_path, f"Update sitemap for {new_url}", updated_xml, existing_file.sha)
            else:
                try:
                    repo.create_file(file_path, f"Create sitemap with {new_url}", updated_xml)
                except GithubException as ce:
                    if ce.status == 422:
                        logging.warning("Create failed for sitemap.xml (file likely exists), retrying as update...")
                        fresh = repo.get_contents(file_path)
                        fresh_xml = fresh.decoded_content.decode('utf-8')
                        if new_url not in fresh_xml:
                            merged_xml = fresh_xml.replace("</urlset>", url_node)
                            repo.update_file(file_path, f"Update sitemap for {new_url}", merged_xml, fresh.sha)
                    else:
                        raise

            logging.info("Sitemap updated successfully!")
            return  # Success

        except Exception as e:
            logging.error(f"Sitemap Update Error (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                logging.error(f"Sitemap update permanently failed after {MAX_RETRIES} attempts.")


# ================= MAIN FLOW =================
async def process_and_publish(job_source_url):
    job = scrape_job_details(job_source_url)

    # Regex / sections se jo fields nahi mile, AI se (source text ke andar se) nikalwao
    ai_fill_missing_fields(job)

    # Galat / khaali data wale pages publish hone se pehle roko (aur source/aggregator links hatao)
    validate_job_data(job)

    # Page body: source ke facts se sections (AI fail ho tab bhi page bhara hua banta hai).
    # AI sirf ek chhota Hinglish intro likhta hai; fail ho to default intro lagta hai.
    intro = ai_intro(job) or default_intro(job)
    body_html, faqs = build_page_body(job, intro)

    full_html = generate_full_html_page(job, body_html, faqs)

    html_repo_path = f"jobs/{job['slug']}.html"
    if not push_to_github(html_repo_path, full_html, f"Add/Update page: {job['slug']}"):
        raise RuntimeError(f"GitHub par page push nahi ho paya: {html_repo_path}")

    page_url = f"{SITE_BASE_URL}/jobs/{job['slug']}.html"

    # NOTE: jobs.json me khaali values khaali hi rakhi hain (site ki listing / filter ke liye);
    # "Coming Soon" sirf page aur Telegram par dikhta hai. source_url public JSON me nahi daalte.
    json_entry = {
        "title": job["title"],
        "slug": job["slug"],
        "vacancies": job["total_vacancies"],
        "qualification": job["qualification"],
        "last_date": job["last_date"],
        "start_date": job["start_date"],
        "age_limit": job["age_limit"],
        "salary": job["salary"],
        "fee": job["fee"],
        "department": job["department"],
        "post_name": job["post_name"],
        "notification_url": job["pdf_url"],
        "apply_url": job["apply_url"],
        "official_site": job["official_site"],
        "category": job["category"],
        "page_url": page_url,
        "post_date": job["post_date"]
    }
    update_jobs_json(json_entry)
    update_sitemap(page_url)

    # Telegram Notification
    lines = [
        f"📢 <b>New Update ({_esc(job['category'], quote=False)}) 2026</b>\n",
        f"🎯 <b>{_esc(job['title'], quote=False)}</b>\n",
        f"📅 {_date_label(job['category'])}: {_esc(_cs(job['last_date']), quote=False)}",
        f"👥 Vacancies: {_esc(_cs(job['total_vacancies']), quote=False)}",
    ]
    if job["qualification"] and len(job["qualification"]) <= 80 and not _is_weak(job["qualification"]):
        lines.append(f"🎓 Qualification: {_esc(job['qualification'], quote=False)}")
    lines.append(f"\n📄 Complete Details & Links:\n{page_url}")
    tg_caption = "\n".join(lines)

    try:
        tg_bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await tg_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=tg_caption, parse_mode="HTML")
        logging.info("Telegram Notification Sent Successfully!")
    except Exception as e:
        logging.error(f"Telegram Notification Error: {e}")

    logging.info(f"✅ Successfully published: {page_url}")


async def _notify_telegram_plain(message: str):
    """Crash/error jaisi situations me seedha Telegram par bata deta hai."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        tg_bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await tg_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Telegram notify failed: {e}")


def _check_input_url(url):
    """Galat paste hue link (jaise ek link ke beech me doosra link) ko pehle hi pakad leta hai."""
    if len(re.findall(r"https?://", url, flags=re.IGNORECASE)) > 1:
        return "Link me do URL mil gaye (ek link ke beech me doosra link paste ho gaya)."
    if re.search(r"\s", url):
        return "Link me space hai."
    host = _host(url)
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return f"Link ka domain galat hai: {host}"
    return ""


if __name__ == "__main__":
    import sys

    # GitHub Actions ke liye: URL env var (JOB_URL) se aata hai.
    # Agar woh nahi mila to command line arg, warna (local testing ke liye) manually poochega.
    url_input = os.getenv("JOB_URL", "").strip()
    if not url_input and len(sys.argv) > 1:
        url_input = sys.argv[1].strip()
    if not url_input:
        url_input = input("Enter Source Job URL: ").strip()

    if url_input.startswith("http"):
        problem = _check_input_url(url_input)
        if problem:
            print(problem)
            asyncio.run(_notify_telegram_plain(
                f"⚠️ <b>Galat URL</b>\n{_esc(problem, quote=False)}\n"
                f"<code>{_esc(url_input[:300], quote=False)}</code>\n"
                "Link dobara saaf paste karke workflow chalayein."
            ))
            raise SystemExit(1)
        try:
            asyncio.run(process_and_publish(url_input))
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            asyncio.run(_notify_telegram_plain(
                f"❌ <b>Automation CRASH ho gaya</b>\n<code>{_esc(e, quote=False)}</code>"
            ))
            raise
    else:
        print("Please enter a valid HTTP/HTTPS URL.")
        if url_input == "":
            asyncio.run(_notify_telegram_plain("⚠️ Koi job source URL provide nahi hui."))
