"""Static index + /api/log."""

from __future__ import annotations

from flask import Blueprint, jsonify, request, send_from_directory

from ..config import STATIC_DIR
from ..events import snapshot

bp = Blueprint("system", __name__)


@bp.route("/")
def index():
    resp = send_from_directory(str(STATIC_DIR), "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@bp.route("/css/<path:filename>")
def css(filename):
    return send_from_directory(str(STATIC_DIR / "css"), filename)


@bp.route("/js/<path:filename>")
def js(filename):
    return send_from_directory(str(STATIC_DIR / "js"), filename)


@bp.route("/api/log")
def get_log():
    since = request.args.get("since")
    cat = request.args.get("cat")
    limit = int(request.args.get("limit", "100"))
    entries = snapshot()
    if since:
        entries = [e for e in entries if e["ts"] > since]
    if cat:
        entries = [e for e in entries if e["cat"] == cat]
    return jsonify(entries[-limit:])
