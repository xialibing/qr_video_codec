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


------------------------------------------------------------------------------------------------------------------
(下面是excel的readme)
# Excel QR Video Codec（Excel 二维码录像文件传输与还原）

一套基于 **Excel VBA** 与 **Python** 的跨环境文件传输工具：在受限终端通过 Excel 将任意文件编码为二维码帧序列并自动轮播，再通过 **屏幕录制 / 摄像头拍摄 + Python 解码脚本** 逐帧提取、重组分片并校验 **SHA-256** 完整性，无损还原原始文件。

---

## 核心特性

* **任意文件编码**：支持文本、脚本、压缩包及各类二进制文件，自动计算原始字节流的 **SHA-256** 摘要并进行 Base64 分片。
* **纯 Excel 自动轮播**：借助 `Application.OnTime` 定时更新工作表内嵌二维码图片帧，支持自定义单帧 Base64 字符上限与切换间隔。
* **多引擎高容错解码**：Python 端逐帧扫描录像，优先使用 `pyzbar` 高速解码，遇到模糊或低分辨率帧自动回退至 OpenCV `QRCodeDetector` 并执行放大重试。
* **自动元信息与完整性校验**：自动解析 `META` 帧中的原始文件名与期望哈希值，重组后严格比对 SHA-256（同时支持通过 `--sha256` 参数手动覆盖校验）。

---

## 传输协议设计

编码生成的帧序列由 **1 帧元信息帧（META）** 和 **N 帧数据分片帧（CHUNK）** 组成：

### 1. 元信息帧（序号 `0`）

```text
0000/XXXX|META|filename|sha256
```

* `XXXX`：数据分片总数（4 位数字补零，如 `0012`）
* `filename`：原始文件名（已自动过滤 `|`、`/`、`\` 及换行符）
* `sha256`：原始文件字节流的 64 位小写十六进制 SHA-256 摘要

### 2. 数据分片帧（序号 `1` ~ `XXXX`）

```text
YYYY/XXXX|Base64片段
```

* `YYYY`：当前分片序号（`0001` ~ `XXXX`）
* `XXXX`：数据分片总数
* `Base64片段`：原始文件 Base64 字符串按设定长度切分后的内容

---

## 仓库结构建议

```text
.
├── README.md                   # 项目说明文档
├── excel_video_decode.py       # Python 录像抽帧解码、分片重组与 SHA-256 校验脚本
├── vba/                        # Excel VBA 源码备份（便于版本控制与审查）
│   ├── Module1.bas             # 文件读取、certutil SHA-256 计算、Base64 分片与轮播控制
│   └── basQRCode.bas           # 二维码矩阵生成核心模块
└── example/                    # 示例工作簿（包含 Player 与 Frames 工作表）
```

> **提示**：由于 `.xlsm` 二进制工作簿不便于 Git 直接对比代码变更，建议将 VBA 编辑器中的 `Module1` 与 `basQRCode` 导出为 `.bas` 文件一并提交到仓库。

---

## 快速开始

### 第一步：Excel 端编码与录屏

1. 打开包含 `Player`、`Frames` 工作表及 `Module1`、`basQRCode` 模块的 `.xlsm` 工作簿，并点击 **启用宏**。
2. 在 `Player` 工作表中配置传输参数：
   * `B2`：待编码的目标文件路径（运行宏后自动回填）
   * `B3`：单帧 Base64 字符数上限（建议设为 `80` ~ `250`；字符越多单帧二维码密度越高，对录屏清晰度要求也越高）
   * `B4`：帧切换时间间隔（单位：秒，建议设为 `0.5` ~ `1.0` 秒以上，确保录屏软件完整捕获每一帧）
   * `B5`：当前编码与轮播状态提示区
3. 运行宏 **`SelectAndEncodeFile`**：
   * 选择目标文件后，自动调用系统 `certutil` 计算标准 SHA-256，生成 `META` 帧及所有数据分片写入 `Frames` 表，并在 `Player` 表生成首帧二维码预览图 `QR_Stream_Image`。
4. 启动录屏软件（框选二维码图片区域），运行宏 **`StartPlayback`** 开始轮播；完整播放完至少一轮后，运行 **`StopPlayback`** 停止并保存视频（如 `record.mp4`）。

---

### 第二步：Python 端解码还原

#### 1. 安装依赖

```bash
pip install opencv-python pyzbar
```

* **Linux（Ubuntu / Debian）** 若使用 `pyzbar`，需额外安装底层 `libzbar0` 动态库：
  ```bash
  sudo apt-get update && sudo apt-get install -y libzbar0
  ```
* **macOS** 可通过 Homebrew 安装 `zbar`：
  ```bash
  brew install zbar
  ```

#### 2. 运行解码命令

```bash
# 1. 输出到指定目录（自动读取 META 中的原始文件名，并自动校验 SHA-256）
python excel_video_decode.py -i record.mp4 -o ./out/

# 2. 输出到指定文件路径
python excel_video_decode.py -i record.mp4 -o ./out/recovered.bin

# 3. 手动指定期望 SHA-256（覆盖 META 中的哈希值）
python excel_video_decode.py -i record.mp4 -o ./out/ --sha256 <64位小写sha256>
```

#### 3. 终端日志说明

* `[META]`：成功识别元信息帧，输出原文件名、总分片数、期望 SHA-256 及命中的录像帧号。
* `[分片]`：捕获到新的有效数据分片，并实时打印收集进度（如 `已收 5/12`）。
* `[未完成]`：录像中存在漏帧，脚本会列出所有缺失的分片编号，方便针对性补录或调整播放间隔。
* `[校验通过]` / `[校验失败]`：所有分片拼装解码后计算 SHA-256 并与期望值比对。
* `[完成]`：校验一致后正式写入目标文件并打印字节大小。

---

## 常见问题与调优建议

1. **多帧传输时出现漏帧（`[未完成] 缺少 X 个分片`）**
   * 适当调大 `Player!B4` 的帧切换间隔（例如从 `0.3` 秒调至 `0.8` 秒），或提高录屏软件帧率（如 `30fps` / `60fps`）。
   * 轮播时建议完整录制 `1.5` ~ `2` 轮，脚本会自动去重拼接，能有效弥补首尾切换时的丢帧。
2. **二维码过密导致识别率下降**
   * 适当减小 `Player!B3`（如从 `250` 降至 `100`），降低单帧二维码的版本与模块密度，同时拉大 `QR_Stream_Image` 的显示尺寸。
3. **视频封装或编码兼容问题**
   * 如果部分录屏软件导出的视频无法被 `cv2.VideoCapture` 正常读取，可使用 `ffmpeg` 转为标准 H.264 MP4 后再执行还原：
     ```bash
     ffmpeg -i raw_record.mkv -c:v libx264 -crf 18 -pix_fmt yuv420p fixed.mp4
     ```


## License

MIT License（可根据仓库实际需要调整）。
