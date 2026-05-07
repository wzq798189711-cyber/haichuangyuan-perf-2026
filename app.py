"""海创元 2026 绩效管理系统 - Flask 后端

替换原 localStorage 方案：账号、会话、所有填报数据全部走 Replit PostgreSQL。
"""
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, make_response, request, send_from_directory

DATABASE_URL = os.environ["DATABASE_URL"]
SESSION_COOKIE = "hcy_session"
SESSION_DAYS = 7

# 财报默认值（首次启动时注入到 dim_a_actual，仅当对应字段为空时填入）
FIN_REPORT_VER = "2026-04-monthly"
FIN_MONTHLY = {
    "1月": {"营业收入": 4.37, "利润总额": -64.84, "销管研费用": 69.01,
            "成本费用总额": 69.21, "经营活动产生的现金流量净额": -67.62,
            "销售回款": 14.28, "年度实现毛利": 4.23,
            "经营性净现金流利润（净利润+非付现）": -63.98},
    "2月": {"营业收入": 0.00, "利润总额": -59.43, "销管研费用": 54.07,
            "成本费用总额": 59.43, "经营活动产生的现金流量净额": -60.41,
            "销售回款": 0.00, "年度实现毛利": -5.00,
            "经营性净现金流利润（净利润+非付现）": -58.66},
    "3月": {"营业收入": 3.66, "利润总额": -73.50, "销管研费用": 70.86,
            "成本费用总额": 77.34, "经营活动产生的现金流量净额": -78.05,
            "销售回款": 3.70, "年度实现毛利": 1.42,
            "经营性净现金流利润（净利润+非付现）": -63.23},
    "4月": {"营业收入": 0.00, "利润总额": -70.07, "销管研费用": 69.74,
            "成本费用总额": 70.07, "经营活动产生的现金流量净额": -58.88,
            "销售回款": 0.00, "年度实现毛利": -0.13,
            "经营性净现金流利润（净利润+非付现）": -66.74},
}

app = Flask(__name__, static_folder=".", static_url_path="")


def db():
    if "_conn" not in g:
        g._conn = psycopg2.connect(DATABASE_URL)
        g._conn.autocommit = True
    return g._conn


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("_conn", None)
    if conn is not None:
        conn.close()


def query(sql, params=None, one=False):
    with db().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        if cur.description is None:
            return None
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


def execute(sql, params=None):
    with db().cursor() as cur:
        cur.execute(sql, params or ())


# ---------- 会话辅助 ----------
def current_user():
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = query(
        """SELECT a.u, a.name, a.org, a.title, a.role, a.scope, a.can_edit_dim_a
           FROM sessions s JOIN accounts a ON a.u = s.u
           WHERE s.token = %s AND s.expires_at > NOW()""",
        (token,), one=True,
    )
    return row


def _session_payload(row):
    """统一前端字段命名：camelCase；DB 用 snake_case"""
    return {
        "u": row["u"], "name": row["name"], "org": row["org"], "title": row["title"],
        "role": row["role"], "scope": row["scope"],
        "canEditDimA": bool(row["can_edit_dim_a"]),
    }


def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        request.user = u
        return f(*a, **kw)
    return w


# ---------- 状态读取 / 财报默认值注入 ----------
def get_state_blob():
    row = query("SELECT data FROM app_state WHERE id = 1", one=True)
    return row["data"] if row else {}


def ensure_finance_defaults():
    """首次启动 / 财报版本变更时，把月度财报默认值灌入 dimAActual['公司']（仅空白字段）。"""
    blob = get_state_blob()
    if blob.get("_finReportV") == FIN_REPORT_VER:
        return
    blob.setdefault("dimAActual", {})
    blob["dimAActual"].setdefault("公司", {})
    company = blob["dimAActual"]["公司"]
    for mo, metrics in FIN_MONTHLY.items():
        company.setdefault(mo, {})
        for k, v in metrics.items():
            if not company[mo].get(k):
                company[mo][k] = str(v)
    blob["_finReportV"] = FIN_REPORT_VER
    execute(
        "UPDATE app_state SET data = %s::jsonb, updated_by = 'system', updated_at = NOW() WHERE id = 1",
        (json.dumps(blob, ensure_ascii=False),),
    )


# ---------- 路由：HTML 注入 ----------
@app.route("/")
def index():
    """读取 index.html，将 SESSION 与云端 state 直接注入到 <body> 顶部，让前端同步流程不变。"""
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()
    user = current_user()
    ensure_finance_defaults()
    state = get_state_blob() if user else {}
    session_payload = _session_payload(user) if user else None
    # 用 <script type="application/json"> + 客户端 JSON.parse，避免用户填报内容里的
    # </script>、< 等字符把脚本截断造成 XSS。再用 \u003c 双保险转义所有 '<'。
    def _safe_json(v):
        return json.dumps(v, ensure_ascii=False).replace("<", "\\u003c")
    bootstrap = (
        f'<script type="application/json" id="__bootstrap_session__">{_safe_json(session_payload)}</script>'
        f'<script type="application/json" id="__bootstrap_state__">{_safe_json(state)}</script>'
        "<script>(function(){"
        "try{window.__SESSION__=JSON.parse(document.getElementById('__bootstrap_session__').textContent);}catch(e){window.__SESSION__=null;}"
        "try{window.__CLOUD_STATE__=JSON.parse(document.getElementById('__bootstrap_state__').textContent);}catch(e){window.__CLOUD_STATE__={};}"
        "})();</script>"
    )
    html = html.replace("<body>", "<body>\n" + bootstrap, 1)
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


# ---------- 路由：认证 ----------
@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True, silent=True) or {}
    u = (body.get("u") or "").strip()
    p = body.get("p") or ""
    remember = bool(body.get("remember", True))
    if not u or not p:
        return jsonify({"error": "请输入账号和密码"}), 400
    acc = query("SELECT * FROM accounts WHERE u = %s AND p = %s", (u, p), one=True)
    if not acc:
        return jsonify({"error": "账号或密码错误"}), 401
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    execute(
        "INSERT INTO sessions (token, u, expires_at) VALUES (%s, %s, %s)",
        (token, u, expires),
    )
    resp = make_response(jsonify(_session_payload(acc)))
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 24 * 3600 if remember else None,
        httponly=True, samesite="Lax", path="/",
    )
    return resp


@app.route("/api/logout", methods=["POST"])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        execute("DELETE FROM sessions WHERE token = %s", (token,))
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.route("/api/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify(None), 200
    return jsonify(_session_payload(u))


# ---------- 路由：状态 ----------
@app.route("/api/state")
@login_required
def api_state_get():
    ensure_finance_defaults()
    return jsonify(get_state_blob())


@app.route("/api/state", methods=["POST"])
@login_required
def api_state_post():
    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "状态必须为 JSON 对象"}), 400
    existing = get_state_blob()
    # 维度A 写权限校验：服务端始终以"已存在的 dimAActual"为基线，
    # 仅当用户拥有 canEditDimA 时才允许覆盖；否则无论 body 中是缺失、为空还是有变化，
    # 都强制保留服务器原值，防止"漏字段绕过"或"清空覆盖"。
    if not request.user["can_edit_dim_a"]:
        body["dimAActual"] = existing.get("dimAActual", {})
    # 保留服务端写入的财报版本号（前端不应当影响）
    if "_finReportV" in existing:
        body["_finReportV"] = existing["_finReportV"]
    execute(
        "UPDATE app_state SET data = %s::jsonb, updated_by = %s, updated_at = NOW() WHERE id = 1",
        (json.dumps(body, ensure_ascii=False), request.user["u"]),
    )
    return jsonify({"ok": True, "updated_at": datetime.now(timezone.utc).isoformat()})


# ---------- 静态文件兜底 ----------
@app.route("/<path:p>")
def static_files(p):
    return send_from_directory(".", p)


if __name__ == "__main__":
    # 开发模式：启动时确保财报默认值已注入
    with app.app_context():
        ensure_finance_defaults()
    app.run(host="0.0.0.0", port=5000, debug=False)
