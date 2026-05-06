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
ACTOR_ID = "websift~seek-job-scraper"
APIFY_BASE = "https://api.apify.com/v2"
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
    """Derive search terms, location, and salary ceiling from me.json."""
    p = _load_profile()
    prefs = p.get("job_preferences", {})

    # Search terms — use target_roles from profile; no hardcoded fallback needed
    search_terms = p.get("target_roles", [])

    # Location — first entry in job_preferences.locations, e.g. "Sydney, NSW"
    location, state = "Sydney", "NSW"
    raw_locs = prefs.get("locations", [])
    if raw_locs:
        parts = [s.strip() for s in raw_locs[0].split(",")]
        if len(parts) >= 2:
            location, state = parts[0], parts[1]
        elif parts:
            location = parts[0]

    # Salary ceiling — 1.5× the profile max (filters clearly senior roles)
    salary_max = prefs.get("salary_range", {}).get("max", 80_000)
    salary_ceiling = int(salary_max * 1.5)

    return {
        "search_terms":   search_terms,
        "location":       location,
        "state":          state,
        "salary_ceiling": salary_ceiling,
    }

# ─── Exclusion filters ────────────────────────────────────────────────────────

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
            discarded     INTEGER NOT NULL DEFAULT 0
        )
    """)
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


# ─── Main crawl ───────────────────────────────────────────────────────────────

def crawl():
    if not APIFY_API_KEY:
        raise EnvironmentError("APIFY_API_KEY is not set in .env")

    cfg = _crawl_config()
    search_terms   = cfg["search_terms"]
    location       = cfg["location"]
    state          = cfg["state"]
    salary_ceiling = cfg["salary_ceiling"]

    if not search_terms:
        raise ValueError("No target_roles found in me.json — add at least one job title to search for.")

    print(f"[crawler] Location : {location}, {state}")
    print(f"[crawler] Salary ceiling : ${salary_ceiling:,}")
    print(f"[crawler] Searching {len(search_terms)} terms from me.json target_roles")

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
        print(f"\n[>] Searching: {term}")
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

                conn.execute(
                    """INSERT INTO jobs
                       (job_id, title, company, location, salary, date_posted,
                        job_url, description, date_crawled, scored, score, discarded)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 0)""",
                    (
                        job["job_id"], job["title"], job["company"],
                        job["location"], job["salary"], job["date_posted"],
                        job["job_url"], job["description"], today,
                    ),
                )
                new_count += 1
                total_new += 1

            conn.commit()
            parts = [f"Inserted {new_count} new", f"Dupes {dupe_count}"]
            if excl_title:  parts.append(f"Excl-title {excl_title}")
            if excl_salary: parts.append(f"Excl-salary {excl_salary}")
            print(f"    {' | '.join(parts)}")

        except Exception as exc:
            print(f"    ERROR for '{term}': {exc}")

    conn.close()

    print(f"""
{'=' * 55}
 CRAWL COMPLETE
{'=' * 55}
 Total returned from Apify : {total_found}
 New jobs inserted         : {total_new}
 Duplicates skipped        : {total_dupes}
 Excluded by title         : {total_excl_title}  (Senior/Lead/Manager/wrong domain)
 Excluded by salary >${salary_ceiling:,} : {total_excl_salary}
{'=' * 55}""")

    if excl_title_examples:
        print(" Sample excluded titles:")
        for ex in excl_title_examples:
            print(f"   • {ex}")
        print(f"{'=' * 55}\n")


if __name__ == "__main__":
    crawl()
