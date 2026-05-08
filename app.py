"""海创元 2026 绩效管理系统 — Flask 后端（细粒度数据库版）

数据库设计：
  accounts      — 14 个账号，密码、权限
  sessions      — HttpOnly Cookie 会话 token
  opp_fill      — 核心攻坚项评分/备注/附件，PK(opp_idx, month)
  opp_milestone — 核心攻坚项自定义里程碑，PK(opp_idx, month)
  task_fill     — 专项任务项评分/备注/附件，PK(task_idx, month)
  task_milestone— 专项任务项自定义里程碑，PK(task_idx, month)
  dim_a_actual  — 维度A业绩指标实际值，PK(month, metric_name)
  system_config — 系统键值配置（财报版本等）

并发策略：每次写操作仅更新单行，行级 INSERT…ON CONFLICT DO UPDATE，
           不同用户编辑不同条目互不干扰；同一条目的极少数碰撞以最后写入获胜。
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

# 财报默认值版本；更新数值时递增
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


# ─────────────────── DB 辅助 ───────────────────
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


# ─────────────────── 会话 ───────────────────
def _session_payload(row):
    return {
        "u": row["u"], "name": row["name"], "org": row["org"],
        "title": row["title"], "role": row["role"], "scope": row["scope"],
        "canEditDimA":    bool(row["can_edit_dim_a"]),
        "canManageData":  bool(row["can_manage_data"]),
    }


def current_user():
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return query(
        """SELECT a.u, a.name, a.org, a.title, a.role, a.scope,
                  a.can_edit_dim_a, a.can_manage_data
           FROM sessions s JOIN accounts a ON a.u = s.u
           WHERE s.token = %s AND s.expires_at > NOW()""",
        (token,), one=True,
    )


def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        request.user = u
        return f(*a, **kw)
    return w


def gm_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if u["role"] != "GM":
            return jsonify({"error": "仅 GM 可执行此操作"}), 403
        request.user = u
        return f(*a, **kw)
    return w


# ─────────────────── 财报默认值初始化 ───────────────────
def ensure_finance_defaults():
    """若 system_config 中 fin_report_ver 与当前版本不符，则向 dim_a_actual 补填缺失数据。"""
    row = query("SELECT value FROM system_config WHERE key='fin_report_ver'", one=True)
    if row and row["value"] == FIN_REPORT_VER:
        return
    for month, metrics in FIN_MONTHLY.items():
        for metric_name, val in metrics.items():
            execute(
                """INSERT INTO dim_a_actual (month, metric_name, value, updated_by)
                   VALUES (%s, %s, %s, 'system')
                   ON CONFLICT (month, metric_name) DO NOTHING""",
                (month, metric_name, str(val)),
            )
    execute(
        """INSERT INTO system_config (key, value, updated_at)
           VALUES ('fin_report_ver', %s, NOW())
           ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
        (FIN_REPORT_VER,),
    )


# ─────────────────── 状态重建（从各细粒度表 → 前端期望结构）───────────────────
def build_state():
    opp_fill_rows = query(
        "SELECT opp_idx, month, score, note, files FROM opp_fill"
    )
    opp_ms_rows = query(
        "SELECT opp_idx, month, milestone_text FROM opp_milestone"
    )
    task_fill_rows = query(
        "SELECT task_idx, month, score, note, files FROM task_fill"
    )
    task_ms_rows = query(
        "SELECT task_idx, month, milestone_text FROM task_milestone"
    )
    dim_rows = query(
        "SELECT month, metric_name, value FROM dim_a_actual"
    )

    opp_fill = {}
    for r in (opp_fill_rows or []):
        idx = str(r["opp_idx"])
        opp_fill.setdefault(idx, {"months": {}, "milestones": {}})
        opp_fill[idx]["months"][r["month"]] = {
            "score": r["score"],
            "note":  r["note"],
            "files": r["files"] if isinstance(r["files"], list) else [],
        }
    for r in (opp_ms_rows or []):
        idx = str(r["opp_idx"])
        opp_fill.setdefault(idx, {"months": {}, "milestones": {}})
        opp_fill[idx]["milestones"][r["month"]] = r["milestone_text"]

    task_fill = {}
    for r in (task_fill_rows or []):
        idx = str(r["task_idx"])
        task_fill.setdefault(idx, {"months": {}, "milestones": {}})
        task_fill[idx]["months"][r["month"]] = {
            "score": r["score"],
            "note":  r["note"],
            "files": r["files"] if isinstance(r["files"], list) else [],
        }
    for r in (task_ms_rows or []):
        idx = str(r["task_idx"])
        task_fill.setdefault(idx, {"months": {}, "milestones": {}})
        task_fill[idx]["milestones"][r["month"]] = r["milestone_text"]

    dim_a = {"公司": {}}
    for r in (dim_rows or []):
        dim_a["公司"].setdefault(r["month"], {})
        dim_a["公司"][r["month"]][r["metric_name"]] = r["value"]

    return {"oppFill": opp_fill, "taskFill": task_fill, "dimAActual": dim_a}


# ─────────────────── 路由：首页（SSI 注入）───────────────────
def _safe_json(v):
    """序列化为 JSON 并转义 < 防止 </script> 注入"""
    return json.dumps(v, ensure_ascii=False).replace("<", "\\u003c")


@app.route("/")
def index():
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()
    user = current_user()
    ensure_finance_defaults()
    state = build_state() if user else {}
    session_payload = _session_payload(user) if user else None
    bootstrap = (
        f'<script type="application/json" id="__bs_sess__">{_safe_json(session_payload)}</script>'
        f'<script type="application/json" id="__bs_state__">{_safe_json(state)}</script>'
        "<script>(function(){"
        "try{window.__SESSION__=JSON.parse(document.getElementById('__bs_sess__').textContent);}catch(e){window.__SESSION__=null;}"
        "try{window.__CLOUD_STATE__=JSON.parse(document.getElementById('__bs_state__').textContent);}catch(e){window.__CLOUD_STATE__={};}"
        "})();</script>"
    )
    html = html.replace("<body>", "<body>\n" + bootstrap, 1)
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


# ─────────────────── 路由：认证 ───────────────────
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


# ─────────────────── 路由：全量状态读取 ───────────────────
@app.route("/api/state")
@login_required
def api_state_get():
    ensure_finance_defaults()
    return jsonify(build_state())


# ─────────────────── 路由：填报单行读取（并发合并用）─────────────────────
@app.route("/api/fill_row")
@login_required
def api_fill_row():
    """读取单条填报行（弹窗保存前用于先读后合并，防覆盖并发修改）。"""
    kind     = request.args.get("kind", "")      # "core" | "task"
    try:
        idx   = int(request.args.get("idx", ""))
        month = str(request.args.get("month", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "参数错误"}), 400

    if kind == "core":
        row = query(
            "SELECT score, note, files FROM opp_fill WHERE opp_idx=%s AND month=%s",
            (idx, month), one=True,
        )
    elif kind == "task":
        row = query(
            "SELECT score, note, files FROM task_fill WHERE task_idx=%s AND month=%s",
            (idx, month), one=True,
        )
    else:
        return jsonify({"error": "kind 必须为 core 或 task"}), 400

    if row is None:
        return jsonify({"exists": False, "score": None, "note": "", "files": []})
    return jsonify({
        "exists": True,
        "score": row["score"],
        "note":  row["note"] or "",
        "files": row["files"] or [],
    })


# ─────────────────── 路由：核心攻坚项 ───────────────────
@app.route("/api/opp_fill", methods=["POST"])
@login_required
def api_opp_fill_upsert():
    """新增或完整更新某条核心攻坚项某月的评分/备注/附件。"""
    b = request.get_json(force=True, silent=True) or {}
    try:
        opp_idx = int(b["opp_idx"])
        month   = str(b["month"])
    except (KeyError, ValueError):
        return jsonify({"error": "缺少 opp_idx / month"}), 400

    score = b.get("score")          # None 表示未评分（不强制要求传入）
    note  = b.get("note", "")
    files = b.get("files", [])

    execute(
        """INSERT INTO opp_fill (opp_idx, month, score, note, files, updated_by)
           VALUES (%s, %s, %s, %s, %s::jsonb, %s)
           ON CONFLICT (opp_idx, month) DO UPDATE SET
             score      = EXCLUDED.score,
             note       = EXCLUDED.note,
             files      = EXCLUDED.files,
             updated_by = EXCLUDED.updated_by,
             updated_at = NOW()""",
        (opp_idx, month, score, note,
         json.dumps(files, ensure_ascii=False), request.user["u"]),
    )
    return jsonify({"ok": True})


@app.route("/api/opp_fill/score", methods=["POST"])
@login_required
def api_opp_fill_score():
    """仅更新评分，不改备注/附件（快速评分按钮调用）。"""
    b = request.get_json(force=True, silent=True) or {}
    try:
        opp_idx = int(b["opp_idx"])
        month   = str(b["month"])
        score   = int(b["score"]) if b.get("score") is not None else None
    except (KeyError, ValueError):
        return jsonify({"error": "参数错误"}), 400

    execute(
        """INSERT INTO opp_fill (opp_idx, month, score, note, files, updated_by)
           VALUES (%s, %s, %s, '', '[]'::jsonb, %s)
           ON CONFLICT (opp_idx, month) DO UPDATE SET
             score      = EXCLUDED.score,
             updated_by = EXCLUDED.updated_by,
             updated_at = NOW()""",
        (opp_idx, month, score, request.user["u"]),
    )
    return jsonify({"ok": True})


@app.route("/api/opp_fill", methods=["DELETE"])
@login_required
def api_opp_fill_delete():
    """清除某条核心攻坚项某月的全部填报。"""
    b = request.get_json(force=True, silent=True) or {}
    try:
        opp_idx = int(b["opp_idx"])
        month   = str(b["month"])
    except (KeyError, ValueError):
        return jsonify({"error": "参数错误"}), 400
    execute(
        "DELETE FROM opp_fill WHERE opp_idx = %s AND month = %s",
        (opp_idx, month),
    )
    return jsonify({"ok": True})


@app.route("/api/opp_milestone", methods=["POST"])
@login_required
def api_opp_milestone():
    b = request.get_json(force=True, silent=True) or {}
    try:
        opp_idx = int(b["opp_idx"])
        month   = str(b["month"])
        text    = str(b.get("text", ""))
    except (KeyError, ValueError):
        return jsonify({"error": "参数错误"}), 400
    execute(
        """INSERT INTO opp_milestone (opp_idx, month, milestone_text, updated_by)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (opp_idx, month) DO UPDATE SET
             milestone_text = EXCLUDED.milestone_text,
             updated_by     = EXCLUDED.updated_by,
             updated_at     = NOW()""",
        (opp_idx, month, text, request.user["u"]),
    )
    return jsonify({"ok": True})


# ─────────────────── 路由：专项任务项 ───────────────────
@app.route("/api/task_fill", methods=["POST"])
@login_required
def api_task_fill_upsert():
    b = request.get_json(force=True, silent=True) or {}
    try:
        task_idx = int(b["task_idx"])
        month    = str(b["month"])
    except (KeyError, ValueError):
        return jsonify({"error": "缺少 task_idx / month"}), 400
    execute(
        """INSERT INTO task_fill (task_idx, month, score, note, files, updated_by)
           VALUES (%s, %s, %s, %s, %s::jsonb, %s)
           ON CONFLICT (task_idx, month) DO UPDATE SET
             score      = EXCLUDED.score,
             note       = EXCLUDED.note,
             files      = EXCLUDED.files,
             updated_by = EXCLUDED.updated_by,
             updated_at = NOW()""",
        (task_idx, month, b.get("score"), b.get("note", ""),
         json.dumps(b.get("files", []), ensure_ascii=False), request.user["u"]),
    )
    return jsonify({"ok": True})


@app.route("/api/task_fill/score", methods=["POST"])
@login_required
def api_task_fill_score():
    b = request.get_json(force=True, silent=True) or {}
    try:
        task_idx = int(b["task_idx"])
        month    = str(b["month"])
        score    = int(b["score"]) if b.get("score") is not None else None
    except (KeyError, ValueError):
        return jsonify({"error": "参数错误"}), 400
    execute(
        """INSERT INTO task_fill (task_idx, month, score, note, files, updated_by)
           VALUES (%s, %s, %s, '', '[]'::jsonb, %s)
           ON CONFLICT (task_idx, month) DO UPDATE SET
             score      = EXCLUDED.score,
             updated_by = EXCLUDED.updated_by,
             updated_at = NOW()""",
        (task_idx, month, score, request.user["u"]),
    )
    return jsonify({"ok": True})


@app.route("/api/task_fill", methods=["DELETE"])
@login_required
def api_task_fill_delete():
    b = request.get_json(force=True, silent=True) or {}
    try:
        task_idx = int(b["task_idx"])
        month    = str(b["month"])
    except (KeyError, ValueError):
        return jsonify({"error": "参数错误"}), 400
    execute(
        "DELETE FROM task_fill WHERE task_idx = %s AND month = %s",
        (task_idx, month),
    )
    return jsonify({"ok": True})


@app.route("/api/task_milestone", methods=["POST"])
@login_required
def api_task_milestone():
    b = request.get_json(force=True, silent=True) or {}
    try:
        task_idx = int(b["task_idx"])
        month    = str(b["month"])
        text     = str(b.get("text", ""))
    except (KeyError, ValueError):
        return jsonify({"error": "参数错误"}), 400
    execute(
        """INSERT INTO task_milestone (task_idx, month, milestone_text, updated_by)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (task_idx, month) DO UPDATE SET
             milestone_text = EXCLUDED.milestone_text,
             updated_by     = EXCLUDED.updated_by,
             updated_at     = NOW()""",
        (task_idx, month, text, request.user["u"]),
    )
    return jsonify({"ok": True})


# ─────────────────── 路由：维度 A 实际值 ───────────────────
@app.route("/api/dim_a_actual", methods=["POST"])
@login_required
def api_dim_a_actual():
    if not request.user["can_edit_dim_a"]:
        return jsonify({"error": "无权修改维度A 业绩指标"}), 403
    b = request.get_json(force=True, silent=True) or {}
    try:
        month       = str(b["month"])
        metric_name = str(b["metric_name"])
        value       = str(b.get("value", ""))
    except (KeyError, ValueError):
        return jsonify({"error": "参数错误"}), 400
    execute(
        """INSERT INTO dim_a_actual (month, metric_name, value, updated_by)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (month, metric_name) DO UPDATE SET
             value      = EXCLUDED.value,
             updated_by = EXCLUDED.updated_by,
             updated_at = NOW()""",
        (month, metric_name, value, request.user["u"]),
    )
    return jsonify({"ok": True})


# ─────────────────── 路由：重置全公司数据 ───────────────────
@app.route("/api/reset", methods=["POST"])
@gm_required
def api_reset():
    """清空所有填报/里程碑数据，保留财报默认值。仅 GM 可执行。"""
    for tbl in ("opp_fill", "opp_milestone", "task_fill", "task_milestone"):
        execute(f"DELETE FROM {tbl}")
    # dim_a_actual 只清除非系统默认的行（有 updated_by != 'system' 的行被视为用户填写）
    execute("DELETE FROM dim_a_actual WHERE updated_by != 'system'")
    return jsonify({"ok": True})


# ─────────────────── 静态文件兜底 ───────────────────
@app.route("/<path:p>")
def static_files(p):
    return send_from_directory(".", p)


if __name__ == "__main__":
    with app.app_context():
        ensure_finance_defaults()
    app.run(host="0.0.0.0", port=5000, debug=False)
