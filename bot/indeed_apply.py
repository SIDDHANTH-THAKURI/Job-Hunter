"""
Indeed Easy Apply bot — Playwright headed Chromium, Option B cookie persistence.

First run: bot pauses at Indeed login page → you log in manually (Google auth or email) →
cookies saved to bot/session.json → all future runs restore session automatically.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR     = Path(__file__).parent.parent
DB_PATH      = BASE_DIR / "data" / "jobs.db"
OUTPUT_DIR   = BASE_DIR / "output"
CHROME_PROFILE = Path(__file__).parent / "chrome_profile"
ME_PATH        = BASE_DIR / "me.json"

INDEED_HOME  = "https://au.indeed.com"


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _load_profile() -> dict:
    with open(ME_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def _get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_setting(conn, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# ─── Login check ─────────────────────────────────────────────────────────────

def _ensure_logged_in(page):
    """Navigate to Indeed; if not logged in, wait silently for manual login."""
    page.goto(INDEED_HOME, timeout=60000, wait_until="domcontentloaded")

    print("\n[bot] Browser opened. Waiting 5 seconds…")
    time.sleep(5)

    sign_in_btn = page.query_selector("[data-gnav-element-name='SignIn']")
    if sign_in_btn is None:
        print("[bot] Already logged in (profile remembered).")
        return

    print("[bot] ⚠  Not logged in.")
    print("[bot] Please sign into Indeed in the browser window (Google auth is fine).")
    print("[bot] Feel free to browse around a bit first — helps with Cloudflare.")
    print("[bot] Waiting up to 5 minutes. Bot will not touch the browser.\n")

    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(10)
        try:
            sign_in_btn = page.query_selector("[data-gnav-element-name='SignIn']")
            if sign_in_btn is None:
                time.sleep(5)
                print("[bot] Login detected — profile saved.")
                return
        except Exception:
            pass

    raise TimeoutError("[bot] Login not completed within 5 minutes.")


# ─── Claude screening helper ──────────────────────────────────────────────────

def _answer_question(question: str, me: dict) -> str:
    """Use Claude to answer an Indeed screening question given the candidate profile."""
    import anthropic
    client = anthropic.Anthropic()
    prompt = (
        f"You are filling out a job application for {me.get('personal', {}).get('name', 'the candidate')}.\n"
        f"Candidate profile:\n{json.dumps(me, indent=2)}\n\n"
        f"Answer this screening question concisely and honestly (1-3 sentences max):\n{question}"
    )
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ─── Form filling ─────────────────────────────────────────────────────────────

def _fill_text(page, selector: str, value: str):
    el = page.query_selector(selector)
    if el:
        el.triple_click()
        el.fill(value)


def _get_form_frame(page):
    """Return the frame containing the Indeed apply form.
    On smartapply.indeed.com it's the full page — no iframe needed."""
    if "smartapply.indeed.com" in page.url:
        return page
    # Legacy modal path — check for iframe
    for selector in ["iframe[src*='indeedapply']", "iframe[id*='indeedapply']", "iframe[title*='Apply']"]:
        el = page.query_selector(selector)
        if el:
            frame = el.content_frame()
            if frame:
                return frame
    return page


def _safe_click(el):
    try:
        el.evaluate("el => el.scrollIntoView({block: 'center'})")
        time.sleep(0.5)
    except Exception:
        pass
    try:
        el.click(timeout=10000)
    except Exception:
        try:
            el.click(force=True, timeout=10000)
        except Exception:
            # Last resort — JS click bypasses all visibility checks
            el.evaluate("el => el.click()")


def _handle_easy_apply_form(page, job: dict, me: dict, resume_pdf: Path, cover_pdf: Path) -> bool:
    """
    Step through an Indeed Easy Apply multi-page form (handles iframe).
    Returns True if submitted successfully, False otherwise.
    """
    personal = me.get("personal", {})
    name_parts = personal.get("name", "").split()
    first = name_parts[0] if name_parts else ""
    last  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    email = personal.get("email", os.getenv("INDEED_EMAIL", ""))
    phone = personal.get("phone", "")

    max_pages = 10
    for _ in range(max_pages):
        time.sleep(2)
        try:
            page.evaluate("document.body.style.zoom='0.5'")
        except Exception:
            pass
        time.sleep(2)  # wait for buttons to activate
        f = _get_form_frame(page)

        # Fill common fields if present
        _fill_text(f, "input[name='applicant.name']", personal.get("name", ""))
        _fill_text(f, "input[name='applicant.firstName']", first)
        _fill_text(f, "input[name='applicant.lastName']", last)
        _fill_text(f, "input[name='applicant.emailAddress']", email)
        _fill_text(f, "input[name='applicant.phoneNumber']", phone)

        # Resume upload
        resume_input = f.query_selector("input[type='file']")
        if resume_input and resume_pdf.exists():
            resume_input.set_input_files(str(resume_pdf))
            time.sleep(2)

        # Cover letter upload
        cover_input = f.query_selector("input[type='file'][name*='cover']")
        if cover_input and cover_pdf.exists():
            cover_input.set_input_files(str(cover_pdf))
            time.sleep(1)

        # Answer screening questions
        question_els = f.query_selector_all("label.ia-Questions-item--label, label[for*='question']")
        for q_el in question_els:
            question_text = q_el.inner_text().strip()
            if not question_text or len(question_text) < 5:
                continue
            input_id = q_el.get_attribute("for")
            if not input_id:
                continue
            # Use attribute selector to handle special chars in IDs like :r10:
            answer_el = f.query_selector(f'[id="{input_id}"]')
            if not answer_el:
                continue
            tag = answer_el.evaluate("el => el.tagName.toLowerCase()")
            if tag == "input":
                input_type = answer_el.get_attribute("type") or "text"
                if input_type in ("text", "number", "tel"):
                    current = answer_el.input_value()
                    if not current:
                        answer = _answer_question(question_text, me)
                        answer_el.fill(answer)
                elif input_type == "radio":
                    # Find all radio options for this question and pick the best one
                    radio_name = answer_el.get_attribute("name")
                    if radio_name:
                        options = f.query_selector_all(f'[name="{radio_name}"]')
                        option_labels = []
                        for opt in options:
                            opt_id = opt.get_attribute("id") or ""
                            lbl = f.query_selector(f'label[for="{opt_id}"]')
                            option_labels.append(lbl.inner_text().strip() if lbl else "")
                        if option_labels:
                            choices = "\n".join(f"{i+1}. {l}" for i, l in enumerate(option_labels))
                            prompt = f"{question_text}\nOptions:\n{choices}\nReply with just the number of the best option."
                            answer = _answer_question(prompt, me)
                            # Extract number from answer
                            import re as _re
                            m = _re.search(r"\d+", answer)
                            idx = int(m.group()) - 1 if m else 0
                            idx = max(0, min(idx, len(options) - 1))
                            _safe_click(options[idx])
            elif tag == "textarea":
                current = answer_el.input_value()
                if not current:
                    answer = _answer_question(question_text, me)
                    answer_el.fill(answer)

        # Handle select dropdowns
        select_els = f.query_selector_all("select")
        for sel_el in select_els:
            current = sel_el.input_value()
            if current:
                continue
            # Get the label for this select
            sel_id = sel_el.get_attribute("id") or ""
            lbl_el = f.query_selector(f'label[for="{sel_id}"]') if sel_id else None
            question_text = lbl_el.inner_text().strip() if lbl_el else "Select the best option"
            options = sel_el.query_selector_all("option")
            option_texts = [o.inner_text().strip() for o in options if o.get_attribute("value")]
            if option_texts:
                choices = "\n".join(f"{i+1}. {t}" for i, t in enumerate(option_texts))
                prompt = f"{question_text}\nOptions:\n{choices}\nReply with just the number of the best option."
                answer = _answer_question(prompt, me)
                import re as _re
                m = _re.search(r"\d+", answer)
                idx = int(m.group()) - 1 if m else 0
                idx = max(0, min(idx, len(option_texts) - 1))
                sel_el.select_option(index=idx + 1)  # +1 to skip blank first option

        # Submit button
        submit_btn = f.query_selector(
            "button[data-testid='submit-application-button'], "
            "button[aria-label*='Submit'], button:has-text('Submit application'), "
            "button:has-text('Submit')"
        )
        if submit_btn:
            _safe_click(submit_btn)
            time.sleep(2)
            return True

        # Next / Continue — broad selector covering Indeed's smartapply buttons
        next_btn = f.query_selector(
            "button[data-testid='continue-button'], "
            "button[data-testid='next-button'], "
            "button:has-text('Continue'), "
            "button:has-text('Next'), "
            "button:has-text('Tell us more'), "
            "button:has-text('Review your application'), "
            "button[type='submit']"
        )
        if next_btn:
            print(f"    [bot] Clicking: {next_btn.inner_text().strip()}")
            _safe_click(next_btn)
            time.sleep(2)
            continue

        # Nothing found — print buttons on page for debugging
        all_btns = f.query_selector_all("button")
        print(f"    [bot] No next/submit found. Buttons on page: {[b.inner_text().strip() for b in all_btns]}")
        break

    return False


# ─── Core apply function ──────────────────────────────────────────────────────

def apply_to_job(page, context, job: dict, me: dict) -> str:
    """
    Attempt to apply to one job.
    Returns: 'applied' | 'skipped_external' | 'skipped_no_button' | 'error:<msg>'
    """
    job_id  = job["job_id"]
    job_url = job["job_url"]
    resume_pdf = OUTPUT_DIR / f"{job_id}_resume.pdf"
    cover_pdf  = OUTPUT_DIR / f"{job_id}_cover.pdf"

    try:
        page.goto(job_url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(5)

        # If Cloudflare challenge is showing, wait for user to solve it
        deadline_cf = time.time() + 120
        while time.time() < deadline_cf:
            cf = page.query_selector("div#challenge-stage, #cf-challenge-running, #challenge-form, [data-ray]")
            title = page.title().lower()
            if cf or "just a moment" in title or "additional verification" in title:
                print(f"    [bot] Cloudflare detected — please solve the challenge in the browser…")
                time.sleep(5)
            else:
                break

        # Look for Indeed Easy Apply button (stays on Indeed)
        apply_btn = page.query_selector(
            "button[id*='indeedApplyButton'], button[data-indeed-apply], "
            "button:has-text('Apply now'), button:has-text('Easy Apply'), "
            "button:has-text('Apply with Indeed'), button:has-text('Apply with indeed')"
        )
        if not apply_btn:
            return "skipped_no_button"

        # Check if it's an external redirect button
        aria_label = (apply_btn.get_attribute("aria-label") or "").lower()
        btn_text   = (apply_btn.inner_text() or "").lower()
        if "external" in aria_label or "external" in btn_text:
            return "skipped_external"

        apply_btn.click()

        # Wait for the apply modal/form to appear (up to 15 seconds)
        print(f"    [bot] Waiting for apply form to load…")
        form_appeared = False
        for _ in range(15):
            time.sleep(1)
            # Check for modal or iframe appearing
            modal = page.query_selector(
                "div[data-testid*='modal'], div[class*='ApplyModal'], "
                "div[class*='applyModal'], iframe[src*='indeedapply'], "
                "iframe[id*='indeedapply'], div[id*='apply']"
            )
            if modal:
                form_appeared = True
                break

        if not form_appeared:
            time.sleep(3)  # give it one more chance

        # Detect if we got redirected off Indeed (external apply)
        if "indeed.com" not in page.url:
            return "skipped_external"

        print(f"    [bot] Form URL: {page.url}")

        success = _handle_easy_apply_form(page, job, me, resume_pdf, cover_pdf)
        return "applied" if success else "skipped_no_button"

    except Exception as exc:
        return f"error:{exc}"


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_apply_session(job_ids: list[str] | None = None, daily_cap: int = 20, log_fn=print):
    """
    Run an apply session.
    job_ids: specific jobs to apply to (None = query DB for eligible jobs).
    daily_cap: max applications this session.
    log_fn: callable for status messages (used by pipeline to capture output).
    """
    from playwright.sync_api import sync_playwright

    me = _load_profile()
    conn = _db()

    if job_ids is None:
        threshold = int(_get_setting(conn, "indeed_auto_apply_threshold", "7"))
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE source='indeed'
                 AND (discarded=0 OR discarded IS NULL)
                 AND COALESCE(status,'Not Applied')='Not Applied'
                 AND scored=1
                 AND score >= ?
               ORDER BY score DESC""",
            (threshold,),
        ).fetchall()
        jobs = [dict(r) for r in rows]
    else:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE job_id IN ({','.join('?'*len(job_ids))})",
            job_ids,
        ).fetchall()
        jobs = [dict(r) for r in rows]

    if not jobs:
        log_fn("[bot] No eligible Indeed jobs to apply to.")
        conn.close()
        return {"applied": 0, "skipped": 0, "errors": 0}

    log_fn(f"[bot] {len(jobs)} eligible jobs. Daily cap: {daily_cap}")

    applied = skipped = errors = 0

    CHROME_PROFILE.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        # Persistent profile — Chrome remembers login, cookies, history between runs
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(CHROME_PROFILE),
            channel="chrome",
            headless=False,
            slow_mo=50,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            no_viewport=True,
        )
        page = context.new_page()
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except Exception:
            pass

        try:
            _ensure_logged_in(page)

            for job in jobs[:daily_cap]:
                if applied >= daily_cap:
                    break

                log_fn(f"[bot] → {job['title']} @ {job['company']} ({job['job_id']})")
                result = apply_to_job(page, context, job, me)
                now = datetime.now(timezone.utc).isoformat()


                if result == "applied":
                    conn.execute(
                        "UPDATE jobs SET status='Applied', applied_at=? WHERE job_id=?",
                        (now, job["job_id"]),
                    )
                    conn.commit()
                    applied += 1
                    log_fn(f"    ✓ Applied")
                elif result.startswith("error:"):
                    errors += 1
                    log_fn(f"    ✗ {result}")
                else:
                    skipped += 1
                    log_fn(f"    — Skipped ({result})")

        finally:
            context.close()

    conn.close()
    summary = {"applied": applied, "skipped": skipped, "errors": errors}
    log_fn(f"[bot] Session complete — {summary}")
    return summary


if __name__ == "__main__":
    run_apply_session()
