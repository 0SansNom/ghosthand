import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PROJECT = PROJECT_ROOT / "Runner" / "GhosthandRunner.xcodeproj"

STATE_DIR = Path(os.environ.get("GHOSTHAND_HOME", Path.home() / ".ghosthand"))
DERIVED_DATA = STATE_DIR / "DerivedData"
RUNNERS_DIR = STATE_DIR / "runners"
TRACES_DIR = STATE_DIR / "traces"
