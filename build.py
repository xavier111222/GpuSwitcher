# -*- coding: utf-8 -*-
"""
构建单文件 exe：
    python build.py

依赖：.venv 中已安装 pyinstaller / pillow
产物：dist/GPU切换助手.exe  →  自动复制到桌面
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "gpu_switcher.py")
ICON = os.path.join(HERE, "assets", "icon.ico")
NAME = "GPU切换助手"
DIST = os.path.join(HERE, "dist")


def desktop_dir() -> str:
    p = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(p):
        return p
    p = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")
    return p if os.path.isdir(p) else os.path.expanduser("~")


def main():
    if sys.platform != "win32":
        print("本工具仅支持 Windows。")
        return 1

    # 1) 图标
    if not os.path.isfile(ICON):
        print("[1/3] 生成图标 ...")
        subprocess.run([sys.executable, os.path.join(HERE, "make_icon.py")], check=False)
    else:
        print("[1/3] 图标已存在，跳过。")

    # 2) PyInstaller
    print("[2/3] 正在打包（约 1-3 分钟）...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--clean",
        "--name", NAME,
        "--distpath", DIST,
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        "--exclude-module", "pytest",
        "--exclude-module", "numpy",
    ]
    if os.path.isfile(ICON):
        cmd += ["--icon", ICON]
    cmd += [ENTRY]

    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        print("打包失败。")
        return r.returncode

    exe = os.path.join(DIST, NAME + ".exe")
    if not os.path.isfile(exe):
        print("未找到产物:", exe)
        return 2

    # 3) 复制到桌面
    print("[3/3] 复制到桌面 ...")
    dt = desktop_dir()
    target = os.path.join(dt, NAME + ".exe")
    try:
        shutil.copy2(exe, target)
        print("完成:", target)
    except Exception as e:  # noqa: BLE001
        print("复制失败:", e)
        print("产物仍在:", exe)
        return 3

    size = os.path.getsize(target) / 1024 / 1024
    print("大小: %.1f MB" % size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
