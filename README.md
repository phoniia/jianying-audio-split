# 剪映按停顿自动切分音频

在剪映专业版草稿里，把所有**超过 0.3 秒的停顿**处自动断开音频片段。

只切分，**不删任何内容、不改变总时长**，其它轨道完全不动 —— 替代手工在波形上一个一个 `Ctrl+B`。

> JianYing auto-split audio segments at pauses. Non-destructive: total duration stays identical,
> other tracks untouched. Pure local (ffmpeg + draft JSON rewriting), no cloud, no upload.

```
python kf_split_silence.py "我的口播稿"          # 预览：会切几刀、总长变化
python kf_split_silence.py "我的口播稿" --apply  # 真正写回
```

示例输出：

```
草稿：我的口播稿
       加密 / 总时长 503.23s
参数： 静音≥0.3s 门限-30.0dB 切点=mid 压静音正中 最短段0.2s

[轨道 3] audio / Music：2 段
  无需切分

[轨道 4] audio / (未命名)：98 段
  切出 78 刀 → 98 段变成 176 段

合计：新增 78 刀，1 条轨道受影响
时间线总长：503.23s → 503.23s（应保持不变）
素材表：audios 178 份 / beats 178 份（清理孤儿 audios 45、beats 45）
```

## 为什么需要这个

剪映自带的「智能剪口播 / 静音检测」都是**删内容**方向（删静音、删废词口误），
而逐句切段（保留全部内容，只是断开）只能手工一帧一帧做。
一个 100 秒的口播稿有 60 多个停顿，手工切一遍容易崩。

## 环境

- Windows（剪映专业版桌面端，草稿路径形态以 6.0 ~ 11.7 为准）
- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) 在 `PATH` 里（只用来做静音检测，本地算）
- 处理**加密草稿**需要 `pip install pycryptodome`（剪映 6.0+ 默认加密）

```bash
pip install pycryptodome
```

## 用法

```bash
# 草稿名（在草稿根目录下模糊匹配）
python kf_split_silence.py "我的口播稿"

# 完整路径最稳
python kf_split_silence.py --path "D:/.../JianyingPro Drafts/我的口播稿" --apply
```

草稿根目录默认取本机剪映的位置；换机器请直接给完整路径，
或设置环境变量 `JY_DRAFT_ROOT`。

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--min-silence` | `0.3` | 多长的静音算一次停顿（秒） |
| `--noise` | `-30` | 静音门限 dB。真人录音底噪大就放到 `-25`，环境嘈杂可到 `-20` |
| `--cut` | `mid` | 切点位置：`mid` 静音正中 / `head` 静音开始后 / `tail` 下一句之前 |
| `--pad` | `0.15` | `head` / `tail` 时的留白秒数（按时间轴计） |
| `--min-seg` | `0.2` | 切出来短于这个秒数就放弃这一刀（防碎段） |
| `--types` | `audio` | 处理的轨道类型，逗号分隔；默认只碰音频轨 |
| `--limit` | — | 只切 N 刀，试水用 |
| `--apply` | 关 | 不带就是预览，绝不写盘 |
| `--force` | 关 | 剪映还在跑也强写（**会白写**，别用） |
| `--restore` | — | 把正文回滚到指定备份文件 |

三个切点位置的实际差别：

```
静音区间 ├─────────────┤
          前一句尾   下一句头
  head ↑          ↑ mid          ↑ tail（= 下一句头 - pad）
```

`mid` 最"干净"（两边各留一半静音）；`tail --pad 0.19` 更像手工剪辑的手感
（句尾干净、句子开头带一点呼吸）。下面「实测」一节有依据。

## 写之前务必知道

1. **必须完全退出剪映，包括托盘里的 `JianyingProTray.exe`**。
   它手里握着草稿的内存状态，你写完它一保存就把文件覆盖回旧内容，
   表现为「明明写进去了，打开什么都没变」。脚本检测到剪映在跑会拒绝写入。
2. **先备份 / 先在副本上试**。脚本写盘前会 `snapshot_meta`，但草稿是你的，谨慎为上。
3. 打不开时用 `--restore` 回滚：

```bash
python kf_split_silence.py "我的口播稿" --restore "备份/draft_content.json.orig"
```
4. **反复加载失败后，草稿可能打不开但不是正文坏了**：
   `.backup/timeline_backup_manifest.json` 里若残留指向已删除快照的条目，
   剪映按清单取备份失败会直接拒绝加载。先跑
   `python kf_crypt.py manifest "草稿名"` 清理（写盘流程也会自动清）。

## 实测：检测准不准

拿一份真实口播稿做对照 —— 里面有一个 106.3 秒的 TTS 音频，作者**手工切成了 65 段**，
正好可以当标准答案：

| 检验项 | 结果 |
|---|---|
| ffmpeg `silencedetect -30dB:d=0.15` 检出的静音区间 | 58 个 |
| 手工切点落在检测区间内 | **63 / 63，100%**（零漏判、零误判） |
| 手工切点相对位置 | 平均在「下一句语音起点前 0.188s」 |
| 阈值 0.25s / 0.3s 会切几刀 | 63 刀 / 57 刀（手工 64 刀） |

也就是说：检测算法没有额外聪明的地方，`ffmpeg silencedetect` 就够用，
真正麻烦的是**把结果写回草稿**。

## 数据结构（从 65 段手工切分样本反推）

剪映切分一个音频片段时，实际做了这些事：

| 项目 | 结论 |
|---|---|
| 素材 | 每段复制一份 `materials.audios`，**保留原全长 `duration`**，只换 `id` |
| 引用 | 每段一整套新的 `extra_material_refs`（speed / beats / vocal_separation …），**无共享** |
| 时间轴 | 相邻段 `target_timerange` 缝隙 **0**，`source_timerange` 也严格首尾相接 |
| 换算 | `源时长 × speed = 时间轴时长`，65 段实测误差 **0.0ms** |
| 段 id | `materials.texts[].text_to_audio_ids` 会引用音频段 id |

所以写回时：首段保留原 `id`（文本关联还指着它），其余换新 GUID 并插入
`text_to_audio_ids`；源切点按静音位置算，时间轴位置按段内比例映射，
最后一段强制收回到原 `tgt_end` —— 总长零误差。

## 踩过的坑

这些坑都踩过，也都写进代码了。如果你要自己改 JSON，建议先看一眼：

- **孤儿素材会让草稿打不开**。切分时每段克隆一份素材，原来那份就没人引用了。
  正常草稿里 `materials.audios` 和 `materials.beats` 与音频段**严格 1:1，零孤儿**；
  多出一份，剪映直接拒绝加载。
  （反例：`speeds` / `placeholder_infos` / `sound_channel_mappings` / `vocal_separations`
  正常稿里本来就有一堆孤儿，删不删无所谓。）
- **JSON 必须紧凑**：`separators=(",", ":")`。带空格的 `{"id": "2B...` 剪映会判格式错误。
- **加密草稿不是简单 Base64**：AES-256-GCM，key 和 nonce 藏在密文文本自身的 12 个固定偏移处
  （每处 4 个字符）。见 `kf_crypt.py`。
- **`template-2.tmp` 和 `Timelines/<id>/draft_content.json` 是正文镜像**，
  写完不对齐它们，剪映会认为「文件被外部改过」。
- **`.backup` 里的旧快照 + `draft_content.json.log` 残留会让草稿永久打不开**（自锁）。
  写盘流程里 `sync_copies()` 负责按内容全量对齐，`clean_journal()` 负责清残留。
- **段内有 `common_keyframes`（音量包络）时直接跳过**，不猜它的拆分规则。

## 限制

- 只处理**音频轨**（`--types` 可以改，但视频轨的切分没实测过）
- 段内有音量关键帧的会跳过并提示
- 静音贴在段首/段尾（切了也是碎段）会跳过 —— 所以**重复跑是幂等的**
- 素材文件找不到的一律不动
- 剪映版本更新可能改字段；本脚本基于 6.0 ~ 11.7 的草稿结构，写入前请先预览

## 文件

| 文件 | 作用 |
|---|---|
| `kf_split_silence.py` | 主脚本：检测 + 切分 + 写回 |
| `kf_crypt.py` | 加密草稿离线解密 / 回加密（AES-256-GCM，需 `pycryptodome`） |

`kf_crypt.py` 也可以单独用：

```bash
python kf_crypt.py info    "草稿目录"   # 明文还是密文
python kf_crypt.py decrypt "草稿目录"   # 就地解密（原件存 .encbak）
python kf_crypt.py encrypt "草稿目录"   # 就地回加密
```

## 免责

非官方工具，与剪映 / 字节跳动无关。修改草稿有风险，**请务必在副本上先验证**。
加解密部分只用于读取和写回**你自己机器上、你自己的草稿**。
