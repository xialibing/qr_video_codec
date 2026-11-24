from __future__ import annotations

import argparse
import base64
import hashlib
import re
from pathlib import Path
from typing import Optional

import cv2

try:
    from pyzbar.pyzbar import ZBarSymbol, decode as zbar_decode
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False


META_RE = re.compile(r"^0+/(\d+)\|META\|([^|]+)\|([0-9a-fA-F]{64})$")
CHUNK_RE = re.compile(r"^(\d+)/(\d+)\|([A-Za-z0-9+/=]+)$")


def _decode_single_image(img, detector) -> list[str]:
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


def decode_frame_qr(frame, detector) -> list[str]:
    if frame is None or frame.size == 0:
        return []

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    texts = _decode_single_image(gray, detector)
    if texts:
        return texts

    h, w = gray.shape[:2]
    if 0 < max(h, w) < 1600:
        scaled = cv2.resize(gray, (int(w * 1.5), int(h * 1.5)), interpolation=cv2.INTER_NEAREST)
        texts = _decode_single_image(scaled, detector)

    return texts


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
) -> None:
    if not video_path.is_file():
        raise FileNotFoundError(f"找不到视频文件: {video_path}")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    detector = cv2.QRCodeDetector()
    chunks: dict[int, str] = {}
    total_expected: Optional[int] = None
    locked_by_meta = False
    meta_filename: Optional[str] = None
    meta_sha256: Optional[str] = None

    frame_no = 0
    new_hits = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_no += 1

            for txt in decode_frame_qr(frame, detector):
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
                            f"| SHA256={meta_sha256} (录像帧 #{frame_no})"
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
                            f"(已收 {len(chunks)}/{total_expected}，录像帧 #{frame_no})"
                        )
    finally:
        cap.release()

    print(
        f"[统计] 录像总帧数={frame_no} | 新增有效帧={new_hits} "
        f"| 分片进度={len(chunks)}/{total_expected or '?'}"
    )

    expected_sha = (override_sha256 or meta_sha256 or "").strip().lower()
    if not expected_sha:
        print("[提示] 未扫到 META 帧且未传 --sha256，还原后将跳过 SHA256 校验。")

    if total_expected is None or len(chunks) < total_expected:
        exp = total_expected if total_expected is not None else 0
        missing = [i for i in range(1, exp + 1) if i not in chunks]
        print(f"[未完成] 缺少 {len(missing)} 个分片: {missing[:40]}{'...' if len(missing) > 40 else ''}")
        return

    full_b64 = "".join(chunks[i] for i in range(1, total_expected + 1))
    raw_bytes = base64.b64decode(full_b64, validate=False)

    if expected_sha:
        actual_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
        if actual_sha != expected_sha:
            print(f"[校验失败] SHA256 不一致！实际={actual_sha}，期望={expected_sha}")
            return
        print(f"[校验通过] SHA256 一致: {actual_sha}")

    final_out = resolve_output_path(out_arg, meta_filename)
    final_out.parent.mkdir(parents=True, exist_ok=True)
    final_out.write_bytes(raw_bytes)
    print(f"[完成] 已还原文件: {final_out} ({len(raw_bytes)} 字节)")


def main() -> None:
    parser = argparse.ArgumentParser(description="还原录制的 Excel 二维码录像为原文件")
    parser.add_argument("-i", "--input", required=True, type=Path, help="Excel 二维码录像路径（如 222.mp4）")
    parser.add_argument("-o", "--output", required=True, type=Path, help="输出目录或输出文件路径")
    parser.add_argument("--sha256", default="", help="可选：手动指定期望 SHA256（覆盖 META）")
    args = parser.parse_args()
    recover_excel_qr_video(args.input, args.output, args.sha256)


if __name__ == "__main__":
    main()
