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


# ─────────────────── 账号权限初始化 ───────────────────
# 发布到生产时，新列默认值为 false，此函数在每次启动时确保关键权限正确
_CAN_MANAGE_ACCOUNTS = ["wangzhaoqian"]

def ensure_account_permissions():
    for u in _CAN_MANAGE_ACCOUNTS:
        execute(
            "UPDATE accounts SET can_manage_data = TRUE WHERE u = %s AND (can_manage_data IS NULL OR can_manage_data = FALSE)",
            (u,),
        )


# ─────────────────── Schema 升级（幂等，不删数据）───────────────────
def ensure_schema_updates():
    """幂等追加新列/新表，绝不删除现有数据。每次启动时执行。"""
    # custom_opps / opp_overrides 新增核心攻坚项扩展字段
    for col in ["业务线", "阶段", "预估金额", "预计签约月"]:
        execute(f'ALTER TABLE custom_opps    ADD COLUMN IF NOT EXISTS "{col}" TEXT')
        execute(f'ALTER TABLE opp_overrides  ADD COLUMN IF NOT EXISTS "{col}" TEXT')
    # opp_overrides / task_overrides 新增部门字段（支持调整责任部门）
    execute('ALTER TABLE opp_overrides  ADD COLUMN IF NOT EXISTS "主责部门" TEXT')
    execute('ALTER TABLE task_overrides ADD COLUMN IF NOT EXISTS "部门" TEXT')
    # 清除部门字段中的空字符串或非法值（防止条目从列表中消失）
    _DEPTS_SQL = "('销售部-1','销售部-2','经营管理部','研发交付部','综合部')"
    execute(f'UPDATE opp_overrides  SET "主责部门"=NULL WHERE "主责部门" IS NOT NULL AND ("主责部门"=\'\' OR "主责部门" NOT IN {_DEPTS_SQL})')
    execute(f'UPDATE task_overrides SET "部门"=NULL     WHERE "部门"     IS NOT NULL AND ("部门"=\'\' OR "部门" NOT IN {_DEPTS_SQL})')
    # 文件存储表
    execute("""CREATE TABLE IF NOT EXISTS uploaded_files (
        id           TEXT PRIMARY KEY,
        filename     TEXT,
        content_type TEXT,
        size         INTEGER,
        data         BYTEA,
        uploaded_by  TEXT,
        created_at   TIMESTAMPTZ DEFAULT NOW()
    )""")


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

    # ── 自定义任务项 ──
    custom_opps_rows  = query("SELECT * FROM custom_opps ORDER BY id")
    custom_opp_ms     = query("SELECT * FROM custom_opp_milestones")
    custom_tasks_rows = query("SELECT * FROM custom_tasks ORDER BY id")
    custom_task_ms    = query("SELECT * FROM custom_task_milestones")
    opp_ovr_rows      = query("SELECT * FROM opp_overrides")
    task_ovr_rows     = query("SELECT * FROM task_overrides")

    co_ms = {}
    for r in (custom_opp_ms or []):
        co_ms.setdefault(r["custom_opp_id"], {})[r["month"]] = r["milestone_text"]
    custom_opps = []
    for r in (custom_opps_rows or []):
        custom_opps.append({
            "id": r["id"], "dept": r["dept"], "客户": r["客户"],
            "opp_type": r["opp_type"], "归类说明": r["归类说明"] or "",
            "业务线": r.get("业务线") or "", "阶段": r.get("阶段") or "",
            "预估金额": r.get("预估金额") or "", "预计签约月": r.get("预计签约月") or "",
            "系数": float(r["系数"] or 1.2),
            "milestones": co_ms.get(r["id"], {}),
            "deleted": bool(r["deleted"]),
        })

    ct_ms = {}
    for r in (custom_task_ms or []):
        ct_ms.setdefault(r["custom_task_id"], {})[r["month"]] = r["milestone_text"]
    custom_tasks = []
    for r in (custom_tasks_rows or []):
        custom_tasks.append({
            "id": r["id"], "dept": r["dept"], "描述": r["描述"],
            "等级": r["等级"], "系数": float(r["系数"] or 1.0),
            "业务线": r["业务线"] or "", "期望完成": r["期望完成"] or "",
            "完成情况": r["完成情况"] or "",
            "milestones": ct_ms.get(r["id"], {}),
            "deleted": bool(r["deleted"]),
        })

    _VALID_DEPTS = {"销售部-1", "销售部-2", "经营管理部", "研发交付部", "综合部"}
    opp_overrides = {}
    for r in (opp_ovr_rows or []):
        row = {}
        for k in ("主责部门", "客户", "opp_type", "归类说明", "业务线", "阶段", "预估金额", "预计签约月", "deleted"):
            v = r.get(k)
            if v is None:
                continue
            # 空字符串或非法部门值不覆盖原始数据
            if k == "主责部门" and (not v or v not in _VALID_DEPTS):
                continue
            row[k] = v
        opp_overrides[r["opp_idx"]] = row
    task_overrides = {}
    for r in (task_ovr_rows or []):
        row = {}
        for k in ("部门", "描述", "等级", "业务线", "期望完成", "完成情况", "deleted"):
            v = r.get(k)
            if v is None:
                continue
            if k == "部门" and (not v or v not in _VALID_DEPTS):
                continue
            row[k] = v
        task_overrides[r["task_idx"]] = row

    return {
        "oppFill": opp_fill, "taskFill": task_fill, "dimAActual": dim_a,
        "customOpps": custom_opps, "customTasks": custom_tasks,
        "oppOverrides": opp_overrides, "taskOverrides": task_overrides,
    }


# ─────────────────── 路由：首页（SSI 注入）───────────────────
def _safe_json(v):
    """序列化为 JSON 并转义 < 防止 </script> 注入"""
    return json.dumps(v, ensure_ascii=False).replace("<", "\\u003c")


@app.route("/")
def index():
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()
    user = current_user()
    ensure_account_permissions()
    ensure_schema_updates()
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


# ─────────────────── 路由：任务管理（仅数据管理员）───────────────────
def manage_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if not u.get("can_manage_data"):
            return jsonify({"error": "仅数据管理员可执行此操作"}), 403
        request.user = u
        return f(*a, **kw)
    return w


@app.route("/api/manage/opp", methods=["POST"])
@login_required
def api_manage_opp_save():
    u_info = request.user
    b = request.get_json(force=True, silent=True) or {}
    custom_id = b.get("custom_id")
    orig_idx  = b.get("orig_idx")
    dept      = b.get("dept", "")
    ke_hu     = b.get("客户", "")
    opp_type  = b.get("opp_type", "other")
    gui_lei   = b.get("归类说明", "")
    ye_wu     = b.get("业务线", "")
    jie_duan  = b.get("阶段", "")
    pre_amt   = b.get("预估金额", "")
    pre_month = b.get("预计签约月", "")
    xi_shu    = float(b.get("系数", 1.2))
    milestones = b.get("milestones", {})
    u = u_info["u"]

    # 非管理员：只能操作自己部门的条目
    if not u_info.get("can_manage_data"):
        scope = u_info.get("scope") or ""
        base_scope = scope.replace("-1", "").replace("-2", "")
        base_dept  = dept.replace("-1", "").replace("-2", "")
        if scope and base_scope != base_dept:
            return jsonify({"error": "只能操作本部门条目"}), 403

    if orig_idx is not None:
        execute(
            """INSERT INTO opp_overrides (opp_idx, "主责部门", 客户, opp_type, 归类说明, 业务线, 阶段, 预估金额, 预计签约月, deleted, updated_by, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, NOW())
               ON CONFLICT (opp_idx) DO UPDATE SET
                 "主责部门"=EXCLUDED."主责部门",
                 客户=EXCLUDED.客户, opp_type=EXCLUDED.opp_type,
                 归类说明=EXCLUDED.归类说明, 业务线=EXCLUDED.业务线,
                 阶段=EXCLUDED.阶段, 预估金额=EXCLUDED.预估金额,
                 预计签约月=EXCLUDED.预计签约月, deleted=FALSE,
                 updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
            (orig_idx, dept, ke_hu, opp_type, gui_lei, ye_wu, jie_duan, pre_amt, pre_month, u),
        )
        for mon, txt in milestones.items():
            execute(
                """INSERT INTO opp_milestone (opp_idx, month, milestone_text, updated_by)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (opp_idx, month) DO UPDATE SET
                     milestone_text=EXCLUDED.milestone_text,
                     updated_by=EXCLUDED.updated_by,
                     updated_at=NOW()""",
                (orig_idx, mon, txt, u),
            )
        return jsonify({"ok": True, "orig_idx": orig_idx})

    if custom_id is not None:
        execute(
            'UPDATE custom_opps SET dept=%s, 客户=%s, opp_type=%s, 归类说明=%s, 业务线=%s, 阶段=%s, 预估金额=%s, 预计签约月=%s, 系数=%s WHERE id=%s',
            (dept, ke_hu, opp_type, gui_lei, ye_wu, jie_duan, pre_amt, pre_month, xi_shu, custom_id),
        )
        execute("DELETE FROM custom_opp_milestones WHERE custom_opp_id=%s", (custom_id,))
        for month, text in milestones.items():
            if text:
                execute(
                    "INSERT INTO custom_opp_milestones (custom_opp_id, month, milestone_text) VALUES (%s,%s,%s)",
                    (custom_id, month, text),
                )
        return jsonify({"ok": True, "custom_id": custom_id})

    row = query(
        'INSERT INTO custom_opps (dept, 客户, opp_type, 归类说明, 业务线, 阶段, 预估金额, 预计签约月, 系数, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (dept, ke_hu, opp_type, gui_lei, ye_wu, jie_duan, pre_amt, pre_month, xi_shu, u), one=True,
    )
    new_id = row["id"]
    for month, text in milestones.items():
        if text:
            execute(
                "INSERT INTO custom_opp_milestones (custom_opp_id, month, milestone_text) VALUES (%s,%s,%s)",
                (new_id, month, text),
            )
    return jsonify({"ok": True, "custom_id": new_id})


@app.route("/api/manage/opp", methods=["DELETE"])
@manage_required
def api_manage_opp_delete():
    b = request.get_json(force=True, silent=True) or {}
    custom_id = b.get("custom_id")
    orig_idx  = b.get("orig_idx")
    u = request.user["u"]
    if orig_idx is not None:
        execute(
            """INSERT INTO opp_overrides (opp_idx, deleted, updated_by, updated_at)
               VALUES (%s, TRUE, %s, NOW())
               ON CONFLICT (opp_idx) DO UPDATE SET deleted=TRUE,
                 updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
            (orig_idx, u),
        )
    elif custom_id is not None:
        execute("UPDATE custom_opps SET deleted=TRUE WHERE id=%s", (custom_id,))
    else:
        return jsonify({"error": "缺少参数"}), 400
    return jsonify({"ok": True})


@app.route("/api/manage/task", methods=["POST"])
@login_required
def api_manage_task_save():
    u_info = request.user
    b = request.get_json(force=True, silent=True) or {}
    custom_id  = b.get("custom_id")
    orig_idx   = b.get("orig_idx")
    dept       = b.get("dept", "")
    miao_shu   = b.get("描述", "")
    deng_ji    = b.get("等级", "B")
    xi_shu     = float(b.get("系数", 1.0))
    ye_wu_xian = b.get("业务线", "")
    qi_wang    = b.get("期望完成", "")
    wan_cheng  = b.get("完成情况", "")
    milestones = b.get("milestones", {})
    u = u_info["u"]

    # 非管理员：只能操作自己部门的条目
    if not u_info.get("can_manage_data"):
        scope = u_info.get("scope") or ""
        base_scope = scope.replace("-1", "").replace("-2", "")
        base_dept  = dept.replace("-1", "").replace("-2", "")
        if scope and base_scope != base_dept:
            return jsonify({"error": "只能操作本部门条目"}), 403

    if orig_idx is not None:
        execute(
            """INSERT INTO task_overrides (task_idx, "部门", 描述, 等级, 业务线, 期望完成, 完成情况, deleted, updated_by, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, %s, NOW())
               ON CONFLICT (task_idx) DO UPDATE SET
                 "部门"=EXCLUDED."部门",
                 描述=EXCLUDED.描述, 等级=EXCLUDED.等级,
                 业务线=EXCLUDED.业务线, 期望完成=EXCLUDED.期望完成,
                 完成情况=EXCLUDED.完成情况, deleted=FALSE,
                 updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
            (orig_idx, dept, miao_shu, deng_ji, ye_wu_xian, qi_wang, wan_cheng, u),
        )
        for mon, txt in milestones.items():
            execute(
                """INSERT INTO task_milestone (task_idx, month, milestone_text, updated_by)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (task_idx, month) DO UPDATE SET
                     milestone_text=EXCLUDED.milestone_text,
                     updated_by=EXCLUDED.updated_by,
                     updated_at=NOW()""",
                (orig_idx, mon, txt, u),
            )
        return jsonify({"ok": True, "orig_idx": orig_idx})

    if custom_id is not None:
        execute(
            "UPDATE custom_tasks SET dept=%s, 描述=%s, 等级=%s, 系数=%s, 业务线=%s, 期望完成=%s, 完成情况=%s WHERE id=%s",
            (dept, miao_shu, deng_ji, xi_shu, ye_wu_xian, qi_wang, wan_cheng, custom_id),
        )
        execute("DELETE FROM custom_task_milestones WHERE custom_task_id=%s", (custom_id,))
        for month, text in milestones.items():
            if text:
                execute(
                    "INSERT INTO custom_task_milestones (custom_task_id, month, milestone_text) VALUES (%s,%s,%s)",
                    (custom_id, month, text),
                )
        return jsonify({"ok": True, "custom_id": custom_id})

    row = query(
        """INSERT INTO custom_tasks (dept, 描述, 等级, 系数, 业务线, 期望完成, 完成情况, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (dept, miao_shu, deng_ji, xi_shu, ye_wu_xian, qi_wang, wan_cheng, u), one=True,
    )
    new_id = row["id"]
    for month, text in milestones.items():
        if text:
            execute(
                "INSERT INTO custom_task_milestones (custom_task_id, month, milestone_text) VALUES (%s,%s,%s)",
                (new_id, month, text),
            )
    return jsonify({"ok": True, "custom_id": new_id})


@app.route("/api/manage/task", methods=["DELETE"])
@manage_required
def api_manage_task_delete():
    b = request.get_json(force=True, silent=True) or {}
    custom_id = b.get("custom_id")
    orig_idx  = b.get("orig_idx")
    u = request.user["u"]
    if orig_idx is not None:
        execute(
            """INSERT INTO task_overrides (task_idx, deleted, updated_by, updated_at)
               VALUES (%s, TRUE, %s, NOW())
               ON CONFLICT (task_idx) DO UPDATE SET deleted=TRUE,
                 updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
            (orig_idx, u),
        )
    elif custom_id is not None:
        execute("UPDATE custom_tasks SET deleted=TRUE WHERE id=%s", (custom_id,))
    else:
        return jsonify({"error": "缺少参数"}), 400
    return jsonify({"ok": True})


# ─────────────────── 路由：文件上传 / 下载预览 ───────────────────
@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    import mimetypes
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    data = f.read()
    if len(data) > 50 * 1024 * 1024:
        return jsonify({"error": "file too large"}), 400
    file_id = secrets.token_hex(16)
    filename = f.filename or "file"
    ct = f.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    execute(
        "INSERT INTO uploaded_files (id, filename, content_type, size, data, uploaded_by) VALUES (%s,%s,%s,%s,%s,%s)",
        (file_id, filename, ct, len(data), psycopg2.Binary(data), request.user["u"]),
    )
    return jsonify({"file_id": file_id, "name": filename, "size": len(data)})


@app.route("/api/files/<file_id>")
@login_required
def api_serve_file(file_id):
    row = query(
        "SELECT filename, content_type, data FROM uploaded_files WHERE id=%s",
        (file_id,), one=True,
    )
    if not row:
        return jsonify({"error": "not found"}), 404
    resp = make_response(bytes(row["data"]))
    ct = row["content_type"] or "application/octet-stream"
    resp.headers["Content-Type"] = ct
    fname = row["filename"] or "file"
    # inline for images/pdf (预览)，attachment for others (下载)
    disposition = "inline" if ct.startswith("image/") or ct == "application/pdf" else "attachment"
    try:
        encoded = fname.encode("utf-8").decode("latin-1")
    except Exception:
        encoded = "file"
    resp.headers["Content-Disposition"] = f"{disposition}; filename=\"{encoded}\""
    return resp


# ─────────────────── 静态文件兜底 ───────────────────
@app.route("/<path:p>")
def static_files(p):
    return send_from_directory(".", p)


if __name__ == "__main__":
    with app.app_context():
        ensure_account_permissions()
        ensure_schema_updates()
        ensure_finance_defaults()
    app.run(host="0.0.0.0", port=5000, debug=False)
