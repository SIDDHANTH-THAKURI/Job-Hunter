import sqlite3
import threading
import webbrowser
import time
from pathlib import Path
from app import app, init_db

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "jobs.db"

# Exposed so app.py routes can reschedule when settings change
scheduler = None


def _get_setting(key: str, default: str = "") -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def _pipeline_job():
    enabled = _get_setting("indeed_scheduler_enabled", "false")
    if enabled != "true":
        return
    from bot.indeed_pipeline import run_indeed_pipeline
    run_indeed_pipeline()


def _reschedule(run_time: str | None = None):
    """Remove existing indeed job and re-add with new time. Call after settings change."""
    global scheduler
    if scheduler is None:
        return
    try:
        scheduler.remove_job("indeed_pipeline")
    except Exception:
        pass

    t = run_time or _get_setting("indeed_schedule_time", "08:00")
    try:
        hour, minute = int(t.split(":")[0]), int(t.split(":")[1])
    except Exception:
        hour, minute = 8, 0

    scheduler.add_job(
        _pipeline_job,
        trigger="cron",
        day_of_week="mon-fri",
        hour=hour,
        minute=minute,
        id="indeed_pipeline",
        replace_existing=True,
    )
    print(f"  [scheduler] Indeed pipeline scheduled at {hour:02d}:{minute:02d} Mon-Fri")


def _start_scheduler():
    global scheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.start()
    _reschedule()
    # Make reschedule accessible from app.py
    app.config["reschedule_indeed"] = _reschedule


def _open_browser():
    time.sleep(1.5)
    webbrowser.open("http://localhost:5000")


def _ensure_templates():
    tdir = Path(__file__).parent / "templates"
    if not (tdir / "resume.docx").exists() or not (tdir / "cover.docx").exists():
        print("  Templates not found — generating...")
        from create_templates import create_resume_template, create_cover_template
        create_resume_template()
        create_cover_template()
        print("  Templates ready.\n")


if __name__ == "__main__":
    init_db()
    _ensure_templates()
    _start_scheduler()
    threading.Thread(target=_open_browser, daemon=True).start()
    print("\n  Job Hunter Dashboard -> http://localhost:5000\n")
    app.run(port=5000, debug=True, use_reloader=False)
