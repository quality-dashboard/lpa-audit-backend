"""
db.py - Turso 云数据库操作层
车间问题分层审核与跟踪系统 (LPA)

v2.0 措施级重构: 系统从"问题级"流转升级为"措施级"流转。
================================================================
表结构(三张表, 关系: 一个问题 → 多条措施 → 每条措施多段审核流水):
  1. issues 表        —— 问题主表。一个现场问题一条记录, 保存问题基本信息
                         (描述/工位/三层责任人) 和"当前状态"。
                         ★ v2.0: 状态由措施实时汇总, 不再人工维护。
  2. measures 表      —— 措施表(v2.0新增)。一个问题可拆成多条改善措施,
                         每条措施独立评审、独立走三层审核、独立关闭。
  3. issue_reviews 表 —— 审核流水表。保存措施级的评审决策(采纳/不采纳)
                         与分层审核(完全符合/完全不符合/部分符合)全轨迹。
                         ★ v2.0: 新增 measure_id 字段关联措施。

措施状态机(v2.0):
  措施.status ∈ {待评审, 已评审, 已退回, 已关闭}
    待评审: 问题刚登记, 措施尚未被第三责任人评审(采纳/不采纳)
    已评审: 措施已被采纳, 正在分层审核流程中
    已退回: 措施在某层被判"完全不符合", 打回第一责任人重新整改
    已关闭: 第三责任人"完全符合", 措施整改闭环
  措施.is_adopted: NULL=待评审 / 1=采纳 / 0=不采纳

问题状态由措施实时汇总(v2.0, 见 recompute_issue_status):
    无任何 is_adopted=1 的措施                → 未评审
    所有 is_adopted=1 的措施 status='已关闭'  → 已关闭
    其余情况(有采纳措施但未全部关闭)          → 已评审

Turso HTTP API 连接方式与之前版本完全一致(urllib 直调 /v2/pipeline)。
"""
import json
import urllib.request
from datetime import datetime

import pandas as pd
import streamlit as st

import auth_core  # v2.5 认证核心(PBKDF2/token/建表SQL, 零依赖, 与移动端后端共用)

# ============================================================
# ★★★ Turso 数据库连接配置区（在这里填你自己的库） ★★★
# ============================================================
# 1. TURSO_URL: 换成你在 Turso 控制台创建的数据库URL (libsql:// 开头)
# 2. Token 两种填法任选其一:
#    a) 直接粘贴到下面的 TURSO_AUTH_TOKEN = "你的token" (本地快速试用)
#    b) 留空, 填到 .streamlit/secrets.toml 里的 LPA_TURSO_TOKEN (推荐,
#       以后项目推送到Git时Token不会泄露)
TURSO_URL = "libsql://lpaauditsystem-quality-dashboard.aws-ap-northeast-1.turso.io"
# 令牌不写在代码里(防止推送到Git时泄露), 统一从 .streamlit/secrets.toml 读取
TURSO_AUTH_TOKEN = ""

# Token留空时, 自动尝试从 Streamlit secrets 读取
if not TURSO_AUTH_TOKEN:
    try:
        TURSO_AUTH_TOKEN = st.secrets["LPA_TURSO_TOKEN"]
    except Exception:
        TURSO_AUTH_TOKEN = ""
# ============================================================


# ---------- 建表SQL ----------
# 问题主表: status 只保留三种(v2.0): 未评审 / 已评审 / 已关闭, 由措施实时汇总。
# 不再使用 improvement_action 作为唯一措施字段(措施已拆分到 measures 表)。
# is_adopted 字段(v0.8遗留)保留但弃用 —— 采纳决策已下移到 measures 表。
# issue_source: 问题来源(v2.1新增) —— 监造提出 / 过程改善发现
# v2.3 新增监造问题清单字段: part_no(零件) / model(型号) / problem_level(问题等级)
#      / defect_quantity(数量) / reason_analysis(原因分析) / supervisor_name(监造师)
_ISSUES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_description TEXT NOT NULL,
    issue_date TEXT,
    work_station TEXT,
    improvement_action TEXT,
    issue_source TEXT,
    part_no TEXT,
    model TEXT,
    problem_level TEXT,
    defect_quantity INTEGER,
    reason_analysis TEXT,
    supervisor_name TEXT,
    responsible_person_1 TEXT,
    responsible_person_2 TEXT,
    responsible_person_3 TEXT,
    status TEXT DEFAULT '未评审',
    is_adopted INTEGER,
    created_at TEXT,
    updated_at TEXT
)
"""

# 措施表(v2.0新增): 一个问题可拆成多条措施, 每条独立走评审+三层审核。
# is_adopted: NULL=待评审 / 1=采纳 / 0=不采纳
# status: 待评审 / 已评审 / 已退回 / 已关闭
# measure_order: 措施在问题内的序号(1,2,3...), 用于展示排序, 可选
_MEASURES_TABLE_SQL = """
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
"""

# 审核流水表(v2.0改造):
#   measure_id  —— v2.0新增, 关联 measures.id(审核对象是措施)
#   issue_id    —— 保留, 冗余存问题编号便于"历史同时显示问题信息"联查/回填
#   review_level—— 1=第一责任人 / 2=第二责任人 / 3=第三责任人
#   review_result —— v2.0语义: 分层审核=完全符合/完全不符合/部分符合;
#                    评审决策=采纳/不采纳(由第三责任人在措施评审环节产生)
#   photo_path  —— 整改照片相对路径(v1.1, 仅"完全符合"时上传, 只存磁盘不存库)
_REVIEWS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS issue_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL,
    measure_id INTEGER,
    review_level INTEGER,
    reviewer TEXT,
    review_result TEXT,
    review_comment TEXT,
    photo_path TEXT,
    reviewed_at TEXT,
    created_at TEXT
)
"""


class _Result:
    """Turso返回结果的简单包装(列名 + 行数据)"""

    def __init__(self, columns, rows, affected_row_count):
        self.columns = columns
        self.rows = rows
        self.affected_row_count = affected_row_count


def _check_config():
    """连接信息未填写时, 给出明确的中文提示(避免看不懂的HTTP报错)"""
    if "在这里填" in TURSO_URL or not TURSO_AUTH_TOKEN:
        raise RuntimeError(
            "Turso数据库尚未配置: 请打开 db.py 顶部配置区填写 TURSO_URL, "
            "并把Token填到 TURSO_AUTH_TOKEN 或 .streamlit/secrets.toml 的 "
            "LPA_TURSO_TOKEN"
        )


def _get_http_url():
    """把 libsql:// 地址转换成 Turso HTTP API 地址"""
    url = TURSO_URL
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    if url.endswith("/"):
        url = url[:-1]
    return url + "/v2/pipeline"


def _to_turso_arg(value):
    """参数类型转换 —— Turso API 规则: 整数传字符串, 浮点传数字, 文本传字符串"""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _from_turso_value(val):
    """把Turso返回的JSON值转回Python值"""
    if val is None:
        return None
    t = val.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(val.get("value", 0))
    if t == "float":
        return float(val.get("value", 0.0))
    if t == "text":
        return val.get("value", "")
    return val.get("value")


def _parse_result(result_json):
    """解析Turso /v2/pipeline 返回的JSON"""
    results = result_json.get("results", [])
    if not results:
        return _Result([], [], 0)
    first = results[0]
    if first.get("type") == "error":
        err = first.get("error", {})
        msg = err.get("message", "未知错误")
        raise RuntimeError("Turso错误: " + msg)
    response = first.get("response", {})
    if response.get("type") != "execute":
        return _Result([], [], 0)
    res = response.get("result", {})
    cols = [c.get("name", "") for c in res.get("cols", [])]
    raw_rows = res.get("rows", [])
    rows = [[_from_turso_value(v) for v in r] for r in raw_rows]
    affected = res.get("affected_row_count", 0)
    return _Result(cols, rows, affected)


def _execute(sql, args=None):
    """执行单条SQL(可带参数), 返回 _Result"""
    _check_config()
    turso_args = [_to_turso_arg(a) for a in args] if args else []
    body = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": turso_args}},
            {"type": "close"},
        ]
    }
    req = urllib.request.Request(
        _get_http_url(),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer {}".format(TURSO_AUTH_TOKEN),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result_json = json.loads(resp.read().decode("utf-8"))
    return _parse_result(result_json)


# ============================================================
# 对外接口: 建表 / 迁移 / 清空测试数据
# ============================================================

_DB_INIT_DONE = False  # 进程内"已建表"标记: 页面每次rerun不重复连库建表


def init_db():
    """建表 + 迁移(幂等, 重复执行不报错)。
    进程内只真正执行一次 —— 避免Streamlit每次页面刷新都连两次Turso。
    v2.0: 新增 measures 表; 为 issue_reviews 补 measure_id 列。
    v2.5: 新增认证三张表(accounts/login_logs/sessions)。
    v3.1 QCS: 新增部门/责任人/时间字段; 移除 skip_level2 回填(三层审核已废弃)。"""
    global _DB_INIT_DONE
    if _DB_INIT_DONE:
        return
    _execute(_ISSUES_TABLE_SQL)
    _execute(_MEASURES_TABLE_SQL)
    _execute(_REVIEWS_TABLE_SQL)
    # v2.5 认证相关表(与移动端后端共用同一份 SQL)
    _execute(auth_core.ACCOUNTS_TABLE_SQL)
    _execute(auth_core.LOGIN_LOGS_TABLE_SQL)
    _execute(auth_core.SESSIONS_TABLE_SQL)
    for _idx_sql in auth_core.AUTH_INDEX_SQLS:
        _execute(_idx_sql)

    # ---- v3.1 QCS 字段(幂等补列, 已存在则跳过) ----
    # accounts.department: P/Q/T, 决定 engineer 的数据范围; admin/manager 为 NULL
    _ensure_column("accounts", "department", "TEXT")
    # measures: 措施责任人 / 部门 / 预计完成 / 实际完成 / 完成时是否逾期
    _ensure_column("measures", "owner", "TEXT")
    _ensure_column("measures", "department", "TEXT")
    _ensure_column("measures", "due_date", "TEXT")
    _ensure_column("measures", "completed_at", "TEXT")
    _ensure_column("measures", "completed_overdue", "INTEGER")
    # v3.1 两层分配留痕: 管理员第一次分配 / 经理第二次分配
    _ensure_column("measures", "assigned_by_admin", "TEXT")
    _ensure_column("measures", "assigned_by_manager", "TEXT")
    # 被指派的部门经理姓名(管理员第一次分配时指定, 不得越级分到工程师)
    _ensure_column("measures", "assigned_manager", "TEXT")
    # issues: 问题部门
    _ensure_column("issues", "department", "TEXT")

    # ---- 历史遗留字段: 仅补列保持兼容, 已无业务含义 ----
    # v2.5 skip_level2: 三层审核已废弃, 列保留以免残留代码报错。
    #   ★ 不再执行 BACKFILL_SKIP_LEVEL2_SQL —— QCS 下 responsible_person_2 恒为空,
    #     回填会给所有措施写入 skip_level2=1 的无意义值。
    _ensure_column("measures", "skip_level2", "INTEGER")
    _ensure_column("issues", "is_adopted", "INTEGER")       # v0.8遗留迁移
    _ensure_column("issues", "issue_source", "TEXT")        # v2.1问题来源迁移
    _ensure_column("issues", "part_no", "TEXT")             # v2.3 零件
    _ensure_column("issues", "model", "TEXT")               # v2.3 型号
    _ensure_column("issues", "problem_level", "TEXT")       # v2.3 问题等级
    _ensure_column("issues", "defect_quantity", "INTEGER")  # v2.3 数量
    _ensure_column("issues", "reason_analysis", "TEXT")     # v2.3 原因分析
    _ensure_column("issues", "supervisor_name", "TEXT")     # v2.3 监造师
    _ensure_column("issue_reviews", "photo_path", "TEXT")    # v1.1照片路径迁移
    _ensure_column("issue_reviews", "measure_id", "INTEGER")  # v2.0措施关联迁移

    # v3.1 两层分配数据迁移(幂等): 旧"待执行"但未分配部门/工程师的措施
    # → 迁移为"已采纳"(待二次分配)。已分配部门+工程师的保持"待执行"不动。
    _execute(
        "UPDATE measures SET status = ? WHERE status = ? "
        "AND (department IS NULL OR department = '' OR owner IS NULL OR owner = '')",
        [auth_core.MEASURE_STATUS_ADOPTED, auth_core.MEASURE_STATUS_PENDING])

    _DB_INIT_DONE = True


def _ensure_column(table, column, col_type):
    """确保指定表存在指定列(幂等迁移)。SQLite 用 PRAGMA table_info 查列名,
    缺列时 ALTER TABLE ADD COLUMN 补上; 已有则跳过。"""
    result = _execute("PRAGMA table_info({})".format(table))
    existing_cols = [row[1] for row in result.rows]
    if column not in existing_cols:
        _execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, col_type))


def clear_all_test_data():
    """★ v2.0 重构专用: 清空 issues / measures / issue_reviews 三张表的测试数据。
    旧数据全部作废(问题级→措施级不兼容), 从零开始。
    说明: 此函数不会自动执行, 需手动调用(见交付说明的"清空测试数据"步骤)。"""
    # 先删子表再删主表(虽然SQLite不强制外键, 按依赖顺序删更稳妥)
    _execute("DELETE FROM issue_reviews")
    _execute("DELETE FROM measures")
    _execute("DELETE FROM issues")
    _clear_issues_cache()
    _clear_measures_cache()
    _clear_reviews_cache()


# ============================================================
# 问题(issues)读写
# ============================================================

@st.cache_data(ttl=3, show_spinner=False)
def _load_issues_cached():
    """真实查询Turso的问题缓存层。
    ★ TTL=30秒: 移动端(api_server)写库走独立进程, 无法触发本进程缓存清空,
      30秒自动过期保证电脑端能较快看到手机端的最新操作结果。
    ★ 全系统统一问题序号(v2.2): 按 created_at 升序(相同再按 id 升序)排序后,
      从 1 开始生成连续"序号"列 —— 所有展示问题的地方都用这列, 不显示数据库id。
      (created_at 为空的问题排最后, 仍按 id 升序兜底)"""
    result = _execute(
        "SELECT * FROM issues "
        "ORDER BY (created_at IS NULL) ASC, created_at ASC, id ASC")
    df = pd.DataFrame(result.rows, columns=result.columns)
    if not df.empty:
        df.insert(0, "序号", range(1, len(df) + 1))
    return df


def load_all_issues():
    """加载全部问题(按id升序, 第一列为全局连续"序号"); 失败返回空表"""
    try:
        return _load_issues_cached()
    except Exception as e:
        print("load_all_issues error:", e)
        return pd.DataFrame()


def get_all_person_names():
    """v2.4: 从 issues 表动态读取所有非空责任人姓名, 去重排序。
    来源: responsible_person_1 / responsible_person_2 / responsible_person_3
    三个字段的并集; 用于"当前操作人"下拉框的动态选项(不再用 config 固定名单)。
    数据库无责任人时返回空列表(下拉框为空, 不报错)。"""
    try:
        issues = load_all_issues()
    except Exception:
        return []
    names = set()
    for col in ("responsible_person_1", "responsible_person_2", "responsible_person_3"):
        if col not in issues.columns:
            continue
        for v in issues[col].dropna().tolist():
            s = str(v).strip()
            if s:
                names.add(s)
    return sorted(names)


def get_person_names_by_level(level):
    """v2.4: 按层级获取该层责任人姓名(从 issues 表对应字段去重排序)。
    参数 level: 1/2/3 → responsible_person_1/2/3。
    用于分析看板图6 的"层级→人员"联动下拉框(动态, 不再用 config 固定名单)。"""
    col = {1: "responsible_person_1", 2: "responsible_person_2", 3: "responsible_person_3"}.get(level)
    if not col:
        return []
    try:
        issues = load_all_issues()
    except Exception:
        return []
    if col not in issues.columns:
        return []
    names = set()
    for v in issues[col].dropna().tolist():
        s = str(v).strip()
        if s:
            names.add(s)
    return sorted(names)


def _clear_issues_cache():
    """清空问题列表缓存(增删改问题后调用)"""
    try:
        _load_issues_cached.clear()
    except Exception:
        pass


def save_issue_with_measures(problem_description, issue_date, work_station,
                             responsible_person_1, responsible_person_2,
                             responsible_person_3, measure_contents,
                             issue_source="监造提出",
                             part_no="", model="", problem_level="",
                             defect_quantity=None, reason_analysis="",
                             supervisor_name=""):
    """v2.0: 新增问题 + 其下多条措施(一次提交, 数据一致)。
    参数:
      problem_description - 问题描述
      issue_date          - 发现日期(YYYY-MM-DD字符串)
      work_station        - 工位
      responsible_person_1/2/3 - 三层责任人姓名(可为空 → 问题进入"待分配")
      measure_contents    - 措施内容列表(list[str]), 至少一条(由调用方校验)
      issue_source        - 问题来源(v2.1新增, 默认"监造提出")
      part_no/model/problem_level/defect_quantity/reason_analysis/supervisor_name
                          - v2.3 监造问题清单字段(均可空)
    流程: ① 插入 issues(状态固定'未评审')
          ② 逐条插入 measures(is_adopted=NULL待评审, status='待评审')
          ③ 清缓存
    返回: 新问题的 id。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "INSERT INTO issues (problem_description, issue_date, work_station, "
        "issue_source, part_no, model, problem_level, defect_quantity, "
        "reason_analysis, supervisor_name, "
        "responsible_person_1, responsible_person_2, responsible_person_3, "
        "status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [problem_description, issue_date, work_station, issue_source,
         part_no, model, problem_level, defect_quantity,
         reason_analysis, supervisor_name,
         responsible_person_1, responsible_person_2, responsible_person_3,
         "未评审", now, now],
    )
    # 取新问题id: 用 MAX(id)(单用户写入场景安全; 也可用 last_insert_rowid 但
    # Turso HTTP API 每个 execute 独立, 拿不到 last_insert_rowid, 故用 MAX)
    res = _execute("SELECT MAX(id) FROM issues")
    issue_id = int(res.rows[0][0]) if res.rows and res.rows[0][0] is not None else 0

    # 逐条插入措施(measure_order 从1开始编号)
    for idx, content in enumerate(measure_contents):
        _execute(
            "INSERT INTO measures (issue_id, measure_content, measure_order, "
            "is_adopted, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            [issue_id, content, idx + 1, None, "待评审", now, now],
        )
    _clear_issues_cache()
    _clear_measures_cache()
    return issue_id


def save_issue_with_measures_pcs(problem_description, issue_date, work_station,
                                 measures,
                                 issue_source="监造提出",
                                 part_no="", model="", problem_level="",
                                 defect_quantity=None, reason_analysis="",
                                 supervisor_name=""):
    """v3.1 QCS: 新增问题 + 其下多条措施(一次提交, 数据一致)。

    参数:
      problem_description - 问题描述
      issue_date          - 发现日期(YYYY-MM-DD字符串)
      work_station        - 工位
      measures            - 措施内容列表 [content, ...]
      issue_source 等      - 问题属性字段(均可空)
    流程: ① 插入 issues(状态'未开始', department 留空 —— 由后续第一条被
             分配措施所在部门决定, 仅展示)
          ② 逐条插入 measures(状态'待评审'; owner/department/due_date 留空,
             待经理采纳后再分配部门/责任人/预计完成时间)
    返回: 新问题的 id。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "INSERT INTO issues (problem_description, issue_date, work_station, "
        "issue_source, part_no, model, problem_level, "
        "defect_quantity, reason_analysis, supervisor_name, "
        "status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [problem_description, issue_date, work_station,
         issue_source, part_no, model, problem_level, defect_quantity,
         reason_analysis, supervisor_name,
         auth_core.ISSUE_STATUS_NOT_STARTED, now, now],
    )
    res = _execute("SELECT MAX(id) FROM issues")
    issue_id = int(res.rows[0][0]) if res.rows and res.rows[0][0] is not None else 0

    for idx, content in enumerate(measures):
        _execute(
            "INSERT INTO measures (issue_id, measure_content, measure_order, "
            "owner, department, due_date, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [issue_id, content, idx + 1, None, None, None,
             auth_core.MEASURE_STATUS_REVIEW, now, now],
        )
    _clear_issues_cache()
    _clear_measures_cache()
    return issue_id


def adopt_measure_pcs(measure_id, decision, reviewer):
    """v3.1 QCS: 经理对某条措施做"采纳/不采纳"评审。

    参数:
      measure_id - 措施id
      decision   - "adopt"(采纳) / "reject"(不采纳)
      reviewer   - 评审人姓名(经理)
    规则:
      采纳   → status: 待评审 → 待执行(部门待分配, department 仍空)
      不采纳 → status: 待评审 → 不采纳(保留记录, 可重新采纳)
    每次评审写一条状态变更日志(review_result='采纳'/'不采纳'), 并汇总问题状态。
    """
    res = _execute(
        "SELECT issue_id, status FROM measures WHERE id = ?", [measure_id])
    if not res.rows:
        raise RuntimeError("措施 #{} 不存在".format(measure_id))
    issue_id = int(res.rows[0][0])
    old_status = str(res.rows[0][1] or "")
    # 仅"待评审"或"不采纳"(重新采纳)状态的措施可评审
    if old_status not in (auth_core.MEASURE_STATUS_REVIEW,
                          auth_core.MEASURE_STATUS_REJECTED):
        raise RuntimeError("措施 #{} 当前状态「{}」不可评审(仅待评审/不采纳可操作)".format(
            measure_id, old_status))

    if decision == "adopt":
        new_status = auth_core.MEASURE_STATUS_ADOPTED
        result = "采纳"
        comment = "【措施评审】采纳, 待二次分配工程师"
    else:
        new_status = auth_core.MEASURE_STATUS_REJECTED
        result = "不采纳"
        comment = "【措施评审】不采纳, 暂不执行(可重新采纳)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "UPDATE measures SET status = ?, updated_at = ? WHERE id = ?",
        [new_status, now, measure_id])
    _execute(
        "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
        "review_result, review_comment, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [issue_id, measure_id, None, reviewer, result, comment, now, now])
    _clear_measures_cache()
    _clear_reviews_cache()
    recompute_issue_status(issue_id)
    return {"issue_id": issue_id, "old_status": old_status, "new_status": new_status}


def assign_measure_dept_pcs(measure_id, department, operator, manager_name=""):
    """v3.1 QCS 两层分配·第一层: 管理员把措施分配给 P/Q/T 部门的【经理】。

    参数:
      measure_id   - 措施id
      department   - P/Q/T(来自被选经理所属部门)
      operator     - 管理员姓名(操作人)
      manager_name - 被指派的部门经理姓名(候选人只能是 role=manager 的账号)
    规则:
      - 仅"待评审"且 department 为空的措施可分配
      - 写 department + assigned_by_admin + assigned_manager, 状态不变(仍待评审)
      - 分配后该措施进入该经理(本部门)的"待评审"列表
      - ★ 不得越级直接分给工程师 —— 工程师由经理做第二次分配
    """
    if department not in auth_core.DEPARTMENTS:
        raise RuntimeError("部门必须是 P / Q / T")
    operator = str(operator or "").strip()
    manager_name = str(manager_name or "").strip()
    if not operator:
        raise RuntimeError("操作人不能为空")
    if not manager_name:
        raise RuntimeError("必须指定接收的部门经理, 不能直接分给工程师")

    res = _execute(
        "SELECT issue_id, status, department FROM measures WHERE id = ?", [measure_id])
    if not res.rows:
        raise RuntimeError("措施 #{} 不存在".format(measure_id))
    issue_id = int(res.rows[0][0])
    old_status = str(res.rows[0][1] or "")
    if old_status != auth_core.MEASURE_STATUS_REVIEW:
        raise RuntimeError("措施 #{} 当前状态「{}」不可第一次分配(仅待评审可分配部门)".format(
            measure_id, old_status))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "UPDATE measures SET department = ?, assigned_by_admin = ?, "
        "assigned_manager = ?, updated_at = ? WHERE id = ?",
        [department, operator, manager_name, now, measure_id])
    _execute(
        "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
        "review_result, review_comment, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [issue_id, measure_id, None, operator, "分配部门",
         "【第一次分配】部门={} 接收经理={}".format(department, manager_name), now, now])

    # 同步问题 department(取第一条已分配部门措施所在部门, 仅展示)
    # ★ 合并为单条 SQL, 省一次远程往返
    _execute(
        "UPDATE issues SET department = ("
        "  SELECT department FROM measures WHERE issue_id = ? "
        "  AND department IS NOT NULL AND department <> '' "
        "  ORDER BY id ASC LIMIT 1), updated_at = ? WHERE id = ?",
        [issue_id, now, issue_id])

    _clear_measures_cache()
    _clear_reviews_cache()
    _clear_issues_cache()
    recompute_issue_status(issue_id)
    return {"issue_id": issue_id, "old_status": old_status}


def assign_measure_person_pcs(measure_id, owner, due_date, operator):
    """v3.1 QCS 两层分配·第二层: 部门经理把已采纳措施分配给本部门工程师。

    参数:
      measure_id - 措施id
      owner      - 工程师姓名
      due_date   - 预计完成日期(YYYY-MM-DD)
      operator   - 部门经理姓名
    规则:
      - 仅"已采纳"状态的措施可二次分配
      - 写 owner + assigned_by_manager + due_date, 状态: 已采纳 → 待执行
      - 分配后该措施出现在工程师"我的措施"列表
    """
    owner = str(owner or "").strip()
    due_date = str(due_date or "").strip()
    operator = str(operator or "").strip()
    if not owner:
        raise RuntimeError("工程师姓名不能为空")
    if not due_date or _parse_date(due_date) is None:
        raise RuntimeError("预计完成时间格式应为 YYYY-MM-DD")
    if not operator:
        raise RuntimeError("操作人不能为空")

    # ★ 校验: owner 必须是【工程师】账号(姓名与账号绑定), 且部门与操作经理一致
    _owner_acc = None
    try:
        for _a in auth.list_accounts():
            if str(_a.get("display_name") or "").strip() == owner or \
               str(_a.get("username") or "").strip() == owner:
                _owner_acc = _a
                break
    except Exception:
        _owner_acc = None
    if not _owner_acc:
        raise RuntimeError("找不到姓名/账号为「{}」的用户".format(owner))
    if _owner_acc.get("role") != auth_core.ROLE_ENGINEER:
        raise RuntimeError("「{}」不是工程师, 不能作为措施责任人".format(owner))
    _mgr_acc = None
    try:
        for _a in auth.list_accounts():
            if str(_a.get("display_name") or "").strip() == operator or \
               str(_a.get("username") or "").strip() == operator:
                _mgr_acc = _a
                break
    except Exception:
        _mgr_acc = None
    if _mgr_acc and str(_owner_acc.get("department") or "").strip().upper() != \
       str(_mgr_acc.get("department") or "").strip().upper():
        raise RuntimeError("「{}」不属于你的部门, 不能跨部门分配".format(owner))
    owner = str(_owner_acc.get("display_name") or owner).strip()

    res = _execute(
        "SELECT issue_id, status FROM measures WHERE id = ?", [measure_id])
    if not res.rows:
        raise RuntimeError("措施 #{} 不存在".format(measure_id))
    issue_id = int(res.rows[0][0])
    old_status = str(res.rows[0][1] or "")
    if old_status != auth_core.MEASURE_STATUS_ADOPTED:
        raise RuntimeError("措施 #{} 当前状态「{}」不可二次分配(仅已采纳可指定工程师)".format(
            measure_id, old_status))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "UPDATE measures SET owner = ?, due_date = ?, assigned_by_manager = ?, "
        "status = ?, updated_at = ? WHERE id = ?",
        [owner, due_date, operator, auth_core.MEASURE_STATUS_PENDING, now, measure_id])
    _execute(
        "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
        "review_result, review_comment, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [issue_id, measure_id, None, operator, "分配工程师",
         "【第二次分配】工程师={} 预计完成={}".format(owner, due_date), now, now])

    _clear_measures_cache()
    _clear_reviews_cache()
    recompute_issue_status(issue_id)
    return {"issue_id": issue_id, "old_status": old_status,
            "new_status": auth_core.MEASURE_STATUS_PENDING}


def assign_issue_persons(issue_id, responsible_person_1, responsible_person_2,
                         responsible_person_3):
    """v2.3: 为"待分配问题"分配三层责任人。
    分配后问题状态保持'未评审'不变; 责任人填好后进入正常评审流程
    (第三责任人可评审其措施)。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "UPDATE issues SET responsible_person_1 = ?, responsible_person_2 = ?, "
        "responsible_person_3 = ?, updated_at = ? WHERE id = ?",
        [responsible_person_1, responsible_person_2, responsible_person_3,
         now, issue_id],
    )
    _clear_issues_cache()


def delete_issue(issue_id):
    """按编号删除问题(DELETE)。
    v2.0: 连带删除该问题下的所有措施(measures)和审核记录(issue_reviews),
    避免孤儿数据; 删除后清空相关缓存。"""
    _execute("DELETE FROM issue_reviews WHERE issue_id = ?", [issue_id])
    _execute("DELETE FROM measures WHERE issue_id = ?", [issue_id])
    _execute("DELETE FROM issues WHERE id = ?", [issue_id])
    _clear_issues_cache()
    _clear_measures_cache()
    _clear_reviews_cache()


def reset_issue_to_initial(issue_id):
    """v3.1 QCS: 把指定问题恢复为【初始状态】(供流程测试重置, 不删除问题)。

    重置内容:
      ① issues.status       → 未开始; issues.department → NULL
      ② 该问题下所有措施:
         status → 待评审; owner / department / due_date / completed_at /
         completed_overdue / assigned_by_admin / assigned_by_manager /
         assigned_manager → 全部 NULL
      ③ 删除该问题下所有 issue_reviews(措施状态变更日志)
    保留内容: 问题本身、问题描述、措施内容、问题来源/工位/零件/型号等基础信息。

    用途: 管理员把跑过流程的测试问题回滚, 重新走"第一次分配 → 评审 → 二次分配"。
    不改变状态机/权限/逾期计算逻辑 —— 只是把字段还原到录入后的初始值。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # ① 问题回到未开始 + 清空问题级部门
    _execute(
        "UPDATE issues SET status = ?, department = NULL, updated_at = ? WHERE id = ?",
        [auth_core.ISSUE_STATUS_NOT_STARTED, now, issue_id])
    # ② 措施全部回退到待评审, 清空分配/执行相关字段
    _execute(
        "UPDATE measures SET status = ?, owner = NULL, department = NULL, "
        "due_date = NULL, completed_at = NULL, completed_overdue = NULL, "
        "assigned_by_admin = NULL, assigned_by_manager = NULL, "
        "assigned_manager = NULL, updated_at = ? WHERE issue_id = ?",
        [auth_core.MEASURE_STATUS_REVIEW, now, issue_id])
    # ③ 清空状态变更日志
    _execute("DELETE FROM issue_reviews WHERE issue_id = ?", [issue_id])
    _clear_issues_cache()
    _clear_measures_cache()
    _clear_reviews_cache()


# ============================================================
# 措施(measures)读写 —— v2.0 核心
# ============================================================

@st.cache_data(ttl=3, show_spinner=False)
def _load_measures_cached():
    """真实查询Turso的措施缓存层(按 issue_id, measure_order 升序)。"""
    result = _execute(
        "SELECT * FROM measures ORDER BY issue_id ASC, measure_order ASC, id ASC")
    return pd.DataFrame(result.rows, columns=result.columns)


def load_all_measures():
    """加载全部措施(失败返回空表)"""
    try:
        return _load_measures_cached()
    except Exception as e:
        print("load_all_measures error:", e)
        return pd.DataFrame()


def _clear_measures_cache():
    """清空措施列表缓存"""
    try:
        _load_measures_cached.clear()
    except Exception:
        pass


def _adopted_flag(value):
    """is_adopted 归一化: None/NaN/异常 → None; 正常 → int(1/0)"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_measures_by_issue(issue_id):
    """返回指定问题下的所有措施(DataFrame, 按 measure_order 升序)。"""
    df = load_all_measures()
    if df.empty or "issue_id" not in df.columns:
        return df
    return df[df["issue_id"] == issue_id]


def update_measure_status(measure_id, new_status):
    """更新 measures 表的 status 字段(同时刷新 updated_at)。
    v2.0: 更新措施状态后不直接改问题状态 —— 由调用方随后调用
    recompute_issue_status 汇总(见各业务函数末尾)。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "UPDATE measures SET status = ?, updated_at = ? WHERE id = ?",
        [new_status, now, measure_id],
    )
    _clear_measures_cache()


def set_measure_adoption(measure_id, is_adopted, reviewer):
    """v2.0: 第三责任人对某条措施做"采纳/不采纳"评审决策。
    参数:
      measure_id  - 措施id
      is_adopted  - 1=采纳 / 0=不采纳
      reviewer    - 评审人姓名(该措施所属问题的第三责任人)
    规则:
      采纳   → is_adopted=1, status='已评审'(进入分层审核, 从第一责任人开始);
      不采纳 → is_adopted=0, status='待评审'(保留在不采纳列表, 可重新采纳)。
    两个动作都会在 issue_reviews 插一条 review_level=3 的评审流水
    (review_result='采纳'/'不采纳', 与分层审核结果区分开)。
    最后立即重新汇总所属问题状态。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 查该措施所属问题(拿 issue_id 回填流水; 顺便校验存在性)
    res = _execute("SELECT issue_id FROM measures WHERE id = ?", [measure_id])
    if not res.rows:
        raise RuntimeError("措施 #{} 不存在".format(measure_id))
    issue_id = int(res.rows[0][0])

    if is_adopted:
        new_status = "已评审"
        result = "采纳"
        comment = "【措施评审】采纳, 进入分层审核流程"
        # v2.5: 采纳时固化"是否跳过第二责任人" —— 查所属问题的第二责任人是否留空。
        #   留空 → skip_level2=1(第一责任人完全符合后直接跳到第三责任人);
        #   非空 → skip_level2=0(走 1→2→3 正常三层)。
        # 固化后不再随 p2 变化, 避免中途补填第二责任人导致流程回退。
        # (重新采纳时也会刷新该值, 以适配责任人配置的变化)
        p2_res = _execute(
            "SELECT responsible_person_2 FROM issues WHERE id = ?", [issue_id])
        p2_val = ""
        if p2_res.rows and p2_res.rows[0][0] is not None:
            p2_val = str(p2_res.rows[0][0]).strip()
        skip_flag = 1 if p2_val == "" else 0
    else:
        new_status = "待评审"
        result = "不采纳"
        comment = "【措施评审】不采纳, 暂不整改"

    if is_adopted:
        _execute(
            "UPDATE measures SET is_adopted = ?, status = ?, skip_level2 = ?, "
            "updated_at = ? WHERE id = ?",
            [1 if is_adopted else 0, new_status, skip_flag, now, measure_id],
        )
    else:
        _execute(
            "UPDATE measures SET is_adopted = ?, status = ?, updated_at = ? WHERE id = ?",
            [1 if is_adopted else 0, new_status, now, measure_id],
        )
    _execute(
        "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
        "review_result, review_comment, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [issue_id, measure_id, 3, reviewer, result, comment, now, now],
    )
    _clear_measures_cache()
    _clear_reviews_cache()
    # 评审决策改变了措施的采纳状态 → 立即重新汇总问题状态
    recompute_issue_status(issue_id)


def assign_measure_level2(measure_id, person2, reviewer):
    """v2.8: 第三责任人"中途补充第二责任人"(问题级粒度)。

    场景: 措施采纳时第二责任人留空 → skip_level2 固化为 1(跳过第二责任人);
    措施流转到第三责任人后, 若认为技术性较强、需要工程师介入, 可发起本动作
    把措施交回第二责任人验收。

    ★ 与"完全不符合"退回的本质区别:
        完全不符合     → 否定整改结果: status='已退回', 强制退回第一责任人重做
        转交第二责任人 → 不否定整改结果: status 保持'已评审', 第一责任人的
                         整改记录与照片完整保留; 仅把 skip_level2 由 1 改为 0,
                         让层级回退到 2, 交给工程师在现有整改结果上验收

    ★ 对状态机零改动(本设计的核心):
        层级推导 compute_measure_level 只取 _LEVEL_REVIEW_RESULTS 内的三种结果
        ("完全符合"/"完全不符合"/"部分符合"); 本动作写入的 review_result
        ='转交第二责任人' 不在其中, 会被自动忽略, 不参与推导。
        层级回退完全由 skip_level2 由 1→0 驱动(已有机制):
            最新记录是 level1 的"完全符合" → "3 if skip_level2 else 2" → 返回 2

    ★ 说明: 当初 skip_level2 在采纳时固化, 正是为了"避免中途补填第二责任人
        导致流程回退"。本次是业务需求变更 —— 主动且有留痕地放开该约束。

    粒度(本次选"问题级"): 一次转交, 该问题下【所有 skip_level2=1 且未关闭】的
    措施一并改为 0, 与当前措施一起走完整三层流程。

    参数:
      measure_id - 触发本动作的措施id
      person2    - 要补充的第二责任人姓名
      reviewer   - 操作人姓名(必须是该措施所属问题的第三责任人)
    返回: 本次受影响的措施数
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    person2 = str(person2 or "").strip()
    reviewer = str(reviewer or "").strip()
    if not person2:
        raise RuntimeError("第二责任人姓名不能为空")
    if not reviewer:
        raise RuntimeError("操作人姓名不能为空")

    # 1) 查措施 + 所属问题(校验存在性 / p3身份 / 跳过标记 / 是否已关闭)
    res = _execute(
        "SELECT m.issue_id, m.status, m.skip_level2, i.responsible_person_3 "
        "FROM measures m JOIN issues i ON m.issue_id = i.id WHERE m.id = ?",
        [measure_id])
    if not res.rows:
        raise RuntimeError("措施 #{} 不存在".format(measure_id))
    issue_id = int(res.rows[0][0])
    measure_status = str(res.rows[0][1] or "")
    skip_flag = res.rows[0][2]
    p3 = str(res.rows[0][3] or "").strip()

    if p3 != reviewer:
        raise RuntimeError("{} 不是该措施的第三责任人, 无权转交".format(reviewer))
    if measure_status == "已关闭":
        raise RuntimeError("措施 #{} 已关闭, 不能再转交".format(measure_id))
    # 只有"因第二责任人留空而跳过"的措施才需要补; 已在正常三层的不处理
    if int(skip_flag or 0) != 1:
        raise RuntimeError("措施 #{} 当前未跳过第二责任人, 无需转交".format(measure_id))

    # 2) 统计本次受影响的措施数(问题级粒度)
    cnt_res = _execute(
        "SELECT COUNT(*) FROM measures WHERE issue_id = ? "
        "AND COALESCE(skip_level2, 0) = 1 AND COALESCE(status, '') <> '已关闭'",
        [issue_id])
    affected = int(cnt_res.rows[0][0]) if cnt_res.rows else 0

    # 3) 补上第二责任人(问题级字段)
    _execute(
        "UPDATE issues SET responsible_person_2 = ?, updated_at = ? WHERE id = ?",
        [person2, now, issue_id])

    # 4) 问题级粒度: 该问题下所有"已跳过且未关闭"的措施一并取消跳过
    _execute(
        "UPDATE measures SET skip_level2 = 0, updated_at = ? "
        "WHERE issue_id = ? AND COALESCE(skip_level2, 0) = 1 "
        "  AND COALESCE(status, '') <> '已关闭'",
        [now, issue_id])

    # 5) 留痕: review_result='转交第二责任人' —— 不在 _LEVEL_REVIEW_RESULTS 内,
    #    状态机推导层级时会被自动忽略, 仅用于追溯
    _execute(
        "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
        "review_result, review_comment, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [issue_id, measure_id, 3, reviewer, "转交第二责任人",
         "【转交第二责任人】技术性较强, 转交工程师 {} 介入验收"
         "(该问题下所有跳过第二责任人的未关闭措施一并改为走三层)".format(person2),
         now, now])

    _clear_measures_cache()
    _clear_issues_cache()
    _clear_reviews_cache()
    # 措施状态虽未变, 但责任人配置变了 → 安全起见重新汇总一次问题状态
    recompute_issue_status(issue_id)

    # 6) 企微通知预留位(本次需求: 暂不发送)
    #    待群机器人 webhook 权限开通后, 取消下方注释即可通知被补的第二责任人。
    #    try:
    #        from notify import send_wechat_message
    #        send_wechat_message(
    #            "【LPA转交验收】问题 #{} 的措施已转交给 {}, 请及时验收。".format(
    #                issue_id, person2))
    #    except Exception:
    #        pass

    return affected


def add_measure_review_record(measure_id, review_level, reviewer,
                              review_result, review_comment, photo_path=None):
    """v2.0: 插入一条"分层审核"流水(完全符合/完全不符合/部分符合)。
    参数:
      measure_id    - 措施id
      review_level  - 审核层级(1/2/3)
      reviewer      - 审核人姓名
      review_result - 完全符合 / 完全不符合 / 部分符合
      review_comment- 备注(完全符合=整改说明 / 完全不符合=不合格原因 / 部分符合=具体说明)
      photo_path    - 照片相对路径(仅"完全符合"时传)
    本函数只写流水, 措施状态/问题状态由调用方按状态机规则更新。"""
    res = _execute("SELECT issue_id FROM measures WHERE id = ?", [measure_id])
    if not res.rows:
        raise RuntimeError("措施 #{} 不存在".format(measure_id))
    issue_id = int(res.rows[0][0])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
        "review_result, review_comment, photo_path, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [issue_id, measure_id, review_level, reviewer, review_result,
         review_comment, photo_path, now, now],
    )
    _clear_reviews_cache()


# ============================================================
# 审核流水(issue_reviews)读写
# ============================================================

@st.cache_data(ttl=3, show_spinner=False)
def _load_all_reviews_cached():
    """拉取 issue_reviews 全表(按id升序)的缓存层。"""
    result = _execute("SELECT * FROM issue_reviews ORDER BY id ASC")
    return pd.DataFrame(result.rows, columns=result.columns)


def load_all_reviews():
    """加载全部审核流水(失败返回空表)"""
    try:
        return _load_all_reviews_cached()
    except Exception as e:
        print("load_all_reviews error:", e)
        return pd.DataFrame()


def _clear_reviews_cache():
    """清空审核流水缓存"""
    try:
        _load_all_reviews_cached.clear()
    except Exception:
        pass


def get_reviews_by_measure(measure_id):
    """返回指定措施的全部审核流水(DataFrame, 按id升序)。"""
    df = load_all_reviews()
    if df.empty or "measure_id" not in df.columns:
        return df
    return df[df["measure_id"] == measure_id]


# ============================================================
# 分层审核层级推导 (v2.0 措施级状态机)
# ============================================================

# 分层审核的三种结果(评审决策"采纳/不采纳"不参与层级推导)
_LEVEL_REVIEW_RESULTS = ("完全符合", "完全不符合", "部分符合")


def compute_measure_level(measure_records, measure_status, skip_level2=False):
    """按状态机规则推导"措施"当前待处理层级(纯函数, 不连网)。

    规则(v2.0 / v2.5):
      ① 措施 status='已关闭' → 返回 None(已闭环, 不再显示待办)
      ② 措施 status='已退回' → 返回 1(被完全不符合打回第一责任人)
      ③ 其余(已评审) 看分层审核流水(完全符合/完全不符合/部分符合):
         - 找最近一次"完全不符合"记录 → 只看它之后的流水(重新开始)
         - 取筛选后最新一条:
            完全符合 level=1 → 层级2 (轮到第二责任人)
                             ★ 若 skip_level2=True 则直接跳到层级3(跳过第二责任人)
            完全符合 level=2 → 层级3 (轮到第三责任人)
            完全符合 level=3 → None (第三责任人确认, 措施已闭环)
            完全不符合     → 层级1 (打回第一责任人)
            部分符合       → 保持该条记录层级(不推进不退回)
         - 无有效流水 → 层级1 (刚采纳, 等第一责任人整改)
    参数:
      measure_records - 该措施的审核流水列表(字典列表, 顺序不限, 内部按id排序)
      measure_status  - measures 表当前 status
      skip_level2     - 是否跳过第二责任人(v2.5): 该措施所属问题的第二责任人
                        留空时为 True, 第一责任人"完全符合"后直接流转到第三责任人。
                        该值在"采纳"时固化到 measures.skip_level2, 之后不再变化,
                        避免中途补填第二责任人导致流程回退。
    返回: 1/2/3(当前待处理层级) 或 None(已关闭/不参与分层审核)
    """
    if measure_status == "已关闭":
        return None
    if measure_status == "已退回":
        return 1

    # 只保留"分层审核"结果(排除评审决策的采纳/不采纳)
    records = [r for r in measure_records
               if r.get("review_result") in _LEVEL_REVIEW_RESULTS]
    records.sort(key=lambda r: r.get("id", 0) or 0)

    # 找最近一次"完全不符合"(退回)记录, 只看它之后的流水
    last_return_id = None
    for r in records:
        if r.get("review_result") == "完全不符合":
            last_return_id = r.get("id", 0) or 0
    if last_return_id is not None:
        records = [r for r in records if (r.get("id", 0) or 0) > last_return_id]

    if records:
        latest = records[-1]
        result = latest.get("review_result")
        level = latest.get("review_level")
        if result == "完全符合":
            if level == 1:
                # v2.5: 跳过第二责任人时, 第一责任人完全符合后直接到第三责任人
                return 3 if skip_level2 else 2
            if level == 2:
                return 3
            return None   # 第三责任人完全符合 → 措施已关闭
        if result == "完全不符合":
            return 1
        # 部分符合: 措施留在当前责任人层(不推进、不退回)
        return level if level in (1, 2, 3) else 1

    # 无有效流水 → 刚采纳, 等第一责任人
    return 1


# ============================================================
# 问题状态实时汇总 (v2.0)
# ============================================================

def recompute_issue_status(issue_id):
    """v3.1 QCS: 按该问题下措施的状态实时汇总, 更新 issues.status。

    汇总规则:
      ① 无措施 或 无"已采纳进入执行流程"的措施 → 未开始
      ② 所有已采纳措施 status='已关闭'        → 已关闭
      ③ 其余                                   → 进行中

    ★ "待评审"/"不采纳"的措施不参与关闭判断 —— 否则一条不采纳的措施
      会让问题永远无法关闭。

    ★ 注意: 汇总基于该问题的【全部】措施(含其他部门)。因此某部门工程师
      可能出现"我部门的措施都关了, 但问题仍显示进行中" —— 这是正确的
      (还在等其他部门), 页面应给出提示避免误解。"""
    measures = get_measures_by_issue(issue_id)
    if measures.empty:
        new_status = auth_core.ISSUE_STATUS_NOT_STARTED
    else:
        active = measures[
            measures["status"].astype(str).isin(auth_core.MEASURE_ACTIVE_STATUSES)]
        if active.empty:
            new_status = auth_core.ISSUE_STATUS_NOT_STARTED
        elif (active["status"].astype(str) == auth_core.MEASURE_STATUS_CLOSED).all():
            new_status = auth_core.ISSUE_STATUS_CLOSED
        else:
            new_status = auth_core.ISSUE_STATUS_DOING

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "UPDATE issues SET status = ?, updated_at = ? WHERE id = ?",
        [new_status, now, issue_id],
    )
    _clear_issues_cache()
    return new_status


# ============================================================
# 个人行动计划 (v2.0): 措施粒度查询
# ============================================================

def get_measures_for_person(person_name):
    """返回"该人作为三层责任人任一角色"所关联问题的全部措施(DataFrame)。

    联表思路: 措施表只存 issue_id, 不存责任人; 责任人存在 issues 表上。
    这里先查出该人关联的全部问题id, 再过滤这些问题的措施, 返回的措施
    DataFrame 额外附加问题字段(problem_description/work_station/issue_date/
    status/responsible_person_1/2/3), 供页面做"问题摘要 + 措施内容"展示与
    "当前层级匹配角色"过滤(身兼多职也不漏任务)。
    参数: person_name - 责任人姓名"""
    issues = load_all_issues()
    measures = load_all_measures()
    if issues.empty or measures.empty:
        return measures.iloc[0:0] if not measures.empty else pd.DataFrame()

    target = str(person_name).strip()
    # 三个责任人字段"或"匹配, 命中任一即算关联问题
    mask = None
    for col in ("responsible_person_1", "responsible_person_2", "responsible_person_3"):
        if col not in issues.columns:
            continue
        col_match = issues[col].fillna("").astype(str).str.strip().eq(target)
        mask = col_match if mask is None else (mask | col_match)
    if mask is None:
        return measures.iloc[0:0]
    my_issues = issues[mask]

    # 措施关联问题字段(带 issue_ 前缀避免与措施自身字段混淆)
    # 注意: issue_date / issue_source 只存在于 issues 表, 必须显式带出,
    # 否则页面"发现日期"显示"未填"、问题来源取不到。
    merged = measures.merge(
        my_issues[["id", "序号", "problem_description", "work_station",
                   "issue_date", "issue_source", "status",
                   "responsible_person_1", "responsible_person_2",
                   "responsible_person_3"]],
        left_on="issue_id", right_on="id", suffixes=("", "_issue"),
        how="inner",
    )
    return merged


# ============================================================
# v3.1 QCS(质量协同系统)核心: 逾期计算 / 状态汇总 / 部门过滤
# ★ 电脑端(Streamlit)与移动端后端(api_server)必须同规则 ——
#   api_server.py 内有同名同逻辑的实现, 改这边时请同步改那边。
# ============================================================

def _today_date():
    """今天的 date 对象(统一入口, 便于排错与测试)"""
    return datetime.now().date()


def _parse_date(v):
    """把 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' / date / datetime / Timestamp
    统一解析成 date 对象; 空值或解析失败返回 None。"""
    if v is None:
        return None
    # datetime / pandas Timestamp 等带 year/month/day 的对象
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


def is_measure_overdue(due_date, status):
    """措施是否逾期 —— ★计算属性, 不入库。
    规则: due_date 非空 且 due_date < 今天 且 状态不在终态(已完成/已关闭)。"""
    if str(status or "") not in auth_core.MEASURE_OVERDUE_STATUSES:
        return False
    d = _parse_date(due_date)
    if d is None:
        return False
    return d < _today_date()


def measure_overdue_days(due_date, status):
    """逾期天数(未逾期返回 0)"""
    if str(status or "") not in auth_core.MEASURE_OVERDUE_STATUSES:
        return 0
    d = _parse_date(due_date)
    if d is None:
        return 0
    delta = (_today_date() - d).days
    return delta if delta > 0 else 0


def is_measure_due_soon(due_date, status, within_days=3):
    """是否即将到期: 今天起 within_days 天内(含今天到期)且未到终态。"""
    if str(status or "") not in auth_core.MEASURE_OVERDUE_STATUSES:
        return False
    d = _parse_date(due_date)
    if d is None:
        return False
    delta = (d - _today_date()).days
    return 0 <= delta <= within_days


def load_dashboard_data():
    """【分析看板】数据源封装(仅 admin 看板用)。

    返回 (issues, measures) 元组:
      - issues   : 全量问题表
      - measures : 全量措施表, 已左连接问题描述(problem_description),
                   并附加两个计算列:
                       is_overdue   (bool) —— 是否逾期
                       overdue_days (int)  —— 逾期天数(未逾期为 0)

    逾期口径复用 is_measure_overdue / measure_overdue_days, 不另起炉灶,
    保证与移动端/台账展示完全一致。
    """
    issues = load_all_issues()
    measures = load_all_measures()
    if measures.empty:
        return issues, measures

    # 左连接问题描述(供逾期明细表 / 图1 使用); 只取必要列, 避免与措施 status 冲突
    if not issues.empty and "id" in issues.columns and "issue_id" in measures.columns:
        need = [c for c in ("id", "problem_description") if c in issues.columns]
        measures = measures.merge(
            issues[need], left_on="issue_id", right_on="id",
            how="left", suffixes=("", "_issue"))

    # 计算列: 逐条复用现有逾期函数(计算属性, 不入库)
    measures["is_overdue"] = measures.apply(
        lambda r: is_measure_overdue(r.get("due_date"), r.get("status")),
        axis=1).astype(bool)
    measures["overdue_days"] = measures.apply(
        lambda r: measure_overdue_days(r.get("due_date"), r.get("status")),
        axis=1).astype(int)
    return issues, measures


def dashboard_kpis(issues, measures):
    """【分析看板】顶部 KPI 汇总, 返回 dict:
      open_issues      : 未关闭问题数(issues.status != '已关闭')
      adopted_measures : 已采纳措施数(已采纳/待执行/执行中/已完成/已关闭)
      overdue_measures : 逾期且未完成措施数
      pending_review   : 待评审措施数
    """
    kpis = {
        "open_issues": 0,
        "adopted_measures": 0,
        "overdue_measures": 0,
        "pending_review": 0,
    }
    if not issues.empty and "status" in issues.columns:
        kpis["open_issues"] = int(
            (issues["status"].fillna("").astype(str) != "已关闭").sum())
    if not measures.empty:
        if "status" in measures.columns:
            st_col = measures["status"].fillna("").astype(str)
            kpis["adopted_measures"] = int(
                st_col.isin(auth_core.MEASURE_ACTIVE_STATUSES).sum())
            kpis["pending_review"] = int(
                (st_col == auth_core.MEASURE_STATUS_REVIEW).sum())
        if "is_overdue" in measures.columns:
            kpis["overdue_measures"] = int(
                measures["is_overdue"].fillna(False).astype(bool).sum())
    return kpis


def load_dashboard_cycle():
    """【分析看板·图8】措施平均处理周期(已采纳→已关闭), 按部门。

    口径:
      - 采纳时间 = issue_reviews 中 review_result='采纳' 的最早 reviewed_at
      - 关闭时间 = issue_reviews 中 review_result='已关闭' 的最后 reviewed_at
      - 处理周期 = (关闭时间 - 采纳时间) 的天数
      - 仅统计"已采纳且已关闭"的措施
    返回: {department: {"avg_days": float, "cnt": int}}, department 空串=未分配。
    """
    reviews = load_all_reviews()
    if reviews.empty:
        return {}
    need = {"measure_id", "review_result", "reviewed_at"}
    if not need.issubset(reviews.columns):
        return {}

    adopted = reviews[reviews["review_result"] == "采纳"]
    closed = reviews[reviews["review_result"] == "已关闭"]
    if adopted.empty or closed.empty:
        return {}

    # 采纳时间取最早, 关闭时间取最后
    a_map, c_map = {}, {}
    for _, r in adopted.iterrows():
        mid, ts = r.get("measure_id"), r.get("reviewed_at")
        if mid is None or not ts:
            continue
        mid = int(mid)
        if mid not in a_map or str(ts) < str(a_map[mid]):
            a_map[mid] = ts
    for _, r in closed.iterrows():
        mid, ts = r.get("measure_id"), r.get("reviewed_at")
        if mid is None or not ts:
            continue
        mid = int(mid)
        if mid not in c_map or str(ts) > str(c_map[mid]):
            c_map[mid] = ts

    # 措施 → 部门
    dept_map = {}
    measures = load_all_measures()
    if not measures.empty and "department" in measures.columns and "id" in measures.columns:
        dept_map = {int(r["id"]): str(r.get("department") or "").strip()
                    for _, r in measures.iterrows()}

    agg = {}
    for mid, a_ts in a_map.items():
        c_ts = c_map.get(mid)
        if not c_ts:
            continue
        d = _parse_date(a_ts)
        e = _parse_date(c_ts)
        if d is None or e is None or e < d:
            continue
        dept = dept_map.get(mid, "")
        agg.setdefault(dept, {"sum": 0, "cnt": 0})
        agg[dept]["sum"] += (e - d).days
        agg[dept]["cnt"] += 1

    result = {}
    for dept, v in agg.items():
        result[dept] = {
            "avg_days": round(v["sum"] / v["cnt"], 1) if v["cnt"] else 0,
            "cnt": v["cnt"],
        }
    return result


def update_measure_status_pcs(measure_id, new_status, operator,
                              comment="", photo_path=None):
    """v3.1 QCS: 更新措施状态(核心动作)。

    与三层审核的区别: 不再有"完全符合/完全不符合/部分符合"与层级推进,
    只做状态流转 + 留痕。

    规则:
      - 推进到终态(已完成/已关闭)→ 写 completed_at;
        完成日期晚于 due_date 时置 completed_overdue=1(统计按期完成率)。
      - 重开(终态 → 执行中)→ 清空 completed_at / completed_overdue。
      - 每次变更在 issue_reviews 写一条状态变更日志(review_result = 新状态)。
      - 变更后立即重新汇总所属问题状态。
    参数:
      new_status - 待执行 / 执行中 / 已完成 / 已关闭
      operator   - 操作人姓名(管理层用户)
    """
    if new_status not in auth_core.MEASURE_STATUSES:
        raise RuntimeError("非法的措施状态: {}".format(new_status))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    res = _execute(
        "SELECT issue_id, due_date, status FROM measures WHERE id = ?", [measure_id])
    if not res.rows:
        raise RuntimeError("措施 #{} 不存在".format(measure_id))
    issue_id = int(res.rows[0][0])
    due_date = res.rows[0][1]
    old_status = str(res.rows[0][2] or "")

    completed_at = None
    completed_overdue = None
    if new_status in auth_core.MEASURE_FINAL_STATUSES:
        completed_at = now
        completed_overdue = 0
        d_due = _parse_date(due_date)
        if d_due is not None and _today_date() > d_due:
            completed_overdue = 1

    _execute(
        "UPDATE measures SET status = ?, completed_at = ?, completed_overdue = ?, "
        "updated_at = ? WHERE id = ?",
        [new_status, completed_at, completed_overdue, now, measure_id])

    # 留痕: issue_reviews 复用为"状态变更日志"(review_result 存新状态)
    _execute(
        "INSERT INTO issue_reviews (issue_id, measure_id, review_level, reviewer, "
        "review_result, review_comment, photo_path, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [issue_id, measure_id, None, operator, new_status,
         comment or "", photo_path, now, now])

    _clear_measures_cache()
    _clear_reviews_cache()
    recompute_issue_status(issue_id)
    return {"issue_id": issue_id, "old_status": old_status,
            "new_status": new_status, "completed_overdue": completed_overdue}


def department_filter_sql(role, department):
    """v3.1 QCS: 按角色+部门生成措施过滤条件(返回 SQL 片段与参数)。

    数据范围规则:
      admin    → 不参与业务(调用方应直接拒绝, 不查业务数据)
      manager  → 不限部门(全部 P/Q/T)
      engineer → 仅 department 等于本人部门的措施

    返回: (sql片段, 参数列表)。sql片段为空串表示不过滤。
    """
    if role == auth_core.ROLE_MANAGER:
        return "", []
    if role == auth_core.ROLE_ENGINEER:
        dept = str(department or "").strip()
        if not dept:
            # 工程师未配置部门 → 看不到任何数据(安全默认, 不会越权)
            return " AND 1=0 ", []
        return " AND COALESCE(m.department, '') = ? ", [dept]
    # admin 或其他未知角色: 一律不返回业务数据
    return " AND 1=0 ", []


def get_measures_for_user(role, department):
    """v3.1 QCS: 按角色+部门取可见措施(联表带出问题字段), 返回 DataFrame。
    admin 返回空表(不参与业务)。"""
    measures = load_all_measures()
    if measures.empty:
        return measures
    if role == auth_core.ROLE_ADMIN:
        return measures.iloc[0:0]
    if role == auth_core.ROLE_ENGINEER:
        dept = str(department or "").strip()
        if not dept:
            return measures.iloc[0:0]
        if "department" not in measures.columns:
            return measures.iloc[0:0]
        measures = measures[
            measures["department"].fillna("").astype(str).str.strip() == dept]

    issues = load_all_issues()
    if issues.empty:
        return measures
    cols = [c for c in ["id", "序号", "problem_description", "work_station",
                        "issue_date", "issue_source", "department",
                        "status"] if c in issues.columns]
    merged = measures.merge(
        issues[cols], left_on="issue_id", right_on="id",
        suffixes=("", "_issue"), how="inner")
    return merged


# ============================================================
# 认证服务(v2.5)
# 用本模块的 _execute 驱动 auth_core.AuthService, 与移动端后端
# (api_server.py)共用同一套账号/密码/会话/日志逻辑, 保证两端一致。
# app.py 通过 db.auth 调用; 账号表在本库的 accounts 表。
# ============================================================
auth = auth_core.AuthService(_execute)


def init_default_admin():
    """初始化默认管理员账号(仅当库里没有任何账号时创建)。
    临时密码随机生成, 首次登录强制修改。
    返回: (是否新建, 账号名, 临时密码或None)"""
    return auth.init_default_admin()
