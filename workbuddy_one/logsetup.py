"""日志级别配置（`LOG_LEVEL`）。

为什么需要单独一个模块：项目里 30 处 `logger.info(...)` 在默认配置下**永远不会输出**
——Python 根 logger 默认 WARNING，而全项目没有任何 `basicConfig` / `setLevel`。
于是"档位为什么被降级""max_tokens 为什么被裁剪"这类只能靠 INFO 记录观察的行为，
排查时既看不到日志、也没有开关，只能改代码加 print（2026-09-21 实测踩过）。

默认**保持 WARNING**，不改变既有输出；要看得显式设 `LOG_LEVEL=INFO`（或用 `--log-level`）。
"""
from __future__ import annotations

import logging
import sys

# 只配置这个命名空间下的 logger（各模块都是 workbuddy_one.* 的子 logger）。
# 刻意不动根 logger：uvicorn 自己会配置 root / uvicorn.* 的 handler，
# 插手会和它的输出格式打架（重复行 / 格式突变）。
ROOT_NAME = "workbuddy_one"

DEFAULT_LEVEL = "WARNING"

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

_configured = False


def parse_level(level: str | None) -> tuple[int, bool]:
    """把级别名解析成 logging 常量，返回 (级别, 是否合法)。

    合法形式：`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`（大小写不敏感，
    也接受 `WARN`）以及纯数字（如 `20`）。空值 = 用默认档（视为合法，不告警）。
    非法值**不抛异常**，回落到 WARNING 并由调用方告警——启动期因为一个环境变量
    拼错就崩掉，比降级严重得多。
    """
    name = str(level or "").strip().upper()
    if not name:
        return logging.WARNING, True
    if name.isdigit():
        return int(name), True
    value = getattr(logging, name, None)
    if isinstance(value, int):
        return value, True
    return logging.WARNING, False


def setup_logging(level: str | None = None) -> int:
    """配置 `workbuddy_one` 命名空间的日志级别与输出格式，返回实际生效的级别。

    幂等：重复调用只更新级别，不会叠加 handler（否则每调一次就多打一行）。
    """
    global _configured
    logger = logging.getLogger(ROOT_NAME)
    resolved, valid = parse_level(level)

    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        logger.addHandler(handler)
        # propagate=False：否则 WARNING 及以上会同时走根 logger 的 lastResort handler，
        # 同一行打两遍（一遍带格式、一遍裸消息）。
        logger.propagate = False
        _configured = True

    logger.setLevel(resolved)
    if not valid:
        logger.warning("LOG_LEVEL=%r 不是合法级别，已回落到 %s", level,
                       logging.getLevelName(resolved))
    return resolved


def reset_for_tests() -> None:
    """仅供测试：把"已配置"标记清掉，让下一条用例能重新观察 handler 装配行为。"""
    global _configured
    logger = logging.getLogger(ROOT_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.propagate = True
    _configured = False
