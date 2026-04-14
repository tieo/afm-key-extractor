"""/api/account/*."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import account_storage, auth, keys
from ..events import emit

bp = Blueprint("account", __name__)


@bp.route("/api/account/status")
def status():
    return jsonify({
        "configured": account_storage.exists(),
        "airtags": keys.count(),
    })


@bp.route("/api/account/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    try:
        return jsonify(auth.begin(data.get("email"), data.get("password")))
    except auth.LoginError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        auth.clear_pending()
        emit("error", "account", f"Login failed: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/account/2fa", methods=["POST"])
def submit_2fa():
    data = request.get_json() or {}
    try:
        return jsonify(auth.submit_2fa(data.get("code"), data.get("method", 0)))
    except auth.LoginError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        emit("error", "account", f"2FA error: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/account/2fa/request", methods=["POST"])
def request_2fa():
    data = request.get_json() or {}
    try:
        auth.request_2fa(data.get("method", 0))
        return jsonify({"status": "sent"})
    except auth.LoginError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
