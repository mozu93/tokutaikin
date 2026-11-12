"""更新の確認と適用。

GitHub の Release から最新の版を調べ、新しければ、インストーラーを取得して実行する。
安全のため、取得先はこのリポジトリの Release に限り、インストーラーは、Release に添付した
SHA-256 の値と一致したものだけを実行する。
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from . import __version__

log = logging.getLogger(__name__)

REPO = "mozu93/tokutaikin"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"  # 取得できるURLは、ここで始まるものだけ
INSTALLER_RE = re.compile(r"^TokutaiApp-Setup-(\d+\.\d+\.\d+)\.exe$")
MAX_INSTALLER_BYTES = 300 * 1024 * 1024  # これより大きいものは取得しない
TIMEOUT = 30


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str  # 例 "1.1.0"
    notes: str  # Release の説明文（変更点）
    installer_name: str
    installer_url: str
    sha256_url: str


def parse_version(text: str) -> tuple[int, ...] | None:
    """「v1.2.3」「1.2.3」を (1, 2, 3) にする。読めなければ None。"""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(latest: str, current: str = __version__) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def is_installed_build() -> bool:
    """インストールした exe として動いているか（ソースから動かしているときは False）。"""
    return bool(getattr(sys, "frozen", False))


def parse_release(data: dict, current: str = __version__) -> UpdateInfo | None:
    """Release の情報から、今の版より新しく、インストーラーとSHA-256が揃っているものを返す。"""
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name", ""))
    ver = parse_version(tag)
    if ver is None or not is_newer(tag, current):
        return None
    version = ".".join(map(str, ver))
    assets = {a.get("name"): a.get("browser_download_url", "") for a in data.get("assets", [])}
    name = f"TokutaiApp-Setup-{version}.exe"
    url, sha_url = assets.get(name, ""), assets.get(name + ".sha256", "")
    if not (url and sha_url):
        log.warning("新しい版 %s は見つかったが、インストーラーまたは SHA-256 が添付されていない", version)
        return None
    for u in (url, sha_url):
        if not u.startswith(DOWNLOAD_PREFIX):
            log.warning("取得先がこのリポジトリの Release ではないため、無視する: %s", u)
            return None
    return UpdateInfo(version, str(data.get("body") or ""), name, url, sha_url)


def check_latest(current: str = __version__) -> UpdateInfo | None:
    """最新の Release を確認する。新しい版があれば UpdateInfo、なければ None。通信に失敗したら UpdateError。"""
    try:
        r = requests.get(LATEST_URL, headers={"Accept": "application/vnd.github+json"}, timeout=TIMEOUT)
        r.raise_for_status()
        return parse_release(r.json(), current)
    except (requests.RequestException, ValueError) as e:
        raise UpdateError(f"更新の確認に失敗しました: {e}") from e


def parse_sha256(text: str) -> str:
    """SHA-256 のファイル（「<ハッシュ>  <ファイル名>」の形）から、ハッシュ値を取り出す。"""
    m = re.match(r"\s*([0-9a-fA-F]{64})\b", text or "")
    if not m:
        raise UpdateError("SHA-256 の値を読み取れません。")
    return m.group(1).lower()


def download_installer(info: UpdateInfo, dest_dir: Path) -> Path:
    """インストーラーを取得し、SHA-256 が一致したものだけを返す。一致しなければ削除して UpdateError。"""
    for u in (info.installer_url, info.sha256_url):
        if not u.startswith(DOWNLOAD_PREFIX):
            raise UpdateError("取得先が、このアプリの Release ではありません。")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / info.installer_name
    try:
        r = requests.get(info.sha256_url, timeout=TIMEOUT)
        r.raise_for_status()
        expected = parse_sha256(r.text)
        h, size = hashlib.sha256(), 0
        with requests.get(info.installer_url, stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(1024 * 256):
                    size += len(chunk)
                    if size > MAX_INSTALLER_BYTES:
                        raise UpdateError("インストーラーが大きすぎるため、中止しました。")
                    h.update(chunk)
                    f.write(chunk)
    except requests.RequestException as e:
        dest.unlink(missing_ok=True)
        raise UpdateError(f"インストーラーを取得できませんでした: {e}") from e
    except UpdateError:
        dest.unlink(missing_ok=True)
        raise
    if h.hexdigest() != expected:
        dest.unlink(missing_ok=True)
        raise UpdateError("インストーラーの SHA-256 が一致しないため、実行しません。")
    return dest


def launch_installer(path: Path) -> None:
    """インストーラーを、アプリとは別のプロセスで実行する（呼び出し側が、このあとアプリを終了する）。"""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(path), "/SILENT", "/CLOSEAPPLICATIONS", "/SUPPRESSMSGBOXES", "/NORESTART"],
                     creationflags=flags, close_fds=True)
