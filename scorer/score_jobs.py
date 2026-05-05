import json
import os
import re
import sqlite3
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "data" / "jobs.db"
ME_PATH = BASE_DIR / "me.json"

MODEL = "claude-haiku-4-5-20251001"


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _profile_json() -> str:
    with open(ME_PATH, encoding="utf-8-sig") as f:
        return f.read()


def _system_prompt(profile_json: str) -> str:
    p = json.loads(profile_json)
    personal = p["personal"]
    prefs = p.get("job_preferences", {})
    sal = prefs.get("salary_range", {})
    sal_str = f"AUD ${sal.get('min', 0):,}–${sal.get('max', 0):,}" if sal else "Not specified"
    visa = personal.get("visa", {})
    visa_str = f"{visa.get('type', '')} — {visa.get('work_rights', '')} until {visa.get('expiry', '')}"
    top_roles = ", ".join(p.get("target_roles", [])[:8])
    skills_tech = (
        p.get("skills", {}).get("programming", {}).get("wrote_professionally", []) +
        p.get("skills", {}).get("programming", {}).get("can_discuss_confidently", [])
    )[:8]
    skills_it = p.get("skills", {}).get("it_support", {}).get("trained", [])[:5]
    core_skills = ", ".join(skills_tech + skills_it)
    employers = [
        f"{e['company']} ({e['period']})"
        for e in p.get("work_experience", [])
        if e.get("show_on_resume")
    ]
    deal_breakers = p.get("deal_breakers", [])
    excluded_industries = prefs.get("industries_excluded", [])

    return f"""You are a career advisor scoring job listings for a specific candidate.

CANDIDATE PROFILE (full JSON):
{profile_json}

KEY FACTS:
- Name: {personal['name']}, {personal['location']}
- Visa: {visa_str}
- Target salary: {sal_str}
- Target roles: {top_roles}
- Core skills: {core_skills}
- Experience: {'; '.join(employers)}
- Deal breakers: {'; '.join(deal_breakers)}
- Excluded industries: {', '.join(excluded_industries)}

SCORING CRITERIA (1–10):
10 = Perfect match (strong skill overlap, junior/mid level, Sydney, no clearance, salary fits)
7-9 = Good match (most criteria met, minor gaps)
5-6 = Borderline (some relevant skills but notable gaps or concerns)
3-4 = Poor match (wrong seniority, wrong domain, or major skill gaps)
1-2 = Very poor match (citizenship required, heavy clearance, completely wrong field)

AUTO-DISCARD triggers (score ≤ 4):
- Requires Australian citizenship or PR (visa_ok = false)
- Requires active security clearance
- Minimum salary clearly above $120k
- Senior/Lead/Manager level despite passing title filter
- Completely unrelated domain

Respond ONLY with valid JSON, no markdown fences:
{{"score": <integer 1-10>, "reason": "<one sentence>", "missing_skills": [<strings>], "visa_ok": <true|false>}}"""


def score_job(job_id: str, client: anthropic.Anthropic = None, profile_json: str = None) -> dict:
    if client is None:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    if profile_json is None:
        profile_json = _profile_json()

    with _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", [job_id]).fetchone()
    if not row:
        raise ValueError(f"Job {job_id} not found")

    description = (row["description"] or "")[:3000]

    user_msg = f"""Score this job for the candidate.

Title: {row['title']}
Company: {row['company']}
Location: {row['location']}
Salary: {row['salary'] or 'Not specified'}
Description:
{description}"""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=[
            {
                "type": "text",
                "text": _system_prompt(profile_json),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = msg.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        data = json.loads(match.group())
        score = max(1, min(10, int(data.get("score", 1))))
        reason = str(data.get("reason", "")).strip()
        missing_skills = data.get("missing_skills", [])
        if not isinstance(missing_skills, list):
            missing_skills = []
        missing_skills = [str(s) for s in missing_skills]
        visa_ok = bool(data.get("visa_ok", True))
    else:
        score = 1
        reason = "Could not parse score"
        missing_skills = []
        visa_ok = True

    auto_discard = score < 5

    try:
        from utils import log_api_call
        log_api_call(MODEL, "score", msg.usage.input_tokens, msg.usage.output_tokens)
    except Exception:
        pass

    with _db() as conn:
        conn.execute(
            """UPDATE jobs
               SET scored=1, score=?, score_reason=?, missing_skills=?, visa_ok=?,
                   discarded=CASE WHEN ? THEN 1 ELSE discarded END
               WHERE job_id=?""",
            [score, reason, json.dumps(missing_skills), 1 if visa_ok else 0,
             auto_discard, job_id],
        )
        conn.commit()

    return {"score": score, "reason": reason, "missing_skills": missing_skills, "visa_ok": visa_ok}


def score_all_jobs() -> dict:
    with _db() as conn:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE (scored=0 OR scored IS NULL) AND (discarded=0 OR discarded IS NULL)"
        ).fetchall()

    total = len(rows)
    if total == 0:
        return {"total": 0, "scored": 0, "errors": 0, "auto_discarded": 0}

    print(f"[scorer] {total} unscored jobs to process…")

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    profile_json = _profile_json()

    done = 0
    errors = 0
    auto_discarded = 0

    for row in rows:
        try:
            result = score_job(row["job_id"], client=client, profile_json=profile_json)
            done += 1
            if result["score"] < 5:
                auto_discarded += 1
            if done % 10 == 0:
                print(f"  {done}/{total} scored…")
        except Exception as exc:
            print(f"  ERROR scoring {row['job_id']}: {exc}")
            errors += 1

    print(f"\n[scorer] Done — {done} scored, {auto_discarded} auto-discarded, {errors} errors")

    # Print top 20
    with _db() as conn:
        top = conn.execute(
            """SELECT title, company, location, salary, score, score_reason, visa_ok
               FROM jobs
               WHERE scored=1 AND (discarded=0 OR discarded IS NULL)
               ORDER BY score DESC
               LIMIT 20"""
        ).fetchall()

    print(f"\n{'='*60}")
    print(f" TOP {min(20, len(top))} JOBS")
    print(f"{'='*60}")
    for i, j in enumerate(top, 1):
        visa_flag = "" if j["visa_ok"] else " [VISA?]"
        sal = f" | {j['salary']}" if j["salary"] else ""
        print(f" {i:>2}. [{j['score']}/10] {j['title']} @ {j['company']}{visa_flag}")
        print(f"      {j['location'] or 'N/A'}{sal}")
        if j["score_reason"]:
            print(f"      {j['score_reason']}")
    print(f"{'='*60}\n")

    return {"total": total, "scored": done, "errors": errors, "auto_discarded": auto_discarded}
