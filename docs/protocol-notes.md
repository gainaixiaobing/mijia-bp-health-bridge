# 协议逆向记录 — 米家智能电子血压计（ihealth.bpm.bpx1）

> 本文档记录本项目用到的全部协议事实，供互操作性研究与排障参考。
> 内容来自对公开射频信号的观测分析与社区公开资料；不含任何厂商私有代码。

## 1. 设备标识

| 项 | 值 |
|----|----|
| 型号（米家内部名） | `ihealth.bpm.bpx1`，品牌名「米家智能电子血压计」 |
| MiBeacon 产品 ID | **8120**（帧内小端存储为 `B8 1F`） |
| 固件（实测版本） | `2.0.1_0005` |
| 广播服务 UUID | `0000fe95-0000-1000-8000-00805f9b34fb`（标准 MiBeacon） |

## 2. 广播帧结构（FE95 服务数据）

实测帧结构（字节序除注明外均为小端）：

```
偏移   字段              说明
0-1    frameControl u16  见下方位定义
2-3    productId   u16   = 8120
4      counter     u8    事件计数器
[5..]  可选字段（按 frameControl 指定位、按固定顺序出现）：
       - MAC(6B, 存储为反序)        当 bit4 (0x10) = 1
       - capability(1B, 含IO位时+1B) 当 bit5 (0x20) = 1
       - payload（TLV 对象序列）     当 bit6 (0x40) = 1
末尾   若加密（bit3=1）：[random 3B][tag 4B]
```

frameControl 位定义（观测值）：

```
bit15-12  协议版本（本设备观测为 v5）
bit8      0x0100  registered 标志（本设备观测为 0）
bit6      0x40    含对象载荷
bit5      0x20    含 capability
bit4      0x10    含设备 MAC（本设备实测【不含】）
bit3      0x08    加密
```

解析约束（v0.2 起在代码里强制执行）：

- **协议版本必须为 5**。其它版本直接拒收，避免固件换代后按老布局误读；
- capability 位存在时必须真的读到那个字节，否则整帧丢弃（v0.1 会越界抛异常）；
- 整帧建议不超过 31 字节（BLE 传统广播上限）。长度异常时按 `bad_frame` 丢弃。

## 3. 加密方式（MiBeacon 标准）

- 算法：**AES-CCM**，tag 长度 **4 字节**
- 密钥：**bindkey**（16 字节，每设备唯一）
- nonce（12 字节）= `MAC 反序(6B) ｜ productId+counter(3B) ｜ random(3B)`
  - `productId+counter` 即帧中偏移 2~4 的 3 个字节原样
  - `random` 即帧尾 tag 前 3 字节
- AAD（附加认证数据）：`0x11`（1 字节）
- 密文范围：偏移 5（含可选字段）到「帧尾-7」；帧尾 7 字节 = random(3) + tag(4)

## 4. 测量对象（解密后载荷）

载荷为 TLV 序列：`[objectId u16][length u8][value...]`

血压测量对象（实测 objectId = `0x561B`，length = **9**）：

```
value[0:4]  测量时间  u32 LE —— 自 2000-01-01T00:00:00Z 起的秒数（Unix 946684800）
value[4:6]  收缩压    u16 LE (mmHg)
value[6]    舒张压    u8     (mmHg)
value[7]    脉率      u8     (bpm)
value[8]    标志位    u8     含义未验证（实测样例为 0x00，见下）
```

> ⚠️ **时间基准**：设备按 **UTC** 计数，本身不带时区；"转换成本地时间"是读取方的事。
> v0.1 文档曾把基准写成 `2000-01-01 00:00:00 (UTC+8)`，若照此实现会整体偏 **8 小时**
> （同一帧会渲染成 08:34:28 而不是 16:34:28）。以本文为准，并按下面「校验方法」实测确认。

**对象识别规则**：必须 `objectId == 0x561B && length == 9`。
v0.1 只判断 `length == 9`，会把任何 9 字节对象当成血压 —— 同一帧里出现其它 9 字节对象时会静默读错。

**标志位**：bit0 / bit1 在实测中常为 0，社区资料多解读为「心律不齐」「体动」，但未获官方确认。
代码里输出为 `irregular_hint` / `movement_hint`，仅作提示；「健康」App 的血压样本也没有对应字段可写。

### 实测样例（完整帧，已解密标注）

```
原始帧: 4858 b81f 02 | a13836970d6cb77a78580bb0 | 740000 | f6038438
        [fc ] [pid ] c   |     密文 (12B)          | rand3 |  tag

frameControl = 0x5848 → v5、加密、含对象、不含MAC
解密后载荷  = 1b56 09 14b55c3273005b4500
             [oid][len] └──────── 9B ────────┘
  time   = 0x325cb514 = 844936468（十进制）→ 2026-10-10 16:34:28 (+08:00)
  systolic = 115   diastolic = 91   pulse = 69   flags = 0x00
```

**校验方法**（换型号/换固件时建议做一次）：

1. 量一次血压，记下血压计屏幕上显示的**时间与数值**；
2. 跑 `python3 scripts/bpx1_scan.py 30`，对比终端输出的时间；
3. 若整体偏移 8 小时，说明该型号用的是 UTC+8 基准 —— 修改 `bpx1_protocol.py` 里的
   `EPOCH_2000_UTC`（改为 `946656000`）并同步更新测试向量。

## 5. 广播行为观测

- **心跳帧**：约每 20~30 秒一次，无对象载荷、不加密（帧短）；
- **测量帧**：测量完成后 **1~2 秒内突发 6~30 帧**（内容一致），之后不再重播；
- 同一测量的重复帧用 `(测量时间, 收缩压, 舒张压, 脉率)` 去重（`Measurement.key`）；
- 计数器随事件递增，可用于辅助去重，但不是唯一依据。

## 6. bindkey 获取（使用者自己的账号）

- 接口：`POST /v2/device/blt_get_beaconkey`
- data：`{"did": "<设备DID>", "pdid": 1}` —— **必须带 `pdid`**，否则返回 `-8 invalid req`
- 环境：你账号所在区域的小米云 API（登录态即可，`mijiaAPI` / micloud 等均可）
- 返回：`{"beaconkey": "<32位hex>"}`（`did` 字段可能为空，属正常）
- 该接口线索来自社区项目 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)（MIT）
- ⚠️ 未公开接口，随时可能变更；本项目把它封在 `get_beaconkey.py::fetch_beaconkey()` 一处。

设备 DID 查法：米家 App → 设备 → 右上角「…」→ 设备信息。

## 7. 官方 App 通道（背景知识，本项目未使用）

官方米家 App 与设备的交互走的是**需要登录认证的私有 BLE GATT 会话**（标准认证握手 + 会话语钥加密），
用于历史记录同步、设备设置等。本项目不依赖该通道——直接监听广播即可获得**每一次新测量**，
且对设备零打扰、无需配对。

## 8. 解析结果与原因码

`parse_frame()` 返回 `(measurement | None, reason)`。实际运行时每个原因码都会计数，
连续异常会打 WARN（v0.1 是静默丢弃）：

| reason | 含义 | 通常原因 / 处理 |
|--------|------|-----------------|
| `ok` | 解析成功 | — |
| `short` | 帧长 < 5 | 截断的广播，忽略 |
| `other_pid` | 产品 ID 不是 8120 | 附近其它米家设备，正常 |
| `unsupported_version` | 协议版本 ≠ 5 | 固件换代，需要重新抓帧核对布局 |
| `no_mac` | 帧内无 MAC 且未配置 `BPX1_MAC` | 设置设备 MAC（macOS 必需） |
| `truncated` | 字段/密文不足 | 广播被截断，或配置错误 |
| `bad_frame` | capability/对象长度不自洽 | 固件改版或帧损坏 |
| `heartbeat` | 无载荷心跳 | 正常，不用管 |
| `decrypt_failed` | AES-CCM 校验失败 | bindkey 不匹配，或 MAC 写错（nonce 用反序） |
| `bad_values` | 数值越界 / 收缩压 ≤ 舒张压 | 解析错位或设备异常 |
| `time_suspect` | 时间偏差 > 24 小时 | 设备未对时（换电池后常见），拒收 |
| `no_measurement` | 没有 0x561B 对象 | 心跳或其它对象，正常 |

另外，`now` 与测量时间相差 5 分钟~24 小时时仍会写入，但记录里 `time_suspect: true`，
用于提醒"这次的时间可能不准"。

## 9. 记录格式与幂等

落盘文件名 / `id`：`bp-YYYYMMDD-HHMMSS-收缩压-舒张压-脉率`（本地时间）。
同一次测量永远得到同一个 id，因此重复广播/重复补写天然幂等。

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

- `time` 是兼容 v0.1 快捷指令的写法（无时区）；新搭建建议用 `time_iso`；
- `done/<id>.json` 表示"手机端已导入"，守护进程与 refill 都不会再补发；
- 临时文件写在 `staging/` 再 rename 进 `inbox/`，所以 inbox 里不会出现 `.tmp` 中间态。

## 10. 排障要点

| 现象 | 检查 |
|------|------|
| 解密失败（decrypt_failed 持续） | MAC 是否正确（nonce 用其**反序**）；bindkey 是否属于该设备 |
| 完全收不到 | Mac 蓝牙已开；测量时 Mac 在范围内且未睡眠（广播窗口仅 1~2 秒、不补发）|
| macOS 上帧里没有 MAC | 正常：需通过 `BPX1_MAC` 预配置设备 MAC |
| 时间差 8 小时 | 该型号时间基准可能是 UTC+8，按 §4「校验方法」核对 |
| 记录时间在 2000 年 | 设备未对时；v0.2 会拒收并打 WARN |
| 重复导入 | 先看 `done/` 是否有标记；见快捷指令文档的排查章节 |
| 想换其他型号 | 改 `PRODUCT_ID`；同系列大概率同构，需自行抓帧验证 |
