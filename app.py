import os
import sqlite3
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dark_visuals_super_secret_key_2026")

# Вечные сессии (30 дней)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True

# ==========================================
#  НАСТРОЙКИ ДЛЯ КЛЮЧЕЙ МОДА (AES)
# ==========================================
MOD_AES_KEY_BASE64 = os.environ.get("MOD_AES_KEY_BASE64", "ZmFrZWtleWZvcmRlbW9uc3RyYXRpb24xMjM0NTY3ODk=")
MOD_AES_IV_BASE64 = os.environ.get("MOD_AES_IV_BASE64", "ZmFrZWl2Zm9yZGVtbzEyMw==")
MOD_FILE_URL = os.environ.get(
    "MOD_FILE_URL",
    "https://raw.githubusercontent.com/kryytoi/WDdwdw/refs/heads/main/darkvisuals.enc",
)


# ==========================================
#  ПОДКЛЮЧЕНИЕ К БАЗЕ (PostgreSQL / SQLite)
# ==========================================
def get_db():
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    else:
        print("[WARNING] DATABASE_URL не задан в Render! Данные будут сбрасываться!")
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

    admin = fetchone(db, "SELECT id FROM users WHERE username = %s", ("admin",))
    if not admin:
        now = datetime.utcnow().isoformat()
        execute(
            db,
            """
            INSERT INTO users (username, password_hash, role, status, expires_at, created_at, is_admin, plan)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ("admin", generate_password_hash("admin"), "Dev", "active", "forever", now, True, "Lifetime"),
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
    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE id = %s", (user_id,))
    db.close()
    return user


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


# ==========================================
#  API ДЛЯ ЛАУНЧЕРА
# ==========================================

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
    return jsonify({
        "success": True,
        "message": "Успешная авторизация!",
        "role": user.get("role", "User")
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
        "ModUrl": MOD_FILE_URL
    }), 200


# ==========================================
#  МАРШРУТЫ САЙТА
# ==========================================

@app.route("/")
def index():
    user = current_user()
    # Если есть index.html (главная с секцией #features), рендерим её!
    index_path = os.path.join(app.template_folder, "index.html")
    if os.path.exists(index_path):
        return render_template("index.html", user=user)
    return render_template("login.html", user=user)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
        db.close()

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
            INSERT INTO users (username, password_hash, role, status, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (username, generate_password_hash(password), "User", "active", now),
        )
        
        user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
        db.close()

        if user:
            session.permanent = True
            session["user_id"] = user["id"]
            return redirect(url_for("profile"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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

    return render_template("profile.html", user=user, sub=sub_info)


# ==========================================
#  АДМИНКА
# ==========================================

@app.route("/admin")
def admin_panel():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    db = get_db()
    all_users = fetchall(db, "SELECT * FROM users ORDER BY id DESC")
    db.close()
    return render_template("admin.html", users=all_users, current_user=user)


@app.route("/admin/create_user", methods=["POST"])
def admin_create_user():
    user = current_user()
    if not user or not user.get("is_admin"):
        return "Доступ запрещен", 403

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    days = request.form.get("days", "0")

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

    if days == "forever":
        expires_at = "forever"
    elif days.isdigit() and int(days) > 0:
        expires_at = (now + timedelta(days=int(days))).isoformat()

    execute(
        db,
        "INSERT INTO users (username, password_hash, plan, expires_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            username,
            generate_password_hash(password),
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

    target_id = request.form.get("user_id")
    action = request.form.get("action")

    if not target_id or not action:
        return redirect(url_for("admin_panel"))

    db = get_db()

    if action == "ban":
        execute(db, "UPDATE users SET status = 'banned' WHERE id = %s", (target_id,))
    elif action == "unban":
        execute(db, "UPDATE users SET status = 'active' WHERE id = %s", (target_id,))
    elif action == "add_days":
        days = int(request.form.get("days", 0))
        target_user = fetchone(db, "SELECT expires_at FROM users WHERE id = %s", (target_id,))
        if target_user:
            cur_exp = target_user.get("expires_at")
            now = datetime.utcnow()
            
            if not cur_exp or cur_exp == "forever":
                base_time = now
            else:
                try:
                    parsed = datetime.fromisoformat(cur_exp)
                    base_time = parsed if parsed > now else now
                except ValueError:
                    base_time = now
            
            new_exp = (base_time + timedelta(days=days)).isoformat()
            execute(
                db, 
                "UPDATE users SET expires_at = %s, plan = 'Active', status = 'active' WHERE id = %s", 
                (new_exp, target_id)
            )
            
    elif action == "freeze":
        execute(db, "UPDATE users SET status = 'frozen' WHERE id = %s", (target_id,))

    db.close()
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
