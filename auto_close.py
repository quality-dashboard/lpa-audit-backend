"""
auto_close.py - 问题自动关闭脚本 (v2.0 已退役)

v2.0 措施级重构说明:
  旧版(v0.x)的问题自动关闭依赖"第三责任人跨两个自然月各通过一次"的
  定时扫描逻辑, 通过 db.get_real_level3_pass_records 实现。

  v2.0 起, 问题关闭逻辑改为【措施状态实时汇总】:
    当某问题下所有 is_adopted=1 的措施 status 都变成"已关闭"时,
    在每次措施审核/评审提交后立即由 db.recompute_issue_status 更新
    issues.status = '已关闭', 不再需要任何定时脚本。

  因此本脚本不再承担任何实际工作, 仅保留一个占位入口, 避免已在
  Windows 任务计划 / 服务器 cron 中挂载的旧任务因模块缺失而报错。
  如果你仍挂着定时任务, 可直接删除该任务, 不影响任何功能。
"""
from datetime import datetime


def auto_close_issues():
    """占位实现: v2.0 下问题关闭已由实时汇总完成, 这里只打印说明。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{}] v2.0: 问题关闭已改为措施状态实时汇总, 本脚本不再需要处理。".format(ts))
    return 0


if __name__ == "__main__":
    auto_close_issues()
