import os
import gzip
import secrets
import string
import sqlite3
import base64
import uuid
import re
import time
import threading
import traceback
import requests
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from authlib.integrations.flask_client import OAuth
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
    send_from_directory,
    g,
    has_app_context,
    Response,
)
from werkzeug.security import generate_password_hash, check_password_hash

from werkzeug.middleware.proxy_fix import ProxyFix  # добавь к остальным импортам вверху файла

app = Flask(__name__)


def _utcnow():
    """Наивное UTC-время (datetime.utcnow() устарел в Python 3.12)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Vercel/Render терминируют HTTPS на прокси — говорим Flask доверять заголовкам прокси,
# иначе он считает соединение http и Secure-кука сессии не сохраняется.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_IS_PROD = bool(os.environ.get("VERCEL") or os.environ.get("RENDER"))
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    if _IS_PROD:
        # Публичный ключ по умолчанию позволил бы подделывать сессии (в т.ч. админа).
        raise RuntimeError("SECRET_KEY не задан! Добавь случайную строку в переменные окружения.")
    _secret = "dev-only-secret-key-do-not-use-in-prod"
app.secret_key = _secret


session_serializer = URLSafeTimedSerializer(app.secret_key, salt="darkvisuals-mod-session")

SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "3600"))

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True  # на Vercel всегда HTTPS

# Версия статики для cache-busting: меняй при каждом изменении css/js,
# чтобы браузеры с кэшем подхватили новую версию (ссылки вида style.css?v=20260917b).
STATIC_VERSION = os.environ.get("STATIC_VERSION", "20261017a")


@app.context_processor
def inject_static_version():
    return {"STATIC_VERSION": STATIC_VERSION}

@app.after_request
def compress_response(resp):
    """
    Гзипуем HTML/CSS/JS/JSON. Страница /admin весила ~68 КБ несжатыми —
    после сжатия это ~6-8 КБ, что заметно сокращает время загрузки.
    """
    try:
        if os.environ.get("VERCEL") == "1":
            return resp
        if resp.direct_passthrough or resp.status_code < 200 or resp.status_code >= 300:
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return resp
        if resp.headers.get("Content-Encoding"):
            return resp

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not any(t in ctype for t in ("text/html", "text/css", "javascript", "application/json", "text/plain", "image/svg")):
            return resp

        data = resp.get_data()
        if len(data) < 1024:
            return resp

        resp.set_data(gzip.compress(data, 6))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
        resp.headers.add("Vary", "Accept-Encoding")
    except Exception:
        pass
    return resp


@app.after_request
def add_no_cache_headers(resp):
    # Статика (картинки, css, js) — кэшируем надолго, чтобы браузер не слал
    # повторные запросы с ответами 304 (они как раз давали огромные задержки).
    # CSS/JS — исключение: кэшуем недолго (плюс к ссылкам на них добавляется
    # ?v=<версия> из STATIC_VERSION, которая меняется при обновлении статики).
    # Иначе после деплоя браузер мог неделю пользоваться старым style.css.
    path = request.path or ""
    if resp.status_code < 400 and (path.startswith("/static/") or path.startswith("/api/img/")):
        if path.startswith("/static/css/") or path.startswith("/static/js/"):
            resp.headers["Cache-Control"] = "public, max-age=600, must-revalidate"
        else:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        resp.headers.pop("Expires", None)
        resp.headers.pop("Pragma", None)
        return resp

    # Не кэшируем HTML-страницы (особенно приватные, вроде /profile)
    ctype = resp.headers.get("Content-Type", "")
    if "text/html" in ctype:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

# === Google OAuth ===
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

oauth = OAuth(app)
google_oauth = None
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    google_oauth = oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

TELEGRAM_ADMIN_URL = os.environ.get("TELEGRAM_ADMIN_URL", "https://t.me/MrStalk3ryoo")

LAUNCHER_URL = os.environ.get(
    "LAUNCHER_URL",
    "https://www.dropbox.com/scl/fi/demam03yn86ra30uthiwm/DarkVisualsLauncher.exe?rlkey=ygyy9cwlsixm5998khcm5brhu&st=bh4m2m3f&dl=1",
)

FUNPAY_LINKS = {
    "custom_achievement": "https://funpay.com/lots/offer?id=77075919",
    "1_month": "https://funpay.com/lots/offer?id=77075709",
    "120_days": "https://funpay.com/lots/offer?id=77075608",
    "lifetime": "https://funpay.com/lots/offer?id=77075813",
}

PLANS = {
    "1_month": {
        "name": "30 Дней",
        "price": "149 ₽",
        "period": "1 месяц",
        "image": "img/plan_banner.png",
        "features": [
            "Полный доступ к Dark Visuals",
            "Автоматические обновления",
            "Базовая поддержка",
        ],
    },
    "120_days": {
        "name": "120 Дней",
        "price": "399 ₽",
        "period": "4 месяца",
        "image": "img/plan_banner.png",
        "features": [
            "Полный доступ к Dark Visuals",
            "Приоритетные обновления",
            "Быстрая поддержка",
        ],
    },
    "lifetime": {
        "name": "Навсегда",
        "price": "799 ₽",
        "period": "Навсегда",
        "image": "img/plan_banner.png",
        "features": [
            "Вечный доступ без ограничений",
            "Приоритетные обновления",
            "VIP Поддержка",
        ],
    },
    "custom_achievement": {
        "name": "Своё достижение",
        "price": "50 ₽",
        "period": "Разовая услуга",
        "image": "img/achievement.png",
        "features": [
            "Своё кастомное достижение в Dark Visuals",
        ],
    },
}

MOD_AES_KEY_BASE64 = os.environ.get("MOD_AES_KEY_BASE64", "")
MOD_AES_IV_BASE64 = os.environ.get("MOD_AES_IV_BASE64", "")
# /api/mod-key требует валидный sessionToken (его лаунчер получает из /api/login).
# MOD_KEY_REQUIRE_TOKEN=0 — временно отключить проверку (не рекомендуется).
REQUIRE_MOD_KEY_TOKEN = os.environ.get("MOD_KEY_REQUIRE_TOKEN", "1") != "0"
MOD_FILE_URL = os.environ.get(
    "MOD_FILE_URL",
    "https://raw.githubusercontent.com/kryytoi/WDdwdw/refs/heads/main/darkvisuals.enc",
)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "kryytoi/darkvisualasss")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_ACHIEVEMENTS_PATH = os.environ.get("GITHUB_ACHIEVEMENTS_PATH", "api/img")
# Публичный префикс, по которому отдаются иконки достижений с нашего домена
# (а не с raw.githubusercontent.com — это убирает лишний медленный внешний запрос).
ACHIEVEMENTS_PUBLIC_PREFIX = "/api/img/"

ALLOWED_ICON_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def allowed_icon_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_ICON_EXTENSIONS


MAX_UPLOAD_BYTES = 2 * 1024 * 1024
_IMG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|jpeg|gif|webp)$")
_IMG_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
_uploaded_images_ready = False


def ensure_uploaded_images_table(db):
    """Таблица для загруженных иконок (раньше они коммитились в GitHub и ждали редеплоя)."""
    global _uploaded_images_ready
    if _uploaded_images_ready:
        return
    if os.environ.get("DATABASE_URL"):
        ddl = (
            "CREATE TABLE IF NOT EXISTS uploaded_images ("
            "name VARCHAR(80) PRIMARY KEY, mime VARCHAR(40) NOT NULL, data BYTEA NOT NULL, created_at TEXT)"
        )
    else:
        ddl = (
            "CREATE TABLE IF NOT EXISTS uploaded_images ("
            "name TEXT PRIMARY KEY, mime TEXT NOT NULL, data BLOB NOT NULL, created_at TEXT)"
        )
    execute(db, ddl)
    _uploaded_images_ready = True


def upload_image_to_github(file_storage):
    """
    Сохраняет картинку (иконка достижения / фото конфига) в БД и возвращает
    (url, error). Имя функции оставлено для совместимости, GitHub больше не нужен:
    картинка доступна сразу, без коммита и редеплоя.
    """
    if not file_storage or not file_storage.filename:
        return None, None

    if not allowed_icon_file(file_storage.filename):
        return None, "Недопустимый формат файла! Разрешены: png, jpg, jpeg, gif, webp."

    ext = secure_filename(file_storage.filename).rsplit(".", 1)[-1].lower()
    file_bytes = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not file_bytes:
        return None, "Файл пустой!"
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        return None, "Файл слишком большой (максимум 2 МБ)."

    unique_name = f"{uuid.uuid4().hex}.{ext}"
    try:
        db = get_db()
        ensure_uploaded_images_table(db)
        blob = psycopg2.Binary(file_bytes) if os.environ.get("DATABASE_URL") else sqlite3.Binary(file_bytes)
        execute(
            db,
            "INSERT INTO uploaded_images (name, mime, data, created_at) VALUES (%s, %s, %s, %s)",
            (unique_name, _IMG_MIME[ext], blob, _utcnow().isoformat()),
        )
    except Exception as e:
        print(f"[upload_image error]: {e}")
        traceback.print_exc()
        return None, "Не удалось сохранить файл. Попробуйте ещё раз."
    return f"{ACHIEVEMENTS_PUBLIC_PREFIX}{unique_name}", None


def delete_image_from_github(image_url):
    """Удаляет загруженную иконку из БД (файлы из репозитория не трогаем). Ошибки игнорируются."""
    if not image_url or not image_url.startswith(ACHIEVEMENTS_PUBLIC_PREFIX):
        return
    name = image_url[len(ACHIEVEMENTS_PUBLIC_PREFIX):]
    if not _IMG_NAME_RE.match(name):
        return
    try:
        execute(get_db(), "DELETE FROM uploaded_images WHERE name = %s", (name,))
    except Exception as e:
        print(f"[delete_image error]: {e}")
        _rollback_current()


def normalize_achievement_image_url(image_url):
    """
    Старые записи хранят ссылки на raw.githubusercontent.com (внешний медленный
    запрос, который к тому же блокируется браузером как ERR_BLOCKED_BY_ORB).
    Переводим любые ссылки на наш репозиторий в локальные пути того же домена.
    """
    if not image_url:
        return ""

    raw_root = f"https://raw.githubusercontent.com/{GITHUB_REPO}/"
    if image_url.startswith(raw_root):
        rest = image_url[len(raw_root):]
        if rest.startswith("refs/heads/"):
            rest = rest[len("refs/heads/"):]
        # отрезаем имя ветки
        rest = rest.split("/", 1)[1] if "/" in rest else rest
        if rest.startswith("static/adviencement/"):
            return ACHIEVEMENTS_PUBLIC_PREFIX + rest[len("static/adviencement/"):]
        if rest.startswith("api/img/"):
            return ACHIEVEMENTS_PUBLIC_PREFIX + rest[len("api/img/"):]
        if rest.startswith("static/"):
            return "/" + rest
        return ACHIEVEMENTS_PUBLIC_PREFIX + rest.rsplit("/", 1)[-1]

    if image_url.startswith("/static/adviencement/"):
        return ACHIEVEMENTS_PUBLIC_PREFIX + image_url[len("/static/adviencement/"):]

    return image_url


def config_images_list(config):
    """Список непустых фото конфига (до 3 шт.) с нормализованными ссылками."""
    images = []
    for i in (1, 2, 3):
        url = normalize_achievement_image_url(config.get(f"image{i}_url"))
        if url:
            images.append(url)
    return images


_user_config_downloads_ready = False


def ensure_user_config_downloads_table(db):
    """
    Создаёт таблицу выдачи доступа к скачиванию конфигов.
    Нужна отдельно от полного init_db: на Vercel миграции по умолчанию пропускаются.
    """
    global _user_config_downloads_ready
    if _user_config_downloads_ready:
        return
    if not _should_init_db():
        _user_config_downloads_ready = True
        return
    is_postgres = bool(os.environ.get("DATABASE_URL"))
    if is_postgres:
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS user_config_downloads (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                config_id INTEGER NOT NULL,
                granted_at TEXT,
                UNIQUE(user_id, config_id)
            );
            """,
        )
    else:
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS user_config_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                config_id INTEGER NOT NULL,
                granted_at TEXT,
                UNIQUE(user_id, config_id)
            );
            """,
        )
    _user_config_downloads_ready = True


def user_can_download_config(user, config_id, db):
    """Админ всегда может скачать; обычный игрок — только после выдачи доступа."""
    if not user or not config_id:
        return False
    if user.get("is_admin"):
        return True
    grant = fetchone(
        db,
        "SELECT id FROM user_config_downloads WHERE user_id = %s AND config_id = %s",
        (user["id"], config_id),
    )
    return bool(grant)


# ==================================================================
#  ПРОМОКОДЫ (Promo codes) — скидка при покупке
# ==================================================================

_promo_codes_ready = False


def ensure_promo_codes_table(db):
    """
    Создаёт таблицу промокодов.
    Нужна отдельно от полного init_db: на Vercel миграции по умолчанию пропускаются.
    """
    global _promo_codes_ready
    if _promo_codes_ready:
        return
    if not _should_init_db():
        _promo_codes_ready = True
        return
    is_postgres = bool(os.environ.get("DATABASE_URL"))
    if is_postgres:
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                id SERIAL PRIMARY KEY,
                code VARCHAR(64) UNIQUE NOT NULL,
                discount_percent INTEGER NOT NULL,
                funpay_url TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                uses_count INTEGER DEFAULT 0,
                created_at TEXT
            );
            """,
        )
    else:
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                discount_percent INTEGER NOT NULL,
                funpay_url TEXT,
                is_active BOOLEAN DEFAULT 1,
                uses_count INTEGER DEFAULT 0,
                created_at TEXT
            );
            """,
        )
    _promo_codes_ready = True


def normalize_promo_code(raw):
    """Промокоды регистронезависимы: храним и ищем в верхнем регистре."""
    return str(raw or "").strip().upper()[:64]


def parse_price_rub(price_str):
    """'149 ₽' -> 149. Нечисловые символы (в т.ч. неразрывный пробел) отбрасываются."""
    digits = "".join(ch for ch in str(price_str or "") if ch.isdigit())
    return int(digits) if digits else 0


def format_price_rub(amount):
    return f"{int(amount)} ₽"


def discounted_price(price_str, discount_percent):
    """Считает новую цену после скидки промокода: '149 ₽', 10 -> '134 ₽'."""
    base = parse_price_rub(price_str)
    try:
        percent = int(discount_percent)
    except (TypeError, ValueError):
        percent = 0
    percent = max(0, min(100, percent))
    return format_price_rub(round(base * (100 - percent) / 100))


def _connect_db():
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(
            db_url,
            cursor_factory=RealDictCursor,
            connect_timeout=5,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )

    # На Vercel SQLite не работает (read-only, эфемерная ФС)
    if os.environ.get("VERCEL") == "1":
        raise RuntimeError(
            "DATABASE_URL не задан! На Vercel нужен Postgres (Neon/Supabase). "
            "Добавь переменную окружения DATABASE_URL в настройках проекта."
        )

    conn = sqlite3.connect("database.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


# --- Пул соединений Postgres -------------------------------------------------
# Раньше каждый запрос открывал новое подключение (TCP + TLS + auth = несколько
# round-trip'ов до Neon). Теперь соединения переиспользуются между запросами
# в рамках одного процесса/инстанса; «залежавшиеся» проверяются через SELECT 1
# (Neon усыпляет compute и рвёт idle-соединения).
_POOL_MAX = int(os.environ.get("DB_POOL_SIZE", "4"))
_POOL_IDLE_CHECK = 20  # сек: после такого простоя соединение проверяем перед использованием
_pool = []
_pool_lock = threading.Lock()


def _pool_checkout():
    while True:
        with _pool_lock:
            item = _pool.pop() if _pool else None
        if item is None:
            return _connect_db()
        conn, last_used = item
        if conn.closed:
            continue
        if time.monotonic() - last_used > _POOL_IDLE_CHECK:
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
                conn.rollback()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                continue
        return conn


def _pool_checkin(conn):
    try:
        if conn.closed:
            return
        if conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
            conn.rollback()
        with _pool_lock:
            if len(_pool) < _POOL_MAX:
                _pool.append((conn, time.monotonic()))
                return
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


class _SharedConnection:
    """
    Обёртка над соединением: .close() ничего не делает, реальное освобождение
    происходит один раз в конце запроса (соединение возвращается в пул).
    """

    def __init__(self, conn, pooled=False):
        self._conn = conn
        self._pooled = pooled

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        return None

    def _real_close(self):
        if self._pooled:
            _pool_checkin(self._conn)
            return
        try:
            self._conn.close()
        except Exception:
            pass


def get_db():
    # Вне контекста запроса (например, init_db при старте) — обычное соединение.
    if not has_app_context():
        return _connect_db()

    shared = getattr(g, "_shared_db", None)
    if shared is None:
        if os.environ.get("DATABASE_URL"):
            shared = _SharedConnection(_pool_checkout(), pooled=True)
        else:
            shared = _SharedConnection(_connect_db())
        g._shared_db = shared
    return shared


@app.teardown_appcontext
def _close_shared_db(exc=None):
    shared = getattr(g, "_shared_db", None)
    if shared is not None:
        shared._real_close()


def _rollback_current():
    """Откат на уже открытом в этом запросе соединении (новое не открываем)."""
    shared = getattr(g, "_shared_db", None) if has_app_context() else None
    if shared is not None:
        _rollback_if_needed(shared)


def _rollback_if_needed(db):
    try:
        # для psycopg2 — сбросить aborted-транзакцию, для sqlite — безопасный noop
        conn = db._conn if isinstance(db, _SharedConnection) else db
        if hasattr(conn, "rollback"):
            conn.rollback()
    except Exception:
        pass


def execute(db, query, params=()):
    cursor = db.cursor()
    if not os.environ.get("DATABASE_URL"):
        query = query.replace("%s", "?")
    try:
        cursor.execute(query, params)
        db.commit()
    except Exception:
        _rollback_if_needed(db)
        raise
    return cursor


def fetchone(db, query, params=()):
    cursor = db.cursor()
    if not os.environ.get("DATABASE_URL"):
        query = query.replace("%s", "?")
    try:
        cursor.execute(query, params)
    except Exception:
        _rollback_if_needed(db)
        raise
    res = cursor.fetchone()
    if res and not os.environ.get("DATABASE_URL"):
        try:
            res = dict(res)
        except Exception:
            pass
    # RealDictRow -> dict для единообразия
    if res is not None and os.environ.get("DATABASE_URL"):
        try:
            if not isinstance(res, dict):
                res = dict(res)
        except Exception:
            pass
    return res


def fetchall(db, query, params=()):
    cursor = db.cursor()
    if not os.environ.get("DATABASE_URL"):
        query = query.replace("%s", "?")
    try:
        cursor.execute(query, params)
    except Exception:
        _rollback_if_needed(db)
        raise
    res = cursor.fetchall()
    if res and not os.environ.get("DATABASE_URL"):
        try:
            res = [dict(row) for row in res]
        except Exception:
            pass
    if res and os.environ.get("DATABASE_URL"):
        try:
            res = [dict(r) if not isinstance(r, dict) else r for r in res]
        except Exception:
            pass
    return res


def init_db():
    db = get_db()
    is_postgres = bool(os.environ.get("DATABASE_URL"))

    if is_postgres:
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role VARCHAR(20) DEFAULT 'User',
                status VARCHAR(20) DEFAULT 'active',
                hwid TEXT,
                plan VARCHAR(50),
                expires_at TEXT,
                created_at TEXT,
                is_admin BOOLEAN DEFAULT FALSE
            );
            """,
        )
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'User';")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'active';")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS hwid TEXT;")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR(50);")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS expires_at TEXT;")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TEXT;")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;")
        execute(db, "ALTER TABLE users DROP COLUMN IF EXISTS password_plain;")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(64) UNIQUE;")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255);")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;")
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS user_config_downloads (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                config_id INTEGER NOT NULL,
                granted_at TEXT,
                UNIQUE(user_id, config_id)
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS subscription_keys (
                id SERIAL PRIMARY KEY,
                key_code VARCHAR(64) UNIQUE NOT NULL,
                days VARCHAR(20) NOT NULL,
                plan_name VARCHAR(50),
                is_used BOOLEAN DEFAULT FALSE,
                used_by VARCHAR(50),
                created_at TEXT,
                used_at TEXT
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS achievements (
                id SERIAL PRIMARY KEY,
                code VARCHAR(64) UNIQUE NOT NULL,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                image_url TEXT,
                unlock_feature VARCHAR(150),
                created_at TEXT
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS user_achievements (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                achievement_id INTEGER NOT NULL,
                granted_at TEXT,
                UNIQUE(user_id, achievement_id)
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS configs (
                id SERIAL PRIMARY KEY,
                name VARCHAR(150) NOT NULL,
                price VARCHAR(50),
                description TEXT,
                funpay_url TEXT,
                download_url TEXT,
                image1_url TEXT,
                image2_url TEXT,
                image3_url TEXT,
                created_at TEXT
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                id SERIAL PRIMARY KEY,
                code VARCHAR(64) UNIQUE NOT NULL,
                discount_percent INTEGER NOT NULL,
                funpay_url TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                uses_count INTEGER DEFAULT 0,
                created_at TEXT
            );
            """,
        )
    else:
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'User',
                status TEXT DEFAULT 'active',
                hwid TEXT,
                plan TEXT,
                expires_at TEXT,
                created_at TEXT,
                is_admin BOOLEAN DEFAULT FALSE
            );
            """,
        )

        for ddl in (
            "ALTER TABLE users ADD COLUMN google_id TEXT;",
            "ALTER TABLE users ADD COLUMN email TEXT;",
            "ALTER TABLE users ADD COLUMN avatar_url TEXT;",
        ):
            try:
                execute(db, ddl)
            except Exception:
                pass

        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS subscription_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_code TEXT UNIQUE NOT NULL,
                days TEXT NOT NULL,
                plan_name TEXT,
                is_used BOOLEAN DEFAULT 0,
                used_by TEXT,
                created_at TEXT,
                used_at TEXT
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                image_url TEXT,
                unlock_feature TEXT,
                created_at TEXT
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS user_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                achievement_id INTEGER NOT NULL,
                granted_at TEXT,
                UNIQUE(user_id, achievement_id)
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT,
                description TEXT,
                funpay_url TEXT,
                download_url TEXT,
                image1_url TEXT,
                image2_url TEXT,
                image3_url TEXT,
                created_at TEXT
            );
            """,
        )
        execute(
            db,
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                discount_percent INTEGER NOT NULL,
                funpay_url TEXT,
                is_active BOOLEAN DEFAULT 1,
                uses_count INTEGER DEFAULT 0,
                created_at TEXT
            );
            """,
        )

    execute(db, "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    if not is_postgres:
        execute(db, "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id)")
    ensure_uploaded_images_table(db)

    # Админа создаём только если явно задан ADMIN_INITIAL_PASSWORD (никаких admin/admin).
    admin_password = os.environ.get("ADMIN_INITIAL_PASSWORD")
    admin = fetchone(db, "SELECT id FROM users WHERE username = %s", ("admin",))
    if not admin and admin_password:
        now = _utcnow().isoformat()
        execute(
            db,
            """
            INSERT INTO users (username, password_hash, role, status, expires_at, created_at, is_admin, plan)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ("admin", generate_password_hash(admin_password), "Dev", "active", "forever", now, True, "Lifetime"),
        )
    elif not admin:
        print("[init_db] Админ не создан: задай ADMIN_INITIAL_PASSWORD или выдай is_admin через БД.")
    db.close()


# init_db() делает CREATE TABLE IF NOT EXISTS + десятки ALTER TABLE.
# На Vercel это выполняется при каждом холодном старте и добавляет секунды к
# первому запросу. Схема уже создана, поэтому по умолчанию на Vercel миграции
# пропускаются; чтобы прогнать их (после изменения схемы) — выставь RUN_DB_INIT=1.
def _should_init_db():
    flag = os.environ.get("RUN_DB_INIT")
    if flag is not None:
        return flag == "1"
    return os.environ.get("VERCEL") != "1"


if _should_init_db():
    try:
        init_db()
    except Exception as e:
        print(f"[DB Init Warning]: {e}")


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        db = get_db()
        user = fetchone(db, "SELECT * FROM users WHERE id = %s", (user_id,))
        db.close()
        return user
    except Exception as e:
        print(f"[current_user error]: {e}")
        return None


_subscription_keys_ready = False

def ensure_subscription_keys_table(db):
    """Ленивая миграция: на Vercel init_db пропускается, а таблица ключей обязательна для redeem / генерации."""
    global _subscription_keys_ready
    if _subscription_keys_ready:
        return
    if not _should_init_db():
        _subscription_keys_ready = True
        return
    is_postgres = bool(os.environ.get("DATABASE_URL"))
    try:
        if is_postgres:
            execute(
                db,
                """
                CREATE TABLE IF NOT EXISTS subscription_keys (
                    id SERIAL PRIMARY KEY,
                    key_code VARCHAR(64) UNIQUE NOT NULL,
                    days VARCHAR(20) NOT NULL,
                    plan_name VARCHAR(50),
                    is_used BOOLEAN DEFAULT FALSE,
                    used_by VARCHAR(50),
                    created_at TEXT,
                    used_at TEXT
                );
                """,
            )
        else:
            execute(
                db,
                """
                CREATE TABLE IF NOT EXISTS subscription_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_code TEXT UNIQUE NOT NULL,
                    days TEXT NOT NULL,
                    plan_name TEXT,
                    is_used BOOLEAN DEFAULT 0,
                    used_by TEXT,
                    created_at TEXT,
                    used_at TEXT
                );
                """,
            )
        _subscription_keys_ready = True
    except Exception as e:
        print(f"[ensure_subscription_keys_table error]: {e}")
        traceback.print_exc()
        _rollback_if_needed(db)


FOREVER_ALIASES = {
    "forever",
    "навсегда",
    "lifetime",
    "вечно",
    "бессрочно",
    "бессрочная",
    "бессрочный",
    "infinity",
    "infinite",
    "inf",
    "перманент",
    "permanent",
    "perm",
    "всегда",
    "вечная",
    "вечный",
    "max",
    "all",
    "unlimited",
    "безлимит",
    "безлимитно",
    "∞",
    "-1",
}


def _apply_forever_sub(db, target_id):
    """
    Выдаёт пользователю вечную подписку (Lifetime).
    Если колонка expires_at строковая (TEXT) — записываем 'forever'.
    Если в Postgres осталась старая схема с TIMESTAMP — фолбэчимся на 9999-12-31T23:59:59.
    """
    try:
        cur = execute(
            db,
            "UPDATE users SET expires_at = 'forever', plan = 'Lifetime', status = 'active' WHERE id = %s",
            (target_id,),
        )
        if getattr(cur, "rowcount", 1) == 0:
            return False, "Пользователь не найден (обновлено 0 строк)!"
        return True, "Выдана вечная подписка (Forever)!"
    except Exception as e:
        print(f"[apply_subscription_days forever fallback]: {e}")
        traceback.print_exc()
        _rollback_if_needed(db)
        try:
            far_future = "9999-12-31T23:59:59"
            cur = execute(
                db,
                "UPDATE users SET expires_at = %s, plan = 'Lifetime', status = 'active' WHERE id = %s",
                (far_future, target_id),
            )
            if getattr(cur, "rowcount", 1) == 0:
                return False, "Пользователь не найден (обновлено 0 строк)!"
            return True, "Выдана вечная подписка (Forever)!"
        except Exception as e2:
            print(f"[apply_subscription_days forever fallback error]: {e2}")
            traceback.print_exc()
            _rollback_if_needed(db)
            return False, f"Ошибка БД при выдаче подписки: {e2}"


def apply_subscription_days(db, target_id, raw_days):
    try:
        raw_days = str(raw_days or "").strip().lower()
        # Нормализуем: "30 дней" -> "30", "forever " -> "forever"
        if " " in raw_days:
            raw_days = raw_days.split()[0]
        raw_days = raw_days.strip()

        if not raw_days:
            return False, "Ошибка: некорректное значение срока подписки! Укажите число дней (например, 30) или forever."

        if raw_days in FOREVER_ALIASES:
            return _apply_forever_sub(db, target_id)

        # поддержка как "30", так и "30.0" или "30 дней" (уже обрезали)
        if raw_days.endswith(".0"):
            raw_days = raw_days[:-2]

        days = None
        if raw_days.isdigit():
            try:
                days = int(raw_days)
            except (ValueError, OverflowError):
                days = None
        else:
            try:
                f_val = float(raw_days)
                if f_val > 0:
                    days = int(f_val)
            except (ValueError, OverflowError):
                days = None

        if days is None or days <= 0:
            return False, "Ошибка: некорректное значение срока подписки! Укажите число дней (например, 30) или forever."

        # Если указано огромное число дней (например, 9999999999 или >= 36500 дн. (~100 лет)),
        # или число, превышающее возможности C int / timedelta / календарной даты —
        # считаем это запросом на вечную подписку (Lifetime), предотвращая ошибку
        # "Python int too large to convert to C int" или переполнение даты.
        if days >= 36500:
            return _apply_forever_sub(db, target_id)

        try:
            target_user = fetchone(db, "SELECT expires_at FROM users WHERE id = %s", (target_id,))
        except Exception as e:
            print(f"[apply_subscription_days fetch error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            return False, f"Ошибка БД при проверке пользователя: {e}"

        if not target_user:
            return False, "Пользователь не найден!"

        now = _utcnow()

        cur_exp = None
        try:
            cur_exp = target_user.get("expires_at") if isinstance(target_user, dict) else target_user["expires_at"]
        except Exception:
            cur_exp = None

        cur_exp_str = str(cur_exp or "").strip().lower()

        # Если подписки не было или она вечная (forever / 9999-12-31), отсчитываем от текущего момента
        if not cur_exp or cur_exp_str == "forever" or cur_exp_str.startswith("9999-12-31") or "9999-12-31" in cur_exp_str:
            base_time = now
        else:
            try:
                clean_exp = str(cur_exp).replace("Z", "").strip()
                parsed = datetime.fromisoformat(clean_exp)
                if parsed.year >= 9000:
                    base_time = now
                else:
                    base_time = parsed if parsed > now else now
            except Exception:
                try:
                    parsed = datetime.strptime(str(cur_exp)[:19], "%Y-%m-%d %H:%M:%S")
                    if parsed.year >= 9000:
                        base_time = now
                    else:
                        base_time = parsed if parsed > now else now
                except Exception:
                    base_time = now

        # Расчёт новой даты окончания с защитой от любых OverflowError (включая C int overflow)
        try:
            new_dt = base_time + timedelta(days=days)
            if new_dt.year >= 9999:
                return _apply_forever_sub(db, target_id)
            new_exp = new_dt.isoformat()
        except (OverflowError, ValueError) as oe:
            print(f"[apply_subscription_days timedelta overflow]: {oe}")
            return _apply_forever_sub(db, target_id)

        try:
            cur = execute(
                db,
                "UPDATE users SET expires_at = %s, plan = 'Active', status = 'active' WHERE id = %s",
                (new_exp, target_id),
            )
            # если 0 строк обновлено — пользователя нет
            if getattr(cur, "rowcount", 1) == 0:
                return False, "Пользователь не найден (обновлено 0 строк)!"
        except Exception as e:
            print(f"[apply_subscription_days update error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            return False, f"Ошибка БД при выдаче подписки: {e}"
        return True, f"Подписка успешно продлена на {days} дн.!"

    except Exception as e:
        print(f"[apply_subscription_days unexpected]: {e}")
        traceback.print_exc()
        _rollback_if_needed(db)
        return False, f"Внутренняя ошибка при выдаче подписки: {e}"


def generate_subscription_key():
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "DARK-" + "-".join(parts)


def generate_achievement_code(name):
    base = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    base = base or "achievement"
    suffix = secrets.token_hex(3)
    return f"{base}_{suffix}"


_fail_log = {}
_fail_lock = threading.Lock()


def _client_key(username):
    return f"{request.remote_addr}:{(username or '').lower()[:64]}"


def _login_blocked(key, limit=8, window=300):
    """True, если с этого IP+логина было слишком много неудачных попыток за окно."""
    now = time.monotonic()
    with _fail_lock:
        hits = [t for t in _fail_log.get(key, []) if now - t < window]
        _fail_log[key] = hits
        return len(hits) >= limit


def _login_failed(key, window=300):
    now = time.monotonic()
    with _fail_lock:
        _fail_log.setdefault(key, []).append(now)
        if len(_fail_log) > 5000:
            for k in list(_fail_log):
                if not _fail_log[k] or now - _fail_log[k][-1] > window:
                    _fail_log.pop(k, None)


def _session_token_ok(token, username, hwid):
    if not token or not username:
        return False
    try:
        payload = session_serializer.loads(token, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    return payload.get("username") == username and payload.get("hwid") == hwid


def validate_user_access(user, hwid_from_req):
    if user.get("status") == "banned":
        return False, "Ваш аккаунт заблокирован!"

    if user.get("status") == "frozen":
        return False, "Ваша подписка временно заморожена!"

    user_hwid = user.get("hwid")
    if user_hwid and user_hwid != "unknown" and user_hwid != hwid_from_req:
        return False, "Привязан другой компьютер (HWID mismatch)!"

    expires_at = user.get("expires_at")
    if not expires_at:
        return False, "У вас нет активной подписки!"

    cur_exp_str = str(expires_at).strip().lower()
    # forever и fallback-дата для Postgres TIMESTAMP
    if cur_exp_str == "forever" or cur_exp_str.startswith("9999-12-31") or "9999-12-31" in cur_exp_str:
        return True, None

    try:
        exp_str = str(expires_at).replace("Z", "").strip()
        exp_date = datetime.fromisoformat(exp_str)
        if exp_date.year >= 9000 or exp_date > _utcnow():
            return True, None
        return False, "Ваша подписка истекла!"
    except (ValueError, TypeError):
        try:
            exp_date = datetime.strptime(str(expires_at)[:19], "%Y-%m-%d %H:%M:%S")
            if exp_date.year >= 9000 or exp_date > _utcnow():
                return True, None
            return False, "Ваша подписка истекла!"
        except Exception:
            return False, "Ошибка формата подписки!"

    return True, None


@app.route("/api/login", methods=["POST"])
@app.route("/api/launcher/login", methods=["POST"])
def launcher_login():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    hwid = data.get("hwid", "unknown")

    if not username or not password:
        return jsonify({"success": False, "message": "Заполните логин и пароль!"}), 400

    rl_key = _client_key(username)
    if _login_blocked(rl_key):
        return jsonify({"success": False, "message": "Слишком много попыток. Подождите несколько минут."}), 429

    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))

    if not user or not check_password_hash(user["password_hash"], password):
        db.close()
        _login_failed(rl_key)
        return jsonify({"success": False, "message": "Неверный логин или пароль!"}), 401

    is_valid, err_msg = validate_user_access(user, hwid)
    if not is_valid:
        db.close()
        return jsonify({"success": False, "message": err_msg}), 403

    if not user.get("hwid") and hwid != "unknown":
        execute(db, "UPDATE users SET hwid = %s WHERE id = %s", (hwid, user["id"]))

    db.close()

    session_token = session_serializer.dumps({"username": username, "hwid": hwid})

    return jsonify({
        "success": True,
        "message": "Успешная авторизация!",
        "role": user.get("role", "User"),
        "sessionToken": session_token
    }), 200



@app.route("/api/mod-key", methods=["POST"])
def get_mod_key():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get("login") or data.get("username", "")
    hwid = data.get("hwid", "unknown")

    if not username:
        return jsonify({"error": "Логин не указан"}), 400

    if not (MOD_AES_KEY_BASE64 and MOD_AES_IV_BASE64):
        return jsonify({"error": "Сервер не настроен (нет ключей мода)"}), 503

    if REQUIRE_MOD_KEY_TOKEN and not _session_token_ok(data.get("sessionToken", ""), username, hwid):
        return jsonify({"error": "Требуется авторизация (sessionToken)"}), 401

    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
    db.close()

    if not user:
        return jsonify({"error": "Пользователь не найден"}), 404

    is_valid, err_msg = validate_user_access(user, hwid)
    if not is_valid:
        return jsonify({"error": err_msg}), 403

    return jsonify({
        "KeyBase64": MOD_AES_KEY_BASE64,
        "IvBase64": MOD_AES_IV_BASE64,
        "ModUrl": MOD_FILE_URL,
        "SessionTtlSeconds": SESSION_TTL_SECONDS
    }), 200


@app.route("/api/img/<path:filename>")
def achievement_image(filename):
    """
    Отдаём иконки достижений с нашего домена с «вечным» кэшем:
    имена файлов уникальны (uuid), поэтому immutable безопасен.
    Это убирает медленные 304-запросы и внешние обращения к raw.githubusercontent.com.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    directory = os.path.join(base, "api", "img")
    if not os.path.isfile(os.path.join(directory, filename)):
        fallback = os.path.join(base, "static", "img")
        if os.path.isfile(os.path.join(fallback, filename)):
            directory = fallback
        elif _IMG_NAME_RE.match(filename):
            # Загруженные через админку иконки лежат в БД
            try:
                row = fetchone(get_db(), "SELECT mime, data FROM uploaded_images WHERE name = %s", (filename,))
            except Exception as e:
                print(f"[achievement_image db error]: {e}")
                _rollback_current()
                row = None
            if row:
                resp = Response(bytes(row["data"]), mimetype=row["mime"])
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return resp
            return "Not found", 404
    resp = send_from_directory(directory, filename, max_age=31536000)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/darkvisuals.enc")
def download_mod_file():
    """
    Запасной маршрут на случай, если rewrite из vercel.json не сработал:
    отдаём darkvisuals.enc из static/ (файл лежит в репозитории).
    """
    # 24 МБ превышает лимит ответа serverless-функции Vercel (4.5 МБ) — отдаём как статику.
    return redirect("/static/darkvisuals.enc", code=302)


@app.route("/healthz")
def healthz():
    # Лёгкий эндпоинт без обращения к БД — для UptimeRobot / проверок.
    return "ok", 200, {"Cache-Control": "no-store"}


@app.route("/api/verify", methods=["POST"])
def verify_mod_session():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get("login", "")
    hwid = data.get("hwid", "unknown")
    token = data.get("sessionToken", "")

    if not username or not token:
        return jsonify({"valid": False}), 200

    try:
        payload = session_serializer.loads(token, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return jsonify({"valid": False}), 200

    if payload.get("username") != username or payload.get("hwid") != hwid:
        return jsonify({"valid": False}), 200

    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
    db.close()

    if not user:
        return jsonify({"valid": False}), 200

    is_valid, _ = validate_user_access(user, hwid)
    return jsonify({"valid": is_valid}), 200


@app.route("/")
def index():
    user = current_user()
    index_path = os.path.join(app.template_folder, "index.html")
    if os.path.exists(index_path):
        return render_template("index.html", user=user, plans=PLANS, launcher_url=LAUNCHER_URL)
    return render_template("login.html", user=user)


@app.route("/buy/<plan_key>")
def buy(plan_key):
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    plan = PLANS.get(plan_key)
    if not plan:
        flash("Выбран несуществующий тариф!", "error")
        return redirect(url_for("index"))

    funpay_url = FUNPAY_LINKS.get(plan_key)
    # Можно прийти сразу со ссылкой вида /buy/1_month?promo=SALE10 — промокод подставится сам.
    promo_prefill = normalize_promo_code(request.args.get("promo", ""))

    return render_template(
        "buy.html",
        user=user,
        plan=plan,
        plan_key=plan_key,
        telegram_url=TELEGRAM_ADMIN_URL,
        funpay_url=funpay_url,
        promo_prefill=promo_prefill,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        rl_key = _client_key(username)
        if _login_blocked(rl_key):
            flash("Слишком много попыток входа. Подождите несколько минут.", "error")
            return render_template("login.html")

        try:
            db = get_db()
            user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
            db.close()
        except Exception as e:
            print(f"[db error]: {e}"); flash("Ошибка базы данных. Попробуйте позже.", "error")
            return render_template("login.html")

        if user and check_password_hash(user["password_hash"], password):
            session.permanent = True
            session["user_id"] = user["id"]
            return redirect(url_for("profile"))

        _login_failed(rl_key)
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Заполните все поля!", "error")
            return render_template("login.html")

        try:
            db = get_db()
            exists = fetchone(db, "SELECT id FROM users WHERE username = %s", (username,))
            if exists:
                db.close()
                flash("Пользователь с таким логином уже существует!", "error")
                return render_template("login.html")

            now = _utcnow().isoformat()
            execute(
                db,
                """
                INSERT INTO users (username, password_hash, role, status, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (username, generate_password_hash(password), "User", "active", now),
            )

            user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
            db.close()
        except Exception as e:
            print(f"[db error]: {e}"); flash("Ошибка базы данных. Попробуйте позже.", "error")
            return render_template("login.html")

        if user:
            session.permanent = True
            session["user_id"] = user["id"]
            return redirect(url_for("profile"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/login/google")
def google_login():
    if not google_oauth:
        flash("Вход через Google временно недоступен (не настроен на сервере).", "error")
        return redirect(url_for("login"))
    redirect_uri = url_for("google_callback", _external=True)
    return google_oauth.authorize_redirect(redirect_uri)


@app.route("/login/google/callback")
def google_callback():
    if not google_oauth:
        flash("Вход через Google временно недоступен (не настроен на сервере).", "error")
        return redirect(url_for("login"))

    try:
        token = google_oauth.authorize_access_token()
        userinfo = token.get("userinfo") or google_oauth.userinfo()
    except Exception as e:
        flash(f"Не удалось войти через Google: {e}", "error")
        return redirect(url_for("login"))

    google_id = userinfo.get("sub")
    email = userinfo.get("email")
    if not google_id or not email:
        flash("Google не вернул данные аккаунта. Попробуйте снова.", "error")
        return redirect(url_for("login"))

    try:
        db = get_db()

        user = fetchone(db, "SELECT * FROM users WHERE google_id = %s", (google_id,))

        if not user:
            # Аккаунт с таким email уже мог быть создан через обычную регистрацию — привязываем Google к нему
            user = fetchone(db, "SELECT * FROM users WHERE email = %s", (email,))
            if user:
                execute(db, "UPDATE users SET google_id = %s WHERE id = %s", (google_id, user["id"]))
            else:
                base_username = (email.split("@")[0] or "user").strip()
                base_username = "".join(ch for ch in base_username if ch.isalnum() or ch in "_.-") or "user"
                username = base_username
                suffix = 0
                while fetchone(db, "SELECT id FROM users WHERE username = %s", (username,)):
                    suffix += 1
                    username = f"{base_username}{suffix}"

                now = _utcnow().isoformat()
                random_password = secrets.token_hex(32)
                execute(
                    db,
                    """
                    INSERT INTO users
                        (username, password_hash, role, status, created_at, google_id, email, avatar_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        username,
                        generate_password_hash(random_password),
                        "User",
                        "active",
                        now,
                        google_id,
                        email,
                        userinfo.get("picture"),
                    ),
                )
                user = fetchone(db, "SELECT * FROM users WHERE google_id = %s", (google_id,))

        db.close()
    except Exception as e:
        print(f"[db error]: {e}"); flash("Ошибка базы данных. Попробуйте позже.", "error")
        return redirect(url_for("login"))

    if not user:
        flash("Не удалось войти через Google.", "error")
        return redirect(url_for("login"))

    if user.get("status") == "banned":
        flash("Ваш аккаунт заблокирован!", "error")
        return redirect(url_for("login"))

    session.permanent = True
    session["user_id"] = user["id"]
    return redirect(url_for("profile"))


@app.route("/profile")
def profile():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    sub_info = {
        "active": False,
        "text": "Нет активной подписки",
        "days_left": 0
    }

    expires_at = user.get("expires_at")
    cur_exp_str = str(expires_at or "").strip().lower()
    # поддержка как 'forever', так и fallback-даты 9999-12-31 для Postgres TIMESTAMP
    if cur_exp_str == "forever" or cur_exp_str.startswith("9999-12-31") or "9999-12-31" in cur_exp_str:
        sub_info = {"active": True, "text": "Навсегда (Forever)", "days_left": "∞"}
    elif expires_at:
        try:
            # отрезаем Z и миллисекунды если есть
            exp_str = str(expires_at).replace("Z", "").strip()
            exp_date = datetime.fromisoformat(exp_str)
            now = _utcnow()
            if exp_date > now:
                # далёкое будущее считаем forever
                if exp_date.year >= 9000:
                    sub_info = {"active": True, "text": "Навсегда (Forever)", "days_left": "∞"}
                else:
                    diff = exp_date - now
                    sub_info = {
                        "active": True,
                        "text": exp_date.strftime("%d.%m.%Y %H:%M"),
                        "days_left": diff.days + 1
                    }
            else:
                sub_info = {"active": False, "text": "Истекла", "days_left": 0}
        except (ValueError, TypeError):
            # попробуем старый формат через strptime
            try:
                exp_date = datetime.strptime(str(expires_at)[:19], "%Y-%m-%d %H:%M:%S")
                now = _utcnow()
                if exp_date > now:
                    if exp_date.year >= 9000:
                        sub_info = {"active": True, "text": "Навсегда (Forever)", "days_left": "∞"}
                    else:
                        diff = exp_date - now
                        sub_info = {
                            "active": True,
                            "text": exp_date.strftime("%d.%m.%Y %H:%M"),
                            "days_left": diff.days + 1
                        }
                else:
                    sub_info = {"active": False, "text": "Истекла", "days_left": 0}
            except Exception:
                sub_info = {"active": False, "text": "Ошибка даты", "days_left": 0}

    db = get_db()
    ensure_user_config_downloads_table(db)
    all_configs = fetchall(db, "SELECT * FROM configs ORDER BY id DESC") or []

    granted_ids = set()
    if not user.get("is_admin"):
        grant_rows = fetchall(
            db,
            "SELECT config_id FROM user_config_downloads WHERE user_id = %s",
            (user["id"],),
        ) or []
        granted_ids = {row["config_id"] for row in grant_rows}

    db.close()

    # Конфиги видны всем; кнопка «Скачать» — только после выдачи доступа админом
    for c in all_configs:
        c["images"] = config_images_list(c)
        has_file = bool(c.get("download_url"))
        c["can_download"] = has_file and (bool(user.get("is_admin")) or c["id"] in granted_ids)
        # Не светим прямую ссылку в HTML тем, у кого нет доступа
        if not c["can_download"]:
            c["download_url"] = None

    return render_template(
        "profile.html",
        user=user,
        sub=sub_info,
        launcher_url=LAUNCHER_URL,
        configs=all_configs,
    )


@app.route("/profile/reset_hwid", methods=["POST"])
def reset_own_hwid():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    db = get_db()
    execute(db, "UPDATE users SET hwid = NULL WHERE id = %s", (user["id"],))
    db.close()

    flash("HWID сброшен. При следующем запуске лаунчера устройство будет привязано заново.", "success")
    return redirect(url_for("profile"))


@app.route("/profile/change_password", methods=["POST"])
def change_own_password():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    old_password = request.form.get("old_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not check_password_hash(user["password_hash"], old_password):
        flash("Текущий пароль указан неверно!", "error")
        return redirect(url_for("profile"))

    if not new_password or new_password != confirm_password:
        flash("Новые пароли не совпадают или пусты!", "error")
        return redirect(url_for("profile"))

    db = get_db()
    execute(
        db,
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (generate_password_hash(new_password), user["id"]),
    )
    db.close()

    flash("Пароль успешно изменён!", "success")
    return redirect(url_for("profile"))


@app.route("/profile/redeem_key", methods=["POST"])
def redeem_key():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    key_code = request.form.get("key_code", "").strip().upper()
    # убираем пробелы/дефисы которые пользователь мог скопировать случайно, оставляем только A-Z0-9 и -
    # но сам поиск идёт по точному коду; нормализуем лишь регистр и пробелы
    key_code = "".join(ch for ch in key_code if ch.isalnum() or ch == "-")
    if not key_code:
        flash("Введите ключ активации!", "error")
        return redirect(url_for("profile"))

    try:
        db = get_db()
        ensure_subscription_keys_table(db)
        try:
            key_row = fetchone(db, "SELECT * FROM subscription_keys WHERE key_code = %s", (key_code,))
        except Exception as e:
            print(f"[redeem_key fetch error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            db.close()
            flash(f"Ошибка БД при проверке ключа: {e}", "error")
            return redirect(url_for("profile"))

        if not key_row:
            db.close()
            flash("Ключ не найден! Проверьте правильность ввода.", "error")
            return redirect(url_for("profile"))

        # Атомарно «занимаем» ключ: при двух одновременных запросах выиграет только один.
        claim = execute(
            db,
            "UPDATE subscription_keys SET is_used = %s, used_by = %s, used_at = %s "
            "WHERE id = %s AND (is_used IS NULL OR is_used = %s)",
            (True, user["username"], _utcnow().isoformat(), key_row["id"], False),
        )
        if getattr(claim, "rowcount", 0) != 1:
            db.close()
            flash("Этот ключ уже был активирован ранее!", "error")
            return redirect(url_for("profile"))

        ok, msg = apply_subscription_days(db, user["id"], key_row.get("days"))
        if ok:
            flash(f"Ключ активирован! {msg}", "success")
        else:
            # Не удалось выдать подписку — освобождаем ключ обратно.
            execute(
                db,
                "UPDATE subscription_keys SET is_used = %s, used_by = NULL, used_at = NULL WHERE id = %s",
                (False, key_row["id"]),
            )
            flash(msg, "error")

        db.close()
        return redirect(url_for("profile"))
    except Exception as e:
        print(f"[redeem_key unexpected]: {e}")
        traceback.print_exc()
        try:
            _rollback_if_needed(get_db())
        except Exception:
            pass
        flash(f"Внутренняя ошибка при активации ключа: {e}", "error")
        return redirect(url_for("profile"))


@app.route("/admin")
def admin_panel():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    try:
        db = get_db()
        ensure_user_config_downloads_table(db)
        ensure_subscription_keys_table(db)
        ensure_promo_codes_table(db)
        # на случай старой БД без таблиц конфигов/ачивок — не падаем 500, покажем пусто
        # Список пользователей не грузим: админ вводит ID и видит одного пользователя.
        lookup_uid = (request.args.get("uid") or "").strip()
        lookup_user = None
        if lookup_uid.isdigit() and len(lookup_uid) < 10:
            try:
                lookup_user = fetchone(db, "SELECT * FROM users WHERE id = %s", (int(lookup_uid),))
            except Exception as e:
                print(f"[admin_panel lookup error]: {e}")
                _rollback_if_needed(db)
            if lookup_user:
                lookup_user.pop("password_hash", None)
                lookup_user.pop("password_plain", None)
        try:
            all_keys = fetchall(db, "SELECT * FROM subscription_keys ORDER BY id DESC")
        except Exception as e:
            print(f"[admin_panel keys fetch error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            all_keys = []
        try:
            all_achievements = fetchall(db, "SELECT * FROM achievements ORDER BY id DESC")
        except Exception as e:
            print(f"[admin_panel achievements fetch error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            all_achievements = []
        for a in all_achievements:
            a["image_url"] = normalize_achievement_image_url(a.get("image_url"))
        try:
            all_configs = fetchall(db, "SELECT * FROM configs ORDER BY id DESC")
        except Exception as e:
            print(f"[admin_panel configs fetch error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            all_configs = []
        for c in all_configs:
            c["images"] = config_images_list(c)
        try:
            all_promos = fetchall(db, "SELECT * FROM promo_codes ORDER BY id DESC")
        except Exception as e:
            print(f"[admin_panel promos fetch error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            all_promos = []
        try:
            all_grants = fetchall(
                db,
                """
                SELECT ua.id AS user_achievement_id, ua.granted_at,
                       u.username, u.id AS user_id,
                       a.name AS achievement_name, a.code AS achievement_code, a.id AS achievement_id
                FROM user_achievements ua
                JOIN users u ON u.id = ua.user_id
                JOIN achievements a ON a.id = ua.achievement_id
                ORDER BY ua.granted_at DESC
                """,
            )
        except Exception as e:
            print(f"[admin_panel grants fetch error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            all_grants = []
        try:
            all_config_grants = fetchall(
                db,
                """
                SELECT ucd.id AS grant_id, ucd.granted_at,
                       u.username, u.id AS user_id,
                       c.name AS config_name, c.id AS config_id, c.price AS config_price
                FROM user_config_downloads ucd
                JOIN users u ON u.id = ucd.user_id
                JOIN configs c ON c.id = ucd.config_id
                ORDER BY ucd.granted_at DESC
                """,
            )
        except Exception as e:
            print(f"[admin_panel config_grants fetch error]: {e}")
            traceback.print_exc()
            _rollback_if_needed(db)
            all_config_grants = []
        try:
            db.close()
        except Exception:
            pass
        return render_template(
            "admin.html",
            lookup_uid=lookup_uid,
            lookup_user=lookup_user,
            keys=all_keys,
            current_user=user,
            achievements=all_achievements,
            grants=all_grants,
            configs=all_configs,
            config_grants=all_config_grants,
            promos=all_promos,
        )
    except Exception as e:
        print(f"[admin_panel unexpected]: {e}")
        traceback.print_exc()
        try:
            _rollback_if_needed(get_db())
        except Exception:
            pass
        flash(f"Ошибка загрузки админки: {e}", "error")
        return redirect(url_for("profile"))


@app.route("/admin/create_user", methods=["POST"])
def admin_create_user():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    raw_days = request.form.get("days", "").strip()

    if not username or not password:
        flash("Логин и пароль обязательны!", "error")
        return redirect(url_for("admin_panel"))

    try:
        db = get_db()
        existing = fetchone(db, "SELECT id FROM users WHERE username = %s", (username,))
        if existing:
            db.close()
            flash("Пользователь с таким логином уже существует!", "error")
            return redirect(url_for("admin_panel"))

        now = _utcnow().isoformat()
        execute(
            db,
            """
            INSERT INTO users (username, password_hash, role, status, created_at, is_admin, plan)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (username, generate_password_hash(password), "User", "active", now, False, None),
        )

        new_user = fetchone(db, "SELECT id FROM users WHERE username = %s", (username,))

        if raw_days and new_user:
            ok, msg = apply_subscription_days(db, new_user["id"], raw_days)
            db.close()
            if ok:
                flash(f"Пользователь «{username}» создан! {msg}", "success")
            else:
                flash(f"Пользователь «{username}» создан, но подписку выдать не удалось: {msg}", "warning")
        else:
            db.close()
            flash(f"Пользователь «{username}» создан!", "success")

        return redirect(url_for("admin_panel"))
    except Exception as e:
        print(f"[admin_create_user error]: {e}")
        traceback.print_exc()
        try:
            _rollback_if_needed(db)
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
        flash(f"Внутренняя ошибка при создании пользователя: {e}", "error")
        return redirect(url_for("admin_panel"))


@app.route("/admin/achievements/create", methods=["POST"])
def admin_create_achievement():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    image_url = request.form.get("image_url", "").strip()
    unlock_feature = request.form.get("unlock_feature", "").strip()
    icon_file = request.files.get("image_file")

    if not name:
        flash("Название достижения не может быть пустым!", "error")
        return redirect(url_for("admin_panel"))

    # Если загружен файл — он приоритетнее ссылки, грузим его на GitHub
    if icon_file and icon_file.filename:
        uploaded_url, upload_error = upload_image_to_github(icon_file)
        if upload_error:
            flash(upload_error, "error")
            return redirect(url_for("admin_panel"))
        if uploaded_url:
            image_url = uploaded_url

    db = get_db()
    code = generate_achievement_code(name)
    while fetchone(db, "SELECT id FROM achievements WHERE code = %s", (code,)):
        code = generate_achievement_code(name)

    execute(
        db,
        """
        INSERT INTO achievements (code, name, description, image_url, unlock_feature, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (code, name, description, image_url, unlock_feature, _utcnow().isoformat()),
    )
    db.close()

    flash(f"Достижение «{name}» создано (код: {code})!", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/bulk_action", methods=["POST"])
def admin_bulk_action():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    action = request.form.get("action")
    raw_ids = request.form.getlist("user_ids")

    ids = []
    for raw_id in raw_ids:
        try:
            ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue

    if not ids:
        flash("Ошибка: Не выбран ни один пользователь!", "error")
        return redirect(url_for("admin_panel"))

    if action in ("ban", "delete"):
        ids = [i for i in ids if i != user.get("id")]  # себя не трогаем
        if not ids:
            flash("Нельзя применить это действие к самому себе!", "warning")
            return redirect(url_for("admin_panel"))

    db = get_db()
    ph = ",".join(["%s"] * len(ids))

    if action == "unban":
        execute(db, f"UPDATE users SET status = 'active' WHERE id IN ({ph})", tuple(ids))
        flash(f"Разблокировано пользователей: {len(ids)}!", "success")

    elif action == "ban":
        execute(db, f"UPDATE users SET status = 'banned' WHERE id IN ({ph})", tuple(ids))
        flash(f"Заблокировано пользователей: {len(ids)}!", "success")

    elif action == "freeze":
        execute(db, f"UPDATE users SET status = 'frozen' WHERE id IN ({ph})", tuple(ids))
        flash(f"Заморожено пользователей: {len(ids)}!", "warning")

    elif action == "delete":
        ensure_user_config_downloads_table(db)
        execute(db, f"DELETE FROM user_config_downloads WHERE user_id IN ({ph})", tuple(ids))
        execute(db, f"DELETE FROM user_achievements WHERE user_id IN ({ph})", tuple(ids))
        execute(db, f"DELETE FROM users WHERE id IN ({ph})", tuple(ids))
        flash(f"Удалено пользователей: {len(ids)}!", "success")

    else:
        flash("Ошибка: Неизвестное массовое действие!", "error")

    db.close()
    return redirect(url_for("admin_panel"))


@app.route("/admin/action", methods=["POST"])
def admin_action():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    target_id = request.form.get("user_id") or request.form.get("id")
    action = request.form.get("action")

    if not target_id or not action:
        flash("Ошибка: Не указан ID пользователя или действие!", "error")
        return redirect(url_for("admin_panel"))

    db = None
    try:
        db = get_db()
    except Exception as e:
        print(f"[admin_action get_db error]: {e}")
        traceback.print_exc()
        flash(f"Ошибка БД: {e}", "error")
        return redirect(url_for("admin_panel"))

    try:
        target_id = int(target_id)
    except ValueError:
        flash("Ошибка: Некорректный ID пользователя!", "error")
        try:
            db.close()
        except Exception:
            pass
        return redirect(url_for("admin_panel"))

    # Не даём администратору банить/замораживать самого себя,
    # чтобы он не заблокировал свой же доступ к панели.
    if target_id == user.get("id") and action in ("ban", "freeze"):
        try:
            db.close()
        except Exception:
            pass
        flash("Нельзя забанить или заморозить самого себя!", "warning")
        return redirect(url_for("admin_panel"))

    try:
        if action == "ban":
            execute(db, "UPDATE users SET status = 'banned' WHERE id = %s", (target_id,))
            flash("Пользователь заблокирован!", "success")

        elif action == "unban":
            execute(db, "UPDATE users SET status = 'active' WHERE id = %s", (target_id,))
            flash("Пользователь разблокирован!", "success")

        elif action == "unfreeze":
            execute(db, "UPDATE users SET status = 'active' WHERE id = %s", (target_id,))
            flash("Подписка разморожена!", "success")

        elif action == "add_days":
            raw_days = str(request.form.get("days") or request.form.get("sub_days") or "").strip().lower()
            if not raw_days:
                flash("Ошибка: Укажите число дней (например, 30 или 120)!", "error")
            else:
                ok, msg = apply_subscription_days(db, target_id, raw_days)
                flash(msg, "success" if ok else "error")

        elif action == "freeze":
            execute(db, "UPDATE users SET status = 'frozen' WHERE id = %s", (target_id,))
            flash("Подписка заморожена!", "warning")

        elif action == "reset_hwid":
            execute(db, "UPDATE users SET hwid = NULL WHERE id = %s", (target_id,))
            flash("HWID пользователя успешно сброшен!", "success")

        elif action == "change_password":
            new_password = request.form.get("new_password", "")
            if not new_password:
                flash("Ошибка: Новый пароль не может быть пустым!", "error")
            else:
                execute(
                    db,
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (generate_password_hash(new_password), target_id),
                )
                flash("Пароль пользователя успешно изменён!", "success")

        elif action == "delete":
            target = fetchone(db, "SELECT username FROM users WHERE id = %s", (target_id,))
            if not target:
                flash("Ошибка: Пользователь не найден!", "error")
            else:
                ensure_user_config_downloads_table(db)
                execute(db, "DELETE FROM user_config_downloads WHERE user_id = %s", (target_id,))
                execute(db, "DELETE FROM user_achievements WHERE user_id = %s", (target_id,))
                execute(db, "DELETE FROM users WHERE id = %s", (target_id,))
                flash(f"Пользователь {target['username']} удалён навсегда!", "success")
                if user.get("id") == target_id:
                    session.clear()
                    try:
                        db.close()
                    except Exception:
                        pass
                    return redirect(url_for("login"))

        elif action == "make_admin":
            execute(db, "UPDATE users SET is_admin = TRUE WHERE id = %s", (target_id,))
            flash("Пользователю выданы права администратора!", "success")

        elif action == "remove_admin":
            execute(db, "UPDATE users SET is_admin = FALSE WHERE id = %s", (target_id,))
            flash("Права администратора отозваны!", "success")

        else:
            flash("Неизвестное действие!", "error")

    except Exception as e:
        print(f"[admin_action error action={action}]: {e}")
        traceback.print_exc()
        _rollback_if_needed(db)
        flash(f"Внутренняя ошибка при выполнении «{action}»: {e}", "error")

    try:
        db.close()
    except Exception:
        pass
    # Возвращаемся к тому же пользователю (после удаления — к пустой форме)
    return redirect(url_for("admin_panel", uid=None if action == "delete" else target_id))


@app.route("/admin/generate_key", methods=["POST"])
def admin_generate_key():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    raw_days = str(request.form.get("days", "")).strip().lower()
    # нормализуем "30 дней" -> "30"
    if " " in raw_days:
        raw_days = raw_days.split()[0].strip()
    if not raw_days:
        flash("Укажите срок подписки для ключа (например, 30 или forever)!", "error")
        return redirect(url_for("admin_panel"))

    is_forever = raw_days in FOREVER_ALIASES
    num_days = None
    if not is_forever:
        if raw_days.endswith(".0"):
            raw_days = raw_days[:-2]
        if raw_days.isdigit():
            try:
                num_days = int(raw_days)
            except (ValueError, OverflowError):
                num_days = None
        else:
            try:
                f_val = float(raw_days)
                if f_val > 0:
                    num_days = int(f_val)
            except (ValueError, OverflowError):
                num_days = None

    if not is_forever and (num_days is None or num_days <= 0):
        flash("Некорректный срок подписки для ключа! Укажите число дней или forever.", "error")
        return redirect(url_for("admin_panel"))

    if is_forever or (num_days is not None and num_days >= 36500):
        raw_days = "forever"
        plan_name = "Lifetime"
    else:
        raw_days = str(num_days)
        plan_name = f"{num_days} дней"

    try:
        db = get_db()
        ensure_subscription_keys_table(db)
        key_code = generate_subscription_key()
        # защита от бесконечного цикла при редком коллизионном ключе
        for _ in range(10):
            try:
                if not fetchone(db, "SELECT id FROM subscription_keys WHERE key_code = %s", (key_code,)):
                    break
            except Exception as e:
                print(f"[generate_key fetch check error]: {e}")
                traceback.print_exc()
                _rollback_if_needed(db)
                break
            key_code = generate_subscription_key()

        execute(
            db,
            "INSERT INTO subscription_keys (key_code, days, plan_name, is_used, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (key_code, raw_days, plan_name, False, _utcnow().isoformat()),
        )
        db.close()

        flash(f"Ключ создан: {key_code} ({plan_name})", "success")
        return redirect(url_for("admin_panel"))
    except Exception as e:
        print(f"[admin_generate_key error]: {e}")
        traceback.print_exc()
        try:
            _rollback_if_needed(db)
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
        flash(f"Внутренняя ошибка при создании ключа: {e}", "error")
        return redirect(url_for("admin_panel"))


@app.route("/admin/delete_key", methods=["POST"])
def admin_delete_key():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    key_id = request.form.get("key_id")
    if key_id:
        db = get_db()
        execute(db, "DELETE FROM subscription_keys WHERE id = %s", (key_id,))
        db.close()
        flash("Ключ удалён!", "success")

    return redirect(url_for("admin_panel"))


# ==================================================================
#  ПРОМОКОДЫ (Promo codes) — скидка при покупке
# ==================================================================

@app.route("/admin/promo/create", methods=["POST"])
def admin_create_promo():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    funpay_url = request.form.get("funpay_url", "").strip()
    code = normalize_promo_code(request.form.get("code", ""))
    raw_percent = request.form.get("discount_percent", "").strip()

    if not code:
        flash("Укажите название промокода!", "error")
        return redirect(url_for("admin_panel"))

    if not funpay_url.startswith(("http://", "https://")):
        flash("Укажите ссылку на FunPay (https://...) для покупки с этим промокодом!", "error")
        return redirect(url_for("admin_panel"))

    try:
        percent = int(raw_percent)
    except (TypeError, ValueError):
        percent = 0
    if not (1 <= percent <= 100):
        flash("Скидка промокода — число от 1 до 100 процентов!", "error")
        return redirect(url_for("admin_panel"))

    db = get_db()
    ensure_promo_codes_table(db)
    if fetchone(db, "SELECT id FROM promo_codes WHERE code = %s", (code,)):
        db.close()
        flash(f"Промокод «{code}» уже существует!", "error")
        return redirect(url_for("admin_panel"))

    execute(
        db,
        """
        INSERT INTO promo_codes (code, discount_percent, funpay_url, is_active, uses_count, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (code, percent, funpay_url, True, 0, _utcnow().isoformat()),
    )
    db.close()

    flash(f"Промокод «{code}» создан: скидка −{percent}%!", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/promo/toggle", methods=["POST"])
def admin_toggle_promo():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    promo_id = request.form.get("promo_id")
    if promo_id:
        db = get_db()
        ensure_promo_codes_table(db)
        execute(db, "UPDATE promo_codes SET is_active = NOT is_active WHERE id = %s", (promo_id,))
        db.close()
        flash("Статус промокода изменён!", "success")

    return redirect(url_for("admin_panel"))


@app.route("/admin/promo/delete", methods=["POST"])
def admin_delete_promo():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    promo_id = request.form.get("promo_id")
    if promo_id:
        db = get_db()
        ensure_promo_codes_table(db)
        execute(db, "DELETE FROM promo_codes WHERE id = %s", (promo_id,))
        db.close()
        flash("Промокод удалён!", "success")

    return redirect(url_for("admin_panel"))


@app.route("/api/promo/apply", methods=["POST"])
def api_apply_promo():
    """
    Активация промокода при покупке: проверяет код, считает новую цену
    со скидкой и возвращает ссылку на FunPay, привязанную к промокоду.
    """
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Сначала войдите в аккаунт, чтобы применить промокод."}), 401

    data = request.get_json(silent=True) or {}
    code = normalize_promo_code(data.get("code") or request.form.get("code", ""))
    plan_key = str(data.get("plan_key") or request.form.get("plan_key") or "").strip()

    if not code:
        return jsonify({"ok": False, "error": "Введите промокод."}), 400

    db = get_db()
    ensure_promo_codes_table(db)
    promo = fetchone(db, "SELECT * FROM promo_codes WHERE code = %s", (code,))
    if not promo:
        db.close()
        return jsonify({"ok": False, "error": "Такого промокода не существует."}), 404

    if not promo.get("is_active"):
        db.close()
        return jsonify({"ok": False, "error": "Промокод деактивирован."}), 400

    try:
        percent = int(promo.get("discount_percent") or 0)
    except (TypeError, ValueError):
        percent = 0

    result = {
        "ok": True,
        "code": promo["code"],
        "discount_percent": percent,
    }

    plan = PLANS.get(plan_key)
    if plan:
        result["plan_key"] = plan_key
        result["original_price"] = plan["price"]
        result["new_price"] = discounted_price(plan["price"], percent)

    # Ссылка на FunPay именно этого промокода; если её нет — обычная ссылка тарифа.
    funpay_url = (promo.get("funpay_url") or "").strip()
    if not funpay_url:
        funpay_url = FUNPAY_LINKS.get(plan_key) or ""
    result["funpay_url"] = funpay_url or None

    # Промокод активирован при покупке — считаем применение.
    execute(db, "UPDATE promo_codes SET uses_count = uses_count + 1 WHERE id = %s", (promo["id"],))
    db.close()

    return jsonify(result)


# ==================================================================
#  КОНФИГИ (Configs) — библиотека в профиле
# ==================================================================

@app.route("/admin/configs/create", methods=["POST"])
def admin_create_config():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    name = request.form.get("name", "").strip()
    price = request.form.get("price", "").strip()
    description = request.form.get("description", "").strip()
    funpay_url = request.form.get("funpay_url", "").strip()
    download_url = request.form.get("download_url", "").strip()

    if not name:
        flash("Название конфига не может быть пустым!", "error")
        return redirect(url_for("admin_panel"))
    if not price:
        flash("Укажите стоимость конфига!", "error")
        return redirect(url_for("admin_panel"))
    if not funpay_url or not funpay_url.startswith(("http://", "https://")):
        flash("Укажите корректную ссылку на оплату через FunPay (https://...)", "error")
        return redirect(url_for("admin_panel"))
    if not download_url or not download_url.startswith(("http://", "https://")):
        flash("Укажите корректную ссылку на скачивание конфига (https://...)", "error")
        return redirect(url_for("admin_panel"))
    if not description:
        flash("Добавьте описание конфига!", "error")
        return redirect(url_for("admin_panel"))

    # 3 фото конфига: загруженный файл приоритетнее ссылки (как у иконок достижений)
    images = []
    for i in (1, 2, 3):
        img_file = request.files.get(f"image{i}_file")
        img_url = request.form.get(f"image{i}_url", "").strip()
        url = None
        if img_file and img_file.filename:
            uploaded_url, upload_error = upload_image_to_github(img_file)
            if upload_error:
                flash(upload_error, "error")
                return redirect(url_for("admin_panel"))
            url = uploaded_url
        elif img_url:
            if not img_url.startswith(("http://", "https://", "/")):
                flash(f"Ссылка на фото {i} должна начинаться с http:// или https://", "error")
                return redirect(url_for("admin_panel"))
            url = img_url
        images.append(url)

    if not any(images):
        flash("Добавьте хотя бы одну фотографию конфига!", "error")
        return redirect(url_for("admin_panel"))

    db = get_db()
    execute(
        db,
        """
        INSERT INTO configs
            (name, price, description, funpay_url, download_url, image1_url, image2_url, image3_url, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            name,
            price,
            description,
            funpay_url,
            download_url,
            images[0],
            images[1],
            images[2],
            _utcnow().isoformat(),
        ),
    )
    db.close()

    flash(f"Конфиг «{name}» добавлен! Он уже виден всем игрокам в профиле → Библиотека → Конфиги.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/configs/delete", methods=["POST"])
def admin_delete_config():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    config_id = request.form.get("config_id")
    if config_id:
        db = get_db()
        config = fetchone(db, "SELECT * FROM configs WHERE id = %s", (config_id,))
        ensure_user_config_downloads_table(db)
        execute(db, "DELETE FROM user_config_downloads WHERE config_id = %s", (config_id,))
        execute(db, "DELETE FROM configs WHERE id = %s", (config_id,))
        db.close()

        # Подчищаем фото конфига из репозитория (тихо, ошибки не критичны)
        if config:
            for i in (1, 2, 3):
                delete_image_from_github(config.get(f"image{i}_url"))

        flash("Конфиг удалён!", "success")

    return redirect(url_for("admin_panel"))


@app.route("/admin/configs/grant", methods=["POST"])
def admin_grant_config():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    target_id = (request.form.get("user_id") or "").strip()
    target_id = target_id if target_id.isdigit() and len(target_id) < 10 else None
    config_id = request.form.get("config_id")

    if not target_id or not config_id:
        flash("Выберите игрока и конфиг!", "error")
        return redirect(url_for("admin_panel"))

    db = get_db()
    ensure_user_config_downloads_table(db)
    target_user = fetchone(db, "SELECT username FROM users WHERE id = %s", (target_id,))
    config = fetchone(db, "SELECT name FROM configs WHERE id = %s", (config_id,))

    if not target_user or not config:
        db.close()
        flash("Пользователь или конфиг не найдены!", "error")
        return redirect(url_for("admin_panel"))

    existing = fetchone(
        db,
        "SELECT id FROM user_config_downloads WHERE user_id = %s AND config_id = %s",
        (target_id, config_id),
    )
    if existing:
        db.close()
        flash(f"У {target_user['username']} уже есть доступ к скачиванию «{config['name']}»!", "warning")
        return redirect(url_for("admin_panel"))

    execute(
        db,
        "INSERT INTO user_config_downloads (user_id, config_id, granted_at) VALUES (%s, %s, %s)",
        (target_id, config_id, _utcnow().isoformat()),
    )
    db.close()

    flash(f"Доступ к скачиванию «{config['name']}» выдан игроку {target_user['username']}!", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/configs/revoke", methods=["POST"])
def admin_revoke_config():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    grant_id = request.form.get("grant_id")
    if grant_id:
        db = get_db()
        ensure_user_config_downloads_table(db)
        execute(db, "DELETE FROM user_config_downloads WHERE id = %s", (grant_id,))
        db.close()
        flash("Доступ к скачиванию конфига отозван!", "success")

    return redirect(url_for("admin_panel"))


@app.route("/configs/<int:config_id>/download")
def download_config(config_id):
    """Скачивание только если админ выдал доступ (админы — всегда)."""
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    db = get_db()
    ensure_user_config_downloads_table(db)
    config = fetchone(db, "SELECT * FROM configs WHERE id = %s", (config_id,))
    allowed = bool(config and config.get("download_url") and user_can_download_config(user, config_id, db))
    db.close()

    if not config or not config.get("download_url"):
        flash("Конфиг не найден!", "error")
        return redirect(url_for("profile"))

    if not allowed:
        flash("Нет доступа к скачиванию. Купите конфиг и дождитесь, пока администратор выдаст доступ.", "error")
        return redirect(url_for("profile"))

    return redirect(config["download_url"])


# ==================================================================
#  ДОСТИЖЕНИЯ (Achievements)
# ==================================================================

@app.route("/api/achievements", methods=["POST"])
def api_get_user_achievements():
    """
    Мод дёргает этот эндпоинт (аналогично /api/verify) чтобы получить
    список достижений, выданных конкретному пользователю.
    Body: {"login": "...", "hwid": "...", "sessionToken": "..."}
    """
    data = request.get_json(force=True, silent=True) or {}
    username = data.get("login", "")
    hwid = data.get("hwid", "unknown")
    token = data.get("sessionToken", "")

    if not username or not token:
        return jsonify({"valid": False, "achievements": []}), 200

    try:
        payload = session_serializer.loads(token, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return jsonify({"valid": False, "achievements": []}), 200

    if payload.get("username") != username or payload.get("hwid") != hwid:
        return jsonify({"valid": False, "achievements": []}), 200

    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
    if not user:
        db.close()
        return jsonify({"valid": False, "achievements": []}), 200

    is_valid, _ = validate_user_access(user, hwid)
    if not is_valid:
        db.close()
        return jsonify({"valid": False, "achievements": []}), 200

    rows = fetchall(
        db,
        """
        SELECT a.code, a.name, a.description, a.image_url, a.unlock_feature, ua.granted_at
        FROM user_achievements ua
        JOIN achievements a ON a.id = ua.achievement_id
        WHERE ua.user_id = %s
        ORDER BY ua.granted_at DESC
        """,
        (user["id"],),
    )
    db.close()

    achievements = [
        {
            "code": r["code"],
            "name": r["name"],
            "description": r.get("description") or "",
            "imageUrl": normalize_achievement_image_url(r.get("image_url")),
            "unlockFeature": r.get("unlock_feature") or "",
            "grantedAt": r.get("granted_at") or "",
        }
        for r in rows
    ]

    return jsonify({"valid": True, "achievements": achievements}), 200




@app.route("/admin/achievements/delete", methods=["POST"])
def admin_delete_achievement():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    achievement_id = request.form.get("achievement_id")
    if achievement_id:
        db = get_db()
        achievement = fetchone(db, "SELECT image_url FROM achievements WHERE id = %s", (achievement_id,))
        execute(db, "DELETE FROM user_achievements WHERE achievement_id = %s", (achievement_id,))
        execute(db, "DELETE FROM achievements WHERE id = %s", (achievement_id,))
        db.close()

        if achievement:
            delete_image_from_github(achievement.get("image_url"))

        flash("Достижение удалено!", "success")

    return redirect(url_for("admin_panel"))


@app.route("/admin/achievements/grant", methods=["POST"])
def admin_grant_achievement():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    target_id = (request.form.get("user_id") or "").strip()
    target_id = target_id if target_id.isdigit() and len(target_id) < 10 else None
    achievement_id = request.form.get("achievement_id")

    if not target_id or not achievement_id:
        flash("Выберите пользователя и достижение!", "error")
        return redirect(url_for("admin_panel"))

    db = get_db()
    target_user = fetchone(db, "SELECT username FROM users WHERE id = %s", (target_id,))
    achievement = fetchone(db, "SELECT name FROM achievements WHERE id = %s", (achievement_id,))

    if not target_user or not achievement:
        db.close()
        flash("Пользователь или достижение не найдены!", "error")
        return redirect(url_for("admin_panel"))

    existing = fetchone(
        db,
        "SELECT id FROM user_achievements WHERE user_id = %s AND achievement_id = %s",
        (target_id, achievement_id),
    )
    if existing:
        db.close()
        flash(f"У {target_user['username']} уже есть это достижение!", "warning")
        return redirect(url_for("admin_panel"))

    execute(
        db,
        "INSERT INTO user_achievements (user_id, achievement_id, granted_at) VALUES (%s, %s, %s)",
        (target_id, achievement_id, _utcnow().isoformat()),
    )
    db.close()

    flash(f"Достижение «{achievement['name']}» выдано игроку {target_user['username']}!", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/achievements/revoke", methods=["POST"])
def admin_revoke_achievement():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    user_achievement_id = request.form.get("user_achievement_id")
    if user_achievement_id:
        db = get_db()
        execute(db, "DELETE FROM user_achievements WHERE id = %s", (user_achievement_id,))
        db.close()
        flash("Достижение отозвано!", "success")

    return redirect(url_for("admin_panel"))


def _is_api_request():
    return (request.path or "").startswith("/api/")


@app.errorhandler(500)
def handle_500(e):
    print(f"[500 error]: {e}")
    traceback.print_exc()
    _rollback_current()
    # Лаунчер/мод ждут JSON, а не редирект на HTML-страницу
    if _is_api_request():
        return jsonify({"success": False, "valid": False, "error": "Внутренняя ошибка сервера"}), 500
    flash("Внутренняя ошибка сервера. Попробуйте ещё раз.", "error")
    if request.path.startswith("/admin"):
        return redirect(url_for("admin_panel"))
    return redirect(url_for("profile"))


@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if _is_api_request():
            return jsonify({"success": False, "error": e.description}), e.code
        return e
    print(f"[unhandled exception {request.path}]: {e}")
    traceback.print_exc()
    _rollback_current()
    if _is_api_request():
        return jsonify({"success": False, "valid": False, "error": "Внутренняя ошибка сервера"}), 500
    flash("Внутренняя ошибка. Попробуйте ещё раз.", "error")
    try:
        referrer = request.referrer or url_for("profile")
        return redirect(referrer)
    except Exception:
        return redirect(url_for("profile"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
