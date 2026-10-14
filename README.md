# mijia-bp-health-bridge · 米家血压计 → Apple 健康

把 **米家智能电子血压计**（`ihealth.bpm.bpx1`）的每次测量自动送进 iPhone 的「健康」App。

**无需开发者账号 · 无需连接设备 · 无需改动血压计** —— Mac 被动监听血压计的蓝牙广播并解密，经 iCloud Drive 中转，由 iPhone 快捷指令写入健康。

> ⚠️ 非官方项目，与小米 / iHealth 无任何关联；仅供读取使用者本人自有设备的数据。

---

## 工作原理

```
┌─────────────┐  BLE 加密广播   ┌─────────────────────────┐
│ 米家血压计   │ ─────────────▶ │ Mac 守护进程             │
│ (测量后广播) │   (MiBeacon)   │ 监听 + 解密 + 去重        │
└─────────────┘                └───────────┬─────────────┘
                                           │ 写入 JSON 文件
                                           ▼
                             iCloud Drive / bp-health / inbox/
                                           │
                                           ▼
                              ┌──────────────────────┐
                              │ iPhone 快捷指令       │
                              │ 读取 → 写入健康 App   │
                              │ → 删除文件（防重复）   │
                              └──────────────────────┘
```

- 测量完成后 1~2 秒内，血压计会以蓝牙广播（MiBeacon）**突发**发出加密的测量数据帧；
- Mac 用从**你自己的小米云账号**取得的 bindkey 被动解码 —— 甚至不需要与设备建立连接；
- 数据经 iCloud Drive 落到手机，由快捷指令写进「健康」（血压 + 心率，含测量时间）。

## 主要发现（逆向记录摘要）

| # | 发现 | 说明 |
|---|------|------|
| 1 | **测量数据是广播出来的** | 服务 UUID `FE95`（MiBeacon），产品 ID **8120**；平时只有无载荷心跳（每 20~30 秒） |
| 2 | **广播是加密的** | AES-CCM（4 字节 tag）：nonce = MAC反序 + 产品号 + 计数器 + 随机数；AAD = `0x11`；密钥 = 16 字节 **bindkey** |
| 3 | **测量对象格式（TLV，长度 9）** | `时间 u32LE（2000-01-01 起秒）｜收缩压 u16LE｜舒张压 u8｜脉率 u8｜标志位 u8` |
| 4 | **bindkey 可从自己的账号获取** | 小米云接口 `/v2/device/blt_get_beaconkey`（data 中必须带 `"pdid": 1`） |
| 5 | **官方 App 通道完全不同** | 官方走的是需登录认证的私有 GATT 会话协议；本项目**不需要**它，零打扰 |

完整协议记录（帧结构逐字节解析、实测样例、避坑要点）见 👉 **[docs/protocol-notes.md](docs/protocol-notes.md)**

## 快速开始

### 0. 准备
- 一台 Mac（本项目在 macOS 验证）+ 一台米家智能电子血压计（`ihealth.bpm.bpx1`）
- Python ≥ 3.10：`pip install bleak cryptography`
- iPhone 与 Mac 登录同一 iCloud 账号，并开启 iCloud Drive

### 1. 取得 bindkey（解密密钥）
```bash
pip install mijiaAPI                    # https://github.com/Do1e/mijia-api
python3 scripts/get_beaconkey.py blt.3.xxxxxx
# 首次使用需按 mijiaAPI 文档完成一次登录（生成 auth.json）
```
也可以使用社区工具 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)（MIT）读取其中的「BLE KEY」。

把结果（32 位 hex）写入仓库根目录 `bp_bindkey.hex`，并记下设备 MAC（形如 `8C:D0:B2:xx:xx:xx`；米家 App → 设备 → 右上角「…」→ 设备信息 可查）。

### 2. 测试：扫描解码
```bash
export BPX1_MAC=8C:D0:B2:xx:xx:xx       # 你的设备 MAC（macOS 上必需）
python3 scripts/bpx1_scan.py 90         # 扫描 90 秒
```
对着血压计做一次测量 —— 终端里应打印出解出的读数。

### 3. 安装常驻守护（推荐）
```bash
mkdir -p out
cp launchd/com.mijia.bpx1scan.plist.template ~/Library/LaunchAgents/com.mijia.bpx1scan.plist
# 按文件内注释替换 __PYTHON__ / __SCRIPT__ / __LOG__ 三个占位符后再加载
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mijia.bpx1scan.plist
```
守护进程会持续监听，每次捕获新测量就写一个 JSON 到
`iCloud Drive/bp-health/inbox/`（可用 `BPX1_INBOX` 环境变量改路径）。

### 4. iPhone 快捷指令
按照 **[docs/iPhone快捷指令-导入血压.md](docs/iPhone快捷指令-导入血压.md)** 创建一个快捷指令：
读取 inbox → 写入「健康」（血压/心率）→ 删除已导入文件。
可手动运行，也可设为「连接家里 Wi-Fi 自动运行」。

## 目录结构
```
scripts/
  bpx1_scan.py        # 扫描 + 解密（测试用）
  bpx1_daemon.py      # 常驻守护（正式用）
  get_beaconkey.py    # 从小米云获取 bindkey
  refill_inbox.py     # 把历史记录补写回 inbox（调试用）
launchd/
  com.mijia.bpx1scan.plist.template
docs/
  protocol-notes.md   # 协议逆向记录（详细）
  iPhone快捷指令-导入血压.md
```

## 常见问题
- **没收到数据？** 广播只在测量完成后 1~2 秒内发出、不会补发；测量时 Mac 需醒着且在蓝牙范围内。
- **Mac 睡眠期间**：收不到（合盖即睡）。开盖后自动恢复监听。
- **健康 App 没出现记录？** iPhone 设置 → 健康 → 数据访问与设备 → 快捷指令 → 打开「血压」「心率」写入开关。
- **其他型号？** 本方案针对 `ihealth.bpm.bpx1`；同系列大概率同构（产品 ID 不同，需自行验证并修改 `PRODUCT_ID`）。

## 法律与免责
- 非官方项目，与小米科技、iHealth 无任何关联；相关商标归各自权利人所有，本项目仅作描述性使用。
- 本项目仅用于读取**使用者本人自有设备**的蓝牙广播数据；bindkey 须通过使用者自己的账号获取。**请勿用于任何未授权设备。**
- 仓库不含任何小米 / iHealth 的专有代码、固件或反编译产物；协议信息为独立的互操作性研究结果。
- 数据仅供参考，不构成医疗建议；一切以设备与「健康」App 显示为准。

## 致谢
- [Do1e/mijia-api](https://github.com/Do1e/mijia-api) —— 小米云 API 登录与请求封装
- [PiotrMachowski/Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)（MIT）—— bindkey 云端接口的关键线索
- [Tinnci/anshin](https://github.com/Tinnci/anshin) —— BPX1 协议解析思路参考（protocol reference）
- MiBeacon 格式的社区公开资料（ESPHome `xiaomi_ble` 等）
- [MiEcosystem/miio_open](https://github.com/MiEcosystem/miio_open) —— 官方 BLE 接入文档

## License
[MIT](LICENSE)
