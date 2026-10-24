import asyncio
from datetime import datetime, timezone
import html as htmllib
import json
import logging
import os
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from bs4 import BeautifulSoup, Comment
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

# AI ke liye models (ek ke baad ek try hote hain) aur kitne round retry karne hain
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

# Source page ki aisi lines jo AI ko nahi bhejni (promo / social / source site ka naam)
JUNK_LINE_RE = re.compile(
    r"(?i)(telegram|whatsapp|join\s+(?:us|our|now)|follow\s+us|subscribe|youtube\s+channel|facebook|instagram|"
    r"free\s*job\s*alert|freejobalert|sarkari\s*result|sarkariresult|sarkari\s*exam|download\s+(?:our\s+)?app|"
    r"share\s+this|click\s+here\s+to\s+join|copyright|all\s+rights\s+reserved|disclaimer\s*:)"
)

# AI ne "N/A", "Not mentioned" jaisa likha to use "Coming Soon" bana dete hain
EMPTY_CELL_RE = re.compile(
    r"^\s*(?:n/?a|na|not\s+available|not\s+mentioned(?:\s+in\s+(?:the\s+)?source)?|not\s+specified|not\s+declared|"
    r"tba|tbd|to\s+be\s+(?:announced|notified|declared)|updating\s+soon)\s*\.?\s*$",
    re.IGNORECASE,
)

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


def _cs(value):
    """Khaali value ki jagah 'Coming Soon' dikhane ke liye."""
    value = (str(value).strip() if value is not None else "")
    return value if value else COMING_SOON


def _clean_value(value):
    if value is None:
        return ""
    value = re.sub(r"\s+", " ", str(value)).strip(" :|-–—")
    if not value or value.lower() in {
        "n/a", "na", "not available", "not mentioned", "various", "various posts", "tba", "tbd",
        "to be announced", "coming soon", "update soon", "updating soon", "-", "--",
    }:
        return ""
    return value


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
    if any(x in t for x in ["apply online", "online application", "apply now", "application form",
                            "registration", "apply link", "login", "apply"]):
        return "apply"
    if any(x in t for x in ["notification", "advertisement", "advt", "notice", "brochure", "download pdf"]):
        return "notification"
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
            kind = "notification"
        elif _is_official_domain(href):
            kind = "website"
    return kind


def _extract_explicit_urls(text):
    return re.findall(r'https?://[^\s<>"\']+', text or "", flags=re.IGNORECASE)


def _collect_link_candidates(scope, base_url):
    """(label, href) pairs. Table me row ka pehla cell label banta hai ("Click Here" ke bajaay)."""
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
            label = a_text if in_first else f"{row_label} {a_text}"
            cands.append((label, _normalize_url(base_url, a.get("href"))))
            seen.add(id(a))

    for a in scope.find_all("a", href=True):
        if id(a) in seen or not ok_href(a.get("href")):
            continue
        label = a.get_text(" ", strip=True)
        parent = a.parent
        ptxt = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)) if parent else ""
        if ptxt and len(ptxt) <= 120:
            label = ptxt
        cands.append((label, _normalize_url(base_url, a.get("href"))))
    return cands


def _first(items):
    return items[0] if items else ""


def _extract_links(scope, base_url, source_url, category, session=None):
    found = {"notification": [], "apply": [], "result": [], "admit": [], "answer_key": [], "website": []}
    for label, href in _collect_link_candidates(scope, base_url):
        if _kind_from_label(label) or _is_shortener(href):
            href = _resolve_redirect(href, session)
        kind = _classify_link(label, href, source_url)
        if kind:
            found[kind].append(href)

    expected = {"Result": "result", "Admit Card": "admit", "Answer Key": "answer_key"}.get(category, "apply")
    links = {
        "pdf_url": _first(found["notification"]),
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
    return links


# ================= FIELD EXTRACTION =================
def _assign_field(fields, key, value):
    key = re.sub(r"\s+", " ", (key or "").lower()).strip(" :-|")
    value = _clean_value(value)
    if not key or not value or len(key) > 60 or len(value) > 600:
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
    elif any(x in key for x in ["start date", "starting date", "application start", "application begin",
                                "apply start", "begin date", "date of start"]):
        name = "start_date"
    elif any(x in key for x in ["salary", "pay scale", "pay level", "pay matrix", "stipend"]):
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

    if name == "total_vacancies" and (not re.search(r"\d", value) or len(value) > 80):
        return
    if name in ("last_date", "start_date") and len(value) > 200:
        return
    if not fields[name]:
        fields[name] = value


def _extract_structured_fields(scope, text, title=""):
    fields = {k: "" for k in FIELD_KEYS}

    # 1) Table rows: key | value
    for table in scope.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
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
    fields["salary"] = fields["salary"] or _first_match(text, [r"(?:salary|pay\s+scale|pay\s+level)\s*[:\-]\s*([^\n]+)"])
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
    t = re.sub(r"\s*[-|–—:]\s*$", "", t)
    t = re.sub(r"^\s*[-|–—:]\s*", "", t)
    return t.strip()


def _detect_category(title):
    title_lower = (title or "").lower()
    if any(k in title_lower for k in ["admit card", "hall ticket", "call letter", "city intimation"]):
        return "Admit Card"
    if any(k in title_lower for k in ["result", "scorecard", "merit list", "selection list", "cut off"]):
        return "Result"
    if any(k in title_lower for k in ["answer key", "response sheet"]):
        return "Answer Key"
    return "Latest Jobs"


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
        "official_site": "", "category": "Latest Jobs"
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
        data.update(fields)

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
        if not any(links.values()) and entry_content is not soup:
            links = _extract_links(soup, res.url, url, data["category"], session)
        data.update({k: v for k, v in links.items() if v})

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

        logging.info("Extracted facts: vacancies=%r qualification=%r age=%r start=%r last=%r salary=%r fee=%r notification=%r apply=%r",
                     data["total_vacancies"], data["qualification"], data["age_limit"], data["start_date"],
                     data["last_date"], data["salary"], data["fee"], data["pdf_url"], data["apply_url"])

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


def sanitize_ai_html(body):
    """AI ke HTML se code fences, source/social links, source site ke naam aur 'N/A' hatata hai."""
    body = re.sub(r"^```(?:html)?\s*", "", body or "", flags=re.IGNORECASE | re.MULTILINE)
    body = re.sub(r"```\s*$", "", body, flags=re.MULTILINE)
    body = re.sub(r"<p>```.*?</p>", "", body, flags=re.DOTALL)
    body = body.strip()
    if not body:
        return ""

    soup = BeautifulSoup(body, "html.parser")

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not re.match(r"^https?://", href, re.IGNORECASE) or _is_blocked_url(href) or _is_shortener(href):
            a.unwrap()

    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment):
            continue
        parent = node.parent.name if node.parent else ""
        if parent in ("script", "style"):
            continue
        text = str(node)
        new = text
        if EMPTY_CELL_RE.match(text):
            new = COMING_SOON
        elif parent in ("td", "th") and re.fullmatch(r"\s*[-–—]+\s*", text):
            new = COMING_SOON
        else:
            new = re.sub(r"(?i)not\s+mentioned(?:\s+in\s+(?:the\s+)?source)?", COMING_SOON, new)
            new = _strip_site_names(new)
        if new != text:
            node.replace_with(new)

    return str(soup).strip()


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

    if job.get("category") == "Latest Jobs" and not any(
        job.get(x) for x in ["total_vacancies", "qualification", "last_date", "age_limit", "salary", "start_date"]
    ):
        problems.append("no structured recruitment facts extracted")

    if problems:
        raise RuntimeError("Publish validation failed: " + "; ".join(problems))


def _guess_official_site(title: str) -> str:
    # Disabled: official domains must come from actual source links.
    return ""


# ================= PAGE BUILDING =================
def _date_label(category):
    if category == "Result":
        return "Released"
    if category == "Admit Card":
        return "Available from"
    if category == "Answer Key":
        return "Released"
    return "Last Date"


def _summary_lines(raw_text, limit=8):
    keys = re.compile(r"(?i)(vacanc|qualification|eligib|age limit|last date|closing date|start date|selection|fee|salary|pay scale|exam date)")
    seen, out = set(), []
    for line in (raw_text or "").splitlines():
        s = re.sub(r"\s+", " ", line).strip()
        if 25 <= len(s) <= 220 and keys.search(s) and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= limit:
            break
    return out


def build_fallback_body(job):
    """AI na chale to sirf source ke facts se page body; jo detail nahi mili wahan 'Coming Soon'."""
    rows = [
        ("Total Vacancies", job["total_vacancies"]),
        ("Start Date", job["start_date"]),
        ("Last Date", job["last_date"]),
        ("Qualification", job["qualification"]),
        ("Age Limit", job["age_limit"]),
        ("Salary / Pay Scale", job["salary"]),
        ("Application Fee", job["fee"]),
        ("Selection Process", job["selection_process"]),
        ("Department", job["department"]),
        ("Post Name", job["post_name"]),
    ]
    table_rows = "".join(
        f'<tr><td style="padding:10px;"><b>{_esc(label)}</b></td><td style="padding:10px;">{_esc(_cs(value))}</td></tr>'
        for label, value in rows
    )
    highlights = _summary_lines(job["raw_text"])
    highlights_html = ""
    if highlights:
        highlights_html = "<h2>Important Highlights</h2><ul>" + "".join(f"<li>{_esc(h)}</li>" for h in highlights) + "</ul>"

    return (
        f"<h2>Overview</h2><p>{_esc(job['title'])} ki jaankari neeche di gayi hai. "
        f"Jis detail ki official jaankari abhi uplabdh nahi hai, wahan '{COMING_SOON}' likha gaya hai.</p>"
        '<table border="1" style="width:100%; border-collapse:collapse; margin:20px 0; font-size:0.95rem;">'
        '<tr style="background:#f1f5f9;"><th style="padding:10px;text-align:left;">Particulars</th>'
        '<th style="padding:10px;text-align:left;">Details</th></tr>'
        f"{table_rows}</table>"
        f"{highlights_html}"
    )


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
    return f"""
<table border="1" style="width:100%; border-collapse:collapse; margin:28px 0; font-size:0.95rem;">
  <tr style="background:#f1f5f9;">
    <th style="padding:10px; text-align:left;">Link</th>
    <th style="padding:10px; text-align:left;">Details</th>
  </tr>
  {''.join(rows)}
</table>
"""


def generate_full_html_page(job, ai_content):
    page_title = f"{job['title']} | Download Link & Details 2026 - BG Sarkari Result"
    meta_desc = f"{job['title']} – complete details, important dates, how to check/download and official links. Updated {job['post_date']}."
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
    if job["title"].startswith("Latest Government Job Recruitment Notification"):
        await _notify_telegram_plain(
            "⚠️ Scraping thik se nahi hui (generic title use ho raha hai)."
        )

    # Regex se jo fields nahi mile, AI se (source text ke andar se) nikalwao
    ai_fill_missing_fields(job)

    # Galat / khaali data wale pages publish hone se pehle roko (aur source/aggregator links hatao)
    validate_job_data(job)

    # ================= SEO-FRIENDLY AI PROMPT =================
    blog_prompt = f"""
You are an expert Indian Sarkari Job content writer and SEO specialist.
Write a clean, professional, SEO-optimized HTML article body for this notification:

Title: {job['title']}
Category: {job['category']}
Total Vacancies: {_cs(job['total_vacancies'])}
Last Date / Available from: {_cs(job['last_date'])}
Start Date: {_cs(job['start_date'])}
Qualification: {_cs(job['qualification'])}
Age Limit: {_cs(job['age_limit'])}
Source Raw Text: {job['raw_text'][:12000]}
Extracted Salary: {_cs(job['salary'])}
Extracted Fee: {_cs(job['fee'])}
Extracted Selection Process: {_cs(job['selection_process'])}
Extracted Department: {_cs(job['department'])}
Extracted Post Name: {_cs(job['post_name'])}
Extracted Official Website: {_cs(job['official_site'])}
Extracted Notification URL: {_cs(job['pdf_url'])}
Extracted Apply URL: {_cs(job['apply_url'])}

STRICT RULES (MUST FOLLOW):
1. Return ONLY raw HTML. No markdown, no ```html, no code fences.
2. Start directly with content. Do NOT repeat the full title in the first paragraph.
3. Use proper semantic HTML:
   - <h2> for main sections
   - <h3> for sub-sections
   - <p> for paragraphs
   - <ul> / <ol> for lists
   - Real HTML <table> (never markdown table)
4. Table style (copy exactly):
   <table border="1" style="width:100%; border-collapse:collapse; margin:20px 0; font-size:0.95rem;">
     <tr style="background:#f1f5f9;"><th style="padding:10px; text-align:left;">Particulars</th><th style="padding:10px; text-align:left;">Details</th></tr>
     ...
   </table>
5. Write in simple, natural Hinglish (easy for students).
6. Include these sections in order (if relevant):
   - Short Introduction (2-3 lines) – title ke related, koi galat exam name mat likho
   - Important Overview Table (Exam Name, Category, Vacancies, Date, Qualification)
   - Important Dates / Exam Schedule (agar raw text me mile)
   - How to Check Result / Download Admit Card / Apply (step-by-step <ol>) – category ke hisaab se
   - Required Documents (agar relevant)
   - Important Instructions / Notes
7. Naturally use keywords from the actual title: "{job['title']}", "download", "official", "{job['category']}".
8. Keep total length 450-700 words. Mobile-friendly short paragraphs.
9. NEVER invent RRB / Group D / CEN details unless the title itself contains them.
10. Do NOT include any links table or "Important Links" section – that is added by the template.
11. If any detail is missing or unknown (shown as "{COMING_SOON}" above), write exactly "{COMING_SOON}" for it.
    NEVER write "Not mentioned", "N/A", "TBA" or guess a value.
12. NEVER mention any other website or aggregator name (like FreeJobAlert or SarkariResult) and NEVER add any hyperlinks.
"""

    ai_raw = get_ai_response(blog_prompt, rounds=3)
    ai_html_body = sanitize_ai_html(ai_raw) if ai_raw else ""

    # AI unavailable ya bekaar output: sirf factual source-based summary (guess nahi), missing = Coming Soon
    visible_len = len(BeautifulSoup(ai_html_body, "html.parser").get_text(strip=True)) if ai_html_body else 0
    if visible_len < 150 or not any(t in ai_html_body.lower() for t in ("<h2", "<p", "<table")):
        logging.warning("AI article usable nahi mila — fallback factual summary use ho rahi hai.")
        ai_html_body = build_fallback_body(job)

    full_html = generate_full_html_page(job, ai_html_body)

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
    tg_caption = (
        f"📢 <b>New Update ({_esc(job['category'], quote=False)}) 2026</b>\n\n"
        f"🎯 <b>{_esc(job['title'], quote=False)}</b>\n\n"
        f"📅 {_date_label(job['category'])}: {_esc(_cs(job['last_date']), quote=False)}\n"
        f"👥 Vacancies: {_esc(_cs(job['total_vacancies']), quote=False)}\n\n"
        f"📄 Complete Details & Links:\n{page_url}"
    )

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
