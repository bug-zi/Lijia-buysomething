# -*- coding: utf-8 -*-
"""临时脚本：调用中转站 gpt-image-2.5 生成项目图片素材，用完即删。"""
import base64
import json
import os
import sys
import urllib.request

BASE_URL = "https://token.aiedulab.cn/v1"
API_KEY = "sk-e557b411e06601c4c8a0238e54ff7b44be03655d6e7a07d630a95dd0e9ccd9e7"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = os.path.join(OUT_DIR, "resource")

MODELS = ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2"]

# 四皮肤色系（与 index.html data-skin 一致）：classic 已有成图直接复制，此处生成其余三色
COLOR_NOTES = {
    "sakura": "soft dusty pink gradient background (#c06c84 family), pink and white color scheme",
    "forest": "deep forest green gradient background (#3d8a68 family), green and white color scheme",
    "sunset": "warm sunset orange gradient background (#cf8a3e family), warm orange and cream color scheme",
}

_ICON_TMPL = (
    "Flat modern app icon for a personal AI shopping assistant: a minimalist shopping bag "
    "with a small AI spark on it, {color}, rounded-square app icon shape filling the frame, "
    "clean flat vector style, subtle soft shadow, no text, centered composition, crisp edges"
)
_AVATAR_TMPL = (
    "Cute friendly robot head avatar for an AI shopping assistant, flat modern illustration, "
    "{color}, rounded shapes, gentle smile, small antenna with a spark, plain very light "
    "matching background, bust centered, circular composition, no text"
)

JOBS = []
for skin in ("sakura", "forest", "sunset"):
    note = COLOR_NOTES[skin]
    JOBS.append((f"icon-{skin}.png", _ICON_TMPL.format(color=note)))
    JOBS.append((f"avatar_ai-{skin}.png", _AVATAR_TMPL.format(color=note)))


def gen_one(model: str, prompt: str, out_path: str) -> bool:
    payload = {"model": model, "prompt": prompt, "size": "1024x1024", "n": 1}
    req = urllib.request.Request(
        BASE_URL + "/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.load(r)
    except Exception as e:
        print("  [FAIL] %s: %s: %s" % (model, type(e).__name__, e))
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
        if body:
            print("  [body]", body)
        return False
    items = data.get("data") or []
    if not items:
        print("  [FAIL] empty data:", json.dumps(data)[:300])
        return False
    item = items[0]
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=120) as r:
            raw = r.read()
    else:
        print("  [FAIL] no b64_json/url:", json.dumps(item)[:300])
        return False
    with open(out_path, "wb") as f:
        f.write(raw)
    print("  [OK] %s <- %s (%.1f KB)" % (os.path.basename(out_path), model, len(raw) / 1024))
    return True


import time

RETRY_ROUNDS = 20   # 中转站偶发 503 无可用账号：每张图最多重试 20 轮
RETRY_WAIT = 90     # 每轮间隔秒数


def main():
    os.makedirs(RESOURCE_DIR, exist_ok=True)
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for name, prompt in JOBS:
        if only and name != only:
            continue
        out_path = os.path.join(RESOURCE_DIR, name)
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 10000:
            print("SKIP (exists):", name, flush=True)
            continue
        print("GEN:", name, flush=True)
        ok = False
        for attempt in range(1, RETRY_ROUNDS + 1):
            for model in MODELS:
                if gen_one(model, prompt, out_path):
                    ok = True
                    break
            if ok:
                break
            print(f"  [RETRY] {name} 第 {attempt}/{RETRY_ROUNDS} 轮失败，{RETRY_WAIT}s 后重试", flush=True)
            time.sleep(RETRY_WAIT)
        if not ok:
            print("ALL ROUNDS FAILED:", name, flush=True)
            sys.exit(1)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
