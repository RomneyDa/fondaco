"""Session data persistence and collection logging."""

import json
import os
from datetime import datetime, timezone

from defaults import DATA_DIR, LOG_FILE
from models import SessionData


class SessionRecorder:
    """Manages writing session data to disk.

    Sessions are stored under data/sessions/{slug_prefix}/ so different
    market types are kept separate.
    """

    def __init__(self, slug_prefix: str):
        # Strip trailing dash for the folder name (e.g. "btc-updown-5m-" -> "btc-updown-5m")
        self.market_dir = os.path.join(DATA_DIR, slug_prefix.rstrip("-"))
        os.makedirs(self.market_dir, exist_ok=True)
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    def session_filepath(self, window_start_utc: str) -> str:
        """Generate file path for a session. Colons replaced for filesystem safety."""
        safe_name = window_start_utc.replace(":", "-")
        return os.path.join(self.market_dir, f"{safe_name}.json")

    def session_exists(self, window_start_utc: str) -> bool:
        """Check if we already collected this session."""
        return os.path.exists(self.session_filepath(window_start_utc))

    def save_session(self, session: SessionData) -> str:
        """Write session data to a JSON file. Returns the file path."""
        filepath = self.session_filepath(session.metadata.window_start_utc)
        with open(filepath, "w") as f:
            f.write(session.to_json())
        return filepath

    def append_log(self, message: str, **kwargs):
        """Append a line to the collection log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
            **kwargs,
        }
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def count_completed_sessions(self) -> int:
        """Count how many session files exist for this market type."""
        if not os.path.exists(self.market_dir):
            return 0
        return len([f for f in os.listdir(self.market_dir) if f.endswith(".json")])
