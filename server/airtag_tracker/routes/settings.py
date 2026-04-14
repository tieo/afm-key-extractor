"""/api/settings — GET/PUT polling config."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import settings as cfg

bp = Blueprint("settings", __name__)


@bp.route("/api/settings", methods=["GET"])
def get_settings():
    s = cfg.load()
    return jsonify({
        **s,
        "adaptive": s.get("adaptive", True),
        "state": {
            "moving": cfg.state["moving"],
            "current_interval": cfg.state["current_interval"],
            "idle_count": cfg.state["idle_count"],
            "last_poll": cfg.state["last_poll"],
        },
    })


@bp.route("/api/settings", methods=["PUT"])
def update_settings():
    return jsonify(cfg.update(request.get_json() or {}))
