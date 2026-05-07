import threading
import webbrowser
import time
from pathlib import Path
from app import app, init_db

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
    threading.Thread(target=_open_browser, daemon=True).start()
    print("\n  Job Hunter Dashboard -> http://localhost:5000\n")
    app.run(port=5000, debug=True, use_reloader=True)
