"""Centralised logging configuration.

Self-contained module (no dependency on core/).  Provides rotating file
handler + optional stderr output.
"""

import logging
import logging.handlers
import os
from typing import Optional


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def setup_logging(
    name,            # type: str
    log_file,        # type: str
    log_dir=None,    # type: Optional[str]
    include_stderr=True,  # type: bool
):
    # type: (...) -> logging.Logger
    """Configure and return a logger with rotating file + optional stderr."""
    if log_dir is not None and not os.path.isabs(log_file):
        log_file = os.path.join(log_dir, log_file)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    handlers = []  # type: list

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=3,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handlers.append(file_handler)

    if include_stderr:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handlers.append(stream_handler)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = list(handlers)

    return logging.getLogger(name)
