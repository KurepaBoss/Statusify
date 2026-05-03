import configparser
from pathlib import Path


def config_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "statusify.cfg"


def history_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "history.json"


def load_config(path: str | Path):
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg


def save_config(cfg, path: str | Path):
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)


def cfg_get(path: str | Path, section: str, key: str, fallback: str = "") -> str:
    cfg = load_config(path)
    return cfg.get(section, key, fallback=fallback)


def cfg_set(path: str | Path, section: str, key: str, value):
    cfg = load_config(path)
    if not cfg.has_section(section):
        cfg.add_section(section)
    cfg.set(section, key, str(value))
    save_config(cfg, path)
