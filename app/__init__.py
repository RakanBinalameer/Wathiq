import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect

from . import db as dbmod

csrf = CSRFProtect()
login_manager = LoginManager()
limiter = Limiter(get_remote_address, default_limits=["600 per hour"], storage_uri="memory://")
compress = Compress()
log = logging.getLogger("nursevoice")

ROOT = Path(__file__).resolve().parent.parent


def create_app(test_config=None):
    app = Flask(__name__, instance_path=str(ROOT / "instance"))
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    force_https = os.environ.get("FORCE_HTTPS") == "1"
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)
        log.warning("SECRET_KEY not set: using a random key (sessions reset on restart). Set it in production.")
    app.config.update(
        SECRET_KEY=secret,
        DATABASE=os.environ.get("DATABASE", str(Path(app.instance_path) / "nursevoice.db")),
        DATA_DIR=str(ROOT / "data"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=force_https,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),  # shared ward terminals: short idle timeout
        WTF_CSRF_TIME_LIMIT=None,
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7),
        TIMEZONE="Asia/Riyadh",
        COMPRESS_MIMETYPES=["text/html", "text/css", "application/javascript", "application/json"],
    )
    if test_config:
        app.config.update(test_config)

    _ensure_db(app)

    csrf.init_app(app)
    limiter.init_app(app)
    compress.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.login"
    Talisman(
        app,
        force_https=force_https,
        session_cookie_secure=force_https,
        frame_options="DENY",
        referrer_policy="same-origin",
        content_security_policy={
            "default-src": "'self'", "script-src": "'self'", "style-src": "'self'",
            "img-src": ["'self'", "data:"], "font-src": "'self'", "connect-src": "'self'",
            "media-src": ["'self'", "blob:"], "frame-ancestors": "'none'",
            "base-uri": "'self'", "form-action": "'self'",
        },
        permissions_policy={"microphone": "(self)", "camera": "()", "geolocation": "()"},
    )
    app.teardown_appcontext(dbmod.close_db)

    from .kb import KnowledgeBase
    con = dbmod.connect(app.config["DATABASE"])
    app.extensions["kb"] = KnowledgeBase(con)
    con.close()

    tz = ZoneInfo(app.config["TIMEZONE"])

    @app.template_filter("local")
    def local_time(value):
        from datetime import datetime, timezone
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M")

    from .views import bp
    app.register_blueprint(bp)

    @app.after_request
    def cache_headers(resp):
        if request.path.startswith("/api/") or resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store"  # clinical data: never cache
        return resp

    def _err(code, msg):
        if request.path.startswith("/api/"):
            return jsonify(error=msg), code
        return render_template("error.html", code=code, msg=msg), code

    app.register_error_handler(404, lambda e: _err(404, "الصفحة غير موجودة"))
    app.register_error_handler(413, lambda e: _err(413, "الملف كبير جداً"))
    app.register_error_handler(429, lambda e: _err(429, "محاولات كثيرة، انتظر قليلاً ثم أعد المحاولة"))
    app.register_error_handler(400, lambda e: _err(400, "طلب غير صالح"))
    return app


def _ensure_db(app):
    """First run convenience: build the KB + a demo nurse so `flask run` just works."""
    path = Path(app.config["DATABASE"])
    if path.exists():
        con = dbmod.connect(str(path))      # existing DB: add any tables introduced later (all statements are IF NOT EXISTS)
        dbmod.init_schema(con)
        con.close()
        return
    from .seed import build_kb, create_user
    con, stats = build_kb(str(path), app.config["DATA_DIR"])
    pw = os.environ.get("DEMO_PASSWORD") or secrets.token_urlsafe(9)
    create_user(con, "nurse1", pw, "ممرضة تجريبية", "nurse")
    con.commit()
    con.close()
    log.warning("KB created %s", stats)
    if not os.environ.get("DEMO_PASSWORD"):
        print(f"\n*** Demo login -> user: nurse1  password: {pw}   (shown once) ***\n", flush=True)
