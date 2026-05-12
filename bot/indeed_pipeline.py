"""
Indeed full pipeline: crawl → score → generate → apply
Called by the scheduler or manually via /api/indeed/run
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH  = BASE_DIR / "data" / "jobs.db"


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_setting(conn, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def run_indeed_pipeline(log_fn=print) -> dict:
    """
    Full pipeline:
      1. Crawl Indeed for new jobs
      2. Score unscored Indeed jobs
      3. Generate resume + cover letter for jobs above threshold
      4. Apply via bot

    Returns summary dict.
    """
    conn = _db()
    now_str = datetime.now(timezone.utc).isoformat()
    _set_setting(conn, "indeed_last_run", now_str)
    daily_cap = int(_get_setting(conn, "indeed_daily_cap", "20"))
    threshold = int(_get_setting(conn, "indeed_auto_apply_threshold", "7"))
    conn.close()

    # ── Step 1: Crawl ─────────────────────────────────────────────────────────
    log_fn("[pipeline] Step 1/4 — Crawling Indeed…")
    try:
        import sqlite3 as _sq
        from crawler.seek_crawler import crawl_indeed, init_db
        from datetime import date

        _conn = _sq.connect(DB_PATH)
        init_db(_conn)
        today = date.today().isoformat()

        from crawler.seek_crawler import _crawl_config
        cfg = _crawl_config()
        ind_new, ind_dupes = crawl_indeed(_conn, today, cfg["salary_ceiling"])
        _conn.close()
        log_fn(f"[pipeline] Crawl done — {ind_new} new, {ind_dupes} dupes")
    except Exception as exc:
        log_fn(f"[pipeline] Crawl error: {exc}")
        ind_new = 0

    # ── Step 2: Score ─────────────────────────────────────────────────────────
    log_fn("[pipeline] Step 2/4 — Scoring new Indeed jobs…")
    scored_count = 0
    try:
        from scorer.score_jobs import score_job

        conn = _db()
        unscored = conn.execute(
            "SELECT job_id FROM jobs WHERE source='indeed' AND scored=0 AND (discarded=0 OR discarded IS NULL)"
        ).fetchall()
        conn.close()

        for row in unscored:
            try:
                score_job(row["job_id"])
                scored_count += 1
            except Exception as exc:
                log_fn(f"[pipeline] Score error for {row['job_id']}: {exc}")

        log_fn(f"[pipeline] Scored {scored_count} jobs")
    except Exception as exc:
        log_fn(f"[pipeline] Score step error: {exc}")

    # ── Step 3: Generate ──────────────────────────────────────────────────────
    log_fn("[pipeline] Step 3/4 — Generating resumes + cover letters…")
    generated_count = 0
    try:
        from generator.resume_generator import generate_for_job

        conn = _db()
        to_generate = conn.execute(
            """SELECT job_id FROM jobs
               WHERE source='indeed'
                 AND scored=1
                 AND score >= ?
                 AND (discarded=0 OR discarded IS NULL)
                 AND COALESCE(status,'Not Applied')='Not Applied'
                 AND (resume_text IS NULL OR resume_text='')""",
            (threshold,),
        ).fetchall()
        conn.close()

        for row in to_generate:
            try:
                generate_for_job(row["job_id"])
                generated_count += 1
            except Exception as exc:
                log_fn(f"[pipeline] Generate error for {row['job_id']}: {exc}")

        log_fn(f"[pipeline] Generated for {generated_count} jobs")
    except Exception as exc:
        log_fn(f"[pipeline] Generate step error: {exc}")

    # ── Step 4: Apply ─────────────────────────────────────────────────────────
    log_fn("[pipeline] Step 4/4 — Applying…")
    apply_result = {"applied": 0, "skipped": 0, "errors": 0}
    try:
        from bot.indeed_apply import run_apply_session
        apply_result = run_apply_session(daily_cap=daily_cap, log_fn=log_fn)

        # Update applies_today counter
        conn = _db()
        prev = int(_get_setting(conn, "indeed_applies_today", "0"))
        _set_setting(conn, "indeed_applies_today", str(prev + apply_result["applied"]))
        conn.close()
    except Exception as exc:
        log_fn(f"[pipeline] Apply step error: {exc}")

    summary = {
        "crawled_new": ind_new,
        "scored": scored_count,
        "generated": generated_count,
        **apply_result,
    }
    log_fn(f"[pipeline] Done — {summary}")
    return summary


def run_indeed_apply_only(log_fn=print) -> dict:
    """
    Skip crawl — just score any unscored Indeed jobs, generate docs, and apply.
    Use this when jobs are already in the DB and you just want to apply.
    """
    conn = _db()
    threshold = int(_get_setting(conn, "indeed_auto_apply_threshold", "7"))
    daily_cap = int(_get_setting(conn, "indeed_daily_cap", "20"))
    conn.close()

    # ── Score unscored ────────────────────────────────────────────────────────
    log_fn("[pipeline] Step 1/3 — Scoring unscored Indeed jobs…")
    scored_count = 0
    try:
        from scorer.score_jobs import score_job
        conn = _db()
        unscored = conn.execute(
            "SELECT job_id FROM jobs WHERE source='indeed' AND scored=0 AND (discarded=0 OR discarded IS NULL)"
        ).fetchall()
        conn.close()
        for row in unscored:
            try:
                score_job(row["job_id"])
                scored_count += 1
            except Exception as exc:
                log_fn(f"[pipeline] Score error for {row['job_id']}: {exc}")
        log_fn(f"[pipeline] Scored {scored_count} jobs")
    except Exception as exc:
        log_fn(f"[pipeline] Score step error: {exc}")

    # ── Generate ──────────────────────────────────────────────────────────────
    log_fn("[pipeline] Step 2/3 — Generating docs…")
    generated_count = 0
    try:
        from generator.resume_generator import generate_for_job
        conn = _db()
        to_generate = conn.execute(
            """SELECT job_id FROM jobs
               WHERE source='indeed' AND scored=1 AND score >= ?
                 AND (discarded=0 OR discarded IS NULL)
                 AND COALESCE(status,'Not Applied')='Not Applied'
                 AND (resume_text IS NULL OR resume_text='')""",
            (threshold,),
        ).fetchall()
        conn.close()
        for row in to_generate:
            try:
                generate_for_job(row["job_id"])
                generated_count += 1
            except Exception as exc:
                log_fn(f"[pipeline] Generate error for {row['job_id']}: {exc}")
        log_fn(f"[pipeline] Generated for {generated_count} jobs")
    except Exception as exc:
        log_fn(f"[pipeline] Generate step error: {exc}")

    # ── Apply ─────────────────────────────────────────────────────────────────
    log_fn("[pipeline] Step 3/3 — Applying…")
    apply_result = {"applied": 0, "skipped": 0, "errors": 0}
    try:
        from bot.indeed_apply import run_apply_session
        apply_result = run_apply_session(daily_cap=daily_cap, log_fn=log_fn)
        conn = _db()
        prev = int(_get_setting(conn, "indeed_applies_today", "0"))
        _set_setting(conn, "indeed_applies_today", str(prev + apply_result["applied"]))
        conn.close()
    except Exception as exc:
        log_fn(f"[pipeline] Apply step error: {exc}")

    summary = {"scored": scored_count, "generated": generated_count, **apply_result}
    log_fn(f"[pipeline] Done — {summary}")
    return summary


if __name__ == "__main__":
    run_indeed_pipeline()
