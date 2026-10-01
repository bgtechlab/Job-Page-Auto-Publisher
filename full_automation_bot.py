import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import time
from bs4 import BeautifulSoup
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


def get_github_repo():
    auth = Auth.Token(GITHUB_TOKEN)
    g = Github(auth=auth)
    return g.get_repo(REPO_NAME)


def slugify(text):
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[\s_-]+', '-', text).strip('-')


def scrape_job_details(url):
    data = {
        "title": "",
        "slug": "",
        "post_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_vacancies": "Various Posts",
        "qualification": "10th / 12th / Graduate",
        "age_limit": "18 to 35 Years",
        "last_date": "Check Official Portal",
        "image": DEFAULT_FALLBACK_IMAGE,
        "raw_text": "",
        "apply_url": "",
        "pdf_url": "",
        "category": "Latest Jobs"
    }

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        res = session.get(url, timeout=20)
        soup = BeautifulSoup(res.content, "html.parser")

        title_elem = soup.find("h1") or soup.find("h2", {"class": "entry-title"}) or soup.find("meta", {"property": "og:title"})
        if title_elem:
            raw_title = title_elem.get("content") if title_elem.name == "meta" else title_elem.get_text()
            data["title"] = re.sub(r"\s*-\s*FreeJobAlert.*$", "", raw_title, flags=re.IGNORECASE).strip()
            data["slug"] = slugify(data["title"])

        entry_content = soup.find("div", {"class": "entry-content"}) or soup.find("article")
        if entry_content:
            data["raw_text"] = entry_content.get_text(separator="\n", strip=True)[:3000]

            vac_match = re.search(r'Total\s+Vacanc[y|ies]\s*:\s*(\d+)', data["raw_text"], re.IGNORECASE)
            if vac_match:
                data["total_vacancies"] = vac_match.group(1)

            date_match = re.search(r'Last\s+Date\s*:\s*([\d\-\/]+|[A-Za-z0-9\s]+202\d)', data["raw_text"], re.IGNORECASE)
            if date_match:
                data["last_date"] = date_match.group(1).strip()

        data["apply_url"] = url
        data["pdf_url"] = url

        title_lower = data["title"].lower()
        if any(keyword in title_lower for keyword in ["admit card", "hall ticket", "call letter", "city intimation"]):
            data["category"] = "Admit Card"
        elif any(keyword in title_lower for keyword in ["result", "scorecard", "merit list", "selection list", "cut off"]):
            data["category"] = "Result"
        elif any(keyword in title_lower for keyword in ["answer key", "response sheet"]):
            data["category"] = "Answer Key"
        else:
            data["category"] = "Latest Jobs"

    except Exception as e:
        logging.error(f"Scraping Error: {e}")

    if not data["title"]:
        data["title"] = "Latest Government Job Recruitment Notification 2026"
        data["slug"] = f"govt-job-{int(time.time())}"

    return data


def get_ai_response(prompt):
    for model_name in ["gpt-4o-mini", "gpt-3.5-turbo"]:
        try:
            res = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}]
            )
            content = res.choices[0].message.content
            if content and len(content.strip()) > 20:
                return content
        except Exception:
            continue
    return ""


def _guess_official_site(title: str) -> str:
    """Title se common boards/commission ka official domain guess karta hai."""
    t = title.lower()
    mapping = [
        (["hpsc", "haryana public service"], "https://hpsc.gov.in"),
        (["uppsc", "uttar pradesh public service"], "https://uppsc.up.nic.in"),
        (["bpsc", "bihar public service"], "https://www.bpsc.bih.nic.in"),
        (["mpsc", "maharashtra public service"], "https://mpsc.gov.in"),
        (["rrb", "railway recruitment"], "https://www.rrbcdg.gov.in"),
        (["ssc", "staff selection commission"], "https://ssc.gov.in"),
        (["upsc"], "https://www.upsc.gov.in"),
        (["ibps"], "https://www.ibps.in"),
        (["ctet", "cbse"], "https://ctet.nic.in"),
        (["neet", "nnta"], "https://neet.nta.nic.in"),
        (["jee"], "https://jeemain.nta.nic.in"),
    ]
    for keys, url in mapping:
        if any(k in t for k in keys):
            return url
    return ""


def generate_full_html_page(job, ai_content):
    page_title = f"{job['title']} | Download Link & Details 2026 - BG Sarkari Result"
    meta_desc = f"{job['title']} – complete details, important dates, how to check/download and official links. Updated {job['post_date']}."
    canonical_url = f"{SITE_BASE_URL}/jobs/{job['slug']}.html"

    if job['category'] == 'Result':
        link1_label = "Result / Notice PDF"
        link2_label = "Official Result Portal"
        meta_label = "Released"
    elif job['category'] == 'Admit Card':
        link1_label = "Admit Card Notice PDF"
        link2_label = "Download Hall Ticket Portal"
        meta_label = "Available from"
    elif job['category'] == 'Answer Key':
        link1_label = "Official Answer Key PDF"
        link2_label = "Raise Objection / Response Sheet"
        meta_label = "Released"
    else:
        link1_label = "Official Notification PDF"
        link2_label = "Apply Online Link"
        meta_label = "Last Date"

    official_site = _guess_official_site(job['title'])
    # Links table – user ke requested format jaisa
    links_rows = []
    if official_site:
        domain = official_site.replace("https://", "").replace("http://", "").rstrip("/")
        links_rows.append(
            f'<tr><td style="padding:10px;"><b>Official Website</b></td>'
            f'<td style="padding:10px;"><a href="{official_site}" target="_blank" rel="noopener noreferrer">{domain}</a></td></tr>'
        )
    links_rows.append(
        f'<tr><td style="padding:10px;"><b>{link1_label}</b></td>'
        f'<td style="padding:10px;"><a href="{job["pdf_url"]}" target="_blank" rel="noopener noreferrer">Check Here</a></td></tr>'
    )
    links_rows.append(
        f'<tr><td style="padding:10px;"><b>{link2_label}</b></td>'
        f'<td style="padding:10px;"><a href="{job["apply_url"]}" target="_blank" rel="noopener noreferrer">Check Here</a></td></tr>'
    )

    links_table = f"""
<table border="1" style="width:100%; border-collapse:collapse; margin:28px 0; font-size:0.95rem;">
  <tr style="background:#f1f5f9;">
    <th style="padding:10px; text-align:left;">Link</th>
    <th style="padding:10px; text-align:left;">Details</th>
  </tr>
  {''.join(links_rows)}
</table>
"""

    safe_title = job['title'].replace('"', "'")
    json_ld = f"""
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "Article",
  "headline": "{safe_title}",
  "description": "{meta_desc}",
  "datePublished": "{job['post_date']}",
  "dateModified": "{job['post_date']}",
  "author": {{
    "@type": "Organization",
    "name": "BG Sarkari Result"
  }},
  "publisher": {{
    "@type": "Organization",
    "name": "BG Sarkari Result",
    "logo": {{
      "@type": "ImageObject",
      "url": "{SITE_BASE_URL}/images/logo.png"
    }}
  }},
  "mainEntityOfPage": {{
    "@type": "WebPage",
    "@id": "{canonical_url}"
  }}
}}
</script>
"""

    keywords = f"{job['title']}, {job['category']}, download, official, notification, BG Sarkari Result"

    full_page = f"""<!DOCTYPE html>
<html lang="hi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{page_title}</title>
  <meta name="description" content="{meta_desc}">
  <meta name="keywords" content="{keywords}">
  <meta name="robots" content="index, follow">
  <meta name="author" content="BG Sarkari Result">
  <link rel="canonical" href="{canonical_url}">

  <!-- Open Graph -->
  <meta property="og:type" content="article">
  <meta property="og:title" content="{page_title}">
  <meta property="og:description" content="{meta_desc}">
  <meta property="og:image" content="{job['image']}">
  <meta property="og:url" content="{canonical_url}">
  <meta property="og:site_name" content="BG Sarkari Result">

  <!-- Twitter -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{page_title}">
  <meta name="twitter:description" content="{meta_desc}">

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
    <h1 class="page-title">{job['title']}</h1>
    <p class="page-desc">
      Category: <strong>{job['category']}</strong> • Updated: {job['post_date']}
    </p>

    <div class="notice" style="margin-bottom:24px;">
      📌 <strong>{meta_label}:</strong> {job['last_date']} &nbsp;|&nbsp;
      <strong>Vacancies:</strong> {job['total_vacancies']} &nbsp;|&nbsp;
      <strong>Qualification:</strong> {job['qualification']}
    </div>

    <div style="text-align:center; margin-bottom:28px;">
      <img src="{job['image']}" alt="{job['title']} Download Link"
           width="800" height="450"
           style="max-width:100%; height:auto; border-radius:12px; max-height:380px; box-shadow:0 4px 16px rgba(0,0,0,0.1);">
    </div>

    <article class="post-content" style="line-height:1.8;">
      {ai_content}
    </article>

    <!-- Important Links Table (user requested format) -->
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
            return  # Success — retry loop se bahar nikal jao

        except Exception as e:
            logging.error(f"GitHub Commit Error (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                logging.error(f"GitHub Commit permanently failed for {file_path} after {MAX_RETRIES} attempts.")


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


async def process_and_publish(job_source_url):
    job = scrape_job_details(job_source_url)
    if job["title"].startswith("Latest Government Job Recruitment Notification"):
        await _notify_telegram_plain(
            f"⚠️ Scraping thik se nahi hui (generic title use ho raha hai).\n🔗 {job_source_url}"
        )

    # ================= SEO-FRIENDLY AI PROMPT =================
    blog_prompt = f"""
You are an expert Indian Sarkari Job content writer and SEO specialist.
Write a clean, professional, SEO-optimized HTML article body for this notification:

Title: {job['title']}
Category: {job['category']}
Total Vacancies: {job['total_vacancies']}
Last Date / Available from: {job['last_date']}
Qualification: {job['qualification']}
Age Limit: {job['age_limit']}
Source Raw Text: {job['raw_text'][:2500]}

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
"""

    ai_html_body = get_ai_response(blog_prompt)

    # Strong cleanup
    ai_html_body = re.sub(r"^```(?:html)?\s*", "", ai_html_body, flags=re.IGNORECASE | re.MULTILINE)
    ai_html_body = re.sub(r"```\s*$", "", ai_html_body, flags=re.MULTILINE)
    ai_html_body = re.sub(r"<p>```.*?</p>", "", ai_html_body, flags=re.DOTALL)
    ai_html_body = ai_html_body.strip()

    # High-quality generic fallback if AI fails (title + category based)
    if not ai_html_body or len(ai_html_body) < 100:
        cat = job['category']
        if cat == "Result":
            how_title = f"How to Check {job['title']}"
            how_steps = """
  <li>Official website par jaayein (neeche Important Links me diya gaya hai).</li>
  <li>Result / Final Result link par click karein.</li>
  <li>Registration Number / Roll Number aur Date of Birth enter karein.</li>
  <li>Captcha fill karke Submit button dabayein.</li>
  <li>Result download karke print nikaal lein ya save kar lein.</li>
"""
            intro = f"<p>{job['title']} release ho chuka hai. Candidates neeche diye gaye steps follow karke apna result check kar sakte hain.</p>"
            instructions = """
  <li>Result ke saath original Photo ID proof rakh lein verification ke liye.</li>
  <li>Cut-off aur merit list carefully check karein.</li>
  <li>Koi bhi galat information hone par official helpline se contact karein.</li>
"""
        elif cat == "Admit Card":
            how_title = f"How to Download {job['title']}"
            how_steps = """
  <li>Official website par jaayein.</li>
  <li>Admit Card / Hall Ticket link par click karein.</li>
  <li>Registration Number / Roll Number aur Date of Birth enter karein.</li>
  <li>Captcha fill karke Submit button dabayein.</li>
  <li>Admit Card download karke print nikaal lein.</li>
"""
            intro = f"<p>{job['title']} available ho gaya hai. Candidates neeche diye gaye steps follow karke apna admit card download kar sakte hain.</p>"
            instructions = """
  <li>Admit Card ke saath original Photo ID proof zaroor lein.</li>
  <li>Exam centre time se 1 ghanta pehle pahunch jaayein.</li>
  <li>Mobile phone, calculator ya koi electronic device allowed nahi hai.</li>
"""
        else:
            how_title = f"How to Apply / Download – {job['title']}"
            how_steps = """
  <li>Official website par jaayein (neeche Important Links me diya gaya hai).</li>
  <li>Notification / Apply Online link par click karein.</li>
  <li>Required details carefully fill karein.</li>
  <li>Documents upload karein aur fee (agar applicable) pay karein.</li>
  <li>Application form download / print karke safe rakh lein.</li>
"""
            intro = f"<p>{job['title']} related update available hai. Candidates neeche diye gaye steps follow karke complete details dekh sakte hain.</p>"
            instructions = """
  <li>Hamesha official website se hi apply / download karein.</li>
  <li>Last date se pehle form submit kar lein.</li>
  <li>Personal details aur documents carefully check karein.</li>
"""

        ai_html_body = f"""
<h2>Overview</h2>
{intro}

<table border="1" style="width:100%; border-collapse:collapse; margin:20px 0; font-size:0.95rem;">
  <tr style="background:#f1f5f9;">
    <th style="padding:10px; text-align:left;">Particulars</th>
    <th style="padding:10px; text-align:left;">Details</th>
  </tr>
  <tr>
    <td style="padding:10px;"><b>Exam / Notification</b></td>
    <td style="padding:10px;">{job['title']}</td>
  </tr>
  <tr>
    <td style="padding:10px;"><b>Category</b></td>
    <td style="padding:10px;">{job['category']}</td>
  </tr>
  <tr>
    <td style="padding:10px;"><b>Total Vacancies</b></td>
    <td style="padding:10px;">{job['total_vacancies']}</td>
  </tr>
  <tr>
    <td style="padding:10px;"><b>Date / Status</b></td>
    <td style="padding:10px;">{job['last_date']}</td>
  </tr>
  <tr>
    <td style="padding:10px;"><b>Qualification</b></td>
    <td style="padding:10px;">{job['qualification']}</td>
  </tr>
</table>

<h2>{how_title}</h2>
<ol>
{how_steps}
</ol>

<h2>Important Instructions</h2>
<ul>
{instructions}
</ul>
"""

    full_html = generate_full_html_page(job, ai_html_body)

    # ... baaki code same (push, json, sitemap, telegram)
    html_repo_path = f"jobs/{job['slug']}.html"
    push_to_github(html_repo_path, full_html, f"Add/Update page: {job['slug']}")

    page_url = f"{SITE_BASE_URL}/jobs/{job['slug']}.html"

    json_entry = {
        "title": job["title"],
        "slug": job["slug"],
        "vacancies": job["total_vacancies"],
        "qualification": job["qualification"],
        "last_date": job["last_date"],
        "category": job["category"],
        "page_url": page_url,
        "post_date": job["post_date"]
    }
    update_jobs_json(json_entry)
    update_sitemap(page_url)

    # Telegram Notification
    tg_caption = (
        f"📢 <b>New Update ({job['category']}) 2026</b>\n\n"
        f"🎯 <b>{job['title']}</b>\n\n"
        f"📅 {job['last_date']}\n"
        f"👥 Vacancies: {job['total_vacancies']}\n\n"
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
        try:
            asyncio.run(process_and_publish(url_input))
        except Exception as e:
            import traceback
            print(traceback.format_exc())
            asyncio.run(_notify_telegram_plain(f"❌ <b>Automation CRASH ho gaya</b>\n<code>{e}</code>"))
            raise
    else:
        print("Please enter a valid HTTP/HTTPS URL.")
        if url_input == "":
            asyncio.run(_notify_telegram_plain("⚠️ Koi job source URL provide nahi hui."))