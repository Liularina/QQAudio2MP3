"""FFmpeg 封装：任务规划、命令构建、进度解析、并发执行与子进程管理。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from config import AppConfig
from utils import format_elapsed, tail_lines

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

STATUS_PENDING = "等待"
STATUS_RUNNING = "转换中"
STATUS_DONE = "成功"
STATUS_FAILED = "失败"
STATUS_SKIPPED = "跳过"
STATUS_CANCELLED = "已取消"

COVER_ARGS = [
    "-map",
    "0:v:0?",
    "-c:v",
    "copy",
    "-disposition:v:0",
    "attached_pic",
    "-metadata:s:v",
    "title=Album cover",
    "-metadata:s:v",
    "comment=Cover (front)",
]


@dataclass
class TaskEntry:
    """单个转换任务的运行时状态。"""

    tid: int
    src: Path
    dst: Path
    status: str = STATUS_PENDING
    progress: float = 0.0
    elapsed: float = 0.0
    message: str = ""
    duration: float | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def snapshot(self) -> TaskEntry:
        """复制一份状态，供跨线程信号安全传递。"""
        return replace(self)


@dataclass
class TaskPlan:
    """任务规划结果：待执行任务与需要跳过的任务。"""

    tasks: list[TaskEntry] = field(default_factory=list)
    skipped: list[TaskEntry] = field(default_factory=list)
    output_dir: Path | None = None


def find_ffmpeg(cfg: AppConfig) -> Path | None:
    """返回可用的 ffmpeg 路径，优先配置值，其次 PATH。"""
    candidate = Path(cfg.ffmpeg_path) if cfg.ffmpeg_path else None
    if candidate and candidate.is_file():
        return candidate
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def find_ffprobe(cfg: AppConfig) -> Path | None:
    """返回可用的 ffprobe 路径，优先配置值，其次与 ffmpeg 同目录，再次 PATH。"""
    candidate = Path(cfg.ffprobe_path) if cfg.ffprobe_path else None
    if candidate and candidate.is_file():
        return candidate
    ffmpeg = find_ffmpeg(cfg)
    if ffmpeg:
        sibling = ffmpeg.with_name("ffprobe.exe")
        if sibling.is_file():
            return sibling
        sibling = ffmpeg.with_name("ffprobe")
        if sibling.is_file():
            return sibling
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def probe_duration(ffprobe: Path, src: Path, timeout: float = 30.0) -> float | None:
    """用 ffprobe 读取媒体总时长（秒），失败返回 None。"""
    base = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(src),
    ]
    for args in (
        base,
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "default=nw=1:nk=1",
            str(src),
        ],
    ):
        try:
            completed = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if not line or line.upper() == "N/A":
                continue
            try:
                value = float(line)
            except ValueError:
                continue
            if value > 0:
                return value
    return None


TAG_KEYS: frozenset[str] = frozenset(
    {
        "title",
        "artist",
        "album",
        "album_artist",
        "albumartist",
        "composer",
        "genre",
        "date",
        "year",
        "track",
        "disc",
        "comment",
        "lyrics",
        "copyright",
        "performer",
    }
)

SKIP_TAG_KEYS: frozenset[str] = frozenset(
    {"encoder", "language", "handler_name", "major_brand", "minor_version", "compatible_brands"}
)


def probe_tags(ffprobe: Path, src: Path, timeout: float = 30.0) -> dict[str, str]:
    """读取音轨与容器标签，返回可直接传给 -metadata 的键值对。"""
    args = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format_tags:stream_tags",
        "-of",
        "json",
        str(src),
    ]
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError:
        return {}
    tags: dict[str, str] = {}
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        if codec_type is not None and codec_type != "audio":
            continue
        for key, value in (stream.get("tags") or {}).items():
            tags.setdefault(str(key).lower(), str(value))
    for key, value in ((payload.get("format") or {}).get("tags") or {}).items():
        tags[str(key).lower()] = str(value)
    result: dict[str, str] = {}
    for key, value in tags.items():
        if key in SKIP_TAG_KEYS or key not in TAG_KEYS:
            continue
        cleaned = value.strip()
        if cleaned and cleaned.upper() != "N/A":
            result[key] = cleaned
    return result


def resolve_output(src: Path, cfg: AppConfig, base_dir: Path | None) -> Path | None:
    """按输出策略计算目标 MP3 路径；策略无效时返回 None。"""
    name = src.with_suffix(".mp3").name
    if cfg.output_mode == "same":
        return src.with_name(name)
    out_dir = Path(cfg.output_dir) if cfg.output_dir else None
    if out_dir is None:
        return None
    if cfg.output_mode == "custom":
        return out_dir / name
    root = base_dir or src.parent
    try:
        relative = src.parent.relative_to(root)
    except ValueError:
        relative = Path()
    return out_dir / relative / name


def plan_tasks(
    sources: Iterable[Path],
    cfg: AppConfig,
    base_dir: Path | None = None,
    start_id: int = 0,
) -> TaskPlan:
    """把源文件列表转换为任务列表，并标记必然跳过的任务。"""
    plan = TaskPlan()
    used: set[Path] = set()
    index = start_id
    if cfg.output_dir and cfg.output_mode in ("custom", "mirror"):
        plan.output_dir = Path(cfg.output_dir)

    for src in sources:
        dst = resolve_output(src, cfg, base_dir)
        if dst is None:
            plan.skipped.append(
                TaskEntry(
                    tid=index,
                    src=src,
                    dst=src.with_suffix(".mp3"),
                    status=STATUS_SKIPPED,
                    message="未设置输出目录",
                    finished_at=time.time(),
                )
            )
            index += 1
            continue
        entry = TaskEntry(tid=index, src=src, dst=dst)
        index += 1
        # 仅“自动重命名”策略需要在规划阶段消除同批次重名；
        # “跳过/覆盖”交给执行阶段按策略处理。
        if cfg.output_mode == "custom" and cfg.conflict_policy == "rename":
            candidate = dst
            counter = 1
            while candidate in used:
                candidate = dst.with_name(f"{dst.stem} ({counter}){dst.suffix}")
                counter += 1
            entry.dst = candidate
            used.add(candidate)
            dst = candidate
        if dst == src:
            entry.status = STATUS_SKIPPED
            entry.message = "目标与源文件相同"
            entry.finished_at = time.time()
            plan.skipped.append(entry)
            continue
        if cfg.skip_existing and dst.exists():
            entry.status = STATUS_SKIPPED
            entry.message = "目标文件已存在"
            entry.finished_at = time.time()
            plan.skipped.append(entry)
            continue
        plan.tasks.append(entry)
    return plan


def build_command(
    ffmpeg: Path,
    src: Path,
    dst: Path,
    temp: Path,
    cfg: AppConfig,
    with_cover: bool,
    tags: dict[str, str] | None = None,
) -> list[str]:
    """构建 ffmpeg 参数列表（不使用 shell）。

    OGG/Opus 的标签通常挂在音轨上，单纯 ``-map_metadata 0`` 会丢失，
    因此额外用 ffprobe 读取到的标签显式写入 ``-metadata``。
    """
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:a:0",
    ]
    keep_cover = bool(with_cover and cfg.keep_cover)
    if keep_cover:
        cmd += COVER_ARGS
    if cfg.keep_metadata:
        cmd += ["-map_metadata", "0"]
        for key, value in (tags or {}).items():
            cmd += ["-metadata", f"{key}={value}"]
    else:
        # 不保留元数据时必须显式清空，否则 ffmpeg 默认会复制全局元数据。
        cmd += ["-map_metadata", "-1"]
    cmd += ["-c:a", "libmp3lame"]
    if cfg.bitrate_mode == "vbr":
        cmd += ["-q:a", "2"]
    else:
        cmd += ["-b:a", f"{cfg.bitrate_mode}k"]
    cmd += ["-id3v2_version", "3", "-sn"]
    if not keep_cover:
        cmd += ["-vn"]
    cmd += ["-progress", "pipe:1", "-nostats", "-f", "mp3", str(temp)]
    return cmd


class ConversionEngine(QObject):
    """在后台线程池中执行转换，并把状态变化通过 Qt 信号回传。"""

    task_changed = Signal(object)
    log_message = Signal(str)
    state_changed = Signal(str)
    all_finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._tasks: dict[int, TaskEntry] = {}
        self._procs: dict[int, subprocess.Popen[str]] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._futures: list[Future] = []
        self._resume_gate = threading.Event()
        self._resume_gate.set()
        self._cancelled = False
        self._running = False
        self._pending_count = 0
        self._finished_tids: set[int] = set()
        self._finalized = False
        self._cfg: AppConfig = AppConfig()
        self._ffmpeg: Path | None = None
        self._ffprobe: Path | None = None
        self._last_output_dir: Path | None = None
        self._reserved: set[Path] = set()

    # ------------------------------------------------------------------ 状态
    @property
    def is_running(self) -> bool:
        """是否还有任务在执行。"""
        return self._running

    @property
    def is_paused(self) -> bool:
        """是否处于暂停（不再启动新任务）状态。"""
        return not self._resume_gate.is_set()

    @property
    def last_output_dir(self) -> Path | None:
        """最近一次任务的输出目录，用于“打开输出目录”。"""
        return self._last_output_dir

    def tasks(self) -> list[TaskEntry]:
        """返回当前任务列表快照。"""
        with self._lock:
            return [task.snapshot() for task in self._tasks.values()]

    def clear(self) -> None:
        """清空任务记录（仅在空闲时调用）。"""
        with self._lock:
            self._tasks.clear()
            self._futures.clear()
            self._pending_count = 0
            self._finished_tids.clear()
            self._reserved.clear()

    # ------------------------------------------------------------------ 控制
    def start(self, plan: TaskPlan, cfg: AppConfig) -> tuple[int, str]:
        """启动批量转换，返回 (已启动任务数, 错误信息)。"""
        with self._lock:
            if self._running:
                return 0, "已有任务正在运行"
        if not plan.tasks:
            return 0, "没有可转换的任务"

        ffmpeg = find_ffmpeg(cfg)
        if ffmpeg is None:
            return 0, f"找不到 ffmpeg：{cfg.ffmpeg_path}"
        ffprobe = find_ffprobe(cfg)

        self._cfg = cfg.copy()
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._cancelled = False
        self._running = True
        self._finalized = False
        self._reserved.clear()
        self._resume_gate.set()
        self._futures.clear()

        with self._lock:
            self._tasks.clear()
            self._finished_tids.clear()
            for entry in plan.tasks:
                entry.status = STATUS_PENDING
                entry.progress = 0.0
                entry.elapsed = 0.0
                self._tasks[entry.tid] = entry
            self._pending_count = len(plan.tasks)
            if plan.output_dir:
                self._last_output_dir = plan.output_dir
            else:
                self._last_output_dir = plan.tasks[0].dst.parent

        self._executor = ThreadPoolExecutor(
            max_workers=max(1, min(8, int(cfg.workers))),
            thread_name_prefix="convert",
        )
        self.state_changed.emit("running")
        self.log_message.emit(
            f"开始转换：{len(plan.tasks)} 个任务，并发 {cfg.workers}"
        )
        for entry in plan.tasks:
            future = self._executor.submit(self._run_task, entry.tid)
            self._futures.append(future)
        return len(plan.tasks), ""

    def pause(self) -> None:
        """暂停：不再启动新任务，正在执行的任务继续完成。"""
        if self._running:
            self._resume_gate.clear()
            self.state_changed.emit("paused")
            self.log_message.emit("已暂停：正在执行的任务将继续完成，不再启动新任务")

    def resume(self) -> None:
        """恢复：继续启动等待中的任务。"""
        if self._running:
            self._resume_gate.set()
            self.state_changed.emit("running")
            self.log_message.emit("已恢复")

    def cancel_all(self) -> None:
        """取消全部：终止正在运行的 ffmpeg，未开始的任务直接标记取消。"""
        self._cancelled = True
        self._resume_gate.set()
        with self._lock:
            procs = list(self._procs.items())
        for tid, proc in procs:
            self._emit(tid, message="正在取消…")
            try:
                proc.terminate()
            except OSError:
                pass
        if procs:
            threading.Thread(target=self._force_kill_later, args=(procs,), daemon=True).start()
        self.log_message.emit("已请求取消全部任务")
        cancelled: list[TaskEntry] = []
        finished_all = False
        with self._lock:
            for entry in self._tasks.values():
                if entry.status == STATUS_PENDING:
                    entry.status = STATUS_CANCELLED
                    entry.message = "已取消"
                    entry.finished_at = time.time()
                    self._finished_tids.add(entry.tid)
                    cancelled.append(entry.snapshot())
            finished_all = len(self._finished_tids) >= self._pending_count
        for snapshot in cancelled:
            self.task_changed.emit(snapshot)
        if finished_all:
            self._finalize()

    def shutdown(self, wait: bool = False) -> None:
        """释放线程池并终止残留子进程。"""
        self._cancelled = True
        self._resume_gate.set()
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=True)
            self._executor = None

    def _force_kill_later(self, procs: list[tuple[int, subprocess.Popen[str]]]) -> None:
        """2 秒后强制结束仍未退出的进程。"""
        time.sleep(2.0)
        for _tid, proc in procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

    # ------------------------------------------------------------------ 执行
    def _emit(self, tid: int, **changes: object) -> None:
        """更新任务字段并发出变更信号。"""
        with self._lock:
            entry = self._tasks.get(tid)
            # 已结束（含取消）的任务不再接受状态回写，避免状态从“已取消”跳回“转换中”。
            if entry is None or tid in self._finished_tids:
                return
            for key, value in changes.items():
                setattr(entry, key, value)
            snapshot = entry.snapshot()
        self.task_changed.emit(snapshot)

    def _run_task(self, tid: int) -> None:
        """线程池任务体：等待闸门 -> 取时长 -> 调用 ffmpeg -> 更新状态。"""
        self._resume_gate.wait()

        with self._lock:
            entry = self._tasks.get(tid)
        if entry is None:
            return
        if self._cancelled:
            self._finish(tid, STATUS_CANCELLED, "已取消")
            return

        started = time.time()
        self._emit(tid, status=STATUS_RUNNING, started_at=started, progress=0.0, message="准备中")
        logger.info("开始转换：%s -> %s", entry.src, entry.dst)

        duration = None
        if self._ffprobe is not None:
            duration = probe_duration(self._ffprobe, entry.src)
        if duration:
            self._emit(tid, duration=duration)

        try:
            entry.dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._finish(tid, STATUS_FAILED, f"无法创建输出目录：{exc}")
            return

        dst, skip_reason = self._claim_destination(entry)
        if dst != entry.dst:
            self._emit(tid, dst=dst)
        if skip_reason:
            self._finish(tid, STATUS_SKIPPED, skip_reason, started)
            return
        if dst != entry.dst:
            entry.dst = dst
            self._emit(tid, dst=dst)

        if entry.src.suffix.lower() == ".mp3":
            if self._cfg.copy_mp3:
                self._copy_mp3(tid, entry.src, dst, started)
                return
            self._finish(tid, STATUS_SKIPPED, "源文件已是 MP3（未开启复制）", started)
            return

        tags: dict[str, str] = {}
        if self._cfg.keep_metadata and self._ffprobe is not None:
            tags = probe_tags(self._ffprobe, entry.src)
        self._transcode(tid, entry, dst, duration, started, tags)

    def _claim_destination(self, entry: TaskEntry) -> tuple[Path, str]:
        """在锁内确定最终输出路径；返回 (路径, 跳过原因)。"""
        with self._lock:
            dst = entry.dst
            on_disk = dst.exists()
            # 本批次内其它任务已占用同一路径时，也视为冲突。
            reserved = dst in self._reserved
            if on_disk and self._cfg.skip_existing:
                return dst, "目标文件已存在"
            if (on_disk or reserved) and self._cfg.conflict_policy == "skip":
                return dst, "目标文件已存在（策略：跳过）"
            if (on_disk or reserved) and self._cfg.conflict_policy == "rename":
                candidate = self._next_free_path(dst)
                self._reserved.add(candidate)
                return candidate, ""
            self._reserved.add(dst)
            return dst, ""

    def _next_free_path(self, dst: Path) -> Path:
        """生成“名称 (n).mp3”形式的空闲路径。

        必须同时检查磁盘与本批次预留集合；调用方需持有 ``_lock``。
        不能只用磁盘检查后再判断预留，否则未落盘的预留路径会被反复取回，
        导致死循环。
        """
        index = 1
        candidate = dst
        while candidate.exists() or candidate in self._reserved:
            candidate = dst.with_name(f"{dst.stem} ({index}){dst.suffix}")
            index += 1
        return candidate

    def _copy_mp3(self, tid: int, src: Path, dst: Path, started: float) -> None:
        """输入本身就是 MP3 时直接复制文件。"""
        # 临时文件名带任务 ID，避免并发任务写同一个 .part 文件。
        temp = dst.with_name(f"{dst.name}.{tid}.part")
        try:
            shutil.copy2(src, temp)
            if self._cancelled:
                self._cleanup_temp(temp)
                self._finish(tid, STATUS_CANCELLED, "已取消", started)
                return
            os.replace(temp, dst)
        except OSError as exc:
            self._cleanup_temp(temp)
            self._finish(tid, STATUS_FAILED, f"复制失败：{exc}", started)
            return
        self._last_output_dir = dst.parent
        self._finish(tid, STATUS_DONE, "已复制（源为 MP3）", started, progress=100.0)

    def _transcode(
        self,
        tid: int,
        entry: TaskEntry,
        dst: Path,
        duration: float | None,
        started: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        """调用 ffmpeg 转码，解析进度，必要时去掉封面重试。"""
        assert self._ffmpeg is not None
        # 临时文件名带任务 ID，避免并发任务写同一个 .part 文件。
        temp = dst.with_name(f"{dst.name}.{tid}.part")
        attempts = [True, False] if self._cfg.keep_cover else [False]
        last_error = ""
        for attempt_index, with_cover in enumerate(attempts):
            if self._cancelled:
                self._cleanup_temp(temp)
                self._finish(tid, STATUS_CANCELLED, "已取消", started)
                return
            cmd = build_command(self._ffmpeg, entry.src, dst, temp, self._cfg, with_cover, tags)
            ok, error, speed = self._execute(tid, cmd, duration)
            if self._cancelled:
                self._cleanup_temp(temp)
                self._finish(tid, STATUS_CANCELLED, "已取消", started)
                return
            if ok and temp.is_file() and temp.stat().st_size > 0:
                try:
                    os.replace(temp, dst)
                except OSError as exc:
                    self._cleanup_temp(temp)
                    self._finish(tid, STATUS_FAILED, f"写入输出文件失败：{exc}", started)
                    return
                self._last_output_dir = dst.parent
                note = "" if with_cover else "（未包含封面）"
                if speed:
                    note = f"{note} {speed}".strip()
                self._finish(tid, STATUS_DONE, note or "转换完成", started, progress=100.0)
                return
            last_error = error
            self._cleanup_temp(temp)
            if with_cover and attempt_index == 0:
                logger.warning("任务 %s 带封面转码失败，去掉封面重试：%s", tid, tail_lines(error, 3))
                self._emit(tid, message="封面处理失败，重试中…", progress=0.0)
                continue
            break

        reason = tail_lines(last_error, 20) or "未知错误"
        self._finish(tid, STATUS_FAILED, reason, started)

    def _execute(
        self, tid: int, cmd: list[str], duration: float | None
    ) -> tuple[bool, str, str]:
        """启动 ffmpeg 并读取进度；返回 (成功, stderr 文本, 速度文本)。"""
        stderr_chunks: list[str] = []
        speed_holder = {"value": ""}
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            return False, f"启动 ffmpeg 失败：{exc}", ""
        with self._lock:
            self._procs[tid] = proc
        # 取消请求可能发生在本进程注册之前，这里补一次检查。
        if self._cancelled:
            try:
                proc.terminate()
            except OSError:
                pass

        def drain_stderr() -> None:
            if proc.stderr is None:
                return
            try:
                for line in proc.stderr:
                    stderr_chunks.append(line)
                    if len(stderr_chunks) > 400:
                        del stderr_chunks[:200]
            except (OSError, ValueError):
                pass

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        last_percent = -1.0
        last_emit = 0.0
        out_time: float | None = None
        try:
            if proc.stdout is not None:
                for raw in proc.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    if line.startswith(("out_time_us=", "out_time_ms=")):
                        value = line.split("=", 1)[1]
                        try:
                            micros = int(value)
                        except ValueError:
                            continue
                        if micros >= 0:
                            out_time = micros / 1_000_000.0
                            if duration and duration > 0:
                                percent = min(99.9, out_time / duration * 100.0)
                                now = time.monotonic()
                                if percent - last_percent >= 0.5 or now - last_emit >= 0.4:
                                    last_percent = percent
                                    last_emit = now
                                    self._emit(tid, progress=percent)
                            else:
                                now = time.monotonic()
                                if now - last_emit >= 0.8:
                                    last_emit = now
                                    self._emit(
                                        tid,
                                        message=f"进行中 {format_elapsed(out_time)}",
                                    )
                    elif line.startswith("speed="):
                        value = line.split("=", 1)[1].strip()
                        if value and value != "N/A":
                            speed_holder["value"] = f"速度 {value}"
                            now = time.monotonic()
                            if duration and now - last_emit >= 0.4:
                                last_emit = now
                                self._emit(tid, message=speed_holder["value"])
        except (OSError, ValueError):
            pass
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except (OSError, ValueError):
                pass
            proc.wait()
            stderr_thread.join(timeout=3.0)
            with self._lock:
                self._procs.pop(tid, None)

        return proc.returncode == 0, "".join(stderr_chunks), speed_holder["value"]

    def _cleanup_temp(self, temp: Path) -> None:
        """删除未完成的临时文件。"""
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass

    def _finish(
        self,
        tid: int,
        status: str,
        message: str,
        started: float | None = None,
        progress: float | None = None,
    ) -> None:
        """写入任务最终状态，并在全部结束时发出完成信号。"""
        now = time.time()
        with self._lock:
            entry = self._tasks.get(tid)
            if entry is None or tid in self._finished_tids:
                return
            entry.status = status
            entry.message = message
            entry.finished_at = now
            entry.elapsed = now - (started if started is not None else (entry.started_at or now))
            if progress is not None:
                entry.progress = progress
            snapshot = entry.snapshot()
            self._finished_tids.add(tid)
            finished_all = len(self._finished_tids) >= self._pending_count
        self.task_changed.emit(snapshot)
        text = f"[{status}] {snapshot.src.name} -> {snapshot.dst.name}"
        if message and status != STATUS_DONE:
            text += f"：{message}"
        logger.info(text)
        if status != STATUS_DONE:
            self.log_message.emit(text)
        if finished_all:
            self._finalize()

    def _finalize(self) -> None:
        """所有任务结束后释放线程池并通知 UI。"""
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            self._running = False
        self._resume_gate.set()
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None
        self.state_changed.emit("cancelled" if self._cancelled else "finished")
        self.all_finished.emit()
