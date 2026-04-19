"""/api/keys/* and /api/extract-keys."""

from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request

from .. import keys as keystore
from .. import opentagviewer_export
from .. import vm

bp = Blueprint("keys", __name__)


@bp.route("/api/keys", methods=["GET"])
def list_keys():
    return jsonify(keystore.list_metadata())


@bp.route("/api/keys/<name>", methods=["DELETE"])
def delete_key(name: str):
    if not keystore.delete(name):
        return jsonify({"error": "Key not found"}), 404
    return jsonify({"status": "deleted"})


@bp.route("/api/keys/upload", methods=["POST"])
def upload_keys():
    if "file" in request.files:
        f = request.files["file"]
        if not f.filename.endswith(".json"):
            return jsonify({"error": "Only .json files accepted"}), 400
        try:
            data = json.loads(f.read())
            stem = keystore.save_upload(data, fallback_name=f.filename.rsplit(".", 1)[0])
            return jsonify({"status": "ok", "name": stem})
        except Exception as e:
            return jsonify({"error": f"Invalid key file: {e}"}), 400
    if request.is_json:
        try:
            stem = keystore.save_upload(request.get_json(), fallback_name="unknown")
            return jsonify({"status": "ok", "name": stem})
        except Exception as e:
            return jsonify({"error": f"Invalid key data: {e}"}), 400
    return jsonify({"error": "Send a JSON file or JSON body"}), 400


@bp.route("/api/extract-keys", methods=["POST"])
def extract_keys():
    try:
        return jsonify(vm.trigger_key_extraction())
    except vm.VmError as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/api/export/opentagviewer.zip", methods=["GET"])
def export_opentagviewer_zip():
    try:
        result = opentagviewer_export.build_zip()
    except opentagviewer_export.NoPlistsError as e:
        return jsonify({"error": str(e)}), 404
    return Response(
        result.data,
        mimetype="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="opentagviewer.zip"',
            "X-Airtag-Count": str(result.tag_count),
        },
    )


@bp.route("/api/export/opentagviewer", methods=["GET"])
def export_opentagviewer_info():
    try:
        result = opentagviewer_export.build_zip()
    except opentagviewer_export.NoPlistsError as e:
        return jsonify({"available": False, "error": str(e)})
    return jsonify({
        "available": True,
        "tag_count": result.tag_count,
        "size_bytes": len(result.data),
    })
