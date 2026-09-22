"""入口：python -m workbuddy_one"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .config import config
from .logsetup import parse_level


def main():
    parser = argparse.ArgumentParser(prog="workbuddy_one", description="WorkBuddy API 网关")
    parser.add_argument("--host", default=config.host)
    parser.add_argument("--port", type=int, default=config.port)
    parser.add_argument("--login", action="store_true",
                        help="通过浏览器 OAuth 登录 WorkBuddy，登录后落盘为 auth 文件")
    parser.add_argument("--no-browser", action="store_true",
                        help="登录时不自动打开浏览器（打印授权 URL 手动打开）")
    parser.add_argument("--region", choices=("cn", "global"), default="cn",
                        help="登录哪个版本：cn=国内版（默认），global=国际版")
    parser.add_argument("--log-level", default=None,
                        help="日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL），"
                             "默认取环境变量 LOG_LEVEL，都没有则 WARNING")
    args = parser.parse_args()

    if args.login:
        from .oauth import oauth_login
        from .config import config as cfg
        oauth_login(open_browser=not args.no_browser, output_dir=cfg.auth_dir_path or None,
                    region_id=args.region)
        return

    # CLI 显式给的级别要覆盖环境变量：create_app 内部读的是 config.log_level，
    # 所以这里先落到 config 上，再由它统一调用 setup_logging（幂等）。
    if args.log_level:
        config.log_level = args.log_level

    app = create_app()
    # create_app 里已经按 config.log_level 配好了；这里只是把**生效值**打出来，
    # 用 parse_level 复算而不是再调一次 setup_logging（免得靠副作用拿返回值）。
    level_name = logging.getLevelName(parse_level(config.log_level)[0])
    print(f"WorkBuddy One 启动: http://{args.host}:{args.port}")
    print(f"  数据库: {config.db_path}")
    print("  模式: 单用户一体化（管理端点无需 Admin Token）")
    print("  API Key: 请在 WebUI「应用」页创建（每个应用独立 Key，便于用量归因）")
    # 明写这一行：默认档位下那 30 处 logger.info 是不输出的，用户得知道怎么打开
    print(f"  日志级别: {level_name}"
          + ("" if level_name != "WARNING" else "（要看得设 LOG_LEVEL=INFO）"))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
