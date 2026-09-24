# -*- coding: utf-8 -*-
"""
生成桌面便签的应用图标（Windows 11 Sticky Notes 风格）

设计要素：
- 外层：圆角方形 + 紫色渐变（Fluent Design 风格）
- 中层：白色便签纸，右上角折角
- 细节：便签上有几条浅灰横线表示文字

运行后会生成：
- app_icon.png  (256x256 预览)
- app_icon.ico  (多尺寸：16/32/48/64/128/256)

依赖：PyQt5, Pillow
"""
import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QRectF, QPointF, QLineF
from PyQt5.QtGui import (
    QPixmap, QPainter, QColor, QBrush, QPen, QLinearGradient, QPolygonF,
)
from PyQt5.QtWidgets import QApplication


def draw_icon(size: int = 256) -> QPixmap:
    """绘制指定尺寸的图标。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)

    # ---- 外层圆角方形 + 紫色渐变 ----
    # 留 8% 边距，让图标不贴边
    margin = size * 0.08
    outer_rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    radius = size * 0.18  # 圆角半径（Fluent Design 风格的大圆角）

    # 蓝色渐变：左上浅蓝 → 右下深蓝（120 度角方向）
    grad = QLinearGradient(outer_rect.topLeft(), outer_rect.bottomRight())
    grad.setColorAt(0.0, QColor("#90CAF9"))  # 浅蓝
    grad.setColorAt(1.0, QColor("#42A5F5"))  # 深蓝
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(outer_rect, radius, radius)

    # ---- 中层白色便签纸 ----
    # 便签比外层小，居中偏下一点
    note_margin_x = size * 0.22
    note_margin_top = size * 0.24
    note_margin_bottom = size * 0.22
    note_w = size - 2 * note_margin_x
    note_h = size - note_margin_top - note_margin_bottom
    note_rect = QRectF(note_margin_x, note_margin_top, note_w, note_h)
    note_radius = size * 0.04

    # 折角尺寸
    fold = size * 0.14

    # 先画完整便签（白色）
    p.setBrush(QColor("#FFFFFF"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(note_rect, note_radius, note_radius)

    # 右上角折角：用一个多边形覆盖右上角，颜色稍深
    # 折角三角形：从便签右上角向内 fold 距离
    top_right = note_rect.topRight()
    fold_poly = QPolygonF([
        QPointF(top_right.x() - fold, top_right.y()),         # 左上
        QPointF(top_right.x(), top_right.y()),                # 右上
        QPointF(top_right.x(), top_right.y() + fold),         # 右下
    ])
    p.setBrush(QColor("#E0E0E0"))  # 折角处浅灰
    p.setPen(Qt.NoPen)
    p.drawPolygon(fold_poly)

    # 折角的对角线（折痕）
    p.setPen(QPen(QColor("#BDBDBD"), size * 0.008))
    p.drawLine(QPointF(top_right.x() - fold, top_right.y()),
               QPointF(top_right.x(), top_right.y() + fold))

    # ---- 便签上的文字横线 ----
    # 避开折角区域，线条画在便签中下部
    p.setPen(QPen(QColor("#BDBDBD"), size * 0.012))
    line_left = note_rect.left() + size * 0.06
    line_right = note_rect.right() - size * 0.06
    # 第一条线避开折角，短一点
    first_line_right = note_rect.right() - fold - size * 0.04
    line_y_start = note_rect.top() + fold + size * 0.06
    line_gap = size * 0.07
    for i in range(3):
        y = line_y_start + i * line_gap
        x2 = first_line_right if i == 0 else line_right
        p.drawLine(QPointF(line_left, y), QPointF(x2, y))

    p.end()
    return pix


def save_ico(pix: QPixmap, path: Path):
    """用 Pillow 将 QPixmap 转为多尺寸 ICO 文件。"""
    # 先保存为临时 PNG
    tmp_png = path.with_suffix(".png")
    pix.save(str(tmp_png), "PNG")

    # 用 Pillow 生成多尺寸 ICO
    from PIL import Image
    img = Image.open(str(tmp_png))
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(str(path), format="ICO", sizes=sizes)


def main():
    app = QApplication(sys.argv)

    out_dir = Path(__file__).resolve().parent
    png_path = out_dir / "app_icon.png"
    ico_path = out_dir / "app_icon.ico"

    # 绘制 256x256 高清图标
    pix = draw_icon(256)
    pix.save(str(png_path), "PNG")
    print(f"已保存预览图: {png_path}")

    # 生成多尺寸 ICO
    save_ico(pix, ico_path)
    print(f"已保存图标文件: {ico_path}")

    print("完成。修改 sticky_notes.py 加载 app_icon.ico 即可使用。")


if __name__ == "__main__":
    main()
