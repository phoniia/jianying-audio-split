#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪映草稿加解密（AES-256-GCM，密钥藏在密文文本自身）。

原理（逆向自 JianyingPro 的 EncryptUtils）：
  加密后的 draft_content.json / draft_meta_info.json 是一行 base64 文本。
  该文本在 12 个固定字符偏移处，各"夹带" 4 个字符：
      前 8 组共 32 字符 -> AES-256 key
      后 4 组共 16 字符 -> GCM nonce
  把这 48 个字符剔除，剩下才是纯密文的 base64；解出 ciphertext || GCM-tag。

因为 key/iv 随文件走，所以不依赖剪映版本、机器、也不需要 hook 进程。
反过来加密时，自己随机生成这 48 个字符即可，剪映读的时候同样能从偏移处取出。

依赖 pycryptodome（pip install pycryptodome）。缺依赖时本模块会优雅降级，
调用方应先用 crypto_available() 判断。
"""

from __future__ import annotations

import base64
import json
import os
import random
import shutil
import time
import uuid
from pathlib import Path

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover
    AES = None

OFFSETS = (0x00, 0x07, 0x14, 0x21, 0x28, 0x2F, 0x3B, 0x42, 0x4C, 0x59, 0x63, 0x7F)
TAG_LEN = 16
MIN_LEN = 131
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

# 剪映会加密这两个文件；其余（template.tmp / key_value.json 等）是明文
ENCRYPTED_FILES = ("draft_content.json", "draft_meta_info.json")
# draft_content.json 的镜像副本，剪映会读它，必须同步
CONTENT_MIRRORS = ("template-2.tmp",)


def crypto_available() -> bool:
    return AES is not None


def _ranges():
    return [(o, o + 4) for o in OFFSETS]


def _in_ranges(i: int) -> bool:
    return any(a <= i < b for a, b in _ranges())


def is_plaintext_bytes(data: bytes) -> bool:
    return data.lstrip()[:1] == b"{"


def is_encrypted(path: Path) -> bool:
    """文件存在且不是明文 JSON，视为加密。"""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    return bool(head) and not is_plaintext_bytes(head)


def decrypt_bytes(data: bytes) -> bytes:
    if AES is None:
        raise RuntimeError("缺少 pycryptodome：pip install pycryptodome")
    if is_plaintext_bytes(data):
        raise ValueError("已是明文 JSON，无需解密")

    text = data.decode("latin1").strip()
    if len(text) < MIN_LEN:
        raise ValueError(f"文件过短（{len(text)} < {MIN_LEN}），不是剪映加密格式")

    key = "".join(text[o:o + 4] for o in OFFSETS[:8]).encode("latin1")
    iv = "".join(text[o:o + 4] for o in OFFSETS[8:]).encode("latin1")
    stripped = "".join(c for i, c in enumerate(text) if not _in_ranges(i))
    if len(stripped) % 4:
        raise ValueError("剔除密钥后 base64 长度异常，格式不符")

    raw = base64.b64decode(stripped, validate=True)
    if len(raw) <= TAG_LEN:
        raise ValueError("密文过短")
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    return cipher.decrypt_and_verify(raw[:-TAG_LEN], raw[-TAG_LEN:])


def encrypt_bytes(plain: bytes, seed: int | None = None) -> bytes:
    """把明文 JSON 加密成剪映可读的一行 base64 文本。"""
    if AES is None:
        raise RuntimeError("缺少 pycryptodome：pip install pycryptodome")

    rnd = random.Random(seed) if seed is not None else random.SystemRandom()
    mux = "".join(rnd.choice(ALPHABET) for _ in range(48))
    key, iv = mux[:32].encode("latin1"), mux[32:].encode("latin1")
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    ct, tag = cipher.encrypt_and_digest(plain)
    b64 = base64.b64encode(ct + tag).decode("ascii")

    total = len(b64) + 48
    out = bytearray(total)
    bi = ki = 0
    for i in range(total):
        if _in_ranges(i):
            out[i] = ord(mux[ki])
            ki += 1
        else:
            out[i] = ord(b64[bi])
            bi += 1
    return bytes(out)


# --------------------------------------------------------------------------
# 草稿级操作
# --------------------------------------------------------------------------
def timeline_dirs(project: Path) -> list[Path]:
    """剪映 11.x 把每条时间线的正文放在 Timelines/<timeline_id>/ 下。

    根目录的 draft_content.json 只是它的镜像：剪映加载时以时间线目录为准，
    保存时再回头覆盖根目录那份。只改根目录 → 打开看到的还是时间线里的旧数据。
    """
    tl = project / "Timelines"
    if not tl.is_dir():
        return []
    return sorted(d for d in tl.iterdir()
                  if d.is_dir() and (d / "draft_content.json").is_file())


def content_targets(project: Path) -> list[Path]:
    """所有承载正文的文件：根目录一份 + 每条时间线各一份。"""
    out = []
    root_c = project / "draft_content.json"
    if root_c.is_file():
        out.append(root_c)
    out.extend(d / "draft_content.json" for d in timeline_dirs(project))
    return out


def propagate_content(project: Path) -> int:
    """把根目录正文同步到每条时间线目录（明文阶段调用）。

    剪映以时间线为真身，所以改完根目录必须再推一份过去，否则等于没改。
    返回实际改写的文件数。
    """
    src = project / "draft_content.json"
    if not src.is_file():
        return 0
    data = src.read_bytes()
    n = 0
    for d in timeline_dirs(project):
        dst = d / "draft_content.json"
        try:
            if dst.read_bytes() == data:
                continue
        except OSError:
            pass
        dst.write_bytes(data)
        n += 1
    return n


def encrypted_targets(project: Path) -> list[Path]:
    """返回需要解密的文件（仅存在的），含各条时间线目录里的正文。"""
    out = content_targets(project)
    meta = project / "draft_meta_info.json"
    if meta.is_file():
        out.append(meta)
    return out


def draft_is_encrypted(project: Path) -> bool:
    return is_encrypted(project / "draft_content.json")


def decrypt_in_place(project: Path) -> list[str]:
    """原地解密，返回处理过的文件名。调用方负责先备份。"""
    done = []
    for p in encrypted_targets(project):
        raw = p.read_bytes()
        if is_plaintext_bytes(raw):
            continue
        p.write_bytes(decrypt_bytes(raw))
        done.append(p.name)
    return done


def encrypt_in_place(project: Path, cleanup: bool = True) -> list[str]:
    """原地加密（把明文写回成密文），并同步正文镜像。

    cleanup: 顺手清掉 capcut-cli 在草稿目录里留下的明文快照
             （.capcut-cli-history/、draft_content.json.bak），
             否则加密草稿里会残留一份可直接阅读的明文。
             原密文已由调用方备份为 *.encbak，不受影响。
    """
    done = []
    # 明文阶段先把根目录正文推到各时间线目录，再逐个加密
    pushed = propagate_content(project)
    root_c = (project / "draft_content.json").resolve()
    for p in encrypted_targets(project):
        if p.name == "draft_content.json" and p.resolve() != root_c:
            continue  # 时间线正文稍后直接用根的密文覆盖，保证字节完全一致
        raw = p.read_bytes()
        if not is_plaintext_bytes(raw):
            continue
        p.write_bytes(encrypt_bytes(raw))
        done.append(p.name)

    # 时间线正文 = 根的密文（同一份字节，避免各处 nonce 不同带来歧义）
    if root_c.is_file():
        data = root_c.read_bytes()
        for d in timeline_dirs(project):
            dst = d / "draft_content.json"
            if dst.read_bytes() != data:
                dst.write_bytes(data)

    if pushed:
        print(f"  已同步正文到 {pushed} 个时间线目录（Timelines/）")

    # 所有镜像（根 template-2.tmp + 时间线目录里的 tmp）都对齐到根正文
    mirror_content(project)
    # 剪映的 .bak / .backup 快照也一起刷新，否则打开时会被旧快照回滚
    fixed = sync_copies(project)
    if fixed:
        print(f"  已刷新 {fixed} 处旧副本（.bak/.backup），防止剪映打开时回滚")
    touch_meta(project)

    if cleanup:
        _clean_plaintext_residue(project)
    return done


def _clean_plaintext_residue(project: Path) -> None:
    """清掉工具链留下的明文副本（只删确实是明文的备份，不碰密文）。"""
    hist = project / ".capcut-cli-history"
    if hist.is_dir():
        shutil.rmtree(hist, ignore_errors=True)
    candidates = list(project.glob("draft_content.json.bak*")) + list(project.glob("draft_info.json.bak*"))
    # 时间线目录里也会被工具链留下明文 bak
    for d in timeline_dirs(project):
        candidates += [p for p in d.glob("*.bak*") if p.is_file()]
    for p in candidates:
        if not p.is_file():
            continue
        try:
            head = p.read_bytes()[:64]
        except OSError:
            continue
        if is_plaintext_bytes(head):
            try:
                p.unlink()
            except OSError:
                pass


def mirror_content(project: Path) -> bool:
    """把正文同步到各处镜像（根目录 template-2.tmp 与时间线目录下的 tmp）。"""
    src = project / "draft_content.json"
    if not src.is_file():
        return False
    data = src.read_bytes()
    touched = False

    def sync(m: Path) -> bool:
        nonlocal touched
        try:
            if m.is_file() and m.read_bytes() == data:
                return False
            m.write_bytes(data)
            touched = True
            return True
        except OSError:
            return False

    for mirror in CONTENT_MIRRORS:
        sync(project / mirror)
    # 时间线目录里只有 template-2.tmp 是正文镜像。
    # template.tmp 不是！它是明文的「默认空骨架」（tracks:[] / duration:0 /
    # source:"default"，约 4KB），覆盖成正文会让剪映 11.7 打不开草稿
    # （2026-11-22 实测，踩了两次）。这里绝不能碰它。
    for d in timeline_dirs(project):
        sync(d / CONTENT_MIRRORS[0])
    return touched


# --------------------------------------------------------------------------
# 让“旧副本”跟上正文：这是原地写入不被剪映回滚的关键
# --------------------------------------------------------------------------
def sync_copies(project: Path) -> int:
    """把正文刷新到剪映自己的各处副本，避免下次打开时被回滚。

    踩过的坑：剪映 11.x 打开草稿时会比对 draft_content.json 与
    draft_content.json.bak / .backup 里的快照。发现不一致（= 被外部改过）
    就判定为异常，把旧快照还原回来，并在 .backup 里留下 *.restore.bak /
    *.template.bak。表现就是“脚本写完、剪映里什么也没变”。
    克隆稿因为 clone_draft 忽略了 *.bak 和 .backup，没有旧快照可比，
    所以一直是正常的——这条线索才把根因定位出来。

    返回刷新的副本数量。
    """
    root = project / "draft_content.json"
    if not root.is_file():
        return 0
    blob = root.read_bytes()
    n = 0

    def sync(p: Path) -> None:
        nonlocal n
        try:
            if p.is_file() and p.read_bytes() != blob:
                p.write_bytes(blob)
                n += 1
        except OSError:
            pass

    # 1) 与正文同级的 .bak（剪映的“上次保存”副本）
    sync(project / "draft_content.json.bak")
    sync(project / "template-2.tmp.bak")
    timelines = timeline_dirs(project)
    for d in timelines:
        sync(d / "draft_content.json.bak")

    # 2) .backup 快照：新增一条（时间戳最新），并让最新那份 close/save 快照
    #    也等于新正文，这样无论剪映按“时间戳最新”还是“上次关闭”恢复，都回到我们这版
    bk = project / ".backup"
    fname = ""
    if bk.is_dir():
        ts = time.strftime("%Y%m%d%H%M%S")
        fname = f"{ts}_{uuid.uuid4().hex}.save.bak"
        try:
            (bk / fname).write_bytes(blob)
            n += 1
        except OSError:
            pass
        manifest = bk / "timeline_backup_manifest.json"
        for d in timelines:
            sub = bk / d.name
            if not sub.is_dir():
                continue
            try:
                (sub / fname).write_bytes(blob)
                n += 1
            except OSError:
                pass
            snaps = sorted(
                (p for p in sub.glob("*.bak") if p.name != fname),
                key=lambda p: p.name,
            )
            if snaps:
                try:
                    snaps[-1].write_bytes(blob)
                    n += 1
                except OSError:
                    pass
        if manifest.is_file():
            try:
                m = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                m = {}
            for d in timelines:
                if isinstance(m.get(d.name), dict):
                    m[d.name][ts] = fname
            try:
                manifest.write_text(
                    json.dumps(m, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
            except OSError:
                pass

    # 3) 关键：把 .backup 里**所有**旧快照都对齐成新正文，不按体积筛选。
    #
    #    2026-11-22 定位到的真凶（三份稿对照，规律完全吻合）：
    #      能开的稿 → .backup 里最新 close.bak 与正文逐字段 0 差异
    #        模式3 副本(1)：close.bak 34636B == 正文
    #        模式4 kf验证-m4：close.bak 34608B == 正文
    #      打不开的稿 → close.bak 还是「改前」内容
    #        模式4 副本(2)：close.bak 34604B（段长还是 2.12s）vs 正文 34608B
    #
    #    剪映加载时拿 close.bak / load.bak 当恢复源，一旦发现与正文不一致
    #    就判「被外部改过」→ 回滚旧版 → 加载失败，并留下
    #    Timelines/<id>/draft_content.json.log；journal 残留会让之后每次加载
    #    都走恢复分支继续失败（自锁），clone_draft 继承后新稿也打不开，
    #    于是表现为「模式4 就是坏的，而模式3 好的」。
    #
    #    旧代码用 `p.stat().st_size == root.stat().st_size` 当门槛，恰好是
    #    这 4 字节的差（34604 vs 34608）让改前快照被跳过 —— 必须改成按内容对齐。
    if bk.is_dir():
        targets = [p for p in bk.iterdir() if p.is_file() and p.suffix == ".bak"]
        for d in timelines:
            sub = bk / d.name
            if sub.is_dir():
                targets += [p for p in sub.iterdir()
                            if p.is_file() and p.suffix == ".bak"]
        for p in targets:
            if p.name == fname:
                continue
            try:
                if p.read_bytes() != blob:
                    p.write_bytes(blob)
                    n += 1
            except OSError:
                pass

    # 4) 顺手清掉加载失败残留的 journal，否则下次还会自锁
    clean_journal(project)

    # 5) 清单里若有条目指向已被删掉的快照，剪映按清单找不到文件会直接拒绝加载。
    #    sync_copies 只往清单里加条目、从不删；而加载失败时剪映/工具会清掉旧
    #    load.bak，于是清单越来越长、失效条目越来越多，最终整份草稿打不开。
    pruned = prune_backup_manifest(project)
    if pruned:
        print(f"  已剔除 {pruned} 条失效备份清单记录")
    return n


def clean_journal(project: Path) -> list[str]:
    """删掉剪映加载失败残留的 draft_content.json.log。

    剪映正常加载成功后会自动删除它；加载失败就残留。残留的 journal 会让
    下一次加载继续走「恢复未完成写入」分支而再次失败，形成自锁；
    clone_draft 也会把它一起复制过去，所以「克隆一份也打不开」。
    它只是 journal，删掉不丢任何内容（正文/备份都在）。
    """
    gone: list[str] = []
    cands = [project / "draft_content.json.log"]
    cands += [d / "draft_content.json.log" for d in timeline_dirs(project)]
    for p in cands:
        if p.is_file():
            try:
                p.unlink()
                gone.append(p.name)
            except OSError:
                pass
    return gone


def prune_backup_manifest(project: Path) -> int:
    """删除 .backup/timeline_backup_manifest.json 中指向不存在快照的条目。

    2026-11-25 定位到的真凶：失败副本的清单有 57 条，其中 22 条指向已被清掉的
    load.bak。剪映加载时按这份清单逐条取备份，有一条找不到就判草稿损坏，
    随后不断写 load.bak + journal，形成自锁。克隆稿没有继承旧清单（只有 4 条
    且全部有效），所以正文/外壳完全相同的克隆稿却都能正常打开。

    返回剔除的条目数；清单不存在或无法解析时返回 0。
    """
    manifest = project / ".backup" / "timeline_backup_manifest.json"
    if not manifest.is_file():
        return 0
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(m, dict):
        return 0

    removed = 0
    for folder, entries in list(m.items()):
        if not isinstance(entries, dict):
            continue
        base = project / ".backup" / folder
        for ts, name in list(entries.items()):
            if not isinstance(name, str) or not (base / name).is_file():
                del entries[ts]
                removed += 1
        if not entries:
            del m[folder]
    if removed:
        try:
            manifest.write_text(
                json.dumps(m, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        except OSError:
            return 0
    return removed


def touch_meta(project: Path) -> bool:
    """把 draft_meta_info.json 的 tm_draft_modified 更新到当前时间。

    正常保存时剪映会把正文和 meta 一起写，两者的时间戳是吻合的；
    我们只改正文，meta 还停在“上次保存”，部分版本会据此判定文件被外部改动。
    """
    p = project / "draft_meta_info.json"
    if not p.is_file():
        return False
    raw = p.read_bytes()
    enc = raw.lstrip()[:1] != b"{"
    try:
        d = json.loads(decrypt_bytes(raw) if enc else raw)
    except Exception:
        return False
    stamp = time.time_ns() // 1000
    if d.get("tm_draft_modified") == stamp:
        return False
    d["tm_draft_modified"] = stamp
    # 紧凑分隔符：剪映自己写的 meta 也没有空格。带空格写回后剪映读不了 meta，
    # 表现是「草稿列表里点不开」，连正文都读不到（2026-11-22 实测）。
    out = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    p.write_bytes(encrypt_bytes(out) if enc else out)
    return True


def clear_stale_lock(project: Path) -> bool:
    """剪映退出后常留下 0 字节的 .locked；残留会被误判成“另一个实例还开着”。"""
    lock = project / ".locked"
    if lock.is_file() and lock.stat().st_size == 0:
        try:
            lock.unlink()
            return True
        except OSError:
            return False
    return False


# --------------------------------------------------------------------------
# 元信息：复制草稿时改名字/ID，避免剪映把两个目录当成同一份
# --------------------------------------------------------------------------
def retarget_meta(project: Path, new_name: str, new_id: str) -> None:
    """改 draft_meta_info.json 里的 draft_name / draft_id / draft_fold_path。

    必须在解密状态下调用。文件不存在或仍是密文时静默跳过。
    """
    import json
    p = project / "draft_meta_info.json"
    if not p.is_file():
        return
    raw = p.read_bytes()
    if not is_plaintext_bytes(raw):
        return
    try:
        meta = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return
    meta["draft_name"] = new_name
    meta["draft_id"] = new_id
    meta["draft_fold_path"] = str(project.resolve()).replace("\\", "/")
    p.write_bytes(json.dumps(meta, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))


def ensure_meta_identity(project: Path) -> list[str]:
    """把 meta 的身份字段对齐到它当前所在的目录。

    在资源管理器里重命名/复制草稿文件夹时，Windows 只改目录名，草稿内部的
    `draft_meta_info.json` 不会跟着改：`draft_name` 还是旧名字、
    `draft_fold_path` 指向一个已不存在（或属于别的草稿）的目录、
    `draft_id` 还和原草稿共用。

    剪映遇到这种稿会直接打不开。2026-11-22 实测：`workbuddy2-副本` 的 meta
    仍指向 `11月22日 (1)`、和 `workbuddy2` 共用 draft_id，无论补多少时长都开不了；
    而内容完全相同、只是 meta 正确的克隆稿可以正常打开。

    返回做过哪些修改；空列表表示本来就对。加密/明文两种状态都支持。
    """
    p = project / "draft_meta_info.json"
    if not p.is_file():
        return []
    raw = p.read_bytes()
    enc = not is_plaintext_bytes(raw)
    try:
        d = json.loads(decrypt_bytes(raw) if enc else raw)
    except Exception:
        return []
    if not isinstance(d, dict):
        return []

    name = project.name
    want = str(project.resolve()).replace("\\", "/")
    have = (d.get("draft_fold_path") or "").replace("\\", "/")
    changes: list[str] = []

    renamed = d.get("draft_name") != name or have != want
    if d.get("draft_name") != name:
        changes.append(f"draft_name: {d.get('draft_name')!r} → {name!r}")
        d["draft_name"] = name
    if have != want:
        changes.append(f"draft_fold_path: {have!r} → {want!r}")
        d["draft_fold_path"] = want
    if renamed:
        # 改名/复制来的稿必然沿用原草稿的 draft_id，两份同 id 会互相打架
        old_id = d.get("draft_id")
        d["draft_id"] = str(uuid.uuid4()).upper()
        changes.append(f"draft_id: {old_id} → {d['draft_id']}")

    if not changes:
        return []
    out = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    p.write_bytes(encrypt_bytes(out) if enc else out)
    return changes


# --------------------------------------------------------------------------
# meta 快照 / 修复
#
# 剪映 11.7 运行中有个后台服务，会对「刚出现或刚被改过」的草稿做一次索引，
# 有时会把 draft_meta_info.json 写成「空草稿」：
#     draft_materials: []      tm_duration: 0      draft_timeline_materials_size_: 0
# 文件从 ~5.9KB 缩到 ~1.8KB，并且不再加密。正文完好，但这种 meta 会让草稿
# 彻底打不开（双击无反应/报错）。
#
# 2026-11-22 复现：同一份内容连续建三份，25 秒后一份被打中、两份没打中 —— 纯随机。
# 这就是这一整天「时而能开、时而打不开」的真正原因：不是 JSON 内容不合法。
# 对策：写完立刻存一份健康 meta 快照；发现被抹平就用快照修回来。
# --------------------------------------------------------------------------
META_SNAPSHOT_DIR = Path(__file__).resolve().parent / ".meta_snapshots"


def _meta_file(project: Path) -> Path:
    return project / "draft_meta_info.json"


def read_meta(project: Path) -> dict | None:
    p = _meta_file(project)
    if not p.is_file():
        return None
    raw = p.read_bytes()
    try:
        return json.loads(decrypt_bytes(raw) if not is_plaintext_bytes(raw) else raw)
    except Exception:
        return None


def meta_health(project: Path) -> str:
    """'ok' 正常 / 'clobbered' 被剪映抹成空草稿 / 'lost' 文件不存在或读不了。

    判据只用 `draft_timeline_materials_size_` 是否为 0 —— 这是实测下来唯一
    可靠的标志（2026-11-22 全目录 82 个草稿扫过一遍，只有真正打不开的
    那两个是 0，其余哪怕是 tm_duration=0 的老稿也都正常）。

    不要用 `tm_duration` 或 `draft_materials` 是否为空来判断：很多正常草稿
    这两个本来就是空/0（例如 `11月22日` 的 tm_duration 就是 0）。
    """
    m = read_meta(project)
    if m is None:
        return "lost"
    if not m.get("draft_timeline_materials_size_"):
        return "clobbered"
    return "ok"


def _snapshot_path(project: Path) -> Path:
    m = read_meta(project) or {}
    key = str(m.get("draft_id") or project.name)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return META_SNAPSHOT_DIR / f"{safe}.json"


def snapshot_meta(project: Path) -> Path | None:
    """健康就把 meta 存一份到工具目录（不往草稿目录里加文件）。"""
    if meta_health(project) != "ok":
        return None
    META_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    dst = _snapshot_path(project)
    dst.write_bytes(_meta_file(project).read_bytes())
    return dst


def repair_meta(project: Path) -> str:
    """用快照把被抹平的 meta 修回来。"""
    health = meta_health(project)
    if health == "ok":
        return "meta 正常，无需修复"
    snap = _snapshot_path(project)
    if not snap.is_file():
        return f"meta 已损坏（{health}），但找不到快照 {snap.name}，无法自动修复"
    raw = snap.read_bytes()
    m = json.loads(decrypt_bytes(raw) if not is_plaintext_bytes(raw) else raw)
    m["draft_name"] = project.name
    m["draft_fold_path"] = str(project.resolve()).replace("\\", "/")
    # 快照是写盘前的，那时正文还是旧内容；这里用当前正文的真实总长覆盖，
    # 免得恢复出来的 meta 与正文对不上。
    c = project / "draft_content.json"
    if c.is_file():
        try:
            craw = c.read_bytes()
            cj = json.loads(decrypt_bytes(craw) if not is_plaintext_bytes(craw) else craw)
            if cj.get("duration"):
                m["tm_duration"] = int(cj["duration"])
        except Exception:
            pass
    m["tm_draft_modified"] = time.time_ns() // 1000
    out = json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if draft_is_encrypted(project):
        out = encrypt_bytes(out)
    _meta_file(project).write_bytes(out)
    return f"meta 已损坏（{health}）→ 已用快照修复"


def clone_draft(src: Path, dst: Path, new_name: str, new_id: str) -> None:
    """把草稿复制到新目录，改写元信息里的名字与 ID。

    源稿是加密的，克隆完成后会重新加密，保持与原稿一致的形态
    （否则会在草稿目录里留下一份明文稿）。
    """
    was_encrypted = draft_is_encrypted(src)
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns(".backup", ".capcut-cli-history", "*.bak",
                                    "*.bak-*", "*.encbak", "*.log")
    shutil.copytree(src, dst, ignore=ignore)
    decrypt_in_place(dst)
    retarget_meta(dst, new_name, new_id)
    if was_encrypted:
        encrypt_in_place(dst)


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="剪映草稿加解密 / meta 体检与修复")
    ap.add_argument("action",
                    choices=["info", "decrypt", "encrypt", "ratio", "meta", "snapshot",
                             "repair", "clean", "manifest", "sync"])
    ap.add_argument("path", help="草稿目录")
    ap.add_argument("--keep", action="store_true", help="原地操作时不保留 .encbak 备份")
    args = ap.parse_args(argv)

    project = Path(args.path).expanduser().resolve()
    if not project.is_dir():
        sys.exit(f"[错误] 目录不存在: {project}")
    if not crypto_available():
        sys.exit("[错误] 缺少 pycryptodome，请先 pip install pycryptodome")

    if args.action == "info":
        enc = draft_is_encrypted(project)
        print(f"草稿: {project}")
        print(f"状态: {'加密' if enc else '明文'}")
        for n in ENCRYPTED_FILES:
            p = project / n
            if p.is_file():
                print(f"  {n:<24} {p.stat().st_size:>10} 字节  {'密文' if is_encrypted(p) else '明文'}")
        return 0

    if args.action == "ratio":
        p = project / "draft_content.json"
        raw = p.read_bytes()
        if is_plaintext_bytes(raw):
            print("已是明文，无需解密")
            return 0
        plain = decrypt_bytes(raw)
        print(f"密文 {len(raw)} 字节 -> 明文 {len(plain)} 字节")
        print("往返一致:", encrypt_bytes(plain, seed=1) is not None and decrypt_bytes(
            encrypt_bytes(plain, seed=1)) == plain)
        return 0

    if args.action == "meta":
        m = read_meta(project) or {}
        health = meta_health(project)
        label = {"ok": "正常", "clobbered": "被抹成空草稿（打不开）", "lost": "缺失"}[health]
        dm = sum(len(g.get("value") or []) for g in (m.get("draft_materials") or [])
                 if isinstance(g, dict))
        print(f"草稿: {project.name}")
        print(f"meta: {label}")
        print(f"  tm_duration={m.get('tm_duration')}  素材记录={dm} 条  "
              f"timeline_materials_size={m.get('draft_timeline_materials_size_')}")
        return 0 if health == "ok" else 1

    if args.action == "snapshot":
        p = snapshot_meta(project)
        print(f"已存快照: {p}" if p else "meta 不健康，未存快照（先修复再存）")
        return 0 if p else 1

    if args.action == "repair":
        print(repair_meta(project))
        print("提示：修复后请确认剪映已完全退出，再打开草稿。")
        return 0

    if args.action == "clean":
        gone = clean_journal(project)
        pruned = prune_backup_manifest(project)
        print(f"已删除 {len(gone)} 个残留 journal（draft_content.json.log）"
              if gone else "没有残留 journal")
        print(f"已剔除 {pruned} 条失效备份清单记录"
              if pruned else "备份清单无失效条目")
        return 0

    if args.action == "manifest":
        pruned = prune_backup_manifest(project)
        print(f"已剔除 {pruned} 条失效备份清单记录"
              if pruned else "备份清单无失效条目")
        return 0

    if args.action == "sync":
        n = sync_copies(project)
        print(f"已对齐 {n} 处旧副本/快照，已清理残留 journal")
        print("提示：剪映必须完全退出（含托盘）再打开草稿。")
        return 0

    if args.action in ("decrypt", "encrypt"):
        if not args.keep:
            for n in ENCRYPTED_FILES:
                p = project / n
                if p.is_file():
                    shutil.copy2(p, p.with_suffix(p.suffix + ".encbak"))
        done = decrypt_in_place(project) if args.action == "decrypt" else encrypt_in_place(project)
        print(f"{args.action} 完成: {', '.join(done) or '无变化'}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
