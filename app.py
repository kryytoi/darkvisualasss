import os
import secrets
import string
import sqlite3
from datetime import datetime, timedelta
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
)
from werkzeug.security import generate_password_hash, check_password_hash

from werkzeug.middleware.proxy_fix import ProxyFix  # добавь к остальным импортам вверху файла

app = Flask(__name__)

# Vercel/Render терминируют HTTPS на прокси — говорим Flask доверять заголовкам прокси,
# иначе он считает соединение http и Secure-кука сессии не сохраняется.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.secret_key = os.environ.get("SECRET_KEY", "dark_visuals_super_secret_key_2026")

session_serializer = URLSafeTimedSerializer(app.secret_key, salt="darkvisuals-mod-session")

SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "3600"))

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True  # на Vercel всегда HTTPS

@app.after_request
def add_no_cache_headers(resp):
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
    "https://www.dropbox.com/scl/fi/spf52kd44fobbs983dvwf/DarkVisualsLoader.exe?rlkey=50ty74749w9g94gmku9y478iu&st=32q3kd1c&dl=1",
)

FUNPAY_LINKS = {
    "hwid_reset": "https://funpay.com/lots/offer?id=74616473",
    "1_month": "https://funpay.com/lots/offer?id=74616107",
    "120_days": "https://funpay.com/lots/offer?id=74616212",
    "lifetime": "https://funpay.com/lots/offer?id=74616281",
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
    "hwid_reset": {
        "name": "Сброс HWID",
        "price": "100 ₽",
        "period": "Разовая услуга",
        "image": "img/plan_banner.png",
        "features": [
            "Сбрасывает ваш HWID",
        ],
    },
}

MOD_AES_KEY_BASE64 = os.environ.get(
    "MOD_AES_KEY_BASE64", "ZmFrZWtleWZvcmRlbW9uc3RyYXRpb24xMjM0NTY3ODk="
)
MOD_AES_IV_BASE64 = os.environ.get("MOD_AES_IV_BASE64", "ZmFrZWl2Zm9yZGVtbzEyMw==")
MOD_FILE_URL = os.environ.get(
    "MOD_FILE_URL",
    "https://raw.githubusercontent.com/kryytoi/WDdwdw/refs/heads/main/darkvisuals.enc",
)


def get_db():
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(db_url, cursor_factory=RealDictCursor)

    # На Vercel SQLite не работает (read-only, эфемерная ФС)
    if os.environ.get("VERCEL") == "1":
        raise RuntimeError(
            "DATABASE_URL не задан! На Vercel нужен Postgres (Neon/Supabase). "
            "Добавь переменную окружения DATABASE_URL в настройках проекта."
        )

    conn = sqlite3.connect("database.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


def execute(db, query, params=()):
    cursor = db.cursor()
    if not os.environ.get("DATABASE_URL"):
        query = query.replace("%s", "?")
    cursor.execute(query, params)
    db.commit()
    return cursor


def fetchone(db, query, params=()):
    cursor = db.cursor()
    if not os.environ.get("DATABASE_URL"):
        query = query.replace("%s", "?")
    cursor.execute(query, params)
    res = cursor.fetchone()
    if res and not os.environ.get("DATABASE_URL"):
        res = dict(res)
    return res


def fetchall(db, query, params=()):
    cursor = db.cursor()
    if not os.environ.get("DATABASE_URL"):
        query = query.replace("%s", "?")
    cursor.execute(query, params)
    res = cursor.fetchall()
    if res and not os.environ.get("DATABASE_URL"):
        res = [dict(row) for row in res]
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
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_plain TEXT;")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(64) UNIQUE;")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255);")
        execute(db, "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;")
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
            "ALTER TABLE users ADD COLUMN password_plain TEXT;",
            "ALTER TABLE users ADD COLUMN google_id TEXT UNIQUE;",
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

    admin = fetchone(db, "SELECT id FROM users WHERE username = %s", ("admin",))
    if not admin:
        now = datetime.utcnow().isoformat()
        execute(
            db,
            """
            INSERT INTO users (username, password_hash, password_plain, role, status, expires_at, created_at, is_admin, plan)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ("admin", generate_password_hash("admin"), "admin", "Dev", "active", "forever", now, True, "Lifetime"),
        )
    db.close()


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


def apply_subscription_days(db, target_id, raw_days):
    raw_days = str(raw_days).strip().lower()

    if raw_days in ["forever", "навсегда"]:
        execute(
            db,
            "UPDATE users SET expires_at = 'forever', plan = 'Lifetime', status = 'active' WHERE id = %s",
            (target_id,),
        )
        return True, "Выдана вечная подписка (Forever)!"

    if raw_days.isdigit() and int(raw_days) > 0:
        days = int(raw_days)
        target_user = fetchone(db, "SELECT expires_at FROM users WHERE id = %s", (target_id,))
        now = datetime.utcnow()

        cur_exp = target_user.get("expires_at") if target_user else None
        if not cur_exp or cur_exp == "forever":
            base_time = now
        else:
            try:
                parsed = datetime.fromisoformat(cur_exp)
                base_time = parsed if parsed > now else now
            except Exception:
                base_time = now

        new_exp = (base_time + timedelta(days=days)).isoformat()
        execute(
            db,
            "UPDATE users SET expires_at = %s, plan = 'Active', status = 'active' WHERE id = %s",
            (new_exp, target_id),
        )
        return True, f"Подписка успешно продлена на {days} дн.!"

    return False, "Ошибка: некорректное значение срока подписки!"


def generate_subscription_key():
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "DARK-" + "-".join(parts)


def generate_achievement_code(name):
    base = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    base = base or "achievement"
    suffix = secrets.token_hex(3)
    return f"{base}_{suffix}"


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

    if expires_at != "forever":
        try:
            exp_date = datetime.fromisoformat(expires_at)
            if datetime.utcnow() > exp_date:
                return False, "Ваша подписка истекла!"
        except ValueError:
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

    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))

    if not user or not check_password_hash(user["password_hash"], password):
        db.close()
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

    return render_template(
        "buy.html",
        user=user,
        plan=plan,
        plan_key=plan_key,
        telegram_url=TELEGRAM_ADMIN_URL,
        funpay_url=funpay_url,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        try:
            db = get_db()
            user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
            db.close()
        except Exception as e:
            flash(f"Ошибка базы данных: {e}", "error")
            return render_template("login.html")

        if user and check_password_hash(user["password_hash"], password):
            session.permanent = True
            session["user_id"] = user["id"]
            return redirect(url_for("profile"))

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

            now = datetime.utcnow().isoformat()
            execute(
                db,
                """
                INSERT INTO users (username, password_hash, password_plain, role, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (username, generate_password_hash(password), password, "User", "active", now),
            )

            user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
            db.close()
        except Exception as e:
            flash(f"Ошибка базы данных: {e}", "error")
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

                now = datetime.utcnow().isoformat()
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
        flash(f"Ошибка базы данных: {e}", "error")
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
    if expires_at == "forever":
        sub_info = {"active": True, "text": "Навсегда (Forever)", "days_left": "∞"}
    elif expires_at:
        try:
            exp_date = datetime.fromisoformat(expires_at)
            now = datetime.utcnow()
            if exp_date > now:
                diff = exp_date - now
                sub_info = {
                    "active": True,
                    "text": exp_date.strftime("%d.%m.%Y %H:%M"),
                    "days_left": diff.days + 1
                }
            else:
                sub_info = {"active": False, "text": "Истекла", "days_left": 0}
        except ValueError:
            sub_info = {"active": False, "text": "Ошибка даты", "days_left": 0}

    return render_template("profile.html", user=user, sub=sub_info, launcher_url=LAUNCHER_URL)


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
        "UPDATE users SET password_hash = %s, password_plain = %s WHERE id = %s",
        (generate_password_hash(new_password), new_password, user["id"]),
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
    if not key_code:
        flash("Введите ключ активации!", "error")
        return redirect(url_for("profile"))

    db = get_db()
    key_row = fetchone(db, "SELECT * FROM subscription_keys WHERE key_code = %s", (key_code,))

    if not key_row:
        db.close()
        flash("Ключ не найден! Проверьте правильность ввода.", "error")
        return redirect(url_for("profile"))

    if key_row.get("is_used"):
        db.close()
        flash("Этот ключ уже был активирован ранее!", "error")
        return redirect(url_for("profile"))

    ok, msg = apply_subscription_days(db, user["id"], key_row.get("days"))
    if ok:
        execute(
            db,
            "UPDATE subscription_keys SET is_used = %s, used_by = %s, used_at = %s WHERE id = %s",
            (True, user["username"], datetime.utcnow().isoformat(), key_row["id"]),
        )
        flash(f"Ключ активирован! {msg}", "success")
    else:
        flash(msg, "error")

    db.close()
    return redirect(url_for("profile"))


@app.route("/admin")
def admin_panel():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    db = get_db()
    all_users = fetchall(db, "SELECT * FROM users ORDER BY id DESC")
    all_keys = fetchall(db, "SELECT * FROM subscription_keys ORDER BY id DESC")
    all_achievements = fetchall(db, "SELECT * FROM achievements ORDER BY id DESC")
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
    db.close()
    return render_template(
        "admin.html",
        users=all_users,
        keys=all_keys,
        current_user=user,
        achievements=all_achievements,
        grants=all_grants,
    )


@app.route("/admin/create_user", methods=["POST"])
def admin_create_user():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    days = str(request.form.get("days", "0")).strip().lower()

    if not username or not password:
        flash("Логин и пароль не могут быть пустыми!", "error")
        return redirect(url_for("admin_panel"))

    db = get_db()
    exists = fetchone(db, "SELECT id FROM users WHERE username = %s", (username,))
    if exists:
        db.close()
        flash("Пользователь с таким ником уже существует!", "error")
        return redirect(url_for("admin_panel"))

    now = datetime.utcnow()
    expires_at = None

    if days in ["forever", "навсегда"]:
        expires_at = "forever"
    elif days.isdigit() and int(days) > 0:
        expires_at = (now + timedelta(days=int(days))).isoformat()

    execute(
        db,
        "INSERT INTO users (username, password_hash, password_plain, plan, expires_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            username,
            generate_password_hash(password),
            password,
            "Active" if expires_at else None,
            expires_at,
            now.isoformat(),
        ),
    )
    db.close()

    flash(f"Пользователь {username} успешно создан!", "success")
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

    db = get_db()

    try:
        target_id = int(target_id)
    except ValueError:
        flash("Ошибка: Некорректный ID пользователя!", "error")
        db.close()
        return redirect(url_for("admin_panel"))

    if action == "ban":
        execute(db, "UPDATE users SET status = 'banned' WHERE id = %s", (target_id,))
        flash("Пользователь заблокирован!", "success")

    elif action == "unban":
        execute(db, "UPDATE users SET status = 'active' WHERE id = %s", (target_id,))
        flash("Пользователь разблокирован!", "success")

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
                "UPDATE users SET password_hash = %s, password_plain = %s WHERE id = %s",
                (generate_password_hash(new_password), new_password, target_id),
            )
            flash("Пароль пользователя успешно изменён!", "success")

    elif action == "delete":
        target = fetchone(db, "SELECT username FROM users WHERE id = %s", (target_id,))
        if not target:
            flash("Ошибка: Пользователь не найден!", "error")
        else:
            execute(db, "DELETE FROM users WHERE id = %s", (target_id,))
            flash(f"Пользователь {target['username']} удалён навсегда!", "success")
            if user.get("id") == target_id:
                session.clear()
                db.close()
                return redirect(url_for("login"))

    elif action == "make_admin":
        execute(db, "UPDATE users SET is_admin = TRUE WHERE id = %s", (target_id,))
        flash("Пользователю выданы права администратора!", "success")

    elif action == "remove_admin":
        execute(db, "UPDATE users SET is_admin = FALSE WHERE id = %s", (target_id,))
        flash("Права администратора отозваны!", "success")

    db.close()
    return redirect(url_for("admin_panel"))


@app.route("/admin/generate_key", methods=["POST"])
def admin_generate_key():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    raw_days = str(request.form.get("days", "")).strip().lower()
    if not raw_days:
        flash("Укажите срок подписки для ключа (например, 30 или forever)!", "error")
        return redirect(url_for("admin_panel"))

    if not (raw_days in ["forever", "навсегда"] or (raw_days.isdigit() and int(raw_days) > 0)):
        flash("Некорректный срок подписки для ключа!", "error")
        return redirect(url_for("admin_panel"))

    plan_name = "Lifetime" if raw_days in ["forever", "навсегда"] else f"{raw_days} дней"

    db = get_db()
    key_code = generate_subscription_key()
    while fetchone(db, "SELECT id FROM subscription_keys WHERE key_code = %s", (key_code,)):
        key_code = generate_subscription_key()

    execute(
        db,
        "INSERT INTO subscription_keys (key_code, days, plan_name, is_used, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (key_code, raw_days, plan_name, False, datetime.utcnow().isoformat()),
    )
    db.close()

    flash(f"Ключ создан: {key_code} ({plan_name})", "success")
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
            "imageUrl": r.get("image_url") or "",
            "unlockFeature": r.get("unlock_feature") or "",
            "grantedAt": r.get("granted_at") or "",
        }
        for r in rows
    ]

    return jsonify({"valid": True, "achievements": achievements}), 200


@app.route("/admin/achievements/create", methods=["POST"])
def admin_create_achievement():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    image_url = request.form.get("image_url", "").strip()
    unlock_feature = request.form.get("unlock_feature", "").strip()

    if not name:
        flash("Название достижения не может быть пустым!", "error")
        return redirect(url_for("admin_panel"))

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
        (code, name, description, image_url, unlock_feature, datetime.utcnow().isoformat()),
    )
    db.close()

    flash(f"Достижение «{name}» создано (код: {code})!", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/achievements/delete", methods=["POST"])
def admin_delete_achievement():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    achievement_id = request.form.get("achievement_id")
    if achievement_id:
        db = get_db()
        execute(db, "DELETE FROM user_achievements WHERE achievement_id = %s", (achievement_id,))
        execute(db, "DELETE FROM achievements WHERE id = %s", (achievement_id,))
        db.close()
        flash("Достижение удалено!", "success")

    return redirect(url_for("admin_panel"))


@app.route("/admin/achievements/grant", methods=["POST"])
def admin_grant_achievement():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    target_id = request.form.get("user_id")
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
        (target_id, achievement_id, datetime.utcnow().isoformat()),
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
