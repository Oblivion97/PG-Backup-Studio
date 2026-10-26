"""Flask app, JSON API, serves static/index.html; binds 127.0.0.1."""

import os
from pathlib import Path

from flask import Flask, send_from_directory

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> Flask:
    """Build the Flask application and register its routes."""
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    return app


def main() -> None:
    """Run the development server on 127.0.0.1 (port from PORT, default 8000)."""
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), threaded=True)


if __name__ == "__main__":
    main()
