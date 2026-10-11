"""Lab-wide config loading. Keeps model/provider details out of the agent
loop, tools, and harness so swapping them only requires editing config.yaml"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    config_path = ROOT / "config.yaml"
    with config_path.open() as f:
        return yaml.safe_load(f)
