"""
notify.py - 企业微信群机器人通知模块 (v0.6)

设计原则: 通知是"锦上添花", 绝不能影响审核主流程 ——
  - webhook未配置 → 只在后台打印日志, 不发真实请求(当前默认状态)
  - 发送失败/超时 → 打印日志并返回False, 不抛异常
启用方式: 把群机器人Webhook地址填到 config.py 的 WECHAT_WEBHOOK_URL,
代码一行都不用改。
"""
import requests

from config import WECHAT_WEBHOOK_URL


def send_wechat_message(content):
    """发送企业微信群机器人文本消息。
    参数: content - 消息内容(纯文本)
    返回: True=发送成功 / False=未发送或发送失败(主流程不受影响)
    说明: 企业微信群机器人是"群消息"通道, 没有单人定向接口;
          通知对象(如"请张三验收")通过在文本里点名实现。"""
    # 未配置webhook → 框架占位: 只打印日志, 不发真实请求
    if not WECHAT_WEBHOOK_URL:
        print("[通知未发送, webhook未配置] {}".format(content))
        return False
    try:
        resp = requests.post(
            WECHAT_WEBHOOK_URL,
            json={"msgtype": "text", "text": {"content": content}},
            timeout=10,
        )
        if resp.status_code == 200:
            print("[通知已发送] {}".format(content))
            return True
        print("[通知发送失败] HTTP {}: {}".format(resp.status_code, resp.text))
        return False
    except Exception as e:
        # 网络异常等 → 记日志即可, 不让通知问题拖垮审核提交
        print("[通知发送异常] {}".format(e))
        return False
