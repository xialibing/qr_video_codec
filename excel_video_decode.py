from __future__ import annotations

import argparse
import base64
import gc
import hashlib
import multiprocessing as mp
import re
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    from pyzbar.pyzbar import ZBarSymbol, decode as zbar_decode
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False


META_RE = re.compile(r"^0+/(\d+)\|META\|([^|]+)\|([0-9a-fA-F]{64})$")
CHUNK_RE = re.compile(r"^(\d+)/(\d+)\|([A-Za-z0-9+/=]+)$")

_WORKER_DETECTOR: Optional[cv2.QRCodeDetector] = None


def _init_worker() -> None:
    global _WORKER_DETECTOR
    try:
        cv2.setNumThreads(1)
    except Exception:
        pass
    _WORKER_DETECTOR = cv2.QRCodeDetector()


def _decode_single_image(img: np.ndarray, detector: cv2.QRCodeDetector) -> list[str]:
    found: list[str] = []

    if HAS_PYZBAR:
        try:
            for item in zbar_decode(img, symbols=[ZBarSymbol.QRCODE]):
                if item.data:
                    txt = item.data.decode("utf-8", errors="ignore").strip()
                    if txt and txt not in found:
                        found.append(txt)
        except Exception:
            pass

    if found:
        return found

    try:
        data, _, _ = detector.detectAndDecode(img)
        if data:
            txt = data.strip()
            if txt and txt not in found:
                found.append(txt)
    except cv2.error:
        pass

    return found


def _decode_frame_worker(task: tuple[int, np.ndarray]) -> tuple[int, list[str]]:
    frame_no, frame = task
    detector = _WORKER_DETECTOR
    if detector is None:
        detector = cv2.QRCodeDetector()

    if frame is None or frame.size == 0:
        return frame_no, []

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    texts = _decode_single_image(gray, detector)
    if texts:
        del gray, frame
        return frame_no, texts

    h, w = gray.shape[:2]
    if 0 < max(h, w) <= 1280:
        scaled = cv2.resize(
            gray,
            (max(1, int(w * 1.5)), max(1, int(h * 1.5))),
            interpolation=cv2.INTER_NEAREST,
        )
        texts = _decode_single_image(scaled, detector)
        del scaled

    del gray, frame
    return frame_no, texts


def is_same_scene(
    prev_thumb: Optional[np.ndarray],
    frame: np.ndarray,
    diff_threshold: float = 2.0,
) -> tuple[bool, np.ndarray]:
    thumb = cv2.resize(frame, (64, 64), interpolation=cv2.INTER_AREA)
    thumb_gray = cv2.cvtColor(thumb, cv2.COLOR_BGR2GRAY).astype(np.float32)
    del thumb

    if prev_thumb is None:
        return False, thumb_gray

    mean_diff = float(np.mean(np.abs(thumb_gray - prev_thumb)))
    if mean_diff < diff_threshold:
        del thumb_gray
        return True, prev_thumb

    del prev_thumb
    return False, thumb_gray


def resolve_output_path(out_arg: Path, meta_filename: Optional[str]) -> Path:
    raw_str = str(out_arg)
    default_name = meta_filename if meta_filename else "recovered_output.bin"
    if out_arg.exists() and out_arg.is_dir():
        return out_arg / default_name
    if raw_str.endswith(("/", "\\")):
        return out_arg / default_name
    return out_arg


def recover_excel_qr_video(
    video_path: Path,
    out_arg: Path,
    override_sha256: str = "",
    frame_step: int = 2,
    diff_threshold: float = 2.0,
    no_skip: bool = False,
    workers: int = 2,
    debug_unmatched: int = 10,
) -> None:
    if not video_path.is_file():
        raise FileNotFoundError(f"找不到视频文件: {video_path}")
    if frame_step < 1:
        frame_step = 1
    if workers < 1:
        workers = 1

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    chunks: dict[int, str] = {}
    total_expected: Optional[int] = None
    locked_by_meta = False
    meta_filename: Optional[str] = None
    meta_sha256: Optional[str] = None

    prev_thumb: Optional[np.ndarray] = None
    frame_no = 0
    sampled_no = 0
    skipped_same = 0
    new_hits = 0
    unmatched_printed = 0

    batch_size = max(2, workers * 2)
    pending_tasks: list[tuple[int, np.ndarray]] = []
    pool: Optional[mp.Pool] = None

    def _consume_results(results: list[tuple[int, list[str]]]) -> None:
        nonlocal total_expected, locked_by_meta, meta_filename, meta_sha256
        nonlocal new_hits, unmatched_printed

        for f_no, texts in results:
            for txt in texts:
                m_meta = META_RE.match(txt)
                if m_meta:
                    t_chunks = int(m_meta.group(1))
                    fname = Path(m_meta.group(2)).name.strip() or "recovered_output.bin"
                    fsha = m_meta.group(3).lower()

                    if not locked_by_meta:
                        total_expected = t_chunks
                        locked_by_meta = True

                    if meta_sha256 is None:
                        meta_filename = fname
                        meta_sha256 = fsha
                        new_hits += 1
                        print(
                            f"[META] 文件名={meta_filename} | 总分片={total_expected} "
                            f"| SHA256={meta_sha256} (录像帧 #{f_no})",
                            flush=True,
                        )
                    continue

                m_chunk = CHUNK_RE.match(txt)
                if m_chunk:
                    idx = int(m_chunk.group(1))
                    t_chunks = int(m_chunk.group(2))
                    b64_part = m_chunk.group(3)

                    if total_expected is None:
                        total_expected = t_chunks
                    elif t_chunks != total_expected:
                        continue

                    if 1 <= idx <= total_expected and idx not in chunks:
                        chunks[idx] = b64_part
                        new_hits += 1
                        print(
                            f"[分片] {idx}/{total_expected} "
                            f"(已收 {len(chunks)}/{total_expected}，录像帧 #{f_no})",
                            flush=True,
                        )
                elif debug_unmatched > 0 and unmatched_printed < debug_unmatched:
                    unmatched_printed += 1
                    preview = txt[:100].replace("\r", "\\r").replace("\n", "\\n")
                    print(
                        f"[未匹配文本] 帧#{f_no} (长度={len(txt)}): {preview}",
                        flush=True,
                    )

    try:
        if workers > 1:
            pool = mp.Pool(processes=workers, initializer=_init_worker)
        else:
            _init_worker()

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_no += 1

            if frame_step > 1 and (frame_no - 1) % frame_step != 0:
                del frame
                continue

            sampled_no += 1
            if not no_skip:
                same_scene, prev_thumb = is_same_scene(
                    prev_thumb, frame, diff_threshold=diff_threshold
                )
                if same_scene:
                    skipped_same += 1
                    del frame
                    continue

            if workers > 1 and pool is not None:
                pending_tasks.append((frame_no, frame))
                if len(pending_tasks) >= batch_size:
                    res = pool.map(_decode_frame_worker, pending_tasks)
                    pending_tasks.clear()
                    _consume_results(res)
                    del res
                    gc.collect()
            else:
                res_one = [_decode_frame_worker((frame_no, frame))]
                _consume_results(res_one)
                del res_one
                if sampled_no % 20 == 0:
                    gc.collect()

        if workers > 1 and pool is not None and pending_tasks:
            res = pool.map(_decode_frame_worker, pending_tasks)
            pending_tasks.clear()
            _consume_results(res)
            del res
            gc.collect()
    finally:
        pending_tasks.clear()
        if pool is not None:
            pool.close()
            pool.join()
        cap.release()
        prev_thumb = None
        gc.collect()

    print(
        f"[统计] 录像总帧数={frame_no} | 抽检帧数={sampled_no} | 跳过静帧={skipped_same} "
        f"| 新增有效帧={new_hits} | 分片进度={len(chunks)}/{total_expected or '?'}",
        flush=True,
    )

    expected_sha = (override_sha256 or meta_sha256 or "").strip().lower()
    if not expected_sha:
        print("[提示] 未扫到 META 帧且未传 --sha256，还原后将跳过 SHA256 校验。", flush=True)

    if total_expected is None or len(chunks) < total_expected:
        exp = total_expected if total_expected is not None else 0
        missing = [i for i in range(1, exp + 1) if i not in chunks]
        print(
            f"[未完成] 缺少 {len(missing)} 个分片: "
            f"{missing[:40]}{'...' if len(missing) > 40 else ''}",
            flush=True,
        )
        return

    full_b64 = "".join(chunks[i] for i in range(1, total_expected + 1))
    raw_bytes = base64.b64decode(full_b64, validate=False)

    if expected_sha:
        actual_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
        if actual_sha != expected_sha:
            print(f"[校验失败] SHA256 不一致！实际={actual_sha}，期望={expected_sha}", flush=True)
            return
        print(f"[校验通过] SHA256 一致: {actual_sha}", flush=True)

    final_out = resolve_output_path(out_arg, meta_filename)
    final_out.parent.mkdir(parents=True, exist_ok=True)
    final_out.write_bytes(raw_bytes)
    print(f"[完成] 已还原文件: {final_out} ({len(raw_bytes)} 字节)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="还原录制的 Excel 二维码录像为原文件")
    parser.add_argument("-i", "--input", required=True, type=Path, help="Excel 二维码录像路径")
    parser.add_argument("-o", "--output", required=True, type=Path, help="输出目录或输出文件路径")
    parser.add_argument("--sha256", default="", help="可选：手动指定期望 SHA256（覆盖 META）")
    parser.add_argument("--step", type=int, default=2, help="抽帧步长，默认 2")
    parser.add_argument("--diff", type=float, default=2.0, help="画面变化阈值，默认 2.0")
    parser.add_argument("--no-skip", action="store_true", help="禁用静帧跳过")
    parser.add_argument("--workers", type=int, default=2, help="解码进程数，默认 2（虚拟机建议 2~4）")
    parser.add_argument("--debug-unmatched", type=int, default=10, help="最多打印多少条未匹配正则的解码文本")
    args = parser.parse_args()
    recover_excel_qr_video(
        args.input,
        args.output,
        args.sha256,
        args.step,
        args.diff,
        args.no_skip,
        args.workers,
        args.debug_unmatched,
    )


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
