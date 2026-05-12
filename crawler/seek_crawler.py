import os
import re
import json
import html
import time
import sqlite3
import requests
from datetime import date
from dotenv import load_dotenv
load_dotenv()

APIFY_API_KEY = os.getenv("APIFY_API_KEY")
ACTOR_ID      = "websift~seek-job-scraper"
LI_ACTOR_ID   = "bebity/linkedin-jobs-scraper"
IND_ACTOR_ID  = "misceres~indeed-scraper"
APIFY_BASE  = "https://api.apify.com/v2"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "jobs.db")
ME_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "me.json")
RUN_TIMEOUT = 420


def _load_profile() -> dict:
    try:
        with open(ME_PATH, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def _crawl_config() -> dict:
    """Derive search terms, locations, and salary ceiling from me.json."""
    p = _load_profile()
    prefs = p.get("job_preferences", {})

    search_terms = p.get("target_roles", [])

    # Parse all "City, STATE" entries from locations (skip free-text entries)
    locations = []
    for raw in prefs.get("locations", []):
        parts = [s.strip() for s in raw.split(",")]
        if len(parts) >= 2:
            locations.append((parts[0], parts[1]))
    if not locations:
        locations = [("Sydney", "NSW")]

    salary_max = prefs.get("salary_range", {}).get("max", 80_000)
    salary_ceiling = int(salary_max * 1.5)

    return {
        "search_terms":   search_terms,
        "locations":      locations,
        "salary_ceiling": salary_ceiling,
    }

# ─── Exclusion filters ────────────────────────────────────────────────────────

# Title substrings that trigger an immediate auto-discard on insert.
# These are roles that will never be relevant — keeping them in the DB
# (discarded=1) prevents the crawler from re-inserting them on future runs.
AUTO_DISCARD_PATTERNS = [
    "it support", "help desk", "helpdesk", "service desk",
    "desktop support", "technical support", "tech support",
    "customer service", "customer support",
    "call centre", "call center", "contact centre",
    "msp engineer", "msp support",
    "level 1 / 2", "level 1/2",
    "l1 msp", "l2 msp", "l3 msp",
    "it technician", "it asset",
    "field technician",
    "network engineer", "network security", "network support",
    "network installation", "networking and field",
    "systems administrator", "system administrator",
    "windows administrator", "cloud administrator",
    "servicenow administrator", "infrastructure system",
    "end user compute",
    "contract administrator", "contracts administrator",
    "construction administrator", "project administrator",
    "projects administrator", "office administrator",
    "operations administrator", "service administrator",
    "junior erp administrator",
    "catering", "kitchen hand", "hospitality",
    "retail project", "brand & growth leader",
    "telco helpdesk", "pos technical",
    "trade counter sales", "sales representative",
    "presales engineer",
]

def _is_auto_discard(title: str) -> bool:
    """Return True if the job title matches any auto-discard pattern."""
    if not title:
        return False
    t = title.lower()
    return any(p in t for p in AUTO_DISCARD_PATTERNS)


# Title keywords that indicate the role is too senior or wrong domain
EXCLUDE_TITLE_WORDS = [
    # Seniority
    "senior", "sr.", " sr ", "lead", "head of", "director", "manager",
    "principal", "chief", "vp ", "vice president", "executive",
    "associate director", "team lead",
    # Wrong domains
    "construction", "finance", "financial", "brand", "marketing",
    "sales", "accounting", "accountant", "legal", "lawyer", "solicitor",
    "property", "real estate",
    # Defence / clearance
    "defence", "defense", "adf ", "army", "navy", "air force",
    "security clearance", "clearance required",
]

def _is_excluded_title(title: str) -> tuple[bool, str]:
    """Return (excluded, matched_keyword). Case-insensitive substring match."""
    if not title:
        return False, ""
    t = title.lower()
    for kw in EXCLUDE_TITLE_WORDS:
        if kw in t:
            return True, kw
    return False, ""


def _salary_min(salary: str) -> int | None:
    """
    Extract the minimum (lower-bound) salary from a salary string.
    Returns None if it cannot be determined or if 'up to' indicates
    only the ceiling is stated.
    """
    if not salary:
        return None
    s_lower = salary.lower()

    # "Up to $X" → X is the ceiling, minimum unknown → keep the job
    if "up to" in s_lower or "up-to" in s_lower:
        return None

    # Find all numeric values that look like salaries (>= $10 000)
    nums = []
    for m in re.finditer(r"[\d,]+", salary):
        try:
            v = int(m.group().replace(",", ""))
            if v >= 10_000:
                nums.append(v)
        except ValueError:
            pass

    return min(nums) if nums else None


def _salary_too_high(salary: str, ceiling: int) -> bool:
    minimum = _salary_min(salary)
    return minimum is not None and minimum > ceiling


# ─── DB ───────────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id        TEXT PRIMARY KEY,
            title         TEXT,
            company       TEXT,
            location      TEXT,
            salary        TEXT,
            date_posted   TEXT,
            job_url       TEXT,
            description   TEXT,
            date_crawled  TEXT,
            scored        INTEGER NOT NULL DEFAULT 0,
            score         INTEGER,
            discarded     INTEGER NOT NULL DEFAULT 0,
            source        TEXT DEFAULT 'seek'
        )
    """)
    # migrate existing DBs that pre-date the source column
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "source" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN source TEXT DEFAULT 'seek'")
    conn.commit()


# ─── Apify helpers ────────────────────────────────────────────────────────────

def start_actor_run(keyword, location="Sydney", state="NSW"):
    payload = {
        "searchTerm": keyword,
        "location": location,
        "state": state,
        "dateRange": 7,
        "workTypes": ["fulltime", "parttime"],
        "maxResults": 30,
    }
    r = requests.post(
        f"{APIFY_BASE}/acts/{ACTOR_ID}/runs",
        params={"token": APIFY_API_KEY},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()["data"]
    return data["id"], data["defaultDatasetId"]


def wait_for_run(run_id):
    deadline = time.time() + RUN_TIMEOUT
    while time.time() < deadline:
        r = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            params={"token": APIFY_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        status = r.json()["data"]["status"]
        if status == "SUCCEEDED":
            return
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Actor run ended with status: {status}")
        time.sleep(10)
    raise TimeoutError(f"Actor run {run_id} did not finish within {RUN_TIMEOUT}s")


def fetch_items(dataset_id):
    r = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        params={"token": APIFY_API_KEY, "format": "json", "limit": 1000},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def strip_html(text):
    if not text:
        return None
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_job(item):
    content = item.get("content") or {}
    advertiser = item.get("advertiser") or {}
    loc = item.get("joblocationInfo") or {}
    job_id = str(item.get("id") or "").strip() or None
    salary = item.get("salary")
    if salary == "N/A":
        salary = None
    raw_desc = content.get("unEditedContent") or content.get("jobHook") or ""
    return {
        "job_id":      job_id,
        "title":       item.get("title"),
        "company":     advertiser.get("name"),
        "location":    loc.get("displayLocation") or loc.get("suburb"),
        "salary":      salary,
        "date_posted": item.get("listedAt"),
        "job_url":     item.get("jobLink"),
        "description": strip_html(raw_desc),
    }


# ─── LinkedIn helpers ─────────────────────────────────────────────────────────

def _linkedin_search_url(keyword: str, city: str, state: str) -> str:
    import urllib.parse
    params = {
        "keywords": keyword,
        "location": f"{city}, {state}, Australia",
        "f_TPR":    "r604800",   # past week
        "sortBy":   "DD",        # most recent first
    }
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


def start_linkedin_run(keyword: str, city: str, state: str):
    payload = {
        "searchUrl": _linkedin_search_url(keyword, city, state),
        "count": 25,
    }
    r = requests.post(
        f"{APIFY_BASE}/acts/{LI_ACTOR_ID}/runs",
        params={"token": APIFY_API_KEY},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()["data"]
    return data["id"], data["defaultDatasetId"]


def extract_linkedin_job(item: dict) -> dict:
    job_id = str(item.get("id") or "").strip()
    return {
        "job_id":      f"li_{job_id}" if job_id else None,
        "title":       item.get("title"),
        "company":     item.get("companyName") or item.get("company"),
        "location":    item.get("location"),
        "salary":      item.get("salary") or None,
        "date_posted": item.get("postedAt") or item.get("publishedAt"),
        "job_url":     item.get("jobUrl") or item.get("url"),
        "description": strip_html(item.get("description") or item.get("descriptionHtml") or ""),
    }


def crawl_linkedin(conn, today: str, salary_ceiling: int) -> tuple[int, int]:
    """Crawl LinkedIn jobs. Returns (total_new, total_dupes)."""
    cfg = _crawl_config()
    search_terms = cfg["search_terms"]
    locations    = cfg["locations"]

    total_new   = 0
    total_dupes = 0

    for term in search_terms:
        for city, state in locations:
            print(f"\n[LI] Searching: {term} in {city}, {state}")
            try:
                run_id, dataset_id = start_linkedin_run(term, city, state)
                print(f"    Run started ({run_id}) — polling…")
                wait_for_run(run_id)

                items = fetch_items(dataset_id)
                print(f"    {len(items)} jobs returned from Apify")

                new_count = 0; dupe_count = 0; excl = 0

                for item in items:
                    job = extract_linkedin_job(item)
                    if not job["job_id"]:
                        continue

                    excluded, _ = _is_excluded_title(job["title"] or "")
                    if excluded:
                        excl += 1
                        continue

                    if _salary_too_high(job["salary"] or "", salary_ceiling):
                        continue

                    exists = conn.execute(
                        "SELECT 1 FROM jobs WHERE job_id = ?", (job["job_id"],)
                    ).fetchone()
                    if exists:
                        dupe_count += 1; total_dupes += 1
                        continue

                    seek_dupe = conn.execute(
                        "SELECT 1 FROM jobs WHERE source='seek' AND company=? AND title=?",
                        (job["company"], job["title"]),
                    ).fetchone()
                    if seek_dupe:
                        dupe_count += 1; total_dupes += 1
                        continue

                    auto_discard = _is_auto_discard(job["title"])
                    conn.execute(
                        """INSERT INTO jobs
                           (job_id, title, company, location, salary, date_posted,
                            job_url, description, date_crawled, scored, score, discarded, source)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, 'linkedin')""",
                        (
                            job["job_id"], job["title"], job["company"],
                            job["location"], job["salary"], job["date_posted"],
                            job["job_url"], job["description"], today,
                            1 if auto_discard else 0,
                        ),
                    )
                    if not auto_discard:
                        new_count += 1; total_new += 1

                conn.commit()
                parts = [f"Inserted {new_count} new", f"Dupes {dupe_count}"]
                if excl: parts.append(f"Excl-title {excl}")
                print(f"    {' | '.join(parts)}")

            except Exception as exc:
                print(f"    ERROR for '{term}' in {city}, {state}: {exc}")

    return total_new, total_dupes


# ─── Indeed helpers ───────────────────────────────────────────────────────────

def start_indeed_run(keyword: str, location: str) -> tuple[str, str]:
    payload = {
        "position": keyword,
        "location": location,
        "country": "AU",
        "maxItems": 30,
        "datePosted": "last7days",
    }
    r = requests.post(
        f"{APIFY_BASE}/acts/{IND_ACTOR_ID}/runs",
        params={"token": APIFY_API_KEY},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()["data"]
    return data["id"], data["defaultDatasetId"]


AU_STATES = {"nsw", "vic", "qld", "wa", "sa", "tas", "act", "nt",
             "sydney", "melbourne", "brisbane", "perth", "adelaide",
             "canberra", "hobart", "darwin", "australia"}

def _is_australian_location(location: str) -> bool:
    if not location:
        return True  # no location info — don't filter out
    loc = location.lower()
    return any(s in loc for s in AU_STATES)


def extract_indeed_job(item: dict) -> dict:
    job_id = str(item.get("jobId") or item.get("id") or "").strip()
    salary = item.get("salary") or None
    if salary == "N/A":
        salary = None
    raw_desc = item.get("description") or item.get("jobDescription") or ""
    return {
        "job_id":      f"ind_{job_id}" if job_id else None,
        "title":       item.get("title") or item.get("positionName"),
        "company":     item.get("company") or item.get("companyName"),
        "location":    item.get("location") or item.get("jobLocation"),
        "salary":      salary,
        "date_posted": item.get("date") or item.get("postedAt") or item.get("datePosted"),
        "job_url":     item.get("url") or item.get("jobUrl") or item.get("externalApplyLink"),
        "description": strip_html(raw_desc),
        "easy_apply":  bool(item.get("easyApply") or item.get("indeedApply")),
    }


def crawl_indeed(conn, today: str, salary_ceiling: int) -> tuple[int, int]:
    """Crawl Indeed jobs. Returns (total_new, total_dupes)."""
    cfg = _crawl_config()
    search_terms = cfg["search_terms"]
    locations    = cfg["locations"]

    total_new   = 0
    total_dupes = 0

    for term in search_terms:
        for city, state in locations:
            location_str = f"{city}, {state}, Australia"
            print(f"\n[IND] Searching: {term} in {location_str}")
            try:
                run_id, dataset_id = start_indeed_run(term, location_str)
                print(f"    Run started ({run_id}) — polling…")
                wait_for_run(run_id)

                items = fetch_items(dataset_id)
                print(f"    {len(items)} jobs returned from Apify")

                new_count = 0; dupe_count = 0; excl = 0

                for item in items:
                    job = extract_indeed_job(item)
                    if not job["job_id"]:
                        continue

                    if not _is_australian_location(job["location"]):
                        excl += 1
                        continue

                    excluded, _ = _is_excluded_title(job["title"] or "")
                    if excluded:
                        excl += 1
                        continue

                    if _salary_too_high(job["salary"] or "", salary_ceiling):
                        continue

                    exists = conn.execute(
                        "SELECT 1 FROM jobs WHERE job_id = ?", (job["job_id"],)
                    ).fetchone()
                    if exists:
                        dupe_count += 1; total_dupes += 1
                        continue

                    cross_dupe = conn.execute(
                        "SELECT 1 FROM jobs WHERE source IN ('seek','linkedin') AND company=? AND title=?",
                        (job["company"], job["title"]),
                    ).fetchone()
                    if cross_dupe:
                        dupe_count += 1; total_dupes += 1
                        continue

                    auto_discard = _is_auto_discard(job["title"])
                    conn.execute(
                        """INSERT INTO jobs
                           (job_id, title, company, location, salary, date_posted,
                            job_url, description, date_crawled, scored, score, discarded, source)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, 'indeed')""",
                        (
                            job["job_id"], job["title"], job["company"],
                            job["location"], job["salary"], job["date_posted"],
                            job["job_url"], job["description"], today,
                            1 if auto_discard else 0,
                        ),
                    )
                    if not auto_discard:
                        new_count += 1; total_new += 1

                conn.commit()
                parts = [f"Inserted {new_count} new", f"Dupes {dupe_count}"]
                if excl: parts.append(f"Excl-title {excl}")
                print(f"    {' | '.join(parts)}")

            except Exception as exc:
                print(f"    ERROR for '{term}' in {location_str}: {exc}")

    return total_new, total_dupes


# ─── Main crawl ───────────────────────────────────────────────────────────────

def crawl():
    if not APIFY_API_KEY:
        raise EnvironmentError("APIFY_API_KEY is not set in .env")

    cfg = _crawl_config()
    search_terms   = cfg["search_terms"]
    locations      = cfg["locations"]
    salary_ceiling = cfg["salary_ceiling"]

    if not search_terms:
        raise ValueError("No target_roles found in me.json — add at least one job title to search for.")

    loc_str = ", ".join(f"{loc}/{st}" for loc, st in locations)
    print(f"[crawler] Locations : {loc_str}")
    print(f"[crawler] Salary ceiling : ${salary_ceiling:,}")
    print(f"[crawler] Searching {len(search_terms)} terms × {len(locations)} cities = {len(search_terms) * len(locations)} runs")

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    today = date.today().isoformat()
    total_found = 0
    total_new = 0
    total_dupes = 0
    total_excl_title = 0
    total_excl_salary = 0
    excl_title_examples = []   # collect a few examples for the summary

    for term in search_terms:
        for location, state in locations:
            print(f"\n[>] Searching: {term} in {location}, {state}")
            try:
                run_id, dataset_id = start_actor_run(term, location, state)
                print(f"    Run started ({run_id}) — polling…")
                wait_for_run(run_id)

                items = fetch_items(dataset_id)
                print(f"    {len(items)} jobs returned from Apify")
                total_found += len(items)

                new_count = 0
                dupe_count = 0
                excl_title = 0
                excl_salary = 0

                for item in items:
                    job = extract_job(item)
                    if not job["job_id"]:
                        continue

                    # ── Filter: title ──────────────────────────────────────────
                    excluded, kw = _is_excluded_title(job["title"])
                    if excluded:
                        excl_title += 1
                        total_excl_title += 1
                        if len(excl_title_examples) < 8:
                            excl_title_examples.append(f"{job['title']} [{kw}]")
                        continue

                    # ── Filter: salary ─────────────────────────────────────────
                    if _salary_too_high(job["salary"], salary_ceiling):
                        excl_salary += 1
                        total_excl_salary += 1
                        continue

                    # ── Deduplicate ────────────────────────────────────────────
                    exists = conn.execute(
                        "SELECT 1 FROM jobs WHERE job_id = ?", (job["job_id"],)
                    ).fetchone()
                    if exists:
                        dupe_count += 1
                        total_dupes += 1
                        continue

                    auto_discard = _is_auto_discard(job["title"])
                    conn.execute(
                        """INSERT INTO jobs
                           (job_id, title, company, location, salary, date_posted,
                            job_url, description, date_crawled, scored, score, discarded, source)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, 'seek')""",
                        (
                            job["job_id"], job["title"], job["company"],
                            job["location"], job["salary"], job["date_posted"],
                            job["job_url"], job["description"], today,
                            1 if auto_discard else 0,
                        ),
                    )
                    if not auto_discard:
                        new_count += 1
                        total_new += 1

                conn.commit()
                parts = [f"Inserted {new_count} new", f"Dupes {dupe_count}"]
                if excl_title:  parts.append(f"Excl-title {excl_title}")
                if excl_salary: parts.append(f"Excl-salary {excl_salary}")
                print(f"    {' | '.join(parts)}")

            except Exception as exc:
                print(f"    ERROR for '{term}' in {location}: {exc}")

    # ── LinkedIn ──────────────────────────────────────────────────────────────
    print(f"\n{'=' * 55}")
    print(" SEEK DONE — starting LinkedIn crawl…")
    print(f"{'=' * 55}")
    li_new = li_dupes = 0
    try:
        li_new, li_dupes = crawl_linkedin(conn, today, salary_ceiling)
    except Exception as exc:
        print(f"[LI] LinkedIn crawl failed: {exc}")

    # ── Indeed ────────────────────────────────────────────────────────────────
    print(f"\n{'=' * 55}")
    print(" LINKEDIN DONE — starting Indeed crawl…")
    print(f"{'=' * 55}")
    ind_new = ind_dupes = 0
    try:
        ind_new, ind_dupes = crawl_indeed(conn, today, salary_ceiling)
    except Exception as exc:
        print(f"[IND] Indeed crawl failed: {exc}")

    conn.close()

    print(f"""
{'=' * 55}
 CRAWL COMPLETE
{'=' * 55}
 ── Seek ──────────────────────────────────────────
 Total returned from Apify : {total_found}
 New jobs inserted         : {total_new}
 Duplicates skipped        : {total_dupes}
 Excluded by title         : {total_excl_title}
 Excluded by salary >${salary_ceiling:,} : {total_excl_salary}
 ── LinkedIn ──────────────────────────────────────
 New jobs inserted         : {li_new}
 Duplicates skipped        : {li_dupes}
 ── Indeed ────────────────────────────────────────
 New jobs inserted         : {ind_new}
 Duplicates skipped        : {ind_dupes}
{'=' * 55}""")

    if excl_title_examples:
        print(" Sample Seek excluded titles:")
        for ex in excl_title_examples:
            print(f"   • {ex}")
        print(f"{'=' * 55}\n")


if __name__ == "__main__":
    crawl()
