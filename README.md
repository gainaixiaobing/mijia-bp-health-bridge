# mijia-bp-health-bridge · 米家血压计 → Apple 健康

把 **米家智能电子血压计**（`ihealth.bpm.bpx1`）的每次测量自动送进 iPhone 的「健康」App。

**无需开发者账号 · 无需连接设备 · 无需改动血压计** —— Mac 被动监听血压计的蓝牙广播并解密，
经 iCloud Drive 中转，由 iPhone 快捷指令写入健康。

> ⚠️ 非官方项目，与小米 / iHealth 无任何关联；仅供读取使用者本人自有设备的数据。

---

## 工作原理

```
┌─────────────┐  BLE 加密广播   ┌──────────────────────────┐
│ 米家血压计   │ ─────────────▶ │ Mac 守护进程              │
│ (测量后广播) │   (MiBeacon)   │ 监听 + 解密 + 去重 + 计数  │
└─────────────┘                └───────────┬──────────────┘
                                           │ staging/ → 原子改名
                                           ▼
                             iCloud Drive / bp-health / inbox/
                                           │
                                           ▼
                               ┌──────────────────────┐
                               │ iPhone 快捷指令       │
                               │ 读取 → 写入健康 App   │
                               │ → 移到 done/（防重复）│
                               └──────────────────────┘
```

- 测量完成后 1~2 秒内，血压计会以蓝牙广播（MiBeacon）**突发**发出加密的测量数据帧；
- Mac 用从**你自己的小米云账号**取得的 bindkey 被动解码 —— 甚至不需要与设备建立连接；
- 数据经 iCloud Drive 落到手机，由快捷指令写进「健康」（血压 + 心率，含测量时间）；
- 导入成功后文件被移到 `done/`，守护进程据此保证**不重复补发**。

## 主要发现（逆向记录摘要）

| # | 发现 | 说明 |
|---|------|------|
| 1 | **测量数据是广播出来的** | 服务 UUID `FE95`（MiBeacon），产品 ID **8120**；平时只有无载荷心跳（每 20~30 秒） |
| 2 | **广播是加密的** | AES-CCM（4 字节 tag）：nonce = MAC反序 + 产品号 + 计数器 + 随机数；AAD = `0x11`；密钥 = 16 字节 **bindkey** |
| 3 | **测量对象格式（TLV，长度 9）** | `时间 u32LE（2000-01-01 **UTC** 起秒）｜收缩压 u16LE｜舒张压 u8｜脉率 u8｜标志位 u8` |
| 4 | **bindkey 可从自己的账号获取** | 小米云接口 `/v2/device/blt_get_beaconkey`（data 中必须带 `"pdid": 1`） |
| 5 | **官方 App 通道完全不同** | 官方走的是需登录认证的私有 GATT 会话协议；本项目**不需要**它，零打扰 |

完整协议记录（帧结构逐字节解析、实测样例、原因码、记录格式）见 👉 **[docs/protocol-notes.md](docs/protocol-notes.md)**

## 快速开始

### 0. 准备

- 一台 Mac（本项目在 macOS 验证）+ 一台米家智能电子血压计（`ihealth.bpm.bpx1`）
- Python ≥ 3.10
- iPhone 与 Mac 登录同一 iCloud 账号，并开启 iCloud Drive
- **macOS 上需要设备 MAC**（广播帧不含 MAC），在 米家 App → 设备 → 右上角「…」→ 设备信息 可查

> 不建议用系统 Python 直接 `pip install`（Homebrew 会报 `externally-managed-environment`）。
> `scripts/install.sh` 会自动建 `.venv`；手动装的话：
> `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

### 1. 取得 bindkey（解密密钥）

```bash
.venv/bin/pip install mijiaAPI          # https://github.com/Do1e/mijia-api
.venv/bin/python scripts/get_beaconkey.py blt.3.xxxxxx --save
# 首次使用需按 mijiaAPI 文档完成一次登录（生成 auth.json）
```

`--save` 会以 `0600` 权限写入仓库根目录 `bp_bindkey.hex`（已 gitignore）。
也可以用社区工具 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)（MIT）读取其中的「BLE KEY」。

### 2. 命令行验证：扫描解码

```bash
export BPX1_MAC=8C:D0:B2:xx:xx:xx       # 你的设备 MAC（macOS 上必需）
.venv/bin/python scripts/bpx1_scan.py 90
```

对着血压计做一次测量 —— 终端里应打印出解出的读数。

### 3. 安装常驻守护（推荐）

```bash
BPX1_MAC=8C:D0:B2:xx:xx:xx scripts/install.sh
```

脚本会：建 venv/装依赖 → 建 iCloud 目录（inbox/done/staging）→ 收紧 bindkey 权限 →
渲染并加载 launchd → 跑一遍自检。重复执行等于"更新"，`--dry-run` 可预演，`--uninstall` 可卸载。

守护进程会持续监听，每次捕获新测量就写一个 JSON 到
`iCloud Drive/bp-health/inbox/`（可用 `BPX1_INBOX` 环境变量改路径）。

### 4. iPhone 快捷指令

按照 **[docs/iPhone快捷指令-导入血压.md](docs/iPhone快捷指令-导入血压.md)** 创建（8 步）：

读取 inbox（**过滤 `bp-` 开头的文件**）→ 写入「健康」（血压/心率）→ **移到 done/**。

第一次搭之前，可以先造一条假数据验证手机端：

```bash
.venv/bin/python scripts/bpx1_inject.py
```

### 5. 自检

```bash
.venv/bin/python scripts/bpx1_doctor.py
```

把"依赖 / bindkey / MAC / iCloud / 目录 / launchd / 最近采集 / 日志"一次查完，✗ 项按提示处理。

## 日常排查

| 症状 | 先看哪里 |
|------|----------|
| 健康里没有新记录 | `out/daemon.log` 有没有 `★ 新测量`；inbox 有没有文件；手机快捷指令有没有跑 |
| 日志里 `no_mac` | 没配 `BPX1_MAC`（macOS 必需） |
| 日志里 `decrypt_failed` 持续增长 | bindkey 不是这台设备的，或 MAC 写错（nonce 用 MAC **反序**） |
| 只有 `scanner ACTIVE` 之后一片安静 | 多半是**蓝牙权限**：终端里手动跑一次 `bpx1_scan.py` 触发授权，再到 系统设置 → 隐私与安全性 → 蓝牙 确认 python 已勾选 |
| `iCloud 未检测到` | 打开 iCloud 云盘，或设 `BPX1_INBOX` 指向自定义目录 |
| 记录时间差了 8 小时 | 该型号时间基准可能是 UTC+8，见 protocol-notes §4「校验方法」 |
| 健康里出现重复记录 | 看 `done/` 是否有对应文件；处理办法见快捷指令文档「常见问题」 |

实时观察：

```bash
tail -f out/daemon.log
launchctl print gui/$(id -u)/com.mijia.bpx1scan | head -20
```

## 数据结构

`inbox/` 与 `done/` 里的文件名即 id：`bp-YYYYMMDD-HHMMSS-收缩压-舒张压-脉率.json`

```json
{
  "schema": 1,
  "id": "bp-20261010-163428-115-91-69",
  "time": "2026-10-10T16:34:28",
  "time_iso": "2026-10-10T16:34:28+08:00",
  "time_utc": "2026-10-10T08:34:28Z",
  "systolic": 115, "diastolic": 91, "heart_rate": 69,
  "flags": 0, "irregular_hint": false, "movement_hint": false,
  "time_suspect": false, "source": "bpx1-beacon",
  "device": "8C:D0:B2:AA:BB:CC", "rssi": -61,
  "object_id": 22043, "captured_at": "2026-10-10T16:34:29+08:00"
}
```

- `time` 为兼容老快捷指令的无时区写法；新搭建建议用 `time_iso`；
- `irregular_hint` / `movement_hint` 来自广播标志位，**语义未经官方确认**，仅作提示；
- `time_suspect: true` 表示设备时钟与 Mac 偏差较大（>5 分钟），时间可能不准。

## 目录结构

```
scripts/
  bpx1_protocol.py    # 协议层：帧解析/解密/时间/命名（纯逻辑，可单测）
  bpx1_config.py      # 环境变量、bindkey/MAC 加载、前置条件检查
  bpx1_store.py       # staging→inbox 原子写、done 幂等、journal
  bpx1_daemon.py      # 常驻守护（计数、告警、日志轮转、空闲回收）
  bpx1_scan.py        # 扫描 + 解密（测试用）
  bpx1_doctor.py      # 环境自检
  bpx1_inject.py      # 注入测试数据（验证手机端）
  get_beaconkey.py    # 从小米云获取 bindkey
  refill_inbox.py     # 从本地流水补写回 inbox
  install.sh          # 一键安装/卸载
tests/                # pytest（不需要蓝牙/真机）
launchd/              # launchd 模板（由 install.sh 渲染）
docs/
  protocol-notes.md   # 协议逆向记录（详细）
  iPhone快捷指令-导入血压.md
```

## 依赖与兼容

- Python ≥ 3.10；`bleak>=0.22`（实测 3.1）、`cryptography>=41`
- macOS 13+ 验证；iPhone 端依赖「快捷指令」的「记录健康样本」；「过滤文件」动作在较旧系统上可能没有，可用「如果」代替（见快捷指令文档）
- 其他型号：改 `scripts/bpx1_protocol.py` 里的 `PRODUCT_ID`；同系列大概率同构，需自行抓帧验证

## 隐私与安全

- **bindkey 等价于该设备的解密密钥**：`0600` 权限、别贴聊天/issue、别放 iCloud，泄露就等于把广播交出去；
- 健康数据在 iCloud Drive 里是**明文 JSON**：建议开启「高级数据保护」，并知悉 iCloud 备份同样包含它；
- 本项目不联网、不上传任何数据；小米云接口只用于取 bindkey（可选步骤）；
- 只监听**使用者本人自有设备**的广播；请勿用于任何未授权设备。

## 开发 / 测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # 协议向量、畸形帧、时区、幂等
.venv/bin/python -m ruff check .    # 只查 E9/F 级错误
```

协议层不 import bleak，所以 CPU/CI 上都能跑；`.github/workflows/ci.yml` 覆盖 3.10/3.12。

## 已知限制

- **广播窗口只有 1~2 秒**，Mac 睡眠/不在范围就永久缺失（设备不补发）；
- 需要一台长期在线的 Mac；想 7×24 更稳，可以换成树莓派/ESP32 常驻采集（未实现）；
- 设备未对时（比如换电池后）会产生不可信时间，v0.2 会拒收并提示，而不是写进健康；
- 快捷指令在"写一半失败"时仍可能产生重复记录（概率低），排查方法见文档。

## 法律与免责

- 非官方项目，与小米科技、iHealth 无任何关联；相关商标归各自权利人所有，本项目仅作描述性使用。
- 本项目仅用于读取**使用者本人自有设备**的蓝牙广播数据；bindkey 须通过使用者自己的账号获取。
- 仓库不含任何小米 / iHealth 的专有代码、固件或反编译产物；协议信息为独立的互操作性研究结果。
- `blt_get_beaconkey` 是未公开接口，随时可能变更或失效。
- 数据仅供参考，不构成医疗建议，不用于诊断；一切以设备与「健康」App 显示为准。

## 致谢

- [Do1e/mijia-api](https://github.com/Do1e/mijia-api) —— 小米云 API 登录与请求封装
- [PiotrMachowski/Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)（MIT）—— bindkey 云端接口的关键线索
- [Tinnci/anshin](https://github.com/Tinnci/anshin) —— BPX1 协议解析思路参考（protocol reference）
- MiBeacon 格式的社区公开资料（ESPHome `xiaomi_ble` 等）
- [MiEcosystem/miio_open](https://github.com/MiEcosystem/miio_open) —— 官方 BLE 接入文档

## License

[MIT](LICENSE)
