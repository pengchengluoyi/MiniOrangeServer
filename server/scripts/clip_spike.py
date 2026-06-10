#!/usr/bin/env python3
# -*-coding:utf-8 -*-
"""CLIP 中英文混合定位验证：python server/scripts/clip_spike.py --image path.png --query '底部想要'"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2

from server.core.vision.clip_service import build_mixed_text_prompts, get_clip_service
from server.services.clip_locate_service import locate_on_screenshot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="截图路径")
    ap.add_argument("--query", required=True, help="中英文 query，如「想要」「bottom want tab」")
    ap.add_argument("--region", default="bottom", choices=["bottom", "login_row", "full"])
    ap.add_argument("--w", type=int, default=0)
    ap.add_argument("--h", type=int, default=0)
    args = ap.parse_args()

    img = cv2.imread(args.image)
    if img is None:
        print(f"无法读取图片: {args.image}")
        sys.exit(1)
    h, w = img.shape[:2]
    if not args.w:
        args.w = w
    if not args.h:
        args.h = h

    svc = get_clip_service()
    if not svc.available():
        print(f"CLIP 不可用: {svc.last_error()}")
        sys.exit(2)

    print(f"model: {svc.model_tag}")
    print("prompts:", build_mixed_text_prompts(args.query))

    hit = locate_on_screenshot(
        img,
        args.query,
        screen_w=args.w,
        screen_h=args.h,
        region=args.region,
    )
    if not hit:
        print("未命中")
        sys.exit(3)
    print("hit:", hit)


if __name__ == "__main__":
    main()
