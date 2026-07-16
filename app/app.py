import html
import base64
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import requests
from authlib.jose import JsonWebToken
from flask import Flask, jsonify, redirect, render_template, render_template_string, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("OPS_DASHBOARD_DATA_DIR", "/data"))
SQLITE_PATH = DATA_DIR / "ops_dashboard.sqlite3"
ZONE_ID = os.getenv("OPS_DASHBOARD_TIME_ZONE", "Asia/Tokyo")
QUERY_TIMEOUT_SECONDS = int(os.getenv("OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS", "10"))
PROD_CONNECTION_PING_INTERVAL_SECONDS = int(os.getenv("OPS_DASHBOARD_CONNECTION_PING_INTERVAL_SECONDS", "30"))
SERVICE_MODE = os.getenv("OPS_DASHBOARD_SERVICE_MODE", "dashboard").strip().lower()
STATS_API_BASE_URL = os.getenv("OPS_DASHBOARD_STATS_API_URL", "").strip().rstrip("/")
STATS_API_TOKEN = os.getenv("OPS_DASHBOARD_STATS_API_TOKEN", "").strip()
STATS_API_TIMEOUT_SECONDS = int(os.getenv("OPS_DASHBOARD_STATS_API_TIMEOUT_SECONDS", "30"))
URL_PREFIX = os.getenv("OPS_DASHBOARD_URL_PREFIX", "").strip().rstrip("/")
STAT_TABLE_PAGE_SIZES = {20, 50, 100}
STAT_TABLE_TABS = {"items", "money", "yuanbao", "prestige", "guild", "registrants"}
SPECIAL_CLINIC_ZONE_ID = "Asia/Shanghai"
SPECIAL_CLINIC_WEEK_TAB_LIMIT = 8
SPECIAL_CLINIC_ITEM_NAMES = {
    1222: "广告牌I",
    1327: "聪明胶囊",
    1329: "胶囊",
    1330: "可爱胶囊",
    1351: "荣光病志残页",
    1662: "很加快预约",
    1664: "急速研究",
    1665: "加快预约",
    1666: "降低成本",
    1667: "提高效率",
    1671: "专家加班",
    1791: "补签卡",
    1792: "特需门诊票",
}
SPECIAL_CLINIC_EVENT_ITEM_IDS = {1351, 1792}
AUTH_MODE = os.getenv("OPS_DASHBOARD_AUTH_MODE", "firebase").strip().lower()
AUTH_ALLOWED_EMAILS = {
    email.strip().lower()
    for email in os.getenv("OPS_DASHBOARD_ALLOWED_EMAILS", "sunshaoxuan@gmail.com").split(",")
    if email.strip()
}
AUTH_PUBLIC_ENDPOINTS = {"healthz", "favicon", "login", "firebase_login"}
TOILET_MARKET_STALE_HOURS = 48
FIREBASE_PROJECT_ID = os.getenv("OPS_DASHBOARD_FIREBASE_PROJECT_ID", "r-hospital-c8069").strip()
FIREBASE_CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
FIREBASE_WEB_CONFIG = {
    "apiKey": os.getenv("OPS_DASHBOARD_FIREBASE_API_KEY", "AIzaSyAvjGq-c6jUho23Gby-sU9Wu_DEg3zqm74"),
    "authDomain": os.getenv("OPS_DASHBOARD_FIREBASE_AUTH_DOMAIN", "r-hospital-c8069.firebaseapp.com"),
    "projectId": FIREBASE_PROJECT_ID,
    "storageBucket": os.getenv("OPS_DASHBOARD_FIREBASE_STORAGE_BUCKET", "r-hospital-c8069.firebasestorage.app"),
    "messagingSenderId": os.getenv("OPS_DASHBOARD_FIREBASE_MESSAGING_SENDER_ID", "165812175721"),
    "appId": os.getenv("OPS_DASHBOARD_FIREBASE_APP_ID", "1:165812175721:web:0b6cf47683368ffe5833f8"),
}
firebase_cert_cache = {"expires_at": 0.0, "certs": {}}
prod_connection_state = threading.local()
stats_api_session_state = threading.local()

app = Flask(__name__, template_folder=str(APP_DIR / "templates"))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.getenv("OPS_DASHBOARD_SECRET_KEY", "")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("OPS_DASHBOARD_COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"},
)


def auth_setup_error():
    if AUTH_MODE in {"none", "off", "disabled"}:
        return None
    if AUTH_MODE != "firebase":
        return f"unsupported OPS_DASHBOARD_AUTH_MODE: {AUTH_MODE}"
    missing = [name for name in ("OPS_DASHBOARD_SECRET_KEY",) if not os.getenv(name, "").strip()]
    if missing:
        return f"missing required auth env: {', '.join(missing)}"
    if not AUTH_ALLOWED_EMAILS:
        return "missing required auth env: OPS_DASHBOARD_ALLOWED_EMAILS"
    if not FIREBASE_PROJECT_ID:
        return "missing required auth env: OPS_DASHBOARD_FIREBASE_PROJECT_ID"
    return None


def wants_json_response():
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def auth_error_response(message, status=403):
    if wants_json_response():
        return jsonify({"error": message}), status
    escaped_message = html.escape(str(message))
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>访问受限</title></head>"
        f"<body><h1>访问受限</h1><p>{escaped_message}</p><p><a href='{prefixed_url_for('login')}'>重新登录</a></p></body></html>",
        status,
    )


def prefixed_path(path):
    if not URL_PREFIX:
        return path
    if path == "/":
        return URL_PREFIX + "/"
    return URL_PREFIX + path


def prefixed_url_for(endpoint, **values):
    return prefixed_path(url_for(endpoint, **values))


def safe_next_url(value):
    next_url = str(value or "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    if URL_PREFIX and next_url == URL_PREFIX:
        return "/"
    if URL_PREFIX and next_url.startswith(URL_PREFIX + "/"):
        next_url = next_url[len(URL_PREFIX):] or "/"
    return next_url


def base64url_json(segment):
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def load_firebase_certs():
    now = time.time()
    if firebase_cert_cache["certs"] and firebase_cert_cache["expires_at"] > now:
        return firebase_cert_cache["certs"]
    response = requests.get(FIREBASE_CERTS_URL, timeout=5)
    response.raise_for_status()
    max_age = 3600
    cache_control = response.headers.get("cache-control", "")
    for part in cache_control.split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                max_age = int(part.split("=", 1)[1])
            except ValueError:
                max_age = 3600
    firebase_cert_cache["certs"] = response.json()
    firebase_cert_cache["expires_at"] = now + max_age
    return firebase_cert_cache["certs"]


def verify_firebase_id_token(id_token):
    if not id_token:
        raise ValueError("missing Firebase ID token")
    header = base64url_json(id_token.split(".", 1)[0])
    kid = header.get("kid")
    cert = load_firebase_certs().get(kid)
    if not cert:
        raise ValueError("unknown Firebase token key")
    claims_options = {
        "iss": {"essential": True, "value": f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}"},
        "aud": {"essential": True, "value": FIREBASE_PROJECT_ID},
        "sub": {"essential": True},
        "email": {"essential": True},
    }
    claims = JsonWebToken(["RS256"]).decode(id_token, cert, claims_options=claims_options)
    claims.validate(leeway=30)
    return dict(claims)


LOGIN_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light">
    <title>登录运营看板</title>
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: Lato, "Noto Sans SC", "Microsoft YaHei", "Segoe UI", sans-serif; background: #f5f7f8; color: #26323a; }
        main { position: relative; width: min(420px, calc(100vw - 32px)); overflow: hidden; border: 1px solid #dfe5e8; background: #fff; border-radius: 8px; padding: 32px 28px 28px; box-shadow: 0 12px 32px rgba(38,50,58,0.10); }
        main::before { content: ""; position: absolute; inset: 0 0 auto; height: 4px; background: #ef6a32; }
        h1 { margin: 0 0 10px; font-size: 22px; letter-spacing: 0; }
        p { margin: 0 0 20px; color: #687680; line-height: 1.6; }
        button { width: 100%; border: 1px solid #ef6a32; border-radius: 6px; padding: 12px 14px; background: #ef6a32; color: #fff; font-weight: 760; cursor: pointer; }
        button:hover { background: #d95724; border-color: #d95724; }
        button:focus-visible { outline: 3px solid rgba(7,156,156,0.24); outline-offset: 2px; }
        button:disabled { opacity: 0.55; cursor: wait; }
        .error { display: none; margin-top: 14px; color: #c6415d; font-size: 13px; line-height: 1.5; }
    </style>
</head>
<body>
<main>
    <h1>运营看板登录</h1>
    <p>请使用允许访问的 Google 账号登录。</p>
    <button id="loginBtn" type="button">使用 Google 登录</button>
    <div class="error" id="error"></div>
</main>
<script type="module">
    import { initializeApp } from "https://www.gstatic.com/firebasejs/12.1.0/firebase-app.js";
    import { getAuth, GoogleAuthProvider, signInWithPopup } from "https://www.gstatic.com/firebasejs/12.1.0/firebase-auth.js";

    const app = initializeApp({{ firebase_config|safe }});
    const auth = getAuth(app);
    const provider = new GoogleAuthProvider();
    const loginBtn = document.getElementById("loginBtn");
    const errorBox = document.getElementById("error");

    loginBtn.addEventListener("click", async () => {
        loginBtn.disabled = true;
        errorBox.style.display = "none";
        try {
            const result = await signInWithPopup(auth, provider);
            const idToken = await result.user.getIdToken();
            const response = await fetch("{{ firebase_login_url }}", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({idToken, next: {{ next_url_json|safe }}})
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.error || "登录失败");
            window.location.href = payload.redirect || "/";
        } catch (error) {
            errorBox.textContent = error.message || String(error);
            errorBox.style.display = "block";
            loginBtn.disabled = false;
        }
    });
</script>
</body>
</html>"""


@app.before_request
def require_dashboard_login():
    if SERVICE_MODE == "statistics_api":
        if request.endpoint == "healthz":
            return None
        if not request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        if not STATS_API_TOKEN:
            return jsonify({"error": "statistics API token is not configured"}), 503
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {STATS_API_TOKEN}"
        if not hmac.compare_digest(supplied, expected):
            return jsonify({"error": "statistics API authentication required"}), 401
        return None
    if request.endpoint in AUTH_PUBLIC_ENDPOINTS:
        return None
    if AUTH_MODE in {"none", "off", "disabled"}:
        return None
    setup_error = auth_setup_error()
    if setup_error:
        return auth_error_response(setup_error, 503)
    user = session.get("user") or {}
    email = str(user.get("email", "")).lower()
    if email in AUTH_ALLOWED_EMAILS:
        return None
    if wants_json_response():
        return jsonify({"error": "authentication required"}), 401
    return redirect(prefixed_url_for("login", next=request.full_path if request.query_string else request.path))


def now_in_zone():
    return datetime.now(ZoneInfo(ZONE_ID))


def require_config(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required env: {name}")
    return value


def use_stats_api():
    return SERVICE_MODE != "statistics_api" and bool(STATS_API_BASE_URL)


def get_stats_api_session():
    client = getattr(stats_api_session_state, "client", None)
    if client is None:
        client = requests.Session()
        stats_api_session_state.client = client
    return client


def fetch_stats_api(path, params=None):
    if not STATS_API_TOKEN:
        raise RuntimeError("missing required env: OPS_DASHBOARD_STATS_API_TOKEN")
    response = get_stats_api_session().get(
        f"{STATS_API_BASE_URL}{path}",
        params=params,
        headers={"Authorization": f"Bearer {STATS_API_TOKEN}"},
        timeout=STATS_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def create_prod_connection():
    conn = psycopg.connect(
        require_config("PROD_DB_URL"),
        user=require_config("PROD_DB_USERNAME"),
        password=require_config("PROD_DB_PASSWORD"),
        connect_timeout=QUERY_TIMEOUT_SECONDS,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("set default_transaction_read_only = on")
            cur.execute(f"set statement_timeout = '{QUERY_TIMEOUT_SECONDS}s'")
            cur.execute("set idle_in_transaction_session_timeout = '10s'")
    finally:
        conn.autocommit = False
    conn.read_only = True
    return conn


def discard_prod_connection(conn=None):
    current = getattr(prod_connection_state, "connection", None)
    target = conn or current
    if target is not None:
        try:
            target.close()
        except Exception:
            pass
    if current is target:
        prod_connection_state.connection = None
        prod_connection_state.last_checked = 0.0


def get_prod_connection():
    conn = getattr(prod_connection_state, "connection", None)
    if conn is not None and conn.closed:
        discard_prod_connection(conn)
        conn = None
    now = time.monotonic()
    last_checked = float(getattr(prod_connection_state, "last_checked", 0.0) or 0.0)
    if conn is not None and now - last_checked >= PROD_CONNECTION_PING_INTERVAL_SECONDS:
        try:
            with conn.cursor() as cur:
                cur.execute("select 1")
            conn.rollback()
        except psycopg.Error:
            discard_prod_connection(conn)
            conn = None
    if conn is None:
        conn = create_prod_connection()
        prod_connection_state.connection = conn
    prod_connection_state.last_checked = now
    return conn


@contextmanager
def prod_connection():
    conn = get_prod_connection()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except psycopg.Error:
            discard_prod_connection(conn)
        raise
    else:
        try:
            conn.rollback()
        except psycopg.Error:
            discard_prod_connection(conn)
            raise


@contextmanager
def sqlite_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def ensure_snapshot_table():
    with sqlite_connection() as conn:
        conn.execute(
            """
            create table if not exists daily_snapshot (
                day text primary key,
                generated_at text not null,
                online_now_accounts integer not null default 0,
                max_online_now_accounts integer not null default 0,
                active_today_accounts integer not null default 0,
                registrations_today integer not null default 0,
                recharge_cny_today real not null default 0,
                recharge_yuanbao_today integer not null default 0,
                recharge_orders_today integer not null default 0,
                skin_owner_accounts integer not null default 0,
                skin_equipped_accounts integer not null default 0,
                skin_free_accounts integer not null default 0,
                skin_purchase_log_accounts integer not null default 0,
                skin_paid_confirmed_accounts integer not null default 0
            )
            """
        )


def decimal_to_float(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def rows_to_dicts(cursor):
    columns = [desc.name for desc in cursor.description]
    return [
        {column: decimal_to_float(value) for column, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


def query_one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [desc.name for desc in cur.description]
        row = cur.fetchone()
        if row is None:
            return {}
        return {column: decimal_to_float(value) for column, value in zip(columns, row)}


def query_list(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return rows_to_dicts(cur)


def column_exists(conn, table_name, column_name):
    row = query_one(
        conn,
        """
        select exists (
            select 1
            from information_schema.columns
            where table_name = %s
              and column_name = %s
        ) as exists
        """,
        (table_name, column_name),
    )
    return bool(row.get("exists"))


def special_clinic_depleted_at_select(has_depleted_at):
    if has_depleted_at:
        return (
            "coalesce(to_char(min(c.depleted_at) at time zone 'UTC' at time zone %s, 'MM-DD HH24:MI'), '') as depleted_at",
            (SPECIAL_CLINIC_ZONE_ID,),
        )
    return "'' as depleted_at", ()


def special_clinic_cycle_start_expr(column_name="clinic_date"):
    return f"({column_name} - (((extract(dow from {column_name})::int + 4) %% 7) * interval '1 day'))::date"


def special_clinic_cycle_filter(column_name="clinic_date", week_start=None):
    if week_start:
        return f"{special_clinic_cycle_start_expr(column_name)} = %s::date", (week_start,)
    return f"{column_name} >= ((now() at time zone %s)::date - 13)", (SPECIAL_CLINIC_ZONE_ID,)


def special_clinic_time_filter(column_name="create_time", week_start=None):
    local_date_expr = f"({column_name} at time zone 'UTC' at time zone %s)::date"
    if week_start:
        return (
            f"{local_date_expr} >= %s::date and {local_date_expr} < (%s::date + interval '7 day')",
            (SPECIAL_CLINIC_ZONE_ID, week_start, SPECIAL_CLINIC_ZONE_ID, week_start),
        )
    return f"{local_date_expr} >= ((now() at time zone %s)::date - 13)", (
        SPECIAL_CLINIC_ZONE_ID,
        SPECIAL_CLINIC_ZONE_ID,
    )


def special_clinic_week_meta(cabinet_row, index):
    week_start = str(cabinet_row.get("clinic_date") or "")
    short_start = week_start[5:] if len(week_start) >= 10 else week_start
    prefix = "本周" if index == 0 else "上周" if index == 1 else f"前{index}周"
    range_label = week_start
    try:
        start_date = datetime.strptime(week_start, "%Y-%m-%d")
        end_date = start_date + timedelta(days=6)
        range_label = f"{start_date.strftime('%m-%d')} 至 {end_date.strftime('%m-%d')}"
    except ValueError:
        pass
    return {
        "key": week_start,
        "clinic_date": week_start,
        "label": f"{prefix} {short_start}".strip(),
        "range_label": range_label,
        "index": index,
    }


def apply_special_clinic_week_summary(summary, cabinet_row):
    summary.update({
        "diagnosis_count": cabinet_row.get("diagnosis_count_from_record", summary.get("diagnosis_count", 0)),
        "latest_clinic_date": cabinet_row.get("clinic_date", ""),
        "cabinet_status": cabinet_row.get("status", ""),
        "initial_total": cabinet_row.get("initial_total", 0),
        "remaining_total": cabinet_row.get("remaining_total", 0),
        "cabinet_remaining_total": cabinet_row.get("cabinet_remaining_total", 0),
        "total_diagnoses": cabinet_row.get("total_diagnoses", 0),
        "weekly_cabinet_diagnoses": cabinet_row.get("weekly_cabinet_diagnoses", 0),
        "non_canonical_cabinet_diagnoses": cabinet_row.get("non_canonical_cabinet_diagnoses", 0),
        "empty_attempt_count": cabinet_row.get("empty_attempt_count", 0),
        "critical_admitted_count": cabinet_row.get("critical_admitted_count", 0),
        "supply_total": cabinet_row.get("supply_total", 0),
        "consume_rate": cabinet_row.get("consume_rate", 0),
        "prescription_page_budget_total": cabinet_row.get("prescription_page_budget_total", 0),
        "prescription_page_awarded_total": cabinet_row.get("prescription_page_awarded_total", 0),
        "prescription_page_consume_rate": cabinet_row.get("prescription_page_consume_rate", 0),
        "base_initial_total": cabinet_row.get("base_initial_total", 0),
        "replenished_total": cabinet_row.get("replenished_total", 0),
        "replenished_equivalent_cost": cabinet_row.get("replenished_equivalent_cost", 0),
        "cycle_day": cabinet_row.get("cycle_day", 0),
        "remaining_replenishment_cap": cabinet_row.get("remaining_replenishment_cap", 0),
        "recent_2h_diagnoses": cabinet_row.get("recent_2h_diagnoses", 0),
        "projected_remaining": cabinet_row.get("projected_remaining", 0),
        "estimated_replenishment_now": cabinet_row.get("estimated_replenishment_now", 0),
    })
    return summary


def major_amount(value):
    return round(float(value or 0) / 100, 2)


def load_summary(conn):
    row = query_one(
        conn,
        """
        with owned as (
            select h.id as hospital_id, h.director_id, coalesce((b.items ->> '1018')::int, 0) as item_count
            from t_backpack b
            join t_hospitals h on h.id = b.hospital_id
            where jsonb_exists(b.items, '1018') and coalesce((b.items ->> '1018')::int, 0) > 0
        ), equipped as (
            select h.id as hospital_id, h.director_id
            from t_hospitals h
            where h.props ->> 'equippedSkin' = 'hospital-fifa2026'
        ), green_effect_used as (
            select distinct h.director_id
            from t_log_right_bottom r
            join t_hospitals h on h.id = r.hospital_id
            where r.content = '成功使用【绿茵盛典【效】(期间限定)】物品。'
        ), free_log as (
            select distinct h.director_id, r.hospital_id
            from t_log_right_bottom r
            join t_hospitals h on h.id = r.hospital_id
            where r.content = '成功领取1个【荣耀绿茵(期间限定)】'
        ), paid_log as (
            select distinct h.director_id, r.hospital_id
            from t_log_right_bottom r
            join t_hospitals h on h.id = r.hospital_id
            where r.content = '成功购买1个【荣耀绿茵(期间限定)】'
        ), paid_yuanbao as (
            select distinct h.director_id, l.hospital_id
            from t_log_yuanbao l
            join t_hospitals h on h.id = l.hospital_id
            where l.reason = '商店购买: 荣耀绿茵(期间限定) x 1'
        ), recharge_today as (
            select 'stripe' as source, amount, currency, yuanbao_amount
            from t_payment_orders
            where status = 'COMPLETED'
              and (update_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date
            union all
            select 'paddle' as source, amount, currency, yuanbao_amount
            from t_paddle_payment_orders
            where status = 'COMPLETED'
              and (update_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date
            union all
            select 'steam' as source, amount, currency, yuanbao_amount
            from t_steam_payment_orders
            where status = 'COMPLETED' and delivered is true
              and (update_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date
        )
        select
            (select count(distinct director_id) from owned) as skin_owner_accounts,
            (select count(*) from owned) as skin_owner_hospitals,
            (select count(distinct director_id) from equipped) as skin_equipped_accounts,
            (select count(*)
               from (select distinct director_id from equipped) e
               join green_effect_used g on g.director_id = e.director_id) as green_combo_equipped_accounts,
            (select count(distinct director_id) from free_log) as skin_free_accounts,
            (select count(distinct director_id) from paid_log) as skin_purchase_log_accounts,
            (select count(distinct director_id) from paid_yuanbao) as skin_paid_confirmed_accounts,
            (select count(distinct director_id)
               from t_hospitals
              where last_heartbeat_time is not null
                and last_heartbeat_time::timestamptz >= now() - interval '10 minutes') as online_now_accounts,
            (select count(distinct h.director_id)
               from t_log_right_bottom r
               join t_hospitals h on h.id = r.hospital_id
              where r.content like '%%院长 驾到主持工作%%'
                and (r.create_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date) as active_today_accounts,
            (select count(*)
               from t_directors
              where (create_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date) as registrations_today,
            (select coalesce(sum(amount), 0) from recharge_today where lower(currency) = 'cny') as recharge_cny_minor_today,
            (select coalesce(sum(yuanbao_amount), 0) from recharge_today) as recharge_yuanbao_today,
            (select count(*) from recharge_today) as recharge_orders_today,
            (select coalesce(sum(amount), 0) from recharge_today where source = 'stripe' and lower(currency) = 'cny') as stripe_recharge_cny_minor_today,
            (select coalesce(sum(yuanbao_amount), 0) from recharge_today where source = 'stripe') as stripe_recharge_yuanbao_today,
            (select count(*) from recharge_today where source = 'stripe') as stripe_recharge_orders_today,
            (select coalesce(sum(amount), 0) from recharge_today where source = 'steam' and lower(currency) = 'cny') as steam_recharge_cny_minor_today,
            (select coalesce(sum(yuanbao_amount), 0) from recharge_today where source = 'steam') as steam_recharge_yuanbao_today,
            (select count(*) from recharge_today where source = 'steam') as steam_recharge_orders_today
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID),
    )
    row["recharge_cny_today"] = major_amount(row.pop("recharge_cny_minor_today", 0))
    row["stripe_recharge_cny_today"] = major_amount(row.pop("stripe_recharge_cny_minor_today", 0))
    row["steam_recharge_cny_today"] = major_amount(row.pop("steam_recharge_cny_minor_today", 0))
    return row


def load_stats_from_prod():
    with prod_connection() as conn:
        summary = load_summary(conn)
        stats = {
            "generatedAt": now_in_zone().isoformat(),
            "zoneId": ZONE_ID,
            "note": "在线历史来自医院最后心跳时间；最近14日日活来自进入游戏日志。",
            "summary": summary,
            "onlineBuckets": query_list(
                conn,
                """
                select to_char(
                           to_timestamp(floor(extract(epoch from last_heartbeat_time::timestamptz) / 600) * 600)
                               at time zone %s,
                           'YYYY-MM-DD HH24:MI') as label,
                       count(distinct director_id) as count
                from t_hospitals
                where last_heartbeat_time is not null
                  and last_heartbeat_time::timestamptz >= now() - interval '24 hours'
                group by label
                order by label
                """,
                (ZONE_ID,),
            ),
            "dailyRegistrations": query_list(
                conn,
                """
                select ((create_time at time zone 'UTC' at time zone %s)::date)::text as day,
                       count(*) as count
                from t_directors
                where (create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
                group by day
                order by day
                """,
                (ZONE_ID, ZONE_ID, ZONE_ID),
            ),
            "dailyActive": query_list(
                conn,
                """
                select ((r.create_time at time zone 'UTC' at time zone %s)::date)::text as day,
                       count(distinct h.director_id) as count
                from t_log_right_bottom r
                join t_hospitals h on h.id = r.hospital_id
                where r.content like '%%院长 驾到主持工作%%'
                  and (r.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
                group by day
                order by day
                """,
                (ZONE_ID, ZONE_ID, ZONE_ID),
            ),
            "dailyRecharge": load_daily_recharge(conn),
            "hourlyYuanbaoSpending": load_hourly_yuanbao_spending(conn),
            "weeklyYuanbaoSpending": load_weekly_yuanbao_spending(conn),
            "weeklyYuanbaoPurchases": load_weekly_yuanbao_purchases(conn),
            "itemPurchases": load_item_purchases(conn),
            "itemUsages": load_item_usages(conn),
        }
        return stats


def load_daily_recharge(conn):
    rows = query_list(
        conn,
        """
        with orders as (
            select update_time, amount, currency, yuanbao_amount from t_payment_orders where status = 'COMPLETED'
            union all
            select update_time, amount, currency, yuanbao_amount from t_paddle_payment_orders where status = 'COMPLETED'
            union all
            select update_time, amount, currency, yuanbao_amount from t_steam_payment_orders where status = 'COMPLETED' and delivered is true
        )
        select ((update_time at time zone 'UTC' at time zone %s)::date)::text as day,
               lower(coalesce(currency, 'unknown')) as currency,
               count(*) as orders,
               coalesce(sum(amount), 0) as amount_minor,
               coalesce(sum(yuanbao_amount), 0) as yuanbao
        from orders
        where (update_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        group by day, lower(coalesce(currency, 'unknown'))
        order by day, currency
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID),
    )
    for row in rows:
        row["amount"] = major_amount(row.pop("amount_minor", 0))
    return rows


def load_hourly_yuanbao_spending(conn):
    return query_list(
        conn,
        """
        select to_char(date_trunc('hour', create_time at time zone 'UTC' at time zone %s),
                       'MM-DD HH24:00') as label,
               count(*) as event_count,
               coalesce(sum(greatest(coalesce(old_value, 0) - coalesce(new_value, 0), 0)), 0) as yuanbao_spent
        from t_log_yuanbao
        where old_value is not null
          and new_value is not null
          and old_value > new_value
          and (create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        group by label
        order by min(create_time)
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID),
    )


def load_weekly_yuanbao_spending(conn):
    return query_list(
        conn,
        """
        with bounds as (
            select date_trunc('week', now() at time zone %s)::date as current_week_start
        ), weeks as (
            select generate_series(
                       current_week_start - interval '9 weeks',
                       current_week_start,
                       interval '1 week'
                   )::date as week_start
            from bounds
        ), spending as (
            select date_trunc('week', create_time at time zone 'UTC' at time zone %s)::date as week_start,
                   count(*) as event_count,
                   coalesce(sum(greatest(coalesce(old_value, 0) - coalesce(new_value, 0), 0)), 0) as yuanbao_spent
            from t_log_yuanbao, bounds
            where old_value is not null
              and new_value is not null
              and old_value > new_value
              and (create_time at time zone 'UTC' at time zone %s)::date >= current_week_start - interval '9 weeks'
            group by week_start
        )
        select weeks.week_start::text as week_start,
               (weeks.week_start + 6)::text as week_end,
               to_char(weeks.week_start, 'MM-DD') || ' 至 ' || to_char(weeks.week_start + 6, 'MM-DD') as label,
               coalesce(spending.event_count, 0) as event_count,
               coalesce(spending.yuanbao_spent, 0) as yuanbao_spent
        from weeks
        left join spending on spending.week_start = weeks.week_start
        order by weeks.week_start
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID),
    )


def load_weekly_yuanbao_purchases(conn):
    return query_list(
        conn,
        """
        with bounds as (
            select date_trunc('week', now() at time zone %s)::date as current_week_start
        ), weeks as (
            select generate_series(
                       current_week_start - interval '9 weeks',
                       current_week_start,
                       interval '1 week'
                   )::date as week_start
            from bounds
        ), orders as (
            select update_time, yuanbao_amount
            from t_payment_orders
            where status = 'COMPLETED'
            union all
            select update_time, yuanbao_amount
            from t_paddle_payment_orders
            where status = 'COMPLETED'
            union all
            select update_time, yuanbao_amount
            from t_steam_payment_orders
            where status = 'COMPLETED' and delivered is true
        ), purchases as (
            select date_trunc('week', update_time at time zone 'UTC' at time zone %s)::date as week_start,
                   count(*) as order_count,
                   coalesce(sum(coalesce(yuanbao_amount, 0)), 0) as yuanbao_purchased
            from orders, bounds
            where (update_time at time zone 'UTC' at time zone %s)::date >= current_week_start - interval '9 weeks'
            group by week_start
        )
        select weeks.week_start::text as week_start,
               (weeks.week_start + 6)::text as week_end,
               to_char(weeks.week_start, 'MM-DD') || ' 至 ' || to_char(weeks.week_start + 6, 'MM-DD') as label,
               coalesce(purchases.order_count, 0) as order_count,
               coalesce(purchases.yuanbao_purchased, 0) as yuanbao_purchased
        from weeks
        left join purchases on purchases.week_start = weeks.week_start
        order by weeks.week_start
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID),
    )


def purchase_category_case() -> str:
    background_names = (
        "几何迷宫【效】", "空间站【效】", "城镇绿地", "海岛风光", "休假岛", "荒漠高原",
        "冰天雪地", "鹰愁涧(马年限定)", "绿水青山", "黄沙荒原", "流波地带", "赤岩绝壁",
        "禁忌遗都", "极地晶域", "迷雾浮岛", "幽冥堡垒",
    )
    quoted_backgrounds = ", ".join(f"'{name}'" for name in background_names)
    return f"""
        case
            when item_name like '%%荣耀绿茵%%' then 'skin'
            when item_name like '%%绿茵盛典%%' or item_name in ({quoted_backgrounds}) then 'background'
            else 'item'
        end
    """


def load_purchase_insights(conn):
    categories = ("item", "background", "skin")
    return {
        category: {
            "top": load_purchase_top(conn, category),
            "buyers": load_purchase_buyers(conn, category),
        }
        for category in categories
    }


def parsed_purchase_cte() -> str:
    category_case = purchase_category_case()
    return f"""
        parsed as (
            select l.hospital_id, h.hospital_name, h.director_name,
                   (regexp_match(l.reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name,
                   ((regexp_match(l.reason, '^商店购买: (.+) x ([0-9]+)$'))[2])::bigint as quantity,
                   greatest(coalesce(l.old_value, 0) - coalesce(l.new_value, 0), 0) as yuanbao_cost,
                   l.create_time
            from t_log_yuanbao l
            left join t_hospitals h on h.id = l.hospital_id
            where l.reason ~ '^商店购买: .+ x [0-9]+$'
              and (l.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        ), categorized as (
            select *, {category_case} as category
            from parsed
            where item_name is not null
        )
    """


def load_purchase_top(conn, category):
    return query_list(
        conn,
        f"""
        with {parsed_purchase_cte()}
        select item_name, count(*) as event_count, count(distinct hospital_id) as hospital_count,
               coalesce(sum(quantity), 0) as quantity, coalesce(sum(yuanbao_cost), 0) as yuanbao
        from categorized
        where category = %s
        group by item_name
        order by event_count desc, quantity desc, yuanbao desc, item_name
        limit 10
        """,
        (ZONE_ID, ZONE_ID, category),
    )


def load_purchase_buyers(conn, category):
    return query_list(
        conn,
        f"""
        with {parsed_purchase_cte()}
        select to_char((create_time at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as purchase_time,
               hospital_id, coalesce(hospital_name, '') as hospital_name, coalesce(director_name, '') as director_name,
               item_name, quantity, yuanbao_cost as yuanbao
        from categorized
        where category = %s
        order by create_time desc, hospital_id desc, item_name
        limit 20
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID, category),
    )


def load_item_purchases(conn):
    return query_list(
        conn,
        """
        with parsed as (
            select (regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name,
                   ((regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[2])::bigint as quantity,
                   greatest(coalesce(old_value, 0) - coalesce(new_value, 0), 0) as yuanbao_cost,
                   hospital_id
            from t_log_yuanbao
            where reason ~ '^商店购买: .+ x [0-9]+$'
              and (create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        )
        select item_name, count(*) as event_count, count(distinct hospital_id) as hospital_count,
               coalesce(sum(quantity), 0) as quantity, coalesce(sum(yuanbao_cost), 0) as yuanbao
        from parsed
        where item_name is not null
        group by item_name
        order by quantity desc, event_count desc, item_name
        limit 12
        """,
        (ZONE_ID, ZONE_ID),
    )


def load_item_usages(conn):
    return query_list(conn, item_usage_sql("summary"), (ZONE_ID, ZONE_ID))


def item_usage_sql(mode):
    select_fields = (
        "item_name, count(*) as event_count, count(distinct hospital_id) as hospital_count, "
        "coalesce(sum(quantity), 0) as quantity, 0::bigint as yuanbao"
        if mode == "summary"
        else "hospital_id, coalesce(hospital_name, '') as hospital_name, coalesce(director_name, '') as director_name, "
        "count(*) as event_count, coalesce(sum(quantity), 0) as quantity, 0::bigint as yuanbao, "
        "to_char((max(create_time) at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as last_time"
    )
    group_by = "item_name" if mode == "summary" else "hospital_id, hospital_name, director_name"
    order_by = "quantity desc, event_count desc, item_name" if mode == "summary" else "quantity desc, event_count desc, max(create_time) desc, hospital_id desc"
    limit = "limit 12" if mode == "summary" else "limit 200"
    filter_item = "" if mode == "summary" else "and item_name = %s"
    return f"""
        with parsed as (
            select r.hospital_id, h.hospital_name, h.director_name,
                   case
                       when r.content ~ '^成功批量使用【.+】物品[0-9]+次' then regexp_replace(r.content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\1')
                       when r.content ~ '^成功使用【.+】物品' then regexp_replace(r.content, '^成功使用【(.+)】物品.*$', '\\1')
                       when r.content ~ '^批量打开【.+】[0-9]+次获得' then regexp_replace(r.content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\1')
                       when r.content ~ '^打开【.+】获得' then regexp_replace(r.content, '^打开【(.+)】获得.*$', '\\1')
                       else null
                   end as item_name,
                   case
                       when r.content ~ '^成功批量使用【.+】物品[0-9]+次' then (regexp_replace(r.content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\2'))::bigint
                       when r.content ~ '^批量打开【.+】[0-9]+次获得' then (regexp_replace(r.content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\2'))::bigint
                       else 1
                   end as quantity,
                   r.create_time
            from t_log_right_bottom r
            left join t_hospitals h on h.id = r.hospital_id
            where (r.content like '成功使用【%%' or r.content like '成功批量使用【%%' or r.content like '打开【%%' or r.content like '批量打开【%%')
              and (r.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        )
        select {select_fields}
        from parsed
        where item_name is not null {filter_item}
        group by {group_by}
        order by {order_by}
        {limit}
    """


def parse_page_args():
    tab = request.args.get("tab", "items").strip().lower()
    if tab not in STAT_TABLE_TABS:
        tab = "items"
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("pageSize", "20"))
    except ValueError:
        page_size = 20
    if page_size not in STAT_TABLE_PAGE_SIZES:
        page_size = 20
    return tab, page, page_size


def window_paged_query(conn, rows_sql, params, page, page_size):
    offset = (page - 1) * page_size
    rows = query_list(conn, rows_sql, (*params, page_size, offset))
    total = int(rows[0].get("total_count", 0)) if rows else 0
    for row in rows:
        row.pop("total_count", None)
    return total, rows


def load_stat_table(tab, page, page_size):
    with prod_connection() as conn:
        if tab == "items":
            payload = load_item_stat_table(conn, page, page_size)
        elif tab == "money":
            payload = load_hospital_value_table(conn, "money", "金钱", page, page_size)
        elif tab == "yuanbao":
            payload = load_hospital_value_table(conn, "ingot", "元宝", page, page_size)
        elif tab == "prestige":
            payload = load_hospital_value_table(conn, "prestige", "声望", page, page_size)
        elif tab == "guild":
            payload = load_guild_table(conn, page, page_size)
        else:
            payload = load_registrant_table(conn, page, page_size)
    payload.update({"tab": tab, "page": page, "pageSize": page_size})
    return payload


def load_item_stat_table(conn, page, page_size):
    rows_sql = """
        with purchases as (
            select (regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name,
                   ((regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[2])::bigint as quantity,
                   greatest(coalesce(old_value, 0) - coalesce(new_value, 0), 0) as yuanbao_cost
            from t_log_yuanbao
            where reason ~ '^商店购买: .+ x [0-9]+$'
        ), purchase_summary as (
            select item_name, count(*) as purchase_events, coalesce(sum(quantity), 0) as purchased_quantity,
                   coalesce(sum(yuanbao_cost), 0) as yuanbao_used
            from purchases
            where item_name is not null
            group by item_name
        ), usages as (
            select case
                       when content ~ '^成功批量使用【.+】物品[0-9]+次' then regexp_replace(content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\1')
                       when content ~ '^成功使用【.+】物品' then regexp_replace(content, '^成功使用【(.+)】物品.*$', '\\1')
                       when content ~ '^批量打开【.+】[0-9]+次获得' then regexp_replace(content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\1')
                       when content ~ '^打开【.+】获得' then regexp_replace(content, '^打开【(.+)】获得.*$', '\\1')
                       else null
                   end as item_name,
                   case
                       when content ~ '^成功批量使用【.+】物品[0-9]+次' then (regexp_replace(content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\2'))::bigint
                       when content ~ '^批量打开【.+】[0-9]+次获得' then (regexp_replace(content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\2'))::bigint
                       else 1
                   end as quantity
            from t_log_right_bottom
            where content like '成功使用【%%' or content like '成功批量使用【%%' or content like '打开【%%' or content like '批量打开【%%'
        ), use_summary as (
            select item_name, count(*) as use_events, coalesce(sum(quantity), 0) as consumed_quantity
            from usages
            where item_name is not null
            group by item_name
        ), combined as (
            select coalesce(p.item_name, u.item_name) as item_name,
                   coalesce(p.purchased_quantity, 0) as purchased_quantity,
                   coalesce(u.consumed_quantity, 0) as consumed_quantity,
                   coalesce(p.yuanbao_used, 0) as yuanbao_used,
                   coalesce(p.purchase_events, 0) as purchase_events,
                   coalesce(u.use_events, 0) as use_events
            from purchase_summary p
            full outer join use_summary u on u.item_name = p.item_name
        )
        select item_name, purchased_quantity, consumed_quantity, yuanbao_used, purchase_events, use_events,
               count(*) over() as total_count
        from combined
        order by purchased_quantity desc, consumed_quantity desc, yuanbao_used desc, item_name
        limit %s offset %s
    """
    total, rows = window_paged_query(conn, rows_sql, (), page, page_size)
    return {
        "title": "道具统计",
        "description": "按所有日志聚合商品购买数量、消耗数量和元宝使用数量。",
        "columns": [
            {"key": "item_name", "label": "道具"},
            {"key": "purchased_quantity", "label": "购买数量", "type": "number"},
            {"key": "consumed_quantity", "label": "消耗数量", "type": "number"},
            {"key": "yuanbao_used", "label": "元宝使用数量", "type": "number"},
            {"key": "purchase_events", "label": "购买日志", "type": "number"},
            {"key": "use_events", "label": "消耗日志", "type": "number"},
        ],
        "total": total,
        "rows": rows,
    }


def load_hospital_value_table(conn, column, label, page, page_size):
    rows_sql = f"""
        select id as hospital_id, coalesce(hospital_name, '') as hospital_name,
               coalesce(director_name, '') as director_name, coalesce({column}, 0) as value,
               to_char((update_time at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as update_time,
               count(*) over() as total_count
        from t_hospitals
        order by coalesce({column}, 0) desc, update_time desc, id desc
        limit %s offset %s
    """
    total, rows = window_paged_query(conn, rows_sql, (ZONE_ID,), page, page_size)
    return {
        "title": f"{label}排行",
        "description": f"按医院当前{label}从多到少排序。",
        "columns": [
            {"key": "hospital_id", "label": "医院 ID", "type": "number"},
            {"key": "hospital_name", "label": "医院名"},
            {"key": "director_name", "label": "院长名"},
            {"key": "value", "label": label, "type": "number"},
            {"key": "update_time", "label": "更新时间"},
        ],
        "total": total,
        "rows": rows,
    }


def load_guild_table(conn, page, page_size):
    rows_sql = """
        select g.id as guild_id, coalesce(g.name, '') as guild_name, coalesce(g.status, '') as status,
               coalesce(g.level, 0) as level, coalesce(g.build_points, 0) as build_points,
               coalesce(g.ingot_pool, 0) as ingot_pool, count(m.id) as members,
               coalesce(sum(m.donation_total), 0) as donation_total,
               to_char((g.create_time at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as create_time,
               count(*) over() as total_count
        from t_guild g
        left join t_guild_member m on m.guild_id = g.id
        group by g.id, g.name, g.status, g.level, g.build_points, g.ingot_pool, g.create_time
        order by coalesce(g.level, 0) desc, coalesce(g.build_points, 0) desc, coalesce(g.ingot_pool, 0) desc, g.id desc
        limit %s offset %s
    """
    total, rows = window_paged_query(conn, rows_sql, (ZONE_ID,), page, page_size)
    return {
        "title": "公会统计",
        "description": "按等级、建设值、元宝池从高到低排序。",
        "columns": [
            {"key": "guild_id", "label": "公会 ID", "type": "number"},
            {"key": "guild_name", "label": "公会名"},
            {"key": "status", "label": "状态"},
            {"key": "level", "label": "等级", "type": "number"},
            {"key": "build_points", "label": "建设值", "type": "number"},
            {"key": "ingot_pool", "label": "元宝池", "type": "number"},
            {"key": "members", "label": "成员数", "type": "number"},
            {"key": "donation_total", "label": "累计贡献", "type": "number"},
            {"key": "create_time", "label": "创建时间"},
        ],
        "total": total,
        "rows": rows,
    }


def load_registrant_table(conn, page, page_size):
    rows_sql = """
        select d.id as director_id, coalesce(d.username, '') as username, coalesce(d.auth_provider, '') as auth_provider,
               to_char((d.create_time at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as create_time,
               count(h.id) as hospital_count,
               coalesce(max(h.hospital_name), '') as hospital_name,
               coalesce(max(h.director_name), '') as director_name,
               count(*) over() as total_count
        from t_directors d
        left join t_hospitals h on h.director_id = d.id
        group by d.id, d.username, d.auth_provider, d.create_time
        order by d.create_time desc, d.id desc
        limit %s offset %s
    """
    total, rows = window_paged_query(conn, rows_sql, (ZONE_ID,), page, page_size)
    return {
        "title": "注册者",
        "description": "按账号注册时间从新到旧排序。",
        "columns": [
            {"key": "director_id", "label": "账号 ID", "type": "number"},
            {"key": "username", "label": "用户名"},
            {"key": "auth_provider", "label": "来源"},
            {"key": "create_time", "label": "注册时间"},
            {"key": "hospital_count", "label": "医院数", "type": "number"},
            {"key": "hospital_name", "label": "医院名"},
            {"key": "director_name", "label": "院长名"},
        ],
        "total": total,
        "rows": rows,
    }


def record_snapshot(stats):
    ensure_snapshot_table()
    summary = stats["summary"]
    day = datetime.fromisoformat(stats["generatedAt"]).astimezone(ZoneInfo(ZONE_ID)).date().isoformat()
    with sqlite_connection() as conn:
        old = conn.execute("select max_online_now_accounts from daily_snapshot where day = ?", (day,)).fetchone()
        max_online = max(int(old["max_online_now_accounts"]) if old else 0, int(summary["online_now_accounts"]))
        conn.execute(
            """
            insert into daily_snapshot (
                day, generated_at, online_now_accounts, max_online_now_accounts, active_today_accounts,
                registrations_today, recharge_cny_today, recharge_yuanbao_today, recharge_orders_today,
                skin_owner_accounts, skin_equipped_accounts, skin_free_accounts,
                skin_purchase_log_accounts, skin_paid_confirmed_accounts
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(day) do update set
                generated_at = excluded.generated_at,
                online_now_accounts = excluded.online_now_accounts,
                max_online_now_accounts = excluded.max_online_now_accounts,
                active_today_accounts = excluded.active_today_accounts,
                registrations_today = excluded.registrations_today,
                recharge_cny_today = excluded.recharge_cny_today,
                recharge_yuanbao_today = excluded.recharge_yuanbao_today,
                recharge_orders_today = excluded.recharge_orders_today,
                skin_owner_accounts = excluded.skin_owner_accounts,
                skin_equipped_accounts = excluded.skin_equipped_accounts,
                skin_free_accounts = excluded.skin_free_accounts,
                skin_purchase_log_accounts = excluded.skin_purchase_log_accounts,
                skin_paid_confirmed_accounts = excluded.skin_paid_confirmed_accounts
            """,
            (
                day,
                stats["generatedAt"],
                summary["online_now_accounts"],
                max_online,
                summary["active_today_accounts"],
                summary["registrations_today"],
                summary["recharge_cny_today"],
                summary["recharge_yuanbao_today"],
                summary["recharge_orders_today"],
                summary["skin_owner_accounts"],
                summary["skin_equipped_accounts"],
                summary["skin_free_accounts"],
                summary["skin_purchase_log_accounts"],
                summary["skin_paid_confirmed_accounts"],
            ),
        )


def merge_snapshot_history(stats):
    ensure_snapshot_table()
    cutoff = (now_in_zone().date() - timedelta(days=13)).isoformat()
    with sqlite_connection() as conn:
        rows = conn.execute("select * from daily_snapshot where day >= ? order by day", (cutoff,)).fetchall()
    if not rows:
        return stats
    active = {row["day"]: row["active_today_accounts"] for row in rows}
    registrations = {row["day"]: row["registrations_today"] for row in rows}
    recharge = {
        (row["day"], "cny"): {
            "day": row["day"],
            "currency": "cny",
            "orders": row["recharge_orders_today"],
            "amount": row["recharge_cny_today"],
            "yuanbao": row["recharge_yuanbao_today"],
        }
        for row in rows
    }
    for row in stats["dailyActive"]:
        active[row["day"]] = row["count"]
    for row in stats["dailyRegistrations"]:
        registrations[row["day"]] = row["count"]
    for row in stats["dailyRecharge"]:
        recharge[(row["day"], row["currency"])] = row
    stats["dailyActive"] = [{"day": day, "count": count} for day, count in sorted(active.items())]
    stats["dailyRegistrations"] = [{"day": day, "count": count} for day, count in sorted(registrations.items())]
    stats["dailyRecharge"] = [row for _, row in sorted(recharge.items())]
    return stats


def load_special_clinic_stats_from_prod(week_start=None):
    with prod_connection() as conn:
        weekly_cabinet = load_special_clinic_weekly_cabinet(conn)
        week_options = [
            special_clinic_week_meta(row, index)
            for index, row in enumerate(weekly_cabinet[:SPECIAL_CLINIC_WEEK_TAB_LIMIT])
            if row.get("clinic_date")
        ]
        weekly_pages = load_special_clinic_weekly_pages(conn, weekly_cabinet, week_start)
        if weekly_pages:
            latest_page = dict(weekly_pages[0])
            summary = latest_page["summary"]
            daily_summary = latest_page["dailySummary"]
            hourly_summary = latest_page["hourlySummary"]
            tier_distribution = latest_page["tierDistribution"]
            patient_distribution = latest_page["patientDistribution"]
            reward_items = latest_page["rewardItems"]
            compensation_rewards = latest_page["compensationRewards"]
            resource_rewards = latest_page["resourceRewards"]
            ticket_flows = latest_page["ticketFlows"]
            hospital_daily = latest_page["hospitalDaily"]
            audit_checks = latest_page["auditChecks"]
        else:
            summary = load_special_clinic_summary(conn)
            daily_summary = load_special_clinic_daily_summary(conn)
            hourly_summary = load_special_clinic_hourly_summary(conn)
            tier_distribution = load_special_clinic_tier_distribution(conn)
            patient_distribution = load_special_clinic_patient_distribution(conn)
            reward_items = load_special_clinic_reward_items(conn)
            compensation_rewards = load_special_clinic_compensation_rewards(conn)
            resource_rewards = load_special_clinic_resource_rewards(conn)
            ticket_flows = load_special_clinic_ticket_flows(conn)
            hospital_daily = load_special_clinic_hospital_daily(conn)
            audit_checks = load_special_clinic_audit_checks(conn)
        return {
            "generatedAt": datetime.now(ZoneInfo(SPECIAL_CLINIC_ZONE_ID)).isoformat(),
            "zoneId": SPECIAL_CLINIC_ZONE_ID,
            "weekOptions": week_options,
            "weeklyPages": weekly_pages,
            "summary": summary,
            "dailySummary": daily_summary,
            "hourlySummary": hourly_summary,
            "tierDistribution": tier_distribution,
            "patientDistribution": patient_distribution,
            "rewardItems": reward_items,
            "compensationRewards": compensation_rewards,
            "resourceRewards": resource_rewards,
            "ticketFlows": ticket_flows,
            "weeklyCabinet": weekly_cabinet,
            "dailyCabinet": weekly_cabinet,
            "hospitalDaily": hospital_daily,
            "auditChecks": audit_checks,
        }


def load_special_clinic_weekly_pages(conn, weekly_cabinet, requested_week=None):
    available = weekly_cabinet[:SPECIAL_CLINIC_WEEK_TAB_LIMIT]
    selected = None
    for index, cabinet_row in enumerate(available):
        week_start = cabinet_row.get("clinic_date")
        if not week_start:
            continue
        if requested_week is None or str(week_start) == requested_week:
            selected = (index, cabinet_row, str(week_start))
            break
    if selected is None:
        return []
    index, cabinet_row, week_start = selected
    summary = load_special_clinic_summary(conn, week_start)
    apply_special_clinic_week_summary(summary, cabinet_row)
    return [{
        "week": special_clinic_week_meta(cabinet_row, index),
        "summary": summary,
        "dailySummary": load_special_clinic_daily_summary(conn, week_start),
        "hourlySummary": load_special_clinic_hourly_summary(conn, week_start),
        "tierDistribution": load_special_clinic_tier_distribution(conn, week_start),
        "patientDistribution": load_special_clinic_patient_distribution(conn, week_start),
        "rewardItems": load_special_clinic_reward_items(conn, week_start),
        "compensationRewards": load_special_clinic_compensation_rewards(conn, week_start),
        "resourceRewards": load_special_clinic_resource_rewards(conn, week_start),
        "ticketFlows": load_special_clinic_ticket_flows(conn, week_start),
        "weeklyCabinet": [cabinet_row],
        "dailyCabinet": [cabinet_row],
        "hospitalDaily": load_special_clinic_hospital_daily(conn, week_start),
        "auditChecks": load_special_clinic_audit_checks(conn, week_start),
    }]


def load_special_clinic_summary(conn, week_start=None):
    record_where, record_params = special_clinic_cycle_filter("clinic_date", week_start)
    ticket_where, ticket_params = special_clinic_cycle_filter("clinic_date", week_start)
    compensation_where, compensation_params = special_clinic_compensation_reward_filter("create_time", week_start)
    cabinet_where, cabinet_params = (
        f"where {special_clinic_cycle_start_expr('clinic_date')} = %s::date",
        (week_start,),
    ) if week_start else ("", ())
    row = query_one(
        conn,
        f"""
        with r as (
            select *
            from t_special_clinic_patient_record
            where {record_where}
        ), t as (
            select *
            from t_special_clinic_ticket_log
            where {ticket_where}
        ), compensation_rewards as (
            select *
            from t_compensation_batch_record
            where {compensation_where}
        ), c as (
            select *
            from t_special_clinic_cabinet
            {cabinet_where}
            order by clinic_date desc, id desc
            limit 1
        )
        select
            (select count(*) from r) as diagnosis_count,
            (select count(distinct hospital_id) from r) as active_hospital_count,
            (select count(*) from r where ticket_type_used = 'PAID') as paid_diagnosis_count,
            (select coalesce(sum(greatest(paid_delta, 0)), 0) from t where change_type = 'PURCHASE') as paid_ticket_purchased,
            (select coalesce(sum(ingot_cost), 0) from t where change_type = 'PURCHASE') as ingot_cost,
            (select coalesce(sum(coalesce(num, 0) * coalesce(success_count, 0)), 0) from compensation_rewards) as compensated_reward_item_count,
            (select coalesce(sum(coalesce(success_count, 0)), 0) from compensation_rewards) as compensated_reward_hospitals,
            (select coalesce(sum(temporary_patients), 0) from r) as temporary_patients,
            (select coalesce(sum(ingot_reward), 0) from r) as ingot_reward,
            (select coalesce(sum(money_reward), 0) from r) as money_reward,
            (select coalesce(sum(prestige_reward), 0) from r) as prestige_reward,
            (select coalesce(sum(glory_reward), 0) from r) as glory_reward,
            coalesce((select clinic_date::text from c), '') as latest_clinic_date,
            coalesce((select status from c), '') as cabinet_status,
            coalesce((select initial_total from c), 0) as initial_total,
            coalesce((select remaining_total from c), 0) as remaining_total,
            coalesce((select total_diagnoses from c), 0) as total_diagnoses,
            coalesce((select prescription_page_budget_total from c), 0) as prescription_page_budget_total,
            coalesce((select prescription_page_awarded_total from c), 0) as prescription_page_awarded_total,
            coalesce((select empty_attempt_count from c), 0) as empty_attempt_count,
            coalesce((select critical_admitted_count from c), 0) as critical_admitted_count
        """,
        (*record_params, *ticket_params, *compensation_params, *cabinet_params),
    )
    return add_special_clinic_supply_metrics(row)


def add_special_clinic_supply_metrics(row):
    initial_total = int(row.get("initial_total") or 0)
    total_diagnoses = int(row.get("total_diagnoses") or 0)
    supply_total = initial_total if initial_total > 0 else total_diagnoses
    row["supply_total"] = supply_total
    row["consume_rate"] = round(total_diagnoses * 100 / supply_total, 2) if supply_total else 0
    prescription_page_budget = int(row.get("prescription_page_budget_total") or 0)
    prescription_page_awarded = int(row.get("prescription_page_awarded_total") or 0)
    row["prescription_page_consume_rate"] = (
        round(prescription_page_awarded * 100 / prescription_page_budget, 2)
        if prescription_page_budget
        else 0
    )
    return row


def special_clinic_compensation_reward_filter(column_name="create_time", week_start=None):
    time_where, time_params = special_clinic_time_filter(column_name, week_start)
    event_ids = ", ".join(str(item_id) for item_id in sorted(SPECIAL_CLINIC_EVENT_ITEM_IDS))
    return (
        f"""
        coalesce(type, '') = 'item'
        and coalesce(status, '') in ('SUCCESS', 'PARTIAL_FAILED')
        and (
            coalesce(item_id, 0) in ({event_ids})
            or coalesce(log, '') like '%%特需%%'
            or coalesce(log, '') like '%%门诊%%'
        )
        and {time_where}
        """,
        time_params,
    )


def load_special_clinic_hourly_summary(conn, week_start=None):
    record_where, record_params = special_clinic_cycle_filter("clinic_date", week_start)
    ticket_where, ticket_params = special_clinic_cycle_filter("clinic_date", week_start)
    compensation_where, compensation_params = special_clinic_compensation_reward_filter("create_time", week_start)
    return query_list(
        conn,
        f"""
        with record_hourly as (
            select date_trunc('hour', create_time at time zone 'UTC' at time zone %s) as hour_bucket,
                   count(*) as diagnosis_count,
                   count(distinct hospital_id) as active_hospital_count,
                   count(*) filter (where ticket_type_used = 'PAID') as paid_diagnosis_count,
                   coalesce(sum((reward_items ->> '1792')::int), 0) as reward_ticket_count,
                   coalesce(sum(temporary_patients), 0) as temporary_patients,
                   coalesce(sum(ingot_reward), 0) as ingot_reward,
                   coalesce(sum(money_reward), 0) as money_reward,
                   coalesce(sum(prestige_reward), 0) as prestige_reward,
                   coalesce(sum(glory_reward), 0) as glory_reward
            from t_special_clinic_patient_record
            where {record_where}
            group by hour_bucket
        ), ticket_hourly as (
            select date_trunc('hour', create_time at time zone 'UTC' at time zone %s) as hour_bucket,
                   coalesce(sum(abs(appointment_delta)) filter (where appointment_delta < 0), 0) as appointment_ticket_consume,
                   coalesce(sum(abs(gifted_delta)) filter (where gifted_delta < 0), 0) as gifted_ticket_consume,
                   coalesce(sum(abs(paid_delta)) filter (where paid_delta < 0), 0) as paid_ticket_consume,
                   coalesce(sum(paid_delta) filter (where paid_delta > 0 and change_type = 'PURCHASE'), 0) as paid_ticket_purchased,
                   coalesce(sum(ingot_cost) filter (where change_type = 'PURCHASE'), 0) as ingot_cost
            from t_special_clinic_ticket_log
            where {ticket_where}
            group by hour_bucket
        ), compensation_hourly as (
            select date_trunc('hour', create_time at time zone 'UTC' at time zone %s) as hour_bucket,
                   coalesce(sum(coalesce(num, 0) * coalesce(success_count, 0)), 0) as compensated_reward_item_count,
                   coalesce(sum(coalesce(success_count, 0)), 0) as compensated_reward_hospitals
            from t_compensation_batch_record
            where {compensation_where}
            group by hour_bucket
        )
        select to_char(coalesce(r.hour_bucket, t.hour_bucket, c.hour_bucket), 'MM-DD HH24:00') as label,
               coalesce(r.diagnosis_count, 0) as diagnosis_count,
               coalesce(r.active_hospital_count, 0) as active_hospital_count,
               coalesce(r.paid_diagnosis_count, 0) as paid_diagnosis_count,
               coalesce(t.appointment_ticket_consume, 0) as appointment_ticket_consume,
               coalesce(t.gifted_ticket_consume, 0) as gifted_ticket_consume,
               coalesce(t.paid_ticket_consume, 0) as paid_ticket_consume,
               coalesce(t.paid_ticket_purchased, 0) as paid_ticket_purchased,
               coalesce(t.ingot_cost, 0) as ingot_cost,
               coalesce(r.reward_ticket_count, 0) as reward_ticket_count,
               coalesce(c.compensated_reward_item_count, 0) as compensated_reward_item_count,
               coalesce(c.compensated_reward_hospitals, 0) as compensated_reward_hospitals,
               coalesce(r.temporary_patients, 0) as temporary_patients,
               coalesce(r.ingot_reward, 0) as ingot_reward,
               coalesce(r.money_reward, 0) as money_reward,
               coalesce(r.prestige_reward, 0) as prestige_reward,
               coalesce(r.glory_reward, 0) as glory_reward
        from record_hourly r
        full outer join ticket_hourly t on t.hour_bucket = r.hour_bucket
        full outer join compensation_hourly c on c.hour_bucket = coalesce(r.hour_bucket, t.hour_bucket)
        order by coalesce(r.hour_bucket, t.hour_bucket, c.hour_bucket)
        """,
        (
            SPECIAL_CLINIC_ZONE_ID,
            *record_params,
            SPECIAL_CLINIC_ZONE_ID,
            *ticket_params,
            SPECIAL_CLINIC_ZONE_ID,
            *compensation_params,
        ),
    )


def load_special_clinic_daily_summary(conn, week_start=None):
    if not week_start:
        record_where, record_params = special_clinic_time_filter("create_time", week_start)
        ticket_where, ticket_params = special_clinic_time_filter("create_time", week_start)
        return query_list(
            conn,
            f"""
            with record_daily as (
                select (create_time at time zone 'UTC' at time zone %s)::date as day,
                       count(*) as diagnosis_count,
                       count(distinct hospital_id) as active_hospital_count,
                       count(*) filter (where ticket_type_used = 'PAID') as paid_diagnosis_count,
                       coalesce(sum((reward_items ->> '1792')::int), 0) as reward_ticket_count,
                       coalesce(sum(temporary_patients), 0) as temporary_patients
                from t_special_clinic_patient_record
                where {record_where}
                group by day
            ), ticket_daily as (
                select (create_time at time zone 'UTC' at time zone %s)::date as day,
                       coalesce(sum(abs(appointment_delta)) filter (where appointment_delta < 0), 0) as appointment_ticket_consume,
                       coalesce(sum(abs(gifted_delta)) filter (where gifted_delta < 0), 0) as gifted_ticket_consume,
                       coalesce(sum(abs(paid_delta)) filter (where paid_delta < 0), 0) as paid_ticket_consume,
                       coalesce(sum(paid_delta) filter (where paid_delta > 0 and change_type = 'PURCHASE'), 0) as paid_ticket_purchased,
                       coalesce(sum(ingot_cost) filter (where change_type = 'PURCHASE'), 0) as ingot_cost
                from t_special_clinic_ticket_log
                where {ticket_where}
                group by day
            ), daily as (
                select coalesce(r.day, t.day) as day,
                       coalesce(r.diagnosis_count, 0) as diagnosis_count,
                       coalesce(r.active_hospital_count, 0) as active_hospital_count,
                       coalesce(r.paid_diagnosis_count, 0) as paid_diagnosis_count,
                       coalesce(t.appointment_ticket_consume, 0) as appointment_ticket_consume,
                       coalesce(t.gifted_ticket_consume, 0) as gifted_ticket_consume,
                       coalesce(t.paid_ticket_consume, 0) as paid_ticket_consume,
                       coalesce(t.paid_ticket_purchased, 0) as paid_ticket_purchased,
                       coalesce(t.ingot_cost, 0) as ingot_cost,
                       coalesce(r.reward_ticket_count, 0) as reward_ticket_count,
                       coalesce(r.temporary_patients, 0) as temporary_patients
                from record_daily r
                full outer join ticket_daily t on t.day = r.day
            )
            select day::text as clinic_date,
                   to_char(day, 'MM-DD') as label,
                   diagnosis_count,
                   (sum(diagnosis_count) over (order by day rows between unbounded preceding and current row))::bigint as cumulative_diagnosis_count,
                   active_hospital_count,
                   paid_diagnosis_count,
                   appointment_ticket_consume,
                   gifted_ticket_consume,
                   paid_ticket_consume,
                   paid_ticket_purchased,
                   ingot_cost,
                   reward_ticket_count,
                   temporary_patients
            from daily
            order by day
            """,
            (SPECIAL_CLINIC_ZONE_ID, *record_params, SPECIAL_CLINIC_ZONE_ID, *ticket_params),
        )

    record_where, record_params = special_clinic_time_filter("create_time", week_start)
    ticket_where, ticket_params = special_clinic_time_filter("create_time", week_start)
    return query_list(
        conn,
        f"""
        with days as (
            select generate_series(%s::date, %s::date + interval '6 day', interval '1 day')::date as day
        ), record_daily as (
            select (create_time at time zone 'UTC' at time zone %s)::date as day,
                   count(*) as diagnosis_count,
                   count(distinct hospital_id) as active_hospital_count,
                   count(*) filter (where ticket_type_used = 'PAID') as paid_diagnosis_count,
                   coalesce(sum((reward_items ->> '1792')::int), 0) as reward_ticket_count,
                   coalesce(sum(temporary_patients), 0) as temporary_patients
            from t_special_clinic_patient_record
            where {record_where}
            group by day
        ), ticket_daily as (
            select (create_time at time zone 'UTC' at time zone %s)::date as day,
                   coalesce(sum(abs(appointment_delta)) filter (where appointment_delta < 0), 0) as appointment_ticket_consume,
                   coalesce(sum(abs(gifted_delta)) filter (where gifted_delta < 0), 0) as gifted_ticket_consume,
                   coalesce(sum(abs(paid_delta)) filter (where paid_delta < 0), 0) as paid_ticket_consume,
                   coalesce(sum(paid_delta) filter (where paid_delta > 0 and change_type = 'PURCHASE'), 0) as paid_ticket_purchased,
                   coalesce(sum(ingot_cost) filter (where change_type = 'PURCHASE'), 0) as ingot_cost
            from t_special_clinic_ticket_log
            where {ticket_where}
            group by day
        ), daily as (
            select d.day,
                   coalesce(r.diagnosis_count, 0) as diagnosis_count,
                   coalesce(r.active_hospital_count, 0) as active_hospital_count,
                   coalesce(r.paid_diagnosis_count, 0) as paid_diagnosis_count,
                   coalesce(t.appointment_ticket_consume, 0) as appointment_ticket_consume,
                   coalesce(t.gifted_ticket_consume, 0) as gifted_ticket_consume,
                   coalesce(t.paid_ticket_consume, 0) as paid_ticket_consume,
                   coalesce(t.paid_ticket_purchased, 0) as paid_ticket_purchased,
                   coalesce(t.ingot_cost, 0) as ingot_cost,
                   coalesce(r.reward_ticket_count, 0) as reward_ticket_count,
                   coalesce(r.temporary_patients, 0) as temporary_patients
            from days d
            left join record_daily r on r.day = d.day
            left join ticket_daily t on t.day = d.day
        )
        select day::text as clinic_date,
               to_char(day, 'MM-DD') as label,
               diagnosis_count,
               (sum(diagnosis_count) over (order by day rows between unbounded preceding and current row))::bigint as cumulative_diagnosis_count,
               active_hospital_count,
               paid_diagnosis_count,
               appointment_ticket_consume,
               gifted_ticket_consume,
               paid_ticket_consume,
               paid_ticket_purchased,
               ingot_cost,
               reward_ticket_count,
               temporary_patients
        from daily
        order by day
        """,
        (week_start, week_start, SPECIAL_CLINIC_ZONE_ID, *record_params, SPECIAL_CLINIC_ZONE_ID, *ticket_params),
    )


def load_special_clinic_tier_distribution(conn, week_start=None):
    where_clause, params = special_clinic_cycle_filter("clinic_date", week_start)
    return query_list(
        conn,
        f"""
        select coalesce(tier, 'UNKNOWN') as tier,
               count(*) as diagnosis_count,
               count(distinct hospital_id) as hospital_count,
               count(*) filter (where ticket_type_used = 'PAID') as paid_ticket_count
        from t_special_clinic_patient_record
        where {where_clause}
        group by coalesce(tier, 'UNKNOWN')
        order by diagnosis_count desc, tier
        """,
        params,
    )


def load_special_clinic_patient_distribution(conn, week_start=None):
    where_clause, params = special_clinic_cycle_filter("clinic_date", week_start)
    return query_list(
        conn,
        f"""
        select coalesce(patient_code, '') as patient_code,
               coalesce(patient_name, '') as patient_name,
               coalesce(tier, 'UNKNOWN') as tier,
               count(*) as diagnosis_count,
               count(distinct hospital_id) as hospital_count,
               count(*) filter (where ticket_type_used = 'PAID') as paid_ticket_count
        from t_special_clinic_patient_record
        where {where_clause}
        group by patient_code, patient_name, tier
        order by diagnosis_count desc, paid_ticket_count desc, patient_name
        limit 40
        """,
        params,
    )


def load_special_clinic_reward_items(conn, week_start=None):
    where_clause, params = special_clinic_cycle_filter("r.clinic_date", week_start)
    compensation_where, compensation_params = special_clinic_compensation_reward_filter("create_time", week_start)
    rows = query_list(
        conn,
        f"""
        with diagnosis_items as (
            select e.key::bigint as item_id,
                   coalesce(sum(e.value::int), 0) as diagnosis_item_count,
                   count(*) as record_count,
                   count(distinct r.hospital_id) as hospital_count
            from t_special_clinic_patient_record r
            cross join lateral jsonb_each_text(coalesce(r.reward_items, '{{}}'::jsonb)) e
            where {where_clause}
            group by e.key::bigint
        ), compensation_items as (
            select coalesce(item_id, 0)::bigint as item_id,
                   coalesce(sum(coalesce(num, 0) * coalesce(success_count, 0)), 0) as compensation_item_count,
                   count(*) as compensation_batch_count,
                   coalesce(sum(coalesce(success_count, 0)), 0) as compensation_hospital_count
            from t_compensation_batch_record
            where {compensation_where}
            group by coalesce(item_id, 0)::bigint
        )
        select coalesce(d.item_id, c.item_id) as item_id,
               coalesce(d.diagnosis_item_count, 0) + coalesce(c.compensation_item_count, 0) as item_count,
               coalesce(d.diagnosis_item_count, 0) as diagnosis_item_count,
               coalesce(c.compensation_item_count, 0) as compensation_item_count,
               coalesce(d.record_count, 0) as record_count,
               coalesce(d.hospital_count, 0) as hospital_count,
               coalesce(c.compensation_batch_count, 0) as compensation_batch_count,
               coalesce(c.compensation_hospital_count, 0) as compensation_hospital_count
        from diagnosis_items d
        full outer join compensation_items c on c.item_id = d.item_id
        where coalesce(d.item_id, c.item_id) > 0
        order by item_count desc, record_count desc, item_id
        """,
        (*params, *compensation_params),
    )
    for row in rows:
        row["item_name"] = SPECIAL_CLINIC_ITEM_NAMES.get(int(row["item_id"]), f"道具 {row['item_id']}")
    return rows


def load_special_clinic_compensation_rewards(conn, week_start=None):
    compensation_where, compensation_params = special_clinic_compensation_reward_filter("create_time", week_start)
    rows = query_list(
        conn,
        f"""
        select to_char(date_trunc('hour', create_time at time zone 'UTC' at time zone %s), 'MM-DD HH24:00') as label,
               coalesce(batch_no, '') as batch_no,
               coalesce(item_id, 0)::bigint as item_id,
               coalesce(num, 0) as item_each,
               coalesce(success_count, 0) as hospital_count,
               coalesce(num, 0) * coalesce(success_count, 0) as item_count,
               coalesce(log, '') as reason
        from t_compensation_batch_record
        where {compensation_where}
        order by create_time desc, id desc
        limit 80
        """,
        (SPECIAL_CLINIC_ZONE_ID, *compensation_params),
    )
    for row in rows:
        row["item_name"] = SPECIAL_CLINIC_ITEM_NAMES.get(int(row["item_id"]), f"道具 {row['item_id']}")
    return rows


def load_special_clinic_resource_rewards(conn, week_start=None):
    where_clause, params = special_clinic_cycle_filter("clinic_date", week_start)
    return query_list(
        conn,
        f"""
        select to_char(date_trunc('hour', create_time at time zone 'UTC' at time zone %s), 'MM-DD HH24:00') as label,
               coalesce(sum(temporary_patients), 0) as temporary_patients,
               coalesce(sum(ingot_reward), 0) as ingot_reward,
               coalesce(sum(money_reward), 0) as money_reward,
               coalesce(sum(prestige_reward), 0) as prestige_reward,
               coalesce(sum(glory_reward), 0) as glory_reward
        from t_special_clinic_patient_record
        where {where_clause}
        group by label
        order by min(create_time)
        """,
        (SPECIAL_CLINIC_ZONE_ID, *params),
    )


def load_special_clinic_ticket_flows(conn, week_start=None):
    where_clause, params = special_clinic_cycle_filter("clinic_date", week_start)
    return query_list(
        conn,
        f"""
        select to_char(date_trunc('hour', create_time at time zone 'UTC' at time zone %s), 'MM-DD HH24:00') as label,
               'ticket_log' as source,
               change_type,
               reason,
               coalesce(sum(appointment_delta), 0) as appointment_delta_sum,
               coalesce(sum(gifted_delta), 0) as gifted_delta_sum,
               coalesce(sum(paid_delta), 0) as paid_delta_sum,
               coalesce(sum(ingot_cost), 0) as ingot_cost_sum,
               count(*) as row_count,
               count(distinct hospital_id) as hospital_count
        from t_special_clinic_ticket_log
        where {where_clause}
        group by label, change_type, reason
        order by min(create_time) desc, row_count desc
        limit 80
        """,
        (SPECIAL_CLINIC_ZONE_ID, *params),
    )


def load_special_clinic_weekly_cabinet(conn):
    depleted_at_select, depleted_at_params = special_clinic_depleted_at_select(
        column_exists(conn, "t_special_clinic_cabinet", "depleted_at")
    )
    cycle_start_expr = special_clinic_cycle_start_expr("clinic_date")
    rows = query_list(
        conn,
        f"""
        with cabinet_recent as (
            select *,
                   {cycle_start_expr} as clinic_week_start
            from t_special_clinic_cabinet
            where clinic_date >= ((now() at time zone %s)::date - 55)
        ), cabinet_ranked as (
            select *,
                   row_number() over (
                       partition by clinic_week_start
                       order by case when clinic_date = clinic_week_start then 0 else 1 end,
                                clinic_date desc,
                                id desc
                   ) as cabinet_rank
            from cabinet_recent
        ), canonical_cabinet as (
            select *
            from cabinet_ranked
            where cabinet_rank = 1
        ), cabinet_aggregate as (
            select clinic_week_start,
                   coalesce(sum(total_diagnoses), 0) as total_diagnoses,
                   coalesce(sum(paid_ticket_count), 0) as paid_ticket_count,
                   coalesce(sum(empty_attempt_count), 0) as empty_attempt_count,
                   {depleted_at_select},
                   coalesce(sum(critical_admitted_count), 0) as critical_admitted_count
            from cabinet_ranked c
            group by clinic_week_start
        ), cabinet_weekly as (
            select c.clinic_week_start,
                   coalesce(c.status::text, '') as status,
                   coalesce(c.initial_total, 0) as initial_total,
                   greatest(coalesce(c.initial_total, 0) - coalesce(c.replenished_total, 0), 0) as base_initial_total,
                   coalesce(c.remaining_total, 0) as cabinet_remaining_total,
                   coalesce(c.remaining_total, 0) as remaining_total,
                   coalesce(c.total_diagnoses, 0) as total_diagnoses,
                   coalesce(c.paid_ticket_count, 0) as paid_ticket_count,
                   coalesce(c.empty_attempt_count, 0) as empty_attempt_count,
                   coalesce(a.depleted_at, '') as depleted_at,
                   coalesce(c.critical_admitted_count, 0) as critical_admitted_count,
                   coalesce(a.total_diagnoses, 0) as weekly_cabinet_diagnoses,
                   greatest(coalesce(a.total_diagnoses, 0) - coalesce(c.total_diagnoses, 0), 0) as non_canonical_cabinet_diagnoses,
                   coalesce(c.consultation_round, 0) as consultation_round,
                   coalesce(c.consultation_heat, 0) as consultation_heat,
                   coalesce(c.consultation_threshold, 0) as consultation_threshold,
                   coalesce(c.remaining_by_tier::text, '{{}}') as remaining_by_tier,
                   coalesce(c.replenishment_policy_version, '') as replenishment_policy_version,
                   coalesce(c.prescription_page_policy_version, '') as prescription_page_policy_version,
                   coalesce(c.prescription_page_budget_total, 0) as prescription_page_budget_total,
                   coalesce(c.prescription_page_awarded_total, 0) as prescription_page_awarded_total,
                   coalesce(c.prescription_page_budget_by_tier::text, '{{}}') as prescription_page_budget_by_tier,
                   coalesce(c.prescription_page_awarded_by_tier::text, '{{}}') as prescription_page_awarded_by_tier,
                   coalesce(c.replenished_total, 0) as replenished_total,
                   coalesce(c.replenished_equivalent_cost, 0) as replenished_equivalent_cost,
                   coalesce(c.last_replenish_hour_key, '') as last_replenish_hour_key,
                   coalesce(c.replenished_by_tier::text, '{{}}') as replenished_by_tier
            from canonical_cabinet c
            left join cabinet_aggregate a on a.clinic_week_start = c.clinic_week_start
        ), policy_weekly as (
            select *,
                   greatest(((now() at time zone %s)::date - clinic_week_start) + 1, 0) as cycle_day,
                   case greatest(((now() at time zone %s)::date - clinic_week_start) + 1, 0)
                       when 1 then 0.35
                       when 2 then 0.28
                       when 3 then 0.21
                       when 4 then 0.15
                       when 5 then 0.09
                       when 6 then 0.04
                       else 0
                   end as reserve_rate,
                   case greatest(((now() at time zone %s)::date - clinic_week_start) + 1, 0)
                       when 1 then 0.20
                       when 2 then 0.15
                       when 3 then 0.12
                       when 4 then 0.08
                       when 5 then 0.05
                       when 6 then 0.03
                       else 0
                   end as max_replenishment_rate
            from cabinet_weekly
        ), recent_two_hours as (
            select clinic_date as clinic_week_start,
                   count(*) as recent_2h_diagnoses
            from t_special_clinic_patient_record
            where create_time >= (now() - interval '2 hours')
            group by clinic_date
        ), record_weekly as (
            select {cycle_start_expr} as clinic_week_start,
                   count(*) as diagnosis_count_from_record
            from t_special_clinic_patient_record
            where clinic_date >= ((now() at time zone %s)::date - 55)
            group by clinic_week_start
        )
        select c.clinic_week_start::text as clinic_date,
               coalesce(c.status, '') as status,
               coalesce(c.initial_total, 0) as initial_total,
               coalesce(c.base_initial_total, 0) as base_initial_total,
               coalesce(c.remaining_total, 0) as remaining_total,
               coalesce(c.cabinet_remaining_total, 0) as cabinet_remaining_total,
               coalesce(c.total_diagnoses, 0) as total_diagnoses,
               coalesce(r.diagnosis_count_from_record, 0) as diagnosis_count_from_record,
               coalesce(c.paid_ticket_count, 0) as paid_ticket_count,
               coalesce(c.empty_attempt_count, 0) as empty_attempt_count,
               coalesce(c.depleted_at, '') as depleted_at,
               coalesce(c.critical_admitted_count, 0) as critical_admitted_count,
               coalesce(c.weekly_cabinet_diagnoses, 0) as weekly_cabinet_diagnoses,
               coalesce(c.non_canonical_cabinet_diagnoses, 0) as non_canonical_cabinet_diagnoses,
               coalesce(c.consultation_round, 0) as consultation_round,
               coalesce(c.consultation_heat, 0) as consultation_heat,
               coalesce(c.consultation_threshold, 0) as consultation_threshold,
               coalesce(c.remaining_by_tier, '{{}}') as remaining_by_tier,
               coalesce(c.replenishment_policy_version, '') as replenishment_policy_version,
               coalesce(c.prescription_page_policy_version, '') as prescription_page_policy_version,
               coalesce(c.prescription_page_budget_total, 0) as prescription_page_budget_total,
               coalesce(c.prescription_page_awarded_total, 0) as prescription_page_awarded_total,
               coalesce(c.prescription_page_budget_by_tier, '{{}}') as prescription_page_budget_by_tier,
               coalesce(c.prescription_page_awarded_by_tier, '{{}}') as prescription_page_awarded_by_tier,
               coalesce(c.replenished_total, 0) as replenished_total,
               coalesce(c.replenished_equivalent_cost, 0) as replenished_equivalent_cost,
               coalesce(c.last_replenish_hour_key, '') as last_replenish_hour_key,
               coalesce(c.replenished_by_tier, '{{}}') as replenished_by_tier,
               coalesce(c.cycle_day, 0) as cycle_day,
               coalesce(c.reserve_rate, 0) as reserve_rate,
               coalesce(c.max_replenishment_rate, 0) as max_replenishment_rate,
               ceil(greatest(c.base_initial_total, 1) * c.reserve_rate)::int as reserve_line,
               ceil(greatest(c.base_initial_total, 1) * c.max_replenishment_rate)::int as max_replenishment,
               greatest(ceil(greatest(c.base_initial_total, 1) * c.max_replenishment_rate)::int - coalesce(c.replenished_total, 0), 0) as remaining_replenishment_cap,
               coalesce(recent.recent_2h_diagnoses, 0) as recent_2h_diagnoses,
               ceil(coalesce(recent.recent_2h_diagnoses, 0) / 2.0 * 4)::int as forecast_4h_diagnoses,
               coalesce(c.cabinet_remaining_total, 0) - ceil(coalesce(recent.recent_2h_diagnoses, 0) / 2.0 * 4)::int as projected_remaining,
               ceil(greatest(c.base_initial_total, 1) * c.reserve_rate)::int
                   - (coalesce(c.cabinet_remaining_total, 0) - ceil(coalesce(recent.recent_2h_diagnoses, 0) / 2.0 * 4)::int) as replenishment_need,
               greatest(
                   least(
                       ceil(greatest(c.base_initial_total, 1) * c.reserve_rate)::int
                           - (coalesce(c.cabinet_remaining_total, 0) - ceil(coalesce(recent.recent_2h_diagnoses, 0) / 2.0 * 4)::int),
                       greatest(ceil(greatest(c.base_initial_total, 1) * c.max_replenishment_rate)::int - coalesce(c.replenished_total, 0), 0)
                   ),
                   0
               ) as estimated_replenishment_now
        from policy_weekly c
        left join record_weekly r on r.clinic_week_start = c.clinic_week_start
        left join recent_two_hours recent on recent.clinic_week_start = c.clinic_week_start
        order by c.clinic_week_start desc
        limit 8
        """,
        (
            SPECIAL_CLINIC_ZONE_ID,
            *depleted_at_params,
            SPECIAL_CLINIC_ZONE_ID,
            SPECIAL_CLINIC_ZONE_ID,
            SPECIAL_CLINIC_ZONE_ID,
            SPECIAL_CLINIC_ZONE_ID,
        ),
    )
    for row in rows:
        add_special_clinic_supply_metrics(row)
    return rows


def load_special_clinic_hospital_daily(conn, week_start=None):
    patient_where, patient_params = special_clinic_cycle_filter("r.clinic_date", week_start)
    ticket_where, ticket_params = special_clinic_cycle_filter("clinic_date", week_start)
    return query_list(
        conn,
        f"""
        with reward_item_summary as (
            select r.hospital_id, r.clinic_date, coalesce(sum(e.value::int), 0) as reward_item_count
            from t_special_clinic_patient_record r
            cross join lateral jsonb_each_text(coalesce(r.reward_items, '{{}}'::jsonb)) e
            where {patient_where}
            group by r.hospital_id, r.clinic_date
        ), ticket_purchase as (
            select hospital_id, clinic_date,
                   coalesce(sum(paid_delta) filter (where paid_delta > 0 and change_type = 'PURCHASE'), 0) as ticket_purchase_count,
                   coalesce(sum(ingot_cost) filter (where change_type = 'PURCHASE'), 0) as ingot_cost
            from t_special_clinic_ticket_log
            where {ticket_where}
            group by hospital_id, clinic_date
        )
        select r.hospital_id,
               coalesce(h.hospital_name, '') as hospital_name,
               coalesce(h.director_name, '') as director_name,
               r.clinic_date::text as clinic_date,
               count(*) as diagnosis_count,
               count(*) filter (where r.ticket_type_used = 'PAID') as paid_diagnosis_count,
               coalesce(max(t.ticket_purchase_count), 0) as ticket_purchase_count,
               coalesce(max(t.ingot_cost), 0) as ingot_cost,
               coalesce(max(i.reward_item_count), 0) as reward_item_count,
               coalesce(sum(r.temporary_patients), 0) as temporary_patients,
               coalesce(sum(r.ingot_reward + r.money_reward + r.prestige_reward + r.glory_reward), 0) as resource_reward_total,
               to_char((min(r.create_time) at time zone 'UTC' at time zone %s), 'MM-DD HH24:MI') as first_diagnosis_time,
               to_char((max(r.create_time) at time zone 'UTC' at time zone %s), 'MM-DD HH24:MI') as last_diagnosis_time
        from t_special_clinic_patient_record r
        left join t_hospitals h on h.id = r.hospital_id
        left join reward_item_summary i on i.hospital_id = r.hospital_id and i.clinic_date = r.clinic_date
        left join ticket_purchase t on t.hospital_id = r.hospital_id and t.clinic_date = r.clinic_date
        where {patient_where}
        group by r.hospital_id, h.hospital_name, h.director_name, r.clinic_date
        order by diagnosis_count desc, paid_diagnosis_count desc, ingot_cost desc, r.hospital_id
        limit 30
        """,
        (
            *patient_params,
            *ticket_params,
            SPECIAL_CLINIC_ZONE_ID,
            SPECIAL_CLINIC_ZONE_ID,
            *patient_params,
        ),
    )


def load_special_clinic_audit_checks(conn, week_start=None):
    patient_where, patient_params = special_clinic_cycle_filter("clinic_date", week_start)
    ticket_where, ticket_params = special_clinic_cycle_filter("clinic_date", week_start)
    yuanbao_where, yuanbao_params = special_clinic_time_filter("create_time", week_start)
    prompt_where, prompt_params = special_clinic_time_filter("create_time", week_start)
    row = query_one(
        conn,
        f"""
        with patient as (
            select count(*) as diagnosis_count,
                   coalesce(sum((reward_items ->> '1792')::int), 0) as reward_ticket_from_records,
                   coalesce(sum(ingot_reward), 0) as ingot_reward_from_records
            from t_special_clinic_patient_record
            where {patient_where}
        ), ticket as (
            select count(*) filter (where change_type = 'CONSUME') as ticket_consume_rows,
                   coalesce(sum(appointment_delta) filter (where reason = '特需诊断药方奖励门诊票'), 0) as reward_ticket_from_logs,
                   coalesce(sum(ingot_cost) filter (where change_type = 'PURCHASE'), 0) as ingot_cost_from_ticket_log
            from t_special_clinic_ticket_log
            where {ticket_where}
        ), yuanbao as (
            select coalesce(sum(greatest(old_value - new_value, 0)) filter (where reason like '特需门诊元宝补诊%%'), 0) as ingot_cost_from_yuanbao_log,
                   coalesce(sum(greatest(new_value - old_value, 0)) filter (where reason = '特需门诊确诊奖励'), 0) as ingot_reward_from_yuanbao_log
            from t_log_yuanbao
            where {yuanbao_where}
        ), balance_diff as (
            select count(*) as mismatch_count
            from t_special_clinic_player_state s
            left join t_backpack b on b.hospital_id = s.hospital_id
            where coalesce(s.appointment_ticket_balance, 0) + coalesce(s.gifted_ticket_balance, 0) + coalesce(s.paid_ticket_balance, 0)
                <> coalesce((b.items ->> '1792')::int, 0)
        ), prompt as (
            select
                count(*) filter (where content like '%%特需门诊票不足%%' or content like '%%门诊票不足%%') as no_ticket_count,
                count(*) filter (where content like '%%今日患者已经看完%%' or content like '%%今日门诊已满%%') as empty_count,
                count(*) filter (where content like '%%今日门诊已经结诊%%' or content like '%%今日门诊已结诊%%') as closed_count,
                count(*) filter (where content like '%%今日元宝购票已达10张上限%%') as purchase_limit_count,
                count(*) filter (where content like '%%该补诊包暂未开放购买%%') as unopened_package_count,
                count(*) filter (where content like '%%元宝不足%%') as insufficient_ingot_count
            from t_log_right_bottom
            where {prompt_where}
        )
        select *
        from patient, ticket, yuanbao, balance_diff, prompt
        """,
        (*patient_params, *ticket_params, *yuanbao_params, *prompt_params),
    )
    checks = [
        {
            "name": "看病数对票消耗",
            "left": row.get("diagnosis_count", 0),
            "right": row.get("ticket_consume_rows", 0),
            "diff": int(row.get("diagnosis_count", 0) or 0) - int(row.get("ticket_consume_rows", 0) or 0),
        },
        {
            "name": "返票记录对流水",
            "left": row.get("reward_ticket_from_records", 0),
            "right": row.get("reward_ticket_from_logs", 0),
            "diff": int(row.get("reward_ticket_from_records", 0) or 0) - int(row.get("reward_ticket_from_logs", 0) or 0),
        },
        {
            "name": "购票元宝成本对日志",
            "left": row.get("ingot_cost_from_ticket_log", 0),
            "right": row.get("ingot_cost_from_yuanbao_log", 0),
            "diff": int(row.get("ingot_cost_from_ticket_log", 0) or 0) - int(row.get("ingot_cost_from_yuanbao_log", 0) or 0),
        },
        {
            "name": "确诊元宝奖励对日志",
            "left": row.get("ingot_reward_from_records", 0),
            "right": row.get("ingot_reward_from_yuanbao_log", 0),
            "diff": int(row.get("ingot_reward_from_records", 0) or 0) - int(row.get("ingot_reward_from_yuanbao_log", 0) or 0),
        },
        {
            "name": "当前票余额不一致医院",
            "left": row.get("mismatch_count", 0),
            "right": 0,
            "diff": int(row.get("mismatch_count", 0) or 0),
        },
    ]
    alerts = [
        {"name": "无票尝试看病", "count": row.get("no_ticket_count", 0)},
        {"name": "库存耗尽提示", "count": row.get("empty_count", 0)},
        {"name": "结诊后尝试", "count": row.get("closed_count", 0)},
        {"name": "购票触达上限", "count": row.get("purchase_limit_count", 0)},
        {"name": "未开放包直调", "count": row.get("unopened_package_count", 0)},
        {"name": "元宝不足", "count": row.get("insufficient_ingot_count", 0)},
    ]
    return {"checks": checks, "alerts": alerts}


def load_stats():
    stats = load_stats_from_prod()
    record_snapshot(stats)
    return merge_snapshot_history(stats)


def load_unavailable_stats(error: Exception):
    summary = {
        "skin_owner_accounts": 0,
        "skin_owner_hospitals": 0,
        "skin_equipped_accounts": 0,
        "green_combo_equipped_accounts": 0,
        "skin_free_accounts": 0,
        "skin_purchase_log_accounts": 0,
        "skin_paid_confirmed_accounts": 0,
        "online_now_accounts": 0,
        "active_today_accounts": 0,
        "registrations_today": 0,
        "recharge_cny_today": 0,
        "recharge_yuanbao_today": 0,
        "recharge_orders_today": 0,
        "stripe_recharge_cny_today": 0,
        "stripe_recharge_yuanbao_today": 0,
        "stripe_recharge_orders_today": 0,
        "steam_recharge_cny_today": 0,
        "steam_recharge_yuanbao_today": 0,
        "steam_recharge_orders_today": 0,
    }
    return {
        "generatedAt": now_in_zone().isoformat(),
        "zoneId": ZONE_ID,
        "note": f"数据源暂不可用：{error}",
        "summary": summary,
        "onlineBuckets": [],
        "dailyRecharge": [],
        "dailyActive": [],
        "dailyRegistrations": [],
        "hourlyYuanbaoSpending": [],
        "weeklyYuanbaoSpending": [],
        "weeklyYuanbaoPurchases": [],
        "itemPurchases": [],
        "itemUsages": [],
    }


def percent(numerator, denominator):
    if not denominator:
        return 0
    return round(float(numerator or 0) * 100 / float(denominator or 0), 2)


def add_broker_rates(row):
    wallet_count = row.get("wallet_count", 0) or 0
    row["wallet_open_rate"] = percent(row.get("wallet_opened", row.get("opened_count", 0)), wallet_count)
    row["item_drop_rate"] = percent(row.get("item_drop_wallets", 0), wallet_count)
    if wallet_count:
        row["avg_money_per_wallet"] = round(float(row.get("wallet_money", row.get("money_reward", 0)) or 0) / float(wallet_count), 2)
    else:
        row["avg_money_per_wallet"] = 0
    return row


def load_broker_stats_from_prod():
    with prod_connection() as conn:
        summary = query_one(
            conn,
            """
            with cutoff as (
                select coalesce(max(update_time), now() at time zone 'UTC') as cutoff_at
                from t_broker_wallet_rule
                where enabled = true
            ),
            ordinary_success as (
                select r.create_time,
                       coalesce(nullif(substring(r.content from '拉走了([0-9]+)位病人'), ''), '0')::int as patients
                from t_log_right_bottom r, cutoff c
                where r.create_time >= now() - interval '14 days'
                  and r.create_time >= c.cutoff_at
                  and r.content like '【%%】派遣医托从您的医院拉走了%%位病人%%'
            ),
            retaliation_click as (
                select r.create_time
                from t_log_right_bottom r, cutoff c
                where r.create_time >= now() - interval '14 days'
                  and r.create_time >= c.cutoff_at
                  and r.content like '您按名片找到了对方医托，准备反拉一次。%%'
            ),
            retaliation_success as (
                select r.create_time,
                       coalesce(nullif(substring(r.content from '反拉走了([0-9]+)位病人'), ''), '0')::int as patients
                from t_log_right_bottom r, cutoff c
                where r.create_time >= now() - interval '14 days'
                  and r.create_time >= c.cutoff_at
                  and r.content like '【%%】顺着医托名片找了回来，从您的医院反拉走了%%位病人%%'
            ),
            retaliation_used as (
                select used_at as create_time
                from t_broker_retaliation_voucher, cutoff c
                where used_at is not null
                  and used_at >= now() - interval '14 days'
                  and used_at >= c.cutoff_at
            ),
            wallet_enriched as (
                select w.*,
                       exists(
                           select 1
                           from jsonb_each_text(coalesce(w.item_rewards, '{}'::jsonb)) item
                           where item.value::int > 0
                       ) as has_item_drop,
                       coalesce((
                           select sum(item.value::int)
                           from jsonb_each_text(coalesce(w.item_rewards, '{}'::jsonb)) item
                           where item.value::int > 0
                       ), 0) as item_drop_quantity
                from t_broker_wallet_drop w, cutoff c
                where w.create_time >= now() - interval '14 days'
                  and w.create_time >= c.cutoff_at
            )
            select
                (select to_char(cutoff_at at time zone 'UTC' at time zone %s, 'YYYY-MM-DD HH24:MI:SS') from cutoff) as cutoff_label,
                (select count(*) from ordinary_success) as ordinary_success_count,
                (select coalesce(sum(patients), 0) from ordinary_success) as ordinary_patient_count,
                (select count(*) from wallet_enriched) as wallet_count,
                (select count(*) from wallet_enriched where opened_at is not null) as wallet_opened,
                (select coalesce(sum(money_reward), 0) from wallet_enriched) as wallet_money,
                (select coalesce(sum(money_reward), 0) from wallet_enriched where opened_at is not null) as opened_money,
                (select count(*) from wallet_enriched where has_item_drop) as item_drop_wallets,
                (select coalesce(sum(item_drop_quantity), 0) from wallet_enriched) as item_drop_quantity,
                (select count(*) from retaliation_click) as retaliation_click_count,
                (select count(*) from retaliation_used) as retaliation_success_count,
                (select coalesce(sum(patients), 0) from retaliation_success) as retaliation_patient_count
            """,
            (ZONE_ID,),
        )
        add_broker_rates(summary)
        stage_comparison = [{**summary, "stage_label": "钱包上线后"}]
        relation_band = query_list(
            conn,
            """
            with cutoff as (
                select coalesce(max(update_time), now() at time zone 'UTC') as cutoff_at
                from t_broker_wallet_rule
                where enabled = true
            ),
            wallet_enriched as (
                select w.*,
                       exists(
                           select 1
                           from jsonb_each_text(coalesce(w.item_rewards, '{}'::jsonb)) item
                           where item.value::int > 0
                       ) as has_item_drop
                from t_broker_wallet_drop w, cutoff c
                where w.create_time >= now() - interval '14 days'
                  and w.create_time >= c.cutoff_at
            )
            select relation_type,
                   case
                       when stolen_patient_count between 1 and 39 then '1-39'
                       when stolen_patient_count between 40 and 69 then '40-69'
                       else '70+'
                   end as patient_band,
                   count(*) as wallet_count,
                   coalesce(sum(stolen_patient_count), 0) as ordinary_patient_count,
                   coalesce(sum(money_reward), 0) as wallet_money,
                   count(*) filter (where opened_at is not null) as wallet_opened,
                   count(*) filter (where has_item_drop) as item_drop_wallets
            from wallet_enriched
            group by relation_type, patient_band
            order by relation_type, patient_band
            """,
        )
        for row in relation_band:
            add_broker_rates(row)
        daily_trend = query_list(
            conn,
            f"""
            with cutoff as (
                select coalesce(max(update_time), now() at time zone 'UTC') as cutoff_at
                from t_broker_wallet_rule
                where enabled = true
            ),
            days as (
                select generate_series(
                    greatest(
                        (select (cutoff_at at time zone 'UTC' at time zone %s)::date from cutoff),
                        (now() at time zone %s)::date - 13
                    ),
                    (now() at time zone %s)::date,
                    interval '1 day'
                )::date as day
            ),
            ordinary_success as (
                select (create_time at time zone 'UTC' at time zone %s)::date as day,
                       coalesce(nullif(substring(content from '拉走了([0-9]+)位病人'), ''), '0')::int as patients
                from t_log_right_bottom, cutoff c
                where create_time >= now() - interval '14 days'
                  and create_time >= c.cutoff_at
                  and content like '【%%】派遣医托从您的医院拉走了%%位病人%%'
            ),
            retaliation_success as (
                select (create_time at time zone 'UTC' at time zone %s)::date as day
                from t_log_right_bottom, cutoff c
                where create_time >= now() - interval '14 days'
                  and create_time >= c.cutoff_at
                  and content like '【%%】顺着医托名片找了回来，从您的医院反拉走了%%位病人%%'
            ),
            retaliation_used as (
                select (used_at at time zone 'UTC' at time zone %s)::date as day
                from t_broker_retaliation_voucher, cutoff c
                where used_at is not null
                  and used_at >= now() - interval '14 days'
                  and used_at >= c.cutoff_at
            ),
            wallet_enriched as (
                select (create_time at time zone 'UTC' at time zone %s)::date as day,
                       money_reward,
                       opened_at
                from t_broker_wallet_drop, cutoff c
                where create_time >= now() - interval '14 days'
                  and create_time >= c.cutoff_at
            )
            select d.day::text as day,
                   coalesce((select count(*) from ordinary_success o where o.day = d.day), 0) as ordinary_success_count,
                   coalesce((select sum(patients) from ordinary_success o where o.day = d.day), 0) as ordinary_patient_count,
                   coalesce((select count(*) from wallet_enriched w where w.day = d.day), 0) as wallet_count,
                   coalesce((select count(*) from wallet_enriched w where w.day = d.day and w.opened_at is not null), 0) as wallet_opened,
                   coalesce((select sum(money_reward) from wallet_enriched w where w.day = d.day), 0) as wallet_money,
                   coalesce((select count(*) from retaliation_used r where r.day = d.day), 0) as retaliation_success_count
            from days d
            order by d.day
            """,
            (ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID),
        )
        current_rules = query_list(
            conn,
            """
            select relation_type,
                   case
                       when min_patients = 1 and max_patients = 39 then '1-39'
                       when min_patients = 40 and max_patients = 69 then '40-69'
                       when min_patients = 70 and max_patients = 0 then '70+'
                       else concat(min_patients, '-', max_patients)
                   end as patient_band,
                   money_per_patient,
                   min_money,
                   max_money,
                   round((item_chance * 100)::numeric, 2) as item_chance_percent,
                   fallback_money_per_patient,
                   retaliation_seconds,
                   to_char(update_time at time zone 'UTC' at time zone %s, 'YYYY-MM-DD HH24:MI:SS') as update_label
            from t_broker_wallet_rule
            where enabled = true
            order by relation_type, min_patients
            """,
            (ZONE_ID,),
        )
    return {
        "generatedAt": now_in_zone().isoformat(),
        "zoneId": ZONE_ID,
        "note": "统计窗口从医托钱包规则上线时间开始；普通拉人使用目标医院日志口径，反拉使用名片点击日志和反拉成功日志口径；钱包指标只归因普通成功拉人。",
        "summary": summary,
        "stageComparison": stage_comparison,
        "relationBand": relation_band,
        "dailyTrend": daily_trend,
        "currentRules": current_rules,
    }


def load_toilet_market_stats_from_prod():
    stale_interval = f"{TOILET_MARKET_STALE_HOURS} hours"
    with prod_connection() as conn:
        has_listing_source = column_exists(conn, "t_toilet_market_listing", "listing_source")
        player_listing_filter = "listing_source = 'PLAYER'" if has_listing_source else "true"
        admin_listing_filter = "listing_source = 'ADMIN'" if has_listing_source else "false"
        player_join_filter = "l.listing_source = 'PLAYER'" if has_listing_source else "true"
        summary = query_one(
            conn,
            f"""
            with active_listings as (
                select *
                from t_toilet_market_listing
                where status = 'ACTIVE'
            ), purchase_tx as (
                select *
                from t_toilet_market_transaction
                where transaction_type = 'PURCHASE'
                  and create_time >= now() - interval '14 days'
            ), street_pickup_tx as (
                select *
                from t_toilet_market_transaction
                where transaction_type = 'STREET_PICKUP'
                  and street_item_id is not null
                  and create_time >= now() - interval '14 days'
            ), sold_latency as (
                select extract(epoch from (min(t.create_time) - l.create_time)) / 60.0 as sale_minutes
                from t_toilet_market_listing l
                join t_toilet_market_transaction t on t.listing_id = l.id and t.transaction_type = 'PURCHASE'
                where t.create_time >= now() - interval '14 days'
                group by l.id, l.create_time
            )
            select
                (select count(*) from active_listings) as active_listing_count,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from active_listings) as active_quantity,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from active_listings where content_type = 'ITEM') as active_item_quantity,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from active_listings where content_type = 'MONEY') as active_money_quantity,
                (select count(distinct seller_hospital_id) from active_listings where {player_listing_filter}) as active_seller_count,
                (select count(*) from active_listings where {admin_listing_filter}) as admin_active_listing_count,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from active_listings where {admin_listing_filter}) as admin_active_quantity,
                (select count(*) from active_listings where create_time < now() - %s::interval) as stale_listing_count,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from active_listings where create_time < now() - %s::interval) as stale_quantity,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from active_listings where content_type = 'ITEM' and create_time < now() - %s::interval) as stale_item_quantity,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from active_listings where content_type = 'MONEY' and create_time < now() - %s::interval) as stale_money_quantity,
                (select count(*) from purchase_tx) as purchase_count,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from purchase_tx) as purchased_quantity,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from purchase_tx where content_type = 'ITEM') as purchased_item_quantity,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from purchase_tx where content_type = 'MONEY') as purchased_money_quantity,
                (select count(distinct actor_hospital_id) from purchase_tx) as buyer_count,
                (select count(distinct seller_hospital_id)
                 from t_toilet_market_listing l
                 join purchase_tx t on t.listing_id = l.id
                 where {player_join_filter}) as seller_count,
                (select count(*) from street_pickup_tx) as street_pickup_count,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from street_pickup_tx) as street_pickup_quantity,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from street_pickup_tx where content_type = 'ITEM') as street_pickup_item_quantity,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from street_pickup_tx where content_type = 'MONEY') as street_pickup_money_quantity,
                (select count(*) from t_toilet_street_item where status = 'AVAILABLE') as street_available_count,
                (select coalesce(sum(coalesce(quantity, 1)), 0) from t_toilet_street_item where status = 'AVAILABLE') as street_available_quantity,
                (select round(avg(sale_minutes)::numeric, 2) from sold_latency) as avg_sale_minutes,
                (select round(percentile_cont(0.5) within group (order by sale_minutes)::numeric, 2) from sold_latency) as median_sale_minutes
            """,
            (stale_interval, stale_interval, stale_interval, stale_interval),
        )
        summary["stale_rate"] = percent(summary.get("stale_listing_count", 0), summary.get("active_listing_count", 0))
        summary["pickup_to_purchase_rate"] = percent(summary.get("street_pickup_count", 0), summary.get("purchase_count", 0))

        daily_trend = query_list(
            conn,
            f"""
            with days as (
                select generate_series(
                    (now() at time zone %s)::date - 13,
                    (now() at time zone %s)::date,
                    interval '1 day'
                )::date as day
            ), listing_create as (
                select (create_time at time zone 'UTC' at time zone %s)::date as day,
                       count(*) as listing_count,
                       coalesce(sum(coalesce(quantity, 1)), 0) as listing_quantity,
                       count(distinct seller_hospital_id) filter (where {player_listing_filter}) as seller_count
                from t_toilet_market_listing
                where create_time >= now() - interval '14 days'
                group by 1
            ), purchase_tx as (
                select (t.create_time at time zone 'UTC' at time zone %s)::date as day,
                       count(*) as purchase_count,
                       coalesce(sum(coalesce(t.quantity, 1)), 0) as purchased_quantity,
                       count(distinct t.actor_hospital_id) as buyer_count,
                       count(distinct l.seller_hospital_id) filter (where {player_join_filter}) as seller_count
                from t_toilet_market_transaction t
                left join t_toilet_market_listing l on l.id = t.listing_id
                where t.transaction_type = 'PURCHASE'
                  and t.create_time >= now() - interval '14 days'
                group by 1
            ), street_pickup as (
                select (create_time at time zone 'UTC' at time zone %s)::date as day,
                       count(*) as pickup_count,
                       coalesce(sum(coalesce(quantity, 1)), 0) as pickup_quantity
                from t_toilet_market_transaction
                where transaction_type = 'STREET_PICKUP'
                  and street_item_id is not null
                  and create_time >= now() - interval '14 days'
                group by 1
            )
            select d.day::text as day,
                   coalesce(l.listing_count, 0) as listing_count,
                   coalesce(l.listing_quantity, 0) as listing_quantity,
                   coalesce(l.seller_count, 0) as listing_seller_count,
                   coalesce(p.purchase_count, 0) as purchase_count,
                   coalesce(p.purchased_quantity, 0) as purchased_quantity,
                   coalesce(p.buyer_count, 0) as buyer_count,
                   coalesce(p.seller_count, 0) as seller_count,
                   coalesce(s.pickup_count, 0) as pickup_count,
                   coalesce(s.pickup_quantity, 0) as pickup_quantity
            from days d
            left join listing_create l on l.day = d.day
            left join purchase_tx p on p.day = d.day
            left join street_pickup s on s.day = d.day
            order by d.day
            """,
            (ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID),
        )
        item_summary = query_list(
            conn,
            f"""
            with listing_summary as (
                select coalesce(item_id, 0) as item_id,
                       coalesce(item_name, '金钱') as item_name,
                       coalesce(content_type::text, 'ITEM') as content_type,
                       count(*) filter (where status = 'ACTIVE') as active_listing_count,
                       coalesce(sum(coalesce(quantity, 1)) filter (where status = 'ACTIVE'), 0) as active_quantity,
                       count(distinct seller_hospital_id) filter (where status = 'ACTIVE' and {player_listing_filter}) as active_seller_count,
                       count(*) filter (where status = 'ACTIVE' and create_time < now() - %s::interval) as stale_listing_count,
                       coalesce(sum(coalesce(quantity, 1)) filter (where status = 'ACTIVE' and create_time < now() - %s::interval), 0) as stale_quantity
                from t_toilet_market_listing
                group by coalesce(item_id, 0), coalesce(item_name, '金钱'), coalesce(content_type::text, 'ITEM')
            ), purchase_summary as (
                select coalesce(t.item_id, l.item_id, 0) as item_id,
                       coalesce(l.item_name, case when t.content_type = 'MONEY' then '金钱' else concat('道具 ', t.item_id) end) as item_name,
                       coalesce(t.content_type, l.content_type::text, 'ITEM') as content_type,
                       count(*) as purchase_count,
                       coalesce(sum(coalesce(t.quantity, 1)), 0) as purchased_quantity,
                       count(distinct t.actor_hospital_id) as buyer_count,
                       count(distinct l.seller_hospital_id) filter (where {player_join_filter}) as seller_count,
                       coalesce(sum(coalesce(t.price, 0)) filter (where t.currency_type = 'MONEY'), 0) as money_amount,
                       coalesce(sum(coalesce(t.price, 0)) filter (where t.currency_type = 'INGOT'), 0) as ingot_amount
                from t_toilet_market_transaction t
                left join t_toilet_market_listing l on l.id = t.listing_id
                where t.transaction_type = 'PURCHASE'
                  and t.create_time >= now() - interval '14 days'
                group by coalesce(t.item_id, l.item_id, 0),
                         coalesce(l.item_name, case when t.content_type = 'MONEY' then '金钱' else concat('道具 ', t.item_id) end),
                         coalesce(t.content_type, l.content_type::text, 'ITEM')
            ), street_summary as (
                select coalesce(t.item_id, s.item_id, 0) as item_id,
                       coalesce(s.item_name, case when t.content_type = 'MONEY' then '金钱' else concat('道具 ', t.item_id) end) as item_name,
                       coalesce(t.content_type, s.content_type::text, 'ITEM') as content_type,
                       count(*) as pickup_count,
                       coalesce(sum(coalesce(t.quantity, 1)), 0) as pickup_quantity,
                       count(distinct t.actor_hospital_id) as picker_count
                from t_toilet_market_transaction t
                left join t_toilet_street_item s on s.id = t.street_item_id
                where t.transaction_type = 'STREET_PICKUP'
                  and t.street_item_id is not null
                  and t.create_time >= now() - interval '14 days'
                group by coalesce(t.item_id, s.item_id, 0),
                         coalesce(s.item_name, case when t.content_type = 'MONEY' then '金钱' else concat('道具 ', t.item_id) end),
                         coalesce(t.content_type, s.content_type::text, 'ITEM')
            ), combined as (
                select coalesce(l.item_id, p.item_id) as item_id,
                       coalesce(l.item_name, p.item_name) as item_name,
                       coalesce(l.content_type, p.content_type) as content_type,
                       l.active_listing_count, l.active_quantity, l.active_seller_count,
                       l.stale_listing_count, l.stale_quantity,
                       p.purchase_count, p.purchased_quantity, p.buyer_count, p.seller_count,
                       p.money_amount, p.ingot_amount
                from listing_summary l
                full outer join purchase_summary p on p.item_id = l.item_id and p.item_name = l.item_name and p.content_type = l.content_type
            )
            select coalesce(c.item_id, s.item_id) as item_id,
                   coalesce(c.item_name, s.item_name) as item_name,
                   coalesce(c.content_type, s.content_type) as content_type,
                   coalesce(c.active_listing_count, 0) as active_listing_count,
                   coalesce(c.active_quantity, 0) as active_quantity,
                   coalesce(c.active_seller_count, 0) as active_seller_count,
                   coalesce(c.stale_listing_count, 0) as stale_listing_count,
                   coalesce(c.stale_quantity, 0) as stale_quantity,
                   coalesce(c.purchase_count, 0) as purchase_count,
                   coalesce(c.purchased_quantity, 0) as purchased_quantity,
                   coalesce(c.buyer_count, 0) as buyer_count,
                   coalesce(c.seller_count, 0) as seller_count,
                   coalesce(c.money_amount, 0) as money_amount,
                   coalesce(c.ingot_amount, 0) as ingot_amount,
                   coalesce(s.pickup_count, 0) as pickup_count,
                   coalesce(s.pickup_quantity, 0) as pickup_quantity,
                   coalesce(s.picker_count, 0) as picker_count
            from combined c
            full outer join street_summary s on s.item_id = c.item_id and s.item_name = c.item_name and s.content_type = c.content_type
            order by (coalesce(purchased_quantity, 0) + coalesce(pickup_quantity, 0)) desc,
                     active_quantity desc, purchase_count desc, item_name
            limit 30
            """,
            (stale_interval, stale_interval),
        )
        fastest_consumed = query_list(
            conn,
            """
            with first_purchase as (
                select l.id as listing_id,
                       min(t.create_time) as sold_at
                from t_toilet_market_listing l
                join t_toilet_market_transaction t on t.listing_id = l.id and t.transaction_type = 'PURCHASE'
                where t.create_time >= now() - interval '14 days'
                group by l.id
            ), purchase_speed as (
                select l.id as source_id,
                       '成交' as consume_type,
                       coalesce(l.item_name, '金钱') as item_name,
                       coalesce(l.quantity, 1) as quantity,
                       coalesce(l.currency_type::text, '') as currency_type,
                       coalesce(l.price, 0) as price,
                       l.seller_hospital_id as source_hospital_id,
                       l.buyer_hospital_id as target_hospital_id,
                       extract(epoch from (f.sold_at - l.create_time)) as consume_seconds,
                       l.create_time as started_at,
                       f.sold_at as consumed_at
                from first_purchase f
                join t_toilet_market_listing l on l.id = f.listing_id
            ), street_speed as (
                select s.id as source_id,
                       '大街捡取' as consume_type,
                       coalesce(s.item_name, '金钱') as item_name,
                       coalesce(s.quantity, 1) as quantity,
                       '' as currency_type,
                       0::bigint as price,
                       s.source_hospital_id,
                       t.actor_hospital_id as target_hospital_id,
                       extract(epoch from (t.create_time - s.create_time)) as consume_seconds,
                       s.create_time as started_at,
                       t.create_time as consumed_at
                from t_toilet_street_item s
                join t_toilet_market_transaction t on t.street_item_id = s.id and t.transaction_type = 'STREET_PICKUP'
                where t.create_time >= now() - interval '14 days'
            ), consumed as (
                select * from purchase_speed
                union all
                select * from street_speed
            )
            select source_id,
                   consume_type,
                   item_name,
                   quantity,
                   currency_type,
                   price,
                   source_hospital_id,
                   target_hospital_id,
                   round(greatest(consume_seconds, 0)::numeric, 2) as consume_seconds,
                   to_char(started_at at time zone 'UTC' at time zone %s, 'YYYY-MM-DD HH24:MI:SS') as started_at,
                   to_char(consumed_at at time zone 'UTC' at time zone %s, 'YYYY-MM-DD HH24:MI:SS') as consumed_at
            from consumed
            order by consume_seconds asc, consumed_at desc
            limit 20
            """,
            (ZONE_ID, ZONE_ID),
        )
        seller_leaderboard = query_list(
            conn,
            f"""
            select l.seller_hospital_id as hospital_id,
                   coalesce(max(h.hospital_name), '') as hospital_name,
                   coalesce(max(h.director_name), '') as director_name,
                   count(*) as purchase_count,
                   coalesce(sum(coalesce(t.quantity, 1)), 0) as sold_quantity,
                   coalesce(sum(coalesce(t.price, 0)) filter (where t.currency_type = 'MONEY'), 0) as money_amount,
                   coalesce(sum(coalesce(t.price, 0)) filter (where t.currency_type = 'INGOT'), 0) as ingot_amount
            from t_toilet_market_transaction t
            join t_toilet_market_listing l on l.id = t.listing_id
            left join t_hospitals h on h.id = l.seller_hospital_id
            where t.transaction_type = 'PURCHASE'
              and t.create_time >= now() - interval '14 days'
              and {player_join_filter}
            group by l.seller_hospital_id
            order by sold_quantity desc, purchase_count desc, ingot_amount desc, money_amount desc
            limit 20
            """
        )
        buyer_leaderboard = query_list(
            conn,
            """
            select t.actor_hospital_id as hospital_id,
                   coalesce(max(h.hospital_name), '') as hospital_name,
                   coalesce(max(h.director_name), '') as director_name,
                   count(*) as purchase_count,
                   coalesce(sum(coalesce(t.quantity, 1)), 0) as purchased_quantity,
                   coalesce(sum(coalesce(t.price, 0)) filter (where t.currency_type = 'MONEY'), 0) as money_amount,
                   coalesce(sum(coalesce(t.price, 0)) filter (where t.currency_type = 'INGOT'), 0) as ingot_amount
            from t_toilet_market_transaction t
            left join t_hospitals h on h.id = t.actor_hospital_id
            where t.transaction_type = 'PURCHASE'
              and t.create_time >= now() - interval '14 days'
            group by t.actor_hospital_id
            order by purchased_quantity desc, purchase_count desc, ingot_amount desc, money_amount desc
            limit 20
            """
        )
        aging_buckets = query_list(
            conn,
            """
            with active_listings as (
                select case
                           when create_time >= now() - interval '6 hours' then '0-6小时'
                           when create_time >= now() - interval '24 hours' then '6-24小时'
                           when create_time >= now() - interval '48 hours' then '24-48小时'
                           when create_time >= now() - interval '7 days' then '48小时-7天'
                           else '7天以上'
                       end as age_bucket,
                       case
                           when create_time >= now() - interval '6 hours' then 1
                           when create_time >= now() - interval '24 hours' then 2
                           when create_time >= now() - interval '48 hours' then 3
                           when create_time >= now() - interval '7 days' then 4
                           else 5
                       end as sort_order,
                       content_type,
                       quantity
                from t_toilet_market_listing
                where status = 'ACTIVE'
            )
            select age_bucket,
                   count(*) as listing_count,
                   count(*) filter (where content_type = 'ITEM') as item_listing_count,
                   count(*) filter (where content_type = 'MONEY') as money_listing_count,
                   coalesce(sum(coalesce(quantity, 1)) filter (where content_type = 'ITEM'), 0) as item_quantity,
                   coalesce(sum(coalesce(quantity, 1)) filter (where content_type = 'MONEY'), 0) as money_quantity
            from active_listings
            group by age_bucket, sort_order
            order by sort_order
            """
        )
    return {
        "generatedAt": now_in_zone().isoformat(),
        "zoneId": ZONE_ID,
        "note": f"近14日成交与捡取统计；滞销品定义为活跃挂单超过 {TOILET_MARKET_STALE_HOURS} 小时未成交。",
        "summary": summary,
        "dailyTrend": daily_trend,
        "itemSummary": item_summary,
        "fastestConsumed": fastest_consumed,
        "sellerLeaderboard": seller_leaderboard,
        "buyerLeaderboard": buyer_leaderboard,
        "agingBuckets": aging_buckets,
    }


def load_unavailable_toilet_market_stats(error: Exception):
    return {
        "generatedAt": now_in_zone().isoformat(),
        "zoneId": ZONE_ID,
        "sourceError": str(error),
        "note": f"跳蚤市场数据源暂不可用：{error}",
        "summary": {},
        "dailyTrend": [],
        "itemSummary": [],
        "fastestConsumed": [],
        "sellerLeaderboard": [],
        "buyerLeaderboard": [],
        "agingBuckets": [],
    }


def load_unavailable_broker_stats(error: Exception):
    return {
        "generatedAt": now_in_zone().isoformat(),
        "zoneId": ZONE_ID,
        "sourceError": str(error),
        "note": f"医托拉人数据源暂不可用：{error}",
        "summary": {},
        "stageComparison": [],
        "relationBand": [],
        "dailyTrend": [],
        "currentRules": [],
    }


def load_unavailable_special_clinic_stats(error: Exception):
    return {
        "generatedAt": datetime.now(ZoneInfo(SPECIAL_CLINIC_ZONE_ID)).isoformat(),
        "zoneId": SPECIAL_CLINIC_ZONE_ID,
        "sourceError": str(error),
        "weekOptions": [],
        "weeklyPages": [],
        "summary": {},
        "dailySummary": [],
        "hourlySummary": [],
        "tierDistribution": [],
        "patientDistribution": [],
        "rewardItems": [],
        "compensationRewards": [],
        "resourceRewards": [],
        "ticketFlows": [],
        "weeklyCabinet": [],
        "dailyCabinet": [],
        "hospitalDaily": [],
        "auditChecks": {"checks": [], "alerts": []},
    }


def empty_purchase_insights():
    return {
        "item": {"top": [], "buyers": []},
        "background": {"top": [], "buyers": []},
        "skin": {"top": [], "buyers": []},
    }


@app.get("/auth/login")
def login():
    setup_error = auth_setup_error()
    if setup_error:
        return auth_error_response(setup_error, 503)
    next_url = safe_next_url(request.args.get("next", "/"))
    return render_template_string(
        LOGIN_TEMPLATE,
        firebase_config=json.dumps(FIREBASE_WEB_CONFIG, separators=(",", ":")),
        firebase_login_url=prefixed_url_for("firebase_login"),
        next_url_json=json.dumps(next_url),
    )


@app.post("/auth/firebase-login")
def firebase_login():
    setup_error = auth_setup_error()
    if setup_error:
        return auth_error_response(setup_error, 503)
    payload = request.get_json(silent=True) or {}
    try:
        userinfo = verify_firebase_id_token(payload.get("idToken", ""))
    except Exception:
        return jsonify({"error": "invalid Firebase login token"}), 401
    email = str(userinfo.get("email", "")).lower()
    if email not in AUTH_ALLOWED_EMAILS:
        session.clear()
        return jsonify({"error": f"{email or 'unknown account'} is not allowed"}), 403
    session["user"] = {
        "email": email,
        "name": userinfo.get("name", ""),
        "picture": userinfo.get("picture", ""),
        "firebase_uid": userinfo.get("sub", ""),
    }
    return jsonify({"redirect": prefixed_path(safe_next_url(payload.get("next", "/")))})


@app.get("/auth/logout")
def logout():
    session.clear()
    return redirect(prefixed_url_for("login"))


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/api/stats")
def stats_api():
    try:
        if use_stats_api():
            return jsonify(fetch_stats_api("/api/stats"))
        return jsonify(load_stats())
    except Exception as exc:
        app.logger.warning("stats unavailable: %s", exc)
        return jsonify(load_unavailable_stats(exc))


@app.get("/api/source-health")
def source_health_api():
    if use_stats_api():
        try:
            return jsonify(fetch_stats_api("/api/source-health"))
        except Exception as exc:
            return jsonify({"status": "unavailable", "error": str(exc)}), 503
    try:
        with prod_connection() as conn:
            source = query_one(
                conn,
                """
                select pg_is_in_recovery() as in_recovery,
                       case when pg_is_in_recovery()
                            then round(extract(epoch from (now() - pg_last_xact_replay_timestamp()))::numeric, 3)
                            else 0
                       end as replay_delay_seconds
                """,
            )
        return jsonify({"status": "ok", **source})
    except Exception as exc:
        return jsonify({"status": "unavailable", "error": str(exc)}), 503


@app.get("/api/special-clinic-stats")
def special_clinic_stats_api():
    week_start = request.args.get("week", "").strip() or None
    if week_start:
        try:
            datetime.strptime(week_start, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "week must use YYYY-MM-DD"}), 400
    try:
        if use_stats_api():
            params = {"week": week_start} if week_start else None
            return jsonify(fetch_stats_api("/api/special-clinic-stats", params))
        return jsonify(load_special_clinic_stats_from_prod(week_start))
    except Exception as exc:
        app.logger.warning("special clinic stats unavailable: %s", exc)
        return jsonify(load_unavailable_special_clinic_stats(exc))


@app.get("/api/broker-stats")
def broker_stats_api():
    try:
        if use_stats_api():
            return jsonify(fetch_stats_api("/api/broker-stats"))
        return jsonify(load_broker_stats_from_prod())
    except Exception as exc:
        app.logger.warning("broker stats unavailable: %s", exc)
        return jsonify(load_unavailable_broker_stats(exc))


@app.get("/api/toilet-market-stats")
def toilet_market_stats_api():
    try:
        if use_stats_api():
            return jsonify(fetch_stats_api("/api/toilet-market-stats"))
        return jsonify(load_toilet_market_stats_from_prod())
    except Exception as exc:
        app.logger.warning("toilet market stats unavailable: %s", exc)
        return jsonify(load_unavailable_toilet_market_stats(exc))


@app.get("/api/stat-table")
def stat_table_api():
    tab, page, page_size = parse_page_args()
    if use_stats_api():
        try:
            return jsonify(fetch_stats_api("/api/stat-table", {
                "tab": tab,
                "page": page,
                "pageSize": page_size,
            }))
        except Exception as exc:
            return jsonify({"error": f"statistics API unavailable: {exc}"}), 503
    return jsonify(load_stat_table(tab, page, page_size))


@app.get("/api/item-activity-details")
def item_activity_details():
    item_name = request.args.get("itemName", "").strip()
    activity_type = request.args.get("type", "").strip().lower()
    if not item_name:
        return jsonify([])
    if use_stats_api():
        try:
            return jsonify(fetch_stats_api("/api/item-activity-details", {
                "type": activity_type,
                "itemName": item_name,
            }))
        except Exception as exc:
            return jsonify({"error": f"statistics API unavailable: {exc}"}), 503
    with prod_connection() as conn:
        if activity_type == "purchase":
            rows = query_list(
                conn,
                """
                with parsed as (
                    select l.hospital_id, h.hospital_name, h.director_name,
                           (regexp_match(l.reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name,
                           ((regexp_match(l.reason, '^商店购买: (.+) x ([0-9]+)$'))[2])::bigint as quantity,
                           greatest(coalesce(l.old_value, 0) - coalesce(l.new_value, 0), 0) as yuanbao_cost,
                           l.create_time
                    from t_log_yuanbao l
                    left join t_hospitals h on h.id = l.hospital_id
                    where l.reason ~ '^商店购买: .+ x [0-9]+$'
                      and (l.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
                )
                select hospital_id, coalesce(hospital_name, '') as hospital_name, coalesce(director_name, '') as director_name,
                       count(*) as event_count, coalesce(sum(quantity), 0) as quantity, coalesce(sum(yuanbao_cost), 0) as yuanbao,
                       to_char((max(create_time) at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as last_time
                from parsed
                where item_name = %s
                group by hospital_id, hospital_name, director_name
                order by quantity desc, event_count desc, max(create_time) desc, hospital_id desc
                limit 200
                """,
                (ZONE_ID, ZONE_ID, ZONE_ID, item_name),
            )
        elif activity_type == "use":
            rows = query_list(conn, item_usage_sql("detail"), (ZONE_ID, ZONE_ID, ZONE_ID, item_name))
        else:
            rows = []
    return jsonify(rows)


def sampler():
    ensure_snapshot_table()
    while True:
        try:
            load_stats()
        except Exception as exc:
            app.logger.warning("snapshot sample failed: %s", exc)
        time.sleep(600)


if os.getenv("OPS_DASHBOARD_DISABLE_SAMPLER", "").strip().lower() not in {"1", "true", "yes"}:
    threading.Thread(target=sampler, daemon=True).start()
