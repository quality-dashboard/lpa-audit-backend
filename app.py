"""
app.py - 车间问题分层审核与跟踪系统 (LPA) 主程序

v2.0 措施级重构: 系统从"问题级"流转升级为"措施级"流转。
  一个问题可拆成多条改善措施; 每条措施独立评审(采纳/不采纳)、独立走
  三层审核(完全符合/完全不符合/部分符合)、独立关闭;
  问题状态由措施状态实时汇总(未评审/已评审/已关闭), 不再人工维护。

标签页(v2.0):
  1. 问题登记    —— 新增问题(含多条措施录入区) + 问题列表(含删除)
  2. 问题评审    —— 第三责任人逐条措施评审(采纳/不采纳)
  3. 分层审核    —— 措施级三层审核(完全符合/完全不符合/部分符合)
  4. 不采纳列表  —— 所有 is_adopted=0 的措施(支持重新采纳)
  5. 个人行动计划 —— 措施粒度, 显示"问题摘要 + 措施内容"
  6. 分析看板    —— 问题状态 + 措施状态分布统计
"""
import io
import os
import random
import re
import string
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from PIL import Image

import auth_core  # v2.5 认证核心(角色常量/PBKDF2)

from notify import send_wechat_message

from config import (SYSTEM_NAME, VERSION, WORK_STATIONS, REVIEW_LEVELS, ISSUE_SOURCES,
                    LEVEL1_PERSONS, LEVEL2_PERSONS, LEVEL3_PERSONS)
from db import (
    init_db, save_issue_with_measures, save_issue_with_measures_pcs,
    load_all_issues, delete_issue, reset_issue_to_initial,
    load_all_measures, load_all_reviews,
    get_measures_by_issue, set_measure_adoption, add_measure_review_record,
    update_measure_status, update_measure_status_pcs,
    adopt_measure_pcs, assign_measure_dept_pcs, assign_measure_person_pcs,
    compute_measure_level, recompute_issue_status,
    is_measure_overdue, measure_overdue_days, is_measure_due_soon,
    load_dashboard_data, dashboard_kpis, load_dashboard_cycle,
    assign_measure_level2,  # v2.8 转交第二责任人(已随 QCS 废弃)
    get_measures_for_person, assign_issue_persons, get_all_person_names,
    get_person_names_by_level,
    _load_issues_cached, _load_measures_cached, _load_all_reviews_cached,
    auth,  # v2.5 认证服务实例(电脑端)
)

st.set_page_config(page_title=SYSTEM_NAME, page_icon="📋", layout="wide")

# ---------- 数据库初始化(首次运行自动建表+迁移, 幂等) ----------
try:
    init_db()
except Exception as e:
    st.error("数据库初始化失败: {}".format(e))
    st.stop()

# ============================================================
# v2.5 登录认证 Gate(渲染任何业务页面前先校验登录态)
# ============================================================

# 会话默认值(电脑端用 st.session_state 保存登录态)
for _k, _v in [("logged_in", False), ("username", ""), ("display_name", ""),
               ("role", ""), ("department", None), ("must_change_password", False)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


def _render_login():
    """登录页: 校验账号密码, 成功写入 session_state"""
    st.title("🔐 " + SYSTEM_NAME + " 登录")
    with st.form("login_form"):
        login_user = st.text_input("账号")
        login_pwd = st.text_input("密码", type="password")
        login_btn = st.form_submit_button("登录", use_container_width=True, type="primary")
    if login_btn:
        if not login_user or not login_pwd:
            st.error("请输入账号和密码")
        else:
            ok, acc, msg = auth.authenticate(
                login_user, login_pwd,
                client_type=auth_core.CLIENT_PC, ip="", user_agent="")
            if ok:
                st.session_state["logged_in"] = True
                st.session_state["username"] = acc.get("username")
                st.session_state["display_name"] = acc.get("display_name")
                st.session_state["role"] = acc.get("role")
                st.session_state["department"] = acc.get("department")
                st.session_state["must_change_password"] = bool(
                    int(acc.get("must_change_password") or 0))
                st.rerun()
            else:
                st.error(msg)


def _render_change_password():
    """首次登录强制改密页: 改完要求重新登录"""
    st.title("🔐 首次登录，请修改密码")
    st.info("账号「{}」为首次登录，请设置新密码后重新登录。".format(
        st.session_state["username"]))
    with st.form("change_pw_form"):
        new_pw = st.text_input("新密码(至少6位)", type="password")
        confirm_pw = st.text_input("确认新密码", type="password")
        btn = st.form_submit_button("确认修改", use_container_width=True, type="primary")
    if btn:
        if len(new_pw or "") < 6:
            st.error("密码至少 6 位")
        elif new_pw != confirm_pw:
            st.error("两次输入的密码不一致")
        else:
            auth.change_password(st.session_state["username"], new_pw)
            st.session_state["must_change_password"] = False
            st.success("密码已修改，请重新登录")
            # 清登录态, 要求用新密码重新登录
            for _k in ["logged_in", "username", "display_name", "role", "department"]:
                st.session_state[_k] = "" if _k != "logged_in" else (None if _k == "department" else False)
            st.rerun()


# Gate 1: 未登录 → 登录页
if not st.session_state["logged_in"]:
    _render_login()
    st.stop()

# Gate 2: 已登录但需改密 → 改密页
if st.session_state["must_change_password"]:
    _render_change_password()
    st.stop()

# 登录成功 → 锁定"当前操作人"为登录人姓名(与 issues 责任人姓名一致)
current_person = st.session_state["display_name"]

# ---------- 页头 ----------
st.title("📋 " + SYSTEM_NAME)
st.caption("{} | 措施级流转: 问题 → 多条措施 → 逐条评审/三层审核/独立关闭".format(VERSION))


# ============================================================
# 照片落盘工具(v2.0): 电脑端"完全符合"也要上传照片, 与移动端同规则
# (照片只存磁盘 uploads/, 库里只存相对路径; 后端二次压缩)
# ============================================================
_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")


def _compress_and_save_photo(uploaded_file):
    """把 Streamlit 上传的照片压缩(最长边<=1600, JPEG, <=500KB)后存到
    uploads/ 目录, 返回相对路径 /uploads/xxx.jpg。与 api_server 同规则。"""
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    raw = uploaded_file.getvalue()
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    else:
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > 1600:
        scale = 1600.0 / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    quality = 80
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    data = out.getvalue()
    while len(data) > 500 * 1024 and quality > 30:
        quality -= 10
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    filename = "{}_{}.jpg".format(ts, rand)
    with open(os.path.join(_UPLOAD_DIR, filename), "wb") as f:
        f.write(data)
    return "/uploads/" + filename


# ============================================================
# 侧边栏: 当前登录人(锁定, 不再手选) + 退出登录
# ============================================================
with st.sidebar:
    st.header("👤 当前操作人")
    # v2.5: 登录后锁定为登录账号对应姓名, 不允许切换他人(防冒用)
    st.markdown("**{}** · {}".format(
        st.session_state["display_name"],
        auth_core.ROLE_LABELS.get(st.session_state["role"], st.session_state["role"])))
    st.caption("已自动锁定为登录账号对应姓名")

    if st.button("🚪 退出登录", use_container_width=True):
        auth.logout_pc(st.session_state["username"])  # 记录退出时间
        for _k in ["logged_in", "username", "display_name", "role", "department", "must_change_password"]:
            if _k == "department":
                st.session_state[_k] = None
            else:
                st.session_state[_k] = False if _k in ("logged_in", "must_change_password") else ""
        st.rerun()

    if st.button("🔄 立即刷新数据", use_container_width=True,
                 help="手机端操作后电脑端没更新? 点此立即拉取最新数据"):
        _load_issues_cached.clear()
        _load_measures_cached.clear()
        _load_all_reviews_cached.clear()
        st.session_state["flash_success"] = "✅ 数据已刷新(问题+措施+审核记录均为最新)"
        st.rerun()

if "flash_success" in st.session_state:
    st.success(st.session_state.pop("flash_success"))
if "import_skips" in st.session_state:
    skips = st.session_state.pop("import_skips")
    st.warning("批量导入跳过的行: " + "; ".join(
        "{}({})".format(pos, reason) for pos, reason in skips))

_PLOTLY_CONFIG = {"displayModeBar": False}


def _notify_measure_action(issue, measure_content, level, action, operator):
    """按措施审核动作决定企微通知对象(v2.0 措施级)。
    - 完全符合 第1层 → 通知第二责任人验收; 第2层 → 通知第三责任人确认; 第3层 → 不发
    - 完全不符合 → 通知第一责任人继续整改(附退回人)
    - 部分符合 → 不发(措施没流转)"""
    disp_no = issue.get("序号", issue.get("id"))
    desc = str(issue.get("problem_description") or "")[:40]
    mc = str(measure_content or "")[:40]
    if action == "完全符合":
        if level == 1:
            receiver = str(issue.get("responsible_person_2") or "").strip()
            if receiver:
                send_wechat_message(
                    "【LPA审核流转】问题(序号{}): {}\n措施: {}\n第一责任人已提交整改, 请 {} 及时验收。".format(
                        disp_no, desc, mc, receiver))
        elif level == 2:
            receiver = str(issue.get("responsible_person_3") or "").strip()
            if receiver:
                send_wechat_message(
                    "【LPA审核流转】问题(序号{}): {}\n措施: {}\n第二责任人已验收通过, 请 {} 最终确认。".format(
                        disp_no, desc, mc, receiver))
    elif action == "完全不符合":
        receiver = str(issue.get("responsible_person_1") or "").strip()
        if receiver:
            send_wechat_message(
                "【LPA审核退回】问题(序号{}): {}\n措施: {}\n审核不通过, 请 {} 继续整改。退回人: {}".format(
                    disp_no, desc, mc, receiver, operator))


# ============================================================
# v2.7 角色权限: 模块可见性映射(管理级/业务级模块的唯一判定入口)
# ============================================================
# 四角色模型(系统管理员与经理层已分离):
#   admin    系统管理员        —— 只负责账号管理, 不进任何业务模块
#   manager  经理/第三责任人    —— 全部业务模块(含问题列表/问题评审/看板)
#   engineer 主管/工程师(第二责任人)
#   employee 普通员工(第一责任人)
#
# 两套身份仍取交集:
#   ① 账号角色(accounts.role) → 控制"模块可见性"(下方 _ALL_PAGES)
#   ② 问题责任位(issues.responsible_person_1/2/3) → 控制"业务数据"范围
#      (分层审核/个人行动计划 等仍按登录人姓名过滤)
# 问题登记模块内部再细分: "新增问题"三个业务角色可见, "问题列表"仅经理层。

# 模块定义: (key, 侧边栏标签, 允许访问的账号角色集合)
# v3.1 QCS: 三角色 + 部门。数据范围由 _current_department 配合过滤。
_ALL_PAGES = [
    ("register", "📝 问题台账",
     {auth_core.ROLE_ADMIN, auth_core.ROLE_MANAGER}),
    ("import", "📥 批量导入",
     {auth_core.ROLE_ADMIN}),
    ("dashboard", "📊 分析看板",
     {auth_core.ROLE_ADMIN}),
    ("accounts", "👥 账号管理",
     {auth_core.ROLE_ADMIN}),
]

# 页面函数路由表 _PAGE_ROUTES 定义在文件末尾 —— 必须等所有 page_xxx 定义完毕。


def _current_role():
    """当前登录账号角色。读取失败时兜底为 engineer(旧 employee 已废弃)。"""
    return st.session_state.get("role") or auth_core.ROLE_ENGINEER


def _current_department():
    """当前登录用户的部门(P/Q/T)。manager/admin 为 None(不限部门)。"""
    return st.session_state.get("department")


def _is_admin():
    return _current_role() == auth_core.ROLE_ADMIN


def _is_manager():
    return _current_role() == auth_core.ROLE_MANAGER


def _can_access(page_key):
    """当前角色是否有权访问某模块。
    这是入口二次校验的唯一判定函数 —— 即使侧边栏被绕过(如手动改
    session_state), 无权限模块也不会被渲染。"""
    role = _current_role()
    for key, _label, roles in _ALL_PAGES:
        if key == page_key:
            return role in roles
    return False


def _allowed_pages():
    """当前角色可访问的模块 [(key, label), ...], 顺序与 _ALL_PAGES 一致。"""
    role = _current_role()
    return [(k, lbl) for k, lbl, roles in _ALL_PAGES if role in roles]


def _visible_measures_df():
    """v3.1 QCS 两层分配: 返回当前用户【可见】的措施 DataFrame(已联表带问题字段)。
    admin 全部; manager 本部门; engineer 本人名下(owner)。"""
    role = _current_role()
    measures = load_all_measures()
    if measures.empty:
        return measures
    if role == auth_core.ROLE_MANAGER:
        # 经理只看"第一次分配给自己"的措施(不是按部门 —— 同部门可能有多个经理)
        nm = str(st.session_state.get("display_name") or "").strip()
        if not nm or "assigned_manager" not in measures.columns:
            return measures.iloc[0:0]
        measures = measures[
            measures["assigned_manager"].fillna("").astype(str).str.strip() == nm]
    elif role == auth_core.ROLE_ENGINEER:
        owner = str(st.session_state.get("display_name") or "").strip()
        if not owner or "owner" not in measures.columns:
            return measures.iloc[0:0]
        measures = measures[
            measures["owner"].fillna("").astype(str).str.strip() == owner]
    # admin: 不过滤(全部措施, 用于第一次分配)
    issues = load_all_issues()
    if issues.empty:
        return measures
    cols = [c for c in ["id", "序号", "problem_description", "work_station",
                        "issue_date", "issue_source", "department",
                        "status"] if c in issues.columns]
    return measures.merge(issues[cols], left_on="issue_id", right_on="id",
                          suffixes=("", "_issue"), how="inner")


# ------------------------------------------------------------
# 侧边栏: 功能菜单(只列出当前角色有权访问的模块)
# ------------------------------------------------------------
# 说明: 这里是第二个 st.sidebar 块, 内容顺序追加在"当前操作人/退出登录"下方。
# 必须放在 _allowed_pages() 定义之后 —— 模块级代码按顺序执行, 若放在前面
# 会因函数未定义而 NameError。
with st.sidebar:
    st.divider()
    st.subheader("🧭 功能菜单")

    _allowed = _allowed_pages()
    if not _allowed:
        # 工程师在电脑端无可访问模块(业务在移动端完成) → 友好提示, 不再报错
        if _current_role() == auth_core.ROLE_ENGINEER:
            st.info("📱 请使用移动端操作")
        else:
            st.error("当前角色没有任何可访问的模块, 请联系管理员。")
        st.stop()

    _nav_keys = [k for k, _lbl in _allowed]
    _nav_labels = [lbl for _k, lbl in _allowed]

    # 上次选中的模块若当前已无权限(如账号角色被调整), 自动回落到第一个可用模块
    _cur_key = st.session_state.get("nav_page")
    if _cur_key not in _nav_keys:
        _cur_key = _nav_keys[0]
        st.session_state["nav_page"] = _cur_key

    _picked_label = st.radio(
        "选择功能模块", _nav_labels, index=_nav_keys.index(_cur_key),
        label_visibility="collapsed")
    st.session_state["nav_page"] = _nav_keys[_nav_labels.index(_picked_label)]

    st.caption("当前角色共 {} 个可用模块".format(len(_allowed)))


# ============================================================
# 批量导入辅助函数(v2.0: Excel 措施自动拆分)
# ============================================================

def split_measures_from_cell(cell):
    """把 Excel"改善措施"单元格按换行拆成多条独立措施。

    处理规则(严格按需求):
      ① 按换行符(\\n / \\r\\n)拆分成多行
      ② 去除每行行首的数字编号和点号(如 "1." "2)" "3、" "１."), 只保留措施内容
      ③ 清洗每行首尾空白, 丢弃空行
      ④ 无换行的单条措施直接原样返回(仅清洗空白)
    返回: 措施内容列表(list[str]), 保留原始顺序; 空单元格返回空列表。
    """
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    text = str(cell)
    # 按换行拆分(兼容 \\n、\\r\\n、\\r 单独出现)
    lines = re.split(r"[\r\n]+", text)
    measures = []
    for line in lines:
        line = line.strip()
        if not line:
            continue  # 跳过空行
        # 去掉行首"数字 + 分隔符(. 、 ) ．)"开头的编号, 支持全角/半角数字
        line = re.sub(r"^[0-9０-９]+\s*[\.、\)）．]\s*", "", line).strip()
        if line:
            measures.append(line)
    return measures


def _find_excel_col(df, candidates):
    """在 DataFrame 列名里找第一个匹配的列(严格按真实表头, 兼容列名前后空格)。
    说明: Excel 导出的表头常带前后空格或不可见字符, 这里对列名做 strip 后再
    精确匹配, 避免因空格导致"缺少必要列"误判。
    返回: 原始列名; 找不到返回 None。"""
    # 建立 "去空格列名 -> 原始列名" 映射
    col_map = {str(c).strip(): c for c in df.columns}
    for cand in candidates:
        key = str(cand).strip()
        if key in col_map:
            return col_map[key]
    return None


def _normalize_date_value(v):
    """把 Excel 读出的日期值统一成 'YYYY-MM-DD' 字符串; 空值返回 ''。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if hasattr(v, "strftime"):
        try:
            return v.strftime("%Y-%m-%d")
        except Exception:
            pass
    return str(v).strip()


# ============================================================
# 标签页1「问题登记」: 新增问题(含措施录入) + 问题列表(含删除)
# ============================================================
def _render_issue_form():
    """v3.1 QCS: 录入问题表单(仅 manager 调用)。
    顺序: 问题描述 → 基础信息 → 原因分析 → 措施内容(可多条)。

    ★ 清空/重置一律不碰 widget 的 session_state key ——
      widget 实例化后对其 key 赋值或删除都会触发 StreamlitAPIException。
      做法:
      - 措施输入用 st.form(clear_on_submit=True), 添加后由 form 自动清空
      - 已添加的措施累积保存在 st.session_state["measure_list"]
      - 整表重置用 pcs_form_seq 序号 +1 换一批新 key, 旧 key 自然失效
      - 问题描述/基础信息在 form 外, 靠 widget key 保留, 添加措施不会丢失
    """
    if "measure_list" not in st.session_state:
        st.session_state["measure_list"] = []
    if "pcs_form_seq" not in st.session_state:
        st.session_state["pcs_form_seq"] = 0
    seq = st.session_state["pcs_form_seq"]   # 表单序号: 换 key 用, 实现安全重置

    # ---- 1. 问题描述(最上) ----
    st.markdown("### ① 问题描述")
    problem_description = st.text_area(
        "问题描述 *", height=100, key="pcs_desc_{}".format(seq),
        placeholder="发生了什么、在哪发现、有什么影响")

    # ---- 2. 基础信息 ----
    st.markdown("### ② 基础信息")
    c_left, c_right = st.columns(2)
    with c_left:
        issue_date = st.date_input("发现日期", value=date.today(),
                                   key="pcs_date_{}".format(seq))
        issue_source = st.selectbox("问题来源", ISSUE_SOURCES,
                                    key="pcs_source_{}".format(seq))
        if WORK_STATIONS:
            work_station = st.selectbox("工位 *", WORK_STATIONS,
                                        key="pcs_ws_{}".format(seq))
        else:
            work_station = st.text_input("工位 *", key="pcs_ws_{}".format(seq),
                                         placeholder="如: 车削1工位")
    with c_right:
        part_no = st.text_input("零件", key="pcs_part_{}".format(seq),
                                placeholder="如: 齿轮毛坯")
        model = st.text_input("型号", key="pcs_model_{}".format(seq),
                              placeholder="如: GH-300")
        problem_level = st.text_input("问题等级", key="pcs_level_{}".format(seq),
                                      placeholder="如: A / B / C")

    # ---- 3. 原因分析 ----
    st.markdown("### ③ 原因分析")
    reason_analysis = st.text_area("原因分析", height=60,
                                   key="pcs_reason_{}".format(seq), placeholder="可留空")

    # ---- 4. 措施内容(可多条; form 提交后自动清空输入框) ----
    st.markdown("### ④ 措施内容（至少一条）")
    with st.form("measure_add_form", clear_on_submit=True):
        m_content = st.text_input("措施内容", placeholder="如: 更换磨损刀具")
        add_submitted = st.form_submit_button("➕ 添加", use_container_width=True)

    if add_submitted:
        content = str(m_content or "").strip()
        if not content:
            st.session_state["pcs_m_warn"] = True
        else:
            st.session_state["measure_list"].append(content)
            st.session_state["pcs_m_warn"] = False

    if st.session_state.pop("pcs_m_warn", False):
        st.warning("措施内容不能为空")

    if st.session_state["measure_list"]:
        st.caption("已添加 {} 条措施:".format(len(st.session_state["measure_list"])))
        for i, content in enumerate(st.session_state["measure_list"]):
            cc1, cc2 = st.columns([9, 1])
            cc1.write("{}. {}".format(i + 1, content))
            if cc2.button("删", key="pcs_del_{}_{}".format(seq, i)):
                st.session_state["measure_list"].pop(i)
                st.rerun()

    st.markdown("---")
    if st.button("💾 提交问题", key="pcs_submit", type="primary", use_container_width=True):
        desc = str(problem_description or "").strip()
        ws = str(work_station or "").strip()
        tips = []
        if not desc:
            tips.append("问题描述必填")
        if not ws:
            tips.append("工位必填")
        if not st.session_state["measure_list"]:
            tips.append("请至少添加一条措施")
        if tips:
            st.warning("；".join(tips))
        else:
            try:
                save_issue_with_measures_pcs(
                    problem_description=desc,
                    issue_date=issue_date.strftime("%Y-%m-%d"),
                    work_station=ws,
                    measures=list(st.session_state["measure_list"]),
                    issue_source=str(issue_source).strip(),
                    part_no=str(part_no or "").strip(),
                    model=str(model or "").strip(),
                    problem_level=str(problem_level or "").strip(),
                    reason_analysis=str(reason_analysis or "").strip(),
                )
                n = len(st.session_state["measure_list"])
                st.session_state["flash_success"] = "✅ 问题已登记(含{}条措施, 待经理评审)".format(n)
                # 重置表单: 措施列表清空 + 表单序号 +1(换一批新 widget key, 字段自然为空)
                # ★ 不 pop / 不赋值任何 widget key, 避免 StreamlitAPIException
                st.session_state["measure_list"] = []
                st.session_state["pcs_form_seq"] = seq + 1
                st.rerun()
            except Exception as e:
                st.error("保存失败: {}".format(e))


def page_register():
    """页面「问题台账」(v3.1 QCS): manager 可录入+看全量; engineer 只读本部门。"""
    st.subheader("📝 问题台账")

    role = _current_role()

    # ---- 录入区(仅 admin) ----
    if role == auth_core.ROLE_ADMIN:
        with st.expander("➕ 录入新问题（含措施）", expanded=False):
            _render_issue_form()

    # ---- 问题列表 ----
    df = load_all_issues()
    if df.empty:
        st.info("暂无问题记录。")
        return

    all_ms = load_all_measures()

    # manager 只看"有措施第一次分配给自己"的问题(与移动端口径一致, 按 assigned_manager)
    if role == auth_core.ROLE_MANAGER:
        nm = str(st.session_state.get("display_name") or "").strip()
        if not nm or all_ms.empty or "assigned_manager" not in all_ms.columns:
            df = df.iloc[0:0]
        else:
            my_ids = set(
                all_ms[all_ms["assigned_manager"].fillna("").astype(str).str.strip() == nm]
                ["issue_id"].astype(str).tolist())
            df = df[df["id"].astype(str).isin(my_ids)]

    if df.empty:
        st.info("暂无可见问题。")
        return

    # ---- 筛选栏(仅电脑端): 型号 / 问题来源 / 工序 / 类型ABC, 支持多条件组合 ----
    def _uniq(col):
        if col not in df.columns:
            return []
        return sorted([x for x in df[col].dropna().astype(str).str.strip().unique().tolist() if x])

    model_opts = ["全部"] + _uniq("model")
    station_opts = ["全部"] + _uniq("work_station")
    source_opts = ["全部", "监造提出", "过程改善发现"]
    level_opts = ["全部", "A", "B", "C"]

    # f_seq: 点"重置"时 +1 换 key, 让四个下拉框回到"全部"(不碰 widget key)
    f_seq = st.session_state.get("f_seq", 0)
    with st.expander("🔍 筛选（型号 / 问题来源 / 工序 / 类型ABC）", expanded=False):
        c1, c2, c3, c4, c5 = st.columns([1.1, 1.1, 1.1, 1.1, 0.7])
        with c1:
            f_model = st.selectbox("型号", model_opts, key="f_model_{}".format(f_seq))
        with c2:
            f_source = st.selectbox("问题来源", source_opts, key="f_source_{}".format(f_seq))
        with c3:
            f_station = st.selectbox("工序", station_opts, key="f_station_{}".format(f_seq))
        with c4:
            f_level = st.selectbox("类型ABC", level_opts, key="f_level_{}".format(f_seq))
        with c5:
            st.write("")
            if st.button("🔄 重置", key="f_reset_{}".format(f_seq), use_container_width=True):
                st.session_state["f_seq"] = f_seq + 1
                st.rerun()

    # 应用筛选(可多条件同时生效)
    def _apply(d, cond, col, val):
        if cond and col in d.columns:
            return d[d[col].fillna("").astype(str).str.strip() == val]
        return d

    df = _apply(df, f_model != "全部", "model", f_model)
    df = _apply(df, f_source != "全部", "issue_source", f_source)
    df = _apply(df, f_station != "全部", "work_station", f_station)
    # 类型ABC: 库中 problem_level 存的是 "A类项点/B类项点/C类项点", 故用包含匹配
    if f_level != "全部" and "problem_level" in df.columns:
        df = df[df["problem_level"].fillna("").astype(str).str.contains(f_level, case=False)]

    if df.empty:
        st.info("筛选后无匹配问题。")
        return

    cnt = df["status"].value_counts().to_dict()
    st.caption("共 {} 条 | 未开始 {} | 进行中 {} | 已关闭 {}".format(
        len(df), cnt.get("未开始", 0), cnt.get("进行中", 0), cnt.get("已关闭", 0)))

    # ---- 状态颜色图例(固定在列表上方, 便于对照) ----
    st.markdown(
        "**图例：** 🔴 有措施逾期未完成　｜　🟠 有措施进行中且未逾期　｜　"
        "🟢 所有措施已关闭　｜　🟢·曾逾期 全部关闭但存在曾逾期措施　｜　"
        "⚪ 未开始（无已采纳措施）")

    # ---- 问题列表: 折叠问题块(不用传统表格) ----
    # 标题行: 状态圆圈 + 问题序号 + 问题描述 + 日期 + 工位 + 来源 + 完成状态
    # ★ 不显示创建时间/录入人(按要求移除)

    total_issues = len(df)
    list_pages = max(1, (total_issues + 19) // 20)
    if list_pages > 1:
        list_page = st.number_input("页码", min_value=1, max_value=list_pages,
                                    value=1, step=1, key="reg_list_page")
    else:
        list_page = 1
    page_df = df.iloc[(int(list_page) - 1) * 20:int(list_page) * 20]

    for _, row in page_df.iterrows():
        iid = int(row["id"])
        no = row.get("序号", iid)
        desc = str(row.get("problem_description") or "(无描述)")
        short = desc if len(desc) <= 60 else desc[:60] + "…"
        prog, _c, circle = _issue_progress(all_ms, iid)
        title = "{} 问题 {} ｜ {} ｜ {} ｜ {} ｜ {} ｜ {}".format(
            circle, no, short,
            str(row.get("issue_date") or "-"),
            str(row.get("work_station") or "-"),
            str(row.get("issue_source") or "-"),
            prog)
        with st.expander(title, expanded=False):
            # 展开后: 措施箭头链(内容 → 一次分配经理 → 二次分配工程师 [状态])
            _render_measures_chain(all_ms, iid, no, role, show_actions=True)

            # ---- 块内操作(仅管理员) ----
            # ★ 不再有独立"操作"列/下拉框; 操作都在问题块内部
            if role == auth_core.ROLE_ADMIN:
                st.markdown("---")
                # ① 恢复初始状态: 块内常驻小按钮
                if st.button("🔄 恢复初始状态", key="blk_reset_{}".format(iid),
                             use_container_width=True):
                    st.session_state["blk_confirm_reset"] = iid
                if st.session_state.get("blk_confirm_reset") == iid:
                    st.warning("确认恢复「问题 {}」？措施的状态/责任人/部门/日期/逾期标记/"
                               "状态日志会被清空; 问题与措施内容保留, 回到待分配状态。".format(no))
                    r1, r2 = st.columns(2)
                    with r1:
                        if st.button("✅ 确认恢复", key="blk_ok_reset_{}".format(iid),
                                     type="primary", use_container_width=True):
                            try:
                                reset_issue_to_initial(iid)
                                st.session_state.pop("blk_confirm_reset", None)
                                st.session_state["flash_success"] = "✅ 问题 {} 已恢复初始状态".format(no)
                                st.rerun()
                            except Exception as e:
                                st.error("恢复失败: {}".format(e))
                    with r2:
                        if st.button("❌ 取消", key="blk_cancel_reset_{}".format(iid),
                                     use_container_width=True):
                            st.session_state.pop("blk_confirm_reset", None)
                            st.rerun()

                # ② 删除: 默认不显示按钮 —— 需先勾选"启用删除"才出现, 避免误触
                if st.checkbox("⚠️ 启用删除", key="blk_showdel_{}".format(iid), value=False):
                    if st.button("🗑️ 删除该问题", key="blk_del_{}".format(iid),
                                 use_container_width=True):
                        st.session_state["blk_confirm_del"] = iid
                    if st.session_state.get("blk_confirm_del") == iid:
                        st.error("确认删除「问题 {}」？该问题下的措施与状态记录会一并删除, 不可恢复。".format(no))
                        d1, d2 = st.columns(2)
                        with d1:
                            if st.button("✅ 确认删除", key="blk_ok_del_{}".format(iid),
                                         type="primary", use_container_width=True):
                                try:
                                    delete_issue(iid)
                                    st.session_state.pop("blk_confirm_del", None)
                                    st.session_state["flash_success"] = "✅ 问题 {} 已删除".format(no)
                                    st.rerun()
                                except Exception as e:
                                    st.error("删除失败: {}".format(e))
                        with d2:
                            if st.button("❌ 取消", key="blk_cancel_del_{}".format(iid),
                                         use_container_width=True):
                                st.session_state.pop("blk_confirm_del", None)
                                st.rerun()


def page_measures():
    """页面「措施管理」(v3.1 QCS): 两层(待评审/已评审) + 问题分组 + 子序号 1.1/1.2
    + 筛选 + 分页。manager 全部; engineer 本部门。"""
    head_col, btn_col = st.columns([6, 1.4])
    with head_col:
        st.subheader("🔧 措施管理")
    with btn_col:
        st.write("")
        # 移动端在别的进程写库, 电脑端缓存感知不到 —— 提供手动强制刷新入口
        if st.button("🔄 刷新数据", use_container_width=True,
                     help="清除本地缓存并重新查库(移动端改动后点此立即同步)"):
            st.cache_data.clear()
            st.session_state["flash_success"] = "✅ 已刷新, 当前为数据库最新数据"
            st.rerun()

    df = _visible_measures_df()
    if df.empty:
        st.info("当前没有可见措施。")
        return

    # 逾期统计
    if "due_date" in df.columns and "status" in df.columns:
        ov_mask = df.apply(
            lambda r: is_measure_overdue(r.get("due_date"), r.get("status")), axis=1)
        overdue_n = int(ov_mask.sum())
    else:
        overdue_n = 0

    # ---- 筛选区(默认折叠, 避免占用视野) ----
    with st.expander("🔍 筛选（状态 / 部门 / 责任人 / 逾期）", expanded=False):
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            status_sel = st.multiselect("状态", auth_core.MEASURE_STATUSES,
                                        key="qcs_f_status")
        with f2:
            dept_sel = st.multiselect("部门", list(auth_core.DEPARTMENTS) + ["(未分配)"],
                                      key="qcs_f_dept")
        with f3:
            owner_kw = st.text_input("责任人含", key="qcs_f_owner", placeholder="关键字")
        with f4:
            only_overdue = st.checkbox("仅看逾期", key="qcs_f_overdue")

    show = df
    if status_sel:
        show = show[show["status"].astype(str).isin(status_sel)]
    if dept_sel:
        dept_series = show["department"].fillna("").astype(str).str.strip().replace("", "(未分配)")
        show = show[dept_series.isin(dept_sel)]
    if owner_kw and "owner" in show.columns:
        show = show[show["owner"].fillna("").astype(str).str.contains(owner_kw, case=False)]
    if only_overdue:
        show = show[show.apply(
            lambda r: is_measure_overdue(r.get("due_date"), r.get("status")), axis=1)]

    st.caption("显示 {} / 共 {} 条 · 逾期 {} 条".format(len(show), len(df), overdue_n))

    if show.empty:
        st.info("无符合筛选条件的措施。")
        return

    # ---- 分页(数据量大时避免一次渲染几百条导致卡顿) ----
    page_df = show
    PAGE_SIZE = 30
    total_pages = max(1, (len(show) + PAGE_SIZE - 1) // PAGE_SIZE)
    if total_pages > 1:
        page_no = st.number_input("页码", min_value=1, max_value=total_pages,
                                  value=1, step=1, key="qcs_page")
        start = (int(page_no) - 1) * PAGE_SIZE
        page_df = show.iloc[start:start + PAGE_SIZE]
        st.caption("第 {} / {} 页 · 本页 {} 条".format(int(page_no), total_pages, len(page_df)))

    role = _current_role()

    # ---- 第一层: 待评审措施(admin=待分配部门; manager=待评审采纳) ----
    not_reviewed = page_df[page_df["status"].astype(str) == "待评审"]
    st.markdown("## 一、待评审措施 · {} 条".format(len(not_reviewed)))
    if not_reviewed.empty:
        st.caption("暂无待评审措施")
    else:
        _render_measures_by_issue(not_reviewed, role, expanded=True)

    st.markdown("---")

    # ---- 第二层: 已评审措施(采纳后落到这里; 已采纳待二次分配/执行/关闭) ----
    reviewed = page_df[page_df["status"].astype(str) != "待评审"]
    st.markdown("## 二、已评审措施 · {} 条".format(len(reviewed)))
    if reviewed.empty:
        st.caption("暂无已评审措施")
    else:
        _render_measures_by_issue(reviewed, role, expanded=False)


def _measure_status_color(m):
    """措施状态颜色(与移动端 statusColorOf 口径一致)。
    逾期未完成红 / 待评审橙黄 / 已采纳紫 / 不采纳红 / 待执行·执行中蓝 / 已完成绿 / 已关闭灰
    """
    if is_measure_overdue(m.get("due_date"), m.get("status")):
        return "#e03131"
    return {
        "待评审": "#e8590c",
        "已采纳": "#7048e8",
        "不采纳": "#c92a2a",
        "待执行": "#3d5a80",
        "执行中": "#1971c2",
        "已完成": "#2f9e44",
        "已关闭": "#868e96",
    }.get(str(m.get("status") or ""), "#3d5a80")


def _issue_progress(all_ms, issue_id):
    """计算问题完成状态 → (文本, 颜色hex, 圆圈emoji)。

    口径: 只统计【已采纳及之后】的措施(MEASURE_ACTIVE_STATUSES)。
          ★ 待评审(尚未采纳)与不采纳都不参与进度计算 ——
            若算进去, 有被拒措施的问题永远到不了 100%。
    颜色: 有措施逾期未完成→红; 未完成未逾期→橙; 全部关闭→绿(曾逾期加"曾逾期")。
    """
    if all_ms is None or all_ms.empty or "status" not in all_ms.columns:
        return "未开始", "#868e96", "⚪"
    sub = all_ms[all_ms["issue_id"].astype(str) == str(issue_id)]
    if sub.empty:
        return "未开始", "#868e96", "⚪"
    st_series = sub["status"].astype(str)
    active = sub[st_series.isin(auth_core.MEASURE_ACTIVE_STATUSES)]
    if active.empty:
        return "未开始", "#868e96", "⚪"
    total = len(active)
    closed = int((active["status"].astype(str) ==
                  auth_core.MEASURE_STATUS_CLOSED).sum())
    overdue_now = False
    ever_overdue = False
    for _, mm in active.iterrows():
        if is_measure_overdue(mm.get("due_date"), mm.get("status")):
            overdue_now = True
        try:
            if int(mm.get("completed_overdue") or 0) == 1:
                ever_overdue = True
        except (TypeError, ValueError):
            pass
    if closed >= total:
        return ("已关闭 · 曾逾期" if ever_overdue else "已关闭"), "#2f9e44", "🟢"
    if overdue_now:
        return "{}/{} · 逾期".format(closed, total), "#e03131", "🔴"
    return "{}/{}".format(closed, total), "#e8590c", "🟠"


def _render_measures_chain(measures_df, issue_id, issue_no, role,
                           show_actions=True):
    """按【箭头链】渲染某问题下的措施 —— 问题台账与措施管理共用。

    每行格式: 1.1 措施内容 → 一次分配(部门经理) → 二次分配(工程师) [状态] ●
    示例:     1.1 调整砂轮修整频率 → 严峰 → 张兵 [执行中] 🔵
    """
    if measures_df is None or measures_df.empty:
        st.caption("该问题下暂无措施")
        return
    sub = measures_df[measures_df["issue_id"].astype(str) == str(issue_id)]
    if sub.empty:
        st.caption("该问题下暂无措施")
        return

    for idx, (_, m) in enumerate(sub.iterrows(), start=1):
        stt = str(m.get("status") or "")
        color = _measure_status_color(m)
        circle = {
            "#e03131": "🔴", "#c92a2a": "🔴",
            "#e8590c": "🟠", "#7048e8": "🟣",
            "#3d5a80": "🔵", "#1971c2": "🔵",
            "#2f9e44": "🟢", "#868e96": "⚪",
        }.get(color, "🔵")
        mgr = str(m.get("assigned_manager") or "").strip() or "未分配"
        eng = str(m.get("owner") or "").strip() or "未分配"
        sub_no = "{}.{}".format(issue_no, m.get("measure_order") or idx)

        overdue_html = ""
        if is_measure_overdue(m.get("due_date"), stt):
            od = measure_overdue_days(m.get("due_date"), stt)
            overdue_html = (' <span style="color:#e03131;font-weight:bold;">'
                            '· 逾期{}天</span>'.format(od))

        st.markdown(
            "**{}** {} → {} → {} **[{}]** {}{}".format(
                sub_no, m.get("measure_content") or "(无措施)",
                mgr, eng, stt or "-", circle, overdue_html),
            unsafe_allow_html=True)
        if show_actions:
            _render_measure_actions(m, role)


def _render_measures_by_issue(sub, role, expanded=False):
    """把一组措施按【问题】分组展示 —— 与问题台账同一套视觉:
    问题为折叠块(标题含完成状态圆圈), 展开后措施用箭头链展示。

    ★管理员: 所有状态的措施都保留显示(待评审/已采纳/执行中/已完成/已关闭/不采纳),
      分配或评审后措施不会从列表中消失。
    """
    sub = sub.copy()
    # 问题序号: 复用 issues 的全局"序号"列(与问题台账完全一致)
    sub["_ino"] = pd.to_numeric(sub.get("序号"), errors="coerce").fillna(0).astype(int)

    # 全量措施一次取出, 用于计算每个问题的完成进度(避免循环内重复查库)
    all_ms = load_all_measures()
    # 自动展开策略: 待评审层条目不多(<=10)时才整体展开, 避免条目多时页面过长卡顿
    auto_open_all = bool(expanded) and (len(sub) <= 10)

    for ino in sorted(sub["_ino"].unique().tolist()):
        grp = sub[sub["_ino"] == ino]
        if grp.empty:
            continue
        first = grp.iloc[0]
        iid = int(first.get("issue_id") or 0)

        # ---- 问题折叠块标题: 圆圈 + 序号 + 描述 + 部门 + 工位 + 完成状态 ----
        dlabel = auth_core.DEPARTMENT_LABELS.get(
            str(first.get("department") or "").strip(), "")
        ws = str(first.get("work_station") or "").strip()
        prog, _c, circle = _issue_progress(all_ms, iid)
        desc = str(first.get("problem_description") or "(无描述)")
        short = desc if len(desc) <= 50 else desc[:50] + "…"
        title = "{} 问题 {} ｜ {}".format(circle, ino, short)
        if dlabel:
            title += " ｜ " + dlabel
        if ws:
            title += " ｜ " + ws
        title += " ｜ " + prog

        with st.expander(title, expanded=auto_open_all):
            # 完整问题描述(不截断, 换行显示)
            st.markdown(desc)
            # 措施箭头链: 内容 → 一次分配(经理) → 二次分配(工程师) [状态]
            _render_measures_chain(grp, iid, ino, role, show_actions=True)


def _clear_manager_cache():
    """账号增删 / 改角色 / 改部门后调用, 让经理候选人立即失效重取。"""
    try:
        _manager_options_cached.clear()
    except Exception:
        pass


@st.cache_data(ttl=60, show_spinner=False)
def _manager_options_cached():
    """部门经理候选人(★ 性能关键, 必须缓存)。

    原实现在每条措施的渲染分支里直接查 accounts 表:
      远程 Turso 单次查询约 400ms, 一页 30 条措施 = 30 次 = 12.5 秒白等。
    缓存后一页只查 1 次(60 秒内), 账号变更时由 _clear_manager_cache 主动失效。
    """
    out = []
    try:
        for a in auth.list_accounts():
            if a.get("role") != auth_core.ROLE_MANAGER:
                continue
            if str(a.get("status") or "") != "active":
                continue
            dept = str(a.get("department") or "").strip()
            if dept not in auth_core.DEPARTMENTS:
                continue
            name = a.get("display_name") or a.get("username") or ""
            out.append({
                "label": "{} ｜ {}（{}）".format(
                    auth_core.DEPARTMENT_LABELS.get(dept, dept), name, a.get("username")),
                "name": name,
                "dept": dept,
            })
    except Exception:
        pass
    return out


def _manager_options():
    """返回可分配的【部门经理】列表 —— 管理员第一次分配的目标只能是经理层。

    候选条件: role=manager 且 status=active 且 department ∈ P/Q/T。
    ★ 不含工程师: 工程师由部门经理在第二次分配时指定, 管理员不得越级分配。
    """
    return _manager_options_cached()


def _render_measure_actions(m, role):
    """渲染单条措施的操作区(按角色):
    admin   —— 第一次分配(待评审 + 未分配部门 → 选 P/Q/T)
    manager —— 评审(待评审→采纳/不采纳) + 二次分配(已采纳→选工程师) + 验收关闭
    """
    mid = int(m["id"])
    stt = str(m.get("status") or "")
    dept = str(m.get("department") or "").strip()

    if role == auth_core.ROLE_ADMIN:
        # 管理员: 第一次分配 —— 目标只能是部门经理(不得越级分给工程师)
        if stt == "待评审" and not dept:
            mgrs = _manager_options()
            if not mgrs:
                st.warning("暂无可分配的部门经理。请先在「账号管理」创建 role=经理 "
                           "且部门为 P/Q/T 的账号。")
            else:
                c1, c2 = st.columns([2.4, 1])
                with c1:
                    mgr = st.selectbox("分配给部门经理", options=mgrs,
                                       format_func=lambda x: x["label"],
                                       key="pcs_mgr_{}".format(mid))
                with c2:
                    st.write("")
                    if st.button("分配", key="pcs_as_{}".format(mid), use_container_width=True):
                        try:
                            assign_measure_dept_pcs(mid, mgr["dept"], current_person, mgr["name"])
                            st.session_state["flash_success"] = "✅ 已分配给 {}".format(mgr["label"])
                            st.rerun()
                        except Exception as e:
                            st.error("分配失败: {}".format(e))
        else:
            st.caption("已分配部门, 等待经理评审")

    elif role == auth_core.ROLE_MANAGER:
        if stt == "待评审":
            c1, c2 = st.columns(2)
            if c1.button("✅ 采纳", key="pcs_adopt_{}".format(mid), use_container_width=True):
                try:
                    adopt_measure_pcs(mid, "adopt", current_person)
                    st.session_state["flash_success"] = "✅ 措施已采纳, 待二次分配工程师"
                    st.rerun()
                except Exception as e:
                    st.error("采纳失败: {}".format(e))
            if c2.button("❌ 不采纳", key="pcs_reject_{}".format(mid), use_container_width=True):
                try:
                    adopt_measure_pcs(mid, "reject", current_person)
                    st.session_state["flash_success"] = "✅ 措施已不采纳"
                    st.rerun()
                except Exception as e:
                    st.error("不采纳失败: {}".format(e))
        elif stt == "不采纳":
            if st.button("🔁 重新采纳", key="pcs_readopt_{}".format(mid)):
                try:
                    adopt_measure_pcs(mid, "adopt", current_person)
                    st.session_state["flash_success"] = "✅ 措施已重新采纳, 待二次分配工程师"
                    st.rerun()
                except Exception as e:
                    st.error("重新采纳失败: {}".format(e))
        elif stt == "已采纳":
            # 二次分配: 指定本部门工程师 + 预计完成
            c1, c2, c3 = st.columns([2, 1.8, 1])
            with c1:
                a_owner = st.text_input("工程师", key="pcs_ao_{}".format(mid))
            with c2:
                a_due = st.date_input("预计完成", key="pcs_au_{}".format(mid), value=date.today())
            with c3:
                st.write("")
                if st.button("分配", key="pcs_as_{}".format(mid), use_container_width=True):
                    if not str(a_owner or "").strip():
                        st.error("请填写工程师姓名")
                    else:
                        try:
                            assign_measure_person_pcs(
                                mid, str(a_owner or "").strip(),
                                a_due.strftime("%Y-%m-%d"), current_person)
                            st.session_state["flash_success"] = "✅ 措施已分配给工程师 {}".format(a_owner)
                            st.rerun()
                        except Exception as e:
                            st.error("分配失败: {}".format(e))
        else:
            _render_status_update(m, True, current_person)


def _render_status_update(m, is_mgr, operator):
    """渲染"更新状态"操作区。经理可验证关闭; 工程师只能标记完成。"""
    mid = int(m["id"])
    stt = str(m.get("status") or "")
    nexts = {
        "待执行": ["执行中"],
        "执行中": ["已完成"],
        "已完成": ["已关闭", "执行中"] if is_mgr else [],
        "已关闭": ["执行中"] if is_mgr else [],
    }.get(stt, [])
    if not nexts:
        return
    col_target, col_comment, col_btn = st.columns([2, 4, 1.4])
    with col_target:
        target = st.selectbox("目标状态", nexts, key="pcs_tgt_{}".format(mid))
    with col_comment:
        comment = st.text_input("备注", key="pcs_cmt_{}".format(mid),
                                placeholder="完成说明/验证结论/退回原因")
    with col_btn:
        st.write("")
        if st.button("更新", key="pcs_upd_{}".format(mid), use_container_width=True):
            if target in ("已完成", "已关闭") and not str(comment or "").strip():
                st.error("完成/验证关闭必须填写备注")
            elif (stt in ("已完成", "已关闭") and target == "执行中"
                    and not str(comment or "").strip()):
                st.error("退回/重开必须填写原因")
            else:
                try:
                    update_measure_status_pcs(
                        mid, target, operator, comment=str(comment or "").strip())
                    st.session_state["flash_success"] = "✅ 措施 #{} 已更新为「{}」".format(mid, target)
                    st.rerun()
                except Exception as e:
                    st.error("更新失败: {}".format(e))


# ============================================================
# 标签页1.5「批量导入」: 监造问题清单双 Sheet 导入 (v2.3)
# 支持 Sheet「拒收统计」和「过程核查统计」; 问题来源固定"监造提出", 责任人留空。
# ============================================================
def page_import():
    """页面「批量导入」(v2.7: manager / engineer 可访问)"""
    st.subheader("📥 批量导入（监造问题清单）")
    st.caption("支持 Excel 两个 Sheet: 「拒收统计」和「过程核查统计」。"
               "问题来源固定为「监造提出」, 责任人留空(导入后进入「待分配问题」)。")

    # 两个 Sheet 的字段映射(严格按真实表头; measure 列按换行拆分生成多条措施)
    _SHEET_CONFIGS = [
        {
            "name": "拒收统计",
            "date": ["时间"],
            "desc": ["拒收描述"],          # 问题描述列(非"描述")
            "ws": ["工序"],
            "part": ["零件"],
            "model": ["型号"],
            "level": ["类型ABC"],          # 问题等级
            "qty": ["数量"],
            "reason": ["原因分析"],
            "measure": ["整改措施"],        # 措施列(非"措施")
            "supervisor": ["监造师"],
        },
        {
            "name": "过程核查统计",
            "date": ["时间"],
            "desc": ["发现问题描述"],       # 问题描述列(非"描述")
            "ws": ["工序"],
            "part": ["零件"],
            "model": ["型号"],
            "level": [],   # 该表无"类型ABC"
            "qty": [],     # 该表无"数量"
            "reason": ["原因分析"],
            "measure": ["整改情况"],        # 措施列(非"措施")
            "supervisor": ["监造师"],
        },
    ]

    uploaded = st.file_uploader("选择 Excel 文件", type=["xlsx", "xls"], key="imp_file")

    if uploaded is not None:
        try:
            all_sheets = pd.read_excel(uploaded, sheet_name=None)  # 返回 {sheet名: DataFrame}
        except Exception as e:
            st.error("Excel 读取失败: {}（请确认文件格式为 .xlsx/.xls 且未加密）".format(e))
            all_sheets = None

        if all_sheets:
            # ★ 临时调试打印(不影响导入逻辑, 确认后删除):
            #   输出实际读取到的 Sheet 名称列表 + 每个 Sheet 的表头列名
            print("=" * 60)
            print("[批量导入调试] 实际读取到的 Sheet 名称列表:", list(all_sheets.keys()))
            for _sn, _sdf in all_sheets.items():
                print("[批量导入调试] Sheet「{}」表头列名: {}".format(
                    _sn, list(_sdf.columns)))
            print("=" * 60)

            st.markdown("**文件包含的 Sheet**: {}".format("、".join(all_sheets.keys())))

            # 匹配 Sheet(精确优先, 否则模糊匹配; 兼容 Sheet 名前后空格)
            sheet_map = {str(sn).strip(): sn for sn in all_sheets.keys()}
            importable = []  # [(sheet_df, sheet_cfg, sheet名)]
            for cfg in _SHEET_CONFIGS:
                target = cfg["name"]
                matched_name = None
                if target in sheet_map:
                    matched_name = sheet_map[target]
                else:
                    for key, sn in sheet_map.items():
                        if target in key or key in target:
                            matched_name = sn
                            break
                if matched_name is not None:
                    importable.append((all_sheets[matched_name], cfg, matched_name))
                else:
                    st.warning("未找到 Sheet「{}」, 跳过该表".format(target))

            if not importable:
                st.error("未找到可导入的 Sheet(需包含「拒收统计」或「过程核查统计」)。"
                         "当前 Sheet: {}".format("、".join(all_sheets.keys())))
            else:
                # 预览每个可导入 Sheet 的前 5 行
                for sdf, cfg, sname in importable:
                    st.markdown("**Sheet「{}」**: {} 行".format(sname, len(sdf)))
                    st.dataframe(sdf.head(5), use_container_width=True, hide_index=True)

                if st.button("✅ 确认导入", key="imp_submit", type="primary"):
                    ok_count = 0
                    skip_rows = []

                    for sdf, cfg, sname in importable:
                        # 列映射
                        col_date = _find_excel_col(sdf, cfg["date"])
                        col_desc = _find_excel_col(sdf, cfg["desc"])
                        col_ws = _find_excel_col(sdf, cfg["ws"])
                        col_part = _find_excel_col(sdf, cfg["part"])
                        col_model = _find_excel_col(sdf, cfg["model"])
                        col_level = _find_excel_col(sdf, cfg["level"])
                        col_qty = _find_excel_col(sdf, cfg["qty"])
                        col_reason = _find_excel_col(sdf, cfg["reason"])
                        col_measure = _find_excel_col(sdf, cfg["measure"])
                        col_supervisor = _find_excel_col(sdf, cfg["supervisor"])

                        if col_desc is None or col_measure is None:
                            skip_rows.append(("Sheet「{}」".format(sname), "缺少必要列(描述/措施)"))
                            continue

                        for idx, row in sdf.iterrows():
                            pos = "Sheet「{}」第{}行".format(sname, idx + 2)

                            def _cell(v_col):
                                if v_col and row[v_col] is not None and not (isinstance(row[v_col], float) and pd.isna(row[v_col])):
                                    return str(row[v_col]).strip()
                                return ""

                            desc = _cell(col_desc)
                            if not desc:
                                skip_rows.append((pos, "描述为空"))
                                continue

                            # ★ 措施按换行拆分 + 去编号 + 清洗空白
                            measures = split_measures_from_cell(row[col_measure])
                            if not measures:
                                skip_rows.append((pos, "措施为空"))
                                continue

                            issue_date = _normalize_date_value(row[col_date]) if col_date else ""
                            ws = _cell(col_ws)
                            part = _cell(col_part)
                            model = _cell(col_model)
                            level = _cell(col_level)
                            reason = _cell(col_reason)
                            supervisor = _cell(col_supervisor)
                            qty = _cell(col_qty)
                            try:
                                defect_qty = int(float(qty)) if qty else None
                            except (TypeError, ValueError):
                                defect_qty = None

                            try:
                                # v3.1 QCS: 措施默认"待评审", 部门/责任人/时间留空,
                                # 待经理评审采纳后再分配
                                save_issue_with_measures_pcs(
                                    problem_description=desc,
                                    issue_date=issue_date,
                                    work_station=ws,
                                    measures=measures,
                                    issue_source="监造提出",    # 固定来源
                                    part_no=part,
                                    model=model,
                                    problem_level=level,
                                    defect_quantity=defect_qty,
                                    reason_analysis=reason,
                                    supervisor_name=supervisor,
                                )
                                ok_count += 1
                            except Exception as e:
                                skip_rows.append((pos, "写入失败: {}".format(e)))

                    # 结果写入 session_state, rerun 后顶部展示
                    summary = "批量导入完成: 成功 {} 条问题".format(ok_count)
                    if skip_rows:
                        summary += ", 跳过 {} 条(见下方明细)".format(len(skip_rows))
                        st.session_state["import_skips"] = skip_rows
                    st.session_state["flash_success"] = summary
                    st.session_state.pop("imp_file", None)  # 清空上传文件, 防重复导入
                    st.rerun()


# ============================================================
# 标签页1.6「待分配问题」: 责任人全空的问题 + 分配责任人 (v2.3)
# ============================================================
def page_unassigned():
    """页面「待分配问题」(v2.7: 仅经理层可访问)"""
    st.subheader("📌 待分配问题")
    st.caption("展示所有第一/二/三责任人均为空的问题, 可在此为每条问题分配责任人。")

    df_all = load_all_issues()
    if df_all.empty:
        st.info("暂无问题数据")
    else:
        # 筛选三个责任人都为空的问题(待分配)
        def _is_blank(series):
            return series.fillna("").astype(str).str.strip() == ""

        unassigned = df_all[
            _is_blank(df_all["responsible_person_1"])
            & _is_blank(df_all["responsible_person_2"])
            & _is_blank(df_all["responsible_person_3"])
        ]

        if unassigned.empty:
            st.success("🎉 当前没有待分配的问题。")
        else:
            st.caption("共 {} 个待分配问题。分配责任人后进入正常评审流程(状态仍为未评审)。".format(len(unassigned)))
            for _, row in unassigned.iterrows():
                issue_id = int(row["id"])
                issue_no = row.get("序号")
                with st.expander(
                        "问题 #{} | {} | 零件:{} | 型号:{} | {}…".format(
                            issue_no, row.get("work_station") or "未填工序",
                            row.get("part_no") or "-", row.get("model") or "-",
                            str(row.get("problem_description") or "")[:16]),
                        expanded=(len(unassigned) == 1)):
                    # 展示字段(问题描述/工序/零件/型号/问题来源/创建时间)
                    st.markdown("**问题描述**: {}".format(row.get("problem_description") or "(无)"))
                    st.caption("工序: {} | 零件: {} | 型号: {} | 问题来源: {} | 创建时间: {}".format(
                        row.get("work_station") or "未填",
                        row.get("part_no") or "未填",
                        row.get("model") or "未填",
                        row.get("issue_source") or "未填",
                        row.get("created_at") or "未填"))

                    # 分配责任人(v2.4: 手动文本输入)
                    st.markdown("**分配责任人**")
                    c1, c2, c3 = st.columns(3)
                    assign_p1 = c1.text_input("第一责任人", key="asg_p1_{}".format(issue_id))
                    assign_p2 = c2.text_input("第二责任人", key="asg_p2_{}".format(issue_id))
                    assign_p3 = c3.text_input("第三责任人", key="asg_p3_{}".format(issue_id))
                    if st.button("✅ 确认分配", key="asg_btn_{}".format(issue_id)):
                        if not str(assign_p1 or "").strip() or not str(assign_p3 or "").strip():
                            st.error("第一/第三责任人必填")
                        else:
                            try:
                                assign_issue_persons(
                                    issue_id,
                                    str(assign_p1).strip(),
                                    str(assign_p2 or "").strip(),
                                    str(assign_p3).strip())
                                st.session_state["flash_success"] = "✅ 问题 #{} 责任人已分配".format(issue_no)
                                st.rerun()
                            except Exception as e:
                                st.error("分配失败: {}".format(e))


# ============================================================
# 标签页2「问题评审」: 第三责任人逐条措施评审(采纳/不采纳) (v2.0)
# ============================================================
def page_judge():
    """页面「问题评审」(v2.7: 仅经理层可见, 负责采纳/不采纳决策)"""
    st.subheader("⚖️ 问题评审（措施级）")

    if not current_person:
        st.warning("请先在左侧选择当前操作人")
    else:
        # 待评审措施: 该人担任第三责任人的问题下, is_adopted=NULL(待评审)的措施
        person_measures = get_measures_for_person(current_person)
        if person_measures.empty:
            st.info("当前没有待「{}」评审的措施。".format(current_person))
        else:
            def _is_pending(v):
                # v 是 is_adopted 列的单个标量值(None/0/1/1.0), 不是 Series,
                # 因此不能调用 .get(); None 表示"待评审", 应返回 True 正常显示。
                return v is None or (isinstance(v, float) and pd.isna(v))
            pending = person_measures[
                person_measures["responsible_person_3"].fillna("").astype(str).str.strip()
                .eq(current_person.strip())
                & person_measures["is_adopted"].apply(_is_pending)
            ]

            if pending.empty:
                st.info("当前没有待「{}」评审的措施。".format(current_person))
            else:
                # 按问题分组: 只显示问题作为唯一入口, 展开后才看到该问题下的措施
                # ★ 序号统一: 先按统一"序号"排序, groupby 保持顺序(sort=False)
                pending_sorted = pending.sort_values(
                    ["序号", "measure_order", "id"], na_position="last")
                issue_count = pending_sorted["issue_id"].nunique()
                if _can_decide_adoption():
                    st.caption("待评审 **{}** 条措施, 分布在 **{}** 个问题下 —— "
                               "点击问题展开, 再逐条措施采纳/不采纳。".format(
                                   len(pending_sorted), issue_count))
                else:
                    st.caption("待评审 **{}** 条措施, 分布在 **{}** 个问题下 —— "
                               "点击问题展开查看。当前角色为**只读**, "
                               "采纳/不采纳由管理员或主管操作。".format(
                                   len(pending_sorted), issue_count))

                for issue_id, grp in pending_sorted.groupby("issue_id", sort=False):
                    first = grp.iloc[0]
                    issue_no = first.get("序号")
                    with st.expander(
                            "问题 #{} | {} | {} | {}… ({} 条措施)".format(
                                issue_no, first.get("issue_source") or "未填来源",
                                first.get("work_station") or "未填工位",
                                str(first.get("problem_description") or "")[:16],
                                len(grp)),
                            expanded=(issue_count == 1)):
                        st.markdown("**问题描述**: {}".format(
                            first.get("problem_description") or "(未填写)"))
                        st.caption("来源: {} | 发现日期: {} | 第一/二/三责任人: {} / {} / {}".format(
                            first.get("issue_source") or "未填",
                            first.get("issue_date") or "未填",
                            first.get("responsible_person_1") or "未填",
                            first.get("responsible_person_2") or "未填",
                            first.get("responsible_person_3") or "未填"))

                        st.markdown("---")
                        # 该问题下的每条措施: 内容 + 采纳/不采纳按钮
                        for _, m in grp.iterrows():
                            measure_id = int(m["id"])
                            order_no = m.get("measure_order")
                            order_text = ""
                            if order_no is not None and not (isinstance(order_no, float) and pd.isna(order_no)):
                                order_text = "{}. ".format(int(order_no))
                            st.markdown("**{}**{}".format(
                                order_text, m.get("measure_content") or "(无内容)"))

                            if not _can_decide_adoption():
                                # employee 只读: 不渲染采纳/不采纳按钮(界面层)。
                                # 后端 /api/measure_decision 另有角色校验兜底, 防绕过。
                                st.caption("🔒 只读: 采纳/不采纳由管理员或主管操作")
                            else:
                                col_adopt, col_reject = st.columns(2)
                                btn_adopt = col_adopt.button(
                                    "✅ 采纳", key="judge_adopt_{}".format(measure_id),
                                    type="primary", use_container_width=True,
                                    help="该措施纳入改善跟踪, 进入分层审核流程")
                                btn_reject = col_reject.button(
                                    "🚫 不采纳", key="judge_reject_{}".format(measure_id),
                                    use_container_width=True,
                                    help="该措施暂不整改, 搁置归档(以后可重新采纳)")
                                if btn_adopt or btn_reject:
                                    try:
                                        set_measure_adoption(
                                            measure_id, 1 if btn_adopt else 0, current_person)
                                        st.session_state["flash_success"] = (
                                            "✅ 措施 #{} 已{}".format(
                                                measure_id, "采纳(进入分层审核)" if btn_adopt else "不采纳(搁置)"))
                                        st.rerun()
                                    except Exception as e:
                                        st.error("评审提交失败: {}".format(e))


# ============================================================
# 标签页3「分层审核」: 措施级三层审核 (v2.0)
# ============================================================
def page_review():
    """页面「分层审核」(v2.7: manager/engineer/employee 可见; 数据按登录人责任位过滤)"""
    if not current_person:
        st.warning("请先在左侧选择当前操作人")
    else:
        st.subheader("✅ 分层审核（措施级, 当前操作人: {}）".format(current_person))
        st.caption("只显示已采纳进入审核流程的措施, 按「整改/验收/最终确认」三组列出。")

        person_measures = get_measures_for_person(current_person)
        reviews_df = load_all_reviews()

        if person_measures.empty:
            st.info("暂无与您相关的措施, 无法审核。")
        else:
            # 审核流水按措施分组(内存分组)
            reviews_by_measure = {}
            if not reviews_df.empty and "measure_id" in reviews_df.columns:
                for _, row in reviews_df.iterrows():
                    mid = row.get("measure_id")
                    if mid is None or (isinstance(mid, float) and pd.isna(mid)):
                        continue
                    reviews_by_measure.setdefault(int(mid), []).append(row.to_dict())

            # 按当前层级+角色归入三个待办分组
            groups = {1: [], 2: [], 3: []}
            for _, m in person_measures.iterrows():
                is_adopted = m.get("is_adopted")
                if not (is_adopted is not None and not (isinstance(is_adopted, float) and pd.isna(is_adopted))
                        and int(is_adopted) == 1):
                    continue  # 未采纳/不采纳的措施不进分层审核
                measure_id = int(m["id"])
                measure_status = m.get("status") or ""
                records = reviews_by_measure.get(measure_id, [])
                # v2.5: skip_level2 固化在措施上(采纳时写入); NULL(极旧数据)按 p2 兜底
                skip2_raw = m.get("skip_level2")
                skip2 = (str(m.get("responsible_person_2") or "").strip() == "") \
                    if skip2_raw is None else bool(int(skip2_raw))
                level = compute_measure_level(records, measure_status, skip2)
                if level is None:
                    continue
                p1 = str(m.get("responsible_person_1") or "").strip()
                p2 = str(m.get("responsible_person_2") or "").strip()
                p3 = str(m.get("responsible_person_3") or "").strip()
                cp = current_person.strip()
                if level == 1 and p1 == cp and measure_status in ("已评审", "已退回"):
                    groups[1].append((m, records, level))
                elif level == 2 and p2 == cp:
                    groups[2].append((m, records, level))
                elif level == 3 and p3 == cp:
                    groups[3].append((m, records, level))

            GROUP_TITLES = {
                1: "🛠️ 需要我整改的措施（我是第一责任人）",
                2: "🔍 需要我验收的措施（我是第二责任人）",
                3: "🏁 需要我最终确认的措施（我是第三责任人）",
            }
            total = len(groups[1]) + len(groups[2]) + len(groups[3])
            if total == 0:
                st.success("🎉 当前没有需要「{}」处理的措施。".format(current_person))
            else:
                for level in (1, 2, 3):
                    pending = groups[level]
                    st.markdown("---")
                    st.markdown("**{}（{} 条）**".format(GROUP_TITLES[level], len(pending)))
                    if not pending:
                        st.caption("无")
                        continue

                    for m, records, cur_level in pending:
                        measure_id = int(m["id"])
                        issue_id = int(m["issue_id"])  # 保留用于下方 recompute_issue_status
                        with st.expander(
                                "问题#{} | {} | 措施: {}…".format(
                                    m.get("序号"), m.get("work_station") or "未填工位",
                                    str(m.get("measure_content") or "")[:24]),
                                expanded=(total == 1)):
                            # 问题信息 + 措施信息
                            st.markdown("**问题描述**: {}".format(m.get("problem_description") or "(未填写)"))
                            st.markdown("**改善措施**: {}".format(m.get("measure_content") or "(未填写)"))
                            st.caption("来源: {} | 发现日期: {} | 措施状态: {} | 第一/二/三责任人: {} / {} / {}".format(
                                m.get("issue_source") or "未填",
                                m.get("issue_date") or "未填", m.get("status") or "未知",
                                m.get("responsible_person_1") or "未填",
                                m.get("responsible_person_2") or "未填",
                                m.get("responsible_person_3") or "未填"))

                            # v2.5: 第二责任人未分配时, 历史区提示"已跳过该层"
                            if not str(m.get("responsible_person_2") or "").strip():
                                st.caption("ℹ️ 第二责任人未分配（已跳过该层）")

                            # 历史审核记录
                            if records:
                                st.markdown("**历史审核记录**")
                                hist = pd.DataFrame(records).sort_values("id", ascending=False)
                                hist = hist.rename(columns={
                                    "review_level": "审核层级", "reviewer": "审核人",
                                    "review_result": "结果", "review_comment": "备注",
                                    "reviewed_at": "审核时间"})
                                if "审核层级" in hist.columns:
                                    hist["审核层级"] = hist["审核层级"].map(
                                        lambda lv: REVIEW_LEVELS.get(lv, str(lv)))
                                show_cols = [c for c in ["审核层级", "审核人", "结果", "备注", "审核时间"]
                                             if c in hist.columns]
                                st.dataframe(hist[show_cols], hide_index=True, use_container_width=True)
                            else:
                                st.caption("暂无审核记录 —— 该措施还没有人处理过。")

                            # 审核操作区
                            st.markdown("---")
                            st.markdown("**审核操作** (您正以 第{}层·{} 身份操作)".format(
                                level, REVIEW_LEVELS.get(level, "")))
                            photo = st.file_uploader(
                                "整改后现场照片(点✅完全符合时必传)",
                                type=["jpg", "jpeg", "png", "webp", "heic"],
                                key="photo_{}".format(measure_id))
                            comment = st.text_area(
                                "审核备注 * (完全符合=整改说明 / 完全不符合=不合格原因 / 部分符合=具体说明)",
                                key="comment_{}".format(measure_id), height=80)

                            col_pass, col_fail, col_partial = st.columns(3)
                            btn_pass = col_pass.button(
                                "✅ 完全符合", key="pass_{}".format(measure_id),
                                use_container_width=True, type="primary")
                            btn_fail = col_fail.button(
                                "❌ 完全不符合", key="fail_{}".format(measure_id),
                                use_container_width=True)
                            btn_partial = col_partial.button(
                                "🔶 部分符合", key="partial_{}".format(measure_id),
                                use_container_width=True)

                            # v2.8: 第三责任人"中途补充第二责任人"
                            # 仅对"因第二责任人留空而跳过该层"且未关闭的措施显示
                            if (level == 3
                                    and int(m.get("skip_level2") or 0) == 1
                                    and str(m.get("status") or "") != "已关闭"):
                                st.markdown("---")
                                st.markdown("**补充第二责任人**（技术性较强时转交工程师验收）")
                                st.caption(
                                    "转交后本条措施交回第二责任人验收, 第一责任人的整改结果与照片**保留**; "
                                    "该问题下所有跳过第二责任人的未关闭措施一并改为完整三层流程。")
                                _ap_col1, _ap_col2 = st.columns([3, 1])
                                with _ap_col1:
                                    _new_p2 = st.selectbox(
                                        "选择第二责任人", options=_person_name_options(),
                                        key="assign_p2_{}".format(measure_id))
                                with _ap_col2:
                                    _do_assign = st.button(
                                        "➡️ 转交", key="assign_btn_{}".format(measure_id),
                                        use_container_width=True)
                                if _do_assign:
                                    _p2_name = str(_new_p2 or "").strip()
                                    if not _p2_name:
                                        st.error("请先选择第二责任人")
                                    else:
                                        try:
                                            _cnt = assign_measure_level2(
                                                measure_id, _p2_name, current_person)
                                            st.session_state["flash_success"] = (
                                                "✅ 已转交第二责任人「{}」, "
                                                "该问题下 {} 条措施改为完整三层流程".format(_p2_name, _cnt))
                                            st.rerun()
                                        except Exception as e:
                                            st.error("转交失败: {}".format(e))

                            action = None
                            if btn_pass:
                                action = "完全符合"
                            elif btn_fail:
                                action = "完全不符合"
                            elif btn_partial:
                                action = "部分符合"

                            if action:
                                if action == "完全符合" and photo is None:
                                    st.error("点✅完全符合必须上传整改后现场照片, 请先选择照片再提交。")
                                elif not str(comment or "").strip():
                                    st.error("审核备注必填: 完全符合=整改说明 / 完全不符合=不合格原因 / 部分符合=具体说明。")
                                else:
                                    try:
                                        photo_path = None
                                        if action == "完全符合":
                                            photo_path = _compress_and_save_photo(photo)
                                        add_measure_review_record(
                                            measure_id, level, current_person,
                                            action, str(comment).strip(), photo_path)
                                        if action == "完全符合":
                                            new_status = "已关闭" if level == 3 else "已评审"
                                            next_hint = ("措施已关闭, 闭环!" if level == 3
                                                         else "措施推进到第{}层".format(level + 1))
                                        elif action == "完全不符合":
                                            new_status = "已退回"
                                            next_hint = "措施已退回第一责任人重新整改"
                                        else:
                                            new_status = "已评审"
                                            next_hint = "措施继续停留在第{}层跟踪".format(level)
                                        update_measure_status(measure_id, new_status)
                                        recompute_issue_status(issue_id)
                                        try:
                                            _notify_measure_action(m, m.get("measure_content"),
                                                                    level, action, current_person)
                                        except Exception as e:
                                            print("通知发送异常(不影响审核结果):", e)
                                        st.session_state["flash_success"] = (
                                            "✅ 措施 #{} 已提交「{}」, {}".format(
                                                measure_id, action, next_hint))
                                        st.rerun()
                                    except Exception as e:
                                        st.error("提交失败: {}".format(e))


# ============================================================
# 标签页4「不采纳列表」: is_adopted=0 的措施(支持重新采纳) (v2.0)
# ============================================================
def page_rejected():
    """页面「不采纳列表」(v2.7: manager/engineer 可访问; 重新采纳仅 manager)"""
    st.subheader("🚫 不采纳列表（措施级）")

    if not current_person:
        st.warning("请先在左侧选择当前操作人")
    else:
        person_measures = get_measures_for_person(current_person)
        if person_measures.empty:
            st.info("「{}」名下没有不采纳的措施。".format(current_person))
        else:
            def _is_rejected(v):
                try:
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        return False
                    return int(v) == 0
                except (TypeError, ValueError):
                    return False
            rejected = person_measures[person_measures["is_adopted"].apply(_is_rejected)]

            if rejected.empty:
                st.info("「{}」名下没有不采纳的措施。".format(current_person))
            else:
                # 概览表(问题编号列使用统一"序号", 不显示数据库id)
                display = pd.DataFrame({
                    "问题序号": rejected["序号"].tolist(),
                    "问题描述": rejected["problem_description"].tolist(),
                    "措施内容": rejected["measure_content"].tolist(),
                    "工位": rejected["work_station"].tolist(),
                    "第三责任人": rejected["responsible_person_3"].tolist(),
                })
                st.dataframe(display, use_container_width=True, hide_index=True)
                _can_readopt = _current_role() == auth_core.ROLE_MANAGER
                st.caption("共 {} 条不采纳措施。{}".format(
                    len(rejected),
                    "经理层可在下方对自己名下(第三责任人)的措施执行「重新采纳」" if _can_readopt
                    else "重新采纳需经理层操作"))

                # 重新采纳(v2.7): 仅经理层, 且当前操作人是该措施的第三责任人
                if _can_readopt:
                    mine = rejected[rejected["responsible_person_3"].fillna("").astype(str)
                                    .str.strip().eq(current_person.strip())]
                    if not mine.empty:
                        st.markdown("---")
                        st.markdown("**可重新采纳的措施（{} 条, 我是第三责任人）**".format(len(mine)))
                        for _, m in mine.iterrows():
                            measure_id = int(m["id"])
                            col_info, col_btn = st.columns([4, 1])
                            col_info.markdown("问题#{} | {} | 措施: {}…".format(
                                m.get("序号"), m.get("work_station") or "未填工位",
                                str(m.get("measure_content") or "")[:24]))
                            if col_btn.button("🔁 重新采纳", key="readopt_{}".format(measure_id),
                                              use_container_width=True,
                                              help="重新采纳: 该措施重回分层审核, 从第一责任人开始"):
                                try:
                                    set_measure_adoption(measure_id, 1, current_person)
                                    st.session_state["flash_success"] = (
                                        "✅ 措施 #{} 重新采纳, 进入分层审核(从第一责任人开始)".format(measure_id))
                                    st.rerun()
                                except Exception as e:
                                    st.error("重新采纳失败: {}".format(e))


# ============================================================
# 标签页5「个人行动计划」: 措施粒度(v2.0)
# ============================================================
def page_plan():
    """页面「个人行动计划」(v2.7: manager/engineer/employee 可见; 仅显示登录人相关措施)"""
    st.subheader("🎯 个人行动计划（措施级）")

    if not current_person:
        st.warning("请先在左侧选择当前操作人")
    else:
        person_measures = get_measures_for_person(current_person)
        if person_measures.empty:
            st.info("「{}」目前没有关联措施。".format(current_person))
        else:
            st.caption("「{}」名下关联的措施, 按责任角色分组, 每个问题展开查看其下措施:".format(current_person))

            reviews_df = load_all_reviews()
            reviews_by_measure = {}
            if not reviews_df.empty and "measure_id" in reviews_df.columns:
                for _, row in reviews_df.iterrows():
                    mid = row.get("measure_id")
                    if mid is None or (isinstance(mid, float) and pd.isna(mid)):
                        continue
                    reviews_by_measure.setdefault(int(mid), []).append(row.to_dict())

            def _latest_layer_result(records, lv):
                """该层最近一次分层审核结果(完全符合/完全不符合/部分符合); 无则返回空"""
                layer = [r for r in records
                         if r.get("review_level") == lv
                         and r.get("review_result") in ("完全符合", "完全不符合", "部分符合")]
                if not layer:
                    return ""
                layer.sort(key=lambda r: r.get("id", 0) or 0)
                return layer[-1].get("review_result") or ""

            def _completion_text(is_adopted, measure_status, records, lv, skip2=False):
                """完成情况列(与图6同规则):
                   - is_adopted=0 → 不采纳
                   - is_adopted=NULL → 待评审
                   - status='已关闭' → 已关闭
                   - 已采纳且未关闭: 该人员是当前处理层 → 该层审核结果; 否则 → 进行中"""
                if is_adopted == 0:
                    return "不采纳"
                if is_adopted is None or (isinstance(is_adopted, float) and pd.isna(is_adopted)):
                    return "待评审"
                if measure_status == "已关闭":
                    return "已关闭"
                cur_level = compute_measure_level(records, measure_status, skip2)
                if cur_level == lv:
                    res = _latest_layer_result(records, lv)
                    return res if res else "待处理"
                return "进行中"

            _GROUPS = [
                ("作为第一责任人的措施", "responsible_person_1", 1),
                ("作为第二责任人的措施", "responsible_person_2", 2),
                ("作为第三责任人的措施", "responsible_person_3", 3),
            ]
            for group_title, col_name, lv_no in _GROUPS:
                col_vals = person_measures[col_name].fillna("").astype(str).str.strip()
                group_df = person_measures[col_vals == current_person.strip()]
                st.markdown("---")
                st.markdown("**{}（{} 条措施）**".format(group_title, len(group_df)))
                if group_df.empty:
                    st.caption("无")
                else:
                    # 按问题分组, 每个问题一个 expander(问题描述+来源只显示一次)
                    for issue_id, grp in group_df.groupby("issue_id", sort=False):
                        first = grp.iloc[0]
                        desc = str(first.get("problem_description") or "(无描述)")
                        if len(desc) > 40:
                            desc = desc[:40] + "…"
                        title = "问题：{} ｜ 来源：{} ｜ {} 条措施".format(
                            desc, first.get("issue_source") or "未填", len(grp))
                        with st.expander(title):
                            rows = []
                            for _, m in grp.iterrows():
                                mid = int(m["id"])
                                measure_status = m.get("status") or ""
                                is_adopted = m.get("is_adopted")
                                records = reviews_by_measure.get(mid, [])
                                skip2_raw = m.get("skip_level2")
                                skip2 = (str(m.get("responsible_person_2") or "").strip() == "") \
                                    if skip2_raw is None else bool(int(skip2_raw))
                                rows.append({
                                    "措施内容": m.get("measure_content") or "",
                                    "当前措施状态": measure_status or "未知",
                                    "完成情况": _completion_text(is_adopted, measure_status, records, lv_no, skip2),
                                })
                            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ============================================================
# 标签页6「分析看板」: 措施级 + 问题来源维度统计 (v2.1, 图1-图5)
# ============================================================
def page_dashboard():
    """页面「分析看板」(v3.1 QCS): 厂长管理驾驶舱, 仅 admin 可见。

    核心回答: 哪个部门任务重 / 谁在拖 / 哪些措施逾期 / 处理快不快。
    本次初步实现: 顶部 KPI 卡片 + 图1 措施状态分布 + 图2 部门堆叠 + 图4 逾期分析。
    数据查询已封装在 db.load_dashboard_data / db.dashboard_kpis, 此处只做渲染。
    """
    st.subheader("📊 分析看板")

    # ---- 数据源(db.py 统一封装, 含逾期计算列 + 问题描述) ----
    issues, measures = load_dashboard_data()
    if measures.empty:
        st.info("暂无措施数据。")
        return

    # ---- 顶部关键指标卡片(管理驾驶舱) ----
    kpis = dashboard_kpis(issues, measures)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📋 未关闭问题", kpis["open_issues"])
    c2.metric("🔧 已采纳措施", kpis["adopted_measures"])
    c3.metric("🔴 逾期措施", kpis["overdue_measures"])
    c4.metric("🟡 待评审措施", kpis["pending_review"])
    st.divider()

    # ============================================================
    # 图1 · 措施状态分布(环形图): 完整 7 态, 每态拆"未逾期/逾期"深浅色
    # ============================================================
    st.markdown("### 图1 · 措施状态分布")
    st.caption("完整 7 态; 每状态按逾期拆分(深色=逾期, 仅待执行/执行中可能逾期)。")

    # 每状态: (未逾期基础色, 逾期深色)
    status_colors = {
        auth_core.MEASURE_STATUS_REVIEW: ("#f1c40f", "#9a7d0a"),      # 待评审 黄
        auth_core.MEASURE_STATUS_ADOPTED: ("#74c0fc", "#2b6ca3"),     # 已采纳 蓝
        auth_core.MEASURE_STATUS_REJECTED: ("#adb5bd", "#6c757d"),    # 不采纳 灰
        auth_core.MEASURE_STATUS_PENDING: ("#4dabf7", "#a61e4d"),     # 待执行 蓝
        auth_core.MEASURE_STATUS_DOING: ("#ffa94d", "#a61e4d"),       # 执行中 橙
        auth_core.MEASURE_STATUS_DONE: ("#69db7c", "#2b8a3e"),        # 已完成 绿
        auth_core.MEASURE_STATUS_CLOSED: ("#868e96", "#495057"),      # 已关闭 灰
    }
    labels, values, colors = [], [], []
    for s in auth_core.MEASURE_STATUSES:
        sub = measures[measures["status"].fillna("").astype(str) == s]
        if sub.empty:
            continue
        overdue_n = int(sub["is_overdue"].fillna(False).astype(bool).sum())
        ontime_n = len(sub) - overdue_n
        light, dark = status_colors.get(s, ("#adb5bd", "#6c757d"))
        if ontime_n:
            labels.append("{}(未逾期)".format(s))
            values.append(ontime_n)
            colors.append(light)
        if overdue_n:
            labels.append("{}(逾期)".format(s))
            values.append(overdue_n)
            colors.append(dark)

    if values:
        fig1 = go.Figure(data=[go.Pie(
            labels=labels, values=values, hole=0.4,
            marker=dict(colors=colors), textinfo="label+value+percent")])
        fig1.update_layout(margin=dict(t=20, b=20), height=380,
                           legend=dict(orientation="h", yanchor="bottom", y=-0.15))
        st.plotly_chart(fig1, use_container_width=True, config=_PLOTLY_CONFIG)
    else:
        st.info("暂无措施状态数据")

    # ============================================================
    # 图2 · 部门维度对比(堆叠柱): X=P/Q/T, 5 段互斥堆叠, 柱顶标总数+逾期
    # ============================================================
    st.markdown("### 图2 · 部门维度对比(按状态堆叠)")
    st.caption("每部门一根柱; 5 段互斥: 逾期 / 待执行(含已采纳) / 执行中 / 已完成 / 已关闭。")

    def _seg(row):
        """措施归入部门柱的互斥分段(避免逾期与待执行/执行中重复计数)。"""
        st_ = str(row.get("status") or "")
        if bool(row.get("is_overdue")):                        # 逾期(待执行/执行中且超期)
            return "逾期"
        if st_ in (auth_core.MEASURE_STATUS_ADOPTED, auth_core.MEASURE_STATUS_PENDING):
            return "待执行"
        if st_ == auth_core.MEASURE_STATUS_DOING:
            return "执行中"
        if st_ == auth_core.MEASURE_STATUS_DONE:
            return "已完成"
        if st_ == auth_core.MEASURE_STATUS_CLOSED:
            return "已关闭"
        return None                                            # 待评审/不采纳不进部门柱

    seg_order = ["逾期", "待执行", "执行中", "已完成", "已关闭"]
    seg_colors = {"逾期": "#e03131", "待执行": "#4dabf7", "执行中": "#ffa94d",
                  "已完成": "#69db7c", "已关闭": "#868e96"}

    dept_labels = [auth_core.DEPARTMENT_LABELS.get(d, d) for d in auth_core.DEPARTMENTS]
    dept_seg = {seg: [] for seg in seg_order}   # 每段在各部门的数量
    dept_total, dept_overdue = [], []           # 每部门总数 / 逾期数(柱顶标注)
    for d in auth_core.DEPARTMENTS:
        sub = measures[measures["department"].fillna("").astype(str) == d]
        seg_cnt = {seg: 0 for seg in seg_order}
        for _, r in sub.iterrows():
            sg = _seg(r)
            if sg:
                seg_cnt[sg] += 1
        for seg in seg_order:
            dept_seg[seg].append(seg_cnt[seg])
        dept_total.append(len(sub))
        dept_overdue.append(int(sub["is_overdue"].fillna(False).astype(bool).sum()))

    fig2 = go.Figure()
    for seg in seg_order:
        fig2.add_trace(go.Bar(
            name=seg, x=dept_labels, y=dept_seg[seg], marker_color=seg_colors[seg]))
    fig2.update_layout(
        barmode="stack", height=380, margin=dict(t=50, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.0),
        annotations=[
            dict(x=dept_labels[i], y=dept_total[i], showarrow=False,
                 text="共{} / 逾期{}".format(dept_total[i], dept_overdue[i]),
                 font=dict(size=11, color="#333"))
            for i in range(len(dept_labels))
        ])
    st.plotly_chart(fig2, use_container_width=True, config=_PLOTLY_CONFIG)

    # ============================================================
    # 图4 · 逾期分析(优先级最高): 柱状图(各部门逾期数) + 明细表(按逾期天数降序)
    # ============================================================
    st.markdown("### 图4 · 逾期分析")
    st.caption("上: 各部门逾期措施数; 下: 全部逾期措施明细, 按逾期天数从大到小。")

    overdue_df = measures[measures["is_overdue"].fillna(False).astype(bool)]
    if overdue_df.empty:
        st.info("当前无逾期措施 ✅")
    else:
        # 各部门逾期措施数柱状图
        od_x = [auth_core.DEPARTMENT_LABELS.get(d, d) for d in auth_core.DEPARTMENTS]
        od_y = [int((overdue_df["department"].fillna("").astype(str) == d).sum())
                for d in auth_core.DEPARTMENTS]
        fig4 = go.Figure(data=[go.Bar(
            x=od_x, y=od_y, marker_color="#e03131",
            text=od_y, textposition="outside")])
        fig4.update_layout(margin=dict(t=20, b=20), height=300,
                           yaxis=dict(title="逾期措施数"))
        st.plotly_chart(fig4, use_container_width=True, config=_PLOTLY_CONFIG)

        # 明细表: 问题描述/措施内容/部门/责任人/逾期天数/当前状态, 按逾期天数降序
        detail_rows = []
        for _, r in overdue_df.iterrows():
            dep_raw = str(r.get("department") or "").strip()
            detail_rows.append({
                "问题描述": str(r.get("problem_description") or "(无描述)"),
                "措施内容": str(r.get("measure_content") or ""),
                "所属部门": auth_core.DEPARTMENT_LABELS.get(dep_raw, dep_raw or "未分配"),
                "责任人": str(r.get("owner") or "未指定"),
                "逾期天数": int(r.get("overdue_days") or 0),
                "当前状态": str(r.get("status") or ""),
            })
        detail_rows.sort(key=lambda x: -x["逾期天数"])
        st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)

    # ============================================================
    # 图3 · 责任人TOP分析: 按工程师统计 措施数/逾期数/按期完成率
    # ============================================================
    st.markdown("### 图3 · 责任人 TOP 分析")
    st.caption("按工程师(owner)统计名下措施数、逾期数、按期完成率(已完成/已关闭中未逾期占比)。")

    if "owner" not in measures.columns:
        st.info("暂无责任人数据")
    else:
        owner_agg = {}
        for _, r in measures.iterrows():
            o = str(r.get("owner") or "").strip() or "未指定"
            od = bool(r.get("is_overdue"))
            st_ = str(r.get("status") or "")
            cov = int(r.get("completed_overdue") or 0)
            d = owner_agg.setdefault(o, {"total": 0, "overdue": 0, "done": 0, "ontime": 0})
            d["total"] += 1
            if od:
                d["overdue"] += 1
            if st_ in ("已完成", "已关闭"):
                d["done"] += 1
                if cov == 0:
                    d["ontime"] += 1
        if not owner_agg:
            st.info("暂无责任人数据")
        else:
            top_rows = []
            for o, d in owner_agg.items():
                top_rows.append({
                    "责任人": o,
                    "措施总数": d["total"],
                    "逾期数": d["overdue"],
                    "按期完成率": "{:.0f}%".format(d["ontime"] / d["done"] * 100)
                    if d["done"] else "—",
                })
            top_rows.sort(key=lambda x: (-x["逾期数"], -x["措施总数"]))
            top = top_rows[:10]

            names = [t["责任人"] for t in top][::-1]
            fig3 = go.Figure()
            fig3.add_trace(go.Bar(
                name="措施总数", x=[t["措施总数"] for t in top][::-1],
                y=names, orientation="h", marker_color="#74c0fc"))
            fig3.add_trace(go.Bar(
                name="逾期数", x=[t["逾期数"] for t in top][::-1],
                y=names, orientation="h", marker_color="#e03131"))
            fig3.update_layout(barmode="group", height=360,
                               margin=dict(t=20, b=20, l=10),
                               legend=dict(orientation="h", yanchor="bottom", y=1.0))
            st.plotly_chart(fig3, use_container_width=True, config=_PLOTLY_CONFIG)
            st.dataframe(pd.DataFrame(top), use_container_width=True, hide_index=True)

    # ============================================================
    # 图5 · 问题关闭趋势: 每月新增/关闭 + 累计关闭率 + 整体按期关闭率
    # ============================================================
    st.markdown("### 图5 · 问题关闭趋势")
    st.caption("柱=每月新增/关闭问题数; 折线=累计关闭率; 下方标注整体按期关闭率(已关闭且无逾期措施)。")

    if issues.empty or "issue_date" not in issues.columns:
        st.info("暂无问题时间数据")
    else:
        iss = issues.copy()
        iss["_ym"] = issues["issue_date"].fillna("").astype(str).str[:7]
        iss["_close_ym"] = issues["updated_at"].fillna("").astype(str).str[:7]
        iss["_closed"] = issues["status"].fillna("").astype(str) == "已关闭"

        # 有逾期措施的问题 id 集合(用于"按期关闭"判断)
        od_ids = set()
        if "issue_id" in measures.columns:
            od_ids = set(measures.loc[
                measures["is_overdue"].fillna(False).astype(bool), "issue_id"]
                .astype(str).tolist())

        ym_list = sorted(set(iss["_ym"][iss["_ym"].str.match(r"^\d{4}-\d{2}$")]))
        if not ym_list:
            st.info("暂无带有效发现日期的问题")
        else:
            new_cnt, close_cnt = [], []
            cum_new = cum_closed = 0
            rate = []
            for ym in ym_list:
                m = iss[iss["_ym"] == ym]
                new_n = len(m)
                closed_n = int((m["_closed"] & (m["_close_ym"] == ym)).sum())
                cum_new += new_n
                cum_closed += closed_n
                new_cnt.append(new_n)
                close_cnt.append(closed_n)
                rate.append(round(cum_closed / cum_new * 100, 1) if cum_new else 0)

            fig5 = go.Figure()
            fig5.add_trace(go.Bar(name="新增问题", x=ym_list, y=new_cnt,
                                  marker_color="#4dabf7"))
            fig5.add_trace(go.Bar(name="关闭问题", x=ym_list, y=close_cnt,
                                  marker_color="#2f9e44"))
            fig5.add_trace(go.Scatter(
                name="累计关闭率(%)", x=ym_list, y=rate, mode="lines+markers",
                yaxis="y2", line=dict(color="#e03131", width=2)))
            fig5.update_layout(
                barmode="group", height=400,
                yaxis=dict(title="问题数"),
                yaxis2=dict(title="关闭率(%)", overlaying="y", side="right",
                           range=[0, 100]),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig5, use_container_width=True, config=_PLOTLY_CONFIG)

            closed_issues = iss[iss["_closed"]]
            ontime_n = int((~closed_issues["id"].astype(str).isin(od_ids)).sum())
            overall = round(ontime_n / len(closed_issues) * 100, 1) if len(closed_issues) else 0
            st.caption("整体按期关闭率(已关闭且无逾期措施的问题占比): {}%".format(overall))

    # ============================================================
    # 图6 · 问题来源分布: 监造提出 vs 过程改善发现
    # ============================================================
    st.markdown("### 图6 · 问题来源分布")
    if issues.empty or "issue_source" not in issues.columns:
        st.info("暂无问题来源数据")
    else:
        src = issues["issue_source"].fillna("未填").astype(str).value_counts().to_dict()
        fig6 = go.Figure(data=[go.Pie(
            labels=list(src.keys()), values=list(src.values()), hole=0.4,
            textinfo="label+value+percent",
            marker=dict(colors=["#4dabf7", "#f1c40f", "#adb5bd"]))])
        fig6.update_layout(margin=dict(t=20, b=20), height=320)
        st.plotly_chart(fig6, use_container_width=True, config=_PLOTLY_CONFIG)

    # ============================================================
    # 图7 · 问题等级分布: A/B/C 数量, 结合逾期
    # ============================================================
    st.markdown("### 图7 · 问题等级分布")
    st.caption("按问题等级(A/B/C)统计问题数; 红柱=该等级下有逾期措施的问题数。")

    if issues.empty or "problem_level" not in issues.columns:
        st.info("暂无问题等级数据")
    else:
        def _lvl(x):
            s = str(x or "").strip()
            for L in ("A", "B", "C"):
                if s.startswith(L):
                    return L
            return "未分类"

        od_ids7 = set()
        if "issue_id" in measures.columns:
            od_ids7 = set(measures.loc[
                measures["is_overdue"].fillna(False).astype(bool), "issue_id"]
                .astype(str).tolist())

        lvl_order = ["A", "B", "C", "未分类"]
        total_cnt, od_cnt = [], []
        for L in lvl_order:
            sub = issues[issues["problem_level"].fillna("").apply(_lvl) == L]
            total_cnt.append(len(sub))
            od_cnt.append(int(sub["id"].astype(str).isin(od_ids7).sum()))
        fig7 = go.Figure(data=[
            go.Bar(name="问题总数", x=lvl_order, y=total_cnt, marker_color="#74c0fc"),
            go.Bar(name="含逾期措施", x=lvl_order, y=od_cnt, marker_color="#e03131"),
        ])
        fig7.update_layout(barmode="group", height=320, margin=dict(t=20, b=20),
                           legend=dict(orientation="h", yanchor="bottom", y=1.0))
        st.plotly_chart(fig7, use_container_width=True, config=_PLOTLY_CONFIG)

    # ============================================================
    # 图8 · 措施平均处理周期: 已采纳→已关闭 平均天数, 按部门
    # ============================================================
    st.markdown("### 图8 · 措施平均处理周期(按部门)")
    st.caption("口径: 处理周期 = 关闭时间 - 采纳时间(天数), 仅统计已采纳且已关闭的措施。")

    cycle = load_dashboard_cycle()
    if not cycle:
        st.info("暂无已关闭措施的处理周期数据(需有措施走完 采纳→关闭 全流程)")
    else:
        keys = list(auth_core.DEPARTMENTS)
        if "" in cycle:
            keys.append("")
        cyc_labels = [auth_core.DEPARTMENT_LABELS.get(k, k) if k else "未分配"
                      for k in keys]
        cyc_vals = [cycle[k]["avg_days"] for k in keys]
        cyc_cnts = [cycle[k]["cnt"] for k in keys]
        fig8 = go.Figure(data=[go.Bar(
            x=cyc_labels, y=cyc_vals, marker_color="#1f77b4",
            text=["{}天 / {}条".format(v, c) for v, c in zip(cyc_vals, cyc_cnts)],
            textposition="outside")])
        fig8.update_layout(margin=dict(t=20, b=20), height=320,
                           yaxis=dict(title="平均处理天数"))
        st.plotly_chart(fig8, use_container_width=True, config=_PLOTLY_CONFIG)

    return


# ============================================================
# 标签页9「账号管理」: 管理员维护账号 (v2.6, 仅 admin 可见)
# ============================================================

_MANUAL_NAME_FLAG = "➕ 手动输入新姓名"


def _person_name_options():
    """姓名下拉选项 = config 三层名单 ∪ issues 表中已出现的责任人姓名(去重排序)。

    取并集的原因:
      - issues 表是业务真相(个人任务按 responsible_person_N == display_name 匹配);
      - config 名单用于给"尚未录入过问题"的新同事预建账号(此时 issues 表查不到)。
    """
    names = set()
    try:
        names.update(str(x).strip() for x in get_all_person_names() if str(x).strip())
    except Exception:
        pass
    for lst in (LEVEL1_PERSONS, LEVEL2_PERSONS, LEVEL3_PERSONS):
        names.update(str(x).strip() for x in lst if str(x).strip())
    return sorted(names)


def _active_admin_count(accounts):
    """启用状态的管理员数量(用于"最后一个管理员"保护)"""
    return sum(1 for a in accounts
               if a.get("role") == auth_core.ROLE_ADMIN
               and str(a.get("status")) == "active")


def _role_label(r):
    return "{} ({})".format(auth_core.ROLE_LABELS.get(r, r), r)


def page_accounts():
    """页面「账号管理」(v2.7: 仅系统管理员可见)"""
    st.subheader("👥 账号管理")
    st.caption("创建账号、调整角色、重置密码、启用/停用。"
               "★ **姓名必须与问题中的责任人姓名完全一致**, 否则该用户登录后看不到自己的任务。")

    try:
        accounts = auth.list_accounts()
    except Exception as e:
        st.error("读取账号列表失败: {}".format(e))
        return

    _me = st.session_state.get("username")

    # ---------------- ① 创建新账号 ----------------
    st.markdown("#### ① 创建新账号")
    c_u, c_r, c_d = st.columns(3)
    with c_u:
        new_username = st.text_input(
            "账号 *", key="acct_new_user", placeholder="登录用账号, 如 zhangsan")
    with c_r:
        new_role = st.selectbox(
            "角色 *", options=auth_core.ROLES,
            format_func=_role_label, key="acct_new_role")
    with c_d:
        new_dept = st.selectbox(
            "部门", options=[""] + auth_core.DEPARTMENTS,
            format_func=lambda d: "不限" if d == "" else auth_core.DEPARTMENT_LABELS.get(d, d),
            key="acct_new_dept", help="manager/engineer 必选 P/Q/T; 仅 admin 可选「不限」")

    name_choice = st.selectbox(
        "姓名 *", options=_person_name_options() + [_MANUAL_NAME_FLAG],
        key="acct_new_name_sel",
        help="必须与问题中的责任人姓名完全一致。选项 = 配置名单 ∪ 问题中已出现的责任人。")
    if name_choice == _MANUAL_NAME_FLAG:
        new_display = st.text_input(
            "请输入姓名 *", key="acct_new_name_manual",
            placeholder="与问题里的责任人姓名完全相同(注意空格)")
    else:
        new_display = name_choice

    new_pwd = st.text_input("初始密码 * (至少6位)", type="password", key="acct_new_pwd")

    if st.button("✅ 创建账号", key="acct_create_btn", type="primary"):
        _u = str(new_username or "").strip()
        _n = str(new_display or "").strip()
        err = None
        if not _u:
            err = "账号不能为空"
        elif not _n:
            err = "姓名不能为空"
        elif len(str(new_pwd or "")) < 6:
            err = "初始密码至少 6 位"
        elif new_role in (auth_core.ROLE_MANAGER, auth_core.ROLE_ENGINEER) and not new_dept:
            err = "经理/工程师必须选择部门 P/Q/T"
        else:
            # 姓名唯一: 一个姓名只能绑定一个账号, 否则两人会看到同一批任务
            dup = [a for a in accounts
                   if str(a.get("display_name") or "").strip() == _n]
            if dup:
                err = "姓名「{}」已被账号「{}」使用 —— 一个姓名只能绑定一个账号, 否则两人会看到同一批任务".format(
                    _n, dup[0].get("username"))
        if err:
            st.error("❌ " + err)
        else:
            ok, msg = auth.create_account(_u, _n, new_pwd, role=new_role, must_change=1)
            if ok:
                if new_dept:
                    auth.set_department(_u, new_dept)
                _clear_manager_cache()
                st.success("✅ {}。首次登录需修改密码。".format(msg))
                st.rerun()
            else:
                st.error("❌ " + msg)

    st.divider()

    # ---------------- ② 已有账号列表 ----------------
    st.markdown("#### ② 已有账号")
    if not accounts:
        st.info("暂无账号。")
        return

    rows = []
    for a in accounts:
        _d = a.get("department")
        rows.append({
            "账号": a.get("username"),
            "姓名": a.get("display_name"),
            "角色": auth_core.ROLE_LABELS.get(a.get("role"), a.get("role")),
            "部门": auth_core.DEPARTMENT_LABELS.get(_d, _d or "不限"),
            "状态": "✅ 启用" if str(a.get("status")) == "active" else "⛔ 停用",
            "需改密": "是" if a.get("must_change_password") else "否",
            "最近登录": a.get("last_login_at") or "从未登录",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()

    # ---------------- ③ 账号操作 ----------------
    st.markdown("#### ③ 账号操作")
    sel = st.selectbox("选择要操作的账号",
                       [a.get("username") for a in accounts], key="acct_sel")
    sel_acc = next((a for a in accounts if a.get("username") == sel), None)
    if sel_acc is None:
        return

    is_self = (sel == _me)
    if is_self:
        st.warning("⚠️ 这是**你自己的账号**。为防止系统锁死, "
                   "不能修改自己的角色, 也不能停用自己。")

    st.caption("当前: 姓名「{}」· 角色「{}」· 状态「{}」".format(
        sel_acc.get("display_name"),
        auth_core.ROLE_LABELS.get(sel_acc.get("role"), sel_acc.get("role")),
        "启用" if str(sel_acc.get("status")) == "active" else "停用"))

    # ---- 3.1 修改角色 ----
    st.markdown("**修改角色**")
    c_role, c_role_btn = st.columns([3, 1])
    with c_role:
        role_new = st.selectbox(
            "新角色", options=auth_core.ROLES, format_func=_role_label,
            key="acct_role_new", label_visibility="collapsed")
    with c_role_btn:
        role_clicked = st.button("💾 保存", key="acct_role_save",
                                 disabled=is_self, use_container_width=True)
    if role_clicked and not is_self:
        # 保护: 不允许把最后一个启用管理员降级
        if (sel_acc.get("role") == auth_core.ROLE_ADMIN
                and role_new != auth_core.ROLE_ADMIN
                and _active_admin_count(accounts) <= 1):
            st.error("❌ 这是最后一个启用状态的管理员, 不能降级, 否则将无人能管理账号。")
        else:
            ok, msg = auth.set_role(sel, role_new)
            if ok:
                _clear_manager_cache()
                st.success("✅ " + msg)
                st.rerun()
            else:
                st.error("❌ " + msg)

    # ---- 3.2 修改部门(v3.1 QCS) ----
    st.markdown("**修改部门**")
    _cur_dept = str(sel_acc.get("department") or "")
    c_dept, c_dept_btn = st.columns([3, 1])
    with c_dept:
        dept_new = st.selectbox(
            "新部门", options=[""] + auth_core.DEPARTMENTS,
            format_func=lambda d: "不限" if d == "" else auth_core.DEPARTMENT_LABELS.get(d, d),
            index=(0 if not _cur_dept else ([""] + auth_core.DEPARTMENTS).index(_cur_dept)),
            key="acct_dept_new", label_visibility="collapsed")
    with c_dept_btn:
        dept_clicked = st.button("💾 保存部门", key="acct_dept_save", use_container_width=True)
    if dept_clicked:
        ok, msg = auth.set_department(sel, dept_new)
        if ok:
            _clear_manager_cache()
            st.success("✅ " + msg)
            st.rerun()
        else:
            st.error("❌ " + msg)

    # ---- 3.3 修改姓名 ----
    st.markdown("**修改姓名**")
    _cur_name = str(sel_acc.get("display_name") or "")
    _n_opts = _person_name_options()
    if _cur_name and _cur_name not in _n_opts:
        _n_opts = [_cur_name] + _n_opts      # 当前姓名可能不在名单里, 保证可选中
    name_sel = st.selectbox(
        "新姓名", options=_n_opts + [_MANUAL_NAME_FLAG], key="acct_name_sel",
        help="姓名必须与问题中的责任人姓名一致, 否则该用户登录后看不到自己的任务。")
    if name_sel == _MANUAL_NAME_FLAG:
        name_new = st.text_input("请输入新姓名", key="acct_name_manual")
    else:
        name_new = name_sel
    if st.button("💾 保存姓名", key="acct_name_save"):
        _nn = str(name_new or "").strip()
        if not _nn:
            st.error("❌ 姓名不能为空")
        else:
            dup = [a for a in accounts
                   if str(a.get("display_name") or "").strip() == _nn
                   and a.get("username") != sel]
            if dup:
                st.error("❌ 姓名「{}」已被账号「{}」使用, 一个姓名只能绑定一个账号。".format(
                    _nn, dup[0].get("username")))
            else:
                ok, msg = auth.set_display_name(sel, _nn)
                if ok:
                    # 若改的是自己, 同步刷新会话姓名(否则侧边栏仍显示旧值)
                    if is_self:
                        st.session_state["display_name"] = _nn
                    st.success("✅ " + msg)
                    st.rerun()
                else:
                    st.error("❌ " + msg)

    # ---- 3.3 重置密码 ----
    st.markdown("**重置密码**")
    reset_pwd = st.text_input("新密码(至少6位)", type="password", key="acct_reset_pwd")
    if st.button("🔑 重置密码", key="acct_reset_btn"):
        if len(str(reset_pwd or "")) < 6:
            st.error("❌ 新密码至少 6 位")
        else:
            ok, msg = auth.admin_reset_password(sel, reset_pwd)
            if ok:
                st.success("✅ {}。请把新密码告知本人。".format(msg))
                st.rerun()
            else:
                st.error("❌ " + msg)

    # ---- 3.4 启用 / 停用 ----
    st.markdown("**启用 / 停用**")
    if str(sel_acc.get("status")) == "active":
        if st.button("⛔ 停用该账号", key="acct_disable_btn", disabled=is_self):
            if (sel_acc.get("role") == auth_core.ROLE_ADMIN
                    and _active_admin_count(accounts) <= 1):
                st.error("❌ 这是最后一个启用状态的管理员, 不能停用, 否则将无人能管理账号。")
            else:
                ok, msg = auth.set_account_status(sel, "disabled")
                if ok:
                    _clear_manager_cache()
                    st.success("✅ 账号已停用")
                    st.rerun()
                else:
                    st.error("❌ " + msg)
    else:
        if st.button("✅ 启用该账号", key="acct_enable_btn"):
            ok, msg = auth.set_account_status(sel, "active")
            if ok:
                _clear_manager_cache()
                st.success("✅ 账号已启用")
                st.rerun()
            else:
                st.error("❌ " + msg)

    # ---- 3.5 删除账号 ----
    st.markdown("**删除账号**")
    if is_self:
        st.caption("⚠️ 不能删除当前登录的账号。")
    elif sel_acc.get("role") == auth_core.ROLE_ADMIN:
        st.caption("⚠️ admin 账号不允许删除。")
    else:
        if st.button("🗑️ 删除该账号", key="acct_del_btn"):
            st.session_state["confirm_del_account"] = sel

        if st.session_state.get("confirm_del_account") == sel:
            st.warning("确认删除账号「{}（{}）」？该账号会从列表中移除并立即下线; "
                       "其历史登录日志将保留, 审计轨迹不断链。".format(
                           sel_acc.get("display_name"), sel))
            d1, d2 = st.columns(2)
            with d1:
                if st.button("✅ 确认删除", key="acct_del_ok", type="primary"):
                    ok, msg = auth.delete_account(sel)
                    st.session_state.pop("confirm_del_account", None)
                    if ok:
                        _clear_manager_cache()
                        st.session_state["flash_success"] = "✅ " + msg
                    else:
                        st.error("❌ " + msg)
                    st.rerun()
            with d2:
                if st.button("❌ 取消", key="acct_del_cancel"):
                    st.session_state.pop("confirm_del_account", None)
                    st.rerun()

    st.caption("说明: 停用后该账号无法登录, 历史问题与审核记录不受影响。"
               "删除会移出账号主体并清除其会话(在线立即失效), 历史登录日志保留。")


# ============================================================
# v2.6 路由: 按角色渲染当前选中模块
# ============================================================
# 必须放在文件末尾 —— 此处所有 page_xxx 函数才定义完毕。
#
# 两道防线:
#   第一道 侧边栏功能菜单只列出当前角色有权访问的模块(界面层隐藏);
#   第二道 此处再校验一次(入口层拦截)。即便 session_state 被手工改成
#          无权限模块, 也只会看到"无权限访问", 不会渲染该模块任何内容。
_PAGE_ROUTES = {
    "register": page_register,
    "import": page_import,
    "dashboard": page_dashboard,
    "accounts": page_accounts,
}

_PAGE_LABELS = {k: lbl for k, lbl, _roles in _ALL_PAGES}

_current_page = st.session_state.get("nav_page") or "register"
_current_role_label = auth_core.ROLE_LABELS.get(_current_role(), _current_role())

if _current_page not in _PAGE_ROUTES or not _can_access(_current_page):
    st.error("🚫 无权限访问该模块")
    st.caption("当前角色「{}」不可访问「{}」。如需使用请联系管理员开通权限。".format(
        _current_role_label, _PAGE_LABELS.get(_current_page, _current_page)))
    st.stop()

st.caption("当前模块: {} ｜ 角色: {}".format(
    _PAGE_LABELS.get(_current_page, _current_page), _current_role_label))
st.divider()

_PAGE_ROUTES[_current_page]()
