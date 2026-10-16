"""固定项目路径；默认配置与产物不依赖进程工作目录。"""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "configs"
DATA_DIR = ROOT_DIR / "data"
LOG_DIR = ROOT_DIR / "logs"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
PLAYERS_PATH = CONFIG_DIR / "players.yaml"
MODELS_PATH = CONFIG_DIR / "models.yaml"
GAME_PATH = CONFIG_DIR / "game.yaml"
CAPABILITIES_PATH = DATA_DIR / "model_capabilities.json"
ENV_PATH = ROOT_DIR / ".env"
TEMPLATE_PATH = ROOT_DIR / "RULES.template.md"
RULES_PATH = ROOT_DIR / "RULES.md"
