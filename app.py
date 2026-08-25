"""
app.py - 车间问题分层审核与跟踪系统 (LPA) 主程序

当前版本: 七标签页(问题评审+不采纳列表) + 企微通知框架 + 自动关闭脚本
后续规划: 登录权限

版本记录:
  v0.1  问题录入 + 问题列表
  v0.2  问题列表新增删除功能: 点击行选中 → 表格下方确认删除(防误删),
        删除后自动清缓存并刷新列表
  v0.3  分层审核操作区(手动切换角色版):
        - 待处理问题由 issue_reviews 流水推导层级(见 db.compute_current_level):
          最近一次"退回"之后的最新记录决定该谁处理
        - 层级1: 通过(需照片+备注→状态'已评审') / 跟踪中(备注)
        - 层级2: 通过(照片+备注) / 退回(原因→'已退回') / 跟踪中
        - 层级3: 通过(照片+备注→'已关闭') / 退回(原因→'已退回') / 跟踪中
        - 通过必须上传现场照片(防造假门槛, 照片仅校验不存库)
        - 每条待处理问题下方展示历史审核记录(时间倒序)
  v0.4  页面分区 + 个人行动计划:
        - st.tabs 拆四个标签页: 问题登记 / 批量导入(占位) / 分层审核 / 个人行动计划
        - 新增"个人行动计划"页: 选责任人 → 按三个责任角色分组展示关联问题,
          第一责任人组额外显示当前层级
  v0.5  侧边栏全局"当前操作人"(替代手动切换角色):
        - 侧边栏选姓名, 系统自动判断他在每个问题上担任的角色层级
        - 分层审核页改为三个待办分组: 需要我整改(第1层) / 需要我验收(第2层)
          / 需要我最终确认(第3层), 移除"当前操作角色"手动切换
        - 审核记录 reviewer 改存真实姓名(此前版本存角色名)
        - 个人行动计划页直接跟随侧边栏所选人员, 移除页内下拉框
        - 未选择操作人时, 分层审核/个人行动计划页提示"请先在左侧选择当前操作人"
  v0.6  采纳字段 + 分析看板 + 企微通知框架 + 自动关闭脚本:
        - issues 表新增 is_adopted 字段(采纳=1/不采纳=0; 老库由 init_db
          用 PRAGMA+ALTER 自动补列, 存量行默认置为"采纳")
        - 录入表单新增"是否采纳"选择(默认采纳); 问题列表增加该列
        - 新增"分析看板"标签页(plotly四图):
          图1 月度趋势(柱=已退回/线=已评审) / 图2 第二责任人通过统计
          / 图3 第三责任人通过统计 / 图4 完成率+采纳率双饼图
        - 新增 notify.py 企微通知框架(WECHAT_WEBHOOK_URL留空则只打日志):
          第1层通过→通知第二责任人 / 第2层通过→通知第三责任人
          / 任意层退回→通知第一责任人(附退回人) / 第3层通过→不发通知
        - 新增 auto_close.py 独立脚本: 第三责任人连续两个自然月各通过
          一次的问题自动关闭(schedule库, 每天06:00检查)
  v0.7  批量导入:
        - 「批量导入」页实装: 上传xlsx/csv → 预览前5行 → 确认导入
        - 逐行校验(必填项/日期格式/采纳枚举), 失败行记录Excel行号+原因
          且不入库; 合格行逐条写入, 初始状态统一"未评审"
        - 中文CSV自动兼容GBK编码; 缺openpyxl时给出安装提示
        - 导入完成汇报成功/失败明细; 成功后自动清缓存并清空文件选择器
          (防止手滑把同一文件导两遍)
  v0.8  问题评审模块(采纳决策权移交第三责任人):
        - 「是否采纳」不再由录入人填写: 新登记问题统一进入"待评审"
          (is_adopted=NULL), 录入表单移除该选择框
        - 新增「问题评审」页: 第三责任人对未评审问题做决策 ——
          采纳(→已评审, 进入整改流程) / 不采纳(→搁置, 归档到不采纳列表)
        - 新增「不采纳列表」页: 二/三责可见全部不采纳问题;
          三责可对自己名下的问题"重新采纳"(→已评审, 重回整改流程)
        - 分层审核过滤规则调整: 组1=已评审/已退回且本人一责(等待上层
          验收期间只读防误操作), 组2/3=已评审且当前层级2/3;
          未评审与不采纳的问题不再进入分层审核
        - compute_current_level补充: 最新记录为第3层"通过"且status≠已关闭
          → 识别为"评审采纳", 层级=1(等第一责任人整改), 而非已关闭
        - 批量导入"是否采纳"改选填(留空=待评审); 看板采纳率饼图只统计
          已评审决策过的问题(待评审不参与)
  v0.9  全局序号统一 + 评审类记录过滤 + 第三责任人必填:
        - load_all_issues 按id升序返回并附加全局连续"序号"列(第一列),
          所有标签页统一用序号展示问题, 不再显示数据库id;
          页面筛选不重排序号, 同一问题各页编号一致
        - 新增问题表单: 第三责任人改为必填(他是评审决策人, 留空的
          问题会永远停在"未评审")
        - 图3与auto_close统一改用db.get_real_level3_pass_records:
          排除「问题评审」的采纳/重新采纳记录(评审打勾≠验收通过),
          修复图3打勾数虚高、自动关闭误判两个隐患
        - 评审类流水备注统一加【评审采纳】/【重新采纳】前缀标记

状态流转设计(参考 ERPNext Quality + Issue Tracker):
    未评审 → 已评审 → 已退回(打回重改) → 已关闭
    第一责任人(操作工)执行改善 → 第二责任人(工程师/主管)验证
    → 第三责任人(经理)确认关闭
    审核明细记录在 issue_reviews 表, 主表 issues.status 只存当前大状态。
"""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from notify import send_wechat_message

from config import SYSTEM_NAME, VERSION, WORK_STATIONS, REVIEW_LEVELS
from db import (
    init_db, save_issue, save_issues_batch, load_all_issues, delete_issue,
    load_all_reviews, add_review_record, update_issue_status,
    compute_current_level, get_all_responsibles, get_issues_by_person,
    get_pending_review_issues, get_not_adopted_issues, set_issue_adoption,
    get_real_level3_pass_records,
    _load_issues_cached, _load_all_reviews_cached,  # ★ v1.0: 侧边栏"立即刷新"按钮用
)

st.set_page_config(page_title=SYSTEM_NAME, page_icon="📋", layout="wide")

# ---------- 数据库初始化(首次运行自动建表, 幂等) ----------
try:
    init_db()
except Exception as e:
    st.error("数据库初始化失败: {}".format(e))
    st.stop()

# ---------- 页头 ----------
st.title("📋 " + SYSTEM_NAME)
st.caption("{} | 状态流转: 未评审 → 已评审 → 已退回(打回重改) → 已关闭".format(VERSION))

# ============================================================
# 侧边栏: 全局"当前操作人"选择器(所有标签页共用, v0.5)
# 真实场景: 每个责任人用自己的手机/电脑打开系统, 选自己的名字,
# 系统自动判断他在每个问题上是什么角色, 只给他看该他处理的内容,
# 不再需要手动切换"第一/第二/第三责任人"。
# ============================================================
with st.sidebar:
    st.header("👤 当前操作人")
    _all_names = get_all_responsibles()
    if not _all_names:
        st.info("暂无责任人数据 —— 请先在「问题登记」页录入问题并填写责任人。")
        current_person = None
    else:
        # index=None: 初始不选中任何人, 用户选择前返回None
        current_person = st.selectbox(
            "选择你的姓名",
            _all_names,
            index=None,
            placeholder="请选择你的姓名",
        )
        if current_person:
            st.caption("已选择 **{}**。「分层审核」和「个人行动计划」只显示与您相关的内容。".format(current_person))
        else:
            st.caption("选择姓名后,「分层审核」和「个人行动计划」将只显示与您相关的内容。")

    # ★ v1.0 新增: 立即刷新数据按钮 —— 移动端(手机)评审/审核写库走的是
    # 独立后端进程, 无法自动清掉本页面的缓存; 缓存虽已降到30秒自动过期,
    # 但手机端刚操作完想马上在电脑端看到, 点这个按钮立即清空两张表的缓存。
    if st.button("🔄 立即刷新数据", use_container_width=True,
                 help="手机端操作后电脑端没更新? 点此立即拉取最新数据(缓存30秒内也会自动过期)"):
        _load_issues_cached.clear()
        _load_all_reviews_cached.clear()
        st.session_state["flash_success"] = "✅ 数据已刷新(问题列表+审核记录均为最新)"
        st.rerun()

# 闪现提示: 提交/审核/删除成功后会rerun一次, 用session_state把成功信息
# 带到下一次渲染(放在标签页外, 无论当前停在哪个标签页都能看到)
if "flash_success" in st.session_state:
    st.success(st.session_state.pop("flash_success"))

# 图表通用配置: 隐藏plotly工具栏, 页面更干净(车间大屏/手机都友好)
_PLOTLY_CONFIG = {"displayModeBar": False}


def _notify_review_action(issue, level, action, operator):
    """按审核动作决定企微通知的对象和内容(v0.6通知规则)。
    - 第1层通过 → 通知第二责任人去验收
    - 第2层通过 → 通知第三责任人去最终确认
    - 第3层通过 → 问题已关闭, 按需求不发通知
    - 任意层退回 → 通知第一责任人继续整改(附退回人姓名)
    - 跟踪中    → 不发通知(问题没流转, 无需打扰)
    说明: webhook未配置时 send_wechat_message 只打印日志, 不会报错;
          通知对象为空(该问题没填这个责任人)时自动跳过。"""
    # v0.9: 对外文案统一用全局连续序号(与页面展示一致), 不再暴露数据库id
    disp_no = issue.get("序号", issue.get("id"))
    desc = str(issue.get("problem_description") or "")[:50]
    if action == "通过":
        if level == 1:
            receiver = str(issue.get("responsible_person_2") or "").strip()
            if receiver:
                send_wechat_message(
                    "【LPA审核流转】问题(序号{}): {}\n第一责任人已提交整改, 请 {} 及时验收。".format(
                        disp_no, desc, receiver))
        elif level == 2:
            receiver = str(issue.get("responsible_person_3") or "").strip()
            if receiver:
                send_wechat_message(
                    "【LPA审核流转】问题(序号{}): {}\n第二责任人已验收通过, 请 {} 最终确认。".format(
                        disp_no, desc, receiver))
        # level == 3: 问题已关闭, 按需求不发通知
    elif action == "退回":
        receiver = str(issue.get("responsible_person_1") or "").strip()
        if receiver:
            send_wechat_message(
                "【LPA审核退回】问题(序号{}): {}\n审核被退回, 请 {} 继续整改。退回人: {}".format(
                    disp_no, desc, receiver, operator))

# ============================================================
# 页面分区: 四个标签页
# ============================================================
tab_register, tab_import, tab_judge, tab_review, tab_rejected, tab_plan, tab_dashboard = st.tabs(
    ["📝 问题登记", "📦 批量导入", "⚖️ 问题评审", "✅ 分层审核",
     "🚫 不采纳列表", "🎯 个人行动计划", "📊 分析看板"])

# ============================================================
# 标签页1「问题登记」: 新增问题表单 + 问题列表(含删除)
# (登记页不做人员过滤: 任何人都可能帮现场登记问题, 列表是全员总览)
# ============================================================
with tab_register:
    # ========================================================
    # 一、新增问题表单
    # 用 st.form: 表单内的输入不会触发页面刷新, 点"提交"才一次性提交(录入不卡)
    # ========================================================
    st.subheader("➕ 新增问题")

    # 表单控件的key(提交成功后逐个清除, 让表单恢复空白)
    _FORM_KEYS = ["form_desc", "form_action", "form_ws",
                  "form_p1", "form_p2", "form_p3"]

    with st.form("add_issue_form"):
        col_left, col_right = st.columns(2)

        with col_left:
            issue_date = st.date_input("问题发现日期", value=date.today())
            if WORK_STATIONS:
                # config.py 里配置了工位清单 → 下拉框选择(防止工位名录得不统一)
                work_station = st.selectbox("工位 *", WORK_STATIONS, key="form_ws")
            else:
                # 清单为空 → 手动输入
                work_station = st.text_input(
                    "工位 *", placeholder="如: 车削1工位", key="form_ws")

        with col_right:
            responsible_person_1 = st.text_input(
                "第一责任人(操作工) *", placeholder="发现问题/执行改善的操作工", key="form_p1")
            responsible_person_2 = st.text_input(
                "第二责任人(工程师/主管)", placeholder="验证改善措施的责任人", key="form_p2")
            responsible_person_3 = st.text_input(
                "第三责任人(经理) *", placeholder="评审决策人(必填)", key="form_p3")
            # v0.8: 不再由录入人填"是否采纳" —— 新问题统一"待评审",
            # 由第三责任人在「问题评审」页决策(采纳/不采纳)

        problem_description = st.text_area(
            "问题描述 *", height=100,
            placeholder="发生了什么问题、在哪里发现的、有什么影响", key="form_desc")
        improvement_action = st.text_area(
            "改善措施", height=80,
            placeholder="针对该问题采取的措施(也可以后续补充)", key="form_action")

        submitted = st.form_submit_button(
            "💾 提交问题", use_container_width=True, type="primary")

    if submitted:
        # ---- 必填校验(校验不过不刷新页面, 已填内容保留在表单里) ----
        missing = []
        if not str(problem_description or "").strip():
            missing.append("问题描述")
        if not str(work_station or "").strip():
            missing.append("工位")
        if not str(responsible_person_1 or "").strip():
            missing.append("第一责任人")
        # v0.9: 第三责任人必填 —— 他是评审决策人, 留空的问题会永远停在"未评审"
        p3_missing = not str(responsible_person_3 or "").strip()
        if missing or p3_missing:
            tips = []
            if missing:
                tips.append("以下必填项未填写: " + "、".join(missing))
            if p3_missing:
                tips.append("请填写第三责任人（评审决策人）")
            st.warning("；".join(tips))
        else:
            # ---- 保存到数据库 ----
            try:
                save_issue(
                    problem_description=str(problem_description).strip(),
                    issue_date=issue_date.strftime("%Y-%m-%d"),
                    work_station=str(work_station).strip(),
                    improvement_action=str(improvement_action or "").strip(),
                    responsible_person_1=str(responsible_person_1).strip(),
                    responsible_person_2=str(responsible_person_2 or "").strip(),
                    responsible_person_3=str(responsible_person_3 or "").strip(),
                )
                # 成功信息存入session_state → rerun后仍然能看到提示
                st.session_state["flash_success"] = "✅ 问题已登记, 等待第三责任人评审(见下方问题列表)"
                # 清空表单控件状态 → rerun后表单恢复空白
                for k in _FORM_KEYS:
                    st.session_state.pop(k, None)
                st.rerun()  # 刷新页面 → 问题列表显示最新数据
            except Exception as e:
                st.error("保存失败: {}".format(e))

    st.markdown("---")

    # ========================================================
    # 二、问题列表(所有已登记问题, 最新在前, 含删除功能)
    # ========================================================
    st.subheader("📋 问题列表")

    df = load_all_issues()

    if df.empty:
        st.info("暂无问题记录。请在上方登记第一个问题。")
    else:
        # 数据库英文字段 → 界面中文列名(只改展示, 不动数据库字段名)
        # 注: 主键id不再直接展示 —— 删除过测试数据后自增id不连续,
        #     界面改用连续"序号"(见下方insert); 底层id仅在删除时内部使用
        column_labels = {
            "problem_description": "问题描述",
            "issue_date": "问题日期",
            "work_station": "工位",
            "improvement_action": "改善措施",
            "responsible_person_1": "第一责任人",
            "responsible_person_2": "第二责任人",
            "responsible_person_3": "第三责任人",
            "status": "状态",
            "is_adopted": "是否采纳",
            "created_at": "创建时间",
            "updated_at": "更新时间",
        }
        display_df = df.rename(columns=column_labels)
        # v0.9: 序号列来自 load_all_issues(按id升序的全局连续编号, 第一列),
        # 任何页面过滤后序号不变 → 同一问题在所有标签页编号一致;
        # 数据库id列保留在df里供删除等内部逻辑定位, 但不再展示
        keep_cols = [c for c in ["序号"] + list(column_labels.values())
                     if c in display_df.columns]
        display_df = display_df[keep_cols]
        # 是否采纳(v0.8三态): NULL=待评审 / 1=采纳 / 0=不采纳
        if "是否采纳" in display_df.columns:
            def _adopt_label(v):
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return "待评审"
                try:
                    return "采纳" if int(v) == 1 else "不采纳"
                except (TypeError, ValueError):
                    return "待评审"
            display_df["是否采纳"] = display_df["是否采纳"].map(_adopt_label)

        # ========================================================
        # 删除功能: 表格行选中 + 确认删除
        # 说明: Streamlit的表格单元格里放不进真正的按钮; 若为每一行单独
        # 生成按钮+文本控件, 数据到几百条时页面会明显变卡。所以采用
        # ERPNext/Notion 同款交互: 点击行选中 → 表格下方出现删除确认区。
        # ========================================================
        st.caption("💡 提示: 点击表格中的行可选中, 选中后可在表格下方删除该问题")

        # 表格版本号: 每次删除/取消后 +1 → 表格key变化 → 选中状态自动清空
        # (防止删除一条后, 旧的选中位置"串行"到下一行别的记录上)
        if "table_version" not in st.session_state:
            st.session_state["table_version"] = 0

        event = st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            height=420,
            on_select="rerun",            # 点击行时触发一次页面刷新
            selection_mode="single-row",  # 只允许选中一行(删一条看一条)
            key="issue_table_{}".format(st.session_state["table_version"]),
        )

        # ---------- 删除确认区(选中某一行后才会显示) ----------
        selected_rows = event.selection.rows  # 选中的行号列表(单行模式最多1个)
        if selected_rows:
            row_idx = selected_rows[0]
            # display_df与df行序完全一致(序号列只是新插入的展示列),
            # 所以表格选中行号直接对应df.iloc[row_idx], 映射不受影响
            if row_idx < len(df):
                sel = df.iloc[row_idx]  # 选中行的完整数据
                issue_id = int(sel["id"])  # 数据库真实主键(仅删除操作内部使用, 不展示)
                desc_preview = str(sel.get("problem_description") or "")[:40]

                st.markdown("---")
                # 确认提示: 用全局连续序号标识(与所有标签页一致), 防止删错
                st.warning(
                    "已选中 **序号 {}** 的问题（工位: {} | 摘要: {}…）。"
                    "删除后不可恢复, 且后续记录序号会整体前移, 请确认!".format(
                        sel.get("序号", row_idx + 1),
                        sel.get("work_station") or "未填", desc_preview))

                col_del, col_cancel = st.columns(2)
                do_delete = col_del.button(
                    "🗑️ 确认删除", type="primary", use_container_width=True)
                do_cancel = col_cancel.button(
                    "↩️ 取消选择", use_container_width=True)

                if do_delete:
                    try:
                        # 调用后端删除(函数内部会自动清缓存)
                        delete_issue(issue_id)
                        # 成功提示存入session_state, rerun后顶部显示(复用闪现机制)
                        # (不用id——列表已改为连续序号, 且删除后序号会重排)
                        st.session_state["flash_success"] = "✅ 已删除问题「{}…」".format(desc_preview)
                        # 表格版本+1 → 重置选中状态, 列表刷新后不再显示该记录
                        st.session_state["table_version"] += 1
                        st.rerun()
                    except Exception as e:
                        st.error("删除失败: {}".format(e))
                elif do_cancel:
                    # 取消: 表格版本+1 → 选中状态清空, 删除区收起
                    st.session_state["table_version"] += 1
                    st.rerun()

        # 状态小结(一行看懂当前有多少问题在流转)
        cnt = df["status"].value_counts().to_dict()
        st.caption("共 {} 条 | 未评审 {} | 已评审 {} | 已退回 {} | 已关闭 {}".format(
            len(df), cnt.get("未评审", 0), cnt.get("已评审", 0),
            cnt.get("已退回", 0), cnt.get("已关闭", 0)))

# ============================================================
# 标签页2「批量导入」: Excel/CSV批量导入问题 (v0.7)
# 流程: 上传文件 → 预览前5行 → 点「确认导入」→ 逐行校验 →
#       合格行批量入库 → 汇报成功数和失败明细(Excel行号+原因)
# 说明: 不做去重(重复导入同一文件会产生重复数据, 由用户自行把关);
#       导入的问题初始状态统一为"未评审", 与手工登记完全一致。
# ============================================================
with tab_import:
    st.subheader("📦 批量导入")
    st.caption(
        "支持 .xlsx / .csv 文件。模板列: 问题描述*、发现日期*、工位、改善措施、"
        "第一责任人*、第二责任人、第三责任人、是否采纳(选填: 采纳 / 不采纳 / 留空)。"
        "带 * 为必填列; 是否采纳留空表示'待评审', 由第三责任人在「问题评审」页决策。")

    # ---------- 上传控件 ----------
    # 若上一轮刚导入完成 → 在控件实例化之前删除其会话状态, 让上传框恢复空白,
    # 防止手滑把同一文件导两遍产生重复数据。
    # 注意: Streamlit禁止对file_uploader的key直接赋值(会抛StreamlitAPIException),
    # 只能用pop删除状态; 删除后下方重建的同key控件即为空白, 无需额外rerun
    if st.session_state.pop("clear_import_uploader", False):
        st.session_state.pop("import_file", None)

    uploaded = st.file_uploader(
        "选择要导入的文件", type=["xlsx", "csv"], key="import_file")

    def _read_upload(file):
        """读取上传文件 → DataFrame。
        中文CSV常为GBK编码: 先按utf-8读, 解码失败自动换GBK重读一次。"""
        if file.name.lower().endswith(".csv"):
            try:
                return pd.read_csv(file)
            except UnicodeDecodeError:
                file.seek(0)  # 重置文件读取位置, 换编码再读
                return pd.read_csv(file, encoding="gbk")
        return pd.read_excel(file)  # xlsx格式(依赖openpyxl库)

    def _cell_str(value):
        """单元格安全转字符串: 空值/NaN → 空串, 其余去首尾空格"""
        try:
            if value is None or pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass  # 个别类型pd.isna会抛异常, 按有值处理
        return str(value).strip()

    if uploaded is None:
        # ---------- 未选文件: 显示上次导入结果(如有), 否则引导提示 ----------
        if "import_result" in st.session_state:
            res = st.session_state["import_result"]
            if res["success"] > 0:
                st.success("✅ 批量导入完成: 成功 {} 条, 失败 {} 条(文件共 {} 行)。"
                           "新问题已可在「问题登记」页查看。".format(
                               res["success"], len(res["fails"]), res["total"]))
            else:
                st.error("❌ 本次导入 0 条成功, 失败 {} 条(文件共 {} 行)。"
                         "请按失败明细修正文件后重新上传。".format(
                             len(res["fails"]), res["total"]))
            if res["fails"]:
                with st.expander("失败明细({} 条)".format(len(res["fails"])), expanded=True):
                    fail_df = pd.DataFrame(res["fails"], columns=["Excel行号", "失败原因"])
                    st.dataframe(fail_df, use_container_width=True, hide_index=True)
                    st.caption("失败行不会入库; 修正后可将失败行单独存成新文件再导一次。")
        else:
            st.info("👆 请先选择文件。上传后先预览前5行, 确认无误再点「确认导入」。")

    else:
        # 选择了新文件 → 清掉上一次的导入结果, 避免新旧结果混淆
        st.session_state.pop("import_result", None)

        # ---------- 读取并做文件级检查 ----------
        try:
            raw_df = _read_upload(uploaded)
        except ImportError:
            # pandas读xlsx需要openpyxl, 没装时给出明确的安装指引
            st.error("缺少Excel读取组件。请在命令行执行: pip install openpyxl , 然后重启系统。")
            raw_df = None
        except Exception as e:
            st.error("文件读取失败: {}。请确认是有效的 .xlsx 或 .csv 文件。".format(e))
            raw_df = None

        if raw_df is not None:
            # 列名去首尾空格(Excel表头常带看不见的空格, 防止"缺列"误报)
            raw_df.columns = [str(c).strip() for c in raw_df.columns]

            # 必需列检查(工位/改善措施/第二、三责任人/是否采纳缺失或留空时
            # 按空值处理, 不报错; v0.8起"是否采纳"为选填)
            _REQUIRED_COLUMNS = ["问题描述", "发现日期", "第一责任人"]
            missing = [c for c in _REQUIRED_COLUMNS if c not in raw_df.columns]

            if raw_df.empty:
                st.warning("文件里没有数据行(只有表头或完全是空的), 请检查后重新上传。")
            elif missing:
                st.error("文件缺少必需列: {}。请把表头改成模板列名后重新上传。".format(
                    "、".join(missing)))
            else:
                # ---------- 预览前5行 ----------
                st.markdown("**前5行预览**(请确认列名和数据内容是否正确):")
                st.dataframe(raw_df.head(5), use_container_width=True, hide_index=True)
                st.info("文件共识别到 **{}** 行数据(不含表头)。确认无误后点击下方按钮开始导入。".format(
                    len(raw_df)))

                if st.button("✅ 确认导入", type="primary", use_container_width=True):
                    # ---------- 逐行校验(不合格行记录Excel行号和原因, 不入库) ----------
                    valid_rows, valid_indices, check_fails = [], [], []
                    for idx, row in raw_df.iterrows():
                        # pandas行号从0起, Excel第1行是表头 → 实际行号 = idx + 2
                        excel_row = idx + 2

                        desc = _cell_str(row.get("问题描述"))
                        p1 = _cell_str(row.get("第一责任人"))
                        adopt = _cell_str(row.get("是否采纳"))

                        if not desc:
                            check_fails.append((excel_row, "问题描述为空"))
                            continue
                        if not p1:
                            check_fails.append((excel_row, "第一责任人为空"))
                            continue
                        if adopt not in ("", "采纳", "不采纳"):
                            check_fails.append((excel_row, "是否采纳填了「{}」, 只能填: 采纳 / 不采纳 / 留空".format(adopt)))
                            continue
                        # 日期先转字符串再解析(纯数字日期如20260822也能正确识别;
                        # 识别不了得到NaT → 该行判失败, 而不是静默存入错误日期)
                        date_raw = _cell_str(row.get("发现日期"))
                        parsed = pd.to_datetime(date_raw, errors="coerce") if date_raw else None
                        if parsed is None or pd.isna(parsed):
                            check_fails.append((excel_row,
                                "发现日期「{}」无法识别, 请写成 2026/8/22 这样的日期".format(date_raw)))
                            continue

                        # ---- 校验通过 → 组装成数据库行 ----
                        valid_rows.append({
                            "problem_description": desc,
                            "issue_date": parsed.strftime("%Y-%m-%d"),
                            "work_station": _cell_str(row.get("工位")),
                            "improvement_action": _cell_str(row.get("改善措施")),
                            "responsible_person_1": p1,
                            "responsible_person_2": _cell_str(row.get("第二责任人")),
                            "responsible_person_3": _cell_str(row.get("第三责任人")),
                            # v0.8: 留空→NULL(待评审); 采纳→1; 不采纳→0
                            "is_adopted": None if adopt == "" else (1 if adopt == "采纳" else 0),
                        })
                        # 保留原始行号 → 入库阶段若失败, 仍能对应回Excel行
                        valid_indices.append(idx)

                    # ---------- 合格行批量入库 ----------
                    ok_count, db_fails = 0, []
                    if valid_rows:
                        valid_df = pd.DataFrame(valid_rows, index=valid_indices)
                        with st.spinner("正在导入 {} 条数据(逐条写入云端数据库), 请勿关闭页面...".format(
                                len(valid_rows))):
                            try:
                                ok_count, _, db_fail_raw = save_issues_batch(valid_df)
                                # 数据库层失败(网络等)也换算成Excel行号, 与校验失败合并汇报
                                db_fails = [(i + 2, r) for i, r in db_fail_raw]
                            except Exception as e:
                                st.error("导入过程出现异常, 本次未完成: {}".format(e))
                                ok_count = None  # 异常时不落结果, 文件保留让用户重试

                    if ok_count is not None:
                        # ---------- 结果存session_state, rerun后统一展示 ----------
                        st.session_state["import_result"] = {
                            "total": len(raw_df),
                            "success": ok_count,
                            "fails": check_fails + db_fails,
                        }
                        # 标记"清空文件选择器"(下轮开头、控件创建前生效)
                        st.session_state["clear_import_uploader"] = True
                        st.rerun()

# ============================================================
# 标签页3「问题评审」: 第三责任人的采纳/不采纳决策 (v0.8)
# 新流程: 问题登记后不再由录入人决定"是否采纳", 而是统一进入
# "待评审"(is_adopted=NULL), 由第三责任人(经理)在此决策:
#   ✅ 采纳   → is_adopted=1, status='已评审', 进入分层整改流程
#   🚫 不采纳 → is_adopted=0, status保持'未评审', 问题被搁置
#               (不进任何人的分层审核待办, 归档到「不采纳列表」)
# 待评审范围: status='未评审' 且 is_adopted≠0 且 当前操作人=第三责任人
# (v0.7批量导入的旧数据虽是"采纳", 但未经过评审, 也会出现在这里重新决策)
# ============================================================
with tab_judge:
    st.subheader("⚖️ 问题评审")

    if not current_person:
        st.warning("请先在左侧选择当前操作人")
    else:
        pending_df = get_pending_review_issues(current_person)

        if pending_df.empty:
            st.info("当前没有待「{}」评审的问题。".format(current_person))
        else:
            st.caption("待评审 **{}** 条 —— 采纳后进入分层整改流程; "
                       "不采纳则搁置到「不采纳列表」。".format(len(pending_df)))

            for _, issue in pending_df.iterrows():
                issue_id = int(issue["id"])  # 数据库主键(仅写库定位用, 不展示)
                disp_no = issue.get("序号", issue_id)  # 展示用全局连续序号(v0.9)
                desc_preview = str(issue.get("problem_description") or "")[:30]
                with st.expander(
                        "序号 {} | {} | {}".format(
                            disp_no, issue.get("work_station") or "未填工位", desc_preview),
                        expanded=(len(pending_df) == 1),  # 只剩一条时默认展开, 方便收尾
                ):
                    # ---- 问题详情 ----
                    col_info, col_owner = st.columns(2)
                    with col_info:
                        st.markdown("**问题描述**")
                        st.write(issue.get("problem_description") or "(未填写)")
                        st.markdown("**改善措施**")
                        st.write(issue.get("improvement_action") or "(未填写)")
                    with col_owner:
                        st.markdown("**责任人**")
                        st.write("第一责任人: {}".format(issue.get("responsible_person_1") or "未填"))
                        st.write("第二责任人: {}".format(issue.get("responsible_person_2") or "未填"))
                        st.write("第三责任人: {}".format(issue.get("responsible_person_3") or "未填"))
                        st.caption("发现日期: {} | 当前状态: {}".format(
                            issue.get("issue_date") or "未填", issue.get("status")))

                    # ---- 评审决策按钮(两个动作都立即写库并刷新) ----
                    col_adopt, col_reject = st.columns(2)
                    btn_adopt = col_adopt.button(
                        "✅ 采纳", key="judge_adopt_{}".format(issue_id),
                        type="primary", use_container_width=True,
                        help="该问题纳入改善跟踪, 进入分层整改流程")
                    btn_reject = col_reject.button(
                        "🚫 不采纳", key="judge_reject_{}".format(issue_id),
                        use_container_width=True,
                        help="该问题暂不整改, 搁置归档(以后可重新采纳)")

                    if btn_adopt or btn_reject:
                        try:
                            if btn_adopt:
                                # 采纳: 标记1 + 状态'已评审' + 第3层'通过'流水
                                # (备注带【评审采纳】前缀: 图3统计和自动关闭
                                #  靠它识别这是评审记录而非真验收, 会自动排除)
                                set_issue_adoption(
                                    issue_id, 1, "已评审", current_person,
                                    "通过", "【评审采纳】进入整改流程")
                                st.session_state["flash_success"] = (
                                    "✅ 问题(序号{}) 评审采纳, 已进入整改流程(状态: 已评审)".format(disp_no))
                            else:
                                # 不采纳: 标记0 + 状态保持'未评审' + 第3层'退回'流水
                                set_issue_adoption(
                                    issue_id, 0, None, current_person,
                                    "退回", "评审不采纳，暂不整改")
                                st.session_state["flash_success"] = (
                                    "🚫 问题(序号{}) 评审不采纳, 已搁置(可在「不采纳列表」重新采纳)".format(disp_no))
                            st.rerun()  # 刷新 → 该问题从待评审列表消失
                        except Exception as e:
                            st.error("评审提交失败: {}".format(e))

# ============================================================
# 标签页4「分层审核」: 按侧边栏"当前操作人"自动匹配待办 (v0.8过滤规则)
# 只处理已通过评审进入整改流程的问题, 分组规则:
#   第1组: status∈(已评审,已退回) 且 当前操作人是第一责任人
#          —— 需要他整改; 若问题正在上层验收(层级2/3)则只读展示防误操作
#   第2组: status=已评审 且 当前层级=2 且 当前操作人是第二责任人
#   第3组: status=已评审 且 当前层级=3 且 当前操作人是第三责任人
# 一律排除: status=未评审(等第三责任人评审, 见「问题评审」页)、
#           is_adopted=0(不采纳)、已关闭的问题。
# ============================================================
with tab_review:
    # ---------- 未选择操作人: 给出明确提示 ----------
    if not current_person:
        st.warning("请先在左侧选择当前操作人")
    else:
        st.subheader("✅ 分层审核 (当前操作人: {})".format(current_person))
        st.caption("只显示已通过评审进入整改流程的问题, 按"
                   "「整改 / 验收 / 最终确认」三组列出; 新登记的问题请先到"
                   "「问题评审」页由第三责任人评审。")

        issues_df = load_all_issues()
        reviews_df = load_all_reviews()

        if issues_df.empty:
            st.info("暂无问题记录, 无法审核。请先在「问题登记」页登记问题。")
        else:
            # ---------- 审核记录按问题分组(内存分组, 不增加网络请求) ----------
            reviews_by_issue = {}
            if not reviews_df.empty:
                for _, row in reviews_df.iterrows():
                    reviews_by_issue.setdefault(row["issue_id"], []).append(row.to_dict())

            # ---------- 该人在此问题上担任的层级(可能身兼多职) ----------
            # 说明: 不用"只查第一个命中字段"的写法 —— 同一人可能在同一
            # 问题上身兼两层(如班长同时是第一、第二责任人), 三个字段全查,
            # 保证无论轮到哪一层, 他的任务都不会漏。
            def _person_roles(issue_row):
                roles = []
                for r in (1, 2, 3):
                    field_val = str(issue_row.get("responsible_person_{}".format(r)) or "").strip()
                    if field_val == current_person.strip():
                        roles.append(r)
                return roles

            # ---------- 判断问题是否"不采纳"(v0.8: 不采纳不进分层审核) ----------
            def _is_rejected_issue(issue_row):
                v = issue_row.get("is_adopted")
                try:
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        return False
                    return int(v) == 0
                except (TypeError, ValueError):
                    return False

            # ---------- 逐条按v0.8规则归入三个待办分组 ----------
            # 组内元组: (问题, 审核记录列表, 当前层级cur_level)
            #   组1的cur_level用于区分"待我整改(=1)"和"已提交等上层验收(=2/3)"
            groups = {1: [], 2: [], 3: []}
            for _, issue in issues_df.iterrows():
                if _is_rejected_issue(issue):
                    continue  # 不采纳的问题被搁置, 不进任何人的分层审核
                status = issue["status"]
                roles = _person_roles(issue)
                records = reviews_by_issue.get(issue["id"], [])
                level = compute_current_level(records, status)
                # 组1: 第一责任人视角 —— 整改流程中(已评审)或被打回(已退回)
                if 1 in roles and status in ("已评审", "已退回"):
                    groups[1].append((issue, records, level))
                # 组2: 第二责任人验收 —— 已评审且恰好轮到第2层
                if 2 in roles and status == "已评审" and level == 2:
                    groups[2].append((issue, records, level))
                # 组3: 第三责任人最终确认 —— 已评审且恰好轮到第3层
                if 3 in roles and status == "已评审" and level == 3:
                    groups[3].append((issue, records, level))

            # 分组标题(层级 → 标题)
            GROUP_TITLES = {
                1: "🛠️ 需要我整改的问题（我是第一责任人）",
                2: "🔍 需要我验收的问题（我是第二责任人）",
                3: "🏁 需要我最终确认的问题（我是第三责任人）",
            }

            total_pending = len(groups[1]) + len(groups[2]) + len(groups[3])
            if total_pending == 0:
                st.success("🎉 当前没有需要「{}」处理的问题。".format(current_person))
            else:
                # ---------- 逐组渲染待办 ----------
                for level in (1, 2, 3):
                    pending = groups[level]
                    st.markdown("---")
                    st.markdown("**{}（{} 条）**".format(GROUP_TITLES[level], len(pending)))
                    if not pending:
                        st.caption("无")
                        continue

                    # ---------- 逐条渲染问题 ----------
                    for issue, records, cur_level in pending:
                        issue_id = int(issue["id"])  # 数据库主键(仅写库定位用, 不展示)
                        disp_no = issue.get("序号", issue_id)  # 展示用全局连续序号(v0.9)
                        desc_preview = str(issue.get("problem_description") or "")[:30]
                        with st.expander(
                            "序号 {} | {} | {}".format(
                                disp_no, issue.get("work_station") or "未填工位", desc_preview),
                            expanded=(total_pending == 1),  # 全部只剩一条时默认展开, 方便收尾
                        ):
                            # ---- 问题详情 ----
                            col_info, col_owner = st.columns(2)
                            with col_info:
                                st.markdown("**问题描述**")
                                st.write(issue.get("problem_description") or "(未填写)")
                                st.markdown("**改善措施**")
                                st.write(issue.get("improvement_action") or "(未填写)")
                            with col_owner:
                                st.markdown("**责任人**")
                                st.write("第一责任人: {}".format(issue.get("responsible_person_1") or "未填"))
                                st.write("第二责任人: {}".format(issue.get("responsible_person_2") or "未填"))
                                st.write("第三责任人: {}".format(issue.get("responsible_person_3") or "未填"))
                                st.caption("发现日期: {} | 当前状态: {}".format(
                                    issue.get("issue_date") or "未填", issue.get("status")))

                            # ---- 历史审核记录(按时间倒序) ----
                            if records:
                                st.markdown("**历史审核记录**")
                                hist = pd.DataFrame(records).sort_values("id", ascending=False)
                                hist = hist.rename(columns={
                                    "review_level": "审核层级",
                                    "reviewer": "审核人",
                                    "review_result": "结果",
                                    "review_comment": "备注",
                                    "reviewed_at": "审核时间",
                                })
                                # 层级数字翻译成角色名, 看记录不用脑内换算
                                if "审核层级" in hist.columns:
                                    hist["审核层级"] = hist["审核层级"].map(
                                        lambda lv: REVIEW_LEVELS.get(lv, str(lv)))
                                show_cols = [c for c in ["审核层级", "审核人", "结果", "备注", "审核时间"]
                                             if c in hist.columns]
                                st.dataframe(hist[show_cols], hide_index=True, use_container_width=True)
                            else:
                                st.caption("暂无审核记录 —— 该问题还没有任何人处理过。")

                            # ---- 审核操作区: 照片 + 备注 + 三个按钮 ----
                            st.markdown("---")
                            if level == 1 and cur_level != 1:
                                # 组1里"整改已提交、正在上层验收"的问题: 只读展示,
                                # 收起操作按钮(此时点"通过"会把流程拽回第2层, 属误操作)
                                st.info("您的整改已提交, 该问题正在第{}层验收中, 暂无需您操作。".format(cur_level))
                                continue  # 跳过下方操作区, 渲染下一条问题
                            st.markdown("**审核操作** (您正在以 第{}层·{} 身份操作)".format(
                                level, REVIEW_LEVELS.get(level, "")))

                            # 防造假门槛: 点"通过"必须上传整改后现场照片。
                            # 说明: 照片仅作为提交校验(证明人真的到过现场), 不存入数据库
                            # (表结构未含照片字段, 且本次不允许改表)。
                            photo = st.file_uploader(
                                "整改后现场照片(点✅通过时必传, 防造假)",
                                type=["jpg", "jpeg", "png", "webp", "heic"],
                                key="photo_{}".format(issue_id),
                            )
                            comment = st.text_area(
                                "审核备注 * (通过=整改说明 / 退回=不合格原因 / 跟踪中=当前进展)",
                                key="comment_{}".format(issue_id), height=80)

                            # 按钮配置(严格按状态机规则):
                            #   层级1: 通过/跟踪中(第一责任人是整改方, 无退回按钮)
                            #   层级2/3: 通过/退回/跟踪中
                            col_pass, col_reject, col_track = st.columns(3)
                            btn_pass = col_pass.button(
                                "✅ 通过", key="pass_{}".format(issue_id),
                                use_container_width=True, type="primary")
                            btn_reject = col_reject.button(
                                "❌ 退回", key="reject_{}".format(issue_id),
                                use_container_width=True,
                                disabled=(level == 1),  # 第一责任人(整改方)无退回按钮
                                help="整改方没有退回按钮" if level == 1
                                     else "验收不合格, 打回第一责任人重新整改")
                            btn_track = col_track.button(
                                "🔺 跟踪中", key="track_{}".format(issue_id),
                                use_container_width=True)

                            # ---------- 提交处理(三个按钮共用一套校验+写库逻辑) ----------
                            action = None
                            if btn_pass:
                                action = "通过"
                            elif btn_reject:
                                action = "退回"
                            elif btn_track:
                                action = "跟踪中"

                            if action:
                                # 校验1: "通过"必须上传照片(防造假), 其余操作不强制
                                if action == "通过" and photo is None:
                                    st.error("点✅通过必须上传整改后现场照片(防造假), 请先选择照片再提交。")
                                # 校验2: 备注必填(通过=整改说明 / 退回=不合格原因 / 跟踪中=进展)
                                elif not str(comment or "").strip():
                                    st.error("审核备注必填: 通过=整改说明 / 退回=不合格原因 / 跟踪中=当前进展。")
                                else:
                                    try:
                                        # 1) 插入审核流水(reviewer记录真实姓名, 比角色名更可追溯)
                                        add_review_record(
                                            issue_id=issue_id,
                                            review_level=level,
                                            reviewer=current_person,
                                            review_result=action,
                                            review_comment=str(comment).strip(),
                                        )
                                        # 2) 按状态机规则更新主表状态(跟踪中不改状态)
                                        if action == "通过":
                                            # 层级1通过→已评审; 层级2通过→保持已评审; 层级3通过→已关闭
                                            new_status = "已关闭" if level == 3 else "已评审"
                                            update_issue_status(issue_id, new_status)
                                            next_hint = ("问题已关闭, 闭环!" if level == 3
                                                         else "已流转到第{}层验收".format(level + 1))
                                        elif action == "退回":
                                            # 任何一层退回 → 打回第一责任人重新整改
                                            update_issue_status(issue_id, "已退回")
                                            next_hint = "已打回第一责任人重新整改"
                                        else:
                                            next_hint = "问题继续停留在第{}层跟踪".format(level)
                                        # 3) 企微通知(v0.6): webhook未配置时只打印日志,
                                        #    通知失败绝不影响审核主流程
                                        try:
                                            _notify_review_action(issue, level, action, current_person)
                                        except Exception as e:
                                            print("通知发送异常(不影响审核结果):", e)
                                        # 成功提示存session_state, rerun后顶部显示(复用闪现机制)
                                        st.session_state["flash_success"] = (
                                            "✅ 问题(序号{}) 已提交「{}」, {}".format(
                                                disp_no, action, next_hint))
                                        st.rerun()  # 刷新 → 待办分组和审核记录立即更新
                                    except Exception as e:
                                        st.error("提交失败: {}".format(e))

# ============================================================
# 标签页5「不采纳列表」: 评审"不采纳"的问题归档 (v0.8)
# 可见性: 当前操作人是任意一条不采纳问题的第二/第三责任人时才展示内容;
# 展示范围: 所有 is_adopted=0 的问题(第二责任人可查看了解全局, 只读)。
# 操作权限: 第三责任人可对自己名下的不采纳问题"重新采纳"
#   → is_adopted=1 + status='已评审' + 第3层'通过'流水, 重回整改流程。
# ============================================================
with tab_rejected:
    st.subheader("🚫 不采纳列表")

    if not current_person:
        st.warning("请先在左侧选择当前操作人")
    else:
        # ---------- 可见性判断: 该人是否与任何不采纳问题相关(二责或三责) ----------
        related_df = get_not_adopted_issues(current_person)
        if related_df.empty:
            st.info("「{}」名下没有不采纳的问题, 无需查看此列表。".format(current_person))
        else:
            # ---------- 展示全部不采纳问题(概览表, 只读) ----------
            all_df = load_all_issues()

            def _is_rejected_cell(v):
                try:
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        return False
                    return int(v) == 0
                except (TypeError, ValueError):
                    return False

            if "is_adopted" in all_df.columns:
                rejected_df = all_df[all_df["is_adopted"].apply(_is_rejected_cell)]
            else:
                rejected_df = all_df.iloc[0:0]

            # 评审时间 = 该问题最新一条审核记录的时间(不采纳决策留下的时间戳)
            rej_reviews = load_all_reviews()
            latest_review_at = {}
            if not rej_reviews.empty:
                for iid, grp in rej_reviews.groupby("issue_id"):
                    latest_review_at[iid] = grp.sort_values("id").iloc[-1].get("reviewed_at")

            display = pd.DataFrame({
                "序号": rejected_df["序号"].tolist(),  # v0.9: 全局连续序号(与登记页一致)
                "问题描述": rejected_df["problem_description"].tolist(),
                "工位": rejected_df["work_station"].tolist(),
                "发现日期": rejected_df["issue_date"].tolist(),
                "第一责任人": rejected_df["responsible_person_1"].tolist(),
                "第二责任人": rejected_df["responsible_person_2"].tolist(),
                "第三责任人": rejected_df["responsible_person_3"].tolist(),
                "是否采纳": ["不采纳"] * len(rejected_df),
                "评审时间": [latest_review_at.get(iid, "—") for iid in rejected_df["id"]],
            })
            st.dataframe(display, use_container_width=True, hide_index=True)
            st.caption("共 {} 条不采纳问题。第二责任人只可查看; "
                       "第三责任人可在下方对自己名下的问题执行「重新采纳」。".format(len(rejected_df)))

            # ---------- 第三责任人: 重新采纳操作区 ----------
            # 只列出当前操作人担任第三责任人的不采纳问题(评审决策权归该问题的经理)
            target = current_person.strip()
            p3_mask = rejected_df["responsible_person_3"].fillna("").astype(str).str.strip().eq(target)
            mine_df = rejected_df[p3_mask]
            if not mine_df.empty:
                st.markdown("---")
                st.markdown("**可重新采纳的问题（{} 条, 我是第三责任人）**".format(len(mine_df)))
                for _, issue in mine_df.iterrows():
                    issue_id = int(issue["id"])  # 数据库主键(仅写库定位用, 不展示)
                    disp_no = issue.get("序号", issue_id)  # 展示用全局连续序号(v0.9)
                    desc_preview = str(issue.get("problem_description") or "")[:30]
                    col_info, col_btn = st.columns([4, 1])
                    col_info.markdown("序号 **{}** | {} | {}…".format(
                        disp_no, issue.get("work_station") or "未填工位", desc_preview))
                    if col_btn.button("🔁 重新采纳", key="readopt_{}".format(issue_id),
                                      use_container_width=True,
                                      help="二次评审采纳: 该问题重回整改流程"):
                        try:
                            # 备注带【重新采纳】前缀: 图3统计和自动关闭据此排除
                            set_issue_adoption(
                                issue_id, 1, "已评审", current_person,
                                "通过", "【重新采纳】重新进入整改")
                            st.session_state["flash_success"] = (
                                "✅ 问题(序号{}) 二次评审采纳, 重新进入整改流程(状态: 已评审)".format(disp_no))
                            st.rerun()  # 刷新 → 该问题从不采纳列表消失
                        except Exception as e:
                            st.error("重新采纳失败: {}".format(e))

# ============================================================
# 标签页6「个人行动计划」: 跟随侧边栏"当前操作人"
# 使用场景: 早会前, 某个责任人想看"我名下都有哪些问题、卡在哪一层"。
# 展示逻辑与v0.4一致(按三个责任角色分组), 只是人员来源改为侧边栏。
# ============================================================
with tab_plan:
    st.subheader("🎯 个人行动计划")

    # ---------- 未选择操作人: 给出明确提示 ----------
    if not current_person:
        st.warning("请先在左侧选择当前操作人")
    else:
        # 该人作为任一责任人的所有问题(最新在前)
        person_issues = get_issues_by_person(current_person)
        if person_issues.empty:
            st.info("「{}」目前没有关联问题。".format(current_person))
        else:
            st.caption("「{}」共关联 {} 个问题, 按责任角色分组如下:".format(
                current_person, len(person_issues)))

            # ---------- 审核流水分组(用于第一责任人组算当前层级) ----------
            # 与分层审核页用同一套推导规则(compute_current_level), 结果必然一致
            plan_reviews = load_all_reviews()
            plan_reviews_by_issue = {}
            if not plan_reviews.empty:
                for _, row in plan_reviews.iterrows():
                    plan_reviews_by_issue.setdefault(row["issue_id"], []).append(row.to_dict())

            # 层级推导结果 → 显示文字(None=已关闭/不再流转)
            def _level_text(issue_row):
                lv = compute_current_level(
                    plan_reviews_by_issue.get(issue_row["id"], []),
                    issue_row["status"])
                return "已关闭" if lv is None else "第{}层".format(lv)

            # ---------- 按责任角色分三组展示 ----------
            # (组标题, 数据库字段, 是否额外显示当前层级)
            _GROUPS = [
                ("作为第一责任人的问题", "responsible_person_1", True),
                ("作为第二责任人的问题", "responsible_person_2", False),
                ("作为第三责任人的问题", "responsible_person_3", False),
            ]
            for group_title, col_name, show_level in _GROUPS:
                # 筛选: 该字段值(去首尾空格)与当前操作人姓名完全一致
                col_vals = person_issues[col_name].fillna("").astype(str).str.strip()
                group_df = person_issues[col_vals == current_person.strip()]

                st.markdown("---")
                st.markdown("**{}（{} 条）**".format(group_title, len(group_df)))

                if group_df.empty:
                    st.caption("无")
                else:
                    # 公共展示列: 序号(v0.9全局连续编号) / 问题描述 / 工位 / 当前状态 / 创建时间
                    display = pd.DataFrame({
                        "序号": group_df["序号"].tolist(),
                        "问题描述": group_df["problem_description"].tolist(),
                        "工位": group_df["work_station"].tolist(),
                        "当前状态": group_df["status"].tolist(),
                        "创建时间": group_df["created_at"].tolist(),
                    })
                    # 第一责任人组: 额外显示当前层级(排在"当前状态"之后)
                    # 第1层=待整改 / 第2层=待工程师验收 / 第3层=待经理关闭 / 已关闭
                    if show_level:
                        display.insert(
                            4, "当前层级",
                            group_df.apply(_level_text, axis=1).tolist())
                    st.dataframe(display, use_container_width=True, hide_index=True)

# ============================================================
# 标签页7「分析看板」: 四张图表(v0.6起)
# 数据全部来自已缓存的 issues / issue_reviews 查询, 不额外连库;
# 某张图数据不足时显示"暂无数据", 不报错。
# ============================================================
with tab_dashboard:
    st.subheader("📊 分析看板")
    st.caption("口径说明: 图1按问题发现日期的月份统计; 图2统计第二责任人验收"
               "'通过'次数(按审核人汇总); 图3统计第三责任人真实验收'通过'次数"
               "(已排除「问题评审」的采纳/重新采纳记录, 评审打勾不算验收打勾); "
               "图4A只统计'采纳'的问题; 图4B只统计已评审决策过的问题"
               "(采纳+不采纳, 待评审不参与)。"
               "(注: 早期版本审核人存的是角色名, 新记录存真实姓名, 图2/图3会混合显示)")

    dash_issues = load_all_issues()
    dash_reviews = load_all_reviews()

    if dash_issues.empty:
        st.info("暂无问题数据, 无法生成图表。请先在「问题登记」页登记问题。")
    else:
        dfc = dash_issues.copy()
        # 月份 = issue_date(格式YYYY-MM-DD)取前7位 → "YYYY-MM"; 空日期记为""
        dfc["月份"] = dfc["issue_date"].fillna("").astype(str).str.slice(0, 7)

        # ---------- 图1: 月度趋势组合图(柱=拒收, 线=过程改善) ----------
        st.markdown("**图1 · 月度趋势**（柱: 拒收数量[状态=已退回] | 线: 过程改善数量[状态=已评审]）")
        month_raw = sorted(dfc.loc[dfc["月份"] != "", "月份"].unique().tolist())
        if not month_raw:
            st.caption("暂无数据(所有问题都没有填写发现日期)")
        else:
            # 生成连续月份序列: 中间没有问题的月份也补上(计0), 趋势线不断档
            def _month_range(start_ym, end_ym):
                y, m = int(start_ym[:4]), int(start_ym[5:7])
                out = []
                while True:
                    cur = "{:04d}-{:02d}".format(y, m)
                    out.append(cur)
                    if cur == end_ym:
                        return out
                    m += 1
                    if m > 12:
                        m, y = 1, y + 1

            months = _month_range(month_raw[0], month_raw[-1])
            bar_vals, line_vals = [], []
            for m in months:
                mdf = dfc[dfc["月份"] == m]
                bar_vals.append(int((mdf["status"] == "已退回").sum()))
                line_vals.append(int((mdf["status"] == "已评审").sum()))

            fig1 = go.Figure()
            fig1.add_trace(go.Bar(
                x=months, y=bar_vals, name="拒收数量(已退回)",
                marker_color="#d62728"))
            fig1.add_trace(go.Scatter(
                x=months, y=line_vals, name="过程改善数量(已评审)",
                mode="lines+markers", line=dict(color="#1f77b4", width=2)))
            fig1.update_layout(
                xaxis_title="月份", yaxis_title="问题数量", height=380,
                legend=dict(orientation="h", y=1.12),
                margin=dict(l=10, r=10, t=30, b=10))
            st.plotly_chart(fig1, use_container_width=True, config=_PLOTLY_CONFIG)

        # ---------- 图2 / 图3: 第二、第三责任人通过次数(并排) ----------
        st.markdown("---")

        def _pass_counts(level):
            """统计指定层级审核'通过'次数(按审核人分组); 无数据返回None。
            v0.9: 第3层改用 get_real_level3_pass_records —— 排除「问题评审」
            的采纳/重新采纳记录(评审打勾≠验收打勾), 修复图3数量虚高。"""
            if level == 3:
                sub = get_real_level3_pass_records()
            else:
                if dash_reviews.empty:
                    return None
                mask = (dash_reviews["review_level"] == level) & (dash_reviews["review_result"] == "通过")
                sub = dash_reviews[mask]
            if sub is None or sub.empty:
                return None
            return sub["reviewer"].value_counts()

        col_fig2, col_fig3 = st.columns(2)
        with col_fig2:
            st.markdown("**图2 · 第二责任人验收通过统计**")
            c2 = _pass_counts(2)
            if c2 is None:
                st.caption("暂无数据")
            else:
                fig2 = go.Figure(go.Bar(
                    x=c2.index.tolist(), y=c2.values.tolist(),
                    marker_color="#1f77b4"))
                fig2.update_layout(
                    xaxis_title="第二责任人", yaxis_title="通过次数",
                    height=340, margin=dict(l=10, r=10, t=20, b=10))
                st.plotly_chart(fig2, use_container_width=True, config=_PLOTLY_CONFIG)
        with col_fig3:
            st.markdown("**图3 · 第三责任人最终确认通过统计**")
            c3 = _pass_counts(3)
            if c3 is None:
                st.caption("暂无数据")
            else:
                fig3 = go.Figure(go.Bar(
                    x=c3.index.tolist(), y=c3.values.tolist(),
                    marker_color="#2ca02c"))
                fig3.update_layout(
                    xaxis_title="第三责任人", yaxis_title="通过次数",
                    height=340, margin=dict(l=10, r=10, t=20, b=10))
                st.plotly_chart(fig3, use_container_width=True, config=_PLOTLY_CONFIG)

        # ---------- 图4: 两个饼图并排(完成率 + 采纳率) ----------
        st.markdown("---")
        col_pieA, col_pieB = st.columns(2)
        with col_pieA:
            st.markdown("**图4A · 采纳问题的完成率**（已完成=已关闭）")
            # 只统计采纳的问题(is_adopted=1)
            if "is_adopted" in dfc.columns:
                adopted = dfc[dfc["is_adopted"] == 1]
            else:
                adopted = dfc.iloc[0:0]
            done_n = int((adopted["status"] == "已关闭").sum())
            undone_n = len(adopted) - done_n
            if done_n + undone_n == 0:
                st.caption("暂无数据(还没有'采纳'的问题)")
            else:
                figA = go.Figure(go.Pie(
                    labels=["已完成(已关闭)", "未完成"],
                    values=[done_n, undone_n], hole=0.4,
                    marker=dict(colors=["#2ca02c", "#ff7f0e"]),
                    textinfo="label+percent"))
                figA.update_layout(height=340, margin=dict(l=10, r=10, t=20, b=10))
                st.plotly_chart(figA, use_container_width=True, config=_PLOTLY_CONFIG)
        with col_pieB:
            st.markdown("**图4B · 问题采纳率**（已评审的问题, 待评审不参与）")
            # v0.8: 只统计已决策过的问题 —— NULL(待评审)既不等于1也不等于0,
            # 天然被两个条件排除在外, 无需额外过滤
            if "is_adopted" in dfc.columns:
                adopt_n = int((dfc["is_adopted"] == 1).sum())
                reject_n = int((dfc["is_adopted"] == 0).sum())
            else:
                adopt_n, reject_n = 0, 0
            if adopt_n + reject_n == 0:
                st.caption("暂无数据(还没有'采纳'或'不采纳'的评审决策)")
            else:
                figB = go.Figure(go.Pie(
                    labels=["采纳", "不采纳"],
                    values=[adopt_n, reject_n], hole=0.4,
                    marker=dict(colors=["#1f77b4", "#7f7f7f"]),
                    textinfo="label+percent"))
                figB.update_layout(height=340, margin=dict(l=10, r=10, t=20, b=10))
                st.plotly_chart(figB, use_container_width=True, config=_PLOTLY_CONFIG)
