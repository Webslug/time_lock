"""Flask application factory and JSON API."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file

from . import functions_browse as browse_mod
from . import functions_config as cfg
from . import functions_db as db
from . import functions_log as log
from . import functions_settings as settings_mod
from .functions_lock import LockError, TimeLockStore

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
TOKEN_HEADER = "X-TimeLock-Token"

# Only our own files may load; nothing from the internet, nothing inline for scripts.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; media-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def create_app(store: TimeLockStore | None = None, test_mode: bool = False,
               offline: bool = False, token: str | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(cfg.HTML_DIR),
                static_folder=str(cfg.ASSETS_DIR), static_url_path="/assets")
    app.config["TEST_MODE"] = test_mode
    app.config["TOKEN"] = token or secrets.token_urlsafe(32)   # new for every run
    app.config["PROBLEM"] = None
    app.config["MAX_CONTENT_LENGTH"] = cfg.MAX_IMAGE_BYTES + 5 * 1024 * 1024   # refuse giant uploads early
    if store is None:
        try:
            store = TimeLockStore(offline=offline)
        except db.VaultProblem as problem:
            app.config["PROBLEM"] = problem
    app.config["STORE"] = store

    @app.before_request
    def guard_request():
        """Local access only, no other websites, no DNS tricks, and a per-run token."""
        host = urlparse("//" + request.host).hostname
        if host not in LOCAL_HOSTS:
            return jsonify(error="Local access only."), 403
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
            return jsonify(error="Cross-site requests are not allowed."), 403
        problem = app.config["PROBLEM"]
        if problem is not None and request.endpoint != "static":
            if request.path.startswith("/api/"):
                return jsonify(error=str(problem), code=problem.kind), 503
            return render_template("problem.html", problem=problem,
                                   key_path=cfg.VAULT_KEY_PATH, db_path=cfg.DB_PATH), 503
        if request.path.startswith("/api/"):
            sent = request.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(sent.encode("utf-8", "replace"), app.config["TOKEN"].encode()):
                log.failure("request without a valid token: %s %s", request.method, request.path)
                return jsonify(error="This page is out of date. Please reload it.",
                               code="bad_token"), 403

    @app.after_request
    def safety_headers(resp):
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
        return resp

    @app.errorhandler(LockError)
    def lock_error(err: LockError):
        if err.status >= 500:
            log.failure("server error: %s", err)
        else:
            log.failure("request refused (%d): %s", err.status, err)
        return jsonify(error=str(err), **err.extra), err.status

    @app.errorhandler(413)
    def too_large(_err):
        return jsonify(error="That file is too large (limit 25 MB)."), 413

    @app.errorhandler(settings_mod.SettingsError)
    def settings_error(err):
        return jsonify(error=str(err)), 400

    @app.get("/")
    def index():
        defaults = store.settings.get()
        return render_template(
            "time_lock.html", test_mode=test_mode, token=app.config["TOKEN"],
            default_seconds=defaults["default_lock_minutes"] * 60, long_seconds=cfg.LONG_LOCK_SECONDS,
            debug_mode=cfg.DEBUG_MODE,
            reveal_hours=cfg.REVEAL_PENALTY_SECONDS // 3600)

    @app.get("/api/items")
    def items():
        return jsonify(store.list_items())

    @app.get("/api/settings")
    def get_settings():
        return jsonify(store.settings.get())

    def _body() -> dict:
        """The JSON body, which must be an object ({...}), never a list or a bare value."""
        body = request.get_json(silent=True)
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise LockError("The request was not understood.")
        return body

    @app.put("/api/settings")
    def put_settings():
        return jsonify(store.update_settings(_body()))

    @app.get("/api/browse")
    def browse():
        try:
            return jsonify(browse_mod.browse(request.args.get("path")))
        except FileNotFoundError as exc:
            raise LockError(str(exc), status=404)

    def _int(source, key) -> int | None:
        value = source.get(key)
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            raise LockError("The number given is not valid.")

    @app.post("/api/items/text")
    def add_text():
        body = _body()
        item_id = store.lock_text(body.get("text", ""), _int(body, "unlock_at"),
                                  body.get("label", ""), bool(body.get("confirm_long")),
                                  random_pick=bool(body.get("random")))
        return jsonify(id=item_id), 201

    @app.post("/api/items/image")
    def add_image():
        form = request.form
        source_path = None
        upload = request.files.get("file")
        if upload and upload.filename:
            raw = upload.read()
        elif form.get("path"):
            source_path = Path(form["path"]).expanduser()
            if not source_path.is_file():
                raise LockError("That image path was not found.")
            raw = source_path.read_bytes()
        else:
            raise LockError("Please choose an image.")
        item_id, deleted = store.lock_image(
            raw, _int(form, "unlock_at"), form.get("label", ""), form.get("confirm_long") == "true",
            source_path, delete_source=form.get("delete_source") == "true",
            random_pick=form.get("random") == "true")
        return jsonify(id=item_id, source_deleted=deleted), 201

    @app.post("/api/items/<item_id>/reveal")
    def reveal(item_id):
        return jsonify(unlock_at=store.reveal(item_id))

    @app.post("/api/items/<item_id>/extend")
    def extend(item_id):
        body = _body()
        return jsonify(store.extend(item_id, _int(body, "seconds") or 0, bool(body.get("confirm_long"))))

    @app.post("/api/decoys")
    def decoys():
        body = _body()
        return jsonify(added=store.add_decoys(_int(body, "count") or 0)), 201

    @app.post("/api/panic-seal")
    def panic_seal():
        body = _body()
        return jsonify(store.panic_seal(_int(body, "seconds") or 0, bool(body.get("confirm_long"))))

    @app.get("/api/items/<item_id>/image.jpg")
    def image(item_id):
        path = store.image_path(item_id)
        return send_file(path, mimetype="image/jpeg", as_attachment=True, download_name=path.name)

    @app.delete("/api/items/<item_id>")
    def delete(item_id):
        store.delete(item_id)
        return jsonify(deleted=True)

    return app
