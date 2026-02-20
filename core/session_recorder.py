"""Session data persistence and collection logging."""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.defaults import DATA_DIR, LIVE_DIR, LOG_FILE
from core.models import SessionData


class SessionRecorder:
    """Manages writing session data to disk.

    Sessions are stored under data/sessions/{slug_prefix}/ so different
    market types are kept separate.
    """

    def __init__(self, slug_prefix):
        # type: (str) -> None
        # Strip trailing dash for the folder name (e.g. "btc-updown-5m-" -> "btc-updown-5m")
        self.slug_key = slug_prefix.rstrip("-")
        self.market_dir = os.path.join(DATA_DIR, self.slug_key)
        self.live_filepath = os.path.join(LIVE_DIR, "{}.json".format(self.slug_key))
        os.makedirs(self.market_dir, exist_ok=True)
        os.makedirs(LIVE_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    def session_filepath(self, window_start_utc):
        # type: (str) -> str
        """Generate file path for a session. Colons replaced for filesystem safety."""
        safe_name = window_start_utc.replace(":", "-")
        return os.path.join(self.market_dir, "{}.json".format(safe_name))

    def session_exists(self, window_start_utc):
        # type: (str) -> bool
        """Check if we already collected this session."""
        return os.path.exists(self.session_filepath(window_start_utc))

    def save_session(self, session):
        # type: (SessionData) -> str
        """Write session data to a JSON file. Returns the file path."""
        filepath = self.session_filepath(session.metadata.window_start_utc)
        with open(filepath, "w") as f:
            f.write(session.to_json())
        return filepath

    def append_log(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Append a line to the collection log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        entry.update(kwargs)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def write_live_status(self, status, next_window_utc=None, trading_info=None):
        # type: (str, Optional[str], Optional[Dict[str, Any]]) -> None
        """Write a status-only live file so the dashboard knows a process is active.

        Args:
            status: e.g. "waiting", "collecting", "resolving"
            next_window_utc: ISO timestamp of next window start
            trading_info: optional dict merged under "trading" key
        """
        entry = {
            "status": status,
            "slug_key": self.slug_key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "next_window_utc": next_window_utc,
        }  # type: Dict[str, Any]
        if trading_info is not None:
            entry["trading"] = trading_info
        with open(self.live_filepath, "w") as f:
            json.dump(entry, f, indent=2)

    def write_live_session(self, session, trading_info=None):
        # type: (SessionData, Optional[Dict[str, Any]]) -> None
        """Write in-progress session data to the live directory for dashboard consumption.

        Args:
            session: current session data
            trading_info: optional dict merged under "trading" key
        """
        data = json.loads(session.to_json())
        if trading_info is not None:
            data["trading"] = trading_info
        with open(self.live_filepath, "w") as f:
            json.dump(data, f, indent=2)

    def clear_live_session(self):
        # type: () -> None
        """Remove the live session file after the session completes."""
        if os.path.exists(self.live_filepath):
            os.remove(self.live_filepath)

    def count_completed_sessions(self):
        # type: () -> int
        """Count how many session files exist for this market type."""
        if not os.path.exists(self.market_dir):
            return 0
        return len([f for f in os.listdir(self.market_dir) if f.endswith(".json")])
