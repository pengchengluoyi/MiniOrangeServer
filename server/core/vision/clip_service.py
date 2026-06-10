# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""OpenCLIP 视觉编码：中英文混合 query 通过 prompt 集成 + 图像 patch 相似度。"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np
from PIL import Image

from script.log import SLog

TAG = "ClipService"
_LOAD_LOCK = threading.Lock()

# 常见 UI 词的中英提示（增强跨语言匹配）
_BILINGUAL_HINTS: dict[str, List[str]] = {
    "想要": ["want", "wishlist", "favorite tab", "heart tab"],
    "首页": ["home", "home tab", "feed"],
    "消息": ["messages", "inbox", "notification"],
    "我的": ["me", "profile", "mine", "account"],
    "发现": ["discover", "explore"],
    "微信": ["wechat", "weixin"],
    "手机": ["phone", "mobile", "sms"],
    "登录": ["login", "sign in"],
    "同意": ["agree", "accept", "consent"],
    "访客": ["guest", "visitor"],
    "苹果": ["apple", "apple id"],
    "密码": ["password", "account"],
}


def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def clip_enabled() -> bool:
    return _env_flag("CLIP_ENABLED", "1")


def _pick_device():
    import torch

    pref = (os.getenv("CLIP_DEVICE") or "").strip().lower()
    if pref in ("cpu", "cuda", "mps"):
        return pref
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def build_mixed_text_prompts(query: str, aliases: Optional[Sequence[str]] = None) -> List[str]:
    """为中英文混合指令生成多条 CLIP 文本 prompt，取 embedding 均值。"""
    q = (query or "").strip()
    if not q:
        return []
    seen: set[str] = set()
    out: List[str] = []

    def add(p: str) -> None:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    add(q)
    add(f"mobile app UI element: {q}")
    add(f"app icon button: {q}")
    add(f"bottom navigation tab: {q}")
    for a in aliases or []:
        a = (a or "").strip()
        if a:
            add(a)
            add(f"{a} icon")
            add(f"mobile app {a}")
    for zh, en_list in _BILINGUAL_HINTS.items():
        if zh in q:
            for en in en_list:
                add(en)
                add(f"mobile app {en}")
    return out[:16]


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-8:
        return v
    return v / n


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    aa = np.asarray(a, dtype=np.float32).reshape(-1)
    bb = np.asarray(b, dtype=np.float32).reshape(-1)
    if aa.size == 0 or bb.size == 0:
        return 0.0
    return float(np.dot(_l2_normalize(aa), _l2_normalize(bb)))


class ClipService:
    _instance: Optional["ClipService"] = None

    def __init__(self) -> None:
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._device: Optional[str] = None
        self._model_tag = ""
        self._load_error: Optional[str] = None

    @classmethod
    def get(cls) -> "ClipService":
        if cls._instance is None:
            cls._instance = ClipService()
        return cls._instance

    @property
    def model_tag(self) -> str:
        return self._model_tag

    def available(self) -> bool:
        if not clip_enabled():
            return False
        try:
            self._ensure_loaded()
            return self._model is not None
        except Exception as e:
            self._load_error = str(e)
            return False

    def last_error(self) -> str:
        return self._load_error or ""

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with _LOAD_LOCK:
            if self._model is not None:
                return
            import open_clip

            model_name = os.getenv("CLIP_MODEL", "ViT-B-32")
            pretrained = os.getenv("CLIP_PRETRAINED", "laion2b_s34b_b79k")
            device = _pick_device()
            SLog.i(TAG, f"Loading OpenCLIP {model_name}/{pretrained} on {device}...")
            model, _, preprocess = open_clip.create_model_and_transforms(
                model_name,
                pretrained=pretrained,
                device=device,
            )
            tokenizer = open_clip.get_tokenizer(model_name)
            model.eval()
            self._model = model
            self._preprocess = preprocess
            self._tokenizer = tokenizer
            self._device = device
            self._model_tag = f"{model_name}:{pretrained}"
            SLog.i(TAG, "OpenCLIP ready")

    def _encode_text_single(self, text: str) -> np.ndarray:
        import torch

        self._ensure_loaded()
        tokens = self._tokenizer([text])
        if self._device:
            tokens = tokens.to(self._device)
        with torch.no_grad():
            feats = self._model.encode_text(tokens)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats[0].detach().cpu().numpy().astype(np.float32)

    def encode_text_mixed(
        self,
        query: str,
        aliases: Optional[Sequence[str]] = None,
    ) -> Optional[np.ndarray]:
        prompts = build_mixed_text_prompts(query, aliases)
        if not prompts:
            return None
        try:
            vecs = [self._encode_text_single(p) for p in prompts]
            mean = np.mean(np.stack(vecs, axis=0), axis=0)
            return _l2_normalize(mean).astype(np.float32)
        except Exception as e:
            SLog.w(TAG, f"encode_text_mixed failed: {e}")
            self._load_error = str(e)
            return None

    def encode_image(
        self,
        image: Union[np.ndarray, Image.Image],
    ) -> Optional[np.ndarray]:
        import torch

        try:
            self._ensure_loaded()
            if isinstance(image, np.ndarray):
                if image.ndim == 2:
                    image = np.stack([image] * 3, axis=-1)
                if image.shape[2] == 3:
                    image = image[:, :, ::-1]  # BGR -> RGB
                pil = Image.fromarray(image.astype(np.uint8))
            else:
                pil = image.convert("RGB")
            tensor = self._preprocess(pil).unsqueeze(0)
            if self._device:
                tensor = tensor.to(self._device)
            with torch.no_grad():
                feats = self._model.encode_image(tensor)
                feats = feats / feats.norm(dim=-1, keepdim=True)
            return feats[0].detach().cpu().numpy().astype(np.float32)
        except Exception as e:
            SLog.w(TAG, f"encode_image failed: {e}")
            return None

    def embedding_dim(self) -> int:
        try:
            self._ensure_loaded()
            import torch

            dummy = self._tokenizer(["test"])
            if self._device:
                dummy = dummy.to(self._device)
            with torch.no_grad():
                v = self._model.encode_text(dummy)
            return int(v.shape[-1])
        except Exception:
            return 512


def get_clip_service() -> ClipService:
    return ClipService.get()


def warmup_clip_service() -> Dict[str, Any]:
    """服务启动时预加载模型，返回状态摘要。"""
    if not clip_enabled():
        return {"ok": False, "reason": "CLIP_ENABLED=0"}
    svc = get_clip_service()
    if not svc.available():
        return {"ok": False, "reason": svc.last_error() or "load failed"}
    dim = svc.embedding_dim()
    return {"ok": True, "model": svc.model_tag, "dim": dim}
