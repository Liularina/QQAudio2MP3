"""程序入口：初始化高 DPI、日志、配置与主窗口。"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from config import AppConfig, log_dir
from converter import find_ffmpeg
from ui_main import MainWindow
from utils import setup_logging


def _ensure_ffmpeg(cfg: AppConfig) -> None:
    """启动时检查 ffmpeg；不存在则提示用户手动选择并保存。"""
    if find_ffmpeg(cfg) is not None:
        return
    QMessageBox.warning(
        None,
        "未找到 ffmpeg",
        "未检测到可用的 ffmpeg.exe。\n\n"
        f"当前配置路径：{cfg.ffmpeg_path or '（空）'}\n\n"
        "请选择 ffmpeg.exe 所在位置（同目录应包含 ffprobe.exe）。\n"
        "也可以稍后在“设置”中修改。",
    )
    path, _ = QFileDialog.getOpenFileName(
        None, "选择 ffmpeg.exe", "", "可执行文件 (*.exe);;所有文件 (*.*)"
    )
    if path:
        cfg.ffmpeg_path = str(Path(path))
        sibling = Path(path).with_name("ffprobe.exe")
        if sibling.is_file():
            cfg.ffprobe_path = str(sibling)
        cfg.save()


def main() -> int:
    """创建 QApplication 与主窗口，进入事件循环。"""
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("QQAudio2MP3")
    app.setApplicationDisplayName("QQ音乐音频转MP3")
    app.setOrganizationName("QQAudio2MP3")

    log_file = setup_logging(log_dir() / "app.log")
    cfg = AppConfig.load()
    _ensure_ffmpeg(cfg)

    window = MainWindow(cfg)
    window.show()

    import logging

    logging.getLogger(__name__).info("应用启动，日志文件：%s", log_file)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
