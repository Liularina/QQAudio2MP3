"""通用工具：扩展名扫描、路径处理、时间格式化、日志初始化。"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

SUPPORTED_EXTS: tuple[str, ...] = (
    ".ogg",
    ".flac",
    ".m4a",
    ".wav",
    ".ape",
    ".wma",
    ".aac",
    ".opus",
    ".mp3",
    ".aiff",
)

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def is_supported_file(path: Path) -> bool:
    """判断文件扩展名是否属于支持的音频格式。"""
    return path.suffix.lower() in SUPPORTED_EXTS


def scan_audio_files(root: Path, recursive: bool) -> list[Path]:
    """扫描目录下的音频文件，返回按路径排序的列表。"""
    results: list[Path] = []
    try:
        if recursive:
            for current, _dirs, files in os.walk(root):
                for name in files:
                    candidate = Path(current) / name
                    if is_supported_file(candidate):
                        results.append(candidate)
        else:
            for entry in root.iterdir():
                if entry.is_file() and is_supported_file(entry):
                    results.append(entry)
    except OSError:
        pass
    results.sort(key=lambda p: str(p).lower())
    return results


def format_elapsed(seconds: float) -> str:
    """把秒数格式化为便于阅读的耗时文本。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:d}:{sec:02d}"


def format_duration(seconds: float | None) -> str:
    """把音频时长格式化为 mm:ss。"""
    if not seconds or seconds <= 0:
        return "--:--"
    total = int(seconds)
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def unique_path(path: Path) -> Path:
    """在目标已存在时生成“名称 (n).ext”形式的唯一路径。"""
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def tail_lines(text: str, count: int = 20) -> str:
    """取文本最后若干行。"""
    if not text:
        return ""
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def open_in_explorer(path: Path) -> bool:
    """在资源管理器中打开目录或定位文件，成功返回 True。"""
    try:
        target = path if path.is_dir() else path.parent
        if not target.exists():
            return False
        if sys.platform == "win32":
            if path.is_file():
                subprocess.Popen(
                    ["explorer", "/select,", str(path)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
            return True
        subprocess.Popen(["xdg-open", str(target)])
        return True
    except (OSError, ValueError):
        return False


def setup_logging(log_file: Path | None = None, level: int = logging.INFO) -> Path:
    """初始化根日志：滚动文件 + 控制台，返回日志文件路径。"""
    from config import log_dir

    target = log_file or (log_dir() / "app.log")
    target.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    already = any(
        isinstance(handler, RotatingFileHandler)
        and Path(getattr(handler, "baseFilename", "")) == target
        for handler in root.handlers
    )
    if not already:
        file_handler = RotatingFileHandler(
            target, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(file_handler)

        # 打包为 --windowed 后 sys.stderr 可能为 None，此时不添加控制台处理器。
        if sys.stderr is not None:
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            root.addHandler(console)
    return target
