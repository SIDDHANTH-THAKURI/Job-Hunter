import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests as _requests
from flask import Flask, abort, jsonify, request, send_file
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "jobs.db"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(BASE_DIR / "dashboard"), static_url_path="")

_tasks: dict = {}


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        new_cols = {
            "status": "ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'Not Applied'",
            "score_reason": "ALTER TABLE jobs ADD COLUMN score_reason TEXT",
            "resume_text": "ALTER TABLE jobs ADD COLUMN resume_text TEXT",
            "cover_letter_text": "ALTER TABLE jobs ADD COLUMN cover_letter_text TEXT",
            "missing_skills": "ALTER TABLE jobs ADD COLUMN missing_skills TEXT",
            "visa_ok": "ALTER TABLE jobs ADD COLUMN visa_ok INTEGER DEFAULT 1",
        }
        for col, sql in new_cols.items():
            if col not in existing:
                conn.execute(sql)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                model       TEXT NOT NULL,
                operation   TEXT NOT NULL,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd    REAL NOT NULL DEFAULT 0
            )
        """)
        conn.commit()


# ─── Static ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(BASE_DIR / "dashboard" / "index.html")


@app.route("/api/profile-meta")
def profile_meta():
    try:
        import json as _json
        with open(BASE_DIR / "me.json") as f:
            me = _json.load(f)
        personal = me.get("personal", {})
        prefs = me.get("job_preferences", {})
        sal = prefs.get("salary_range", {})
        visa = personal.get("visa", {})
        return _json.dumps({
            "name": personal.get("name", ""),
            "location": personal.get("location", "").split(",")[0],
            "visa_type": visa.get("type", "").split()[1] if visa.get("type") else "",
            "salary_min": sal.get("min", 0),
            "salary_max": sal.get("max", 0),
            "currency": sal.get("currency", "AUD"),
        }), 200, {"Content-Type": "application/json"}
    except Exception:
        return _json.dumps({}), 200, {"Content-Type": "application/json"}


# ─── Jobs ─────────────────────────────────────────────────────────────────────

@app.route("/api/jobs")
def list_jobs():
    status_filter = request.args.get("status", "")
    search = request.args.get("search", "").strip()
    min_score = request.args.get("min_score", 0, type=int)

    query = "SELECT * FROM jobs WHERE (discarded=0 OR discarded IS NULL)"
    params = []

    if status_filter:
        query += " AND COALESCE(status,'Not Applied') = ?"
        params.append(status_filter)
    if search:
        query += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        params += [f"%{search}%"] * 3
    if min_score > 0:
        query += " AND score >= ?"
        params.append(min_score)

    query += " ORDER BY COALESCE(score,-1) DESC, date_posted DESC"

    with _db() as conn:
        rows = conn.execute(query, params).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/jobs/<job_id>")
def get_job(job_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", [job_id]).fetchone()
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.route("/api/jobs/<job_id>", methods=["PATCH"])
def update_job(job_id):
    data = request.json or {}
    allowed = {"status", "discarded", "score", "score_reason"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "no valid fields"}), 400
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with _db() as conn:
        conn.execute(
            f"UPDATE jobs SET {set_clause} WHERE job_id=?",
            list(updates.values()) + [job_id],
        )
        conn.commit()
    return jsonify({"ok": True})


# ─── Stats ────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def stats():
    with _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE discarded=0 OR discarded IS NULL"
        ).fetchone()[0]
        scored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE scored=1 AND (discarded=0 OR discarded IS NULL)"
        ).fetchone()[0]
        avg_score = conn.execute(
            "SELECT AVG(score) FROM jobs WHERE scored=1 AND (discarded=0 OR discarded IS NULL)"
        ).fetchone()[0]
        status_rows = conn.execute(
            "SELECT COALESCE(status,'Not Applied'), COUNT(*) FROM jobs "
            "WHERE discarded=0 OR discarded IS NULL GROUP BY status"
        ).fetchall()
        generated = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE resume_text IS NOT NULL AND resume_text != '' "
            "AND (discarded=0 OR discarded IS NULL)"
        ).fetchone()[0]

    by_status = dict(status_rows)
    return jsonify(
        {
            "total": total,
            "scored": scored,
            "generated": generated,
            "avg_score": round(avg_score or 0, 1),
            "by_status": by_status,
        }
    )


# ─── Background tasks ─────────────────────────────────────────────────────────

def _bg(task_id: str, fn, *args):
    def run():
        try:
            result = fn(*args)
            _tasks[task_id] = {"running": False, "status": "Done", "result": result}
        except Exception as exc:
            _tasks[task_id] = {"running": False, "status": f"Error: {exc}", "result": None}

    _tasks[task_id] = {"running": True, "status": "Running…", "result": None}
    threading.Thread(target=run, daemon=True).start()


@app.route("/api/usage")
def usage():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _db() as conn:
        all_ = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0) FROM api_usage"
        ).fetchone()
        tod = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0) FROM api_usage WHERE timestamp LIKE ?",
            [f"{today}%"],
        ).fetchone()
        ops = conn.execute(
            "SELECT operation, COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0) FROM api_usage GROUP BY operation ORDER BY SUM(cost_usd) DESC"
        ).fetchall()
    return jsonify({
        "total":   {"calls": all_[0], "input_tokens": all_[1], "output_tokens": all_[2], "cost_usd": round(all_[3], 4)},
        "today":   {"calls": tod[0],  "input_tokens": tod[1],  "output_tokens": tod[2],  "cost_usd": round(tod[3], 4)},
        "by_op":   [{"operation": r[0], "calls": r[1], "input_tokens": r[2], "output_tokens": r[3], "cost_usd": round(r[4], 4)} for r in ops],
        "note":    "Costs are estimates based on published Anthropic pricing.",
    })


@app.route("/api/apify-usage")
def apify_usage():
    key = os.getenv("APIFY_API_KEY")
    if not key:
        return jsonify({"error": "APIFY_API_KEY not set"}), 500
    try:
        r = _requests.get(
            "https://api.apify.com/v2/users/me",
            params={"token": key},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        plan = data.get("plan", {})
        usage_obj = data.get("monthlyUsage", {})
        return jsonify({
            "plan_id":        plan.get("id", "unknown"),
            "plan_name":      plan.get("name") or plan.get("id", "unknown"),
            "credits_total":  plan.get("monthlyUsageCreditsUsd", 0),
            "credits_used":   usage_obj.get("monthlyUsageCycleCreditsUsd", 0),
            "cycle_start":    data.get("currentBillingPeriodStart"),
            "cycle_end":      data.get("currentBillingPeriodEnd"),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/task/<task_id>")
def task_status(task_id):
    task = _tasks.get(task_id, {"running": False, "status": "Not started", "result": None})
    return jsonify(task)


# ─── Crawl ────────────────────────────────────────────────────────────────────

@app.route("/api/crawl", methods=["POST"])
def crawl():
    if _tasks.get("crawl", {}).get("running"):
        return jsonify({"error": "Crawler already running"}), 400
    from crawler.seek_crawler import crawl as _crawl
    _bg("crawl", _crawl)
    return jsonify({"task_id": "crawl"})


# ─── Score ────────────────────────────────────────────────────────────────────

@app.route("/api/score", methods=["POST"])
def score_all():
    if _tasks.get("score", {}).get("running"):
        return jsonify({"error": "Scorer already running"}), 400
    from scorer.score_jobs import score_all_jobs
    _bg("score", score_all_jobs)
    return jsonify({"task_id": "score"})


@app.route("/api/score/<job_id>", methods=["POST"])
def score_one(job_id):
    from scorer.score_jobs import score_job
    try:
        result = score_job(job_id)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ─── Generate ─────────────────────────────────────────────────────────────────

@app.route("/api/generate/<job_id>", methods=["POST"])
def generate(job_id):
    force = request.args.get("force", "false").lower() == "true"
    from generator.resume_generator import generate_for_job
    try:
        result = generate_for_job(job_id, force=force)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ─── Download ─────────────────────────────────────────────────────────────────

@app.route("/api/download/<job_id>/<doc_type>")
def download(job_id, doc_type):
    # doc_type: resume_docx | resume_pdf | cover_docx | cover_pdf
    ext_map = {"resume_docx": ".docx", "resume_pdf": ".pdf", "cover_docx": ".docx", "cover_pdf": ".pdf"}
    stem_map = {"resume_docx": "resume", "resume_pdf": "resume", "cover_docx": "cover", "cover_pdf": "cover"}
    if doc_type not in ext_map:
        abort(400)

    filepath = OUTPUT_DIR / f"{job_id}_{stem_map[doc_type]}{ext_map[doc_type]}"

    # If the PDF is missing but the DOCX exists, try to generate it now
    if not filepath.exists() and ext_map[doc_type] == ".pdf":
        docx_path = OUTPUT_DIR / f"{job_id}_{stem_map[doc_type]}.docx"
        if docx_path.exists():
            try:
                from generator.resume_generator import _to_pdf
                _to_pdf(docx_path, filepath)
            except Exception:
                pass  # will 404 below; user can still download DOCX

    if not filepath.exists():
        abort(404)

    with _db() as conn:
        row = conn.execute("SELECT title, company FROM jobs WHERE job_id=?", [job_id]).fetchone()

    try:
        import json as _json
        with open(BASE_DIR / "me.json") as _f:
            _me = _json.load(_f)
        _full_name = _me["personal"]["name"].replace(" ", "_")
    except Exception:
        _full_name = "Candidate"

    company = (row["company"] or "company").replace(" ", "_")[:20] if row else "company"
    title = (row["title"] or "role").replace(" ", "_")[:25] if row else "role"
    nice_name = f"{_full_name}_{stem_map[doc_type].capitalize()}_{company}_{title}{ext_map[doc_type]}"

    return send_file(filepath, as_attachment=True, download_name=nice_name)


if __name__ == "__main__":
    init_db()
    print("Job Hunter Dashboard -> http://localhost:5000")
    app.run(port=5000, debug=True)
