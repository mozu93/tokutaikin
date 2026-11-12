import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokutai import updater  # noqa: E402

BASE = updater.DOWNLOAD_PREFIX + "v1.1.0/"
EXE, SHA = "TokutaiApp-Setup-1.1.0.exe", "TokutaiApp-Setup-1.1.0.exe.sha256"


def release(**kw):
    d = {
        "tag_name": "v1.1.0", "draft": False, "prerelease": False, "body": "変更点です",
        "assets": [{"name": EXE, "browser_download_url": BASE + EXE},
                   {"name": SHA, "browser_download_url": BASE + SHA}],
    }
    d.update(kw)
    return d


def test_versions():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.10.0") == (1, 10, 0)
    assert updater.parse_version("latest") is None and updater.parse_version("1.2") is None
    assert updater.is_newer("v1.1.0", "1.0.0") and updater.is_newer("1.10.0", "1.9.0")  # 数として比べる
    assert not updater.is_newer("1.0.0", "1.0.0") and not updater.is_newer("0.9.0", "1.0.0")
    assert not updater.is_newer("???", "1.0.0")


def test_parse_release():
    info = updater.parse_release(release(), "1.0.0")
    assert info and info.version == "1.1.0" and info.notes == "変更点です" and info.installer_name == EXE
    assert updater.parse_release(release(), "1.1.0") is None  # 同じ版
    assert updater.parse_release(release(draft=True), "1.0.0") is None
    assert updater.parse_release(release(prerelease=True), "1.0.0") is None
    assert updater.parse_release(release(assets=[]), "1.0.0") is None  # インストーラーがない
    only_exe = release(assets=[{"name": EXE, "browser_download_url": BASE + EXE}])
    assert updater.parse_release(only_exe, "1.0.0") is None  # SHA-256 がない
    evil = release(assets=[{"name": EXE, "browser_download_url": "https://example.com/" + EXE},
                           {"name": SHA, "browser_download_url": BASE + SHA}])
    assert updater.parse_release(evil, "1.0.0") is None  # 取得先がこのリポジトリではない


def test_parse_sha256():
    h = "a" * 64
    assert updater.parse_sha256(f"{h}  {EXE}\n") == h
    assert updater.parse_sha256(h.upper()) == h
    with pytest.raises(updater.UpdateError):
        updater.parse_sha256("not a hash")


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text, self.content = text, content

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_get(payload: bytes, sha_text: str):
    def get(url, **kw):
        return FakeResp(text=sha_text) if url.endswith(".sha256") else FakeResp(content=payload)
    return get


def test_download_ok_and_mismatch(tmp_path, monkeypatch):
    info = updater.parse_release(release(), "1.0.0")
    payload = b"installer-bytes" * 1000
    good = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater.requests, "get", fake_get(payload, f"{good}  {EXE}"))
    dl = updater.download_installer(info, tmp_path)
    assert dl.path.read_bytes() == payload and dl.sha256 == good
    assert dl.path.parent != tmp_path and dl.path.parent.name.startswith("TokutaiApp-update-")  # 新しいランダムなフォルダ
    # 2回目は、別のフォルダになる（固定の名前ではない）
    assert updater.download_installer(info, tmp_path).path.parent != dl.path.parent
    # ハッシュが違うものは、削除して、実行させない（空のフォルダも残さない）
    before = set(tmp_path.iterdir())
    monkeypatch.setattr(updater.requests, "get", fake_get(payload, "0" * 64))
    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, tmp_path)
    assert set(tmp_path.iterdir()) == before


def test_launch_rechecks_hash(tmp_path, monkeypatch):
    info = updater.parse_release(release(), "1.0.0")
    payload = b"installer-bytes"
    good = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater.requests, "get", fake_get(payload, f"{good}  {EXE}"))
    dl = updater.download_installer(info, tmp_path)
    launched = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: launched.append(a[0]))
    updater.launch_installer(dl)  # 変わっていなければ、実行する
    assert launched and launched[0][0] == str(dl.path) and "/SILENT" in launched[0]
    # 取得のあとに、差し替えられたら、実行しない
    dl.path.write_bytes(b"tampered")
    launched.clear()
    with pytest.raises(updater.UpdateError):
        updater.launch_installer(dl)
    assert launched == [] and not dl.path.exists()


def test_download_rejects_foreign_url(tmp_path):
    info = updater.UpdateInfo("1.1.0", "", EXE, "https://example.com/" + EXE, BASE + SHA)
    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, tmp_path)
