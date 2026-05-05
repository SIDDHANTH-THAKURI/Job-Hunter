import threading
import webbrowser
import time
from app import app, init_db

def _open_browser():
    time.sleep(1.5)
    webbrowser.open("http://localhost:5000")

if __name__ == "__main__":
    init_db()
    threading.Thread(target=_open_browser, daemon=True).start()
    print("\n  Job Hunter Dashboard -> http://localhost:5000\n")
    app.run(port=5000, debug=True, use_reloader=True)
