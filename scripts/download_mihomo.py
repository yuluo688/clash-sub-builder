#!/usr/bin/env python3
"""下载 MetaCubeX/mihomo 最新稳定版到 bin/。"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import platform
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx

REPO = "MetaCubeX/mihomo"
API = f"https://api.github.com/repos/{REPO}/releases/latest"


def detect_asset_name(tag: str) -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "386"
    else:
        arch = "amd64"

    if system == "windows":
        return f"mihomo-windows-{arch}-{tag}.zip"
    if system == "darwin":
        return f"mihomo-darwin-{arch}-{tag}.gz"
    # linux
    return f"mihomo-linux-{arch}-{tag}.gz"


def download_latest(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "clash-sub-builder",
    }
    with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as client:
        r = client.get(API)
        r.raise_for_status()
        release = r.json()
        tag = release["tag_name"]
        asset_name = detect_asset_name(tag)
        assets = {a["name"]: a for a in release.get("assets", [])}
        asset = assets.get(asset_name)
        if not asset:
            # 模糊匹配
            for name, a in assets.items():
                if "mihomo" in name and platform.system().lower()[:3] in name.lower():
                    if "amd64" in name or "arm64" in name:
                        asset = a
                        asset_name = name
                        break
        if not asset:
            raise RuntimeError(f"No suitable asset for {asset_name}. Available: {list(assets)[:20]}")

        url = asset["browser_download_url"]
        print(f"Downloading {asset_name} ...")
        data = client.get(url).content

    out = dest_dir / ("mihomo.exe" if platform.system().lower() == "windows" else "mihomo")

    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # 找可执行文件
            names = zf.namelist()
            exe = next((n for n in names if n.endswith(".exe") or n.endswith("mihomo")), names[0])
            with zf.open(exe) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
    elif asset_name.endswith(".gz") and not asset_name.endswith(".tar.gz"):
        raw = gzip.decompress(data)
        out.write_bytes(raw)
    elif asset_name.endswith(".tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            member = next(m for m in tf.getmembers() if m.isfile())
            f = tf.extractfile(member)
            assert f is not None
            out.write_bytes(f.read())
    else:
        out.write_bytes(data)

    if platform.system().lower() != "windows":
        out.chmod(0o755)

    print(f"Saved: {out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Download mihomo binary")
    parser.add_argument("-o", "--output-dir", default="bin", help="output directory")
    args = parser.parse_args()
    try:
        download_latest(Path(args.output_dir))
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())