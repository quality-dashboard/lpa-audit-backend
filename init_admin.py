"""
init_admin.py - 初始化默认管理员账号 (v2.5)

用法:
    python init_admin.py

功能:
    1. 建表(幂等, 含认证三张表 + measures.skip_level2 回填)
    2. 若 accounts 表为空, 创建默认管理员 admin, 随机生成临时密码并打印
    3. admin 首次登录会强制修改密码

说明:
    本脚本从 .streamlit/secrets.toml 读取 Turso token(与 db.py 一致),
    避免依赖 Streamlit 运行环境, 可直接用 python 执行。
"""
import os
import sys

import db  # 复用 db.py 的连接配置与 _execute


def _load_token():
    """从 .streamlit/secrets.toml 读取 LPA_TURSO_TOKEN"""
    secrets_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
    try:
        with open(secrets_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("LPA_TURSO_TOKEN") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def main():
    token = _load_token()
    if token:
        db.TURSO_AUTH_TOKEN = token
    if not db.TURSO_AUTH_TOKEN:
        print("× 未找到 Turso token。请在 .streamlit/secrets.toml 配置 LPA_TURSO_TOKEN")
        sys.exit(1)

    print("→ 初始化数据库(建表 + 迁移, 幂等)...")
    db.init_db()

    created, username, temp_pw = db.init_default_admin()
    if created:
        print("=" * 56)
        print("✓ 已创建默认管理员账号:")
        print("  账号: {}".format(username))
        print("  临时密码: {}".format(temp_pw))
        print("  ★ 请立即保存此密码! 首次登录将强制修改。")
        print("=" * 56)
    else:
        print("✓ accounts 表已有账号, 跳过创建默认管理员。")


if __name__ == "__main__":
    main()
