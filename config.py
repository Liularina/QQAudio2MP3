"""应用配置：默认路径、持久化到 %APPDATA%\\QQAudio2MP3\\config.json。"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

APP_NAME = "QQAudio2MP3"


def detected_ffmpeg() -> str:
    """从 PATH 探测 ffmpeg.exe；找不到时返回空字符串。"""
    found = shutil.which("ffmpeg")
    return str(Path(found)) if found else ""


def detected_ffprobe() -> str:
    """优先从 PATH 探测 ffprobe.exe，其次与 ffmpeg 同目录查找。"""
    found = shutil.which("ffprobe")
    if found:
        return str(Path(found))
    ffmpeg = detected_ffmpeg()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe.exe")
        if sibling.is_file():
            return str(sibling)
    return ""


DEFAULT_FFMPEG = detected_ffmpeg()
DEFAULT_FFPROBE = detected_ffprobe() or DEFAULT_FFMPEG.replace(
    "ffmpeg.exe", "ffprobe.exe"
)

BITRATE_MODES = ("vbr", "128", "192", "256", "320")
OUTPUT_MODES = ("same", "custom", "mirror")
CONFLICT_POLICIES = ("rename", "skip", "overwrite")
SOURCE_MODES = ("file", "folder")


def app_data_dir() -> Path:
    """返回应用数据目录（%APPDATA%\\QQAudio2MP3），不存在时自动创建。"""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    path = root / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = Path.home() / f".{APP_NAME.lower()}"
        path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    """配置文件完整路径。"""
    return app_data_dir() / "config.json"


def log_dir() -> Path:
    """日志目录，不存在时自动创建。"""
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_workers() -> int:
    """默认并发数：min(4, CPU 核数)，至少 1。"""
    try:
        cpu = os.cpu_count() or 4
    except (TypeError, ValueError, OSError):
        cpu = 4
    return max(1, min(4, int(cpu)))


@dataclass
class AppConfig:
    """全部可持久化的用户设置。"""

    ffmpeg_path: str = DEFAULT_FFMPEG
    ffprobe_path: str = DEFAULT_FFPROBE
    output_mode: str = "same"
    output_dir: str = ""
    bitrate_mode: str = "vbr"
    workers: int = field(default_factory=default_workers)
    recursive: bool = False
    skip_existing: bool = True
    keep_metadata: bool = True
    keep_cover: bool = True
    copy_mp3: bool = True
    conflict_policy: str = "rename"
    last_source: str = ""
    last_source_mode: str = "file"

    def copy(self) -> AppConfig:
        """返回当前配置的独立副本。"""
        return replace(self)

    def normalized(self) -> AppConfig:
        """校验并修正非法字段，返回新的配置对象。"""
        cfg = self.copy()
        try:
            cfg.workers = max(1, min(8, int(cfg.workers)))
        except (TypeError, ValueError):
            cfg.workers = default_workers()
        if cfg.bitrate_mode not in BITRATE_MODES:
            cfg.bitrate_mode = "vbr"
        if cfg.output_mode not in OUTPUT_MODES:
            cfg.output_mode = "same"
        if cfg.conflict_policy not in CONFLICT_POLICIES:
            cfg.conflict_policy = "rename"
        if cfg.last_source_mode not in SOURCE_MODES:
            cfg.last_source_mode = "file"
        for name in ("ffmpeg_path", "ffprobe_path", "output_dir", "last_source"):
            value = getattr(cfg, name)
            setattr(cfg, name, str(value or "").strip())
        for name in ("recursive", "skip_existing", "keep_metadata", "keep_cover", "copy_mp3"):
            setattr(cfg, name, bool(getattr(cfg, name)))
        return cfg

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return asdict(self)

    @classmethod
    def load(cls) -> AppConfig:
        """读取配置文件；文件缺失或损坏时返回默认配置。"""
        path = config_path()
        if not path.is_file():
            return cls().normalized()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls().normalized()
        if not isinstance(raw, dict):
            return cls().normalized()
        valid_names = {f.name for f in fields(cls)}
        cfg = cls()
        for key, value in raw.items():
            if key in valid_names:
                try:
                    setattr(cfg, key, value)
                except (TypeError, ValueError) as exc:
                    logger.debug("忽略无效配置项 %s=%r：%s", key, value, exc)
        return cfg.normalized()

    def save(self) -> None:
        """原子写入配置文件。"""
        path = config_path()
        payload = json.dumps(self.normalized().to_dict(), ensure_ascii=False, indent=2)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                path.write_text(payload, encoding="utf-8")
            except OSError:
                pass
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
