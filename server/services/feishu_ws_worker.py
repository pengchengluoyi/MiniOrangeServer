# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""独立进程跑飞书长连接，避免占用 FastAPI 的 event loop。"""
from __future__ import annotations

import json
import os
import ssl
import sys


def _install_ssl(insecure: bool) -> None:
    import lark_oapi.ws.client as ws_mod

    if insecure:
        ctx = ssl._create_unverified_context()
    else:
        try:
            import truststore

            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            import certifi

            ctx = ssl.create_default_context()
            ctx.load_verify_locations(certifi.where())
    orig = getattr(ws_mod, "_ws_connect_kwargs_orig", ws_mod._ws_connect_kwargs)
    ws_mod._ws_connect_kwargs_orig = orig

    def patched():
        kw = dict(orig() or {})
        kw["ssl"] = ctx
        return kw

    ws_mod._ws_connect_kwargs = patched


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    app_id = os.environ.get("MO_FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("MO_FEISHU_APP_SECRET", "").strip()
    insecure = os.environ.get("MO_FEISHU_WS_INSECURE", "") == "1"
    if not app_id or not app_secret:
        _emit({"type": "error", "error": "missing credentials"})
        return 2
    import lark_oapi as lark

    _install_ssl(insecure)

    def on_message(data) -> None:
        raw = json.loads(lark.JSON.marshal(data))
        _emit({"type": "event", "payload": raw})

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
    _emit({"type": "starting", "insecure": insecure, "app_id": app_id})
    try:
        client.start()
    except Exception as e:
        _emit({"type": "error", "error": str(e) or "start failed", "insecure": insecure})
        if (not insecure) and "CERTIFICATE_VERIFY_FAILED" in str(e):
            os.environ["MO_FEISHU_WS_INSECURE"] = "1"
            os.execv(sys.executable, [sys.executable, *sys.argv])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
