"""GUI 集成冒烟测试：真实创建窗口，驱动扫描/开始/取消流程（offscreen）。"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

from config import DEFAULT_FFMPEG, DEFAULT_FFPROBE, AppConfig
from ui_main import MainWindow

ROOT = Path(__file__).resolve().parent / "_guitest"
SRC = ROOT / "输入 中文"
OUT = ROOT / "输出 目录"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")


def pump(app: QApplication, seconds: float, until=None) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    app.processEvents()
    return until() if until is not None else True


def main() -> int:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    (SRC / "子目录").mkdir(parents=True)
    import subprocess

    for target, dur in ((SRC / "歌曲 一.ogg", 4), (SRC / "子目录" / "歌曲 二.flac", 3)):
        subprocess.run(
            [DEFAULT_FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
             "-c:a", "libvorbis" if target.suffix == ".ogg" else "flac", str(target)],
            check=True,
        )
    (SRC / "坏文件.opus").write_bytes(b"garbage" * 20)

    app = QApplication(sys.argv)
    cfg = AppConfig()
    cfg.ffmpeg_path = DEFAULT_FFMPEG
    cfg.ffprobe_path = DEFAULT_FFPROBE
    cfg.output_mode = "mirror"
    cfg.output_dir = str(OUT)
    cfg.recursive = True
    cfg.skip_existing = False
    cfg.workers = 2

    window = MainWindow(cfg)
    window.show()
    check("窗口标题正确", window.windowTitle() == "QQ音乐音频转MP3")
    check("表格列头正确", [
        window.table.horizontalHeaderItem(i).text() for i in range(5)
    ] == ["文件名", "状态", "进度", "耗时", "信息"])

    window.mode_combo.setCurrentIndex(window.mode_combo.findData("folder"))
    window.source_edit.setText(str(SRC))
    window.output_edit.setText(str(OUT))
    window.output_mode_combo.setCurrentIndex(window.output_mode_combo.findData("mirror"))
    window.recursive_check.setChecked(True)
    window.skip_check.setChecked(False)
    window.workers_spin.setValue(2)
    window._scan_source()
    check("扫描并加入 3 个任务", window.table.rowCount() == 3, f"rows={window.table.rowCount()}")

    window._start()
    finished = pump(app, 180, lambda: not window.engine.is_running)
    check("GUI 批量转换完成", finished)
    rows_text = [
        (window.table.item(r, 0).text(), window.table.item(r, 1).text())
        for r in range(window.table.rowCount())
    ]
    statuses = {name: status for name, status in rows_text}
    check("表格状态正确（2 成功 1 失败）",
          list(statuses.values()).count("成功") == 2 and list(statuses.values()).count("失败") == 1,
          str(statuses))
    produced = sorted(str(p.relative_to(OUT)) for p in OUT.rglob("*.mp3"))
    check("输出保留目录结构", len(produced) == 2 and any("子目录" in p for p in produced), str(produced))
    failed_info = [
        window.table.item(r, 4).text() for r in range(window.table.rowCount())
        if window.table.item(r, 1).text() == "失败"
    ]
    check("失败原因显示在信息列", bool(failed_info and failed_info[0]), failed_info[0][:80] if failed_info else "")

    # 跳过已存在验证
    window._clear_tasks()
    window._scan_source()
    window.skip_check.setChecked(True)
    window._scan_source()
    skipped = sum(
        1 for r in range(window.table.rowCount())
        if window.table.item(r, 1).text() == "跳过"
    )
    check("GUI 跳过已存在", skipped >= 2, f"skipped={skipped}")

    # 拖拽添加
    window._clear_tasks()
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(SRC))])
    drop = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    window.dropEvent(drop)
    check("拖拽文件夹添加任务", window.table.rowCount() >= 3, f"rows={window.table.rowCount()}")

    # 取消流程
    window._clear_tasks()
    long_src = SRC / "长音频.ogg"
    subprocess.run(
        [DEFAULT_FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=900",
         "-c:a", "libvorbis", str(long_src)], check=True,
    )
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("file"))
    window.source_edit.setText(str(long_src))
    window.skip_check.setChecked(False)
    window._scan_source()
    window._start()
    pump(app, 30, lambda: any(
        window.table.item(r, 1).text() == "转换中" for r in range(window.table.rowCount())))
    window.engine.cancel_all()
    cancelled = pump(app, 40, lambda: not window.engine.is_running)
    status = window.table.item(0, 1).text() if window.table.rowCount() else ""
    check("GUI 取消任务", cancelled and status == "已取消", f"status={status}")

    # 关闭窗口时清理子进程（自动确认对话框）
    from PySide6.QtWidgets import QMessageBox

    import ui_main

    window._clear_tasks()
    long_src2 = SRC / "长音频2.ogg"
    subprocess.run(
        [DEFAULT_FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=900",
         "-c:a", "libvorbis", str(long_src2)], check=True,
    )
    window.source_edit.setText(str(long_src2))
    window._scan_source()
    window._start()
    pump(app, 30, lambda: any(
        window.table.item(r, 1).text() == "转换中" for r in range(window.table.rowCount())))
    original = QMessageBox.question
    ui_main.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    try:
        window.close()
        closed = pump(app, 40, lambda: not window.engine.is_running)
        parts = list(OUT.rglob("*.part"))
        check("关闭窗口时确认并清理子进程", closed and not parts, f"closed={closed} parts={parts}")
    finally:
        ui_main.QMessageBox.question = original

    app.processEvents()
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\nGUI 测试：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1

if __name__ == "__main__":
    sys.exit(main())
