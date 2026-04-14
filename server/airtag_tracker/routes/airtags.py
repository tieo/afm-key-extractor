"""/api/airtags/* and /api/poll."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import db, polling
from ..events import emit

bp = Blueprint("airtags", __name__)


@bp.route("/api/airtags")
def list_airtags():
    return jsonify(db.latest_per_airtag())


@bp.route("/api/airtags/<airtag_id>/history")
def airtag_history(airtag_id: str):
    since = request.args.get("since", "1970-01-01T00:00:00")
    limit = int(request.args.get("limit", "1000"))
    return jsonify(db.history(airtag_id, since, limit))


@bp.route("/api/poll", methods=["POST"])
def trigger_poll():
    emit("info", "poll", "Manual poll triggered")
    polling.poll_async()
    return jsonify({"status": "polling"})
