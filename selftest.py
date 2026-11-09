"""自测脚本：生成测试音频并验证转换核心逻辑（不启动 GUI）。"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import DEFAULT_FFMPEG, DEFAULT_FFPROBE, AppConfig
from converter import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    ConversionEngine,
    plan_tasks,
    probe_duration,
)

FFMPEG = Path(DEFAULT_FFMPEG)
FFPROBE = Path(DEFAULT_FFPROBE)
ROOT = Path(__file__).resolve().parent / "_selftest"
MEDIA = ROOT / "素材 中文 空格"
CREATED: list[Path] = []


def run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")


def make_source(path: Path, duration: int, extra: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         *extra, str(path)]
    )
    CREATED.append(path)


def build_fixtures() -> list[Path]:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)

    ogg = MEDIA / "单曲 测试.ogg"
    make_source(ogg, 6, ["-c:a", "libvorbis", "-q:a", "4"])
    flac = MEDIA / "sub dir" / "专辑 一" / "歌曲 [flac].flac"
    make_source(flac, 5, ["-c:a", "flac"])
    wav = MEDIA / "sub dir" / "wav test.WAV"
    make_source(wav, 4, ["-c:a", "pcm_s16le"])
    m4a = MEDIA / "m4a 音轨.m4a"
    make_source(m4a, 3, ["-c:a", "aac", "-b:a", "96k"])
    mp3 = MEDIA / "已经是 mp3.mp3"
    make_source(mp3, 3, ["-c:a", "libmp3lame", "-b:a", "128k"])
    # 损坏文件
    broken = MEDIA / "损坏 文件.ogg"
    broken.write_bytes(b"OggS\x00garbage-not-audio" * 30)
    CREATED.append(broken)
    return [ogg, flac, wav, m4a, mp3, broken]


class Collector:
    def __init__(self) -> None:
        self.rows: dict[int, object] = {}
        self.logs: list[str] = []
        self.states: list[str] = []
        self.finished = False

    def bind(self, engine: ConversionEngine) -> None:
        engine.task_changed.connect(lambda t: self.rows.__setitem__(t.tid, t))
        engine.log_message.connect(self.logs.append)
        engine.state_changed.connect(self.states.append)
        engine.all_finished.connect(self._done)

    def _done(self) -> None:
        self.finished = True


class Harness:
    """在无 QApplication 的情况下驱动 Qt 信号（使用 QCoreApplication）。"""

    def __init__(self) -> None:
        from PySide6.QtCore import QCoreApplication

        self.app = QCoreApplication.instance() or QCoreApplication(sys.argv)

    def wait(self, seconds: float, until=None) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if until is not None and until():
                return True
            time.sleep(0.02)
        self.app.processEvents()
        return until() if until is not None else True


def base_config(**overrides) -> AppConfig:
    cfg = AppConfig()
    cfg.ffmpeg_path = str(FFMPEG)
    cfg.ffprobe_path = str(FFPROBE)
    cfg.workers = 3
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg.normalized()


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""), flush=True)


def test_probe(files: list[Path]) -> None:
    duration = probe_duration(FFPROBE, files[0])
    check("ffprobe 读取时长", duration is not None and 5.5 < duration < 6.5, f"duration={duration}")


def _wait(collector: Collector, app, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and not collector.finished:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def test_metadata_and_cover(h: Harness) -> None:
    """验证 ogg 标签写入 MP3，以及 flac 封面保留。"""
    import json as _json

    from converter import probe_tags

    base = ROOT / "标签 测试"
    base.mkdir(parents=True, exist_ok=True)
    tagged = base / "带标签 中文.ogg"
    run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-c:a", "libvorbis", "-q:a", "4",
         "-metadata", "title=测试标题", "-metadata", "artist=测试歌手",
         "-metadata", "album=测试专辑", str(tagged)])
    CREATED.append(tagged)

    cover = base / "cover.png"
    run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=blue:s=200x200:d=1", "-frames:v", "1", str(cover)])
    covered = base / "带封面 中文.flac"
    run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-i", str(cover),
         "-map", "0:a", "-map", "1:v", "-c:a", "flac", "-c:v", "mjpeg",
         "-disposition:v:0", "attached_pic",
         "-metadata", "title=封面标题", "-metadata", "artist=封面歌手", str(covered)])
    CREATED.extend([cover, covered])

    tags = probe_tags(FFPROBE, tagged)
    check("ffprobe 读取 ogg 标签",
          tags.get("title") == "测试标题" and tags.get("artist") == "测试歌手", str(tags))

    out = ROOT / "out_meta"
    cfg = base_config(output_mode="custom", output_dir=str(out), skip_existing=False,
                      keep_metadata=True, keep_cover=True)
    plan = plan_tasks([tagged, covered], cfg, base)
    collector = Collector()
    engine = ConversionEngine()
    collector.bind(engine)
    engine.start(plan, cfg)
    _wait(collector, h.app, 120)
    engine.shutdown(wait=True)

    def inspect(path: Path) -> tuple[dict, bool]:
        probe = subprocess.run(
            [str(FFPROBE), "-v", "error", "-show_entries",
             "format_tags:stream=codec_type,codec_name", "-of", "json", str(path)],
            check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        payload = _json.loads(probe.stdout or "{}")
        fmt_tags = {str(k).lower(): v
                    for k, v in ((payload.get("format") or {}).get("tags") or {}).items()}
        has_video = any(
            s.get("codec_type") == "video" and s.get("codec_name") == "mjpeg"
            for s in payload.get("streams") or []
        )
        return fmt_tags, has_video

    ogg_tags, _ = inspect(out / (tagged.stem + ".mp3"))
    _, has_cover = inspect(out / (covered.stem + ".mp3"))
    check("保留元数据（ogg 标签写入 mp3）",
          ogg_tags.get("title") == "测试标题" and ogg_tags.get("artist") == "测试歌手",
          str(ogg_tags))
    check("保留内嵌封面", has_cover, f"has_video={has_cover}")
    done = sum(1 for t in collector.rows.values() if t.status == STATUS_DONE)
    check("标签/封面任务全部成功", done == 2, f"done={done}")


def test_same_folder(h: Harness) -> None:
    src = MEDIA / "单曲 测试.ogg"
    cfg = base_config(output_mode="same", skip_existing=False, copy_mp3=False)
    plan = plan_tasks([src], cfg, src.parent)
    collector = Collector()
    engine = ConversionEngine()
    collector.bind(engine)
    engine.start(plan, cfg)
    _wait(collector, h.app, 90)
    dst = src.with_suffix(".mp3")
    ok = dst.is_file() and dst.stat().st_size > 1000
    check("输出到源文件同目录", ok, str(dst))
    engine.shutdown(wait=True)


def test_pause(h: Harness) -> None:
    """验证暂停后不再启动新任务，恢复后继续完成。"""
    sources = []
    for index in range(3):
        item = MEDIA / f"暂停测试 {index}.ogg"
        make_source(item, 600, ["-c:a", "libvorbis", "-q:a", "4"])
        sources.append(item)
    out = ROOT / "out_pause"
    cfg = base_config(output_mode="custom", output_dir=str(out), skip_existing=False,
                      workers=1, bitrate_mode="320")
    plan = plan_tasks(sources, cfg, sources[0].parent)
    collector = Collector()
    engine = ConversionEngine()
    collector.bind(engine)
    engine.start(plan, cfg)
    engine.pause()
    h.wait(2.0)
    snapshot = engine.tasks()
    pending = [t for t in snapshot if t.status == STATUS_PENDING]
    started = sum(1 for t in snapshot if t.status in (STATUS_RUNNING, STATUS_DONE))
    paused_ok = engine.is_paused and len(pending) == 2 and started == 1
    detail = f"paused={engine.is_paused} pending={len(pending)} started={started}"
    engine.resume()
    resumed = h.wait(3.0, lambda: sum(
        1 for t in engine.tasks() if t.status in (STATUS_RUNNING, STATUS_DONE)) >= 1)
    engine.cancel_all()
    h.wait(30, lambda: collector.finished)
    statuses = [t.status for t in engine.tasks()]
    cancelled = statuses.count(STATUS_CANCELLED)
    check("暂停后不再启动新任务、恢复后继续", paused_ok and resumed and cancelled == 3,
          detail + f" resumed={resumed} cancelled={cancelled} {statuses}")
    engine.shutdown(wait=True)


def test_single(h: Harness, files: list[Path]) -> None:
    src = files[0]
    out = ROOT / "out_single"
    cfg = base_config(output_mode="custom", output_dir=str(out), skip_existing=False)
    plan = plan_tasks([src], cfg, src.parent)
    collector = Collector()
    engine = ConversionEngine()
    collector.bind(engine)
    count, error = engine.start(plan, cfg)
    h.wait(90, lambda: collector.finished)
    dst = out / (src.stem + ".mp3")
    ok = count == 1 and dst.is_file() and dst.stat().st_size > 1000
    check("单文件 ogg -> mp3", ok, f"count={count} err={error} dst={dst.exists()}")
    if dst.is_file():
        probe = probe_duration(FFPROBE, dst)
        check("输出 MP3 可被 ffprobe 解析", probe is not None and probe > 5, f"duration={probe}")
    engine.shutdown(wait=True)


def test_batch_recursive(h: Harness) -> None:
    out = ROOT / "out_batch"
    cfg = base_config(output_mode="mirror", output_dir=str(out), recursive=True, skip_existing=False)
    sources = [p for p in MEDIA.rglob("*") if p.is_file() and p.suffix.lower() in {
        ".ogg", ".flac", ".wav", ".m4a", ".mp3", ".aiff", ".opus", ".ape", ".wma", ".aac"}]
    plan = plan_tasks(sources, cfg, MEDIA)
    collector = Collector()
    engine = ConversionEngine()
    collector.bind(engine)
    count, _error = engine.start(plan, cfg)
    h.wait(150, lambda: collector.finished)
    produced = sorted(str(p.relative_to(out)) for p in out.rglob("*.mp3"))
    ok = produced and any("sub dir" in name and "专辑 一" in name for name in produced)
    check("批量递归 + 保留目录结构", bool(ok), f"count={count} 产物={len(produced)} {produced}")
    check("损坏文件不影响其他文件", any(
        t.status == STATUS_FAILED for t in collector.rows.values()) and any(
        t.status == STATUS_DONE for t in collector.rows.values()),
        f"失败={sum(1 for t in collector.rows.values() if t.status==STATUS_FAILED)} "
        f"成功={sum(1 for t in collector.rows.values() if t.status==STATUS_DONE)}")
    failed = [t for t in collector.rows.values() if t.status == STATUS_FAILED]
    if failed:
        check("失败原因记录 stderr", bool(failed[0].message.strip()), failed[0].message.splitlines()[-1][:90])
    engine.shutdown(wait=True)
    return produced


def test_skip_existing(h: Harness) -> None:
    out = ROOT / "out_batch"
    cfg = base_config(output_mode="mirror", output_dir=str(out), recursive=True, skip_existing=True)
    sources = [p for p in MEDIA.rglob("*") if p.is_file() and p.suffix.lower() in {
        ".ogg", ".flac", ".wav", ".m4a", ".mp3"}]
    plan = plan_tasks(sources, cfg, MEDIA)
    skipped = [t for t in plan.skipped if t.message == "目标文件已存在"]
    check("跳过已存在 MP3 生效", len(skipped) >= 3, f"skipped={len(skipped)} tasks={len(plan.tasks)}")


def test_mp3_copy() -> None:
    src = MEDIA / "已经是 mp3.mp3"
    out = ROOT / "out_copy"
    cfg = base_config(output_mode="custom", output_dir=str(out), skip_existing=False, copy_mp3=True)
    plan = plan_tasks([src], cfg, src.parent)
    collector = Collector()
    engine = ConversionEngine()
    collector.bind(engine)
    engine.start(plan, cfg)
    deadline = time.time() + 40
    while time.time() < deadline and not collector.finished:
        time.sleep(0.05)
    dst = out / (src.stem + ".mp3")
    ok = dst.is_file() and dst.read_bytes() == src.read_bytes()
    check("MP3 输入直接复制（字节一致）", ok, f"exists={dst.exists()}")
    engine.shutdown(wait=True)


def test_cancel(h: Harness) -> None:
    long_src = MEDIA / "长音频 取消测试.ogg"
    make_source(long_src, 120, ["-c:a", "libvorbis", "-q:a", "4"])
    out = ROOT / "out_cancel"
    cfg = base_config(output_mode="custom", output_dir=str(out), skip_existing=False, workers=1, bitrate_mode="320")
    plan = plan_tasks([long_src], cfg, long_src.parent)
    collector = Collector()
    engine = ConversionEngine()
    collector.bind(engine)
    engine.start(plan, cfg)
    h.wait(3.0, lambda: any(t.status not in ("等待",) for t in collector.rows.values()))
    running = [t for t in collector.rows.values() if t.status == "转换中"]
    engine.cancel_all()
    h.wait(20, lambda: collector.finished)
    cancelled = [t for t in collector.rows.values() if t.status == STATUS_CANCELLED]
    leftover = list(out.glob("*.part")) if out.exists() else []
    ok = bool(running) and bool(cancelled) and not leftover
    check("取消任务并终止 ffmpeg", ok, f"running={len(running)} cancelled={len(cancelled)} part={leftover}")
    time.sleep(2.5)
    engine.shutdown(wait=True)


def test_rename_conflict() -> None:
    src = MEDIA / "单曲 测试.ogg"
    out = ROOT / "out_rename"
    out.mkdir(parents=True, exist_ok=True)
    first = out / (src.stem + ".mp3")
    first.write_bytes(b"placeholder")
    cfg = base_config(output_mode="custom", output_dir=str(out), skip_existing=False, conflict_policy="rename")
    plan = plan_tasks([src], cfg, src.parent)
    collector = Collector()
    engine = ConversionEngine()
    collector.bind(engine)
    engine.start(plan, cfg)
    deadline = time.time() + 60
    while time.time() < deadline and not collector.finished:
        time.sleep(0.05)
    renamed = out / (src.stem + " (1).mp3")
    ok = renamed.is_file() and first.read_bytes() == b"placeholder"
    check("同名冲突自动重命名", ok, f"renamed={renamed.exists()}")
    engine.shutdown(wait=True)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    print("生成测试素材…", flush=True)
    files = build_fixtures()
    h = Harness()
    test_probe(files)
    test_single(h, files)
    test_same_folder(h)
    test_metadata_and_cover(h)
    test_batch_recursive(h)
    test_skip_existing(h)
    test_mp3_copy()
    test_cancel(h)
    test_pause(h)
    test_rename_conflict()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\n自测结果：{passed}/{len(RESULTS)} 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  失败：{name} :: {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
