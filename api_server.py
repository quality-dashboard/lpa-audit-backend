"""
api_server.py - LPA 移动端原型后端 (v1.0)
车间问题分层审核与跟踪系统 · API服务

功能: 提供 GET /api/tasks 接口 —— 按责任人姓名返回其关联问题(分三组),
      供移动端 HTML/JS 原型(frontend)读取真实 Turso 数据。

连接方式: 与本项目 Streamlit 版(db.py)完全一致的 Turso HTTP API
      (urllib 直调 /v2/pipeline 接口)。
      好处: 不引入 libsql-client 新依赖, 且连接代码已在产线环境验证过;
      依赖仅需 fastapi + uvicorn 两个。

启动命令(在项目目录执行):
    uvicorn api_server:app --reload --host 0.0.0.0 --port 8000
    (--reload 改代码自动重启, 演示/调试用; 正式部署可去掉)

前端访问: 保持 index.html 双击打开(file://协议),
      已启用CORS允许所有来源, 跨域请求本服务没问题。
"""
import json
import os
import urllib.request
from datetime import datetime
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ============================================================
# ★★★ Turso 数据库连接配置区 ★★★
# ============================================================
# TURSO_URL 数据库地址(非机密); 令牌不写在代码里, 按以下顺序读取:
#   1) 环境变量 TURSO_AUTH_TOKEN
#   2) 项目目录下 .streamlit/secrets.toml 里的 LPA_TURSO_TOKEN
#      (该文件已被 .gitignore 排除, 推送到Git不会泄露)
TURSO_URL = "libsql://lpaauditsystem-quality-dashboard.aws-ap-northeast-1.turso.io"


def _load_turso_token() -> str:
    """读取Turso令牌: 优先环境变量, 其次.streamlit/secrets.toml"""
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if token:
        return token
    # 尝试从 secrets.toml 解析 LPA_TURSO_TOKEN (简单逐行解析, 不引额外依赖)
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
    # 启动即报错并给出解决方法, 避免带着空令牌跑起来再莫名失败
    raise RuntimeError(
        "未找到Turso数据库令牌。请任选其一: "
        "1) 设置环境变量 TURSO_AUTH_TOKEN; "
        "2) 在项目目录 .streamlit/secrets.toml 中配置 LPA_TURSO_TOKEN"
    )
# ============================================================


# ------------------------------------------------------------
# Turso HTTP API 执行层(与 db.py 同款逻辑的精简版, 独立实现
# 是为了完全不动 Streamlit 项目的任何文件)
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
    """Python值 → Turso API参数格式。
    类型规则(与Streamlit版一致): int/bool→integer(字符串), float→float,
    text→text, None→null。"""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):   # bool必须在int之前判断(True也是int)
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


# ------------------------------------------------------------
# FastAPI 应用
# ------------------------------------------------------------

app = FastAPI(
    title="LPA 移动端原型 API",
    description="按责任人姓名返回其关联的车间问题列表",
    version="1.0",
)

# CORS: 允许所有来源 —— 前端用 file:// 双击打开时也能跨域访问本服务
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    """根路径: 服务状态自检(浏览器访问 http://127.0.0.1:8000 可见)"""
    return {
        "service": "LPA移动端原型API",
        "status": "运行中",
        "接口": "GET /api/tasks?person_name=责任人姓名",
        "数据库配置": "已配置" if TURSO_URL and TURSO_AUTH_TOKEN else "未配置(请填api_server.py顶部配置区)",
    }


@app.get("/api/tasks")
def get_tasks(person_name: str = Query(..., description="责任人姓名", min_length=1)):
    """核心接口: 返回该责任人【当前真正需要处理】的问题, 按角色分三组。

    过滤逻辑(与Streamlit版 db.compute_current_level 同一套分层审核状态机):
      ① 评审类记录(备注以【评审采纳】/【重新采纳】开头)不参与层级判断
      ② "跟踪中"记录不参与层级判断(只记过程, 不改层级)
      ③ 有效记录 = 排除①②后的"通过/退回"记录
      ④ 存在"退回"→ 只看最近一次退回【之后】的有效记录
      ⑤ 取筛选后最新一条有效记录推导当前层级:
         level=1通过 → 当前层级2(轮到第二责任人验收)
         level=2通过 → 当前层级3(轮到第三责任人确认)
         level=3通过 → 已关闭语义, 不出现在任何待处理组
         退回        → 当前层级1(打回第一责任人整改)
         无有效记录  → status为已评审/已退回 → 层级1; 否则(未评审)不返回
      ⑥ status='未评审'(还没被采纳)或'已关闭'的问题一律不返回
    分组规则: 当前层级与该人担任的角色对得上才进对应组
      (如问题当前在层级2, 只有第二责任人能在 as_level2 里看到它)。
    """
    # 1) 配置自检: 没填URL/Token时给出明确指引, 而不是报一堆网络错误
    if not TURSO_URL or not TURSO_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="Turso数据库未配置, 请填写api_server.py顶部的TURSO_URL和TURSO_AUTH_TOKEN")

    name = person_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="责任人姓名不能为空")

    # 2) 一次SQL查出该人关联的全部问题(按id升序)
    #    带出三个责任人字段+is_adopted: 用于分组判断/待评审过滤, 免第二次查库
    try:
        cols, rows = _execute(
            "SELECT id, problem_description, work_station, status, issue_date, "
            "responsible_person_1, responsible_person_2, responsible_person_3, is_adopted "
            "FROM issues "
            "WHERE responsible_person_1 = ? OR responsible_person_2 = ? "
            "   OR responsible_person_3 = ? "
            "ORDER BY id ASC",
            [name, name, name],
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # ---------- to_review: 第三责任人"待评审"问题(在分层审核之前的前置环节) ----------
    # 业务规则: 问题录入后status='未评审', 必须先由第三责任人评审(采纳/不采纳),
    # 采纳后才进入分层审核流程。所以未评审问题不算分层审核待办, 而是单独
    # 放进to_review组 —— 只对第三责任人生效(一责/二责的to_review恒为空数组)。
    # 过滤条件额外排除 is_adopted=0: 已被"不采纳"的问题不再出现在待评审列表
    # (否则刚点完不采纳的问题会立刻弹回待评审)。
    def _issue_obj(row):
        """cols顺序: id, desc, station, status, date, p1, p2, p3, adopted → 前端问题对象"""
        return {
            "id": row[0],
            "problem_description": row[1] or "",
            "work_station": row[2] or "",
            "status": row[3] or "",
            "issue_date": row[4] or "",
        }

    def _adopted_flag(v):
        """is_adopted 归一化: None(待评审)/1(采纳)/0(不采纳); 异常值按None处理"""
        return 0 if str(v) == "0" else 1

    to_review = [
        _issue_obj(r) for r in rows
        if r[3] == "未评审" and str(r[7] or "").strip() == name  # 该人是第三责任人
        and _adopted_flag(r[8]) != 0                             # 排除已"不采纳"的
    ]

    # ---------- closed: 与该人相关的已关闭问题(不分层级, 任一责任人即可) ----------
    # 供移动端"已处理"页签展示 —— 数据存在数据库里, 刷新/重开页面仍在(持久化),
    # 不再依赖前端会话临时状态; 待处理过滤逻辑(as_level1/2/3)不受影响。
    closed = [_issue_obj(r) for r in rows if r[3] == "已关闭"]

    # 未评审(还没被第三责任人采纳)和已关闭的问题不进分层审核待处理, 直接排除
    candidates = [r for r in rows if r[3] != "未评审" and r[3] != "已关闭"]

    as_level1, as_level2, as_level3 = [], [], []
    if not candidates:
        return {"as_level1": as_level1, "as_level2": as_level2,
                "as_level3": as_level3, "to_review": to_review, "closed": closed}

    # 3) 一次SQL拉取这些问题的全部审核流水(按流水id升序, 保证"最新"判断准确)
    issue_ids = [r[0] for r in candidates]
    placeholders = ",".join("?" for _ in issue_ids)
    try:
        _, review_rows = _execute(
            "SELECT id, issue_id, review_level, review_result, review_comment "
            "FROM issue_reviews WHERE issue_id IN ({}) "
            "ORDER BY id ASC".format(placeholders),
            issue_ids,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # 按问题编号归组流水: {issue_id: [{rid, level, result, comment}, ...]}
    reviews_by_issue = {}
    for rid, iid, level, result, comment in review_rows:
        reviews_by_issue.setdefault(iid, []).append(
            {"rid": rid, "level": level, "result": result, "comment": comment})

    # 4) 逐条问题推导"当前待处理层级", 再按层级和该人角色分组
    for row in candidates:
        # cols顺序: id, problem_description, work_station, status, issue_date, p1, p2, p3
        issue_id, desc, station, status, issue_date = row[0], row[1], row[2], row[3], row[4]
        p1, p2, p3 = (str(row[5] or "").strip(), str(row[6] or "").strip(),
                      str(row[7] or "").strip())

        level = _compute_current_level(reviews_by_issue.get(issue_id, []), status)
        if level is None:
            continue  # 已关闭语义/无有效记录且不该他处理 → 不进任何组

        # 组装问题对象(字段与现有前端兼容)
        issue_obj = {
            "id": issue_id,
            "problem_description": desc or "",
            "work_station": station or "",
            "status": status or "",
            "issue_date": issue_date or "",
        }
        # 层级1额外要求status∈(已评审,已退回) —— 与待处理语义一致
        if level == 1 and p1 == name and status in ("已评审", "已退回"):
            as_level1.append(issue_obj)
        elif level == 2 and p2 == name:
            as_level2.append(issue_obj)
        elif level == 3 and p3 == name:
            as_level3.append(issue_obj)

    return {
        "as_level1": as_level1,  # 需要我整改(第一责任人, 当前层级1)
        "as_level2": as_level2,  # 需要我验收(第二责任人, 当前层级2)
        "as_level3": as_level3,  # 需要我最终确认(第三责任人, 当前层级3)
        "to_review": to_review,  # 待我评审(第三责任人, status=未评审的前置评审环节)
        "closed": closed,        # 已处理(与该人相关的已关闭问题, 不分层级)
    }


# 评审类流水备注前缀 —— 命中任一前缀的记录是"评审采纳动作"而非真实验收,
# 不参与层级判断。含新旧两种格式(与Streamlit版 db.py 保持一致):
#   新格式(v0.9起): 【评审采纳】/【重新采纳】
#   旧格式(v0.8存量数据): "评审采纳，"/"二次评审采纳，"
_ADOPTION_COMMENT_PREFIXES = ("【评审采纳】", "【重新采纳】", "评审采纳，", "二次评审采纳，")


# ------------------------------------------------------------
# 分层审核层级推导(纯函数, 不连网)
# 与Streamlit版 db.compute_current_level 同一套规则, 但按移动端
# 需求做了两点收紧: ①"跟踪中"记录不参与判断 ②评审类记录不参与判断
# ------------------------------------------------------------
def _compute_current_level(records, issue_status):
    """按分层审核状态机推导问题"当前待处理层级"。
    参数:
      records      - 该问题的审核流水列表 [{rid, level, result, comment}](顺序不限)
      issue_status - issues 表当前 status
    返回: 1/2/3(当前待处理层级) 或 None(已关闭/未评审 → 不出现在待处理)
    """
    # ①② 过滤出"有效记录": 排除评审类(备注前缀)和跟踪中
    valid = []
    for r in records:
        comment = str(r.get("comment") or "").strip()
        if comment.startswith(_ADOPTION_COMMENT_PREFIXES):
            continue  # 评审采纳≠真实验收, 不参与层级判断(新旧前缀都排除)
        if r.get("result") == "跟踪中":
            continue  # 跟踪中只记过程, 不改层级
        valid.append(r)
    # 按流水id升序(保证"最新一条"判断准确)
    valid.sort(key=lambda r: r.get("rid", 0))

    # ④ 存在退回 → 只看最近一次退回之后的有效记录
    last_return_rid = None
    for r in valid:
        if r.get("result") == "退回":
            last_return_rid = r.get("rid")
    if last_return_rid is not None:
        valid = [r for r in valid if r.get("rid", 0) > last_return_rid]

    # ⑤ 取最新一条有效记录推导层级
    if valid:
        latest = valid[-1]
        if latest.get("result") == "通过":
            lv = latest.get("level")
            if lv == 1:
                return 2   # 第一责任人提交整改 → 轮到第二责任人验收
            if lv == 2:
                return 3   # 第二责任人验收通过 → 轮到第三责任人确认
            return None    # 第三责任人通过 → 已闭环, 不进待处理
        return 1           # 退回 → 打回第一责任人整改

    # 无有效记录: 已评审/已退回 → 层级1(等第一责任人整改); 其他(未评审)→不返回
    if issue_status in ("已评审", "已退回"):
        return 1
    return None


# ============================================================
# POST /api/review — 移动端审核写入接口(真实写入Turso)
# ============================================================

class ReviewRequest(BaseModel):
    """审核提交请求体(pydantic自动校验字段类型)"""
    issue_id: int            # 问题编号
    review_level: int        # 审核层级: 1=第一责任人 / 2=第二责任人 / 3=第三责任人
    reviewer: str            # 审核人姓名(须与数据库责任人字段一致)
    review_result: str       # 审核结果: 通过 / 退回 / 跟踪中
    review_comment: str      # 审核备注(通过=整改说明 / 退回=不合格原因 / 跟踪中=进展)


def _reject(message: str):
    """构造业务校验失败的统一响应(400 + success:false + 中文原因)"""
    return JSONResponse(status_code=400, content={"success": False, "message": message})


@app.post("/api/review")
def submit_review(req: ReviewRequest):
    """移动端审核写入: 插入审核流水 + 按分层审核规则更新问题状态。

    核心审核规则(与Streamlit版/任务定义一致):
      第1层(一责): 仅当前层级=1且status∈(已评审,已退回)可操作;
                  通过→status='已评审'(层级推进到2); 跟踪中→状态不变; 无退回
      第2层(二责): 仅当前层级=2可操作;
                  通过→status保持'已评审'(推进到3); 退回→'已退回'(回到1); 跟踪中→不变
      第3层(三责): 仅当前层级=3可操作;
                  通过→status='已关闭'(闭环); 退回→'已退回'(回到1); 跟踪中→不变
    安全校验: 审核人必须是该问题对应层级的责任人, 防止越权/乱提交。
    说明: 移动端只做真实验收, review_comment 不带评审采纳类前缀。
    """
    # ---------- 1) 参数合法性校验 ----------
    comment = (req.review_comment or "").strip()
    reviewer = (req.reviewer or "").strip()
    if not reviewer:
        return _reject("审核人姓名不能为空")
    if req.review_level not in (1, 2, 3):
        return _reject("review_level必须是1/2/3")
    if req.review_result not in ("通过", "退回", "跟踪中"):
        return _reject("review_result必须是: 通过/退回/跟踪中")
    if not comment:
        return _reject("备注必填(通过=整改说明 / 退回=不合格原因 / 跟踪中=当前进展)")

    # ---------- 2) 查问题是否存在 ----------
    try:
        cols, rows = _execute(
            "SELECT id, status, responsible_person_1, responsible_person_2, "
            "responsible_person_3 FROM issues WHERE id = ?",
            [req.issue_id],
        )
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("问题 #{} 不存在".format(req.issue_id))
    issue = rows[0]  # cols顺序: id, status, p1, p2, p3
    status = issue[1]
    p1, p2, p3 = (str(issue[2] or "").strip(), str(issue[3] or "").strip(),
                  str(issue[4] or "").strip())

    # ---------- 3) 越权校验: 审核人必须是该层对应的责任人 ----------
    level_owner = {1: p1, 2: p2, 3: p3}[req.review_level]
    if level_owner != reviewer:
        return _reject("{} 不是问题 #{} 的第{}责任人, 无权在该层操作".format(
            reviewer, req.issue_id, req.review_level))

    # ---------- 4) 层级校验: 复用与GET /api/tasks同一套状态机推导 ----------
    if status == "未评审":
        return _reject("问题 #{} 尚未评审(待第三责任人采纳), 不在分层审核范围".format(req.issue_id))
    if status == "已关闭":
        return _reject("问题 #{} 已关闭, 不能再审核".format(req.issue_id))
    if req.review_level == 1 and status not in ("已评审", "已退回"):
        return _reject("第一责任人只能对状态为已评审/已退回的问题操作")

    try:
        _, review_rows = _execute(
            "SELECT id, issue_id, review_level, review_result, review_comment "
            "FROM issue_reviews WHERE issue_id = ? ORDER BY id ASC",
            [req.issue_id],
        )
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    records = [{"rid": r[0], "level": r[2], "result": r[3], "comment": r[4]}
               for r in review_rows]
    current_level = _compute_current_level(records, status)
    if current_level != req.review_level:
        return _reject("问题 #{} 当前在第{}层待处理, 第{}责任人不能操作".format(
            req.issue_id, current_level or 0, req.review_level))

    # ---------- 5) 规则校验: 第一责任人没有"退回"动作 ----------
    if req.review_level == 1 and req.review_result == "退回":
        return _reject("第一责任人是整改方, 没有退回操作(只能通过/跟踪中)")

    # ---------- 6) 确定新状态并写入(插流水 + 按需更新status) ----------
    # 通过/退回改变status; 跟踪中只记过程, status与层级都不变
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_status = None
    if req.review_result == "通过":
        new_status = "已关闭" if req.review_level == 3 else "已评审"
    elif req.review_result == "退回":
        new_status = "已退回"

    try:
        # ① 插入审核流水(真实验收记录, 备注不带任何评审类前缀)
        _execute(
            "INSERT INTO issue_reviews (issue_id, review_level, reviewer, "
            "review_result, review_comment, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [req.issue_id, req.review_level, reviewer,
             req.review_result, comment, now, now],
        )
        # ② 需要变更status时同步更新主表(跟踪中不更新)
        if new_status and new_status != status:
            _execute(
                "UPDATE issues SET status = ?, updated_at = ? WHERE id = ?",
                [new_status, now, req.issue_id],
            )
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))

    msg = "审核已提交"
    if new_status == "已关闭":
        msg = "审核已提交, 问题 #{} 已闭环关闭".format(req.issue_id)
    elif new_status == "已退回":
        msg = "审核已提交, 问题 #{} 已退回第一责任人".format(req.issue_id)
    return {"success": True, "message": msg}


# ============================================================
# GET /api/history — 问题流转历史查询(移动端点击卡片查看)
# ============================================================

@app.get("/api/history")
def get_history(issue_id: int = Query(..., description="问题编号")):
    """返回指定问题的全部审核流水(含评审采纳/退回原因), 最新在前。

    查询 issue_reviews 表中该问题的所有记录, 按流水id倒序返回,
    前端逐条渲染即可还原完整流转过程(谁采纳、谁通过、谁退回、原因)。
    容错: issue_id 不存在或无任何记录 → 返回空数组(不抛错, 前端不崩)。
    """
    try:
        _, rows = _execute(
            "SELECT review_level, reviewer, review_result, review_comment, reviewed_at, id "
            "FROM issue_reviews WHERE issue_id = ? ORDER BY id DESC",  # 倒序: 最新在最前
            [issue_id],
        )
    except RuntimeError as e:
        # 数据库异常 → 返回success:false(前端弹窗内提示, 不让页面崩溃)
        return {"success": False, "message": "数据库查询失败: " + str(e)}

    history = [
        {
            "review_level": r[0],
            "reviewer": r[1] or "",
            "review_result": r[2] or "",
            "review_comment": r[3] or "",
            "reviewed_at": r[4] or "",
        }
        for r in rows
    ]
    return history


# ============================================================
# POST /api/review_decision — 第三责任人评审决策(采纳/不采纳)
# ============================================================

class ReviewDecisionRequest(BaseModel):
    """评审决策请求体(移动端"待我评审"列表的采纳/不采纳按钮)"""
    issue_id: int     # 问题编号
    reviewer: str     # 评审人姓名(必须是该问题的第三责任人)
    decision: str     # 决策: "adopt"=采纳 / "reject"=不采纳


@app.post("/api/review_decision")
def submit_review_decision(req: ReviewDecisionRequest):
    """第三责任人对未评审问题做采纳/不采纳决策(与Streamlit版规则一致):

    采纳(adopt):
      issues.is_adopted=1, status='已评审' → 进入分层整改流程(轮到第一责任人);
      issue_reviews 插第3层"通过"流水, 备注固定'【评审采纳】进入整改流程'
      (带【】前缀供层级推导/统计识别为评审动作而非真实验收)
    不采纳(reject):
      issues.is_adopted=0, status保持'未评审'(问题搁置, 不进任何整改待办);
      issue_reviews 插第3层"退回"流水, 备注固定'【评审不采纳】暂不整改'
    安全校验: 评审人必须是该问题的第三责任人; 问题必须处于未评审且未被
    评审过(is_adopted≠0表示已有决策, 防止重复评审)。
    """
    # ---------- 1) 参数校验 ----------
    reviewer = (req.reviewer or "").strip()
    if not reviewer:
        return _reject("评审人姓名不能为空")
    if req.decision not in ("adopt", "reject"):
        return _reject("decision必须是 adopt(采纳) 或 reject(不采纳)")

    # ---------- 2) 查问题是否存在 ----------
    try:
        cols, rows = _execute(
            "SELECT id, status, is_adopted, responsible_person_3 FROM issues WHERE id = ?",
            [req.issue_id],
        )
    except RuntimeError as e:
        return _reject("数据库查询失败: " + str(e))
    if not rows:
        return _reject("问题 #{} 不存在".format(req.issue_id))
    status, adopted, p3 = rows[0][1], rows[0][2], str(rows[0][3] or "").strip()

    # ---------- 3) 业务校验 ----------
    if p3 != reviewer:
        return _reject("{} 不是问题 #{} 的第三责任人, 无权评审".format(reviewer, req.issue_id))
    if status != "未评审":
        return _reject("问题 #{} 状态为'{}', 只能评审未评审状态的问题".format(req.issue_id, status))
    if str(adopted) == "0":
        return _reject("问题 #{} 已被评审为'不采纳', 不能重复评审(如需整改请在电脑端重新采纳)".format(req.issue_id))

    # ---------- 4) 写入(更新主表 + 插评审流水, 备注按规则固定) ----------
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if req.decision == "adopt":
        # 采纳: 标记+状态一起更新, 进入分层整改流程
        comment = "【评审采纳】进入整改流程"
        try:
            _execute(
                "UPDATE issues SET is_adopted = 1, status = '已评审', updated_at = ? WHERE id = ?",
                [now, req.issue_id],
            )
            _execute(
                "INSERT INTO issue_reviews (issue_id, review_level, reviewer, "
                "review_result, review_comment, reviewed_at, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [req.issue_id, 3, reviewer, "通过", comment, now, now],
            )
        except RuntimeError as e:
            return _reject("数据库写入失败: " + str(e))
        return {"success": True,
                "message": "评审完成: 已采纳, 问题 #{} 进入整改流程(第一责任人待办)".format(req.issue_id)}

    # 不采纳: 只改标记, 状态保持'未评审'(搁置, 不进分层审核)
    comment = "【评审不采纳】暂不整改"
    try:
        _execute(
            "UPDATE issues SET is_adopted = 0, updated_at = ? WHERE id = ?",
            [now, req.issue_id],
        )
        _execute(
            "INSERT INTO issue_reviews (issue_id, review_level, reviewer, "
            "review_result, review_comment, reviewed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [req.issue_id, 3, reviewer, "退回", comment, now, now],
        )
    except RuntimeError as e:
        return _reject("数据库写入失败: " + str(e))
    return {"success": True,
            "message": "评审完成: 不采纳, 问题 #{} 已搁置(可在电脑端重新采纳)".format(req.issue_id)}
