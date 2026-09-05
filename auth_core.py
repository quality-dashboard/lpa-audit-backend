"""
auth_core.py - 认证核心工具(零依赖, 电脑端与移动端共用)

设计目的:
  电脑端(Streamlit / db.py)和移动端后端(FastAPI / api_server.py)都需要
  同一套账号体系与密码算法。但两者各自持有独立的 Turso 连接与 _execute,
  彼此不互相 import(避免 streamlit 依赖污染 FastAPI)。
  因此把"纯算法 + SQL 常量"抽到本文件, 两端各自 import 后用各自的
  _execute 执行 SQL, 保证:
    ① 密码哈希格式完全一致 → 账号在两端通用
    ② 表结构完全一致 → 无需维护两份 SQL
    ③ 零第三方依赖 → 只用标准库 hashlib / hmac / os / secrets / base64

密码算法: PBKDF2-HMAC-SHA256(标准库), 迭代 600000 次, 每账号独立随机盐。
哈希存储格式(单字段):
    pbkdf2_sha256$<迭代次数>$<盐_base64>$<哈希_base64>
"""
import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

# ============================================================
# 安全策略常量(与需求确认一致)
# ============================================================
PBKDF2_ITERATIONS = 600000   # OWASP 对 PBKDF2-HMAC-SHA256 的建议下限
SALT_BYTES = 16              # 盐长度(字节)
HASH_BYTES = 32              # 哈希长度(字节)

SESSION_TIMEOUT_HOURS = 8    # 会话超时: 8 小时(一个班次)
MAX_FAILED_ATTEMPTS = 5      # 密码错误次数上限
LOCK_MINUTES = 15            # 触发上限后锁定时长(分钟)

# 角色定义(v3.1 QCS: 三角色, 废弃原"普通员工"; 两层分配架构)
ROLE_ADMIN = "admin"        # 系统管理员: 账号管理 + 录入问题 + 第一次分配(按措施给部门)
ROLE_MANAGER = "manager"    # 部门经理: 本部门评审采纳 + 二次分配(给本部门工程师) + 验收关闭
ROLE_ENGINEER = "engineer"  # 工程师: 只看自己名下(owner)措施, 执行 + 上传证据
ROLE_EMPLOYEE = "employee"  # 【v3.0 已废弃】常量保留, 仅用于识别/迁移旧账号
ROLES = [ROLE_ADMIN, ROLE_MANAGER, ROLE_ENGINEER]

ROLE_LABELS = {
    ROLE_ADMIN: "系统管理员",
    ROLE_MANAGER: "经理",
    ROLE_ENGINEER: "工程师/主管",
    ROLE_EMPLOYEE: "普通员工(已废弃)",
}

# 部门定义(v3.1 QCS): P/Q/T 三类, 决定 engineer 的数据可见范围。
# admin 与 manager 的 department 为 NULL(表示不限部门)。
DEPT_P = "P"   # 制造/生产
DEPT_Q = "Q"   # 质量
DEPT_T = "T"   # 技术/工艺
DEPARTMENTS = [DEPT_P, DEPT_Q, DEPT_T]

DEPARTMENT_LABELS = {
    DEPT_P: "P 制造/生产",
    DEPT_Q: "Q 质量",
    DEPT_T: "T 技术/工艺",
}

# 措施状态(v3.1 QCS) —— 共 7 个(两层分配架构)
#   待评审 →(经理采纳)→ 已采纳 →(经理二次分配)→ 待执行 → 执行中 → 已完成 →(验证)→ 已关闭
#   待评审 →(经理不采纳)→ 不采纳 ──重新采纳──→ 已采纳
MEASURE_STATUS_REVIEW = "待评审"    # 导入/录入后的初始状态(admin 录入, 待第一次分配部门)
MEASURE_STATUS_ADOPTED = "已采纳"   # 经理采纳后、二次分配(指定工程师)前的中间态
MEASURE_STATUS_REJECTED = "不采纳"  # 经理评审拒绝, 保留记录, 可重新采纳
MEASURE_STATUS_PENDING = "待执行"   # 已二次分配(指定工程师), 等待执行
MEASURE_STATUS_DOING = "执行中"
MEASURE_STATUS_DONE = "已完成"      # 已填完成说明, 待经理验证(未闭环)
MEASURE_STATUS_CLOSED = "已关闭"    # 经理验证通过, 终态
MEASURE_STATUSES = [MEASURE_STATUS_REVIEW, MEASURE_STATUS_ADOPTED,
                    MEASURE_STATUS_REJECTED, MEASURE_STATUS_PENDING,
                    MEASURE_STATUS_DOING, MEASURE_STATUS_DONE,
                    MEASURE_STATUS_CLOSED]

# 终态: 进入这两个状态后不再计算逾期
MEASURE_FINAL_STATUSES = (MEASURE_STATUS_DONE, MEASURE_STATUS_CLOSED)
# ★ 参与逾期计算的状态(按需求: 只有"待执行"或"执行中"才计算逾期)。
#   待评审/已采纳/不采纳 尚未进入执行且通常无 due_date, 不计逾期。
MEASURE_OVERDUE_STATUSES = (MEASURE_STATUS_PENDING, MEASURE_STATUS_DOING)
# 已采纳并进入执行流程的状态。用于"问题是否可关闭"的汇总判断 ——
# 待评审(尚未采纳, 结果未定)与不采纳(明确不执行)都不参与关闭判断。
MEASURE_ACTIVE_STATUSES = (MEASURE_STATUS_ADOPTED, MEASURE_STATUS_PENDING,
                           MEASURE_STATUS_DOING, MEASURE_STATUS_DONE,
                           MEASURE_STATUS_CLOSED)

# 问题状态(v3.1 QCS): 由措施状态实时汇总, 不手工维护
ISSUE_STATUS_NOT_STARTED = "未开始"
ISSUE_STATUS_DOING = "进行中"
ISSUE_STATUS_CLOSED = "已关闭"
ISSUE_STATUSES = [ISSUE_STATUS_NOT_STARTED, ISSUE_STATUS_DOING, ISSUE_STATUS_CLOSED]

# 客户端类型
CLIENT_PC = "pc"
CLIENT_MOBILE = "mobile"
CLIENT_API = "api"


# ============================================================
# 密码哈希(PBKDF2, 零依赖)
# ============================================================

def hash_password(plain_password):
    """生成密码哈希字符串。
    返回格式: pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
    (盐与迭代次数都编码在内, 验证时按同一参数重算即可)"""
    salt = os.urandom(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        str(plain_password).encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=HASH_BYTES,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(plain_password, stored_hash):
    """校验密码。返回 True/False。
    用 hmac.compare_digest 做定长时间比较, 防止时序攻击。"""
    if not stored_hash or "$" not in stored_hash:
        return False
    try:
        algo, iterations, salt_b64, hash_b64 = stored_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            str(plain_password).encode("utf-8"),
            salt,
            int(iterations),
            dklen=len(expect),
        )
        return hmac.compare_digest(dk, expect)
    except Exception:
        return False


# ============================================================
# 会话 token
# ============================================================

def generate_token():
    """生成 URL-safe 随机 token(用于 Bearer 认证)"""
    return secrets.token_urlsafe(32)


def now_str():
    """当前时间字符串(YYYY-MM-DD HH:MM:SS)"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def expire_str(hours=SESSION_TIMEOUT_HOURS):
    """会话过期时刻字符串"""
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def lock_until_str(minutes=LOCK_MINUTES):
    """账号锁定截止时刻字符串"""
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def is_time_after(time_str, base=None):
    """判断 time_str 是否晚于 base(默认当前时间)。
    用于判断"锁定是否已过期": locked_until > now 表示仍在锁定中。
    空值返回 False(视为未锁定)。"""
    if not time_str:
        return False
    base = base or datetime.now()
    try:
        return datetime.strptime(str(time_str), "%Y-%m-%d %H:%M:%S") > base
    except (TypeError, ValueError):
        return False


# ============================================================
# 建表 SQL(幂等; 电脑端 init_db 与移动端后端 _migrate_db 共用)
# ============================================================

# v3.1 QCS: 新增 department(P/Q/T) —— engineer 的数据范围; admin/manager 为 NULL。
# 本 SQL 由电脑端(db.init_db)与移动端后端(api_server._migrate_db)共用。
# 注意: CREATE TABLE IF NOT EXISTS 对【已存在】的库不会补列, 因此两端还需各自
#       执行 ALTER TABLE 幂等补列(见 db.init_db 与 api_server._migrate_db)。
ACCOUNTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE,
    display_name         TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'engineer',
    department           TEXT,
    status               TEXT NOT NULL DEFAULT 'active',
    must_change_password INTEGER NOT NULL DEFAULT 1,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    created_at           TEXT,
    updated_at           TEXT,
    last_login_at        TEXT
)
"""

LOGIN_LOGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS login_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT,
    display_name TEXT,
    role         TEXT,
    client_type  TEXT,
    login_at     TEXT,
    logout_at    TEXT,
    ip           TEXT,
    user_agent   TEXT,
    result       TEXT,
    fail_reason  TEXT
)
"""

SESSIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL,
    client_type TEXT,
    login_at    TEXT,
    logout_at   TEXT,
    expire_at   TEXT NOT NULL,
    ip          TEXT,
    user_agent  TEXT
)
"""

# 索引(幂等)
AUTH_INDEX_SQLS = [
    "CREATE INDEX IF NOT EXISTS idx_sessions_expire ON sessions(expire_at)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions(username)",
    "CREATE INDEX IF NOT EXISTS idx_loginlogs_username ON login_logs(username)",
    "CREATE INDEX IF NOT EXISTS idx_loginlogs_loginat ON login_logs(login_at)",
]

# 旧数据回填: 把"问题第二责任人是否为空"固化到措施的 skip_level2
# (幂等, 只处理 skip_level2 IS NULL 的行)
BACKFILL_SKIP_LEVEL2_SQL = """
UPDATE measures
SET skip_level2 = CASE
    WHEN COALESCE(TRIM((
        SELECT i.responsible_person_2
        FROM issues i
        WHERE i.id = measures.issue_id
    )), '') = ''
    THEN 1
    ELSE 0
    END
WHERE skip_level2 IS NULL
"""


# ============================================================
# 认证服务(电脑端与移动端共用, 避免逻辑重复)
# ============================================================

def _rows_of(result):
    """统一 _execute 的返回值。
    电脑端 db.py 返回 _Result 对象(有 .rows);
    移动端 api_server.py 返回 (列名列表, 行列表) 元组。
    这里统一取出"行列表"。"""
    if hasattr(result, "rows"):
        return result.rows or []
    if isinstance(result, tuple) and len(result) == 2:
        return result[1] or []
    return []


_ACCOUNT_COLS = ("id", "username", "display_name", "password_hash", "role",
                 "department", "status", "must_change_password", "failed_attempts",
                 "locked_until", "created_at", "updated_at", "last_login_at")


def _row_to_account(row):
    """行元组 → 账号字典"""
    return dict(zip(_ACCOUNT_COLS, row))


class AuthService:
    """认证服务。构造时传入各自进程的 execute 函数, 从而同时服务
    电脑端(Streamlit / db.py)与移动端后端(FastAPI / api_server.py),
    保证两端账号、密码、会话、日志逻辑完全一致。"""

    def __init__(self, execute_fn):
        self._execute = execute_fn

    # ---------- 账号 ----------

    def get_account(self, username):
        """按账号名查账号, 返回字典; 不存在返回 None"""
        res = self._execute(
            "SELECT id, username, display_name, password_hash, role, department, "
            "status, must_change_password, failed_attempts, locked_until, created_at, "
            "updated_at, last_login_at FROM accounts WHERE username = ?",
            [str(username).strip()])
        rows = _rows_of(res)
        return _row_to_account(rows[0]) if rows else None

    def list_accounts(self):
        """列出全部账号(管理员用), 不含密码哈希"""
        res = self._execute(
            "SELECT id, username, display_name, role, department, status, "
            "must_change_password, locked_until, created_at, last_login_at "
            "FROM accounts ORDER BY id ASC")
        out = []
        for r in _rows_of(res):
            out.append({
                "id": r[0], "username": r[1], "display_name": r[2],
                "role": r[3], "department": r[4], "status": r[5],
                "must_change_password": r[6],
                "locked_until": r[7], "created_at": r[8], "last_login_at": r[9],
            })
        return out

    def create_account(self, username, display_name, plain_password,
                       role=ROLE_EMPLOYEE, must_change=1):
        """新建账号。返回 (成功与否, 提示信息)"""
        username = str(username or "").strip()
        display_name = str(display_name or "").strip()
        if not username:
            return False, "账号不能为空"
        if not display_name:
            display_name = username
        if role not in ROLES:
            return False, "角色无效"
        if self.get_account(username):
            return False, "账号已存在"
        now = now_str()
        try:
            self._execute(
                "INSERT INTO accounts (username, display_name, password_hash, role, "
                "status, must_change_password, failed_attempts, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [username, display_name, hash_password(plain_password), role,
                 "active", 1 if must_change else 0, 0, now, now])
        except Exception as e:
            return False, "创建失败: {}".format(e)
        return True, "账号已创建"

    def change_password(self, username, new_plain_password):
        """修改密码, 并清除强制改密标志与锁定状态"""
        if not str(new_plain_password or ""):
            return False, "新密码不能为空"
        now = now_str()
        self._execute(
            "UPDATE accounts SET password_hash = ?, must_change_password = 0, "
            "failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE username = ?",
            [hash_password(new_plain_password), now, str(username).strip()])
        return True, "密码已更新"

    def set_account_status(self, username, status):
        """启用 / 禁用账号"""
        self._execute(
            "UPDATE accounts SET status = ?, updated_at = ? WHERE username = ?",
            [status, now_str(), str(username).strip()])
        return True, "状态已更新"

    def set_role(self, username, role):
        """修改账号角色(管理员用)。返回 (成功与否, 提示信息)。

        role 必须属于 ROLES, 非法值直接拒绝(不落库)。
        """
        if role not in ROLES:
            return False, "角色无效"
        self._execute(
            "UPDATE accounts SET role = ?, updated_at = ? WHERE username = ?",
            [role, now_str(), str(username).strip()])
        return True, "角色已更新为「{}」".format(ROLE_LABELS.get(role, role))

    def set_display_name(self, username, display_name):
        """修改账号姓名(display_name)。返回 (成功与否, 提示信息)。

        ★ 重要: 该姓名必须与 issues 表里的责任人姓名完全一致 ——
        系统按 display_name == issues.responsible_person_N 匹配个人任务,
        姓名对不上则该用户登录后看不到任何任务。
        """
        name = str(display_name or "").strip()
        if not name:
            return False, "姓名不能为空"
        self._execute(
            "UPDATE accounts SET display_name = ?, updated_at = ? WHERE username = ?",
            [name, now_str(), str(username).strip()])
        return True, "姓名已更新为「{}」".format(name)

    def set_department(self, username, department):
        """修改账号部门(P/Q/T)。返回 (成功与否, 提示信息)。

        v3.1 QCS: department 决定 engineer 的数据可见范围;
        admin/manager 传 None(不限部门)。空值按 None 处理。"""
        dept = str(department or "").strip()
        val = dept if dept else None
        self._execute(
            "UPDATE accounts SET department = ?, updated_at = ? WHERE username = ?",
            [val, now_str(), str(username).strip()])
        return True, "部门已更新"

    def admin_reset_password(self, username, new_plain_password):
        """管理员重置他人密码。返回 (成功与否, 提示信息)。

        与 change_password(本人改密)的区别:
          change_password      → must_change_password = 0 (本人已知新密码)
          admin_reset_password → must_change_password = 1 (强制下次登录改密)
        同时清除失败计数与锁定状态, 避免重置后仍被锁。
        """
        if not str(new_plain_password or ""):
            return False, "新密码不能为空"
        self._execute(
            "UPDATE accounts SET password_hash = ?, must_change_password = 1, "
            "failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE username = ?",
            [hash_password(new_plain_password), now_str(), str(username).strip()])
        return True, "密码已重置(该用户下次登录需修改密码)"

    def delete_account(self, username):
        """删除账号主体。返回 (成功与否, 提示信息)。

        规则:
          - admin 账号不允许删除(防止系统失去唯一管理员)
          - 删除该账号的 sessions —— 若其当前在线, 立即失效(被踢下线)
          - ★ 保留 login_logs 登录日志(日志按 username 记录, 与 accounts 无外键),
            保证审计轨迹不断链
        """
        username = str(username or "").strip()
        if not username:
            return False, "账号不能为空"
        acc = self.get_account(username)
        if not acc:
            return False, "账号不存在"
        if acc.get("role") == ROLE_ADMIN:
            return False, "admin 账号不允许删除"

        # 先清会话(在线立即失效)
        self._execute("DELETE FROM sessions WHERE username = ?", [username])
        # 再删账号主体(login_logs 保留)
        self._execute("DELETE FROM accounts WHERE username = ?", [username])
        return True, "账号已删除"

    # ---------- 登录(含失败计数与锁定) ----------

    def authenticate(self, username, password, client_type=CLIENT_PC,
                     ip="", user_agent=""):
        """登录校验。返回 (成功与否, 账号字典或None, 提示信息)。
        流程: 查账号 → 判锁定 → 判启用 → 校验密码 → 更新计数 → 写登录日志。"""
        username = str(username or "").strip()
        acc = self.get_account(username)

        def _fail(reason):
            # 失败: 累加错误次数, 达上限则锁定
            if acc:
                attempts = int(acc.get("failed_attempts") or 0) + 1
                if attempts >= MAX_FAILED_ATTEMPTS:
                    self._execute(
                        "UPDATE accounts SET failed_attempts = ?, locked_until = ?, "
                        "updated_at = ? WHERE username = ?",
                        [attempts, lock_until_str(), now_str(), username])
                else:
                    self._execute(
                        "UPDATE accounts SET failed_attempts = ?, updated_at = ? "
                        "WHERE username = ?",
                        [attempts, now_str(), username])
            self._write_login_log(username,
                                  acc.get("display_name") if acc else "",
                                  acc.get("role") if acc else "",
                                  client_type, "failed", reason, ip, user_agent)
            return False, None, reason

        if not acc:
            return _fail("账号不存在")
        if is_time_after(acc.get("locked_until")):
            left = acc.get("locked_until")
            return _fail("账号已锁定, 请在 {} 后重试".format(left))
        if str(acc.get("status")) != "active":
            return _fail("账号已禁用")

        if not verify_password(password, acc.get("password_hash")):
            attempts_left = MAX_FAILED_ATTEMPTS - int(acc.get("failed_attempts") or 0) - 1
            if attempts_left > 0:
                return _fail("密码错误, 还可尝试 {} 次".format(attempts_left))
            return _fail("密码错误次数过多, 账号已锁定 {} 分钟".format(LOCK_MINUTES))

        # 成功: 清零计数与锁定, 更新最近登录时间, 写日志
        self._execute(
            "UPDATE accounts SET failed_attempts = 0, locked_until = NULL, "
            "last_login_at = ?, updated_at = ? WHERE username = ?",
            [now_str(), now_str(), username])
        self._write_login_log(username, acc.get("display_name"), acc.get("role"),
                              client_type, "success", "", ip, user_agent)
        return True, acc, "登录成功"

    def _write_login_log(self, username, display_name, role, client_type,
                         result, fail_reason, ip, user_agent):
        """写登录日志(成功/失败都记一条, logout_at 留空待退出时更新)"""
        self._execute(
            "INSERT INTO login_logs (username, display_name, role, client_type, "
            "login_at, logout_at, ip, user_agent, result, fail_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [username, display_name, role, client_type, now_str(), None,
             ip, user_agent, result, fail_reason])

    # ---------- 会话 ----------

    def create_session(self, username, role, client_type=CLIENT_PC,
                       ip="", user_agent=""):
        """创建会话, 返回 token"""
        token = generate_token()
        self._execute(
            "INSERT INTO sessions (token, username, role, client_type, login_at, "
            "logout_at, expire_at, ip, user_agent) VALUES (?,?,?,?,?,?,?,?,?)",
            [token, username, role, client_type, now_str(), None,
             expire_str(), ip, user_agent])
        return token

    def validate_token(self, token):
        """校验 token。返回 (有效与否, 会话字典或None, 提示信息)。
        过期或未退出检查: now 必须早于 expire_at, 且 logout_at 为空。"""
        if not token:
            return False, None, "缺少 token"
        res = self._execute(
            "SELECT id, token, username, role, client_type, login_at, logout_at, "
            "expire_at, ip, user_agent FROM sessions WHERE token = ?", [token])
        rows = _rows_of(res)
        if not rows:
            return False, None, "token 无效"
        r = rows[0]
        sess = {
            "id": r[0], "token": r[1], "username": r[2], "role": r[3],
            "client_type": r[4], "login_at": r[5], "logout_at": r[6],
            "expire_at": r[7], "ip": r[8], "user_agent": r[9],
        }
        if sess.get("logout_at"):
            return False, None, "会话已退出"
        # expire_at > now 才有效
        if not is_time_after(sess.get("expire_at")):
            # 已过期: 补记退出时间
            self._execute(
                "UPDATE sessions SET logout_at = ? WHERE token = ?",
                [now_str(), token])
            return False, None, "会话已过期, 请重新登录"
        return True, sess, "有效"

    def logout(self, token):
        """退出: 更新 sessions.logout_at 与对应 login_logs.logout_at"""
        ok, sess, _ = self.validate_token(token)
        if not ok:
            # 已过期/已退出的 token 也尝试标记, 保证幂等
            self._execute(
                "UPDATE sessions SET logout_at = COALESCE(logout_at, ?) WHERE token = ?",
                [now_str(), token])
            return False, "会话无效或已退出"
        now = now_str()
        self._execute("UPDATE sessions SET logout_at = ? WHERE token = ?",
                      [now, token])
        # 把该账号最近一条"未记录退出"的成功登录日志补上退出时间
        self._execute(
            "UPDATE login_logs SET logout_at = ? "
            "WHERE username = ? AND result = 'success' AND logout_at IS NULL",
            [now, sess.get("username")])
        return True, "已退出"

    def logout_pc(self, username):
        """电脑端(Streamlit)退出: 补记该账号最近一次未退出的成功登录日志的退出时间。
        电脑端用 st.session_state 管登录态、没有 token, 故与 logout(token) 分开。"""
        self._execute(
            "UPDATE login_logs SET logout_at = ? "
            "WHERE username = ? AND result = 'success' AND logout_at IS NULL",
            [now_str(), str(username).strip()])
        return True

    def cleanup_expired_sessions(self):
        """清理过期会话(补记退出时间), 供启动时调用"""
        self._execute(
            "UPDATE sessions SET logout_at = ? "
            "WHERE logout_at IS NULL AND expire_at <= ?",
            [now_str(), now_str()])

    # ---------- 初始化 ----------

    def init_default_admin(self):
        """初始化默认管理员账号(仅当不存在任何账号时执行)。
        临时密码随机生成, 首次登录强制改密。
        返回 (是否新建, 账号名, 临时密码或None)"""
        res = self._execute("SELECT COUNT(*) FROM accounts")
        rows = _rows_of(res)
        count = int(rows[0][0]) if rows and rows[0][0] is not None else 0
        if count > 0:
            return False, "", None
        temp_pw = secrets.token_urlsafe(10)
        ok, msg = self.create_account(
            "admin", "系统管理员", temp_pw, role=ROLE_ADMIN, must_change=1)
        return ok, "admin", temp_pw if ok else None
