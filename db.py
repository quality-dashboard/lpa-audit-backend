"""
db.py - Turso 云数据库操作层
车间问题分层审核与跟踪系统 (LPA)

表结构设计思路 (参考 ERPNext Quality 模块 + Issue Tracker 状态流转):
  1. issues 表        —— 问题主表, 每个现场问题一条记录, 保存"当前状态"
     (问题登记 / 责任分配 / 状态跟踪 / 改善措施)
  2. issue_reviews 表 —— 审核记录表, 保存"流转历史" (本次只建表预留, 不做界面)
     未来每做一次分层审核就插入一条记录, 一张问题可对应多条审核记录;
     issues.status 由审核动作联动更新, 审核轨迹完整可追溯。
     —— 主表存状态快照、明细表存审计轨迹, 是问题跟踪系统的标准做法。

Turso HTTP API 连接方式与 quality_tracking_system 项目完全一致。
"""
import json
import urllib.request
from datetime import datetime

import pandas as pd
import streamlit as st

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
# 问题主表: status 用中文枚举值(未评审/已评审/已退回/已关闭), 与界面直接对应
# is_adopted 是否采纳(v0.8): NULL=待评审(默认, 等第三责任人决策) / 1=采纳(进入整改流程) / 0=不采纳(搁置)
_ISSUES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_description TEXT NOT NULL,
    issue_date TEXT,
    work_station TEXT,
    improvement_action TEXT,
    responsible_person_1 TEXT,
    responsible_person_2 TEXT,
    responsible_person_3 TEXT,
    status TEXT DEFAULT '未评审',
    is_adopted INTEGER,
    created_at TEXT,
    updated_at TEXT
)
"""

# 审核记录表 (为"分层审核状态流转"预留, 本次只建表不做功能)
# 未来某一层责任人审核问题时, 往这张表插一条记录:
#   review_level=1/2/3 对应三层责任人, review_result=通过/退回
# 三层全部通过 → issues.status 联动更新为"已关闭"
_REVIEWS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS issue_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL,
    review_level INTEGER,
    reviewer TEXT,
    review_result TEXT,
    review_comment TEXT,
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
# 对外接口: 建表 / 新增问题 / 查询问题列表
# ============================================================

_DB_INIT_DONE = False  # 进程内"已建表"标记: 页面每次rerun不重复连库建表


def init_db():
    """建表(幂等, 重复执行不报错)。
    进程内只真正执行一次 —— 避免Streamlit每次页面刷新都连两次Turso。"""
    global _DB_INIT_DONE
    if _DB_INIT_DONE:
        return
    _execute(_ISSUES_TABLE_SQL)
    _execute(_REVIEWS_TABLE_SQL)
    _ensure_is_adopted_column()
    _DB_INIT_DONE = True


def _ensure_is_adopted_column():
    """确保 issues 表存在 is_adopted 列(v0.8: 默认值语义调整为 NULL=待评审)。
    - 新库: 建表SQL已含该列(不带DEFAULT→NULL), 检查后直接跳过
    - 老库缺列(v0.1~v0.5建的): ALTER 补列(不带DEFAULT, 与v0.6不同——
      那时用DEFAULT 1把存量行置为"采纳"; 现在新行应为NULL=待评审)
    - 老库已有列(v0.6/v0.7建的, 列默认值=1): SQLite不支持ALTER修改列
      默认值, 保持不动; 新插入由代码显式传值(save_issue固定传NULL),
      不依赖列默认值, 新老库行为完全一致。
    说明: v0.6/v0.7的存量数据(1=采纳/0=不采纳)全部保留, 其中状态
      '未评审'的记录会出现在第三责任人的「问题评审」待办里重新决策。"""
    result = _execute("PRAGMA table_info(issues)")
    # PRAGMA返回: [列序号, 列名, 类型, 非空, 默认值, 主键] → 第2个元素是列名
    existing_cols = [row[1] for row in result.rows]
    if "is_adopted" not in existing_cols:
        _execute("ALTER TABLE issues ADD COLUMN is_adopted INTEGER")


def save_issue(problem_description, issue_date, work_station,
               improvement_action, responsible_person_1,
               responsible_person_2, responsible_person_3):
    """新增问题(INSERT)。新问题状态固定为"未评审"。
    v0.8: 不再接收is_adopted参数 —— 录入时不做采纳决策, 统一进入
    "待评审", 由第三责任人在「问题评审」页决定采纳与否。
    注意: 这里显式写入NULL而不是省略该列 —— 老库(v0.6/v0.7)该列的
    默认值是1, 省略会被写成"采纳"; 显式NULL在新库/老库行为完全一致。
    保存成功后自动清空列表缓存, 页面刷新即显示最新数据。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "INSERT INTO issues (problem_description, issue_date, work_station, "
        "improvement_action, responsible_person_1, responsible_person_2, "
        "responsible_person_3, status, is_adopted, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [problem_description, issue_date, work_station, improvement_action,
         responsible_person_1, responsible_person_2, responsible_person_3,
         "未评审", None, now, now],  # is_adopted=NULL → 待评审
    )
    _clear_issues_cache()


def save_issues_batch(df):
    """批量导入问题(v0.7): 接收已通过页面校验的DataFrame, 循环逐条INSERT。
    入参df需包含以下列(值已由页面完成转换):
      problem_description, issue_date('YYYY-MM-DD'字符串), work_station,
      improvement_action, responsible_person_1/2/3,
      is_adopted(1/0/None, v0.8起留空=None=待评审)
    df的index保留原始行号(pandas行号), 入库失败的行能对应回Excel行。
    说明: Turso HTTP API 没有executemany批量通道, 只能逐条pipeline插入;
          单条失败不中断整批, 记录(行号,原因)供页面汇报。
    返回: (成功条数, 失败条数, [(df行索引, 失败原因), ...])"""
    ok_count, fail_list = 0, []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for idx, row in df.iterrows():
        try:
            # 是否采纳(v0.8改为选填): None/NaN→NULL(待评审), 1/0→照常入库
            _adopted_val = row["is_adopted"]
            if _adopted_val is None or (isinstance(_adopted_val, float) and pd.isna(_adopted_val)):
                _adopted_val = None
            else:
                _adopted_val = int(_adopted_val)
            _execute(
                "INSERT INTO issues (problem_description, issue_date, work_station, "
                "improvement_action, responsible_person_1, responsible_person_2, "
                "responsible_person_3, status, is_adopted, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [row["problem_description"], row["issue_date"],
                 row["work_station"], row["improvement_action"],
                 row["responsible_person_1"], row["responsible_person_2"],
                 row["responsible_person_3"], "未评审",
                 _adopted_val, now, now],
            )
            ok_count += 1
        except Exception as e:
            fail_list.append((idx, str(e)))
    # 只要有成功写入就清缓存, 问题列表/侧边栏责任人下拉随下次刷新更新
    if ok_count > 0:
        _clear_issues_cache()
    return ok_count, len(fail_list), fail_list


@st.cache_data(ttl=30, show_spinner=False)
def _load_issues_cached():
    """真实查询Turso的缓存层。
    ★ v1.0 缓存TTL从1小时降为30秒: 移动端(api_server)写库走的是独立进程,
      无法触发本进程的缓存清空(_clear_issues_cache只在本进程生效);
      旧TTL=3600导致手机端评审/审核后, 电脑端最长1小时看不到变化。
      现在最长30秒自动过期; 需要立即生效可点侧边栏"🔄 立即刷新数据"。
    v0.9: 按id升序返回, 并在第一列附加全局连续"序号"(1,2,3...):
      - 序号在加载时一次性分配, 之后任何页面做筛选都不重排不重置,
        同一个问题在所有标签页里编号一致;
      - 原始id列保留(删除/审核等内部逻辑仍用它定位), 仅页面不再展示;
      - 删除某条记录后重新加载, 序号按剩余记录重新连续编号。"""
    result = _execute("SELECT * FROM issues ORDER BY id ASC")
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


def _clear_issues_cache():
    """清空问题列表缓存(新增/删除问题后调用, 确保列表立即刷新)"""
    try:
        _load_issues_cached.clear()
    except Exception:
        pass


def delete_issue(issue_id):
    """按编号删除问题(DELETE)。
    连带删除该问题在 issue_reviews 表里的审核记录, 避免"孤儿数据"
    (审核功能上线后同样适用); 删除后自动清空列表缓存。
    参数: issue_id - 问题编号(issues表主键)"""
    _execute("DELETE FROM issues WHERE id = ?", [issue_id])
    # 连带清理审核记录(当前该表暂无数据, 属于防患于未然)
    _execute("DELETE FROM issue_reviews WHERE issue_id = ?", [issue_id])
    _clear_issues_cache()
    _clear_reviews_cache()


# ============================================================
# 分层审核功能 (v0.3): 审核记录读写 + 待处理层级推导
# ============================================================

@st.cache_data(ttl=30, show_spinner=False)
def _load_all_reviews_cached():
    """真实查询Turso的审核记录缓存层: 拉取 issue_reviews 全表(按id升序)。
    ★ v1.0 缓存TTL从1小时降为30秒: 原因同_load_issues_cached ——
      移动端写库无法清本进程缓存, 长TTL会让层级推导/历史记录显示旧数据。
      移动端操作后最长30秒自动过期; 侧边栏"立即刷新数据"可即时清空。"""
    result = _execute("SELECT * FROM issue_reviews ORDER BY id ASC")
    return pd.DataFrame(result.rows, columns=result.columns)


def load_all_reviews():
    """加载全部审核记录(失败返回空表)"""
    try:
        return _load_all_reviews_cached()
    except Exception as e:
        print("load_all_reviews error:", e)
        return pd.DataFrame()


def _clear_reviews_cache():
    """清空审核记录缓存(新增/删除审核记录后调用)"""
    try:
        _load_all_reviews_cached.clear()
    except Exception:
        pass


def add_review_record(issue_id, review_level, reviewer, review_result, review_comment):
    """插入一条审核记录到 issue_reviews 表。
    参数:
      issue_id       - 问题编号(issues表主键)
      review_level   - 审核层级(1/2/3)
      reviewer       - 审核人(暂用角色名代替, 以后接登录后传真实姓名)
      review_result  - 审核结果(通过/退回/跟踪中)
      review_comment - 审核备注(通过=整改说明 / 退回=不合格原因 / 跟踪中=当前进展)"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "INSERT INTO issue_reviews (issue_id, review_level, reviewer, "
        "review_result, review_comment, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [issue_id, review_level, reviewer, review_result, review_comment, now, now],
    )
    _clear_reviews_cache()


def update_issue_status(issue_id, new_status):
    """更新 issues 表的 status 字段(同时刷新 updated_at 时间戳)。
    参数: issue_id - 问题编号; new_status - 新状态(未评审/已评审/已退回/已关闭)"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "UPDATE issues SET status = ?, updated_at = ? WHERE id = ?",
        [new_status, now, issue_id],
    )
    _clear_issues_cache()


def get_latest_review_before_return(issue_id):
    """查询单个问题最近一次 review_result='退回' 的审核记录。
    这是层级判断规则第1步的基础查询(单问题场景使用, 例如详情页)。
    列表页为省网络往返, 走批量版: load_all_reviews() + compute_current_level()。
    返回: 记录字典(含id/review_level/review_result等); 无退回记录时返回 None"""
    result = _execute(
        "SELECT * FROM issue_reviews WHERE issue_id = ? AND review_result = '退回' "
        "ORDER BY id DESC LIMIT 1",
        [issue_id],
    )
    if not result.rows:
        return None
    return dict(zip(result.columns, result.rows[0]))


# ============================================================
# 评审类记录识别 (v0.9)
# 「问题评审」的采纳/重新采纳决策也会写入第3层"通过"流水, 但那是
# 评审动作而不是真正的第3层验收 —— 统计(图3)和自动关闭都必须排除,
# 否则第三责任人打勾数虚高、评审记录会被误判为跨月验收通过。
# 前缀清单含新旧两种写法: v0.8旧数据("评审采纳，/二次评审采纳，") +
# v0.9起新数据统一带【】标记。
# ============================================================
_ADOPTION_COMMENT_PREFIXES = ("【评审采纳】", "【重新采纳】", "评审采纳，", "二次评审采纳，")


def get_real_level3_pass_records(issue_id=None):
    """返回"真正的"第三责任人验收通过记录(已排除评审采纳类), DataFrame。
    参数: issue_id=None返回全部; 传问题编号只返回该问题的记录(按流水id升序)。
    供两处调用, 保证口径一致:
      - app.py 分析看板图3(按reviewer统计打勾数)
      - auto_close.py 自动关闭(判断跨自然月的真验收)
    复用 load_all_reviews() 缓存, 不额外连库。"""
    df = load_all_reviews()
    if df.empty:
        return df
    comments = df["review_comment"].fillna("").astype(str).str.strip()
    mask = (
        df["review_level"].eq(3)
        & df["review_result"].eq("通过")
        # 反选评审类记录: 备注以采纳类前缀开头的都剔除
        & ~comments.str.startswith(_ADOPTION_COMMENT_PREFIXES)
    )
    sub = df[mask]
    if issue_id is not None:
        sub = sub[sub["issue_id"] == issue_id].sort_values("id")
    return sub


def compute_current_level(issue_records, issue_status):
    """按状态机规则推导问题"当前待处理层级"(纯函数, 不连网, 可反复调用)。

    规则(严格按系统定义执行, 不做引申):
      1. 找出该问题最近一次 review_result='退回' 的记录
      2. 存在退回记录 → 只看该记录之后(id更大)的记录; 不存在 → 看全部记录
      3. 筛选后取最新一条:
         - level=1 且 通过 → 当前层级=2 (轮到第二责任人验收)
         - level=2 且 通过 → 当前层级=3 (轮到第三责任人验收)
         - level=3 且 通过 → status='已关闭'→问题已关闭(返回 None);
                            status≠已关闭 → 是v0.8「问题评审/重新采纳」
                            留下的采纳记录(评审通过≠整改闭环), 当前层级=1
                            (轮到第一责任人整改)
         - 退回            → 当前层级=1 (打回第一责任人重新整改)
         - level=N 跟踪中  → 层级保持 N (跟踪中不改层级, 也不改主表状态)
      4. 没有筛选后记录: status='已关闭' → 返回 None(不再显示); 否则层级=1

    参数:
      issue_records - 该问题的审核记录列表(字典列表, 顺序不限, 内部按id排序)
      issue_status  - issues 表当前 status
    返回: 1/2/3(当前待处理层级) 或 None(已关闭/不再显示)
    """
    # 按id升序整理(保证"最新"的判断准确)
    records = sorted(issue_records, key=lambda r: r.get("id", 0) or 0)

    # 第1步: 找最近一次退回记录(升序遍历, 最后命中的即最近的)
    last_return = None
    for r in records:
        if r.get("review_result") == "退回":
            last_return = r

    # 第2步: 筛选退回记录之后的记录; 无退回记录则看全部
    if last_return is not None:
        return_id = last_return.get("id", 0) or 0
        records = [r for r in records if (r.get("id", 0) or 0) > return_id]

    # 第3步: 取筛选后的最新一条, 推导层级
    if records:
        latest = records[-1]
        level = latest.get("review_level")
        result = latest.get("review_result")
        if result == "通过":
            if level == 1:
                return 2   # 第一责任人提交整改证据 → 轮到第二责任人
            if level == 2:
                return 3   # 第二责任人验收通过 → 轮到第三责任人
            # 第三责任人"通过"有两种含义(v0.8):
            #   status='已关闭' → 分层审核最终确认, 问题真闭环
            #   status≠已关闭(如'已评审') → 「问题评审/重新采纳」的采纳记录,
            #     问题其实在等第一责任人整改 → 层级=1
            return None if issue_status == "已关闭" else 1
        if result == "退回":
            return 1       # 被打回 → 重新回到第一责任人整改
        # 跟踪中 → 层级不变(问题还停在这一层继续整改)
        return level if level in (1, 2, 3) else 1

    # 第4步: 没有筛选后记录(从未审核, 或退回后还没有新动作)
    if issue_status == "已关闭":
        return None
    return 1


# ============================================================
# 个人行动计划 (v0.4): 责任人维度查询
# 两个函数都复用 load_all_issues() 的缓存, 不额外连库。
# ============================================================

def get_all_responsibles():
    """从 issues 表三个责任人字段提取所有不重复的姓名。
    处理规则: 跳过空值(None/空串/纯空格), 去首尾空格后去重, 排序返回。
    返回: 姓名列表(无数据时返回空列表)"""
    df = load_all_issues()
    if df.empty:
        return []
    names = set()
    for col in ("responsible_person_1", "responsible_person_2", "responsible_person_3"):
        if col not in df.columns:
            continue
        for v in df[col].dropna():
            name = str(v).strip()
            if name:
                names.add(name)
    return sorted(names)


def get_issues_by_person(person_name):
    """返回该人作为第一/第二/第三责任人任一角色的所有问题(DataFrame, 按序号升序)。
    匹配规则: 责任人字段值去首尾空格后与姓名完全相等(与
    get_all_responsibles 的清洗规则一致, 不会"提取出来却查不到")。
    参数: person_name - 责任人姓名"""
    df = load_all_issues()
    if df.empty:
        return df
    target = str(person_name).strip()
    mask = None
    for col in ("responsible_person_1", "responsible_person_2", "responsible_person_3"):
        if col not in df.columns:
            continue
        col_match = df[col].fillna("").astype(str).str.strip().eq(target)
        # 三个字段是"或"的关系: 命中任意一个字段即算该人的关联问题
        mask = col_match if mask is None else (mask | col_match)
    if mask is None:
        return df.iloc[0:0]  # 极端情况(表里没有责任人列) → 返回空结果
    return df[mask]


def get_role_of_person(issue_id, person_name):
    """判断某人在指定问题上担任第几责任人。
    返回: 1/2/3(第一/第二/第三责任人); 不是任何一个责任人时返回 0。
    说明:
      - 同一问题身兼多个角色的少见场景(如班长同时是第一、第二责任人),
        本函数返回最先命中的层级; 页面上的待办分组用"三个字段全查"的
        写法(app.py的_person_roles), 保证身兼多职也不漏任务。
      - 复用 load_all_issues() 缓存, 不额外连库。
    参数: issue_id - 问题编号; person_name - 责任人姓名"""
    df = load_all_issues()
    if df.empty or "id" not in df.columns:
        return 0
    rows = df[df["id"] == issue_id]
    if rows.empty:
        return 0
    row = rows.iloc[0]
    target = str(person_name).strip()
    for r in (1, 2, 3):
        col = "responsible_person_{}".format(r)
        if col in row.index and str(row[col] or "").strip() == target:
            return r
    return 0


# ============================================================
# 问题评审功能 (v0.8): 第三责任人的采纳/不采纳决策 + 不采纳问题查询
# 三个函数都复用 load_all_issues() 的缓存, 不额外连库。
# ============================================================

def _adopted_flag(value):
    """is_adopted 单元格值归一化: None/NaN/异常 → None; 正常 → int(1/0)。
    (Turso返回NULL时, pandas里该值可能是None也可能是NaN, 统一成好判断的形式)"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_pending_review_issues(person_name):
    """返回该第三责任人「待评审」的问题(DataFrame, 按序号升序)。
    待评审 = status='未评审' 且 is_adopted≠0 且 responsible_person_3=该人:
      - is_adopted=NULL → v0.8新登记的问题, 从未评审过
      - is_adopted=1    → v0.7及以前导入的旧数据, 未经过评审, 需重新决策
      - is_adopted=0    → 已被"不采纳"(状态仍为'未评审'), 不在待评审之列
                          (否则刚点完"不采纳"的问题会立刻弹回待评审列表)
    参数: person_name - 第三责任人姓名"""
    df = load_all_issues()
    if df.empty or "is_adopted" not in df.columns:
        return df.iloc[0:0]
    target = str(person_name).strip()
    mask = (
        df["status"].eq("未评审")
        & (df["is_adopted"].apply(_adopted_flag) != 0)
        & df["responsible_person_3"].fillna("").astype(str).str.strip().eq(target)
    )
    return df[mask]


def get_not_adopted_issues(person_name):
    """返回与该人相关的「不采纳」问题(DataFrame, 按序号升序):
    is_adopted=0 且 该人担任第二或第三责任人。
    用途: 「不采纳列表」页的可见性判断 —— 名下(二责/三责)有不采纳问题
    的人才能看到该页内容。"""
    df = load_all_issues()
    if df.empty or "is_adopted" not in df.columns:
        return df.iloc[0:0]
    target = str(person_name).strip()
    rejected = df["is_adopted"].apply(_adopted_flag) == 0
    p2 = df["responsible_person_2"].fillna("").astype(str).str.strip().eq(target)
    p3 = df["responsible_person_3"].fillna("").astype(str).str.strip().eq(target)
    return df[rejected & (p2 | p3)]


def set_issue_adoption(issue_id, is_adopted, new_status, reviewer,
                       review_result, review_comment):
    """执行一次问题评审决策(v0.8), 一个函数完成"改主表+记流水":
      ① 更新 issues.is_adopted(及可选的status)
      ② 向 issue_reviews 插入第三责任人(review_level=3)的评审记录
      ③ 清空两张表的缓存
    参数:
      issue_id      - 问题编号
      is_adopted    - 1=采纳 / 0=不采纳
      new_status    - 问题新状态; 传None表示不改status
                      (采纳/重新采纳→'已评审'; 不采纳→None保持'未评审')
      reviewer      - 评审人姓名(当前操作人, 即该问题的第三责任人)
      review_result - 审核结果: '通过'(采纳/重新采纳) / '退回'(不采纳)
      review_comment- 审核备注(如'评审采纳，进入整改流程')"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if new_status is None:
        # 不采纳: 只改采纳标记, 状态保持'未评审'(问题被搁置, 不进任何整改待办)
        _execute(
            "UPDATE issues SET is_adopted = ?, updated_at = ? WHERE id = ?",
            [1 if is_adopted else 0, now, issue_id],
        )
    else:
        # 采纳/重新采纳: 采纳标记与状态一起更新, 进入分层整改流程
        _execute(
            "UPDATE issues SET is_adopted = ?, status = ?, updated_at = ? WHERE id = ?",
            [1 if is_adopted else 0, new_status, now, issue_id],
        )
    # 评审决策同样留一条第3层审核流水(与分层审核共用流水表, 全程可追溯;
    # 该记录会被层级推导识别: l3通过+status=已评审 → 轮到第一责任人整改)
    add_review_record(issue_id=issue_id, review_level=3, reviewer=reviewer,
                      review_result=review_result, review_comment=review_comment)
    _clear_issues_cache()  # add_review_record只清审核缓存, 问题缓存在这里清
