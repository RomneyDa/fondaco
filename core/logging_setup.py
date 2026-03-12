"""Centralised logging configuration.

Provides a single ``setup_logging`` function that every entry-point should
call instead of manually configuring handlers.  Each caller gets:

* A ``RotatingFileHandler`` (10 MB, 3 backups) at **DEBUG** level.
* An optional ``StreamHandler`` on *stderr* at **INFO** level.

Usage::

    from core.logging_setup import setup_logging
    logger = setup_logging("app", log_file="data/app.log")
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
    """Configure and return a logger with rotating file + optional stderr.

    Parameters
    ----------
    name:
        Logger name (also used as the returned logger's name).
    log_file:
        Path to the log file.  If *log_dir* is given and *log_file* is not
        absolute, it is joined with *log_dir*.
    log_dir:
        Optional directory to prepend to *log_file*.  Created if it does not
        exist.
    include_stderr:
        When ``True`` (default), add a ``StreamHandler`` on *stderr* at INFO
        level.  Set to ``False`` for worker processes whose stdout/stderr is
        already redirected to a file.

    Returns
    -------
    logging.Logger
        A fully-configured logger ready to use.
    """
    if log_dir is not None and not os.path.isabs(log_file):
        log_file = os.path.join(log_dir, log_file)

    # Ensure the directory for the log file exists.
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # Build the handler list.
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

    # Configure the root logger so libraries that use ``logging.getLogger()``
    # without a name still go through these handlers.
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Remove any previously-installed handlers (e.g. from basicConfig).
    root.handlers = list(handlers)

    return logging.getLogger(name)
