"""アプリのアイコン tokutai/data/app.ico を作る。  python tools/make_icon.py

角丸の青い四角に、白い「退」。16〜256px の画像を1つの .ico にまとめる（PNG形式で格納）。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter

SIZES = [16, 24, 32, 48, 64, 128, 256]
BG = QColor("#2f6fd0")
OUT = Path(__file__).resolve().parent.parent / "tokutai" / "data" / "app.ico"


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setBrush(BG)
    p.setPen(Qt.PenStyle.NoPen)
    margin = size * 0.03
    r = size * 0.22  # 角丸の半径
    p.drawRoundedRect(QRectF(margin, margin, size - 2 * margin, size - 2 * margin), r, r)
    font = QFont("Yu Gothic UI")
    font.setPixelSize(round(size * 0.68))
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("white"))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "退")
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, png in images:
        w = 0 if size >= 256 else size  # 256 は 0 で表す
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return head + entries + data


def main() -> int:
    _app = QGuiApplication(sys.argv)  # フォント描画のために必要
    ico = build_ico([(s, png_bytes(draw(s))) for s in SIZES])
    OUT.write_bytes(ico)
    print(f"作成: {OUT}（{len(ico)} バイト）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
