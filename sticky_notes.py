# -*- coding: utf-8 -*-
"""
桌面悬浮便签
- 无边框、半透明、始终置顶
- 可拖动、可调整大小、可最小化到托盘
- 内容/位置/颜色持久化到 JSON 文件，关机不丢失
- 通过系统托盘图标管理：新建 / 显示全部 / 退出

依赖：pip install PyQt5
运行：python sticky_notes.py
"""

import sys
import json
import uuid
import ctypes
import math
import random
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QWidget, QTextEdit, QVBoxLayout, QHBoxLayout,
    QPushButton, QSystemTrayIcon, QMenu, QSizeGrip, QLineEdit, QShortcut,
    QSlider, QLabel, QFrame, QListWidget, QListWidgetItem, QMessageBox,
)
from PyQt5.QtCore import (
    Qt, QTimer, pyqtSignal, QEvent, QRect, QObject,
    QVariantAnimation, QEasingCurve,
)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtGui import QColor, QIcon, QPixmap, QPainter, QBrush, QFont, QKeySequence


# 配置文件放在程序所在目录，便于备份/迁移，也避免用户目录权限问题
# 注意：PyInstaller 打包后 __file__ 指向 sys._MEIPASS 临时目录，
# 退出时该目录会被自动清理，导致数据丢失。因此打包后必须改用
# sys.executable 的目录（exe 所在目录）存放配置，才能持久化。
if getattr(sys, "frozen", False):
    # PyInstaller 打包后：配置存放在 exe 所在目录
    CONFIG_PATH = Path(sys.executable).resolve().parent / "sticky_notes.json"
else:
    # 开发环境：配置放在脚本所在目录
    CONFIG_PATH = Path(__file__).resolve().parent / "sticky_notes.json"


def resource_path(relative: str) -> Path:
    """获取资源文件的绝对路径，兼容 PyInstaller 打包环境。
    打包后资源会被解压到 sys._MEIPASS 临时目录。"""
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller 打包后
        return Path(sys._MEIPASS) / relative
    # 开发环境
    return Path(__file__).resolve().parent / relative

# 便签颜色循环方案（柔和的浅色系）
NOTE_COLORS = ["#FFF9C4", "#FFCCBC", "#C8E6C9", "#BBDEFB", "#E1BEE7", "#FFCDD2"]

# Win32 SetWindowPos 置顶相关常量(用 ctypes 直接切换 TOPMOST，避免 setWindowFlags 重建窗口闪烁)
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010

# 声明 SetWindowPos 参数类型：HWND 必须用 c_void_p(64位指针)
# 否则传 -1 会被当成 c_int 截断成 32 位 0xFFFFFFFF，导致 HWND_TOPMOST 失效、置顶切不回去
try:
    _user32 = ctypes.windll.user32
    _user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,  # HWND hWnd, HWND hWndInsertAfter
        ctypes.c_int, ctypes.c_int,        # int X, Y
        ctypes.c_int, ctypes.c_int,        # int cx, cy
        ctypes.c_uint,                     # UINT uFlags
    ]
    _user32.SetWindowPos.restype = ctypes.c_int
except (AttributeError, OSError):
    _user32 = None  # 非 Windows 平台


# --------------------------------------------------------------------------- #
# 标题编辑控件
# --------------------------------------------------------------------------- #
class TitleEdit(QLineEdit):
    """可编辑标题：默认只读并透传鼠标事件(便于拖动便签)，双击进入编辑，
    回车/Esc/失焦退出编辑并恢复只读。文字居中显示。"""

    def __init__(self, text: str = "便签"):
        super().__init__(text)
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignCenter)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        # 字体用 QFont 控制(粗体)，便于父窗口按尺寸动态 setPixelSize
        f = self.font()
        f.setBold(True)
        f.setPixelSize(11)  # 初始值，后续由 _update_title_size 覆盖
        self.setFont(f)
        # padding 较小，让标题文字在整栏水平居中；按钮浮动在右上角不参与布局
        self.setStyleSheet(
            "QLineEdit { background: transparent; border: none;"
            "            color: #5d4037; padding: 0 8px; }"
            "QLineEdit:focus { background: rgba(255,255,255,0.45); border-radius: 3px; }"
        )

    # 只读状态下，鼠标事件 ignore 让父窗口接管拖动
    def mousePressEvent(self, event):
        if self.isReadOnly():
            event.ignore()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isReadOnly():
            event.ignore()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.isReadOnly():
            event.ignore()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setReadOnly(False)
            self.setFocus()
            self.selectAll()
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event):
        self.setReadOnly(True)
        self.deselect()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        # 回车/Esc 结束编辑
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
            self.setReadOnly(True)
            self.clearFocus()
        else:
            super().keyPressEvent(event)


# --------------------------------------------------------------------------- #
# 透明度调节面板（悬浮在便签右上角的小型 QSlider 面板）
# --------------------------------------------------------------------------- #
class OpacityPanel(QFrame):
    """悬浮面板：包含一个 0-100 的 QSlider 和一个显示百分比的 QLabel。
    点击面板外部或按 Esc 自动关闭。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 不用 Qt.Popup（它会 grab 鼠标导致便签收不到事件），改用 Tool + Frameless
        # 外部点击检测通过应用级事件过滤器实现
        # 加 WindowStaysOnTopHint：当便签已置顶（Win32 HWND_TOPMOST）时，
        # 透明面板也需要保持最顶层，否则会被便签挡住看不见
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedWidth(200)
        self.setStyleSheet(
            "QFrame { background: #fff; border: 1px solid #ccc; border-radius: 6px; }"
            "QSlider::groove:horizontal { height: 6px; background: #ddd; border-radius: 3px; }"
            "QSlider::sub-page:horizontal { background: #6d4c41; border-radius: 3px; }"
            "QSlider::handle:horizontal { width: 14px; margin: -5px 0; border-radius: 7px; background: #5d4037; }"
            "QLabel { color: #5d4037; font-size: 12px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # 标题行
        self.title_label = QLabel("透明度")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)

        # 百分比显示
        self.value_label = QLabel("92%")
        self.value_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value_label)

        # 滑杆 10-100（最低 10，避免便签完全看不见）
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(10, 100)
        self.slider.setValue(92)
        self.slider.setTickPosition(QSlider.NoTicks)
        self.slider.valueChanged.connect(self._update_label)
        layout.addWidget(self.slider)

    def _update_label(self, value: int):
        self.value_label.setText(f"{value}%")

    def set_opacity(self, value: int):
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.value_label.setText(f"{value}%")

    def mousePressEvent(self, event):
        # 点击面板自身不关闭（Popup 会自动关闭外部点击）
        event.accept()


# --------------------------------------------------------------------------- #
# 单个便签窗口
# --------------------------------------------------------------------------- #
class StickyNote(QWidget):
    # 状态变更信号：发送 note_id，由管理器统一读取最新状态并保存
    noteChanged = pyqtSignal(str)
    # 关闭信号：用户点击 × 删除该便签
    noteDeleted = pyqtSignal(str)

    def __init__(self, data: dict):
        super().__init__()
        self.note_id = data["id"]
        self._color = data.get("color", NOTE_COLORS[0])
        self._pinned = data.get("pinned", False)  # 是否置顶(默认不置顶)

        # 窗口标志：无边框(在任务栏显示图标，便于用户切换/恢复)
        # 置顶不用 WindowStaysOnTopHint(会重建窗口闪烁)，改由 Win32 SetWindowPos 控制
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self._opacity = data.get("opacity", 92)  # 透明度 0-100，默认 92
        self.setWindowOpacity(self._opacity / 100.0)
        self.setGeometry(
            data.get("x", 100), data.get("y", 100),
            data.get("w", 500), data.get("h", 500),
        )
        # 记录"展开态"高度：get_state 在折叠态下保存的是展开高度，
        # 因此 data["h"] 始终代表展开高度，用于展开时恢复
        self._expanded_h = data.get("h", 500)

        self._drag_offset = None  # 鼠标拖动偏移量
        self._font_size = data.get("font_size", 13)  # 内容字号(Ctrl+/- 可调)
        # 完成状态：None=未设置, "done"=已完成, "undone"=未完成（控制窗口里标记）
        self._done_status = data.get("done_status", None)
        # 正文是否折叠（只隐藏 editor，标题与截止日期仍显示）
        self._content_collapsed = data.get("content_collapsed", False)

        self._build_ui(
            data.get("content", ""),
            data.get("title", "便签"),
            data.get("deadline", ""),
        )
        self._apply_color(self._color)
        self._update_title_size()  # 按窗口尺寸初始化标题栏高度与字号
        self._update_deadline_size()  # 按窗口尺寸初始化截止日期栏高度与字号

        # 防抖保存定时器：500ms 内多次变更只触发一次保存
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(lambda: self.noteChanged.emit(self.note_id))

    # --------------------------- UI 构建 --------------------------- #
    def _build_ui(self, content: str, title: str, deadline: str = ""):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏：承担拖动；标题占满整栏居中，按钮浮动在右上角(不挤压标题)
        self.title_bar = QWidget()
        self.title_bar.setObjectName("titleBar")
        self.title_bar.setFixedHeight(28)
        self.title_bar.setCursor(Qt.SizeAllCursor)  # 提示可拖动
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)

        self.title_edit = TitleEdit(title)
        self.title_edit.textChanged.connect(self._schedule_save)
        title_layout.addWidget(self.title_edit)

        # 置顶切换按钮 📌
        self.pin_btn = self._make_btn("📌", 15, self._toggle_pin, "#6d4c41")
        # 颜色切换按钮 ◐
        self.color_btn = self._make_btn("◐", 17, self._cycle_color, "#6d4c41")
        self.color_btn.setToolTip("切换颜色")
        # 折叠正文按钮 ▤/▭：点击隐藏正文 editor，标题与截止日期仍显示；再次点击恢复
        self.min_btn = self._make_btn("▤", 16, self._toggle_content, "#6d4c41")
        self.min_btn.setToolTip("折叠正文")
        # 关闭按钮 ×（删除该便签）
        self.close_btn = self._make_btn("×", 19, self._on_delete, "#6d4c41", hover="#c62828")
        self.close_btn.setToolTip("删除便签")
        # 透明度按钮 ◌（打开透明度调节面板）
        self.opacity_btn = self._make_btn("◌", 16, self._toggle_opacity_panel, "#6d4c41")
        self.opacity_btn.setToolTip("调整透明度")
        # 按钮浮动在标题栏右上角，不参与布局，由 _update_title_size 定位
        for b in (self.pin_btn, self.color_btn, self.min_btn, self.close_btn, self.opacity_btn):
            b.setParent(self.title_bar)
            b.raise_()
        # 初始化置顶按钮样式（根据恢复的 pinned 状态：镂空或常态）
        self._update_pin_style()

        # 截止日期行：需在 [输入框] 之前完成
        self.deadline_bar = QWidget()
        self.deadline_bar.setObjectName("deadlineBar")
        self.deadline_bar.setFixedHeight(26)
        deadline_layout = QHBoxLayout(self.deadline_bar)
        deadline_layout.setContentsMargins(10, 0, 10, 0)
        deadline_layout.setSpacing(4)

        self.deadline_label_left = QLabel("需在")
        self.deadline_label_left.setStyleSheet(
            "color: #8d6e63; font-weight: bold;"
        )
        deadline_layout.addWidget(self.deadline_label_left)

        self.deadline_edit = QLineEdit(deadline)
        self.deadline_edit.setPlaceholderText("日期时间，例如 2026-08-15 18:00")
        self.deadline_edit.textChanged.connect(self._schedule_save)
        self.deadline_edit.setStyleSheet(
            "QLineEdit { background: rgba(255,255,255,0.5); border: 1px solid rgba(141, 110, 99, 0.3);"
            "           border-radius: 3px; padding: 0 6px; color: #5d4037; }"
            "QLineEdit:focus { background: rgba(255,255,255,0.85); border: 1px solid #8d6e63; }"
        )
        deadline_layout.addWidget(self.deadline_edit, 1)

        self.deadline_label_right = QLabel("之前完成")
        self.deadline_label_right.setStyleSheet(
            "color: #8d6e63; font-weight: bold;"
        )
        deadline_layout.addWidget(self.deadline_label_right)

        # 内容编辑区
        self.editor = QTextEdit()
        self.editor.setPlainText(content)
        self.editor.setStyleSheet(
            "QTextEdit { background: transparent; border: none; padding: 6px;"
            "            color: #2d2d2d; }"
        )
        self._apply_font_size(self._font_size)
        self.editor.textChanged.connect(self._schedule_save)

        # Ctrl + 加号放大 / Ctrl + 减号缩小 内容字号
        QShortcut(QKeySequence("Ctrl++"), self, activated=self._zoom_in)
        QShortcut(QKeySequence("Ctrl+="), self, activated=self._zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self, activated=self._zoom_out)

        # 底部缩放手柄
        self.bottom = QWidget()
        self.bottom.setFixedHeight(16)
        bottom_layout = QHBoxLayout(self.bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addStretch()
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(16, 16)
        bottom_layout.addWidget(self.grip)

        layout.addWidget(self.title_bar)
        layout.addWidget(self.deadline_bar)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.bottom)

        # 根据持久化的折叠状态初始化：折叠时正文框与底部手柄都隐藏，
        # 窗口高度收缩到只装下标题栏 + 截止日期栏
        self._apply_content_visibility()

    def _make_btn(self, text, size, slot, color, hover=None):
        btn = QPushButton(text)
        btn.setFixedSize(28, 28)
        hover_css = f"color: {hover};" if hover else "color: #333;"
        btn.setStyleSheet(
            f"QPushButton {{ border: none; color: {color};"
            f"  font-size: {size}px; font-weight: bold;"
            f"  border-radius: 4px; }}"
            f"QPushButton:hover {{ background: rgba(0,0,0,0.08); {hover_css} }}"
        )
        btn.clicked.connect(slot)
        return btn

    # --------------------------- 透明度 --------------------------- #
    def _toggle_opacity_panel(self):
        if hasattr(self, "_opacity_panel") and self._opacity_panel.isVisible():
            self._opacity_panel.hide()
            # 移除应用级事件过滤器
            if hasattr(self, "_app_event_filter_installed"):
                QApplication.instance().removeEventFilter(self)
                self._app_event_filter_installed = False
            return
        # 初始化面板（首次）
        if not hasattr(self, "_opacity_panel"):
            self._opacity_panel = OpacityPanel()
            self._opacity_panel.slider.valueChanged.connect(self._set_opacity)
        self._opacity_panel.set_opacity(self._opacity)
        # 定位到透明度按钮正下方
        btn_pos = self.opacity_btn.mapToGlobal(self.opacity_btn.rect().bottomLeft())
        self._opacity_panel.move(btn_pos.x() - 80, btn_pos.y() + 6)
        self._opacity_panel.show()
        # 用 Win32 SetWindowPos 把面板设为 TOPMOST：
        # 便签置顶是通过 Win32 API 直接设 HWND_TOPMOST 的（比 Qt.WindowStaysOnTopHint 更底层），
        # 如果面板只靠 WindowStaysOnTopHint，仍会被便签挡住。调用同一 API 保证两者层级一致。
        if _user32 is not None:
            hwnd = int(self._opacity_panel.winId())
            _user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                ctypes.c_void_p(_HWND_TOPMOST),
                0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
            )
        # 安装应用级事件过滤器：检测面板外点击（包括点击便签本身）
        if not hasattr(self, "_app_event_filter_installed") or not self._app_event_filter_installed:
            QApplication.instance().installEventFilter(self)
            self._app_event_filter_installed = True

    def eventFilter(self, obj, event):
        """应用级事件过滤器：点击透明度面板外部时自动关闭面板。
        覆盖范围：便签本身、其他窗口、桌面任意位置。"""
        if hasattr(self, "_opacity_panel") and self._opacity_panel.isVisible():
            if event.type() == QEvent.MouseButtonPress:
                panel = self._opacity_panel
                panel_rect = QRect(panel.mapToGlobal(panel.rect().topLeft()), panel.size())
                click_pos = event.globalPos()
                if not panel_rect.contains(click_pos):
                    panel.hide()
                    QApplication.instance().removeEventFilter(self)
                    self._app_event_filter_installed = False
        # 用 try/except 兼容测试时的假事件对象
        try:
            return super().eventFilter(obj, event)
        except TypeError:
            return False

    def _set_opacity(self, value: int):
        """10-100 整数 → QWidget 透明度。"""
        self._opacity = max(10, min(100, value))
        self.setWindowOpacity(self._opacity / 100.0)
        self._schedule_save()

    # --------------------------- 置顶切换 --------------------------- #
    def _toggle_pin(self):
        # 直接用 Win32 切换 TOPMOST，不重建窗口，无闪烁
        self._pinned = not self._pinned
        self._set_topmost(self._pinned)
        self._update_pin_style()
        self._schedule_save()

    def _update_pin_style(self):
        """置顶按钮样式：不置顶时透明镂空（只有边框+浅字），置顶时与其他按钮同风格。"""
        if self._pinned:
            # 置顶态：和颜色/最小化按钮一致（由 _make_btn 生成的 QSS）
            self.pin_btn.setStyleSheet(
                "QPushButton { border: none; color: #6d4c41; font-size: 15px;"
                "  font-weight: bold; border-radius: 4px; }"
                "QPushButton:hover { background: rgba(0,0,0,0.08); color: #333; }"
            )
            self.pin_btn.setToolTip("取消置顶")
        else:
            # 未置顶态：镂空效果——背景全透明、图标浅灰、虚线边框
            self.pin_btn.setStyleSheet(
                "QPushButton { background: transparent; border: 1.5px dashed #9c8c80;"
                "  color: #9c8c80; font-size: 15px; font-weight: bold;"
                "  border-radius: 4px; }"
                "QPushButton:hover { background: rgba(0,0,0,0.06);"
                "  border-style: solid; color: #6d4c41; border-color: #6d4c41; }"
            )
            self.pin_btn.setToolTip("置顶")

    def _set_topmost(self, topmost: bool):
        """通过 Win32 SetWindowPos 切换置顶，避免 setWindowFlags 重建窗口导致闪烁。"""
        if _user32 is None:
            return  # 非 Windows 平台无法使用，保持默认(不置顶)
        hwnd = int(self.winId())
        if not hwnd:
            return
        # HWND_TOPMOST/HWND_NOTOPMOST 是指针值 -1/-2，必须用 c_void_p 包装
        # 否则 64 位系统上会被截断成 32 位，导致置顶失效
        insert_after = _HWND_TOPMOST if topmost else _HWND_NOTOPMOST
        flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
        _user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(insert_after),
            0, 0, 0, 0, flags,
        )

    def showEvent(self, event):
        super().showEvent(event)
        # 窗口显示后同步置顶状态(winId 在显示后才稳定可靠)
        if self._pinned:
            self._set_topmost(True)

    # --------------------------- 字号缩放 --------------------------- #
    def _apply_font_size(self, size: int):
        """应用内容编辑区字号(用 QFont 而非 QSS，避免被样式表覆盖)。"""
        self._font_size = size
        self.editor.setStyleSheet(
            "QTextEdit { background: transparent; border: none; padding: 6px;"
            "            color: #2d2d2d; }"
        )
        f = self.editor.font()
        f.setPointSize(size)
        self.editor.setFont(f)

    def _zoom_in(self):
        self._apply_font_size(min(72, self._font_size + 1))
        self._schedule_save()

    def _zoom_out(self):
        self._apply_font_size(max(8, self._font_size - 1))
        self._schedule_save()

    # --------------------------- 标题尺寸同步 --------------------------- #
    def _update_title_size(self):
        """窗口缩放时同步标题栏高度与标题字号；按钮垂直居中靠右排列。"""
        h = self.height()
        if getattr(self, "_content_collapsed", False):
            # 折叠态：保持当前标题栏高度，不按(变小的)窗口高度重算
            title_h = self.title_bar.height()
        else:
            title_h = max(32, min(48, h // 8))
        font_px = max(13, int(title_h * 0.5))
        self.title_bar.setFixedHeight(title_h)
        f = self.title_edit.font()
        f.setPixelSize(font_px)
        self.title_edit.setFont(f)
        # 五个按钮固定 28x28，垂直居中、靠右排列
        # 顺序：📌置顶 ◐颜色 ▤折叠正文 ×删除 ◌透明度
        btn, gap = 28, 5
        total = 5 * btn + 4 * gap
        x0 = self.width() - total - 4
        y0 = (title_h - btn) // 2
        self.pin_btn.move(x0, y0)
        self.color_btn.move(x0 + btn + gap, y0)
        self.min_btn.move(x0 + 2 * (btn + gap), y0)
        self.close_btn.move(x0 + 3 * (btn + gap), y0)
        self.opacity_btn.move(x0 + 4 * (btn + gap), y0)

    def _update_deadline_size(self):
        """窗口缩放时同步截止日期栏高度与字号（与标题栏一致）。"""
        # 防御：_build_ui 尚未执行时（setGeometry 提前触发 resizeEvent）跳过
        if not hasattr(self, "deadline_bar"):
            return
        h = self.height()
        if getattr(self, "_content_collapsed", False):
            # 折叠态：保持当前截止日期栏高度，不按(变小的)窗口高度重算
            bar_h = self.deadline_bar.height()
        else:
            # 与标题栏使用相同的尺寸规则，保持视觉一致
            bar_h = max(32, min(48, h // 8))
        font_px = max(13, int(bar_h * 0.5))
        self.deadline_bar.setFixedHeight(bar_h)
        for w in (self.deadline_label_left, self.deadline_label_right):
            f = w.font()
            f.setPixelSize(font_px)
            f.setBold(True)
            w.setFont(f)
        f = self.deadline_edit.font()
        f.setPixelSize(font_px)
        self.deadline_edit.setFont(f)

    # --------------------------- 颜色 --------------------------- #
    def _apply_color(self, color: str):
        self._color = color
        c = QColor(color)
        # 标题栏颜色稍深一点（降低明度）
        h, s, v, a = c.getHsv()
        darker = QColor.fromHsv(h, s, max(0, v - 18), a)
        self.setStyleSheet(f"StickyNote {{ background: {color}; }}")
        # 仅作用于标题栏本身，不影响浮动的标题与按钮
        self.title_bar.setStyleSheet(
            f"QWidget#titleBar {{ background: {darker.name()}; border: none; }}"
        )

    def _cycle_color(self):
        idx = NOTE_COLORS.index(self._color) if self._color in NOTE_COLORS else 0
        self._apply_color(NOTE_COLORS[(idx + 1) % len(NOTE_COLORS)])
        self._schedule_save()

    # --------------------------- 正文折叠 --------------------------- #
    def _toggle_content(self):
        """切换正文折叠，带高度动画：
        - 折叠：正文框由下向上收缩消失（窗口高度渐缩到标题栏+截止日期栏）
        - 展开正文框由上向下展开出现（窗口高度渐扩到记录的展开高度）"""
        if not self._content_collapsed:
            # 即将折叠：记录当前展开高度，供下次展开恢复
            self._expanded_h = self.height()
        self._content_collapsed = not self._content_collapsed
        # 更新按钮图标与提示
        if self._content_collapsed:
            self.min_btn.setText("▭")
            self.min_btn.setToolTip("展开正文")
        else:
            self.min_btn.setText("▤")
            self.min_btn.setToolTip("折叠正文")

        if self._content_collapsed:
            # 折叠方向：动画期间 editor/bottom 仍可见，被窗口收缩自然"卷起"
            start_h = self.height()
            end_h = self.title_bar.height() + self.deadline_bar.height()
            self._animate_height(start_h, end_h, collapsing=True)
        else:
            # 展开方向：先解除固定高度并显示 editor/bottom，再动画展开
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX
            self.editor.setVisible(True)
            self.bottom.setVisible(True)
            start_h = self.height()
            end_h = getattr(self, "_expanded_h", 0) or 500
            self._animate_height(start_h, end_h, collapsing=False)
        self._schedule_save()

    def _animate_height(self, start_h: int, end_h: int, collapsing: bool):
        """窗口高度动画。collapsing=True 折叠方向，结束时隐藏 editor/bottom 并锁高度。"""
        # 若有正在进行的动画，先停掉，避免冲突
        if hasattr(self, "_height_anim") and self._height_anim.state():
            self._height_anim.stop()
        anim = QVariantAnimation(self)
        anim.setStartValue(int(start_h))
        anim.setEndValue(int(end_h))
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def on_value(v):
            # 动画每帧改变窗口高度，editor 被 QBoxLayout 拉伸自然产生
            # "由下向上消失/由上向下出现" 的视觉效果
            self.resize(self.width(), int(v))

        def on_finished():
            if collapsing:
                # 折叠完成：隐藏正文框与底部手柄，锁定窗口高度
                self.editor.setVisible(False)
                self.bottom.setVisible(False)
                self.setFixedHeight(int(end_h))
            # 同步按钮排列与栏内字号
            self._update_title_size()
            self._update_deadline_size()

        anim.valueChanged.connect(on_value)
        anim.finished.connect(on_finished)
        # 持有引用防止被 Python GC 回收导致动画中断
        self._height_anim = anim
        anim.start()

    def _apply_content_visibility(self):
        """根据 _content_collapsed 立即应用 editor/bottom 可见性与窗口高度（无动画）。
        仅用于 _build_ui 初始化持久化的折叠状态。"""
        if not hasattr(self, "editor") or not hasattr(self, "bottom"):
            return  # _build_ui 尚未完成
        if self._content_collapsed:
            self.editor.setVisible(False)
            self.bottom.setVisible(False)
            # 收缩窗口到只装下标题栏 + 截止日期栏
            collapsed_h = self.title_bar.height() + self.deadline_bar.height()
            self.setFixedHeight(collapsed_h)
        else:
            # 解除 setFixedHeight 设置的 min/max 限制
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX
            self.editor.setVisible(True)
            self.bottom.setVisible(True)
            target_h = getattr(self, "_expanded_h", 0) or 500
            self.resize(self.width(), target_h)
        # 同步按钮排列与栏内字号
        self._update_title_size()
        self._update_deadline_size()

    # --------------------------- 状态读写 --------------------------- #
    def get_state(self) -> dict:
        # 折叠态下 self.height() 是收缩后的小高度，需保存展开高度，
        # 否则重启后展开会恢复成错误的小高度
        saved_h = (self._expanded_h if self._content_collapsed else self.height())
        return {
            "id": self.note_id,
            "x": self.x(), "y": self.y(),
            "w": self.width(), "h": saved_h,
            "content": self.editor.toPlainText(),
            "color": self._color,
            "title": self.title_edit.text(),
            "deadline": self.deadline_edit.text(),
            "font_size": self._font_size,
            "pinned": self._pinned,
            "opacity": self._opacity,
            "done_status": self._done_status,
            "content_collapsed": self._content_collapsed,
        }

    def _schedule_save(self):
        """防抖：500ms 内多次变更合并为一次保存。"""
        self._save_timer.start(500)

    def _on_delete(self, *args, confirm: bool = True):
        """删除便签。
        confirm=True（便签右上角 × 触发）→ 先弹确认框，用户选"是"才删除。
        confirm=False（控制窗口右键/批量删除触发）→ 直接删除，不再弹窗。
        用 *args 吸收 QPushButton.clicked 信号自带的 bool 参数，
        避免它被误传给 confirm 导致跳过确认框。"""
        if confirm:
            reply = QMessageBox.question(
                self, "确认删除",
                f"确定要删除便签「{self.title_edit.text() or '无标题'}」吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,  # 默认聚焦"否"，防止误点
            )
            if reply != QMessageBox.Yes:
                return
        self.noteDeleted.emit(self.note_id)

    # --------------------------- 拖动 --------------------------- #
    def mousePressEvent(self, event):
        # 仅当点击发生在标题栏区域时启动拖动
        if event.button() == Qt.LeftButton and event.pos().y() <= self.title_bar.height():
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            self._schedule_save()  # 拖动结束保存位置
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_title_size()  # 同步标题栏高度与标题字号
        self._update_deadline_size()  # 同步截止日期栏高度与字号
        self._schedule_save()  # 缩放后保存尺寸

    def hideEvent(self, event):
        # 便签隐藏(最小化)时关闭透明度面板
        if hasattr(self, "_opacity_panel"):
            self._opacity_panel.hide()
            if hasattr(self, "_app_event_filter_installed") and self._app_event_filter_installed:
                QApplication.instance().removeEventFilter(self)
                self._app_event_filter_installed = False
        super().hideEvent(event)


# --------------------------------------------------------------------------- #
# 便签管理器：持久化 + 创建/删除/恢复
# --------------------------------------------------------------------------- #
class NoteManager(QObject):
    # 列表变更信号：便签增删或标题变更时触发，通知控制窗口刷新
    listChanged = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.notes: dict[str, StickyNote] = {}

    # --------------------------- 持久化 --------------------------- #
    def load(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for item in data.get("notes", []):
                self._create_from_data(item)
        except (json.JSONDecodeError, KeyError):
            # 配置损坏时不崩溃，静默跳过
            pass

    def save(self):
        data = {"notes": [note.get_state() for note in self.notes.values()]}
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # --------------------------- 增删 --------------------------- #
    def _create_from_data(self, data: dict) -> StickyNote:
        note = StickyNote(data)
        note.noteChanged.connect(self._on_note_changed)
        note.noteDeleted.connect(self._on_note_deleted)
        # 标题/截止日期变更时通知控制窗口刷新列表
        note.title_edit.textChanged.connect(lambda: self.listChanged.emit())
        note.deadline_edit.textChanged.connect(lambda: self.listChanged.emit())
        # 颜色变更时也刷新列表（列表项图标依赖颜色）
        note.noteChanged.connect(lambda _nid: self.listChanged.emit())
        self.notes[note.note_id] = note
        return note

    def create_new(self) -> StickyNote:
        # 新建便签默认偏移一点，避免完全重叠
        offset = len(self.notes) * 30 % 200
        data = {
            "id": uuid.uuid4().hex,
            "x": 120 + offset, "y": 120 + offset,
            "w": 500, "h": 500,
            "content": "", "color": NOTE_COLORS[len(self.notes) % len(NOTE_COLORS)],
            "title": "便签", "font_size": 13, "pinned": False, "opacity": 50,
        }
        note = self._create_from_data(data)
        note.show()
        self.save()
        return note

    def _on_note_changed(self, note_id: str):
        self.save()

    def _on_note_deleted(self, note_id: str):
        note = self.notes.pop(note_id, None)
        if note:
            note.hide()
            note.deleteLater()
            self.save()
            self.listChanged.emit()

    def show_all(self):
        for note in self.notes.values():
            # showNormal 恢复最小化的窗口(任务栏图标点击恢复等效)
            note.showNormal()
            note.raise_()
            note.activateWindow()

    def close_all(self):
        """关闭所有便签窗口（不删除数据，下次启动可恢复）。"""
        for note in list(self.notes.values()):
            note.hide()
        self.save()

    def delete_all(self):
        """删除所有便签（清空内存 + 写空数据到 JSON，不可恢复）。"""
        for note in list(self.notes.values()):
            note.hide()
            note.deleteLater()
        self.notes.clear()
        self.save()
        self.listChanged.emit()


# --------------------------------------------------------------------------- #
# 控制面板便签列表项 widget：[色条][状态方框][标题+截止][已完成][未完成]
# --------------------------------------------------------------------------- #
class NoteListItemWidget(QWidget):
    """控制窗口便签列表的每一行：
    - 左侧颜色色条（标识便签颜色）
    - 状态方框：未设置时空白，"已完成"显示 🙂，"未完成"显示 😞
    - 中间标题 + 截止日期
    - 右侧"已完成""未完成"两个按钮，点击切换状态并持久化"""

    def __init__(self, note: "StickyNote", manager: "NoteManager", parent=None):
        super().__init__(parent)
        self._note = note
        self._manager = manager

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 14, 12, 14)
        lay.setSpacing(12)

        # 颜色色条
        color_bar = QLabel()
        color_bar.setFixedSize(4, 68)
        color_bar.setStyleSheet(
            f"background: {note._color}; border-radius: 2px;"
        )
        lay.addWidget(color_bar)

        # 状态方框（显示笑脸/沮丧脸）
        self.status_box = QLabel()
        self.status_box.setFixedSize(46, 46)
        self.status_box.setAlignment(Qt.AlignCenter)
        self.status_box.setStyleSheet(
            "QLabel { border: 2px solid #bdbdbd; border-radius: 6px;"
            "  background: #ffffff; font-size: 22px; }"
        )
        lay.addWidget(self.status_box)

        # 标题 + 截止日期（用垂直布局+两个纯文本QLabel，避免RichText尺寸估算导致第二行被裁掉）
        info_wrap = QWidget()
        info_lay = QVBoxLayout(info_wrap)
        info_lay.setContentsMargins(0, 0, 0, 0)
        info_lay.setSpacing(4)

        title_label = QLabel(note.title_edit.text() or "（无标题）")
        title_label.setStyleSheet("font-size:16px; font-weight:bold; color:#424242;")
        title_label.setWordWrap(True)  # 长标题自动换行，避免被截断
        title_label.setMinimumHeight(26)
        info_lay.addWidget(title_label)

        deadline = note.deadline_edit.text().strip()
        deadline_label = QLabel(f"⏰ 需在 {deadline} 之前完成" if deadline else "")
        if deadline:
            deadline_label.setStyleSheet("font-size:13px; color:#8d6e63;")
        else:
            deadline_label.setStyleSheet("font-size:13px; color:transparent;")  # 占位保持高度一致
        deadline_label.setWordWrap(True)  # 长截止日期也允许换行
        deadline_label.setMinimumHeight(22)
        info_lay.addWidget(deadline_label)

        lay.addWidget(info_wrap, 1)

        # 已完成 / 未完成 按钮
        self.btn_done = QPushButton("已完成")
        self.btn_undone = QPushButton("未完成")
        for b in (self.btn_done, self.btn_undone):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
        self.btn_done.setStyleSheet(
            "QPushButton { background: #E8F5E9; color: #2E7D32; border: 1px solid #A5D6A7;"
            "  border-radius: 6px; padding: 2px 12px; font-size: 13px; }"
            "QPushButton:hover { background: #C8E6C9; }"
            "QPushButton:pressed { background: #A5D6A7; }"
        )
        self.btn_undone.setStyleSheet(
            "QPushButton { background: #FFEBEE; color: #C62828; border: 1px solid #FFCDD2;"
            "  border-radius: 6px; padding: 2px 12px; font-size: 13px; }"
            "QPushButton:hover { background: #FFCDD2; }"
            "QPushButton:pressed { background: #EF9A9A; }"
        )
        self.btn_done.clicked.connect(lambda: self._set_status("done"))
        self.btn_undone.clicked.connect(lambda: self._set_status("undone"))
        lay.addWidget(self.btn_done)
        lay.addWidget(self.btn_undone)

        self._update_box()

    def _set_status(self, status: str):
        """切换状态：更新便签的 _done_status，触发防抖保存，刷新方框。"""
        # 重复点击同一状态 → 取消（切回未设置）
        if self._note._done_status == status:
            self._note._done_status = None
        else:
            self._note._done_status = status
        self._note._schedule_save()  # 复用便签的防抖保存机制持久化
        self._update_box()

    def _update_box(self):
        s = self._note._done_status
        if s == "done":
            self.status_box.setText("🙂")
            self.status_box.setStyleSheet(
                "QLabel { border: 2px solid #66BB6A; border-radius: 6px;"
                "  background: #E8F5E9; font-size: 22px; }"
            )
        elif s == "undone":
            self.status_box.setText("😞")
            self.status_box.setStyleSheet(
                "QLabel { border: 2px solid #EF5350; border-radius: 6px;"
                "  background: #FFEBEE; font-size: 22px; }"
            )
        else:
            self.status_box.setText("")
            self.status_box.setStyleSheet(
                "QLabel { border: 2px solid #bdbdbd; border-radius: 6px;"
                "  background: #ffffff; font-size: 22px; }"
            )


# --------------------------------------------------------------------------- #
# 烟花庆祝窗口：删除所有便签后弹出，显示烟花动画 + "完成所有任务"文字
# --------------------------------------------------------------------------- #
class FireworksWindow(QWidget):
    """全屏半透明烟花窗口：
    - 黑色半透明背景覆盖整个屏幕
    - 烟花从底部上升、爆炸成多色粒子向四周扩散，受重力下落
    - 中心显示"🎉 完成所有任务 🎉"文字，带淡入淡出效果
    - 持续 5 秒后自动关闭"""

    def __init__(self, duration_ms: int = 5000, parent=None):
        super().__init__(parent)
        # 无边框 + 工具窗口 + 置顶 + 半透明背景
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 覆盖整个屏幕
        screen = QApplication.instance().primaryScreen().geometry()
        self.setGeometry(screen)

        self._particles = []  # 爆炸后的粒子
        self._rockets = []    # 上升中的烟花弹
        self._flashes = []    # 爆炸瞬间的白色闪光
        self._elapsed = 0
        self._duration = duration_ms
        self._text_alpha = 0  # 文字透明度（淡入淡出）

        # 计时器：每 30ms 更新一帧（约 33fps）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self):
        """显示窗口并启动动画。"""
        self.showFullScreen()
        self.raise_()
        self._timer.start(30)

    # --------------------------- 烟花逻辑 --------------------------- #
    def _launch_rocket(self):
        """从屏幕底部随机位置发射一枚上升烟花弹，目标爆炸点在屏幕上半部分。"""
        w = self.width()
        h = self.height()
        x = random.uniform(w * 0.1, w * 0.9)
        # 目标爆炸高度集中在屏幕上半部分（10%~45%）
        target_y = random.uniform(h * 0.10, h * 0.45)
        # 初始上升速度（向上为负 y）
        vy = -random.uniform(12, 16)
        color = QColor.fromHsv(random.randint(0, 359), 220, 255)
        # 随机选择爆炸类型：球形 / 环形 / 双层 / 心形 / 柳树形
        etype = random.choice(["ball", "ring", "double", "heart", "willow"])
        self._rockets.append({
            "x": x, "y": h, "vy": vy, "target_y": target_y,
            "color": color, "trail": [], "etype": etype,
        })

    def _explode(self, rocket):
        """烟花弹到达目标高度后爆炸成多个粒子。
        支持多种爆炸形态，并在爆炸瞬间添加一个白色闪光。"""
        x, y = rocket["x"], rocket["y"]
        color = rocket["color"]
        etype = rocket["etype"]

        # 爆炸闪光：一个快速衰减的白色大圆，营造爆点感
        self._flashes.append({"x": x, "y": y, "life": 1.0, "decay": 0.08})

        if etype == "ball":
            # 球形：粒子向各方向均匀扩散，速度随机
            n = random.randint(80, 120)
            for _ in range(n):
                angle = random.uniform(0, 2 * math.pi)
                speed = random.uniform(2, 8)
                self._add_particle(x, y, angle, speed, color)

        elif etype == "ring":
            # 环形：粒子速度相近，形成圆环
            n = random.randint(60, 90)
            base_speed = random.uniform(4, 6)
            for _ in range(n):
                angle = random.uniform(0, 2 * math.pi)
                speed = base_speed + random.uniform(-0.5, 0.5)
                self._add_particle(x, y, angle, speed, color)

        elif etype == "double":
            # 双层：内圈慢速 + 外圈快速，两圈不同颜色
            inner_color = QColor(color)
            outer_color = QColor.fromHsv((color.hue() + 120) % 360, 220, 255)
            for _ in range(50):
                angle = random.uniform(0, 2 * math.pi)
                self._add_particle(x, y, angle, random.uniform(2, 4), inner_color)
            for _ in range(70):
                angle = random.uniform(0, 2 * math.pi)
                self._add_particle(x, y, angle, random.uniform(5, 9), outer_color)

        elif etype == "heart":
            # 心形：用心形参数方程生成粒子方向
            n = random.randint(70, 100)
            for i in range(n):
                t = (i / n) * 2 * math.pi
                # 心形参数方程：x=16sin³t, y=13cos t-5cos2t-2cos3t-cos4t
                hx = 16 * math.sin(t) ** 3
                hy = -(13 * math.cos(t) - 5 * math.cos(2 * t)
                       - 2 * math.cos(3 * t) - math.cos(4 * t))
                # 归一化方向 + 缩放速度
                mag = math.hypot(hx, hy)
                speed = random.uniform(4, 6)
                angle = math.atan2(hy, hx)
                self._add_particle(x, y, angle, speed, color)

        else:  # willow 柳树形
            # 柳树形：粒子向下垂落，重力大、寿命长，形成垂柳效果
            n = random.randint(60, 90)
            for _ in range(n):
                angle = random.uniform(-math.pi, 0)  # 向上及两侧
                speed = random.uniform(3, 6)
                p = self._add_particle(x, y, angle, speed, color,
                                       gravity=0.18, decay=0.006)
                if p:
                    p["size"] = random.uniform(2.5, 4.5)

    def _add_particle(self, x, y, angle, speed, color,
                      gravity: float = 0.08, decay: float = None):
        """生成一个粒子并加入列表。返回粒子字典以便定制。"""
        p = {
            "x": x, "y": y,
            "vx": math.cos(angle) * speed,
            "vy": math.sin(angle) * speed,
            "life": 1.0,
            "decay": decay if decay is not None else random.uniform(0.008, 0.016),
            "color": QColor(color),
            "size": random.uniform(2, 4),
            "gravity": gravity,
            "twinkle": random.random() < 0.3,  # 30% 粒子会闪烁
            "twinkle_phase": random.uniform(0, 2 * math.pi),
            "trail": [],  # 粒子拖尾
        }
        self._particles.append(p)
        return p

    def _tick(self):
        """每帧更新：发射新烟花、移动烟花弹、移动粒子、衰减生命。"""
        self._elapsed += 30

        # 随机发射新烟花（前 85% 持续时间内，频率更高 → 更绚烂）
        if self._elapsed < self._duration * 0.85 and random.random() < 0.28:
            self._launch_rocket()

        # 更新上升中的烟花弹
        for r in self._rockets[:]:
            r["trail"].append((r["x"], r["y"]))
            if len(r["trail"]) > 10:
                r["trail"].pop(0)
            r["y"] += r["vy"]
            r["vy"] += 0.18  # 减速效果
            # 到达目标高度或速度接近 0 → 爆炸
            if r["y"] <= r["target_y"] or r["vy"] >= 0:
                self._explode(r)
                self._rockets.remove(r)

        # 更新爆炸粒子
        for p in self._particles[:]:
            # 记录拖尾
            p["trail"].append((p["x"], p["y"]))
            if len(p["trail"]) > 5:
                p["trail"].pop(0)
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            p["vy"] += p["gravity"]  # 重力下落
            p["vx"] *= 0.992  # 空气阻力
            p["life"] -= p["decay"]
            p["twinkle_phase"] += 0.4
            if p["life"] <= 0:
                self._particles.remove(p)

        # 更新爆炸闪光
        for f in self._flashes[:]:
            f["life"] -= f["decay"]
            if f["life"] <= 0:
                self._flashes.remove(f)

        # 文字淡入淡出：前 0.8 秒淡入，最后 1 秒淡出
        if self._elapsed < 800:
            self._text_alpha = min(255, self._elapsed // 4)
        elif self._elapsed > self._duration - 1000:
            self._text_alpha = max(0, (self._duration - self._elapsed) // 4)
        else:
            self._text_alpha = 255

        # 时间到 → 停止定时器并关闭窗口
        if self._elapsed >= self._duration:
            self._timer.stop()
            self.close()

        self.update()  # 触发重绘

    # --------------------------- 键盘事件 --------------------------- #
    def keyPressEvent(self, event):
        """按 Esc 提前关闭烟花动画窗口。"""
        if event.key() == Qt.Key_Escape:
            self._timer.stop()
            self.close()
        else:
            super().keyPressEvent(event)

    # --------------------------- 绘制 --------------------------- #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 半透明黑色背景（覆盖屏幕）
        painter.fillRect(self.rect(), QColor(0, 0, 0, 180))

        # 绘制爆炸闪光（径向渐变白色，快速衰减）
        painter.setPen(Qt.NoPen)
        for f in self._flashes:
            radius = int(60 * (1.2 - f["life"] * 0.4))
            alpha = int(220 * f["life"])
            # 中心亮白 → 外缘透明
            for i in range(3):
                r = radius - i * 15
                if r <= 0:
                    break
                a = max(0, alpha - i * 70)
                painter.setBrush(QColor(255, 255, 255, a))
                painter.drawEllipse(int(f["x"]) - r, int(f["y"]) - r, r * 2, r * 2)

        # 绘制上升烟花弹及其尾迹
        for r in self._rockets:
            color = QColor(r["color"])
            # 尾迹
            for i, (tx, ty) in enumerate(r["trail"]):
                alpha = int(255 * (i / max(1, len(r["trail"]))) * 0.6)
                c = QColor(color)
                c.setAlpha(alpha)
                painter.setPen(Qt.NoPen)
                painter.setBrush(c)
                painter.drawEllipse(int(tx), int(ty), 3, 3)
            # 弹体（带白色高光）
            painter.setBrush(color)
            painter.drawEllipse(int(r["x"]), int(r["y"]), 6, 6)
            painter.setBrush(QColor(255, 255, 255, 200))
            painter.drawEllipse(int(r["x"]) + 1, int(r["y"]) + 1, 2, 2)

        # 绘制爆炸粒子（含拖尾 + 闪烁）
        painter.setPen(Qt.NoPen)
        for p in self._particles:
            # 拖尾
            for i, (tx, ty) in enumerate(p["trail"]):
                t_alpha = int(120 * p["life"] * (i / max(1, len(p["trail"]))))
                c = QColor(p["color"])
                c.setAlpha(t_alpha)
                painter.setBrush(c)
                ts = max(1, int(p["size"] * (i / max(1, len(p["trail"]))) * 0.7))
                painter.drawEllipse(int(tx), int(ty), ts, ts)
            # 粒子本体
            c = QColor(p["color"])
            if p["twinkle"]:
                # 闪烁：用 sin 波调制透明度
                twinkle_a = 0.5 + 0.5 * math.sin(p["twinkle_phase"])
                c.setAlpha(int(255 * p["life"] * twinkle_a))
            else:
                c.setAlpha(int(255 * p["life"]))
            painter.setBrush(c)
            painter.drawEllipse(
                int(p["x"]), int(p["y"]),
                int(p["size"]), int(p["size"]),
            )
            # 粒子核心高光（让粒子更亮）
            if p["life"] > 0.6:
                painter.setBrush(QColor(255, 255, 255, int(150 * p["life"])))
                painter.drawEllipse(
                    int(p["x"]) + 1, int(p["y"]) + 1, 1, 1,
                )

        # 中心文字：🎉 完成所有任务 🎉
        if self._text_alpha > 0:
            painter.setPen(QColor(255, 230, 120, self._text_alpha))
            font = QFont("Microsoft YaHei", 42, QFont.Bold)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, "🎉 完成所有任务 🎉")


# --------------------------------------------------------------------------- #
# 控制面板窗口（启动时显示，提供三个操作入口）
# --------------------------------------------------------------------------- #
class ControlWindow(QWidget):
    """启动时显示的控制面板：
    - 左侧：已存在的便签列表（双击显示，右键删除）
    - 右侧：新建便签 / 显示所有便签 / 退出程序 三个按钮
    点击右上角 × 不退出程序，只是隐藏本窗口，便签保留，托盘图标保留。"""

    def __init__(self, manager: "NoteManager", tray: QSystemTrayIcon):
        super().__init__()
        self._manager = manager
        self._tray = tray
        self._app = QApplication.instance()

        # 监听便签列表变更，自动刷新
        self._manager.listChanged.connect(self.refresh_list)

        self.setWindowTitle("桌面便签")
        self.resize(1000, 800)
        # 让控制窗口在任务栏显示图标
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self._build_ui()
        self._center_on_screen()

    def _build_ui(self):
        # 主水平布局：左侧便签列表 + 右侧操作区
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---- 左侧：便签列表 ----
        left_panel = QWidget()
        left_panel.setFixedWidth(600)
        left_panel.setStyleSheet("background: #fafafa;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(20, 20, 20, 20)
        left_layout.setSpacing(12)

        list_title = QLabel("便签列表")
        list_title.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #5d4037;"
        )
        left_layout.addWidget(list_title)

        list_subtitle = QLabel("双击便签可显示到桌面，右键可删除")
        list_subtitle.setStyleSheet("font-size: 11px; color: #9e9e9e;")
        left_layout.addWidget(list_subtitle)

        # 便签列表
        self.note_list = QListWidget()
        self.note_list.setStyleSheet(
            "QListWidget { background: #ffffff; border: 1px solid #e0e0e0;"
            "  border-radius: 6px; }"
            "QListWidget::item { border-bottom: 1px solid #f0f0f0; padding: 8px; }"
            "QListWidget::item:selected { background: #E3F2FD; color: #1565C0; }"
            "QListWidget::item:hover { background: #f5f5f5; }"
        )
        self.note_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.note_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.note_list.customContextMenuRequested.connect(self._on_list_context_menu)
        left_layout.addWidget(self.note_list, 1)

        main_layout.addWidget(left_panel)

        # ---- 右侧：操作区 ----
        right_panel = QWidget()
        right_panel.setStyleSheet("background: #ffffff;")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(40, 40, 40, 40)
        right_layout.setSpacing(16)

        # 标题
        title = QLabel("桌面便签")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 28px; font-weight: bold; color: #5d4037;")
        right_layout.addWidget(title)

        subtitle = QLabel("请选择操作")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("font-size: 13px; color: #9e9e9e;")
        right_layout.addWidget(subtitle)

        right_layout.addSpacing(20)

        # 三个按钮
        btn_style = (
            "QPushButton { background: #42A5F5; color: white; border: none;"
            "  border-radius: 8px; padding: 14px; font-size: 16px; }"
            "QPushButton:hover { background: #2196F3; }"
            "QPushButton:pressed { background: #1976D2; }"
        )
        btn_quit_style = (
            "QPushButton { background: #f5f5f5; color: #757575; border: 1px solid #e0e0e0;"
            "  border-radius: 8px; padding: 14px; font-size: 16px; }"
            "QPushButton:hover { background: #fafafa; color: #616161; border-color: #bdbdbd; }"
            "QPushButton:pressed { background: #eeeeee; }"
        )
        btn_danger_style = (
            "QPushButton { background: #ffffff; color: #e53935; border: 1px solid #ffcdd2;"
            "  border-radius: 8px; padding: 14px; font-size: 16px; }"
            "QPushButton:hover { background: #ffebee; border-color: #ef9a9a; }"
            "QPushButton:pressed { background: #ffcdd2; }"
        )

        btn_new = QPushButton("📝  新建便签")
        btn_new.setStyleSheet(btn_style)
        btn_new.setCursor(Qt.PointingHandCursor)
        btn_new.clicked.connect(self._on_new_note)
        right_layout.addWidget(btn_new)

        btn_show = QPushButton("📋  显示所有便签")
        btn_show.setStyleSheet(btn_style)
        btn_show.setCursor(Qt.PointingHandCursor)
        btn_show.clicked.connect(self._on_show_all)
        right_layout.addWidget(btn_show)

        btn_delete_all = QPushButton("🗑  删除所有便签")
        btn_delete_all.setStyleSheet(btn_danger_style)
        btn_delete_all.setCursor(Qt.PointingHandCursor)
        btn_delete_all.clicked.connect(self._on_delete_all)
        right_layout.addWidget(btn_delete_all)

        btn_quit = QPushButton("✕  退出程序")
        btn_quit.setStyleSheet(btn_quit_style)
        btn_quit.setCursor(Qt.PointingHandCursor)
        btn_quit.clicked.connect(self._on_quit)
        right_layout.addWidget(btn_quit)

        right_layout.addStretch()

        # 底部便签数量统计
        self.count_label = QLabel()
        self.count_label.setAlignment(Qt.AlignCenter)
        self.count_label.setStyleSheet("font-size: 11px; color: #bdbdbd;")
        right_layout.addWidget(self.count_label)

        main_layout.addWidget(right_panel, 1)

    def _center_on_screen(self):
        screen = self._app.desktop().screenGeometry()
        self.move(
            (screen.width() - self.width()) // 2,
            (screen.height() - self.height()) // 2,
        )

    # --------------------------- 便签列表 --------------------------- #
    def refresh_list(self):
        """刷新左侧便签列表。在新建/删除/显示窗口时调用。
        每项用 NoteListItemWidget 渲染：色条 + 状态方框 + 标题截止 + 已完成/未完成按钮。"""
        self.note_list.clear()
        notes = list(self._manager.notes.values())
        for note in notes:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, note.note_id)
            widget = NoteListItemWidget(note, self._manager)
            # setItemWidget 需要 sizeHint 匹配，否则 widget 会被截断
            item.setSizeHint(widget.sizeHint())
            self.note_list.addItem(item)
            self.note_list.setItemWidget(item, widget)

        # 更新数量统计
        self.count_label.setText(f"共 {len(notes)} 个便签")

    def _on_item_double_clicked(self, item: QListWidgetItem):
        """双击列表项：显示对应便签到桌面。"""
        note_id = item.data(Qt.UserRole)
        note = self._manager.notes.get(note_id)
        if note:
            note.showNormal()
            note.raise_()
            note.activateWindow()

    def _on_list_context_menu(self, pos):
        """右键菜单：显示/隐藏便签、删除便签。
        便签可见时第一项显示"隐藏便签"，不可见时显示"显示便签"。"""
        item = self.note_list.itemAt(pos)
        if item is None:
            return
        note_id = item.data(Qt.UserRole)
        note = self._manager.notes.get(note_id)
        if note is None:
            return

        visible = note.isVisible()

        menu = QMenu(self)
        act_toggle = menu.addAction("隐藏便签" if visible else "显示便签")
        menu.addSeparator()
        act_delete = menu.addAction("删除便签")

        action = menu.exec_(self.note_list.mapToGlobal(pos))
        if action == act_toggle:
            if visible:
                note.hide()
            else:
                note.showNormal()
                note.raise_()
                note.activateWindow()
        elif action == act_delete:
            note._on_delete(confirm=False)  # 控制窗口内删除不再二次弹窗确认
            self.refresh_list()

    # --------------------------- 按钮事件 --------------------------- #
    def _on_new_note(self):
        self._manager.create_new()
        self.refresh_list()

    def _on_show_all(self):
        self._manager.show_all()
        self.refresh_list()

    def _on_delete_all(self):
        """删除所有便签：先弹确认框，用户选"是"后才真正删除，
        删除后弹出烟花庆祝窗口显示"完成所有任务"。"""
        # 没有便签时直接返回，避免无意义弹窗
        if not self._manager.notes:
            QMessageBox.information(self, "提示", "当前没有任何便签。")
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除所有 {len(self._manager.notes)} 个便签吗？\n此操作不可恢复！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,  # 默认聚焦"否"，防止误点
        )
        if reply == QMessageBox.Yes:
            self._manager.delete_all()
            self.refresh_list()
            # 弹出烟花庆祝窗口（持有引用避免被 GC，5 秒后自动关闭）
            self._fireworks = FireworksWindow(duration_ms=5000)
            self._fireworks.start()

    def _on_quit(self):
        """退出程序：关闭所有便签 + 退出 + 托盘消失。"""
        self._manager.close_all()
        self._tray.hide()
        self._tray.deleteLater()
        self._app.quit()

    # --------------------------- 窗口事件 --------------------------- #
    def showEvent(self, event):
        """窗口显示时刷新便签列表。"""
        super().showEvent(event)
        self.refresh_list()

    def closeEvent(self, event):
        """点击右上角 × 不退出程序，只隐藏控制窗口。
        便签保留，托盘图标保留，程序继续运行。"""
        event.ignore()
        self.hide()
        # 提示用户程序仍在托盘运行
        self._tray.showMessage(
            "桌面便签",
            "程序已最小化到托盘，点击托盘图标可重新打开。",
            QSystemTrayIcon.Information,
            2000,
        )


# --------------------------------------------------------------------------- #
# 系统托盘 + 应用入口
# --------------------------------------------------------------------------- #
def make_tray_icon() -> QIcon:
    """用 QPainter 画一个黄色便签图标，避免依赖外部图片文件。"""
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QBrush(QColor("#FFF9C4")))
    p.setPen(QColor("#FBC02D"))
    p.drawRoundedRect(8, 6, 48, 52, 4, 4)
    p.setPen(QColor("#9e9e9e"))
    for i in range(3):
        y = 24 + i * 8
        p.drawLine(16, y, 48, y)
    p.end()
    return QIcon(pix)


# 单实例管道名（唯一标识本应用）
APP_SERVER_NAME = "DesktopStickyNotes_AppInstance"


class SingleInstanceGuard:
    """单实例守卫：用 QLocalServer/QLocalSocket 确保只有一个进程运行。
    第二个实例启动时，会通知第一个实例显示控制窗口，然后自己退出。"""

    def __init__(self):
        self._server = None
        self._on_wakeup = None

    def try_acquire(self, on_wakeup) -> bool:
        """尝试获取实例锁。返回 True 表示成功（首个实例），False 表示已有实例。
        on_wakeup: 收到第二个实例的唤醒请求时的回调。"""
        self._on_wakeup = on_wakeup
        socket = QLocalSocket()
        socket.connectToServer(APP_SERVER_NAME)
        if socket.waitForConnected(500):
            # 已有实例 → 发送唤醒消息，然后退出
            socket.write(b"WAKEUP")
            socket.flush()
            socket.waitForBytesWritten(500)
            socket.close()
            return False
        # 无已有实例 → 创建服务器
        socket.close()
        self._server = QLocalServer()
        # 先移除可能残留的旧管道（上次进程异常退出时）
        QLocalServer.removeServer(APP_SERVER_NAME)
        if not self._server.listen(APP_SERVER_NAME):
            return False
        self._server.newConnection.connect(self._on_new_connection)
        return True

    def _on_new_connection(self):
        """收到第二个实例的连接请求。"""
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        if socket.waitForReadyRead(300):
            data = bytes(socket.readAll())
            if data == b"WAKEUP" and self._on_wakeup:
                self._on_wakeup()
        socket.close()

    def cleanup(self):
        if self._server:
            self._server.close()
            self._server.removeServer(APP_SERVER_NAME)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关闭所有便签/控制窗口不退出，靠托盘退出

    # 加载应用图标（任务栏、Alt+Tab、系统托盘统一使用）
    # 用 resource_path 兼容 PyInstaller 打包后的资源路径
    icon_path = resource_path("app_icon.ico")
    if icon_path.exists():
        app_icon = QIcon(str(icon_path))
        app.setWindowIcon(app_icon)  # 所有窗口继承此图标（任务栏 + Alt+Tab）
    else:
        app_icon = make_tray_icon()  # 图标文件不存在时回退到 QPainter 绘制

    manager = NoteManager()
    manager.load()

    # ---- 单实例检测 ----
    # 第二个实例启动时，唤醒已有实例的控制窗口
    def _on_wakeup():
        if control_window is not None:
            control_window.showNormal()
            control_window.raise_()
            control_window.activateWindow()

    guard = SingleInstanceGuard()
    # 先声明 control_window，让 _on_wakeup 能引用到
    control_window = None
    if not guard.try_acquire(_on_wakeup):
        # 已有实例 → 它会收到消息显示控制窗口，我们直接退出
        return

    # 系统托盘（使用同一图标）
    tray = QSystemTrayIcon(app_icon, app)
    tray.setToolTip("桌面便签")

    # 创建控制窗口
    control_window = ControlWindow(manager, tray)

    # 托盘右键菜单
    menu = QMenu()
    act_new = menu.addAction("新建便签")
    act_show = menu.addAction("显示所有便签")
    act_panel = menu.addAction("打开控制面板")
    menu.addSeparator()
    act_quit = menu.addAction("退出")

    act_new.triggered.connect(manager.create_new)
    act_show.triggered.connect(manager.show_all)
    act_panel.triggered.connect(control_window.showNormal)
    act_quit.triggered.connect(control_window._on_quit)

    tray.setContextMenu(menu)
    # 双击托盘图标 → 显示控制窗口
    tray.activated.connect(
        lambda reason: control_window.showNormal()
        if reason == QSystemTrayIcon.DoubleClick
        else None
    )
    tray.show()

    # 恢复显示上次退出前已存在的便签到桌面（数据已由 manager.load() 读入内存，
    # 但 _create_from_data 不调用 show()，需在此显式恢复，否则重启后便签"消失"）
    manager.show_all()

    # 显示控制窗口（启动入口）
    control_window.show()
    control_window.raise_()
    control_window.activateWindow()

    # 关键：程序退出前显式清理托盘图标 + 单实例管道，防止残留
    def _cleanup():
        guard.cleanup()
        tray.hide()
        tray.deleteLater()
        # 删除所有便签窗口
        for note in list(manager.notes.values()):
            note.hide()
            note.deleteLater()
        manager.notes.clear()

    app.aboutToQuit.connect(_cleanup)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
