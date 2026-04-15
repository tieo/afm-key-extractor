"""/api/vm/*."""

from __future__ import annotations

from flask import Blueprint, jsonify

from .. import vm as vmmgr
from .. import vm_apple_signin

bp = Blueprint("vm", __name__)


@bp.route("/api/vm/status")
def status():
    st = vmmgr.status()
    st["apple_signin"] = vm_apple_signin.status()
    return jsonify(st)


def _action(fn):
    try:
        return jsonify(fn())
    except vmmgr.VmError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/vm/start-setup", methods=["POST"])
def start_setup():
    return _action(vmmgr.start)


@bp.route("/api/vm/start-manual", methods=["POST"])
def start_manual():
    return _action(vmmgr.start_manual)


@bp.route("/api/vm/stop", methods=["POST"])
def stop():
    return _action(vmmgr.stop)


@bp.route("/api/vm/bake-golden", methods=["POST"])
def bake_golden():
    return _action(vmmgr.bake_golden)


@bp.route("/api/vm/reset-to-golden", methods=["POST"])
def reset_to_golden():
    return _action(vmmgr.reset_to_golden)


@bp.route("/api/vm/apple-signin/start", methods=["POST"])
def apple_signin_start():
    from flask import request
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(vm_apple_signin.start(
            email=body.get("email"), password=body.get("password"),
        ))
    except RuntimeError as e:
        if str(e) == "needs_password":
            return jsonify({"error": "needs_password"}), 400
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/vm/apple-signin/status")
def apple_signin_status():
    return jsonify(vm_apple_signin.status())


@bp.route("/api/vm/apple-signin/2fa", methods=["POST"])
def apple_signin_2fa():
    from flask import request
    code = (request.get_json(silent=True) or {}).get("code", "")
    try:
        return jsonify(vm_apple_signin.submit_2fa(code))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/vm/apple-signin/request-sms", methods=["POST"])
def apple_signin_request_sms():
    try:
        return jsonify(vm_apple_signin.request_sms())
    except Exception as e:
        return jsonify({"error": str(e)}), 400
