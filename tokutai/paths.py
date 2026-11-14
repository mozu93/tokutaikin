"""データ保存先のパス管理。環境変数 TOKUTAI_HOME で上書きできる。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("TOKUTAI_HOME")
    if env:
        base = Path(env)
    else:
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / "TokutaiApp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def settings_path() -> Path:
    return data_dir() / "settings.json"


def db_path() -> Path:
    return data_dir() / "tokutai.db"


def token_cache_path() -> Path:
    return data_dir() / "token_cache.bin"


def log_path() -> Path:
    return data_dir() / "tokutai.log"


def backup_dir() -> Path:
    p = data_dir() / "backup"
    p.mkdir(parents=True, exist_ok=True)
    return p


def abbr_path() -> Path:
    return data_dir() / "corporate_abbr.csv"


def bundled_abbr_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    if hasattr(sys, "_MEIPASS"):
        return base / "tokutai" / "data" / "corporate_abbr.csv"
    return base / "data" / "corporate_abbr.csv"


def app_icon_path() -> Path:
    """アプリのアイコン（exe 化したときも、同梱のデータから探す）。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    if hasattr(sys, "_MEIPASS"):
        return base / "tokutai" / "data" / "app.ico"
    return base / "data" / "app.ico"


def manual_path() -> Path:
    """操作マニュアル。インストール版は exe と同じフォルダ、ソースから動かすときはリポジトリの直下。"""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys.executable).parent / "MANUAL.md"
    return Path(__file__).parent.parent / "MANUAL.md"


def ensure_abbr_file() -> Path:
    """法人略称表を利用者フォルダにコピーする（後から編集できるようにするため）。"""
    dst = abbr_path()
    if not dst.exists():
        shutil.copyfile(bundled_abbr_path(), dst)
    return dst
