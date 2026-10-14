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
value[0:4]  测量时间  u32 LE —— 自 2000-01-01 00:00:00 (UTC+8) 起的秒数
value[4:6]  收缩压    u16 LE (mmHg)
value[6]    舒张压    u8     (mmHg)
value[7]    脉率      u8     (bpm)
value[8]    标志位    u8      bit0=心律不齐提示  bit1=体动  其余位未验证
```

### 实测样例（完整帧，已解密标注）

```
原始帧: 4858 b81f 02 | a13836970d6cb77a78580bb0 | 740000 | f6038438
        [fc ] [pid ] c   |     密文 (12B)          | rand3 |  tag

frameControl = 0x5848 → v5、加密、含对象、不含MAC
解密后载荷  = 1b56 09 14b55c3273005b4500
             [oid][len] └──────── 9B ────────┘
  time   = 0x325cb514 = 844800276 → 2026-10-10 16:34:28
  systolic = 115   diastolic = 91   pulse = 69   flags = 0x00
```

## 5. 广播行为观测

- **心跳帧**：约每 20~30 秒一次，无对象载荷、不加密（帧短）；
- **测量帧**：测量完成后 **1~2 秒内突发 6~30 帧**（内容一致），之后不再重播；
- 同一测量的重复帧可用 `(时间, 收缩压, 舒张压, 脉率)` 去重；
- 计数器随事件递增，可用于辅助去重。

## 6. bindkey 获取（使用者自己的账号）

- 接口：`POST /v2/device/blt_get_beaconkey`
- data：`{"did": "<设备DID>", "pdid": 1}` —— **必须带 `pdid`**，否则返回 `-8 invalid req`
- 环境：你账号所在区域的小米云 API（登录态即可，`mijiaAPI` / micloud 等均可）
- 返回：`{"beaconkey": "<32位hex>"}`（`did` 字段可能为空，属正常）
- 该接口线索来自社区项目 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)（MIT）

设备 DID 查法：米家 App → 设备 → 右上角「…」→ 设备信息。

## 7. 官方 App 通道（背景知识，本项目未使用）

官方米家 App 与设备的交互走的是**需要登录认证的私有 BLE GATT 会话**（标准认证握手 + 会话语钥加密），用于历史记录同步、设备设置等。本项目不依赖该通道——直接监听广播即可获得**每一次新测量**，且对设备零打扰、无需配对。

## 8. 排障要点

| 现象 | 检查 |
|------|------|
| 解密失败 | MAC 是否正确（nonce 用其**反序**）；bindkey 是否属于该设备 |
| 完全收不到 | Mac 蓝牙已开；测量时 Mac 在范围内且未睡眠（广播窗口仅 1~2 秒、不补发）|
| macOS 上帧里没有 MAC | 正常：需通过 `BPX1_MAC` 环境变量预配置设备 MAC |
| 重复导入 | 守护进程按 (时间,数值) 去重；手机端快捷指令导入后删除文件 |
| 想换其他型号 | 改 `PRODUCT_ID`；同系列大概率同构，需自行抓帧验证 |
