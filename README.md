# QR Video Codec（任意文件 ↔ 加密二维码载体视频）

一个基于 Python 的命令行工具，可将**任意文件**（文本、压缩包、视频、固件及各类二进制文件）加密、分块并编码为动态二维码载体视频（`.mp4`），随后通过逐帧扫描二维码、AES-GCM 认证解密与 SHA256 校验，无损还原原始文件及元数据。

## 核心特性

* **通用文件支持**：不局限于视频，支持 `.mp4`、`.zip`、`.tar.gz`、`.txt` 等任意二进制文件，还原时保留原文件名、扩展名、备注信息与创建时间戳（视频文件额外记录媒体参数）。
* **强密码派生与认证加密**：采用 **PBKDF2-HMAC-SHA256**（默认 100,000 次迭代）派生 256-bit 密钥，配合 **AES-256-GCM**（带分块序号与总块数的 AAD 关联认证数据），密码错误或帧被篡改时解密直接拦截。
* **Base64 字节防损封装**：二维码载荷采用 `QRH1:<base64>`（密钥头帧）与 `QRV1:<base64>`（数据分块帧）纯 ASCII 封装，规避部分条码解码库在 Latin-1 / UTF-8 字节转换时对高位字节（`> 0x7F`）造成的破坏。
* **多进程并行编码**：支持 `--workers` 多进程并行生成二维码帧并按帧序号有序落盘，结合 `ffmpeg`（`libx264` + `yuv420p`）合成标准 MP4（若未安装 `ffmpeg`，自动回退至 OpenCV `VideoWriter`）。
* **完整校验闭环**：重组后校验原始文件 `SHA256`，确保逐字节无损还原。

## 封包与帧格式简述

1. **载荷重组前结构**：`[4字节大端元数据长度][UTF-8 JSON 元数据][原始文件字节流]`
2. **分块与加密**：按 `--chunk-size` 切块，每块独立生成 12 字节随机 Nonce，使用 `AESGCM.encrypt(nonce, chunk, aad)` 加密，附加头部：
   * 魔数 `QRV1`（4B）+ `total_chunks`（4B）+ `chunk_idx`（4B）+ `est_offset_ms`（8B）+ `nonce`（12B）+ `ciphertext+tag`
3. **二维码帧序列**：
   * 第 0 帧：`QRH1:` + Base64 编码的加密头 JSON（含随机 16 字节 `salt`、迭代次数、总分块数）。
   * 第 `1..N` 帧：`QRV1:` + Base64 编码的数据分块包。

## 环境依赖

### 1. Python 依赖
```bash
pip install opencv-python numpy qrcode cryptography pyzbar
```

### 2. 系统依赖（Linux / Ubuntu）
`pyzbar` 需要系统层 `libzbar0`；若需更清晰的 H.264 编码与更小的体积，建议安装 `ffmpeg`：
```bash
sudo apt-get update
sudo apt-get install -y libzbar0 ffmpeg
```

> **提示**：部分环境下的 OpenCV 未链接 `QUIRC` 模块，导致 `cv2.QRCodeDetector()` 无法解码；本工具默认优先使用 `pyzbar`（并限定 `ZBarSymbol.QRCODE`），稳定性更高。

## 快速使用

### 编码（任意文件 → 二维码载体视频）

```bash
python -u qr_video_codec.py encode \
  -i demo.tar.gz \
  -o qr_demo.mp4 \
  -p "your_password" \
  --note "压缩包测试" \
  --chunk-size 600 \
  --box-size 8 \
  --size 1080 \
  --crf 16
```

### 解码（二维码载体视频 → 还原原始文件）

```bash
python -u qr_video_codec.py decode \
  -i qr_demo.mp4 \
  -o restored_demo.tar.gz \
  -p "your_password"

# 校验完整性
sha256sum demo.tar.gz restored_demo.tar.gz
```

若 `-o` 传入已存在目录或以 `/` 结尾的路径，将自动按元数据中的原文件名保存：
```bash
python -u qr_video_codec.py decode -i qr_demo.mp4 -o ./output_dir/ -p "your_password"
```

## 参数说明

### `encode` / `encode-file`
| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `-i, --input` | 必填 | 输入原始文件路径 |
| `-o, --output` | 必填 | 输出二维码载体视频路径（建议 `.mp4`） |
| `-p, --password` | 必填 | 加密密码 |
| `--note` | `""` | 附加备注信息（写入加密元数据） |
| `--chunk-size` | `900` | 单块原始字节数（建议 `400~900`，越小单帧二维码越稀疏、抗压缩越好，但总帧数越多） |
| `--fps` | `12.0` | 载体视频帧率 |
| `--box-size` | `6` | 单个二维码模块（格子）像素大小 |
| `--size` | `720` | 正方形输出画布边长（像素，如 `720` 或 `1080`） |
| `--crf` | `20` | `ffmpeg` `libx264` 恒定质量因子（建议 `14~20`，越低越清晰但文件越大） |
| `--workers` | `0` | 编码进程数（`0` 表示自动取 `min(8, cpu_count)`） |
| `--save-frames` | `None` | 可选：额外保存每一帧二维码 PNG 图片目录 |

### `decode` / `decode-file`
| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `-i, --input` | 必填 | 输入二维码载体视频路径 |
| `-o, --output` | 必填 | 输出还原文件路径或目录 |
| `-p, --password` | 必填 | 解密密码 |
| `--frames-dir` | `None` | 可选：直接从已保存的二维码 PNG 序列帧目录解码 |

## 实测参考

在测试环境（433,812 字节 `demo.tar.gz`，`--chunk-size 600 --box-size 8 --size 1080 --crf 16`，共 725 帧）下的表现：
* **编码耗时**：约 `16.1s`（8 进程画帧约 `11.6s` + `ffmpeg` 封装约 `4.5s`），输出 `qr_demo.mp4` 约 `2.28 MB`。
* **解码耗时**：约 `16.3s`，725 帧全部识别（0 失败），`SHA256` 完全一致。

> **体积与性能说明**：由于二维码承载的是加密后的高熵数据且经过视频帧编码，载体视频体积通常会大于原始文件（尤其对极小文件如几字节文本会有一个基础头帧+数据帧开销）。大文件建议先用小样本测试合适的 `--chunk-size` 与 `--crf` 组合。

## 后续规划

* 跨端解码与播放协议对齐（移动端通过相机或本地视频逐帧扫码、重组解密并打开/播放）。
* 针对大文件的分块容错/重传或流式处理优化(正在进行)。

## License

MIT License（可根据仓库实际需要调整）。
