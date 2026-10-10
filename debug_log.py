"""Structured, local diagnostics for MAX parsing and Telegram delivery."""

import logging
import os
import re
from logging.handlers import RotatingFileHandler


_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "max_parser_debug.log")
logger = logging.getLogger("max_parser")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(logging.CRITICAL + 1)
    logger.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            _LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("LOG_FILE_UNAVAILABLE path=%s error=%s", _LOG_PATH, exc)


_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_TOKEN_RE = re.compile(r"(?i)bot\d{5,}:[A-Za-z0-9_-]{20,}")
_QUERY_RE = re.compile(r"([?&][^=\s]+)=([^&\s]+)")


def preview(value, limit=180):
    """Return a one-line log preview with phone-like strings masked."""
    text = " ".join(str(value or "").replace("\x00", "").split())
    text = _PHONE_RE.sub("[номер скрыт]", text)
    text = _TOKEN_RE.sub("[telegram token скрыт]", text)
    text = _QUERY_RE.sub(r"\1=[скрыто]", text)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text
