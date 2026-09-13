"""入口：python -m workbuddy_one"""
from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .config import config


def main():
    parser = argparse.ArgumentParser(prog="workbuddy_one", description="WorkBuddy API 网关")
    parser.add_argument("--host", default=config.host)
    parser.add_argument("--port", type=int, default=config.port)
    parser.add_argument("--login", action="store_true",
                        help="通过浏览器 OAuth 登录 WorkBuddy，登录后落盘为 auth 文件")
    parser.add_argument("--no-browser", action="store_true",
                        help="登录时不自动打开浏览器（打印授权 URL 手动打开）")
    args = parser.parse_args()

    if args.login:
        from .oauth import oauth_login
        from .config import config as cfg
        oauth_login(open_browser=not args.no_browser, output_dir=cfg.auth_dir_path or None)
        return

    app = create_app()
    print(f"WorkBuddy One 启动: http://{args.host}:{args.port}")
    print(f"  数据库: {config.db_path}")
    print("  模式: 单用户一体化（管理端点无需 Admin Token）")
    print("  API Key: 请在 WebUI「应用」页创建（每个应用独立 Key，便于用量归因）")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
