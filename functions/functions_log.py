"""DEBUG_MODE logging. With DEBUG_MODE off nothing is written and the console is quiet."""

from __future__ import annotations

import logging

from . import functions_config as cfg

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("time_lock")
    logger.propagate = False
    logger.handlers.clear()
    if cfg.DEBUG_MODE:
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(cfg.LOG_DIR / "time_lock.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    else:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
    _logger = logger
    return logger


def event(message: str, *args) -> None:
    get_logger().info(message, *args)


def failure(message: str, *args) -> None:
    get_logger().warning(message, *args)


class _SkipNoise(logging.Filter):
    """Keep the log file useful: no page polling and no static file requests."""

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        return not ("GET /api/items HTTP" in text or "GET /assets/" in text or "GET /api/browse" in text)


def quiet_console() -> None:
    """Stop the web server printing a line for every request to the terminal.

    Requests never reach the console. With DEBUG_MODE on, the interesting ones go to the
    log file; with it off, nothing is written anywhere. Real errors still show."""
    try:
        import flask.cli
        flask.cli.show_server_banner = lambda *args, **kwargs: None
    except ImportError:
        pass
    web = logging.getLogger("werkzeug")
    web.handlers.clear()
    web.propagate = False
    if cfg.DEBUG_MODE:
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(cfg.LOG_DIR / "time_lock.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s web: %(message)s"))
        handler.addFilter(_SkipNoise())
        web.addHandler(handler)
        web.setLevel(logging.INFO)
    else:
        web.addHandler(logging.NullHandler())
        web.setLevel(logging.CRITICAL + 1)
