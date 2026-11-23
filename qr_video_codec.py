from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from multiprocessing import Pool
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
import qrcode
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

try:
    from pyzbar.pyzbar import ZBarSymbol, decode as zbar_decode
    HAS_PYZBAR = True
except ImportError:
    ZBarSymbol = None
    HAS_PYZBAR = False

MAGIC = b"QRV1"  # 4字节魔数
HEADER_STRUCT = struct.Struct(">4sIIQ")  # MAGIC(4B) + total_chunks(4B) + chunk_idx(4B) + est_offset_ms(8B)
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".ts"}


def derive_key(password: str, salt: bytes, iterations: int = 100_000) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,  # AES-256
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


def get_media_probe_info(file_path: Path) -> Optional[dict]:
    """仅当文件看起来是视频且能正常读取时返回媒体参数，普通文件返回 None"""
    if file_path.suffix.lower() not in VIDEO_EXTS:
        return None
    cap = cv2.VideoCapture(str(file_path))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    if fps <= 0 and frame_count <= 0 and width <= 0 and height <= 0:
        return None
    duration_ms = (frame_count / fps * 1000.0) if fps > 0 else 0.0
    return {
        "fps": round(fps, 3),
        "frame_count": frame_count,
        "duration_ms": round(duration_ms, 1),
        "width": width,
        "height": height,
    }


def make_qr_frame(
    payload_bytes: bytes,
    box_size: int = 6,
    border: int = 4,
    target_size: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    """将纯 ASCII / Base64 字节编码为二维码图像（BGR numpy 数组）"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload_bytes)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    if target_size is not None:
        tw, th = target_size
        fh, fw, _ = frame.shape
        canvas = np.full((th, tw, 3), 255, dtype=np.uint8)
        scale = min(tw / fw, th / fh)
        if scale >= 1.0:
            int_scale = max(1, int(scale))
            nw, nh = fw * int_scale, fh * int_scale
            if nw <= tw and nh <= th:
                resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_NEAREST)
            else:
                resized = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_NEAREST)
        else:
            nw, nh = int(fw * scale), int(fh * scale)
            resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)

        rh, rw, _ = resized.shape
        x0 = (tw - rw) // 2
        y0 = (th - rh) // 2
        canvas[y0 : y0 + rh, x0 : x0 + rw] = resized
        return canvas

    return frame


def _encode_single_chunk_frame(
    task: Tuple[int, int, bytes, bytes, int, float, int, int, int, int, Tuple[int, int]]
) -> Tuple[int, np.ndarray]:
    """多进程 worker：加密单个数据块并生成对应二维码帧（返回 frame_idx, bgr_frame）"""
    (
        idx,
        total_chunks,
        part,
        key,
        full_payload_len,
        duration_ms,
        meta_prefix_len,
        raw_len,
        chunk_size,
        box_size,
        canvas_size,
    ) = task
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    aad = struct.pack(">II", total_chunks, idx)
    ct = aesgcm.encrypt(nonce, part, aad)

    byte_pos = min(idx * chunk_size, full_payload_len)
    file_byte_pos = max(0, byte_pos - meta_prefix_len)
    ratio = min(1.0, file_byte_pos / max(1, raw_len))
    est_offset_ms = int(duration_ms * ratio) if duration_ms > 0 else 0

    pkt = HEADER_STRUCT.pack(MAGIC, total_chunks, idx, est_offset_ms) + nonce + ct
    qr_payload = b"QRV1:" + base64.b64encode(pkt)
    frame = make_qr_frame(qr_payload, box_size=box_size, target_size=canvas_size)
    return idx + 1, frame  # frame_idx=0 为密钥头帧，数据帧从 1 开始


def encode_file_to_qr_video(
    input_file: Path,
    out_video: Path,
    password: str,
    note: str = "",
    chunk_size: int = 900,
    fps: float = 12.0,
    box_size: int = 6,
    canvas_size: Tuple[int, int] = (720, 720),
    save_frames_dir: Optional[Path] = None,
    crf: int = 20,
    workers: int = 0,
):
    if not input_file.is_file():
        raise FileNotFoundError(f"找不到输入文件: {input_file}")

    t_start = time.time()
    raw_bytes = input_file.read_bytes()
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
    media_probe = get_media_probe_info(input_file)
    duration_ms = float(media_probe["duration_ms"]) if media_probe else 0.0

    salt = os.urandom(16)
    key = derive_key(password, salt)

    meta = {
        "filename": input_file.name,
        "ext": input_file.suffix.lower(),
        "size": len(raw_bytes),
        "sha256": sha256_hex,
        "created_at": int(time.time() * 1000),
        "note": note,
        "media_probe": media_probe,
    }
    meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")

    # 封包结构: [4B meta_len][meta_bytes][raw_file_bytes]
    full_payload = struct.pack(">I", len(meta_bytes)) + meta_bytes + raw_bytes
    chunks = [full_payload[i : i + chunk_size] for i in range(0, len(full_payload), chunk_size)]
    total_chunks = len(chunks)
    total_frames = total_chunks + 1

    cpu_cnt = os.cpu_count() or 1
    if workers <= 0:
        num_workers = min(8, cpu_cnt)
    else:
        num_workers = max(1, min(workers, cpu_cnt))

    crypto_header = {
        "v": 1,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "iter": 100_000,
        "total_chunks": total_chunks,
    }
    header_raw = json.dumps(crypto_header, separators=(",", ":")).encode("utf-8")
    header_qr_payload = b"QRH1:" + base64.b64encode(header_raw)
    header_frame = make_qr_frame(header_qr_payload, box_size=box_size, target_size=canvas_size)

    out_video.parent.mkdir(parents=True, exist_ok=True)
    if save_frames_dir is not None:
        save_frames_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(save_frames_dir / "frame_00000.png"), header_frame)

    ffmpeg_bin = shutil.which("ffmpeg")
    tmp_ctx = tempfile.TemporaryDirectory(prefix="qr_enc_") if ffmpeg_bin is not None else None
    tmp_path = Path(tmp_ctx.name) if tmp_ctx is not None else None

    if tmp_path is not None:
        cv2.imwrite(str(tmp_path / "f_00000.png"), header_frame)

    writer = None
    ordered_buffer: dict[int, np.ndarray] = {}
    next_write_idx = 1

    if tmp_path is None:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(out_video), fourcc, fps, canvas_size)
        if not writer.isOpened():
            raise RuntimeError(f"无法打开 VideoWriter 写入: {out_video}")
        writer.write(header_frame)

    def commit_frame(f_idx: int, frame: np.ndarray) -> None:
        if save_frames_dir is not None:
            cv2.imwrite(str(save_frames_dir / f"frame_{f_idx:05d}.png"), frame)
        if tmp_path is not None:
            cv2.imwrite(str(tmp_path / f"f_{f_idx:05d}.png"), frame)
        elif writer is not None:
            writer.write(frame)

    tasks = [
        (
            idx,
            total_chunks,
            part,
            key,
            len(full_payload),
            duration_ms,
            4 + len(meta_bytes),
            len(raw_bytes),
            chunk_size,
            box_size,
            canvas_size,
        )
        for idx, part in enumerate(chunks)
    ]

    done_count = 0
    try:
        if num_workers > 1 and total_chunks > 1:
            print(f"[编码并行] 使用 {num_workers} 个进程生成二维码帧...")
            chunkstep = max(1, total_chunks // (num_workers * 4))
            with Pool(processes=num_workers) as pool:
                for f_idx, frame in pool.imap_unordered(_encode_single_chunk_frame, tasks, chunksize=chunkstep):
                    done_count += 1
                    ordered_buffer[f_idx] = frame
                    while next_write_idx in ordered_buffer:
                        commit_frame(next_write_idx, ordered_buffer.pop(next_write_idx))
                        next_write_idx += 1

                    if done_count % 100 == 0 or done_count == total_chunks:
                        print(f"[编码进度] {done_count}/{total_chunks} 块 ({time.time() - t_start:.1f}s)")
        else:
            for t in tasks:
                f_idx, frame = _encode_single_chunk_frame(t)
                done_count += 1
                commit_frame(f_idx, frame)
                if done_count % 100 == 0 or done_count == total_chunks:
                    print(f"[编码进度] {done_count}/{total_chunks} 块 ({time.time() - t_start:.1f}s)")

        if writer is not None:
            writer.release()
            writer = None

        if tmp_path is not None and ffmpeg_bin is not None:
            t_enc_vid = time.time()
            cmd = [
                ffmpeg_bin,
                "-y",
                "-framerate",
                str(fps),
                "-i",
                str(tmp_path / "f_%05d.png"),
                "-c:v",
                "libx264",
                "-crf",
                str(crf),
                "-pix_fmt",
                "yuv420p",
                str(out_video),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            print(f"[视频封装] ffmpeg 合成完成 ({time.time() - t_enc_vid:.1f}s)")
    finally:
        if writer is not None:
            writer.release()
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    print(f"[完成] 原始文件: {input_file} ({len(raw_bytes)} bytes)")
    print(f"[完成] 生成二维码载体视频: {out_video} ({out_video.stat().st_size} bytes), 总耗时: {time.time() - t_start:.1f}s")
    print(f"[信息] 总帧数: {total_frames} (含1个密钥头帧 + {total_chunks} 个数据帧), 帧率: {fps} fps, 预计时长: {total_frames/fps:.2f}s")


def extract_qr_strings(frame: np.ndarray) -> list[str]:
    """使用 pyzbar 解码二维码 ASCII 文本，避免原始字节被 Latin-1/UTF-8 转换破坏"""
    texts: list[str] = []
    if HAS_PYZBAR:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        results = zbar_decode(gray, symbols=[ZBarSymbol.QRCODE])
        for r in results:
            if r.data:
                texts.append(bytes(r.data).decode("ascii", errors="ignore").strip())
        if texts:
            return texts

    detector = cv2.QRCodeDetector()
    data_str, _, _ = detector.detectAndDecode(frame)
    if data_str:
        texts.append(data_str.strip())
    return texts


def resolve_output_file_path(out_arg: Path, orig_filename: str) -> Path:
    """若 -o 指向已存在目录或以路径分隔符结尾，则自动使用原文件名"""
    as_str = str(out_arg)
    if out_arg.exists() and out_arg.is_dir():
        return out_arg / orig_filename
    if as_str.endswith(("/", "\\")):
        return out_arg / orig_filename
    return out_arg


def decode_qr_video_to_file(qr_video: Path, out_file: Path, password: str, frames_dir: Optional[Path] = None):
    if not HAS_PYZBAR:
        print("[提示] 未检测到 pyzbar，正尝试 OpenCV QRCodeDetector（若提示 QUIRC 未链接请安装 libzbar0 和 pyzbar）。")

    t_start = time.time()
    salt = None
    iterations = 100_000
    key = None
    aesgcm = None

    total_chunks = None
    received = {}
    pending_b64_pkts: list[bytes] = []
    scanned_frames = 0
    decoded_qrs = 0
    failed_qrs = 0

    def decrypt_data_pkt(raw_pkt: bytes):
        nonlocal total_chunks, decoded_qrs, failed_qrs
        assert aesgcm is not None
        if len(raw_pkt) < HEADER_STRUCT.size + 12 + 16 or raw_pkt[:4] != MAGIC:
            failed_qrs += 1
            return
        magic, t_chunks, idx, est_ms = HEADER_STRUCT.unpack(raw_pkt[: HEADER_STRUCT.size])
        if total_chunks is None:
            total_chunks = t_chunks
        nonce = raw_pkt[HEADER_STRUCT.size : HEADER_STRUCT.size + 12]
        ct = raw_pkt[HEADER_STRUCT.size + 12 :]
        aad = struct.pack(">II", t_chunks, idx)
        try:
            pt = aesgcm.decrypt(nonce, ct, aad)
            received[idx] = (est_ms, pt)
            decoded_qrs += 1
        except Exception as e:
            failed_qrs += 1
            if idx == 0:
                print(f"[警告] 块 #0 解密失败，请检查密码是否正确 ({e})")

    def handle_text(txt: str):
        nonlocal salt, iterations, key, aesgcm, total_chunks, decoded_qrs, failed_qrs

        if txt.startswith("QRH1:"):
            try:
                raw_hdr = base64.b64decode(txt[5:], validate=True)
                hdr = json.loads(raw_hdr.decode("utf-8"))
                salt = base64.b64decode(hdr["salt_b64"])
                iterations = int(hdr.get("iter", 100_000))
                total_chunks = int(hdr["total_chunks"])
                key = derive_key(password, salt, iterations)
                aesgcm = AESGCM(key)
                decoded_qrs += 1
                for raw_pkt in pending_b64_pkts:
                    decrypt_data_pkt(raw_pkt)
                pending_b64_pkts.clear()
            except Exception as e:
                failed_qrs += 1
                print(f"[警告] 密钥头帧解析失败: {e}")
            return

        if txt.startswith("QRV1:"):
            try:
                raw_pkt = base64.b64decode(txt[5:], validate=True)
            except Exception:
                failed_qrs += 1
                return
            if aesgcm is None:
                pending_b64_pkts.append(raw_pkt)
                return
            decrypt_data_pkt(raw_pkt)
            return

        failed_qrs += 1

    def process_frame(frame: np.ndarray):
        nonlocal scanned_frames, failed_qrs
        scanned_frames += 1
        txts = extract_qr_strings(frame)
        if not txts:
            failed_qrs += 1
        else:
            for t in txts:
                handle_text(t)

        if scanned_frames % 100 == 0:
            exp_str = str(total_chunks) if total_chunks is not None else "?"
            print(f"[解码进度] 已扫描 {scanned_frames} 帧 | 已恢复 {len(received)}/{exp_str} 块 ({time.time() - t_start:.1f}s)")

    if frames_dir is not None and frames_dir.exists():
        for fp in sorted(frames_dir.glob("*.png")):
            f = cv2.imread(str(fp))
            if f is not None:
                process_frame(f)
    else:
        cap = cv2.VideoCapture(str(qr_video))
        if not cap.isOpened():
            raise RuntimeError(f"无法打开二维码载体视频: {qr_video}")
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            process_frame(frame)
        cap.release()

    print(f"[统计] 扫描帧数: {scanned_frames}, 成功解码QR: {decoded_qrs}, 未识别/失败: {failed_qrs}, 耗时: {time.time() - t_start:.1f}s")

    if total_chunks is None or len(received) < total_chunks:
        got = len(received)
        exp = total_chunks if total_chunks is not None else "?"
        print(f"[错误] 数据不完整：已恢复 {got}/{exp} 块。")
        sys.exit(1)

    ordered_payload = b"".join(received[i][1] for i in range(total_chunks))
    (meta_len,) = struct.unpack(">I", ordered_payload[:4])
    meta_bytes = ordered_payload[4 : 4 + meta_len]
    file_bytes = ordered_payload[4 + meta_len :]

    meta = json.loads(meta_bytes.decode("utf-8"))
    actual_sha256 = hashlib.sha256(file_bytes).hexdigest()

    if actual_sha256 != meta["sha256"]:
        print("[错误] SHA256 校验不匹配，数据可能已损坏！")
        sys.exit(1)

    final_out_path = resolve_output_file_path(out_file, meta["filename"])
    final_out_path.parent.mkdir(parents=True, exist_ok=True)
    final_out_path.write_bytes(file_bytes)

    print("[成功] 密码校验通过，SHA256 完整性校验一致！")
    print(f"[还原文件] 保存至: {final_out_path} ({len(file_bytes)} bytes)")
    print(
        f"[附带信息] 原文件名: {meta['filename']} | 备注: {meta['note'] or '(无)'} | 创建时间戳(ms): {meta['created_at']}"
    )
    if meta.get("media_probe"):
        print(f"[媒体参数] {meta['media_probe']}")


def main():
    parser = argparse.ArgumentParser(description="任意文件 <-> 加密二维码视频 编解码工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    enc_names = ["encode", "encode-file"]
    dec_names = ["decode", "decode-file"]

    for name in enc_names:
        p_enc = sub.add_parser(name, help="将任意文件加密转为二维码视频")
        p_enc.add_argument("-i", "--input", required=True, type=Path, help="输入原始文件路径")
        p_enc.add_argument("-o", "--output", required=True, type=Path, help="输出二维码视频路径(.mp4)")
        p_enc.add_argument("-p", "--password", required=True, type=str, help="加密密码")
        p_enc.add_argument("--note", default="", type=str, help="附加备注信息")
        p_enc.add_argument("--chunk-size", default=900, type=int, help="单块字节数(建议 400~900)")
        p_enc.add_argument("--fps", default=12.0, type=float, help="二维码视频帧率")
        p_enc.add_argument("--box-size", default=6, type=int, help="二维码单格像素大小")
        p_enc.add_argument("--size", default=720, type=int, help="输出画布边长(正方形像素)")
        p_enc.add_argument("--crf", default=20, type=int, help="H.264 CRF 质量(建议 14~20)")
        p_enc.add_argument("--workers", default=0, type=int, help="编码进程数(0 为自动，最大 8)")
        p_enc.add_argument("--save-frames", default=None, type=Path, help="可选：同时保存每一帧二维码 PNG")

    for name in dec_names:
        p_dec = sub.add_parser(name, help="从二维码视频解密还原任意文件")
        p_dec.add_argument("-i", "--input", required=True, type=Path, help="输入二维码视频路径")
        p_dec.add_argument("-o", "--output", required=True, type=Path, help="输出还原文件路径(或目录)")
        p_dec.add_argument("-p", "--password", required=True, type=str, help="解密密码")
        p_dec.add_argument("--frames-dir", default=None, type=Path, help="可选：直接从 PNG 帧目录解码")

    args = parser.parse_args()
    if args.cmd in enc_names:
        encode_file_to_qr_video(
            input_file=args.input,
            out_video=args.output,
            password=args.password,
            note=args.note,
            chunk_size=args.chunk_size,
            fps=args.fps,
            box_size=args.box_size,
            canvas_size=(args.size, args.size),
            save_frames_dir=args.save_frames,
            crf=args.crf,
            workers=args.workers,
        )
    elif args.cmd in dec_names:
        decode_qr_video_to_file(
            qr_video=args.input,
            out_file=args.output,
            password=args.password,
            frames_dir=args.frames_dir,
        )


if __name__ == "__main__":
    main()
