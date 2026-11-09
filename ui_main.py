"""主界面：源选择、参数区、任务表格、设置对话框。"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import BITRATE_MODES, AppConfig, config_path, log_dir
from converter import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    ConversionEngine,
    TaskEntry,
    find_ffmpeg,
    plan_tasks,
)
from utils import (
    SUPPORTED_EXTS,
    format_elapsed,
    open_in_explorer,
    scan_audio_files,
)

logger = logging.getLogger(__name__)

AUDIO_FILTER = "音频文件 (" + " ".join(f"*{ext}" for ext in SUPPORTED_EXTS) + ");;所有文件 (*.*)"
BITRATE_LABELS: dict[str, str] = {
    "vbr": "VBR（-q:a 2，推荐）",
    "128": "CBR 128k",
    "192": "CBR 192k",
    "256": "CBR 256k",
    "320": "CBR 320k",
}
OUTPUT_LABELS: list[tuple[str, str]] = [
    ("same", "与源文件同目录"),
    ("custom", "指定统一输出目录"),
    ("mirror", "指定目录并保留目录结构"),
]
CONFLICT_LABELS: list[tuple[str, str]] = [
    ("rename", "自动重命名"),
    ("skip", "跳过"),
    ("overwrite", "覆盖"),
]

STATUS_COLORS = {
    STATUS_PENDING: "#666666",
    STATUS_RUNNING: "#1565c0",
    STATUS_DONE: "#2e7d32",
    STATUS_FAILED: "#c62828",
    STATUS_SKIPPED: "#8d6e63",
    STATUS_CANCELLED: "#8d6e63",
}


class SettingsDialog(QDialog):
    """设置对话框：ffmpeg/ffprobe 路径、输出目录、编码参数、并发数。"""

    def __init__(self, cfg: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self._cfg = cfg.copy()

        self.ffmpeg_edit = QLineEdit(self._cfg.ffmpeg_path)
        self.ffprobe_edit = QLineEdit(self._cfg.ffprobe_path)
        self.output_edit = QLineEdit(self._cfg.output_dir)

        self.output_combo = QComboBox()
        for value, label in OUTPUT_LABELS:
            self.output_combo.addItem(label, value)
        self.output_combo.setCurrentIndex(max(0, self.output_combo.findData(self._cfg.output_mode)))

        self.bitrate_combo = QComboBox()
        for value in BITRATE_MODES:
            self.bitrate_combo.addItem(BITRATE_LABELS[value], value)
        self.bitrate_combo.setCurrentIndex(max(0, self.bitrate_combo.findData(self._cfg.bitrate_mode)))

        self.conflict_combo = QComboBox()
        for value, label in CONFLICT_LABELS:
            self.conflict_combo.addItem(label, value)
        self.conflict_combo.setCurrentIndex(max(0, self.conflict_combo.findData(self._cfg.conflict_policy)))

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 8)
        self.workers_spin.setValue(self._cfg.workers)

        self.recursive_check = QCheckBox("扫描子文件夹（递归）")
        self.recursive_check.setChecked(self._cfg.recursive)
        self.skip_check = QCheckBox("跳过已存在的 MP3")
        self.skip_check.setChecked(self._cfg.skip_existing)
        self.meta_check = QCheckBox("保留元数据（-map_metadata 0）")
        self.meta_check.setChecked(self._cfg.keep_metadata)
        self.cover_check = QCheckBox("保留内嵌封面（失败自动忽略）")
        self.cover_check.setChecked(self._cfg.keep_cover)
        self.copy_check = QCheckBox("输入为 MP3 时直接复制")
        self.copy_check.setChecked(self._cfg.copy_mp3)

        form = QFormLayout()
        form.addRow("ffmpeg 路径", self._path_row(self.ffmpeg_edit, "选择 ffmpeg.exe", "可执行文件 (*.exe);;所有文件 (*.*)"))
        form.addRow("ffprobe 路径", self._path_row(self.ffprobe_edit, "选择 ffprobe.exe", "可执行文件 (*.exe);;所有文件 (*.*)"))
        form.addRow("输出方式", self.output_combo)
        form.addRow("输出目录", self._path_row(self.output_edit, "选择输出目录", ""))
        form.addRow("MP3 码率", self.bitrate_combo)
        form.addRow("同名文件策略", self.conflict_combo)
        form.addRow("并发数（1-8）", self.workers_spin)
        form.addRow("", self.recursive_check)
        form.addRow("", self.skip_check)
        form.addRow("", self.meta_check)
        form.addRow("", self.cover_check)
        form.addRow("", self.copy_check)

        hint = QLabel("配置文件位置：" + str(config_path()))
        hint.setStyleSheet("color:#777;")
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _path_row(self, edit: QLineEdit, title: str, file_filter: str) -> QWidget:
        """构造“输入框 + 浏览按钮”的横向组合。"""
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        button = QPushButton("浏览…")

        def browse() -> None:
            if file_filter:
                path, _ = QFileDialog.getOpenFileName(self, title, edit.text(), file_filter)
            else:
                path = QFileDialog.getExistingDirectory(self, title, edit.text())
            if path:
                edit.setText(str(Path(path)))
                if edit is self.ffmpeg_edit and not self.ffprobe_edit.text().strip():
                    sibling = Path(path).with_name("ffprobe.exe")
                    if sibling.is_file():
                        self.ffprobe_edit.setText(str(sibling))

        button.clicked.connect(browse)
        row.addWidget(button)
        return wrapper

    def result_config(self) -> AppConfig:
        """把对话框取值合并回配置对象。"""
        cfg = self._cfg.copy()
        cfg.ffmpeg_path = self.ffmpeg_edit.text().strip()
        cfg.ffprobe_path = self.ffprobe_edit.text().strip()
        cfg.output_dir = self.output_edit.text().strip()
        cfg.output_mode = str(self.output_combo.currentData())
        cfg.bitrate_mode = str(self.bitrate_combo.currentData())
        cfg.conflict_policy = str(self.conflict_combo.currentData())
        cfg.workers = int(self.workers_spin.value())
        cfg.recursive = self.recursive_check.isChecked()
        cfg.skip_existing = self.skip_check.isChecked()
        cfg.keep_metadata = self.meta_check.isChecked()
        cfg.keep_cover = self.cover_check.isChecked()
        cfg.copy_mp3 = self.copy_check.isChecked()
        if not cfg.ffprobe_path and cfg.ffmpeg_path:
            sibling = Path(cfg.ffmpeg_path).with_name("ffprobe.exe")
            if sibling.is_file():
                cfg.ffprobe_path = str(sibling)
        return cfg.normalized()

    def _on_save(self) -> None:
        """保存时校验 ffmpeg 是否存在。"""
        cfg = self.result_config()
        if cfg.ffmpeg_path and not Path(cfg.ffmpeg_path).is_file():
            answer = QMessageBox.question(
                self,
                "ffmpeg 路径无效",
                "当前 ffmpeg 路径不存在，仍要保存吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._cfg = cfg
        self.accept()


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self.cfg = cfg.copy()
        self.engine = ConversionEngine(self)
        self._row_by_tid: dict[int, int] = {}
        self._progress_bars: dict[int, QProgressBar] = {}
        self._tasks: dict[int, TaskEntry] = {}
        self._next_tid = 0
        self._last_output: Path | None = None
        self._closing = False
        self._total_timer = QTimer(self)
        self._total_timer.setInterval(300)
        self._total_timer.timeout.connect(self._update_total_progress)

        self.setWindowTitle("QQ音乐音频转MP3")
        self.resize(1040, 680)
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_engine()
        self._apply_config_to_ui()
        self._update_buttons()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        """构建全部控件布局。"""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(8)

        title = QLabel("QQ音乐音频转MP3")
        font = QFont()
        font.setPointSize(15)
        font.setBold(True)
        title.setFont(font)
        root.addWidget(title)

        root.addWidget(self._build_source_group())
        root.addWidget(self._build_param_group())
        root.addWidget(self._build_table(), 1)
        root.addLayout(self._build_bottom())

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_label = QLabel("就绪")
        status.addWidget(self.status_label, 1)
        self.ffmpeg_label = QLabel("")
        status.addPermanentWidget(self.ffmpeg_label)
        self._refresh_ffmpeg_label()

    def _build_source_group(self) -> QWidget:
        """顶部：源路径、输出目录、操作按钮。"""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row1 = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("单个文件", "file")
        self.mode_combo.addItem("文件夹批量", "folder")
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("选择音频文件，或把文件/文件夹拖拽到窗口")
        self.browse_source_btn = QPushButton("浏览…")
        self.browse_source_btn.clicked.connect(self._browse_source)
        self.scan_btn = QPushButton("扫描/添加")
        self.scan_btn.clicked.connect(self._scan_source)
        row1.addWidget(QLabel("源："))
        row1.addWidget(self.mode_combo)
        row1.addWidget(self.source_edit, 1)
        row1.addWidget(self.browse_source_btn)
        row1.addWidget(self.scan_btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择指定输出目录（输出方式为“同目录”时可留空）")
        self.output_edit.editingFinished.connect(self._on_params_changed)
        self.output_browse_btn = QPushButton("输出目录…")
        self.output_browse_btn.clicked.connect(self._browse_output)
        self.start_btn = QPushButton("开始转换")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.cancel_btn = QPushButton("取消全部")
        self.cancel_btn.clicked.connect(self._cancel_all)
        for button in (self.start_btn, self.pause_btn, self.cancel_btn):
            button.setMinimumWidth(92)
        row2.addWidget(QLabel("输出："))
        row2.addWidget(self.output_edit, 1)
        row2.addWidget(self.output_browse_btn)
        row2.addWidget(self.start_btn)
        row2.addWidget(self.pause_btn)
        row2.addWidget(self.cancel_btn)
        layout.addLayout(row2)
        return box

    def _build_param_group(self) -> QWidget:
        """中部：码率、递归、跳过、并发等参数。"""
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.bitrate_combo = QComboBox()
        for value in BITRATE_MODES:
            self.bitrate_combo.addItem(BITRATE_LABELS[value], value)
        self.bitrate_combo.currentIndexChanged.connect(self._on_params_changed)

        self.output_mode_combo = QComboBox()
        for value, label in OUTPUT_LABELS:
            self.output_mode_combo.addItem(label, value)
        self.output_mode_combo.currentIndexChanged.connect(self._on_params_changed)

        self.recursive_check = QCheckBox("包含子文件夹")
        self.recursive_check.stateChanged.connect(self._on_params_changed)
        self.skip_check = QCheckBox("跳过已存在")
        self.skip_check.stateChanged.connect(self._on_params_changed)
        self.meta_check = QCheckBox("保留元数据")
        self.meta_check.stateChanged.connect(self._on_params_changed)
        self.cover_check = QCheckBox("保留封面")
        self.cover_check.stateChanged.connect(self._on_params_changed)
        self.copy_check = QCheckBox("MP3 直接复制")
        self.copy_check.stateChanged.connect(self._on_params_changed)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 8)
        self.workers_spin.setSuffix(" 线程")
        self.workers_spin.valueChanged.connect(self._on_params_changed)

        layout.addWidget(QLabel("码率："))
        layout.addWidget(self.bitrate_combo)
        layout.addWidget(QLabel("输出方式："))
        layout.addWidget(self.output_mode_combo)
        layout.addWidget(self.recursive_check)
        layout.addWidget(self.skip_check)
        layout.addWidget(self.meta_check)
        layout.addWidget(self.cover_check)
        layout.addWidget(self.copy_check)
        layout.addStretch(1)
        layout.addWidget(QLabel("并发："))
        layout.addWidget(self.workers_spin)
        return box

    def _build_table(self) -> QTableWidget:
        """任务表格。"""
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["文件名", "状态", "进度", "耗时", "信息"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 84)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 84)
        return self.table

    def _build_bottom(self) -> QHBoxLayout:
        """底部：总进度、打开输出目录、导出日志、设置。"""
        row = QHBoxLayout()
        self.total_label = QLabel("总进度：0/0")
        self.total_bar = QProgressBar()
        self.total_bar.setRange(0, 100)
        self.total_bar.setValue(0)
        self.total_bar.setFormat("%p%")
        self.total_bar.setMinimumWidth(220)
        self.open_btn = QPushButton("打开输出目录")
        self.open_btn.clicked.connect(self._open_output)
        self.export_btn = QPushButton("导出日志")
        self.export_btn.clicked.connect(self._export_log)
        self.clear_btn = QPushButton("清空列表")
        self.clear_btn.clicked.connect(self._clear_tasks)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.clicked.connect(self._open_settings)
        row.addWidget(self.total_label)
        row.addWidget(self.total_bar, 1)
        for button in (self.open_btn, self.export_btn, self.clear_btn, self.settings_btn):
            row.addWidget(button)
        return row

    # -------------------------------------------------------------- 配置同步
    def _apply_config_to_ui(self) -> None:
        """把配置写入界面控件（不触发自动保存风暴）。"""
        self._loading = True
        try:
            self.bitrate_combo.setCurrentIndex(max(0, self.bitrate_combo.findData(self.cfg.bitrate_mode)))
            self.output_mode_combo.setCurrentIndex(
                max(0, self.output_mode_combo.findData(self.cfg.output_mode))
            )
            self.recursive_check.setChecked(self.cfg.recursive)
            self.skip_check.setChecked(self.cfg.skip_existing)
            self.meta_check.setChecked(self.cfg.keep_metadata)
            self.cover_check.setChecked(self.cfg.keep_cover)
            self.copy_check.setChecked(self.cfg.copy_mp3)
            self.workers_spin.setValue(self.cfg.workers)
            self.output_edit.setText(self.cfg.output_dir)
            self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(self.cfg.last_source_mode)))
            if self.cfg.last_source and Path(self.cfg.last_source).exists():
                self.source_edit.setText(self.cfg.last_source)
        finally:
            self._loading = False

    def _collect_ui_config(self) -> AppConfig:
        """把界面取值合并到配置副本。"""
        cfg = self.cfg.copy()
        cfg.bitrate_mode = str(self.bitrate_combo.currentData())
        cfg.output_mode = str(self.output_mode_combo.currentData())
        cfg.recursive = self.recursive_check.isChecked()
        cfg.skip_existing = self.skip_check.isChecked()
        cfg.keep_metadata = self.meta_check.isChecked()
        cfg.keep_cover = self.cover_check.isChecked()
        cfg.copy_mp3 = self.copy_check.isChecked()
        cfg.workers = int(self.workers_spin.value())
        cfg.output_dir = self.output_edit.text().strip()
        cfg.last_source = self.source_edit.text().strip()
        cfg.last_source_mode = str(self.mode_combo.currentData())
        return cfg.normalized()

    def _on_params_changed(self, *_args: object) -> None:
        """参数变化时立即持久化。"""
        if getattr(self, "_loading", False):
            return
        self.cfg = self._collect_ui_config()
        self.cfg.save()

    def _refresh_ffmpeg_label(self) -> None:
        """状态栏显示 ffmpeg 状态。"""
        ffmpeg = find_ffmpeg(self.cfg)
        if ffmpeg is None:
            self.ffmpeg_label.setText("ffmpeg：未找到")
            self.ffmpeg_label.setStyleSheet("color:#c62828;")
        else:
            self.ffmpeg_label.setText(f"ffmpeg：{ffmpeg}")
            self.ffmpeg_label.setStyleSheet("color:#2e7d32;")

    # -------------------------------------------------------------- 引擎信号
    def _connect_engine(self) -> None:
        """连接后台引擎信号。"""
        self.engine.task_changed.connect(self._on_task_changed)
        self.engine.log_message.connect(self._append_log)
        self.engine.state_changed.connect(self._on_state_changed)
        self.engine.all_finished.connect(self._on_all_finished)

    def _on_task_changed(self, task: TaskEntry) -> None:
        """单个任务状态变化。"""
        self._tasks[task.tid] = task
        row = self._row_by_tid.get(task.tid)
        if row is None:
            return
        self._fill_row(row, task)
        self._update_total_progress()

    def _append_log(self, text: str) -> None:
        """把日志写入状态栏与日志文件。"""
        self.status_label.setText(text.replace("\n", " ")[:160])

    def _on_state_changed(self, state: str) -> None:
        """引擎状态变化时刷新按钮。"""
        self._update_buttons()
        if state == "running":
            self.pause_btn.setText("暂停")
            self.status_label.setText("转换中…")
        elif state == "paused":
            self.pause_btn.setText("继续")
            self.status_label.setText("已暂停：正在执行的任务将完成，不再启动新任务")
        elif state == "finished":
            self.status_label.setText("全部任务完成")
        elif state == "cancelled":
            self.status_label.setText("已取消")

    def _on_all_finished(self) -> None:
        """全部结束时刷新汇总。"""
        self._total_timer.stop()
        self._last_output = self.engine.last_output_dir or self._last_output
        self._update_total_progress()
        self._update_buttons()
        done = sum(1 for task in self._tasks.values() if task.status == STATUS_DONE)
        failed = sum(1 for task in self._tasks.values() if task.status == STATUS_FAILED)
        skipped = sum(1 for task in self._tasks.values() if task.status in (STATUS_SKIPPED, STATUS_CANCELLED))
        self.status_label.setText(f"完成 {done} 个，失败 {failed} 个，跳过/取消 {skipped} 个")

    def _update_buttons(self) -> None:
        """根据引擎状态切换按钮可用性。"""
        running = self.engine.is_running
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.cancel_btn.setEnabled(running)
        self.scan_btn.setEnabled(not running)
        self.browse_source_btn.setEnabled(not running)
        self.clear_btn.setEnabled(not running)
        self.settings_btn.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        if running:
            self.pause_btn.setText("继续" if self.engine.is_paused else "暂停")

    # -------------------------------------------------------------- 任务表格
    def _append_row(self, task: TaskEntry) -> None:
        """为任务追加一行。"""
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._row_by_tid[task.tid] = row
        self._tasks[task.tid] = task

        name_item = QTableWidgetItem(task.src.name)
        name_item.setToolTip(f"源：{task.src}\n输出：{task.dst}")
        self.table.setItem(row, 0, name_item)

        status_item = QTableWidgetItem(task.status)
        status_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 1, status_item)

        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setFormat("%p%")
        bar.setMaximumHeight(20)
        self.table.setCellWidget(row, 2, bar)
        self._progress_bars[task.tid] = bar

        elapsed_item = QTableWidgetItem("")
        elapsed_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 3, elapsed_item)

        info_item = QTableWidgetItem(task.message)
        info_item.setToolTip(task.message)
        self.table.setItem(row, 4, info_item)

        self._fill_row(row, task)

    def _fill_row(self, row: int, task: TaskEntry) -> None:
        """用任务快照刷新一行。"""
        name_item = self.table.item(row, 0)
        if name_item is not None:
            name_item.setToolTip(f"源：{task.src}\n输出：{task.dst}")
        status_item = self.table.item(row, 1)
        if status_item is not None:
            status_item.setText(task.status)
            status_item.setForeground(QColor(STATUS_COLORS.get(task.status, "#333333")))
        bar = self._progress_bars.get(task.tid)
        if bar is not None:
            value = int(max(0.0, min(100.0, task.progress)) * 10)
            bar.setValue(value)
            if task.status == STATUS_DONE:
                bar.setFormat("100%")
            elif task.status == STATUS_SKIPPED or task.status == STATUS_CANCELLED:
                bar.setFormat(task.status)
                bar.setValue(0)
            elif task.status == STATUS_FAILED:
                bar.setFormat("失败")
            elif task.status == STATUS_RUNNING and not task.duration:
                bar.setFormat("进行中")
            else:
                bar.setFormat("%p%")
        elapsed_item = self.table.item(row, 3)
        if elapsed_item is not None:
            seconds = task.elapsed
            if task.status == STATUS_RUNNING and task.started_at:
                seconds = time.time() - task.started_at
            elapsed_item.setText(format_elapsed(seconds) if seconds else "")
        info_item = self.table.item(row, 4)
        if info_item is not None:
            text = task.message.replace("\n", " | ")
            info_item.setText(text)
            info_item.setToolTip(task.message)

    def _update_total_progress(self) -> None:
        """按任务数量计算总进度。"""
        if not self._tasks:
            self.total_label.setText("总进度：0/0")
            self.total_bar.setValue(0)
            return
        tasks = list(self._tasks.values())
        finished = [
            t
            for t in tasks
            if t.status in (STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED, STATUS_CANCELLED)
        ]
        running = [t for t in tasks if t.status == STATUS_RUNNING]
        total = len(tasks)
        accumulated = len(finished) * 100.0 + sum(t.progress for t in running)
        percent = accumulated / total if total else 0.0
        self.total_bar.setValue(int(max(0.0, min(100.0, percent))))
        self.total_label.setText(f"总进度：{len(finished)}/{total}")

    # -------------------------------------------------------------- 操作
    def _current_mode(self) -> str:
        """当前源模式：file / folder。"""
        return str(self.mode_combo.currentData())

    def _browse_source(self) -> None:
        """浏览选择源文件或源文件夹。"""
        current = self.source_edit.text().strip()
        start = current if current and Path(current).exists() else str(Path.home())
        if self._current_mode() == "file":
            path, _ = QFileDialog.getOpenFileName(self, "选择音频文件", start, AUDIO_FILTER)
        else:
            path = QFileDialog.getExistingDirectory(self, "选择源文件夹", start)
        if path:
            self.source_edit.setText(str(Path(path)))
            self._scan_source()

    def _browse_output(self) -> None:
        """浏览选择输出目录。"""
        current = self.output_edit.text().strip()
        start = current if current and Path(current).exists() else str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", start)
        if path:
            self.output_edit.setText(str(Path(path)))
            if str(self.output_mode_combo.currentData()) == "same":
                self.output_mode_combo.setCurrentIndex(
                    max(0, self.output_mode_combo.findData("custom"))
                )
            self._on_params_changed()

    def _resolve_sources(self, raw: str, mode: str) -> tuple[list[Path], Path | None, str]:
        """把界面输入解析为源文件列表；返回 (文件, 基准目录, 错误)。"""
        path = Path(raw) if raw else None
        if path is None or not str(path).strip():
            return [], None, "请先选择源文件或源文件夹"
        if mode == "file":
            if not path.is_file():
                return [], None, f"文件不存在：{path}"
            if path.suffix.lower() not in SUPPORTED_EXTS:
                return [], None, f"不支持的音频格式：{path.suffix}"
            return [path], path.parent, ""
        if not path.is_dir():
            return [], None, f"文件夹不存在：{path}"
        files = scan_audio_files(path, self.recursive_check.isChecked())
        if not files:
            return [], None, "该文件夹中未找到支持的音频文件"
        return files, path, ""

    def _scan_source(self) -> None:
        """扫描源并加入任务列表。"""
        if self.engine.is_running:
            return
        cfg = self._collect_ui_config()
        raw = self.source_edit.text().strip()
        mode = self._current_mode()
        sources, base_dir, error = self._resolve_sources(raw, mode)
        if error:
            QMessageBox.warning(self, "无法添加任务", error)
            return
        self._add_sources(sources, base_dir, cfg)

    def _add_sources(self, sources: list[Path], base_dir: Path | None, cfg: AppConfig) -> None:
        """规划并追加任务到表格。"""
        if cfg.output_mode != "same" and not cfg.output_dir:
            QMessageBox.warning(
                self,
                "缺少输出目录",
                "当前输出方式需要指定输出目录，请先选择。",
            )
            return
        plan = plan_tasks(sources, cfg, base_dir, start_id=self._next_tid)
        for entry in plan.tasks + plan.skipped:
            self._next_tid = max(self._next_tid, entry.tid + 1)
            self._append_row(entry)
        if plan.output_dir:
            self._last_output = plan.output_dir
        elif sources:
            self._last_output = sources[0].parent
        self._update_total_progress()
        added = len(plan.tasks)
        skipped = len(plan.skipped)
        message = f"已添加 {added} 个任务"
        if skipped:
            message += f"，{skipped} 个已跳过"
        self.status_label.setText(message)
        if self.table.rowCount():
            self.table.scrollToBottom()

    def _start(self) -> None:
        """开始转换：未添加任务时先扫描当前源。"""
        if self.engine.is_running:
            return
        cfg = self._collect_ui_config()
        self.cfg = cfg
        self.cfg.save()

        if not self._tasks:
            sources, base_dir, error = self._resolve_sources(
                self.source_edit.text().strip(), self._current_mode()
            )
            if error:
                QMessageBox.warning(self, "无法开始", error)
                return
            self._add_sources(sources, base_dir, cfg)

        pending = [task for task in self._tasks.values() if task.status == STATUS_PENDING]
        if not pending:
            answer = QMessageBox.question(
                self,
                "没有待转换任务",
                "当前没有“等待”状态的任务。是否清空后重新扫描源并开始？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return
            self._clear_tasks()
            sources, base_dir, error = self._resolve_sources(
                self.source_edit.text().strip(), self._current_mode()
            )
            if error:
                QMessageBox.warning(self, "无法开始", error)
                return
            self._add_sources(sources, base_dir, cfg)
            pending = [task for task in self._tasks.values() if task.status == STATUS_PENDING]
            if not pending:
                QMessageBox.information(self, "没有任务", "没有可转换的文件。")
                return

        if cfg.skip_existing:
            for task in pending:
                if task.dst.exists():
                    task.status = STATUS_SKIPPED
                    task.message = "目标文件已存在"
                    task.finished_at = time.time()
                    row = self._row_by_tid.get(task.tid)
                    if row is not None:
                        self._fill_row(row, task)
            pending = [task for task in pending if task.status == STATUS_PENDING]
            if not pending:
                QMessageBox.information(self, "全部跳过", "所有任务的目标文件都已存在。")
                self._update_total_progress()
                return

        from converter import TaskPlan

        if cfg.output_mode == "same":
            plan_output = pending[0].dst.parent
        else:
            plan_output = Path(cfg.output_dir) if cfg.output_dir else None
        plan = TaskPlan(tasks=pending, output_dir=plan_output)
        count, error = self.engine.start(plan, cfg)
        if error:
            QMessageBox.warning(self, "无法开始", error)
            self._update_buttons()
            return
        if count:
            self._total_timer.start()
            self.status_label.setText(f"已启动 {count} 个任务")

    def _toggle_pause(self) -> None:
        """暂停/继续切换。"""
        if not self.engine.is_running:
            return
        if self.engine.is_paused:
            self.engine.resume()
        else:
            self.engine.pause()
        self._update_buttons()

    def _cancel_all(self) -> None:
        """取消全部任务。"""
        if not self.engine.is_running:
            return
        answer = QMessageBox.question(
            self,
            "取消全部",
            "确定要取消所有任务吗？正在运行的 ffmpeg 进程会被终止。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.engine.cancel_all()
            self._update_buttons()

    def _clear_tasks(self) -> None:
        """清空任务列表与引擎记录。"""
        if self.engine.is_running:
            return
        self.engine.clear()
        self.table.setRowCount(0)
        self._row_by_tid.clear()
        self._progress_bars.clear()
        self._tasks.clear()
        self._next_tid = 0
        self._update_total_progress()
        self.status_label.setText("列表已清空")

    def _open_output(self) -> None:
        """打开输出目录。"""
        target = (
            self.engine.last_output_dir
            or (Path(self.cfg.output_dir) if self.cfg.output_dir else None)
            or self._last_output
        )
        if target is None:
            QMessageBox.information(self, "没有输出目录", "还没有生成任何输出文件。")
            return
        if not target.exists():
            QMessageBox.information(self, "目录不存在", f"目录不存在：\n{target}")
            return
        if not open_in_explorer(target):
            QMessageBox.warning(self, "打开失败", f"无法打开目录：\n{target}")

    def _export_log(self) -> None:
        """导出日志与任务汇总到用户选择的文件。"""
        default_name = f"QQAudio2MP3_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", str(Path.home() / default_name), "文本文件 (*.txt)"
        )
        if not path:
            return
        lines = [
            f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"ffmpeg：{self.cfg.ffmpeg_path}",
            f"ffprobe：{self.cfg.ffprobe_path}",
            f"码率模式：{self.cfg.bitrate_mode}，并发：{self.cfg.workers}",
            "-" * 60,
            "任务列表：",
        ]
        for task in self._tasks.values():
            lines.append(
                f"[{task.status}] {task.src} -> {task.dst} | 进度 {task.progress:.1f}% | "
                f"耗时 {format_elapsed(task.elapsed)} | {task.message}"
            )
        app_log = log_dir() / "app.log"
        if app_log.is_file():
            lines += ["", "=" * 60, "程序日志：", ""]
            try:
                lines.append(app_log.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                lines.append("（读取日志失败）")
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"写入文件失败：{exc}")
            return
        self.status_label.setText(f"日志已导出：{path}")

    def _open_settings(self) -> None:
        """打开设置对话框。"""
        if self.engine.is_running:
            return
        dialog = SettingsDialog(self._collect_ui_config(), self)
        if dialog.exec() == QDialog.Accepted:
            self.cfg = dialog.result_config()
            self.cfg.save()
            self._apply_config_to_ui()
            self._refresh_ffmpeg_label()
            self.status_label.setText("设置已保存")
            if find_ffmpeg(self.cfg) is None:
                QMessageBox.warning(
                    self,
                    "ffmpeg 不可用",
                    "当前 ffmpeg 路径无效，转换将无法进行。请在设置中重新选择。",
                )

    # -------------------------------------------------------------- 拖拽
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """接受文件/文件夹拖拽。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        """拖入文件或文件夹时直接添加任务。"""
        if self.engine.is_running:
            self.status_label.setText("转换进行中，暂不能添加任务")
            return
        urls = [url for url in event.mimeData().urls() if url.isLocalFile()]
        if not urls:
            return
        event.acceptProposedAction()
        cfg = self._collect_ui_config()
        sources: list[Path] = []
        base_dir: Path | None = None
        folders = 0
        for url in urls:
            path = Path(url.toLocalFile())
            if path.is_dir():
                folders += 1
                base_dir = path
                sources.extend(scan_audio_files(path, cfg.recursive))
            elif path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
                sources.append(path)
                base_dir = base_dir or path.parent
        if not sources:
            self.status_label.setText("拖入的内容中没有支持的音频文件")
            return
        unique_sources: list[Path] = []
        seen: set[Path] = set()
        for item in sources:
            key = item.resolve()
            if key not in seen:
                seen.add(key)
                unique_sources.append(item)
        if folders > 1:
            base_dir = None
        single_file = len(unique_sources) == 1 and folders == 0
        if single_file:
            self.source_edit.setText(str(unique_sources[0]))
            self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData("file")))
        else:
            self.source_edit.setText(str(base_dir or ""))
            self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData("folder")))
        self._add_sources(unique_sources, base_dir, cfg)
        self._on_params_changed()

    # -------------------------------------------------------------- 关闭
    def closeEvent(self, event) -> None:  # type: ignore[override]
        """有任务运行时确认并清理子进程，等待后台线程退出后再关闭。"""
        if self.engine.is_running and not self._closing:
            answer = QMessageBox.question(
                self,
                "任务正在运行",
                "还有任务正在转换。关闭窗口将终止所有任务，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._closing = True
            self._total_timer.stop()
            self.engine.cancel_all()
            self.engine.shutdown(wait=False)
            self.status_label.setText("正在停止任务并退出…")
            if not hasattr(self, "_close_timer"):
                self._close_timer = QTimer(self)
                self._close_timer.setInterval(150)
                self._close_timer.timeout.connect(self._wait_close)
            self._close_timer.start()
            event.ignore()
            return
        if self._closing and self.engine.is_running:
            event.ignore()
            return
        if not self._closing:
            self.engine.shutdown(wait=False)
        try:
            self.cfg = self._collect_ui_config()
            self.cfg.save()
        except Exception:  # 退出阶段不应因保存配置失败而阻止关闭
            logger.exception("退出时保存配置失败")
        event.accept()

    def _wait_close(self) -> None:
        """等待转换线程结束后再次触发关闭。"""
        if self.engine.is_running:
            return
        self._close_timer.stop()
        self.close()
