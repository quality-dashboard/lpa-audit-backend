auto_close.py - 问题自动关闭脚本 (v0.6)

功能: 定时检查, 把满足条件的问题自动置为"已关闭":
  条件: 该问题在 issue_reviews 表中, 第三责任人(review_level=3)的"通过"
        记录至少有两条, 且审核时间(reviewed_at)分布在两个不同的自然月
        —— 连续两个自然月各确认通过一次, 视为改善有效, 自动闭环。
        (v0.9: "通过"记录已排除「问题评审」的采纳/重新采纳类 ——
         评审打勾不算验收, 只统计真正的第3层验收通过)

运行方式:
  1) 常驻运行(推荐): 在项目目录执行  python auto_close.py
     启动后立即检查一次(方便当天验证), 之后每天 06:00 自动检查一次,
     Ctrl+C 退出。
  2) 交给 Windows 任务计划程序 / 服务器 cron: 每天拉起一次本脚本进程
     也可以, 但需要删掉文件底部的 while 定时循环(脚本幂等,
     重复执行无副作用 —— 已关闭的问题不会被重复处理)。

依赖: 复用 db.py 的 Turso 连接配置和HTTP执行函数, 不依赖Streamlit
     运行环境(直接 python 运行即可); 请在装好 requirements.txt 依赖的
     虚拟环境里运行。
"""
import time
from datetime import datetime

import schedule

import db  # 复用 db.py 的连接配置和 _execute(只import, 不涉及任何页面逻辑)
from db import _execute, get_real_level3_pass_records

# ------------------------------------------------------------
# 配置覆盖区(可选): 如果 Token 配在 .streamlit/secrets.toml 里,
# 裸Python脚本读不到, 可以在这里直接粘贴一份;
# 留空(默认)则使用 db.py 顶部配置区的值。
# ------------------------------------------------------------
URL_OVERRIDE = ""
TOKEN_OVERRIDE = ""

if URL_OVERRIDE:
    db.TURSO_URL = URL_OVERRIDE
if TOKEN_OVERRIDE:
    db.TURSO_AUTH_TOKEN = TOKEN_OVERRIDE


def _config_ok():
    """检查Turso连接配置是否可用; 不可用时打印清晰的填写指引后退出。"""
    if "在这里填" in db.TURSO_URL or not db.TURSO_AUTH_TOKEN:
        print("=" * 56)
        print("× Turso 连接未配置, 脚本退出。请任选其一:")
        print("  1) 在 db.py 顶部配置区填写 TURSO_URL 和 TURSO_AUTH_TOKEN")
        print("  2) 在本文件顶部 URL_OVERRIDE / TOKEN_OVERRIDE 直接填写")
        print("=" * 56)
        return False
    return True


def auto_close_issues():
    """执行一次自动关闭检查, 返回本次自动关闭的问题数量。
    v0.9: 第3层"通过"记录改用 db.get_real_level3_pass_records 获取 ——
    已在函数内排除「问题评审」的采纳/重新采纳类记录(评审打勾≠验收通过),
    避免评审记录被误判为跨月验收导致误关闭。"""
    # 1) 一次性拉取全部"真验收通过"记录并按问题分组
    #    (比逐问题查SQL少N-1次网络往返; 评审类记录已在函数内排除)
    pass_df = get_real_level3_pass_records()
    pass_dates_by_issue = {}
    if pass_df is not None and not pass_df.empty:
        for iid, grp in pass_df.groupby("issue_id"):
            pass_dates_by_issue[iid] = grp.sort_values("reviewed_at")["reviewed_at"].tolist()

    # 2) 查所有未关闭的问题(只取id和描述, 描述用于日志)
    result = _execute(
        "SELECT id, problem_description FROM issues WHERE status != '已关闭'")
    total = len(result.rows)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if total == 0:
        print("[{}] 当前没有未关闭的问题。".format(ts))
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    closed = 0
    for issue_id, desc in result.rows:
        # 3) 该问题的真验收通过时间列表 → 提取年月("YYYY-MM");
        #    出现过两个不同自然月即满足条件(同月通过两次不算, 必须跨月持续确认)
        year_months = set()
        for reviewed_at in pass_dates_by_issue.get(issue_id, []):
            ym = str(reviewed_at or "")[:7]  # "2026-08-21 10:00:00" → "2026-08"
            if len(ym) == 7:
                year_months.add(ym)
        if len(year_months) >= 2:
            # 4) 满足条件 → 直接UPDATE关闭
            #    (不走 db.update_issue_status, 避免触碰Streamlit缓存机制)
            _execute(
                "UPDATE issues SET status = '已关闭', updated_at = ? WHERE id = ?",
                [now, issue_id])
            closed += 1
            print("[{}] 问题#{} 已自动关闭(跨{}两个自然月确认): {}".format(
                ts, issue_id, "、".join(sorted(year_months)),
                str(desc or "")[:40]))
    print("[{}] 检查完成: 扫描 {} 条未关闭问题, 自动关闭 {} 条。".format(
        ts, total, closed))
    return closed


def _job():
    """定时任务入口: 包一层异常捕获, 单次失败不影响后续调度。"""
    print("-" * 56)
    try:
        auto_close_issues()
    except Exception as e:
        print("[{}] 自动关闭检查失败: {}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), e))


if __name__ == "__main__":
    if not _config_ok():
        raise SystemExit(1)
    print("LPA 自动关闭脚本启动 (Ctrl+C 退出)")
    _job()  # 启动时先立即执行一次, 上线当天就能看到检查效果
    schedule.every().day.at("06:00").do(_job)  # 之后每天早上6点各检查一次
    print("定时任务已挂载: 每天 06:00 检查一次, 等待中...")
    while True:
        schedule.run_pending()
        time.sleep(60)
