"""
api_server.py - LPA 移动端原型后端 (v2.0 措施级)
车间问题分层审核与跟踪系统 · API服务

v2.0 措施级重构: 审核对象从"问题"改为"措施"。一条措施独立评审、独立走
三层审核、独立关闭; 问题状态由措施实时汇总。

主要接口:
  GET  /api/persons            责任人名单(来自 config.py, 保持不变)
  GET  /api/tasks              措施级待办任务(按责任人姓名)
  POST /api/create_issue       新增问题 + 其下多条措施
  POST /api/measure_decision   措施评审决策(第三责任人 采纳/不采纳)
  POST /api/measure_review     措施分层审核(完全符合/完全不符合/部分符合)
  POST /api/upload_review_photo 整改照片上传(保持不变)
  GET  /api/measure_history    措施流转历史(含问题信息)

连接方式: 与本项目 Streamlit 版(db.py)完全一致的 Turso HTTP API
      (urllib 直调 /v2/pipeline 接口), 不引入额外依赖。

启动命令(在项目目录执行):
    uvicorn api_server:app --reload --host 0.0.0.0 --port 8000
"""
import io
import json
import os
import threading
import urllib.request
from datetime import datetime
from typing import Any, Dict, List

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

import auth_core  # v2.5 认证核心(PBKDF2/token/建表SQL, 与电脑端 db.py 共用)

# v2.8: 人员池配置(与电脑端 app.py 同源, 便于选到尚未录过问题的人员)
from config import LEVEL1_PERSONS, LEVEL2_PERSONS, LEVEL3_PERSONS

# ============================================================
# ★★★ Turso 数据库连接配置区 ★★★
# ============================================================
TURSO_URL = "libsql://lpaauditsystem-quality-dashboard.aws-ap-northeast-1.turso.io"


def _load_turso_token() -> str:
    """读取Turso令牌: 优先环境变量, 其次.streamlit/secrets.toml"""
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if token:
        return token
    secrets_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml"
    )
    try:
        with open(secrets_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("LPA_TURSO_TOKEN") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


TURSO_AUTH_TOKEN = _load_turso_token()
if not TURSO_AUTH_TOKEN:
    raise RuntimeError(
        "未找到Turso数据库令牌。请任选其一: "
        "1) 设置环境变量 TURSO_AUTH_TOKEN; "
        "2) 在项目目录 .streamlit/secrets.toml 中配置 LPA_TURSO_TOKEN"
    )
# ============================================================


# ============================================================
# ★★★ 整改照片上传配置区 (v1.1, 保持不变) ★★★
# ============================================================
_UPLOAD_DIR = os.environ.get(
    "LPA_UPLOAD_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
)
UPLOAD_URL_PREFIX = "/uploads"

_MAX_SIDE = 1600          # 二次压缩后最长边像素上限
_TARGET_QUALITY = 80      # 初始 JPEG 质量(0~100)
_MAX_FILE_BYTES = 500 * 1024   # 最终文件大小上限(500KB)

_DB_MIGRATED = False
_MIGRATE_LOCK = threading.Lock()


def _ensure_upload_dir():
    """确保上传目录存在, 不存在则自动创建。"""
    os.makedirs(_UPLOAD_DIR, exist_ok=True)


def compress_image_to_bytes(raw: bytes) -> bytes:
    """后端二次压缩: 最长边<=1600px, 转JPEG, 最终<=500KB(详情见注释)。"""
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as e:
        raise RuntimeError("无法识别的图片文件: {}".format(e))
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    else:
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > _MAX_SIDE:
        scale = _MAX_SIDE / float(max(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    quality = _TARGET_QUALITY
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    data = out.getvalue()

    while len(data) > _MAX_FILE_BYTES and quality > 30:
        quality -= 10
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()

    shrink_times = 0
    while len(data) > _MAX_FILE_BYTES and shrink_times < 2:
        w, h = img.size
        img = img.resize((int(w * 0.8), int(h * 0.8)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=max(quality, 30), optimize=True)
        data = out.getvalue()
        shrink_times += 1

    return data


def _ensure_column(table: str, column: str, col_type: str):
    """确保指定表存在指定列(幂等迁移)。
    SQLite 用 PRAGMA table_info 查列名, 缺列时 ALTER TABLE ADD COLUMN 补上。
    (与电脑端 db._ensure_column 同语义; 本模块独立实现, 避免依赖 streamlit)"""
    _, cols = _execute("PRAGMA table_info({})".format(table))
    existing = [r[1] for r in cols]
    if column not in existing:
        _execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, col_type))


def _migrate_db():
    """启动钩子: 建表(幂等) + 补列迁移(措施级 + 认证 + v3.1 QCS 部门/责任人/时间)。"""
    global _DB_MIGRATED
    if _DB_MIGRATED:
        return
    with _MIGRATE_LOCK:
        if _DB_MIGRATED:
            return
        try:
            # 建表(幂等, IF NOT EXISTS): measures 表
            _execute("""
                CREATE TABLE IF NOT EXISTS measures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    measure_content TEXT NOT NULL,
                    measure_order INTEGER,
                    is_adopted INTEGER,
                    status TEXT DEFAULT '待评审',
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            # v2.5 认证相关表(与电脑端 db.py 共用同一份 SQL)
            _execute(auth_core.ACCOUNTS_TABLE_SQL)
            _execute(auth_core.LOGIN_LOGS_TABLE_SQL)
            _execute(auth_core.SESSIONS_TABLE_SQL)
            for _idx_sql in auth_core.AUTH_INDEX_SQLS:
                _execute(_idx_sql)

            # issue_reviews 补列(老库迁移)
            _, cols_rows = _execute("PRAGMA table_info(issue_reviews)")
            existing = [r[1] for r in cols_rows]
            if "measure_id" not in existing:
                _execute("ALTER TABLE issue_reviews ADD COLUMN measure_id INTEGER")
            if "photo_path" not in existing:
                _execute("ALTER TABLE issue_reviews ADD COLUMN photo_path TEXT")

            # ---- v3.1 QCS 字段(幂等补列, 已存在则跳过) ----
            # accounts.department: P/Q/T, 决定 engineer 的数据范围; admin/manager 为 NULL
            _ensure_column("accounts", "department", "TEXT")
            # measures: 措施责任人 / 部门 / 预计完成 / 实际完成 / 完成时是否逾期
            for _c, _t in [("owner", "TEXT"), ("department", "TEXT"),
                           ("due_date", "TEXT"), ("completed_at", "TEXT"),
                           ("completed_overdue", "INTEGER")]:
                _ensure_column("measures", _c, _t)
            # issues: 问题部门
            _ensure_column("issues", "department", "TEXT")
            # v3.1 两层分配留痕字段
            _ensure_column("measures", "assigned_by_admin", "TEXT")
            _ensure_column("measures", "assigned_by_manager", "TEXT")
            # 被指派的部门经理(管理员第一次分配的目标, 不得越级到工程师)
            _ensure_column("measures", "assigned_manager", "TEXT")

            # ---- 历史遗留字段: 仅补列保持兼容, 已无业务含义 ----
            # v2.5 skip_level2: 三层审核已废弃, 列保留以免残留代码报错。
            #   ★ 不再执行 BACKFILL_SKIP_LEVEL2_SQL —— QCS 下 responsible_person_2
            #     恒为空, 回填会给所有措施写入 skip_level2=1 的无意义值。
            _ensure_column("measures", "skip_level2", "INTEGER")

            # v3.1 两层分配数据迁移(幂等): 旧"待执行"但未分配部门/工程师 → 已采纳(待二次分配)
            _execute(
                "UPDATE measures SET status = ? WHERE status = ? "
                "AND (department IS NULL OR department = '' OR owner IS NULL OR owner = '')",
                [auth_core.MEASURE_STATUS_ADOPTED, auth_core.MEASURE_STATUS_PENDING])
        except RuntimeError as e:
            print("数据库迁移跳过:", e)
        _DB_MIGRATED = True


# ============================================================


# ------------------------------------------------------------
# Turso HTTP API 执行层(与 db.py 同款逻辑的精简版)
# ------------------------------------------------------------

def _get_http_url() -> str:
    """把 libsql:// 地址转换成 HTTP pipeline 接口地址"""
    url = TURSO_URL
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    if url.endswith("/"):
        url = url[:-1]
    return url + "/v2/pipeline"


def _to_turso_arg(value: Any) -> Dict[str, Any]:
    """Python值 → Turso API参数格式。"""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _from_turso_value(val: Dict[str, Any]) -> Any:
    """Turso返回值 → Python值"""
    if val is None:
        return None
    t = val.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(val.get("value", 0))
    if t == "float":
        return float(val.get("value", 0.0))
    return val.get("value", "")


def _execute(sql: str, args: List[Any] = None):
    """执行单条SQL, 返回(列名列表, 行列表)。失败抛RuntimeError(中文信息)。"""
    turso_args = [_to_turso_arg(a) for a in args] if args else []
    body = json.dumps({
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": turso_args}},
            {"type": "close"},
        ]
    }).encode("utf-8")
    req = urllib.request.Request(
        _get_http_url(),
        data=body,
        headers={
            "Authorization": "Bearer {}".format(TURSO_AUTH_TOKEN),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result_json = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError("Turso连接失败: {} (请检查URL/Token/网络)".format(e))

    results = result_json.get("results", [])
    if not results:
        raise RuntimeError("Turso返回空结果")
    first = results[0]
    if first.get("type") == "error":
        raise RuntimeError("Turso错误: " + first.get("error", {}).get("message", "未知错误"))
    response = first.get("response", {})
    if response.get("type") != "execute":
        return [], []
    res = response.get("result", {})
    cols = [c.get("name", "") for c in res.get("cols", [])]
    rows = [[_from_turso_value(v) for v in r] for r in res.get("rows", [])]
    return cols, rows


# v2.5 认证服务实例。
# 注意: 必须放在 _execute 定义之后 —— 这里是模块级语句, 若在 _execute 之前
# 引用会触发 NameError: name '_execute' is not defined。
# 移动端后端用本模块 _execute 驱动, 与电脑端 db.py 共用同一套认证逻辑。
_auth = auth_core.AuthService(_execute)


# ------------------------------------------------------------
# v3.1 QCS 辅助函数(与 db.py 同规则 —— 改这边请同步改 db.py)
# ------------------------------------------------------------
# ★ v2.x 的三层审核状态机(_compute_measure_level / _LEVEL_REVIEW_RESULTS /
#   skip_level2 层级推导)已随 QCS 改造整体废弃, 不再保留。

def _today_date():
    """今天的 date 对象(统一入口, 便于排错)"""
    return datetime.now().date()


def _parse_date(v):
    """把 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' / date / datetime / Timestamp
    统一解析成 date 对象; 空值或解析失败返回 None。"""
    if v is None:
        return None
    if hasattr(v, "year") and hasattr(v, "month") and hasattr(v, "day"):
        try:
            return datetime(int(v.year), int(v.month), int(v.day)).date()
        except (ValueError, TypeError):
            return None
    s = str(v).strip()
    if not s:
        return None
    s = s.split(" ")[0].split("T")[0]      # 兼容带时间的字符串
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _is_overdue(due_date, status):
    """措施是否逾期 —— ★计算属性, 不入库。
    规则: due_date 非空 且 due_date < 今天 且 状态不在终态(已完成/已关闭)。"""
    if str(status or "") not in auth_core.MEASURE_OVERDUE_STATUSES:
        return False
    d = _parse_date(due_date)
    return d is not None and d < _today_date()


def _overdue_days(due_date, status):
    """逾期天数(未逾期返回 0)"""
    if str(status or "") not in auth_core.MEASURE_OVERDUE_STATUSES:
        return 0
    d = _parse_date(due_date)
    if d is None:
        return 0
    delta = (_today_date() - d).days
    return delta if delta > 0 else 0


def _is_due_soon(due_date, status, within_days=3):
    """是否即将到期: 今天起 within_days 天内(含今天)且未到终态。"""
    if str(status or "") not in auth_core.MEASURE_OVERDUE_STATUSES:
        return False
    d = _parse_date(due_date)
    if d is None:
        return False
    delta = (d - _today_date()).days
    return 0 <= delta <= within_days


def _dept_filter(role, department, alias="m", person_name=""):
    """v3.1 QCS 两层分配数据范围过滤 —— 返回 (SQL片段, 参数列表)。

      admin    → 不过滤(全部措施, 用于第一次分配)
      manager  → 仅【第一次分配给自己】的措施(assigned_manager = 本人姓名)
      engineer → 仅【第二次分配给自己】的措施(owner = 本人姓名)

    ★经理按 assigned_manager 而非 department 过滤:
      同部门可能有多个经理, 只有被指派的那个经理才能看到该措施。
      未取到姓名时返回 1=0(看不到任何数据, 安全默认)。
    """
    if role == auth_core.ROLE_ADMIN:
        return "", []
    if role == auth_core.ROLE_MANAGER:
        nm = str(person_name or "").strip()
        if not nm:
            return " AND 1=0 ", []
        return " AND COALESCE({}.assigned_manager, '') = ? ".format(alias), [nm]
    if role == auth_core.ROLE_ENGINEER:
        nm = str(person_name or "").strip()
        if not nm:
            return " AND 1=0 ", []
        return " AND COALESCE({}.owner, '') = ? ".format(alias), [nm]
    return " AND 1=0 ", []


def _recompute_issue_status(issue_id: int) -> str:
    """v3.1 QCS: 按该问题下措施的状态实时汇总, 写回 issues.status。
    规则: 无措施/无已采纳措施→未开始; 所有已采纳措施已关闭→已关闭; 其余→进行中。
    (待评审/不采纳 不参与关闭判断, 否则问题永远关不掉)"""
    _, measures = _execute(
        "SELECT status FROM measures WHERE issue_id = ?", [issue_id])
    active = [m for m in measures
              if (m[0] or "") in auth_core.MEASURE_ACTIVE_STATUSES]
    if not active:
        new_status = auth_core.ISSUE_STATUS_NOT_STARTED
    elif all((m[0] or "") == auth_core.MEASURE_STATUS_CLOSED for m in active):
        new_status = auth_core.ISSUE_STATUS_CLOSED
    else:
        new_status = auth_core.ISSUE_STATUS_DOING
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "UPDATE issues SET status = ?, updated_at = ? WHERE id = ?",
        [new_status, now, issue_id],
    )
    return new_status


# ------------------------------------------------------------
# FastAPI 应用
# ------------------------------------------------------------

app = FastAPI(
    title="QCS 质量协同系统 API",
    description="QCS 质量协同系统后端接口: 登录认证 / 措施任务 / 评审采纳 / 部门分配 / 状态流转 / 逾期提醒",
    version="3.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_ensure_upload_dir()
app.mount(UPLOAD_URL_PREFIX, StaticFiles(directory=_UPLOAD_DIR), name="uploads")


@app.on_event("startup")
def _on_startup():
    """服务启动钩子: 建表 + 补列迁移(幂等)。"""
    _migrate_db()


@app.get("/")
def root():
    """根路径: 服务状态自检"""
    return {
        "service": "LPA移动端原型API(措施级)",
        "status": "运行中",
        "接口": "GET /api/tasks?person_name=责任人姓名",
        "数据库配置": "已配置" if TURSO_URL and TURSO_AUTH_TOKEN else "未配置",
    }


def _reject(message: str):
    """构造业务校验失败的统一响应(400 + success:false + 中文原因)"""
    return JSONResponse(status_code=400, content={"success": False, "message": message})


# ============================================================
# 认证接口 (v2.5): 登录 / 登出 / 当前用户 / token 校验依赖
# ============================================================

def _require_token(authorization: str = Header(None)):
    """FastAPI 依赖: 校验请求头 Authorization: Bearer <token>。
    校验通过返回会话字典(含 username/role); 无效或过期抛 401。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录或缺少凭证")
    token = authorization[7:].strip()
    ok, sess, msg = _auth.validate_token(token)
    if not ok:
        raise HTTPException(status_code=401, detail=msg)
    return sess


class LoginRequest(BaseModel):
    """登录请求体"""
    username: str
    password: str


@app.post("/api/login")
def login(req: LoginRequest):
    """登录: 校验账号密码 → 生成会话 token → 记录登录日志。
    成功返回 {success, token, username, display_name, role, must_change_password}。
    首次登录(must_change_password=1)由前端引导到改密页。"""
    ok, acc, msg = _auth.authenticate(
        req.username, req.password,
        client_type=auth_core.CLIENT_MOBILE, ip="", user_agent="")
    if not ok:
        return _reject(msg)
    # v3.1 QCS: 系统管理员只负责账号管理(仅电脑端), 禁止其通过移动端登录
    if acc.get("role") == auth_core.ROLE_ADMIN:
        return _reject("系统管理员请使用电脑端管理账号, 不支持移动端登录")
    token = _auth.create_session(
        acc.get("username"), acc.get("role"), client_type=auth_core.CLIENT_MOBILE)
    return {
        "success": True,
        "token": token,
        "username": acc.get("username"),
        "display_name": acc.get("display_name"),
        "role": acc.get("role"),
        # v3.1 QCS: 部门(P/Q/T), 决定 engineer 的数据范围; manager 为 None
        "department": acc.get("department"),
        "must_change_password": int(acc.get("must_change_password") or 0),
    }


class ChangePasswordRequest(BaseModel):
    """改密请求体(首次登录强制改密 / 主动改密)"""
    username: str
    old_password: str
    new_password: str


@app.post("/api/change_password")
def change_password(req: ChangePasswordRequest):
    """修改密码(校验旧密码后更新; 同时清除强制改密标志与锁定状态)。"""
    acc = _auth.get_account(req.username)
    if not acc:
        return _reject("账号不存在")
    if not auth_core.verify_password(req.old_password, acc.get("password_hash")):
        return _reject("旧密码错误")
    ok, msg = _auth.change_password(req.username, req.new_password)
    if not ok:
        return _reject(msg)
    return {"success": True, "message": msg}


@app.post("/api/logout")
def logout(sess: dict = Depends(_require_token)):
    """登出: 更新会话与登录日志的退出时间。"""
    token = sess.get("token")
    _auth.logout(token)
    return {"success": True, "message": "已退出登录"}


@app.get("/api/me")
def me(sess: dict = Depends(_require_token)):
    """返回当前登录用户信息(用于移动端启动时校验 token 是否仍有效)。"""
    acc = _auth.get_account(sess.get("username"))
    if not acc:
        raise HTTPException(status_code=401, detail="账号不存在")
    return {
        "success": True,
        "username": acc.get("username"),
        "display_name": acc.get("display_name"),
        "role": acc.get("role"),
        # v3.1 QCS: 部门(P/Q/T), 决定 engineer 的数据范围; manager/admin 为 None
        "department": acc.get("department"),
        "must_change_password": int(acc.get("must_change_password") or 0),
    }


# ============================================================
# GET /api/persons — 责任人名单(v3.0: 从措施责任人动态读取)
# ============================================================

def _get_all_person_names():
    """v3.1 QCS: 从 measures 表动态读取所有非空【措施责任人 owner】姓名, 去重排序。
    (v2.x 从 issues 的 responsible_person_1/2/3 读取 —— QCS 下这三字段已废弃)
    数据库无措施时返回空列表(下拉框为空, 不报错)。"""
    try:
        _, rows = _execute("SELECT DISTINCT owner FROM measures")
    except RuntimeError:
        return []
    names = set()
    for r in rows:
        s = str(r[0] or "").strip()
        if s:
            names.add(s)
    return sorted(names)


@app.get("/api/persons")
def get_persons(sess: dict = Depends(_require_token)):
    """返回可选的责任人名单 + 部门列表 + 按部门工程师名单(v3.1 QCS 两层分配)。

    all         —— 已出现过的措施责任人(measures.owner) ∪ config 配置人员池,
                   保证能选到"尚未担任过责任人"的新人员(与电脑端同源)。
    departments —— P/Q/T 部门列表, 供录入问题时选择归属部门。
    engineers   —— 按部门分组的工程师账号 {P: [...], Q: [...], T: [...]},
                   供经理二次分配时选"本部门工程师"。
    """
    names = set(_get_all_person_names())
    for _lst in (LEVEL1_PERSONS, LEVEL2_PERSONS, LEVEL3_PERSONS):
        for _x in _lst:
            _s = str(_x or "").strip()
            if _s:
                names.add(_s)

    # 按部门列出工程师账号(供经理二次分配选人)
    engineers = {d: [] for d in auth_core.DEPARTMENTS}
    # 按部门列出经理账号(供管理员第一次分配选人; 只能分到经理层)
    managers = {d: [] for d in auth_core.DEPARTMENTS}
    try:
        for _a in _auth.list_accounts():
            _d = str(_a.get("department") or "").strip()
            if _d not in auth_core.DEPARTMENTS:
                continue
            _nm = _a.get("display_name") or _a.get("username")
            if _a.get("role") == auth_core.ROLE_ENGINEER and _a.get("status") == "active":
                engineers[_d].append(_nm)
            elif _a.get("role") == auth_core.ROLE_MANAGER and _a.get("status") == "active":
                managers[_d].append(_nm)
        for _d in auth_core.DEPARTMENTS:
            engineers[_d] = sorted(set(engineers[_d]))
            managers[_d] = sorted(set(managers[_d]))
    except Exception:
        pass

    # ★ 权限收敛: 经理只拿【本部门】的工程师/经理名单, 不暴露其他部门
    _acc = _auth.get_account(sess.get("username"))
    _role = _acc.get("role") if _acc else None
    if _role == auth_core.ROLE_MANAGER:
        _my_dept = str((_acc.get("department") or "")).strip()
        for _d in list(engineers.keys()):
            if _d != _my_dept:
                engineers[_d] = []
        for _d in list(managers.keys()):
            if _d != _my_dept:
                managers[_d] = []

    return {
        "all": sorted(names),
        "departments": list(auth_core.DEPARTMENTS),
        "engineers": engineers,
        "managers": managers,
    }


# ============================================================
# POST /api/create_issue — 新增问题 + 其下多条措施 (v2.0)
# ============================================================

class MeasureInput(BaseModel):
    """v3.1 QCS: 单条措施输入(录入时只需内容, 责任人/部门/时间在分配时设置)"""
    content: str          # 措施内容


class CreateIssueRequest(BaseModel):
    """新增问题请求体(v3.1 QCS, 仅 manager 可录入)"""
    problem_description: str      # 问题描述
    issue_date: str = ""          # 发现日期(YYYY-MM-DD)
    work_station: str = ""        # 工位
    issue_source: str = ""        # 问题来源(可选, 默认"监造提出")
    measures: List[MeasureInput]  # 措施列表(至少一条)


@app.post("/api/create_issue")
def create_issue(req: CreateIssueRequest, sess: dict = Depends(_require_token)):
    """v3.1 QCS: 新增问题 + 其下多条措施(一次提交保证数据一致)。

    权限: 仅 manager 可录入。
    措施录入时只需内容, 状态='待评审'; 部门/责任人/预计完成时间留空,
    待经理采纳后再分配(见 /api/measure_assign)。
    """
    _acc = _auth.get_account(sess.get("username"))
    if not _acc:
        raise HTTPException(status_code=401, detail="账号不存在")
    role = _acc.get("role")

    if role not in (auth_core.ROLE_ADMIN, auth_core.ROLE_MANAGER):
        return _reject("仅管理员或经理可录入问题")

    desc = (req.problem_description or "").strip()
    source = (req.issue_source or "").strip() or "监造提出"

    if not desc:
        return _reject("问题描述不能为空")
    if not req.measures:
        return _reject("必须至少添加一条改善措施")

    contents = []
    for m in req.measures:
        content = (m.content or "").strip()
        if not content:
            return _reject("存在空白的措施内容")
        contents.append(content)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        # ① 插入问题(状态'未开始', department 留空 —— 由第一条被分配措施决定)
        _execute(
            "INSERT INTO issues (problem_description, issue_date, work_station, "
            "issue_source, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [desc, (req.issue_date or "").strip(), (req.work_station or "").strip(),
             source, auth_core.ISSUE_STATUS_NOT_STARTED, now, now],
        )
        # ② 取新问题id
        _, id_rows = _execute("SELECT MAX(id) FROM issues")
        issue_id = int(id_rows[0][0]) if id_rows and id_rows[0][0] is not None else 0
        # ③ 逐条插入措施(状态'待评审'; owner/department/due_date 留空)
        for idx, content in enumerate(contents):
            _execute(
                "INSERT INTO measures (issue_id, measure_content, measure_order, "
                "owner, department, due_date, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [issue_id, content, idx + 1, None, None, None,
                 auth_core.MEASURE_STATUS_REVIEW, now, now],
            )
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    return {"success": True, "issue_id": issue_id,
            "message": "问题已登记(含{}条措施, 待经理评审)".format(len(contents))}


# ============================================================
# GET /api/tasks — 措施级待办任务 (v2.0)
# ============================================================

@app.get("/api/tasks")
def get_tasks(sess: dict = Depends(_require_token),
              person_name: str = Query(
                  None,
                  description="【已废弃】服务端一律以登录人身份为准, 该参数仅保留兼容")):
    """v3.1 QCS 核心接口: 返回【当前登录用户可见】的措施 + 逾期统计。

    数据范围(★服务端强制过滤, 前端无法绕过):
      admin    → 不参与业务, 返回空
      manager  → 全部 P/Q/T
      engineer → 仅本部门(accounts.department)的措施

    ★ 逾期是计算属性, 不入库:
        逾期     = due_date 过期 且 状态不在终态(已完成/已关闭)
        即将到期 = 今天起 3 天内到期 且 未到终态

    返回结构:
      {
        "role": "engineer",
        "department": "P",        # engineer 的部门; manager 为 None
        "groups": {               # 按状态分组, 每组是措施对象数组
          "待执行": [...], "执行中": [...],
          "已完成": [...], "已关闭": [...]
        },
        "overdue":  [...],        # 已逾期(按逾期天数倒序)
        "due_soon": [...],        # 3 天内到期
        "stats": {"total": 12, "overdue": 3, "due_soon": 2,
                  "by_status": {"待执行": 4, ...}}
      }
    每条措施对象字段: measure_id / issue_id / issue_no / measure_content /
      measure_order / owner / department / due_date / status /
      is_overdue / overdue_days / is_due_soon / completed_at /
      completed_overdue / problem_description / work_station /
      issue_date / issue_status
    """
    # ---- 当前用户身份与数据范围 ----
    _acc = _auth.get_account(sess.get("username"))
    if not _acc:
        raise HTTPException(status_code=401, detail="账号不存在, 请重新登录")
    role = _acc.get("role")
    dept = _acc.get("department")

    _empty_stats = {"total": 0, "overdue": 0, "due_soon": 0,
                    "by_status": {s: 0 for s in auth_core.MEASURE_STATUSES}}

    # 两层分配: admin 看全部(第一次分配); manager 本部门; engineer 本人名下
    dept_sql, dept_args = _dept_filter(role, dept, alias="m",
                                       person_name=_acc.get("display_name"))

    # ---- 统一问题序号(与 db.py 口径一致: created_at, id 升序) ----
    issue_no_map = {}
    try:
        _, no_rows = _execute(
            "SELECT id FROM issues "
            "ORDER BY (created_at IS NULL) ASC, created_at ASC, id ASC")
        issue_no_map = {row[0]: idx + 1 for idx, row in enumerate(no_rows)}
    except RuntimeError:
        issue_no_map = {}

    # ---- 查询可见措施(联表带出问题字段; 先按 due_date 排, 紧急的在前) ----
    try:
        cols, rows = _execute(
            "SELECT m.id, m.issue_id, m.measure_content, m.measure_order, "
            "       m.owner, m.department, m.due_date, "
            "       m.status AS measure_status, m.completed_at, m.completed_overdue, "
            "       i.problem_description, i.work_station, i.issue_date, "
            "       i.status AS issue_status "
            "FROM measures m JOIN issues i ON m.issue_id = i.id "
            "WHERE 1=1 " + dept_sql +
            " ORDER BY (m.due_date IS NULL) ASC, m.due_date ASC, "
            "          i.created_at ASC, m.measure_order ASC, m.id ASC",
            dept_args,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    c = {n: i for i, n in enumerate(cols)}

    def _obj(r):
        """行 → 措施对象(供前端渲染)"""
        due = r[c["due_date"]] if "due_date" in c else None
        st = r[c["measure_status"]] or ""
        return {
            "measure_id": r[c["id"]],
            "issue_id": r[c["issue_id"]],
            "issue_no": issue_no_map.get(r[c["issue_id"]]),
            "measure_content": r[c["measure_content"]] or "",
            "measure_order": r[c["measure_order"]],
            "owner": r[c["owner"]] or "",
            "department": r[c["department"]] or "",
            "due_date": due or "",
            "status": st,
            # 逾期 / 即将到期: 实时计算, 不入库
            "is_overdue": _is_overdue(due, st),
            "overdue_days": _overdue_days(due, st),
            "is_due_soon": _is_due_soon(due, st),
            "completed_at": r[c["completed_at"]] or "",
            "completed_overdue": r[c["completed_overdue"]],
            "problem_description": r[c["problem_description"]] or "",
            "work_station": r[c["work_station"]] or "",
            "issue_date": r[c["issue_date"]] or "",
            "issue_status": r[c["issue_status"]] or "",
        }

    groups = {s: [] for s in auth_core.MEASURE_STATUSES}
    overdue, due_soon = [], []
    for r in rows:
        o = _obj(r)
        groups.setdefault(o["status"], []).append(o)
        if o["is_overdue"]:
            overdue.append(o)
        elif o["is_due_soon"]:
            due_soon.append(o)

    overdue.sort(key=lambda x: x["overdue_days"], reverse=True)

    return {
        "role": role,
        "department": dept if role == auth_core.ROLE_ENGINEER else None,
        "groups": groups,
        "overdue": overdue,
        "due_soon": due_soon,
        "stats": {
            "total": len(rows),
            "overdue": len(overdue),
            "due_soon": len(due_soon),
            "by_status": {s: len(groups.get(s, [])) for s in auth_core.MEASURE_STATUSES},
        },
    }

    # ↓↓↓ 以下是 v2.x 三层审核遗留代码(return 之后不可达, 待后续清理) ↓↓↓
    _acc = _auth.get_account(sess.get("username"))
    name = str((_acc or {}).get("display_name") or sess.get("username") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="无法解析登录人身份, 请重新登录")

    # 1.5) 全系统统一问题序号(v2.2): 按 created_at,id 排序生成 {issue_id: 序号} 映射,
    #      与 db.py 的 load_all_issues 完全一致, 保证移动端与电脑端同一问题同序号。
    issue_no_map = {}
    try:
        _, no_rows = _execute(
            "SELECT id FROM issues "
            "ORDER BY (created_at IS NULL) ASC, created_at ASC, id ASC")
        issue_no_map = {row[0]: idx + 1 for idx, row in enumerate(no_rows)}
    except RuntimeError:
        issue_no_map = {}

    # 列名索引
    c = {n: i for i, n in enumerate(cols)}
    p1_idx, p2_idx, p3_idx = c["responsible_person_1"], c["responsible_person_2"], c["responsible_person_3"]

    def _measure_obj(r):
        """行 → 措施对象(供前端渲染)"""
        return {
            "measure_id": r[c["id"]],
            "issue_id": r[c["issue_id"]],
            "issue_no": issue_no_map.get(r[c["issue_id"]]),   # 统一问题序号(非数据库id)
            "measure_content": r[c["measure_content"]] or "",
            "measure_order": r[c["measure_order"]],
            "is_adopted": r[c["is_adopted"]],
            # v2.8 修正: 原 SQL 中 m.status 与 i.status 同名且无别名, 列名索引被
            # 后者覆盖, 导致 measure_status 实际取到的是【问题状态】。现已给两列
            # 加别名, 这里取措施自身状态(待评审/已评审/已退回/已关闭)。
            "measure_status": r[c["measure_status"]] or "",
            "problem_description": r[c["problem_description"]] or "",
            "work_station": r[c["work_station"]] or "",
            "issue_date": r[c["issue_date"]] or "",
            "issue_status": r[c["issue_status"]] or "",
            # v2.8: 供前端判断是否显示"转交第二责任人"按钮(仅 skip_level2=1 的措施)
            "skip_level2": r[c["skip_level2"]],
        }

    to_review, as_level1, as_level2, as_level3, closed = [], [], [], [], []

    # 2) 拉取这些措施的全部审核流水(一次性, 供层级推导)
    measure_ids = [r[c["id"]] for r in rows]
    reviews_by_measure = {}
    if measure_ids:
        placeholders = ",".join("?" for _ in measure_ids)
        try:
            _, review_rows = _execute(
                "SELECT id, measure_id, review_level, review_result "
                "FROM issue_reviews WHERE measure_id IN ({}) ORDER BY id ASC".format(placeholders),
                measure_ids,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))
        for rid, mid, level, result in review_rows:
            reviews_by_measure.setdefault(mid, []).append(
                {"rid": rid, "level": level, "result": result})

    # 3) 逐条措施归类(附带 level/role 字段, 供前端按措施自身层级提交审核)
    for r in rows:
        mid = r[c["id"]]
        is_adopted = r[c["is_adopted"]]
        measure_status = r[c["status"]] or ""
        p1 = str(r[p1_idx] or "").strip()
        p2 = str(r[p2_idx] or "").strip()
        p3 = str(r[p3_idx] or "").strip()
        obj = _measure_obj(r)
        # skip_level2: 采纳时固化在 measures.skip_level2; 若为 NULL(极旧数据)按 p2 兜底
        skip2_raw = r[c["skip_level2"]] if "skip_level2" in c else None
        skip2 = (str(p2) == "") if skip2_raw is None else bool(int(skip2_raw))

        # 待评审(采纳决策): 第三责任人视角, 措施 is_adopted=NULL
        if str(is_adopted) not in ("0", "1") and p3 == name:
            obj["role"] = 3          # 该人担任第三责任人
            obj["level"] = None      # 待评审, 无分层层级
            to_review.append(obj)

        # 已关闭措施 → "已处理"
        if measure_status == "已关闭":
            closed.append(obj)
            continue

        # 已采纳(is_adopted=1) → 分层审核, 推导当前层级并匹配角色
        if str(is_adopted) == "1":
            level = _compute_measure_level(reviews_by_measure.get(mid, []), measure_status, skip2)
            if level == 1 and p1 == name and measure_status in ("已评审", "已退回"):
                obj["role"] = 1
                obj["level"] = 1
                as_level1.append(obj)
            elif level == 2 and p2 == name:
                obj["role"] = 2
                obj["level"] = 2
                as_level2.append(obj)
            elif level == 3 and p3 == name:
                obj["role"] = 3
                obj["level"] = 3
                as_level3.append(obj)

    result = {
        "to_review": to_review,
        "as_level1": as_level1,
        "as_level2": as_level2,
        "as_level3": as_level3,
        "closed": closed,
    }

    # v2.7 角色权限: "待我评审(to_review)"是采纳/不采纳决策, 仅经理层有权限。
    # 其余角色一律置空, 移动端前端就不会渲染该区块;
    # 后端 measure_decision 另有角色校验兜底。
    if sess.get("role") != auth_core.ROLE_MANAGER:
        result["to_review"] = []

    return result


# ============================================================
# POST /api/measure_decision — 措施评审决策(采纳/不采纳) (v2.0)
# ============================================================

class MeasureDecisionRequest(BaseModel):
    """措施评审决策请求体(第三责任人)"""
    measure_id: int      # 措施id
    reviewer: str        # 评审人姓名(该措施所属问题的第三责任人)
    decision: str        # "adopt"=采纳 / "reject"=不采纳


@app.post("/api/measure_decision")
def submit_measure_decision(req: MeasureDecisionRequest,
                            sess: dict = Depends(_require_token)):
    # v3.1 QCS: 已废弃 —— QCS 无"采纳/不采纳"环节, 保留仅为兼容旧客户端
    return _reject("该接口已随 v3.1 QCS 改造废弃（已无采纳/不采纳环节）")
    """第三责任人对某条措施做采纳/不采纳决策(v2.0 措施级)。

    采纳(adopt):  is_adopted=1, status='已评审'(进入分层审核, 从第一责任人开始);
    不采纳(reject): is_adopted=0, status='待评审'(保留在不采纳列表, 可重新采纳);
    两种决策都在 issue_reviews 插 review_level=3 的评审流水
    (review_result='采纳'/'不采纳')。决策后立即重新汇总问题状态。"""
    # v2.7 角色权限: 采纳/不采纳是经理层(第三责任人)的决策动作 —— 仅 manager
    # 可执行。这是服务端兜底校验, 防止绕过界面(或移动端)直接调接口。
    if sess.get("role") != auth_core.ROLE_MANAGER:
        return _reject("当前角色无权执行采纳/不采纳决策(仅经理层可操作)")

    reviewer = (req.reviewer or "").strip()
    if not reviewer:
        return _reject("评审人姓名不能为空")

    # v2.6 安全: 评审人必须是登录人本人(防止冒用他人姓名提交决策)
    _acc = _auth.get_account(sess.get("username"))
    _self_name = str((_acc or {}).get("display_name") or "").strip()
    if _self_name and reviewer != _self_name:
        return _reject("评审人必须是登录人本人, 不能代他人提交决策")

    if req.decision not in ("adopt", "reject"):
        return _reject("decision必须是 adopt(采纳) 或 reject(不采纳)")

    # 查措施及其所属问题(含第二责任人, 用于采纳时固化 skip_level2)
    try:
        _, rows = _execute(
            "SELECT m.issue_id, i.responsible_person_3, i.responsible_person_2, m.is_adopted "
            "FROM measures m JOIN issues i ON m.issue_id = i.id WHERE m.id = ?",
            [req.measure_id],
        )
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("措施 #{} 不存在".format(req.measure_id))
    issue_id = rows[0][0]
    p3 = str(rows[0][1] or "").strip()
    p2 = str(rows[0][2] or "").strip()
    is_adopted = rows[0][3]

    if p3 != reviewer:
        return _reject("{} 不是该措施的第三责任人, 无权评审".format(reviewer))
    if str(is_adopted) in ("0", "1"):
        return _reject("措施 #{} 已评审过, 不能重复决策".format(req.measure_id))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if req.decision == "adopt":
        new_status = "已评审"
        result = "采纳"
        comment = "【措施评审】采纳, 进入分层审核流程"
    else:
        new_status = "待评审"
        result = "不采纳"
        comment = "【措施评审】不采纳, 暂不整改"

    try:
        # v2.5: 采纳时固化"是否跳过第二责任人"(p2 留空 → skip_level2=1)
        if req.decision == "adopt":
            skip_flag = 1 if p2 == "" else 0
            _execute(
                "UPDATE measures SET is_adopted = ?, status = ?, skip_level2 = ?, "
                "updated_at = ? WHERE id = ?",
                [1, new_status, skip_flag, now, req.measure_id],
            )
        else:
            _execute(
                "UPDATE measures SET is_adopted = ?, status = ?, updated_at = ? WHERE id = ?",
                [0, new_status, now, req.measure_id],
            )
        _execute(
            "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
            "review_result, review_comment, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [issue_id, req.measure_id, 3, reviewer, result, comment, now, now],
        )
        _recompute_issue_status(issue_id)
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    action_word = "采纳" if req.decision == "adopt" else "不采纳"
    return {"success": True,
            "message": "措施评审完成: {}, 措施 #{} 已{}".format(
                action_word, req.measure_id,
                "进入分层审核流程" if req.decision == "adopt" else "搁置(可重新采纳)")}


# ============================================================
# POST /api/measure_assign_level2 — 转交第二责任人 (v2.8)
# ============================================================

class AssignLevel2Request(BaseModel):
    """转交第二责任人请求体(第三责任人发起)"""
    measure_id: int      # 措施id
    person2: str         # 要补充的第二责任人姓名
    reviewer: str        # 操作人姓名(必须是该措施所属问题的第三责任人)


@app.post("/api/measure_assign_level2")
def assign_level2(req: AssignLevel2Request,
                  sess: dict = Depends(_require_token)):
    # v3.1 QCS: 已废弃 —— QCS 无"第二责任人"概念, 保留仅为兼容旧客户端
    return _reject("该接口已随 v3.1 QCS 改造废弃（已无第二责任人概念）")
    """v2.8: 第三责任人"中途补充第二责任人"(问题级粒度)。

    场景: 措施采纳时第二责任人留空 → skip_level2 固化为 1(跳过第二责任人);
    措施流转到第三责任人后, 若认为技术性较强需要工程师介入, 可发起本动作
    把措施交回第二责任人验收。

    ★ 与"完全不符合"退回的区别(关键):
        完全不符合     → 否定整改结果: status='已退回', 强制退回第一责任人重做
        转交第二责任人 → 不否定整改结果: status 保持'已评审', 第一责任人的
                         整改记录与照片完整保留, 仅取消"跳过第二责任人"标记

    ★ 对状态机零改动:
        写入的 review_result='转交第二责任人' 不在 _LEVEL_REVIEW_RESULTS 内,
        层级推导时会被自动忽略; 层级回退完全由 skip_level2 由 1→0 驱动。

    ★ 粒度: 问题级 —— 该问题下所有 skip_level2=1 且未关闭的措施一并改为 0,
      与当前措施一起走完整三层流程。

    校验:
      - 操作人必须是该措施所属问题的第三责任人(姓名匹配)
      - 操作人必须是登录人本人(防冒用)
      - 该措施必须处于"跳过第二责任人"状态(skip_level2=1)
      - 该措施不能已关闭
    """
    reviewer = (req.reviewer or "").strip()
    person2 = (req.person2 or "").strip()
    if not reviewer:
        return _reject("操作人姓名不能为空")
    if not person2:
        return _reject("第二责任人姓名不能为空")

    # 安全: 操作人必须是登录人本人(防止冒用他人姓名提交)
    _acc = _auth.get_account(sess.get("username"))
    _self_name = str((_acc or {}).get("display_name") or "").strip()
    if _self_name and reviewer != _self_name:
        return _reject("操作人必须是登录人本人, 不能代他人提交")

    # 查措施 + 所属问题(校验存在性 / p3身份 / 跳过标记 / 是否已关闭)
    try:
        _, rows = _execute(
            "SELECT m.issue_id, m.status, m.skip_level2, i.responsible_person_3 "
            "FROM measures m JOIN issues i ON m.issue_id = i.id WHERE m.id = ?",
            [req.measure_id],
        )
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("措施 #{} 不存在".format(req.measure_id))
    issue_id = rows[0][0]
    measure_status = str(rows[0][1] or "")
    skip_flag = rows[0][2]
    p3 = str(rows[0][3] or "").strip()

    if p3 != reviewer:
        return _reject("{} 不是该措施的第三责任人, 无权转交".format(reviewer))
    if measure_status == "已关闭":
        return _reject("措施 #{} 已关闭, 不能再转交".format(req.measure_id))
    if int(skip_flag or 0) != 1:
        return _reject("措施 #{} 当前未跳过第二责任人, 无需转交".format(req.measure_id))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        # 1) 统计本次受影响的措施数(问题级粒度)
        _, cnt_rows = _execute(
            "SELECT COUNT(*) FROM measures WHERE issue_id = ? "
            "AND COALESCE(skip_level2, 0) = 1 AND COALESCE(status, '') <> '已关闭'",
            [issue_id])
        affected = int(cnt_rows[0][0]) if cnt_rows else 0

        # 2) 补上第二责任人(问题级字段)
        _execute(
            "UPDATE issues SET responsible_person_2 = ?, updated_at = ? WHERE id = ?",
            [person2, now, issue_id])

        # 3) 问题级: 该问题下所有"已跳过且未关闭"的措施一并取消跳过
        _execute(
            "UPDATE measures SET skip_level2 = 0, updated_at = ? "
            "WHERE issue_id = ? AND COALESCE(skip_level2, 0) = 1 "
            "  AND COALESCE(status, '') <> '已关闭'",
            [now, issue_id])

        # 4) 留痕: 该 result 不在 _LEVEL_REVIEW_RESULTS 内, 状态机推导时自动忽略
        _execute(
            "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
            "review_result, review_comment, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [issue_id, req.measure_id, 3, reviewer, "转交第二责任人",
             "【转交第二责任人】技术性较强, 转交工程师 {} 介入验收"
             "(该问题下所有跳过第二责任人的未关闭措施一并改为走三层)".format(person2),
             now, now])

        _recompute_issue_status(issue_id)
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    # 企微通知预留位(本次需求: 暂不发送)
    # 待群机器人 webhook 权限开通后, 取消下方注释即可通知被补的第二责任人:
    #     try:
    #         send_wechat_message(
    #             "【LPA转交验收】措施已转交给 {}, 请及时验收。".format(person2))
    #     except Exception:
    #         pass

    return {"success": True,
            "message": "已转交第二责任人「{}」, 该问题下 {} 条措施改为完整三层流程".format(
                person2, affected)}


# ============================================================
# POST /api/measure_review — 措施分层审核 (v2.0)
# ============================================================

class MeasureReviewRequest(BaseModel):
    """措施分层审核请求体"""
    measure_id: int          # 措施id
    review_level: int        # 审核层级: 1/2/3
    reviewer: str            # 审核人姓名(该层责任人)
    review_result: str       # 完全符合 / 完全不符合 / 部分符合
    review_comment: str      # 备注(完全符合=整改说明 / 完全不符合=原因 / 部分符合=说明)
    photo_path: str = None   # 照片相对路径(仅"完全符合"时必传)


@app.post("/api/measure_review")
def submit_measure_review(req: MeasureReviewRequest,
                          sess: dict = Depends(_require_token)):
    # v3.1 QCS: 已废弃 —— 三层审核(完全符合/不符合/部分符合)已取消,
    # 请改用 POST /api/measure_status 更新措施状态。保留仅为兼容旧客户端。
    return _reject("该接口已随 v3.1 QCS 改造废弃（请改用 /api/measure_status）")
    """措施分层审核(v2.0): 完全符合/完全不符合/部分符合。

    状态机规则:
      完全符合:  本层通过, 推进到下一层; 第三责任人完全符合 → 措施'已关闭'
      完全不符合: 本层否决, 措施'已退回', 回到第一责任人重新整改
      部分符合:  措施留在当前层, 不推进不退回(状态保持'已评审')
    校验:
      - 审核人必须是该措施所属问题对应层级的责任人
      - 当前层级必须匹配 review_level
      - 完全符合必须上传照片; 完全不符合/部分符合必须填原因/说明
    每次审核后立即重新汇总问题状态。"""
    reviewer = (req.reviewer or "").strip()
    comment = (req.review_comment or "").strip()
    photo_path = (req.photo_path or "").strip()

    if not reviewer:
        return _reject("审核人姓名不能为空")
    if req.review_level not in (1, 2, 3):
        return _reject("review_level必须是1/2/3")
    if req.review_result not in ("完全符合", "完全不符合", "部分符合"):
        return _reject("review_result必须是: 完全符合/完全不符合/部分符合")
    if not comment:
        return _reject("备注必填(完全符合=整改说明 / 完全不符合=不合格原因 / 部分符合=具体说明)")
    if req.review_result == "完全符合" and not photo_path:
        return _reject("完全符合必须上传整改照片(请先上传照片再提交)")

    # 查措施 + 问题责任人 + 措施当前状态
    try:
        _, rows = _execute(
            "SELECT m.issue_id, m.status, m.is_adopted, m.skip_level2, "
            "       i.responsible_person_1, i.responsible_person_2, i.responsible_person_3 "
            "FROM measures m JOIN issues i ON m.issue_id = i.id WHERE m.id = ?",
            [req.measure_id],
        )
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("措施 #{} 不存在".format(req.measure_id))

    issue_id = rows[0][0]
    measure_status = rows[0][1] or ""
    is_adopted = rows[0][2]
    skip2_raw = rows[0][3]
    p1 = str(rows[0][4] or "").strip()
    p2 = str(rows[0][5] or "").strip()
    p3 = str(rows[0][6] or "").strip()
    # skip_level2: 采纳时固化在 measures.skip_level2; 若为 NULL(极旧数据)按 p2 兜底
    skip2 = (str(p2) == "") if skip2_raw is None else bool(int(skip2_raw))

    if str(is_adopted) != "1":
        return _reject("措施 #{} 尚未被采纳, 不能分层审核(先由第三责任人评审)".format(req.measure_id))
    if measure_status == "已关闭":
        return _reject("措施 #{} 已关闭, 不能再审核".format(req.measure_id))

    # 越权校验: 审核人必须是该层责任人
    level_owner = {1: p1, 2: p2, 3: p3}[req.review_level]
    if level_owner != reviewer:
        return _reject("{} 不是该措施所属问题第{}责任人, 无权操作".format(
            reviewer, req.review_level))

    # 层级校验: 当前层级必须匹配
    try:
        _, review_rows = _execute(
            "SELECT id, review_level, review_result FROM issue_reviews "
            "WHERE measure_id = ? ORDER BY id ASC",
            [req.measure_id],
        )
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    records = [{"rid": r[0], "level": r[1], "result": r[2]} for r in review_rows]
    current_level = _compute_measure_level(records, measure_status, skip2)
    if current_level != req.review_level:
        return _reject("措施 #{} 当前在第{}层待处理, 第{}责任人不能操作".format(
            req.measure_id, current_level or 0, req.review_level))

    # 确定措施新状态
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if req.review_result == "完全符合":
        new_status = "已关闭" if req.review_level == 3 else "已评审"
    elif req.review_result == "完全不符合":
        new_status = "已退回"
    else:  # 部分符合
        new_status = "已评审"  # 留在当前层, 状态保持已评审

    try:
        # ① 插分层审核流水
        _execute(
            "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
            "review_result, review_comment, photo_path, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [issue_id, req.measure_id, req.review_level, reviewer,
             req.review_result, comment, photo_path or None, now, now],
        )
        # ② 更新措施状态
        _execute(
            "UPDATE measures SET status = ?, updated_at = ? WHERE id = ?",
            [new_status, now, req.measure_id],
        )
        # ③ 重新汇总问题状态
        _recompute_issue_status(issue_id)
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    if req.review_result == "完全符合" and req.review_level == 3:
        msg = "审核已提交, 措施 #{} 已关闭".format(req.measure_id)
    elif req.review_result == "完全不符合":
        msg = "审核已提交, 措施 #{} 已退回第一责任人".format(req.measure_id)
    elif req.review_result == "部分符合":
        msg = "审核已提交, 措施 #{} 继续在第{}层跟踪".format(req.measure_id, req.review_level)
    else:
        msg = "审核已提交, 措施 #{} 推进到第{}层".format(req.measure_id, req.review_level + 1)
    return {"success": True, "message": msg}


# ============================================================
# POST /api/upload_review_photo — 整改照片上传(保持不变)
# ============================================================

_ALLOWED_IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "jpg",
    "image/webp": "jpg",
    "image/heic": "jpg",
    "image/heif": "jpg",
    "image/bmp": "jpg",
}


@app.post("/api/upload_review_photo")
async def upload_review_photo(file: UploadFile = File(..., description="整改照片文件"),
                              sess: dict = Depends(_require_token)):
    """接收照片, 后端二次压缩后保存磁盘, 返回相对路径(详情见旧版注释)。"""
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_IMAGE_TYPES:
        return _reject("仅支持图片格式(JPEG/PNG/WebP/HEIC/BMP), 当前类型: {}".format(
            content_type or "未知"))
    raw = await file.read()
    if not raw:
        return _reject("上传文件为空")

    try:
        compressed = compress_image_to_bytes(raw)
    except RuntimeError as e:
        return _reject("照片压缩失败: " + str(e))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rand = os.urandom(3).hex()
    filename = "{}_{}.jpg".format(ts, rand)
    save_path = os.path.join(_UPLOAD_DIR, filename)
    try:
        with open(save_path, "wb") as f:
            f.write(compressed)
    except OSError as e:
        return _reject("照片保存失败(磁盘写入错误): " + str(e))

    rel_path = "{}/{}".format(UPLOAD_URL_PREFIX, filename)
    return {"success": True, "photo_path": rel_path}


# ============================================================
# GET /api/measure_history — 措施流转历史(含问题信息) (v2.0)
# ============================================================

@app.get("/api/measure_history")
def get_measure_history(measure_id: int = Query(..., description="措施编号"),
                        sess: dict = Depends(_require_token)):
    """返回指定措施的完整流转历史 + 问题信息 + 措施内容。
    前端据此同时显示"问题信息 + 措施信息", 不能只显示措施。
    返回: { measure, issue, history: [...] }"""
    try:
        # 措施 + 问题联查(v3.1 QCS: 带出责任人/部门/时间字段)
        _, rows = _execute(
            "SELECT m.id, m.measure_content, m.measure_order, m.status, "
            "       m.owner, m.department, m.due_date, m.completed_at, m.completed_overdue, "
            "       i.id, i.problem_description, i.work_station, i.issue_date, "
            "       i.department, i.status "
            "FROM measures m JOIN issues i ON m.issue_id = i.id WHERE m.id = ?",
            [measure_id],
        )
    except RuntimeError as e:
        return {"success": False, "message": "数据库查询失败: " + str(e)}
    if not rows:
        return {"success": False, "message": "措施 #{} 不存在".format(measure_id)}

    r = rows[0]
    _st = r[3] or ""
    _due = r[6]
    measure_info = {
        "id": r[0], "measure_content": r[1] or "", "measure_order": r[2],
        "status": _st,
        # v3.1 QCS 字段
        "owner": r[4] or "", "department": r[5] or "", "due_date": _due or "",
        "completed_at": r[7] or "", "completed_overdue": r[8],
        # 逾期: 实时计算, 不入库
        "is_overdue": _is_overdue(_due, _st),
        "overdue_days": _overdue_days(_due, _st),
    }
    issue_info = {
        "id": r[9], "problem_description": r[10] or "",
        "work_station": r[11] or "", "issue_date": r[12] or "",
        # v3.1 QCS 字段
        "department": r[13] or "", "status": r[14] or "",
    }

    # 审核流水(倒序, 最新在前)
    try:
        _, review_rows = _execute(
            "SELECT review_level, reviewer, review_result, review_comment, "
            "reviewed_at, photo_path, id "
            "FROM issue_reviews WHERE measure_id = ? ORDER BY id DESC",
            [measure_id],
        )
    except RuntimeError as e:
        return {"success": False, "message": "数据库查询失败: " + str(e)}

    history = [
        {
            "review_level": rr[0],
            "reviewer": rr[1] or "",
            "review_result": rr[2] or "",
            "review_comment": rr[3] or "",
            "reviewed_at": rr[4] or "",
            "photo_path": rr[5] or "",
        }
        for rr in review_rows
    ]
    return {"success": True, "measure": measure_info,
            "issue": issue_info, "history": history}


# ============================================================
# v3.1 QCS: POST /api/measure_adopt — 措施采纳/不采纳(经理评审)
# ============================================================

class MeasureAdoptRequest(BaseModel):
    """措施采纳/不采纳请求体(经理)"""
    measure_id: int          # 措施id
    decision: str            # adopt(采纳) / reject(不采纳)
    reviewer: str            # 评审人姓名(经理)


@app.post("/api/measure_adopt")
def adopt_measure(req: MeasureAdoptRequest, sess: dict = Depends(_require_token)):
    """v3.1 QCS: 经理对措施做采纳/不采纳。

    采纳   → 待评审 → 待执行(部门待分配)
    不采纳 → 待评审 → 不采纳(保留记录, 可重新采纳)
    权限: 仅 manager。
    """
    _acc = _auth.get_account(sess.get("username"))
    if not _acc:
        raise HTTPException(status_code=401, detail="账号不存在")
    if _acc.get("role") != auth_core.ROLE_MANAGER:
        return _reject("仅经理可执行采纳/不采纳")

    reviewer = (req.reviewer or "").strip()
    if not reviewer:
        return _reject("评审人姓名不能为空")
    if str((_acc or {}).get("display_name") or "").strip() != reviewer:
        return _reject("评审人必须是登录人本人, 不能代他人提交")
    if req.decision not in ("adopt", "reject"):
        return _reject("decision 必须是 adopt(采纳) 或 reject(不采纳)")

    try:
        _, rows = _execute(
            "SELECT id, issue_id, status FROM measures WHERE id = ?", [req.measure_id])
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("措施 #{} 不存在".format(req.measure_id))
    issue_id = int(rows[0][1])
    old_status = str(rows[0][2] or "")
    if old_status not in (auth_core.MEASURE_STATUS_REVIEW,
                          auth_core.MEASURE_STATUS_REJECTED):
        return _reject("措施 #{} 当前状态「{}」不可评审(仅待评审/不采纳可操作)".format(
            req.measure_id, old_status))

    if req.decision == "adopt":
        new_status = auth_core.MEASURE_STATUS_ADOPTED
        result = "采纳"
        comment = "【措施评审】采纳, 待二次分配工程师"
    else:
        new_status = auth_core.MEASURE_STATUS_REJECTED
        result = "不采纳"
        comment = "【措施评审】不采纳, 暂不执行(可重新采纳)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _execute(
            "UPDATE measures SET status = ?, updated_at = ? WHERE id = ?",
            [new_status, now, req.measure_id])
        _execute(
            "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
            "review_result, review_comment, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [issue_id, req.measure_id, None, reviewer, result, comment, now, now])
        _recompute_issue_status(issue_id)
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    return {"success": True, "measure_id": req.measure_id,
            "old_status": old_status, "new_status": new_status,
            "message": "措施 #{} 已{}".format(
                req.measure_id,
                "采纳, 待分配部门" if req.decision == "adopt" else "不采纳")}


# ============================================================
# v3.1 QCS: POST /api/measure_assign — 分配部门+责任人+预计完成(经理)
# ============================================================

class MeasureAssignDeptRequest(BaseModel):
    """措施第一次分配请求体(管理员 → P/Q/T 部门经理)"""
    measure_id: int          # 措施id
    department: str          # P / Q / T
    manager_name: str        # 接收的部门经理姓名(必须是 role=manager 的账号)
    operator: str            # 操作人姓名(管理员)


@app.post("/api/measure_assign_dept")
def assign_measure_dept(req: MeasureAssignDeptRequest, sess: dict = Depends(_require_token)):
    """v3.1 QCS 两层分配·第一层: 管理员把措施分配给 P/Q/T 部门的【经理】。

    分配后, 该措施进入该经理(本部门)的"待评审"列表(状态仍待评审)。权限: 仅 admin。
    ★ 只能分到经理层, 不得越级分给工程师。
    """
    _acc = _auth.get_account(sess.get("username"))
    if not _acc:
        raise HTTPException(status_code=401, detail="账号不存在")
    if _acc.get("role") != auth_core.ROLE_ADMIN:
        return _reject("仅管理员可第一次分配部门")

    operator = (req.operator or "").strip()
    if not operator:
        return _reject("操作人姓名不能为空")
    if str((_acc or {}).get("display_name") or "").strip() != operator:
        return _reject("操作人必须是登录人本人, 不能代他人提交")
    dept = (req.department or "").strip().upper()
    if dept not in auth_core.DEPARTMENTS:
        return _reject("部门必须是 P / Q / T")
    manager_name = (req.manager_name or "").strip()
    if not manager_name:
        return _reject("必须指定接收的部门经理")

    # ★ 校验接收人: 必须是该部门的经理(engineer 不允许)
    _mgr = None
    for _a in _auth.list_accounts():
        if str(_a.get("display_name") or "").strip() == manager_name or \
           str(_a.get("username") or "").strip() == manager_name:
            _mgr = _a
            break
    if not _mgr:
        return _reject("找不到姓名/账号为「{}」的用户".format(manager_name))
    if _mgr.get("role") != auth_core.ROLE_MANAGER:
        return _reject("「{}」不是部门经理, 管理员只能分配给经理层".format(manager_name))
    if str(_mgr.get("department") or "").strip().upper() != dept:
        return _reject("「{}」不属于 {} 部门".format(manager_name, dept))
    manager_name = str(_mgr.get("display_name") or manager_name).strip()

    try:
        _, rows = _execute(
            "SELECT id, issue_id, status, department FROM measures WHERE id = ?",
            [req.measure_id])
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("措施 #{} 不存在".format(req.measure_id))
    issue_id = int(rows[0][1])
    old_status = str(rows[0][2] or "")
    if old_status != auth_core.MEASURE_STATUS_REVIEW:
        return _reject("措施 #{} 当前状态「{}」不可第一次分配(仅待评审可分配部门)".format(
            req.measure_id, old_status))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _execute(
            "UPDATE measures SET department = ?, assigned_by_admin = ?, "
            "assigned_manager = ?, updated_at = ? WHERE id = ?",
            [dept, operator, manager_name, now, req.measure_id])
        _execute(
            "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
            "review_result, review_comment, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [issue_id, req.measure_id, None, operator, "分配部门",
             "【第一次分配】部门={} 接收经理={}".format(dept, manager_name), now, now])
        # 同步问题 department(取第一条已分配部门措施所在部门, 仅展示)
        # ★ 合并为单条 SQL, 省一次远程往返
        _execute(
            "UPDATE issues SET department = ("
            "  SELECT department FROM measures WHERE issue_id = ? "
            "  AND department IS NOT NULL AND department <> '' "
            "  ORDER BY id ASC LIMIT 1), updated_at = ? WHERE id = ?",
            [issue_id, now, issue_id])
        _recompute_issue_status(issue_id)
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    return {"success": True, "measure_id": req.measure_id,
            "message": "措施 #{} 已分配到 {} 部门".format(req.measure_id, dept)}


class MeasureAssignRequest(BaseModel):
    """措施二次分配请求体(部门经理 → 本部门工程师)"""
    measure_id: int          # 措施id
    owner: str               # 工程师姓名
    due_date: str            # 预计完成 YYYY-MM-DD
    operator: str            # 操作人姓名(经理)


@app.post("/api/measure_assign")
def assign_measure(req: MeasureAssignRequest, sess: dict = Depends(_require_token)):
    """v3.1 QCS 两层分配·第二层: 部门经理把已采纳措施分配给本部门工程师。

    分配后, 该工程师才能在"我的措施"里看到并操作。权限: 仅 manager。
    """
    _acc = _auth.get_account(sess.get("username"))
    if not _acc:
        raise HTTPException(status_code=401, detail="账号不存在")
    if _acc.get("role") != auth_core.ROLE_MANAGER:
        return _reject("仅部门经理可二次分配")

    operator = (req.operator or "").strip()
    if not operator:
        return _reject("操作人姓名不能为空")
    if str((_acc or {}).get("display_name") or "").strip() != operator:
        return _reject("操作人必须是登录人本人, 不能代他人提交")
    owner = (req.owner or "").strip()
    due = (req.due_date or "").strip()
    if not owner:
        return _reject("工程师姓名不能为空")
    if not due or _parse_date(due) is None:
        return _reject("预计完成时间格式应为 YYYY-MM-DD")

    # ★ 校验: owner 必须是【本部门】的工程师账号(姓名与账号绑定)
    _owner_acc = None
    for _a in _auth.list_accounts():
        if str(_a.get("display_name") or "").strip() == owner or \
           str(_a.get("username") or "").strip() == owner:
            _owner_acc = _a
            break
    if not _owner_acc:
        return _reject("找不到姓名/账号为「{}」的用户".format(owner))
    if _owner_acc.get("role") != auth_core.ROLE_ENGINEER:
        return _reject("「{}」不是工程师, 不能作为措施责任人".format(owner))
    if str(_owner_acc.get("department") or "").strip().upper() != \
       str(_acc.get("department") or "").strip().upper():
        return _reject("「{}」不属于你的部门({}), 不能跨部门分配".format(
            owner, _acc.get("department")))
    owner = str(_owner_acc.get("display_name") or owner).strip()

    try:
        _, rows = _execute(
            "SELECT id, issue_id, status FROM measures WHERE id = ?", [req.measure_id])
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("措施 #{} 不存在".format(req.measure_id))
    issue_id = int(rows[0][1])
    old_status = str(rows[0][2] or "")
    if old_status != auth_core.MEASURE_STATUS_ADOPTED:
        return _reject("措施 #{} 当前状态「{}」不可二次分配(仅已采纳可指定工程师)".format(
            req.measure_id, old_status))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _execute(
            "UPDATE measures SET owner = ?, due_date = ?, assigned_by_manager = ?, "
            "status = ?, updated_at = ? WHERE id = ?",
            [owner, due, operator, auth_core.MEASURE_STATUS_PENDING, now, req.measure_id])
        _execute(
            "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
            "review_result, review_comment, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [issue_id, req.measure_id, None, operator, "分配工程师",
             "【第二次分配】工程师={} 预计完成={}".format(owner, due), now, now])
        _recompute_issue_status(issue_id)
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    return {"success": True, "measure_id": req.measure_id,
            "message": "措施 #{} 已分配给工程师 {}".format(req.measure_id, owner)}


# ============================================================
# v3.1 QCS: POST /api/measure_status — 更新措施状态(核心动作)
# ============================================================

class MeasureStatusRequest(BaseModel):
    """更新措施状态请求体(v3.1 QCS)"""
    measure_id: int                 # 措施id
    new_status: str                 # 待执行 / 执行中 / 已完成 / 已关闭
    operator: str                   # 操作人姓名(管理层用户)
    comment: str = ""               # 变更说明(完成说明/验证结论/退回原因)
    photo_path: str = None          # 完成证据照片(可选)


@app.post("/api/measure_status")
def update_status(req: MeasureStatusRequest,
                  sess: dict = Depends(_require_token)):
    """v3.1 QCS 核心动作: 更新措施状态。

    状态流转: 待执行 → 执行中 → 已完成 → 已关闭
      - 推进到终态(已完成/已关闭)→ 写 completed_at;
        完成日期晚于 due_date 时 completed_overdue=1(统计按期完成率)
      - 重开(终态 → 执行中)→ 清空完成信息, 且必须填原因
    每次变更写一条状态变更日志到 issue_reviews, 并立即重新汇总问题状态。

    权限:
      admin    不参与业务操作
      manager  可更新任何措施
      engineer 仅可更新【本部门】措施(越权拒绝)
    """
    _acc = _auth.get_account(sess.get("username"))
    if not _acc:
        raise HTTPException(status_code=401, detail="账号不存在")
    role = _acc.get("role")
    dept = _acc.get("department")

    operator = (req.operator or "").strip()
    if not operator:
        return _reject("操作人姓名不能为空")
    # 安全: 操作人必须是登录人本人(防冒用他人姓名提交)
    if str((_acc or {}).get("display_name") or "").strip() != operator:
        return _reject("操作人必须是登录人本人, 不能代他人提交")
    if role == auth_core.ROLE_ADMIN:
        return _reject("系统管理员不参与业务操作")
    if req.new_status not in auth_core.MEASURE_STATUSES:
        return _reject("非法状态, 必须是: " + "/".join(auth_core.MEASURE_STATUSES))

    # 查措施(含部门, 用于越权校验)
    try:
        _, rows = _execute(
            "SELECT m.id, m.status, m.department, m.due_date, m.issue_id "
            "FROM measures m WHERE m.id = ?", [req.measure_id])
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("措施 #{} 不存在".format(req.measure_id))
    old_status = str(rows[0][1] or "")
    m_dept = str(rows[0][2] or "").strip()
    due_date = rows[0][3]
    issue_id = int(rows[0][4])

    # 数据范围校验: engineer 只能操作本部门措施
    if role == auth_core.ROLE_ENGINEER:
        if not dept or m_dept != str(dept).strip():
            return _reject("无权操作其他部门的措施(本部门: {})".format(dept or "未配置"))

    # v3.1 QCS: "已完成 → 已关闭"是经理验证动作, 仅 manager 可执行(工程师只能标记"已完成")
    if (old_status == auth_core.MEASURE_STATUS_DONE
            and req.new_status == auth_core.MEASURE_STATUS_CLOSED
            and role != auth_core.ROLE_MANAGER):
        return _reject("措施验证关闭需经理操作(工程师只能标记为已完成)")

    # 重开(终态 → 非终态)必须填原因
    if (old_status in auth_core.MEASURE_FINAL_STATUSES
            and req.new_status not in auth_core.MEASURE_FINAL_STATUSES):
        if not (req.comment or "").strip():
            return _reject("退回/重开必须填写原因")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    completed_at = None
    completed_overdue = None
    if req.new_status in auth_core.MEASURE_FINAL_STATUSES:
        completed_at = now
        completed_overdue = 0
        d_due = _parse_date(due_date)
        if d_due is not None and _today_date() > d_due:
            completed_overdue = 1

    try:
        _execute(
            "UPDATE measures SET status = ?, completed_at = ?, completed_overdue = ?, "
            "updated_at = ? WHERE id = ?",
            [req.new_status, completed_at, completed_overdue, now, req.measure_id])
        # 留痕: issue_reviews 复用为"状态变更日志"(review_result 存新状态)
        _execute(
            "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
            "review_result, review_comment, photo_path, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [issue_id, req.measure_id, None, operator, req.new_status,
             req.comment or "", req.photo_path, now, now])
        _recompute_issue_status(issue_id)
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    return {"success": True,
            "message": "措施 #{} 状态已更新为「{}」".format(req.measure_id, req.new_status),
            "old_status": old_status, "new_status": req.new_status,
            "issue_id": issue_id, "completed_overdue": completed_overdue}


# ============================================================
# v3.1 QCS: GET /api/issues — 问题台账(按部门过滤)
# ============================================================

@app.get("/api/issues")
def list_issues(sess: dict = Depends(_require_token)):
    """问题台账(v3.1 QCS)。

    返回可见问题列表 + 每个问题的措施进度(已关闭数/总数, 只统计可见部门的措施)。
    数据范围与 /api/tasks 一致: manager 全部; engineer 仅含本部门措施的问题。
    """
    _acc = _auth.get_account(sess.get("username"))
    if not _acc:
        raise HTTPException(status_code=401, detail="账号不存在")
    role = _acc.get("role")
    dept = _acc.get("department")

    dept_sql, dept_args = _dept_filter(role, dept, alias="m",
                                       person_name=_acc.get("display_name"))

    # 问题 + 其下【可见】措施的进度(部门过滤放在 LEFT JOIN 的 ON 子句,
    # 保证统计口径与列表页一致: 工程师只统计本部门措施)
    # ★ JOIN 方式按角色区分 —— 这是"经理问题列表过滤失效"的根因所在:
    #   若统一用 LEFT JOIN, 过滤条件写在 ON 子句里【不起作用】:
    #   LEFT JOIN 会返回左表 issues 的所有行, 右表没匹配时只是计数为 0,
    #   于是经理照样看到全部问题(只是没有措施数据), 等于没过滤。
    #   因此:
    #     admin            → LEFT JOIN(看全部问题, 含尚无措施的问题)
    #     manager/engineer → INNER JOIN(只返回"有措施分配给自己"的问题)
    if role == auth_core.ROLE_ADMIN:
        join_sql = "LEFT JOIN measures m ON m.issue_id = i.id"
        join_args = []
    else:
        join_sql = "JOIN measures m ON m.issue_id = i.id AND (1=1 " + dept_sql + ")"
        join_args = dept_args

    try:
        cols, rows = _execute(
            "SELECT i.id, i.problem_description, i.work_station, i.issue_date, "
            "       i.department, i.status, i.issue_source, "
            "       COUNT(m.id) AS total_cnt, "
            "       SUM(CASE WHEN m.status = '已关闭' THEN 1 ELSE 0 END) AS closed_cnt "
            "FROM issues i " + join_sql + " "
            "GROUP BY i.id "
            "ORDER BY (i.created_at IS NULL) ASC, i.created_at ASC, i.id ASC",
            join_args)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    c = {n: i for i, n in enumerate(cols)}
    issues = []
    for idx, r in enumerate(rows):
        total = int(r[c["total_cnt"]] or 0)
        closed = int(r[c["closed_cnt"]] or 0)
        issues.append({
            "issue_no": idx + 1,          # 统一序号(与电脑端/移动端一致)
            "issue_id": r[c["id"]],
            "problem_description": r[c["problem_description"]] or "",
            "work_station": r[c["work_station"]] or "",
            "issue_date": r[c["issue_date"]] or "",
            "department": r[c["department"]] or "",
            "status": r[c["status"]] or "",
            "issue_source": r[c["issue_source"]] or "",
            "measure_total": total,
            "measure_closed": closed,
        })

    return {"success": True, "issues": issues, "stats": {"total": len(issues)}}


# ============================================================
# v3.1 QCS: GET /api/overdue — 逾期与即将到期
# ============================================================

@app.get("/api/overdue")
def list_overdue(sess: dict = Depends(_require_token),
                 within_days: int = Query(3, description="即将到期的天数窗口")):
    """逾期提醒(v3.1 QCS): 返回已逾期与即将到期的措施 + 统计(供移动端角标/看板)。
    逾期为实时计算, 不入库。"""
    _acc = _auth.get_account(sess.get("username"))
    if not _acc:
        raise HTTPException(status_code=401, detail="账号不存在")
    role = _acc.get("role")
    dept = _acc.get("department")

    empty = {"success": True, "overdue": [], "due_soon": [],
             "stats": {"overdue": 0, "due_soon": 0,
                       "by_department": {d: 0 for d in auth_core.DEPARTMENTS}}}
    dept_sql, dept_args = _dept_filter(role, dept, alias="m",
                                       person_name=_acc.get("display_name"))
    try:
        cols, rows = _execute(
            "SELECT m.id, m.issue_id, m.measure_content, m.owner, m.department, "
            "       m.due_date, m.status, "
            "       i.problem_description, i.work_station "
            "FROM measures m JOIN issues i ON m.issue_id = i.id "
            "WHERE 1=1 " + dept_sql +
            " ORDER BY (m.due_date IS NULL) ASC, m.due_date ASC",
            dept_args)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    c = {n: i for i, n in enumerate(cols)}
    overdue, due_soon = [], []
    by_dept = {d: 0 for d in auth_core.DEPARTMENTS}
    for r in rows:
        st = r[c["status"]] or ""
        due = r[c["due_date"]]
        o = {
            "measure_id": r[c["id"]],
            "issue_id": r[c["issue_id"]],
            "measure_content": r[c["measure_content"]] or "",
            "owner": r[c["owner"]] or "",
            "department": r[c["department"]] or "",
            "due_date": due or "",
            "status": st,
            "problem_description": r[c["problem_description"]] or "",
            "work_station": r[c["work_station"]] or "",
        }
        if _is_overdue(due, st):
            o["overdue_days"] = _overdue_days(due, st)
            overdue.append(o)
            if o["department"] in by_dept:
                by_dept[o["department"]] += 1
        elif _is_due_soon(due, st, within_days):
            d_due = _parse_date(due)
            o["days_left"] = (d_due - _today_date()).days if d_due else None
            due_soon.append(o)

    overdue.sort(key=lambda x: x["overdue_days"], reverse=True)
    return {"success": True, "overdue": overdue, "due_soon": due_soon,
            "stats": {"overdue": len(overdue), "due_soon": len(due_soon),
                      "by_department": by_dept}}
