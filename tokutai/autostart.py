"""Windows スタートアップへの登録（HKCU\\...\\Run）。"""
from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "TokutaiShoribore"


def _command() -> str:
    """スタートアップで実行するコマンド。PCの起動時は、画面を表示する（最小化の引数は付けない）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    interpreter = pyw if pyw.exists() else exe
    main = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{interpreter}" "{main}"'


def refresh() -> bool:
    """登録済みのコマンドが古い（--minimized 付き）ときに、今の形へ直す。直したら True。

    インストール版（exe）でだけ行う。ソースから動かしたときに、インストール版の登録を書き換えないため。
    """
    if not getattr(sys, "frozen", False):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
            cur, _ = winreg.QueryValueEx(k, VALUE_NAME)
            if cur == _command():
                return False
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, _command())
            return True
    except (ImportError, OSError):  # 未登録（オフにしている）ときは、何もしない
        return False


def is_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
            return True
    except (ImportError, OSError):
        return False


def set_enabled(enabled: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(k, VALUE_NAME)
            except FileNotFoundError:
                pass
