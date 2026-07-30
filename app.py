import os
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-render-env-vars")

# ---------------------------------------------------------------------------
# База данных: PostgreSQL (например Neon — neon.tech, бесплатный тариф,
# не удаляется по времени в отличие от бесплатного Render Postgres).
# Если DATABASE_URL не задан — используется локальный SQLite-файл (для теста
# на своём компьютере). На самом Render без DATABASE_URL данные будут теряться!
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
# Слой работы с базой данных (единый интерфейс для Postgres и SQLite)
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
    """SQL пишем с плейсхолдером %s (стиль Postgres), для SQLite конвертируем в ?"""
    return sql if USE_POSTGRES else sql.replace("%s", "?")


def fetchone(db, sql, params=()):
    cur = db.cursor()
    cur.execute(q(sql), params)
    row = cur.fetchone()
    cur.close()
    return row


def execute(db, sql, params=()):
    cur = db.cursor()
    cur.execute(q(sql), params)
    db.commit()
    cur.close()


def init_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.cursor().execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                plan TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                plan TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()


init_db()


# ---------- вспомогательные функции ----------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return fetchone(db, "SELECT * FROM users WHERE id = %s", (uid,))


def subscription_status(user):
    if not user or not user["expires_at"]:
        return "none", None
    if user["expires_at"] == "forever":
        return "forever", None
    expires = datetime.fromisoformat(user["expires_at"])
    if expires < datetime.utcnow():
        return "expired", expires
    return "active", expires


@app.context_processor
def inject_user():
    return {"user": current_user()}


# ---------- маршруты ----------

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


# ---------- активация подписки (временно вручную админом после оплаты) ----------

@app.route("/admin/activate", methods=["POST"])
def admin_activate():
    """
    Пример ручной активации после того, как ты проверил оплату.
    Вызывается так (curl / Postman):

    curl -X POST https://your-site.onrender.com/admin/activate \
      -d "key=supersecret-change-me&username=Vasya&plan=30"

    plan: 30 | 120 | forever
    """
    key = request.form.get("key")
    if key != ADMIN_KEY:
        return {"error": "forbidden"}, 403

    username = request.form.get("username", "").strip()
    plan_key = request.form.get("plan")
    if plan_key not in PLANS:
        return {"error": "bad plan"}, 400

    db = get_db()
    user = fetchone(db, "SELECT * FROM users WHERE username = %s", (username,))
    if not user:
        return {"error": "no such user"}, 404

    if plan_key == "forever":
        expires_value = "forever"
    else:
        days = PLANS[plan_key]["days"]
        expires_value = (datetime.utcnow() + timedelta(days=days)).isoformat()

    execute(
        db,
        "UPDATE users SET plan = %s, expires_at = %s WHERE id = %s",
        (plan_key, expires_value, user["id"]),
    )
    return {"ok": True, "username": username, "plan": plan_key, "expires_at": expires_value}


if __name__ == "__main__":
    app.run(debug=True, port=5000)
