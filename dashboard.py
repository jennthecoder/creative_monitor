"""Start the Creative Monitor web app: http://127.0.0.1:8765  (see web/app.py).
Auto-reloads on code and template changes (no debugger)."""
from web.app import app

if __name__ == "__main__":
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.run(host="127.0.0.1", port=8765, debug=False, use_reloader=True)
