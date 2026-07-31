import os
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-render-env-vars")

# ---------------------------------------------------------------------------
# База данных: PostgreSQL или SQLite
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    import sqlite3
    DB_PATH = os.path.join(os.path.dirname(__file__), "database.sqlite3")

LAUNCHER_URL = os.environ.get(
    "LAUNCHER_URL",
    "https://github.com/kryytoi/WDdwdw/releases/latest/download/launcher.exe",
)

PLANS = {
    "30": {"name": "30 дней", "days": 30, "price": 200},
    "120": {"name": "120 дней", "days": 120, "price": 400},
    "forever": {"name": "Навсегда", "days": None, "price": 600},
}

ADMIN_KEY = os.environ.get("ADMIN_KEY", "supersecret-change-me")


# ---------------------------------------------------------------------------
# Инициализация БД
# ---------------------------------------------------------------------------

def init_db():
    hashed_password = generate_password_hash("1488yanertviet1")
    now_str = datetime.utcnow().isoformat()

    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                plan TEXT,
                expires_at TEXT,
                hwid TEXT,
                status TEXT DEFAULT 'active',
                freeze_until TEXT,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TEXT NOT NULL
            )
            """
        )
        # Добавляем колонки, если таблица уже существовала
        for col in [("hwid", "TEXT"), ("status", "TEXT DEFAULT 'active'"), ("freeze_until", "TEXT"), ("is_admin", "BOOLEAN DEFAULT FALSE")]:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN {col[0]} {col[1]};")
            except Exception:
                conn.rollback()

        cur.execute("SELECT id FROM users WHERE username = %s", ("MrDarko",))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (username, password_hash, plan, expires_at, is_admin, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("MrDarko", hashed_password, "forever", "forever", True, now_str),
            )
        else:
            cur.execute("UPDATE users SET is_admin = TRUE WHERE username = %s", ("MrDarko",))
        conn.commit()
        conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                plan TEXT,
                expires_at TEXT,
                hwid TEXT,
                status TEXT DEFAULT 'active',
                freeze_until TEXT,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TEXT NOT NULL
            )
            """
        )
        # Добавление отсутствующих колонок для SQLite
        columns = [row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()]
        if "hwid" not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN hwid TEXT")
        if "status" not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")
        if "freeze_until" not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN freeze_until TEXT")
        if "is_admin" not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE")

        cur.execute("SELECT id FROM users WHERE username = ?", ("MrDarko",))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (username, password_hash, plan, expires_at, is_admin, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("MrDarko", hashed_password, "forever", "forever", True, now_str),
            )
        else:
            cur.execute("UPDATE users SET is_admin = 1 WHERE username = ?", ("MrDarko",))
        conn.commit()
        conn.close()

init_db()


# ---------------------------------------------------------------------------
# Слой работы с базой данных
# ---------------------------------------------------------------------------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        if USE_POSTGRES:
            db = g._database = psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
            )
        else:
            db = g._database = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def q(sql):
    return sql if USE_POSTGRES else sql.replace("%s", "?")

def fetchone(db, sql, params=()):
    cur = db.cursor()
    cur.execute(q(sql), params)
    row = cur.fetchone()
    cur.close()
    return row

def fetchall(db, sql, params=()):
    cur = db.cursor()
    cur.execute(q(sql), params)
    rows = cur.fetchall()
    cur.close()
    return rows

def execute(db, sql, params=()):
    cur = db.cursor()
    cur.execute(q(sql), params)
    db.commit()
    cur.close()


# ---------- вспомогательные функции ----------

def check_and_update_freeze(user, db):
    """Проверяет, не закончился ли срок заморозки."""
    if user and user["status"] == "frozen" and user["freeze_until"]:
        freeze_until = datetime.fromisoformat(user["freeze_until"])
        if datetime.utcnow() >= freeze_until:
            execute(db, "UPDATE users SET status = 'active', freeze_until = NULL WHERE id = %s", (user["id"],))

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE id = %s", (uid,))
    check_and_update_freeze(user, db)
    return user

def subscription_status(user):
    if not user or not user["expires_at"]:
        return "none", None
    if user["status"] == "banned":
        return "banned", None
    if user["status"] == "frozen":
        return "frozen", user["freeze_until"]
    if user["expires_at"] == "forever":
        return "forever", None

    expires = datetime.fromisoformat(user["expires_at"])
    if expires < datetime.utcnow():
        return "expired", expires
    return "active", expires

@app.context_processor
def inject_user():
    return {"user": current_user()}


# ---------- Маршруты приложения ----------

@app.route("/")
def index():
    return render_template("index.html", plans=PLANS)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3 or len(password) < 4:
            flash("Логин от 3 символов, пароль от 4 символов.", "error")
            return redirect(url_for("register"))

        db = get_db()
        exists = fetchone(db, "SELECT id FROM users WHERE username = %s", (username,))
        if exists:
            flash("Такой пользователь уже существует.", "error")
            return redirect(url_for("register"))

        execute(
            db,
            "INSERT INTO users (username, password_hash, plan, expires_at, created_at) "
            "VALUES (%s, %s, NULL, NULL, %s)",
            (username, generate_password_hash(password), datetime.utcnow().isoformat()),
        )

        user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
        session["user_id"] = user["id"]
        flash("Регистрация прошла успешно!", "success")
        return redirect(url_for("profile"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Неверный логин или пароль.", "error")
            return redirect(url_for("login"))

        session["user_id"] = user["id"]
        return redirect(url_for("profile"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/profile")
def profile():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    status, expires = subscription_status(user)
    return render_template(
        "profile.html",
        status=status,
        expires=expires,
        launcher_url=LAUNCHER_URL,
        plans=PLANS,
    )

@app.route("/buy/<plan_key>")
def buy(plan_key):
    user = current_user()
    if not user:
        flash("Сначала войдите в аккаунт.", "error")
        return redirect(url_for("login"))
    if plan_key not in PLANS:
        return redirect(url_for("index"))
    return render_template("buy.html", plan=PLANS[plan_key], plan_key=plan_key)


# ---------- АДМИН-ПАНЕЛЬ ВЕБ ----------

@app.route("/admin")
def admin_panel():
    user = current_user()
    if not user or not user["is_admin"]:
        return "Доступ запрещен", 403

    db = get_db()
    users = fetchall(db, "SELECT * FROM users ORDER BY id ASC")
    return render_template("admin.html", users=users)

@app.route("/admin/action", methods=["POST"])
def admin_action():
    user = current_user()
    if not user or not user["is_admin"]:
        return "Доступ запрещен", 403

    user_id = request.form.get("user_id")
    action = request.form.get("action")
    db = get_db()
    target_user = fetchone(db, "SELECT * FROM users WHERE id = %s", (user_id,))
    if not target_user:
        return "Пользователь не найден", 404

    now = datetime.utcnow()

    if action == "add_days":
        days = int(request.form.get("days", 30))
        if target_user["expires_at"] and target_user["expires_at"] != "forever":
            curr_exp = datetime.fromisoformat(target_user["expires_at"])
            start_date = curr_exp if curr_exp > now else now
            new_exp = (start_date + timedelta(days=days)).isoformat()
        else:
            new_exp = (now + timedelta(days=days)).isoformat()
        execute(db, "UPDATE users SET expires_at = %s, status = 'active' WHERE id = %s", (new_exp, user_id))

    elif action == "ban":
        execute(db, "UPDATE users SET status = 'banned' WHERE id = %s", (user_id,))
    elif action == "unban":
        execute(db, "UPDATE users SET status = 'active' WHERE id = %s", (user_id,))
    elif action == "freeze":
        days = int(request.form.get("freeze_days", 7))
        freeze_until = (now + timedelta(days=days)).isoformat()
        
        # Если есть активная подписка по датам, продлеваем её на время заморозки
        if target_user["expires_at"] and target_user["expires_at"] != "forever":
            curr_exp = datetime.fromisoformat(target_user["expires_at"])
            new_exp = (curr_exp + timedelta(days=days)).isoformat()
            execute(db, "UPDATE users SET status = 'frozen', freeze_until = %s, expires_at = %s WHERE id = %s", (freeze_until, new_exp, user_id))
        else:
            execute(db, "UPDATE users SET status = 'frozen', freeze_until = %s WHERE id = %s", (freeze_until, user_id))

    return redirect(url_for("admin_panel"))


# ---------- API ДЛЯ ЛАУНЧЕРА ----------

@app.route("/api/launcher/login", methods=["POST"])
def api_launcher_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    hwid = data.get("hwid", "")

    if not username or not password or not hwid:
        return jsonify({"status": "error", "message": "Не заполнено одно из полей"}), 400

    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"status": "error", "message": "Неверный логин или пароль"}), 401

    check_and_update_freeze(user, db)

    if user["status"] == "banned":
        return jsonify({"status": "error", "message": "Аккаунт заблокирован"}), 403

    if user["status"] == "frozen":
        return jsonify({"status": "error", "message": "Ваша подписка временно заморожена"}), 403

    # Привязка/проверка HWID
    if not user["hwid"]:
        execute(db, "UPDATE users SET hwid = %s WHERE id = %s", (hwid, user["id"]))
    elif user["hwid"] != hwid:
        return jsonify({"status": "error", "message": "Привязан другой HWID"}), 403

    status, expires = subscription_status(user)
    if status not in ("active", "forever"):
        return jsonify({"status": "error", "message": "Подписка неактивна или истекла"}), 403

    return jsonify({
        "status": "success",
        "username": user["username"],
        "expires_at": user["expires_at"]
    }), 200


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", os.environ.get("DEV_PORT", 3000))))
