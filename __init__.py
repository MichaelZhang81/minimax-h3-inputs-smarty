"""MiniMax H3 素材可选输入 —— 目录插件（minimax-h3-inputs-smarty）。

解决的问题：官方 MiniMax H3 Reference to Video 的 ref_image_0..8 / ref_video_0..2 /
ref_audio_0..2 一旦连线就必须给定输入。本插件提供「打包-解析」两个节点：

  1) MiniMaxH3素材参数可选输入（MiniMaxH3_InputBundle）
     提示词 + 目标时长(float秒) + 图片1..9/视频1..3/音频1..3（全部可选）。
     每个槽位支持：手输文件名或网址URL（自动下载到 ComfyUI/input）、
     输入目录文件下拉列表、上传按钮（系统文件选择器）。打包为 inputs_bundle。

  2) MiniMaxH3素材输入包解析（MiniMaxH3_BundleParser）
     接收 inputs_bundle，输出 image_0..8 / video_0..2 / audio_0..2 / prompt /
     length。空槽位输出 None —— 官方 MiniMaxH3ReferenceToVideo 对 None 引用
     直接跳过（comfy_extras/nodes_minimax_h3.py），因此 15 条线全部预连好后，
     不填素材也能正常运行。length 公式：
         length = max(5, round(a*24)) + (5 - (max(5, round(a*24)) % 17)) % 17
     （a = 目标时长秒；结果落在模型 17k+5 网格上，5 秒 -> 124 帧）。

目录结构：
  __init__.py                        后端（两个声明式节点 + 文件列表路由 + URL 下载）
  web/minimax_h3_inputs_smarty.js    前端（每个槽位：下拉列表+自由输入+上传+刷新）

说明：
  - 前端每槽位控件（下拉+输入+上传+预览）在节点详情面板渲染；真实 STRING 控件
    隐藏但继续持有/序列化槽位值（H3MediaBoard 同款模式）；节点窗口拖高时多余
    高度只与提示词输入框联动（吸收）。
  - 网络下载：读取环境变量 http_proxy/https_proxy，设置了就走代理，未设置则
    直连；带 3 次重试 + Range 断点续传；同 URL 下载结果按 md5 前缀缓存复用。
  - 媒体解码全部走 ComfyUI 官方加载路径（InputImpl/av/PIL），零新增依赖，
    与 H3MediaBoard media_loader 同款实现（本机 torchaudio.load 不可用）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from urllib.parse import unquote, urlparse

import av
import numpy as np
import torch
import urllib.request
from PIL import Image as PILImage
from PIL import ImageOps

import folder_paths
from comfy_api.latest import InputImpl
from comfy_api.latest import io

WEB_DIRECTORY = "./web"

# ============================== 1、文件列表 HTTP 路由 ==============================


def _register_routes() -> bool:
    """GET /minimax-h3-inputs-smarty/files?kind=image|video|audio 返回 input 目录
    当前文件列表（前端 ⟳ 刷新按钮使用；与 H3MediaBoard 路由注册同款做法）。"""
    try:
        from aiohttp import web
        from server import PromptServer
    except Exception:
        return False
    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        return False

    async def _files_handler(request):
        kind = request.query.get("kind", "image")
        return web.json_response(_list_input_files(kind))

    async def _view_handler(request):
        """预览文件流：value 为 annotated 相对路径或绝对路径（input 目录/本地文件）。"""
        value = (request.query.get("value") or "").strip()
        if not value:
            raise web.HTTPBadRequest(text="missing value")
        try:
            path = _resolve_media_path(_ANNOT_SUFFIX_RE.sub("", value).strip())
        except Exception:
            raise web.HTTPBadRequest(text="bad value")
        if not os.path.isfile(path):
            raise web.HTTPNotFound(text="file not found")
        return web.FileResponse(path)

    try:
        instance.routes.get("/minimax-h3-inputs-smarty/files")(_files_handler)
        instance.routes.get("/minimax-h3-inputs-smarty/view")(_view_handler)
        return True
    except Exception as exc:
        logging.warning("[MiniMaxH3Inputs] 文件列表路由注册失败: %s", exc)
        return False


_register_routes()

# ============================== 2、常量与辅助 ==============================

BUNDLE_IO = io.Custom("MMX_H3_INPUTS_BUNDLE")

IMG_NAMES = [f"img_{i}" for i in range(1, 10)]      # img_1..img_9
VIDEO_NAMES = [f"video_{i}" for i in range(1, 4)]   # video_1..video_3
AUDIO_NAMES = [f"audio_{i}" for i in range(1, 4)]   # audio_1..audio_3
SLOT_NAMES = IMG_NAMES + VIDEO_NAMES + AUDIO_NAMES
SLOT_KIND = {**{n: "image" for n in IMG_NAMES},
             **{n: "video" for n in VIDEO_NAMES},
             **{n: "audio" for n in AUDIO_NAMES}}
_KIND_CN = {"image": "图片", "video": "视频", "audio": "音频"}
# 同类各自从 1 编号：图片1..9、视频1..3、音频1..3
_SLOT_COUNTER: dict[str, int] = {}
SLOT_LABEL: dict[str, str] = {}
for _n in SLOT_NAMES:
    _kind = SLOT_KIND[_n]
    _SLOT_COUNTER[_kind] = _SLOT_COUNTER.get(_kind, 0) + 1
    SLOT_LABEL[_n] = f"{_KIND_CN[_kind]}{_SLOT_COUNTER[_kind]}"

_KIND_EXTS = {
    "image": {"png", "jpg", "jpeg", "webp", "bmp", "gif"},
    "video": {"mp4", "mov", "webm", "mkv", "avi"},
    "audio": {"mp3", "wav", "flac", "m4a", "ogg", "aac"},
}
_KIND_FALLBACK_EXT = {"image": ".png", "video": ".mp4", "audio": ".wav"}
_CTYPE_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/bmp": ".bmp",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/x-msvideo": ".avi",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/flac": ".flac", "audio/mp4": ".m4a", "audio/ogg": ".ogg",
    "audio/aac": ".aac",
}
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_ANNOT_SUFFIX_RE = re.compile(r"\s*\[(input|output|temp)\]$", re.IGNORECASE)

# 网络代理：读取环境变量 http_proxy / https_proxy；设置了就走代理，未设置则直连。
def _proxy_opener():
    proxies = {}
    for key in ("http", "https"):
        env_val = (os.environ.get(key + "_proxy") or os.environ.get(key.upper() + "_PROXY")
                   or os.environ.get(key + "_PROXY") or "")
        if env_val:
            proxies[key] = env_val
    if not proxies:
        # 未设置代理环境变量 -> 直连。去掉默认 ProxyHandler，
        # 避免它回退读取系统注册表/系统代理设置。
        opener = urllib.request.build_opener()
        opener.handlers = [h for h in opener.handlers
                           if not isinstance(h, urllib.request.ProxyHandler)]
        return opener
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))


def _list_input_files(kind: str) -> list[str]:
    """input 目录（用户上传目录）根层文件列表（核心 LoadImage 同款方式）。"""
    try:
        root = folder_paths.get_input_directory()
        files = [f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))]
        return sorted(folder_paths.filter_files_content_types(files, [kind]))
    except Exception as exc:
        logging.warning("[MiniMaxH3Inputs] 列出 input 目录失败(%s): %s", kind, exc)
        return []


def _resolve_media_path(value: str) -> str:
    """annotated 相对路径（"name" / "subfolder/name"）解析到 input 目录；绝对路径原样返回
    （对齐 H3MediaBoard/H3PromptOpt _resolve_media_path 做法）。"""
    if os.path.isabs(value):
        return value
    return folder_paths.get_annotated_filepath(value)


def _safe_float(value, default: float = 5.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _download_url_to_input(url: str, kind: str) -> str | None:
    """URL -> 下载到 input 目录，返回文件名（input 根目录相对名）。

    - 同 URL 按 md5 前缀缓存复用，不重复下载；
    - 3 次重试 + Range 断点续传（.part 临时文件）；
    - 失败返回 None（槽位按空处理，不打断工作流）。
    """
    root = folder_paths.get_input_directory()
    tag = "minimaxh3_" + hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
    for name in sorted(os.listdir(root)):
        if (name.startswith(tag + "_") and not name.endswith(".part")
                and os.path.isfile(os.path.join(root, name))):
            return name  # 已下载过，直接复用

    base = unquote(os.path.basename(urlparse(url).path)) or "download"
    base = re.sub(r"[^\w.\-]+", "_", base)[:80]
    stem, ext = os.path.splitext(base)
    ext = ext.lower() if ext.lower() in _KIND_EXTS[kind] else ""
    final_name = f"{tag}_{stem}{ext}" if stem else f"{tag}{ext or _KIND_FALLBACK_EXT[kind]}"
    part_path = os.path.join(root, final_name + ".part")
    done = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    ctype = ""
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            if done > 0:
                req.add_header("Range", f"bytes={done}-")
            with _proxy_opener().open(req, timeout=120) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if resp.status not in (200, 206):
                    raise RuntimeError(f"HTTP {resp.status}")
                if resp.status == 200:  # 服务器不支持断点，重头来
                    done = 0
                with open(part_path, "wb" if resp.status == 200 else "ab") as fh:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
            if done <= 0:
                raise RuntimeError("下载内容为空")
            if not os.path.splitext(final_name)[1]:
                final_name += _CTYPE_EXT.get(ctype) or _KIND_FALLBACK_EXT[kind]
            final_path = os.path.join(root, final_name)
            if os.path.exists(final_path):  # 极少数并发场景下防覆盖
                os.remove(final_path)
            os.replace(part_path, final_path)
            return final_name
        except Exception as exc:
            last_err = exc
            logging.warning("[MiniMaxH3Inputs] 下载失败(第%d次) %s: %s", attempt + 1, url, exc)
            time.sleep(2.0 * (attempt + 1))
    if last_err is not None:
        logging.error("[MiniMaxH3Inputs] 下载最终失败 %s: %s", url, last_err)
    return None


# ============================== 3、媒体解码（vendored from H3MediaBoard media_loader） ==============================


def _f32_pcm(wav: torch.Tensor) -> torch.Tensor:
    """与核心 comfy_extras/nodes_audio.py 的 f32_pcm 相同：按样本格式归一化到 float32。"""
    if wav.dtype == torch.float32:
        return wav
    elif wav.dtype == torch.int16:
        return wav.float() / (2 ** 15)
    elif wav.dtype == torch.int32:
        return wav.float() / (2 ** 31)
    raise ValueError(f"Unsupported wav dtype: {wav.dtype}")


def _pil_fallback(path: str) -> torch.Tensor:
    """pyav 不支持的静态图（如动画 webp）回退 PIL，与官方 LoadImage 兜底逻辑一致。"""
    img = PILImage.open(path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    return torch.from_numpy(np.array(img).astype(np.float32) / 255.0).unsqueeze(0)


def _load_image(path: str) -> torch.Tensor:
    """图片 -> [N,H,W,C] float32 张量（0-1）。官方 LoadImage 同款加载路径。"""
    components = InputImpl.VideoFromFile(path).get_components()
    if components.images is not None and components.images.shape[0] > 0:
        return components.images
    return _pil_fallback(path)


def _load_video_frames(path: str) -> torch.Tensor | None:
    """视频 -> 帧张量 [N,H,W,C] float32（MiniMax ref_video 即按帧输入）。"""
    components = InputImpl.VideoFromFile(path).get_components()
    if components.images is not None and components.images.shape[0] > 0:
        return components.images
    return None


def _load_audio(path: str) -> dict:
    """音频文件 -> {"waveform": [1,C,T] float32, "sample_rate": int}。

    照抄核心 LoadAudio.load（comfy_extras/nodes_audio.py:333），末尾 unsqueeze(0)
    与核心 LoadAudio 输出 [1,C,T] 保持一致。
    """
    with av.open(path) as af:
        if not af.streams.audio:
            raise ValueError("No audio stream found in the file.")
        stream = af.streams.audio[0]
        sr = stream.codec_context.sample_rate
        n_channels = stream.channels
        frames = []
        for frame in af.decode(streams=stream.index):
            buf = torch.from_numpy(frame.to_ndarray())
            if buf.shape[0] != n_channels:
                buf = buf.view(-1, n_channels).t()
            frames.append(buf)
        if not frames:
            raise ValueError("No audio frames decoded.")
    wav = torch.cat(frames, dim=1)
    wav = _f32_pcm(wav)
    return {"waveform": wav.unsqueeze(0), "sample_rate": sr}


# ============================== 4、节点一：MiniMaxH3素材参数可选输入 ==============================


class MiniMaxH3_InputBundle(io.ComfyNode):
    """提示词 + 目标时长 + 15 个可选素材槽位 -> inputs_bundle 载荷。

    槽位值为字符串：可手输 URL（执行时自动下载到 input 目录）或文件名
    （相对 input 目录 / 子目录 / 绝对路径均可）。全部槽位可空。
    """

    @classmethod
    def define_schema(cls):
        inputs = [
            # 顺序即面板显示顺序（required 在前）：提示词、目标时长、图片1..9、视频1..3、音频1..3
            io.String.Input("prompt", display_name="提示词", default="", multiline=True,
                            tooltip="会随包透传给解析节点的 prompt 输出（可接到 MiniMax 的 prompt）"),
            io.Float.Input("duration", display_name="目标时长(秒)", default=5.0,
                           min=1, max=60, step=0.1,
                           tooltip="float 秒数；解析节点换算为 MiniMax 的 length 帧数（17k+5 网格）"),
        ]
        for name in SLOT_NAMES:
            kind = SLOT_KIND[name]
            inputs.append(io.String.Input(
                name,
                display_name=f"{SLOT_LABEL[name]}（文件名/网址，可空）",
                default="",
                optional=True,
                extra_dict={
                    "mmxh3_kind": kind,
                    "mmxh3_label": SLOT_LABEL[name],
                    "mmxh3_files": _list_input_files(kind),
                },
            ))
        return io.Schema(
            node_id="MiniMaxH3_InputBundle",
            display_name="MiniMaxH3素材参数可选输入",
            category="MiniMaxH3",
            inputs=inputs,
            outputs=[BUNDLE_IO.Output(display_name="inputs_bundle")],
        )

    @classmethod
    def execute(cls, prompt="", duration=5.0, **slots):
        bundle_slots = {}
        for name in SLOT_NAMES:
            kind = SLOT_KIND[name]
            value = str(slots.get(name) or "").strip()
            value = _ANNOT_SUFFIX_RE.sub("", value).strip()
            path = None
            if value:
                if _URL_RE.match(value):
                    try:
                        path = _download_url_to_input(value, kind)
                        if path:
                            logging.info("[MiniMaxH3Inputs] %s 下载完成 -> %s", value, path)
                    except Exception as exc:  # 下载异常按空槽处理
                        logging.warning("[MiniMaxH3Inputs] %s 下载异常: %s", value, exc)
                else:
                    path = value
            bundle_slots[name] = {"kind": kind, "path": path}
        bundle = {
            "v": 1,
            "prompt": str(prompt or ""),
            "duration": _safe_float(duration, 5.0),
            "slots": bundle_slots,
        }
        return io.NodeOutput(bundle)


# ============================== 5、节点二：MiniMaxH3素材输入包解析 ==============================


def _length_for_duration(a: float) -> int:
    """目标时长秒 -> MiniMax length 帧数：max(5, round(a*24)) + (5 - (max(5, round(a*24)) % 17)) % 17"""
    base = max(5, round(a * 24))
    return base + (5 - (base % 17)) % 17


def _load_slot(info, kind: str):
    """单槽解析：{kind,path} -> 张量/音频 dict；空槽与加载失败 -> None。"""
    if not isinstance(info, dict):
        return None
    path = str(info.get("path") or "").strip()
    if not path:
        return None
    try:
        resolved = _resolve_media_path(path)
        if kind == "image":
            return _load_image(resolved)
        if kind == "video":
            return _load_video_frames(resolved)
        return _load_audio(resolved)
    except Exception as exc:
        logging.warning("[MiniMaxH3Inputs] 素材加载失败(%s) %s: %s", kind, path, exc)
        return None


class MiniMaxH3_BundleParser(io.ComfyNode):
    """inputs_bundle -> image_0..8 / video_0..2 / audio_0..2 / prompt / length。

    空槽位输出 None —— 官方 MiniMaxH3ReferenceToVideo 对 None 引用直接跳过，
    因此全部输出点可预先连到 MiniMax 节点的 ref_image_* / ref_video_* /
    ref_audio_* / length，不填素材也能正常运行。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3_BundleParser",
            display_name="MiniMaxH3素材输入包解析",
            category="MiniMaxH3",
            inputs=[BUNDLE_IO.Input("inputs_bundle", display_name="inputs_bundle",
                                    optional=True)],
            outputs=(
                [io.Image.Output(display_name=f"image_{i}") for i in range(9)]
                + [io.Image.Output(display_name=f"video_{i}") for i in range(3)]
                + [io.Audio.Output(display_name=f"audio_{i}") for i in range(3)]
                + [io.String.Output(display_name="prompt"),
                   io.Int.Output(display_name="length")]
            ),
        )

    @classmethod
    def execute(cls, inputs_bundle=None):
        bundle = inputs_bundle if isinstance(inputs_bundle, dict) else {}
        slots = bundle.get("slots") if isinstance(bundle.get("slots"), dict) else {}
        duration = _safe_float(bundle.get("duration"), 5.0)
        length = _length_for_duration(duration)
        prompt = str(bundle.get("prompt") or "")
        images = [_load_slot(slots.get(n), "image") for n in IMG_NAMES]
        videos = [_load_slot(slots.get(n), "video") for n in VIDEO_NAMES]
        audios = [_load_slot(slots.get(n), "audio") for n in AUDIO_NAMES]
        return io.NodeOutput(*images, *videos, *audios, prompt, length)


# ============================== 6、注册 ==============================

NODE_CLASS_MAPPINGS = {
    "MiniMaxH3_InputBundle": MiniMaxH3_InputBundle,
    "MiniMaxH3_BundleParser": MiniMaxH3_BundleParser,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3_InputBundle": "MiniMaxH3素材参数可选输入",
    "MiniMaxH3_BundleParser": "MiniMaxH3素材输入包解析",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
