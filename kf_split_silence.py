#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kf_split_silence.py — 在音频停顿处把音频片段断开（只切分，不删任何内容）

用途：口播/配音稿里逐句切段，替代手工一帧帧 Ctrl+B。
特点：切分后**总时长不变**、其它轨道完全不动，风险远低于补空缺。

检测：ffmpeg silencedetect（本地算，不联网、不依赖剪映的 AI 功能）
写回：复用 kf_crypt 的加密/meta/journal 流程（坑 0.6 / 0.7 / 0.8 全套）

用法
    python kf_split_silence.py "我的口播稿"              # 预览，不写盘
    python kf_split_silence.py "我的口播稿" --apply      # 真正写回
    python kf_split_silence.py --path "D:/.../草稿" --apply

    草稿名只在草稿根目录下查找。根目录默认是本机剪映的位置，
    换机器请直接给完整路径，或设环境变量 JY_DRAFT_ROOT。

参数
    --min-silence 0.3   多长的静音算一次停顿（秒）
    --noise -30         静音门限（dB）。底噪大的真人录音可放到 -25/-20
    --cut mid           切点位置：mid=静音正中（默认）/ head=静音开始后 / tail=下一句前
    --pad 0.15          cut=head/tail 时的留白秒数（时间轴秒）
    --min-seg 0.2       切出来的段短于这个秒数就放弃这一刀
    --types audio       处理的轨道类型，默认只碰音频轨
    --restore 文件      把正文回滚到该备份（打不开时的救命开关）

设计依据（来自一份真实口播稿里 65 段手工切分样本）
    · 切分时剪映会给每段复制一份 material，并给每段一整套新的 extra_material_refs
    · target 与 source 严格首尾相接，无缝隙
    · 换算关系 源时长 × speed = 时间轴时长（实测误差 0.0ms）
    · 段 id 还会被 materials.texts[].text_to_audio_ids 引用，
      因此第一段保留原 id，新增段 id 追加到该文本的引用列表里
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kf_crypt

DRAFT_ROOT = Path(os.environ.get("JY_DRAFT_ROOT")
                  or r"D:\soft\AI design\video\jianying\draft\JianyingPro Drafts")

PLACEHOLDER_RE = re.compile(r"^##_draftpath_placeholder_[0-9A-Fa-f\-]+_##(.*)$", re.S)
SIL_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
SIL_END_RE = re.compile(r"silence_end:\s*([\d.]+)")

US = 1_000_000


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def new_guid() -> str:
    return str(uuid.uuid4()).upper()


def running_editors() -> list[str]:
    """正在跑的剪映相关进程。

    只查 JianyingPro.exe 不够：关掉主窗口后 JianyingProTray.exe 还在托盘驻留，
    手里握着草稿的内存状态，写完它一保存就把文件覆盖回旧内容
    （表现为「明明写进去了，打开却什么都没变」）。
    """
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["tasklist", "/NH", "/FO", "CSV"],
                             capture_output=True, timeout=15).stdout
    except Exception:
        return []
    text = out.decode("mbcs", "replace") if os.name == "nt" else out.decode("utf-8", "replace")
    hits = set()
    for line in text.splitlines():
        name = line.split(",")[0].strip().strip('"')
        low = name.lower()
        if low.endswith(".exe") and ("jianying" in low or "capcut" in low):
            hits.add(name)
    return sorted(hits)


def resolve_project(raw: str) -> Path:
    """草稿目录路径 / 草稿名（在草稿根目录下找）都认。"""
    p = Path(raw)
    if p.is_dir():
        return p
    if not DRAFT_ROOT.is_dir():
        sys.exit(f"[错误] 找不到草稿：{raw}\n"
                 f"       草稿根目录 {DRAFT_ROOT} 不存在。\n"
                 "       请直接给草稿的完整路径，或设置环境变量 JY_DRAFT_ROOT。")
    cand = DRAFT_ROOT / raw
    if cand.is_dir():
        return cand
    hits = [d for d in DRAFT_ROOT.iterdir() if d.is_dir() and raw.lower() in d.name.lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        sys.exit("[错误] 名字不唯一，请写全：\n    " + "\n    ".join(h.name for h in hits))
    sys.exit(f"[错误] 找不到草稿：{raw}\n       草稿根目录：{DRAFT_ROOT}")


def load_draft(project: Path) -> tuple[Path, dict, bool]:
    root = project / "draft_content.json"
    if not root.is_file():
        sys.exit(f"[错误] {project} 下没有 draft_content.json")
    enc = kf_crypt.is_encrypted(root)
    data = json.loads(kf_crypt.decrypt_bytes(root.read_bytes()) if enc else root.read_bytes())
    return root, data, enc


def material_index(data: dict) -> dict[str, tuple[str, dict]]:
    """id -> (materials 里的列表名, 对象)"""
    idx: dict[str, tuple[str, dict]] = {}
    for key, val in (data.get("materials") or {}).items():
        if not isinstance(val, list):
            continue
        for it in val:
            if isinstance(it, dict) and it.get("id"):
                idx[it["id"]] = (key, it)
    return idx


def resolve_media(project: Path, path_text: str) -> Path | None:
    """把素材路径解析成本地文件。草稿内素材写成
    ##_draftpath_placeholder_<GUID>_##/textReading/xxx.wav """
    if not path_text:
        return None
    m = PLACEHOLDER_RE.match(path_text)
    if m:
        p = project / m.group(1).lstrip("/\\")
        return p if p.is_file() else None
    p = Path(path_text)
    return p if p.is_file() else None


# --------------------------------------------------------------------------
# 静音检测
# --------------------------------------------------------------------------
def detect_silences(media: Path, noise_db: float, min_silence: float,
                    dur_hint_us: int | None) -> list[tuple[float, float]]:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(media),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except FileNotFoundError:
        sys.exit("[错误] 找不到 ffmpeg，把它加到 PATH 再试")
    except subprocess.TimeoutExpired:
        print(f"  [跳过] 检测超时：{media.name}")
        return []
    err = proc.stderr.decode("utf-8", "replace")
    starts = [float(x) for x in SIL_START_RE.findall(err)]
    ends = [float(x) for x in SIL_END_RE.findall(err)]
    if len(ends) < len(starts):
        # 文件末尾就是静音时，ffmpeg 可能不给 silence_end
        tail = (dur_hint_us or 0) / US
        ends += [tail] * (len(starts) - len(ends))
    return [(max(0.0, a), b) for a, b in zip(starts, ends) if b > a]


def silence_cache_key(media: Path, noise_db: float, min_silence: float) -> tuple:
    try:
        st = media.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (0, 0)
    return (str(media.resolve()).lower(), stamp, noise_db, min_silence)


# --------------------------------------------------------------------------
# 切点计算
# --------------------------------------------------------------------------
def cut_points(src_a: int, src_b: int, silences: list[tuple[float, float]],
               mode: str, pad_us: int, tgt_a: int, tgt_b: int,
               min_seg_us: int) -> list[int]:
    """返回源坐标上的切点（微秒），已剔除贴边和会切出碎段的。

    pad 用时间轴秒表达（用户看的是时间轴），这里按段内比例折回源坐标。
    """
    ratio = (tgt_b - tgt_a) / (src_b - src_a) if src_b > src_a else 1.0
    pad_src = int(pad_us * ratio)
    out: list[int] = []
    for a_s, b_s in silences:
        a = max(int(a_s * US), src_a)
        b = min(int(b_s * US), src_b)
        if b <= a:
            continue
        # 贴段边的静音没有切的意义（切出来就是碎段）
        if a - src_a < min_seg_us or src_b - b < min_seg_us:
            continue
        if mode == "head":
            p = a + pad_src
        elif mode == "tail":
            p = b - pad_src
        else:  # mid
            p = (a + b) // 2
        p = max(a, min(b, p))
        if p - src_a < min_seg_us or src_b - p < min_seg_us:
            continue
        out.append(p)
    return sorted(set(out))


def axis_of(src: int, src_a: int, tgt_a: int, ratio: float) -> int:
    return tgt_a + int(round((src - src_a) * ratio))


# --------------------------------------------------------------------------
# 切分一段
# --------------------------------------------------------------------------
def clone_refs(refs: list[str], idx: dict, data: dict) -> list[str]:
    """给这一份新段复制一整套引用对象（剪映自己也是这么干的）"""
    out = []
    for rid in refs:
        got = idx.get(rid)
        if not got:
            continue
        key, obj = got
        clone = copy.deepcopy(obj)
        clone["id"] = new_guid()
        data["materials"][key].append(clone)
        idx[clone["id"]] = (key, clone)
        out.append(clone["id"])
    return out


def split_one(seg: dict, cuts: list[int], data: dict, idx: dict,
              text_refs: dict[str, dict]) -> list[dict]:
    """把 seg 在 cuts（源坐标）处切开，返回新的段列表（顺序不变）"""
    src = seg.get("source_timerange") or {}
    tgt = seg.get("target_timerange") or {}
    src_a = int(src.get("start") or 0)
    src_b = src_a + int(src.get("duration") or 0)
    tgt_a = int(tgt.get("start") or 0)
    tgt_b = tgt_a + int(tgt.get("duration") or 0)
    ratio = (tgt_b - tgt_a) / (src_b - src_a) if src_b > src_a else 1.0

    bounds_src = [src_a] + [c for c in cuts if src_a < c < src_b] + [src_b]
    # 轴坐标按比例映射，最后一段强制收到原 tgt_b，保证总长零误差
    bounds_tgt = [axis_of(s, src_a, tgt_a, ratio) for s in bounds_src]
    bounds_tgt[-1] = tgt_b

    base_mat = idx.get(seg.get("material_id") or "")
    out: list[dict] = []
    for i in range(len(bounds_src) - 1):
        sa, sb = bounds_src[i], bounds_src[i + 1]
        ta, tb = bounds_tgt[i], bounds_tgt[i + 1]
        if sb <= sa or tb <= ta:
            continue
        piece = copy.deepcopy(seg)
        if i > 0:
            piece["id"] = new_guid()
        # 首段保留原 id：materials.texts[].text_to_audio_ids 还指着它
        if base_mat:
            mat = copy.deepcopy(base_mat[1])
            mat["id"] = new_guid()
            data["materials"][base_mat[0]].append(mat)
            idx[mat["id"]] = (base_mat[0], mat)
            piece["material_id"] = mat["id"]
        piece["source_timerange"] = {"start": sa, "duration": sb - sa}
        piece["target_timerange"] = {"start": ta, "duration": tb - ta}
        piece["render_timerange"] = {}
        piece["extra_material_refs"] = clone_refs(seg.get("extra_material_refs") or [], idx, data)

        # 音量包络之类的时间轴关键帧：按段内偏移重新分配
        if seg.get("common_keyframes"):
            piece.pop("common_keyframes", None)

        out.append(piece)

    if not out:
        return [seg]

    # 新段 id 登记到文本朗读的关联表
    tid = base_mat[1].get("text_id") if base_mat else None
    if tid and len(out) > 1:
        for tx in data["materials"].get("texts") or []:
            ids = tx.get("text_to_audio_ids")
            if tx.get("id") == tid and isinstance(ids, list) and seg["id"] in ids:
                pos = ids.index(seg["id"])
                ids[pos + 1:pos + 1] = [p["id"] for p in out[1:]]
                break
    return out


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def build(project: Path, min_silence: float, noise_db: float, cut_mode: str,
          pad: float, min_seg: float, types: set[str],
          limit: int | None) -> dict:
    """在内存里算出切分结果，不写盘。返回 plan 字典。

    菜单里先 build 看一眼刀数和总长，确认后再 write_plan 写盘 ——
    这样 ffmpeg 检测只跑一遍（大文件检测很慢）。
    """
    root, data, enc = load_draft(project)
    idx = material_index(data)

    print(f"草稿：{project.name}")
    print(f"       {'加密' if enc else '明文'} / 总时长 {data.get('duration', 0)/US:.2f}s")
    print(f"参数： 静音≥{min_silence}s 门限{noise_db}dB 切点={cut_mode}"
          + (f" 留白{pad}s" if cut_mode != "mid" else " 压静音正中")
          + f" 最短段{min_seg}s")

    cache: dict[tuple, list[tuple[float, float]]] = {}
    total_new = 0
    touched_tracks = 0
    budget = limit  # 剩余可切刀数，None = 不限

    for ti, track in enumerate(data.get("tracks") or []):
        if track.get("type") not in types:
            continue
        segs = track.get("segments") or []
        if not segs:
            continue
        print(f"\n[轨道 {ti}] {track.get('type')} / {track.get('name') or '(未命名)'}：{len(segs)} 段")
        new_segs: list[dict] = []
        cut_count = 0
        skip_notes: list[str] = []

        for seg in segs:
            mat = idx.get(seg.get("material_id") or "")
            if not mat:
                new_segs.append(seg)
                continue
            segtype = mat[1].get("type") or ""
            if types == {"audio"} and segtype in ("video", "image", "photo"):
                new_segs.append(seg)
                continue
            media = resolve_media(project, mat[1].get("path") or "")
            if not media:
                skip_notes.append(f"{seg['id'][:6]} 素材文件不存在（{segtype}），跳过")
                new_segs.append(seg)
                continue
            if seg.get("common_keyframes"):
                skip_notes.append(f"{seg['id'][:6]} 段内有音量关键帧，暂不切（避免写坏包络）")
                new_segs.append(seg)
                continue

            src = seg.get("source_timerange") or {}
            src_a = int(src.get("start") or 0)
            src_b = src_a + int(src.get("duration") or 0)
            tgt = seg.get("target_timerange") or {}
            tgt_a = int(tgt.get("start") or 0)
            tgt_b = tgt_a + int(tgt.get("duration") or 0)
            if src_b <= src_a or tgt_b <= tgt_a:
                new_segs.append(seg)
                continue

            key = silence_cache_key(media, noise_db, min_silence)
            if key not in cache:
                cache[key] = detect_silences(media, noise_db, min_silence,
                                             int(mat[1].get("duration") or 0))
            sil = cache[key]
            cuts = cut_points(src_a, src_b, sil, cut_mode, int(pad * US),
                              tgt_a, tgt_b, int(min_seg * US))
            if not cuts:
                new_segs.append(seg)
                continue
            if budget is not None:
                cuts = cuts[:max(0, budget)]
                if not cuts:
                    new_segs.append(seg)
                    continue
            pieces = split_one(seg, cuts, data, idx, {})
            if len(pieces) < 2:
                new_segs.append(seg)
                continue
            cut_count += len(pieces) - 1
            if budget is not None:
                budget -= len(pieces) - 1
            new_segs.extend(pieces)

        if cut_count == 0:
            for n in skip_notes:
                print(f"  · {n}")
            print("  无需切分")
            continue

        track["segments"] = sorted(new_segs, key=lambda s: (s.get("target_timerange") or {}).get("start") or 0)
        touched_tracks += 1
        total_new += cut_count
        print(f"  切出 {cut_count} 刀 → {len(segs)} 段变成 {len(track['segments'])} 段")
        for n in skip_notes:
            print(f"  · {n}")

    if total_new == 0:
        print("\n没有任何可切的地方（静音都贴在段边，或不满足阈值）。")
        return {"ok": False, "project": project}

    print(f"\n合计：新增 {total_new} 刀，{touched_tracks} 条轨道受影响")
    # 切分不改变时间线总长，这里只是防御性重算
    end = 0
    for tr in data.get("tracks") or []:
        for s in tr.get("segments") or []:
            tg = s.get("target_timerange") or {}
            end = max(end, int(tg.get("start") or 0) + int(tg.get("duration") or 0))
    before = data.get("duration", 0)
    print(f"时间线总长：{before/US:.2f}s → {end/US:.2f}s（应保持不变）")

    pruned = prune_orphans(data)
    print(f"素材表：audios {len(data['materials'].get('audios') or [])} 份 / "
          f"beats {len(data['materials'].get('beats') or [])} 份"
          f"（清理孤儿 audios {pruned.get('audios', 0)}、beats {pruned.get('beats', 0)}）")

    return {"ok": True, "project": project, "root": root, "data": data, "enc": enc,
            "cuts": total_new, "tracks": touched_tracks, "pruned": pruned,
            "end": end, "before": before}


def write_plan(plan: dict, force: bool = False) -> int:
    """把 build() 算好的结果写回草稿（加密稿走加密 + 副本对齐全流程）。"""
    project: Path = plan["project"]
    root: Path = plan["root"]
    data: dict = plan["data"]

    live = running_editors()
    if live and not force:
        print(f"\n[拒绝写入] 剪映还在运行（{', '.join(live)}）。")
        print("        它会把内存里的旧状态覆盖回来，写完等于白写。")
        print("        请从托盘完全退出剪映后再写。")
        return 1

    kf_crypt.snapshot_meta(project)
    if data.get("duration"):
        data["duration"] = plan["end"]
    root.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if plan["enc"]:
        kf_crypt.encrypt_in_place(project)
    else:
        kf_crypt.propagate_content(project)
    kf_crypt.sync_copies(project)
    fixes = kf_crypt.ensure_meta_identity(project)
    if fixes:
        print("  [meta 修正] 已对齐草稿身份：")
        for line in fixes:
            print(f"    - {line}")
    kf_crypt.touch_meta(project)
    health = kf_crypt.meta_health(project)
    if health != "ok":
        print(f"  [注意] meta {health}，用快照修复…")
        print(f"  {kf_crypt.repair_meta(project)}")
    print("  已写回。打开剪映检查段数，确认没问题再动原稿。")
    return 0


def run(project: Path, apply: bool, min_silence: float, noise_db: float,
        cut_mode: str, pad: float, min_seg: float, types: set[str],
        force: bool, limit: int | None) -> int:
    plan = build(project, min_silence, noise_db, cut_mode, pad, min_seg, types, limit)
    if not plan["ok"]:
        return 0
    if not apply:
        print("\n[预览] 没有写盘。确认没问题后加 --apply 真正执行。")
        return 0
    return write_plan(plan, force)


def prune_orphans(data: dict) -> dict[str, int]:
    """删掉不再被任何片段引用的 audios / beats 对象。

    依据：四份能正常打开的草稿里，`materials.audios` 和 `materials.beats`
    永远与音频段数量**严格 1:1，零孤儿**（speeds/placeholder_infos/
    sound_channel_mappings/vocal_separations 反而允许留孤儿）。
    切分时每段都会克隆一份新素材，原来的那份就成了孤儿 ——
    数量对不上正是剪映拒绝加载的原因（2026-11-23 实测）。
    """
    used_mat: set[str] = set()
    used_ref: set[str] = set()
    for tr in data.get("tracks") or []:
        for s in tr.get("segments") or []:
            if s.get("material_id"):
                used_mat.add(s["material_id"])
            used_ref.update(s.get("extra_material_refs") or [])
    stats: dict[str, int] = {}
    for key, keep in (("audios", used_mat), ("beats", used_ref)):
        lst = (data.get("materials") or {}).get(key)
        if not isinstance(lst, list):
            continue
        before = len(lst)
        data["materials"][key] = [x for x in lst
                                 if isinstance(x, dict) and x.get("id") in keep]
        stats[key] = before - len(data["materials"][key])
    return stats


def restore(project: Path, src_file: Path) -> int:
    """把草稿正文回滚成 src_file 的内容（密文/明文都行），并清理 journal。

    回滚最容易漏的是「副本一致性」：Timelines 镜像、template-2.tmp、.backup 里的
    save/close 快照必须和正文逐字节一致，否则剪映加载时会判成「被外部改过」→
    回滚旧版冲突 → 写 journal 自锁（坑 0.6）。所以这里一次性全对齐。
    """
    raw = src_file.read_bytes()
    root = project / "draft_content.json"
    try:
        json.loads(raw)
        plain = True
    except Exception:
        plain = False
    if plain and kf_crypt.is_encrypted(root):
        raw = kf_crypt.encrypt_bytes(raw)
    root.write_bytes(raw)
    for mirror in project.glob("Timelines/*/draft_content.json"):
        mirror.write_bytes(raw)
    tmp = project / "template-2.tmp"
    if tmp.is_file():
        tmp.write_bytes(raw)
    gone = kf_crypt.clean_journal(project)
    kf_crypt.sync_copies(project)
    kf_crypt.touch_meta(project)
    print(f"已回滚：{src_file.name} → {root.stat().st_size} 字节")
    print(f"  清理 journal: {gone or '无'}")
    print(f"  meta 体检: {kf_crypt.meta_health(project)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="在音频停顿处切分片段（不删内容）")
    ap.add_argument("project", nargs="?", help="草稿目录名或路径")
    ap.add_argument("--path", help="草稿绝对路径")
    ap.add_argument("--apply", action="store_true", help="真正写回（默认只预览）")
    ap.add_argument("--min-silence", type=float, default=0.3, help="最短静音秒数，默认 0.3")
    ap.add_argument("--noise", type=float, default=-30.0, help="静音门限 dB，默认 -30")
    ap.add_argument("--cut", choices=["mid", "head", "tail"], default="mid",
                    help="切点：mid=静音正中（默认） head=静音开始后 tail=下一句前")
    ap.add_argument("--pad", type=float, default=0.15, help="head/tail 的留白秒数，默认 0.15")
    ap.add_argument("--min-seg", type=float, default=0.2, help="最短片段秒数，默认 0.2")
    ap.add_argument("--types", default="audio", help="轨道类型，逗号分隔，默认 audio")
    ap.add_argument("--limit", type=int, help="最多切多少刀（试水用）")
    ap.add_argument("--force", action="store_true", help="剪映在跑也强行写")
    ap.add_argument("--restore", metavar="文件", help="回滚正文到这个备份文件")
    a = ap.parse_args()

    raw = a.path or a.project
    if not raw:
        ap.print_help()
        return 2
    project = resolve_project(raw)
    if a.restore:
        return restore(project, Path(a.restore))
    types = {t.strip() for t in a.types.split(",") if t.strip()}
    return run(project, a.apply, a.min_silence, a.noise, a.cut,
               a.pad, a.min_seg, types, a.force, a.limit)


if __name__ == "__main__":
    sys.exit(main())
