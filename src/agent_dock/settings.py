"""User preferences, stored as JSON. Unknown keys are ignored; bad values fall back to defaults."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)


@dataclass
class Settings:
    # Broker: how long a request waits for a dock answer before the agent's own
    # prompt takes over. Must stay below the hook timeout written into agent config.
    request_timeout_seconds: float = 45.0
    # Presentation timings (seconds).
    expanded_seconds: float = 10.0
    visible_seconds: float = 30.0
    hover_leave_seconds: float = 1.0
    # Which display hosts the drop-down: "active" (screen with key window),
    # "mouse" (screen under the pointer) or "primary" (menu-bar screen).
    display: str = "active"
    # Show only the badge, never the floating panel.
    badge_only: bool = False
    # Tools that are always handed back to the agent's native UI because a plain
    # allow/deny is not a meaningful answer (they need structured updatedInput).
    passthrough_tools: list[str] = field(default_factory=lambda: ["AskUserQuestion"])
    # Hide command text until hovered/opened (privacy blur).
    sensitive_mode: bool = False
    max_queue: int = 100

    @property
    def hook_timeout_seconds(self) -> int:
        """Timeout written into Claude/Codex hook config (agent kills the hook after this)."""
        return int(self.request_timeout_seconds) + 15


def _coerce(default, value):
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, (int, float)):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return type(default)(value)
        return default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, list):
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else default
    return default


def load(path: Path | None = None) -> Settings:
    path = path or paths.settings_path()
    base = Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return base
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable settings file %s: %s", path, exc)
        return base
    if not isinstance(raw, dict):
        return base
    for f in fields(Settings):
        if f.name in raw:
            setattr(base, f.name, _coerce(getattr(base, f.name), raw[f.name]))
    if base.visible_seconds < base.expanded_seconds:
        base.visible_seconds = base.expanded_seconds
    return base


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or paths.settings_path()
    paths.ensure_private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
