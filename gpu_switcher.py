# -*- coding: utf-8 -*-
"""
GPU 切换助手 (GpuSwitcher)  —— 独立单文件版
==========================================
在 NVIDIA 独立显卡与集成显卡之间一键切换（节能模式 / 高性能模式），
并附带重启、注销、结束占用进程、功耗限制、系统还原点等配套操作。

许可证: MIT
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

import winreg

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

APP_NAME = "GPU 切换助手"
APP_VERSION = "1.0.0"
APP_ID = "GpuSwitcher"
TASK_NAME = "GpuSwitcher-AutoPowerSaving"

CREATE_NO_WINDOW = 0x08000000
POWERSHELL = "powershell.exe"

# 厂商 PCI Vendor ID
VENDOR_NVIDIA = "10DE"
VENDOR_INTEL = "8086"
VENDOR_AMD = "1002"


# ============================================================ 基础工具

def app_dir() -> str:
    """程序所在目录（兼容 PyInstaller 打包后的单文件模式）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def log_path() -> str:
    return os.path.join(app_dir(), "gpu_switcher.log")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """以管理员权限重新启动本程序"""
    exe = sys.executable
    args = " ".join('"%s"' % a for a in sys.argv[1:])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, args, app_dir(), 1
        )
        return rc > 32
    except Exception:
        return False


def _decode(raw: bytes) -> str:
    """Windows 控制台程序常输出 GBK，这里做 UTF-8 → GBK → 忽略 的兜底解码"""
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8")
    except Exception:  # noqa: BLE001
        text = ""
    if not text or text.count("\ufffd") > len(text) * 0.05:
        for enc in ("gbk", "mbcs", "latin-1"):
            try:
                alt = raw.decode(enc)
            except Exception:  # noqa: BLE001
                continue
            if alt.count("\ufffd") <= text.count("\ufffd"):
                text = alt
                break
    return text


def run_cmd(cmd, timeout: int = 120):
    """执行命令，返回 (returncode, stdout, stderr)"""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return (p.returncode,
                _decode(p.stdout).strip(),
                _decode(p.stderr).strip())
    except subprocess.TimeoutExpired:
        return -1, "", "执行超时（%ss）" % timeout
    except Exception as e:  # noqa: BLE001
        return -2, "", str(e)


PS_HEADER = (
    "$ErrorActionPreference='Continue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
)


def run_ps(script: str, timeout: int = 180):
    """执行 PowerShell 脚本"""
    cmd = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-Command", PS_HEADER + script,
    ]
    return run_cmd(cmd, timeout=timeout)


def ps_quote(s: str) -> str:
    """PowerShell 单引号字符串转义"""
    return "'" + (s or "").replace("'", "''") + "'"


# ============================================================ 显卡识别（注册表，毫秒级）

DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
ENUM_ROOT = r"SYSTEM\CurrentControlSet\Enum"
CLASS_ROOT = r"SYSTEM\CurrentControlSet\Control\Class"
HKLM = winreg.HKEY_LOCAL_MACHINE


def _subkeys(key):
    out = []
    i = 0
    while True:
        try:
            out.append(winreg.EnumKey(key, i))
            i += 1
        except OSError:
            break
    return out


def _reg(key, name):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _clean_desc(v):
    """'@oem24.inf,%nvidia_dev.2d19%;NVIDIA GeForce ...' -> 'NVIDIA GeForce ...'"""
    if not v:
        return ""
    v = str(v).strip()
    if v.startswith("@") and ";" in v:
        return v[v.rfind(";") + 1:].strip()
    return v


def _vram_from_driver_key(drv_key) -> int:
    raw = _reg(drv_key, "HardwareInformation.qwMemorySize")
    if isinstance(raw, bytes) and len(raw) >= 8:
        try:
            return int.from_bytes(raw[:8], "little")
        except Exception:  # noqa: BLE001
            return 0
    if isinstance(raw, int):
        return raw
    return 0


def vendor_of(instance_id: str) -> str:
    m = re.search(r"VEN_([0-9A-Fa-f]{4})", instance_id or "")
    return m.group(1).upper() if m else ""


def classify(name: str, instance_id: str, service: str = ""):
    """返回 (厂商, '独显'/'集显'/'其他')"""
    n = (name or "").upper()
    s = (service or "").upper()
    vid = vendor_of(instance_id)
    if vid == VENDOR_NVIDIA or s.startswith("NVLDDMKM") or "NVIDIA" in n:
        return "NVIDIA", "独显"
    if vid == VENDOR_INTEL or s.startswith("IGFX") or "INTEL" in n or "IRIS" in n or "ARC" in n:
        return "Intel", "集显"
    if vid == VENDOR_AMD or s.startswith("AMDKMDAG") or "AMD" in n or "RADEON" in n:
        if re.search(r"\bRX\b|\bR[579]\b|FIREPRO|RADEON\s*PRO|VEGA\s*(56|64)", n):
            return "AMD", "独显"
        return "AMD", "集显"
    if "MICROSOFT BASIC" in n or "基本显示" in n:
        return "Microsoft", "其他"
    return "未知", "其他"


class GpuDevice:
    def __init__(self, name, instance_id, disabled, driver="", vram_bytes=0,
                 service="", provider="", location=""):
        self.name = name or "未知设备"
        self.instance_id = instance_id or ""
        self.disabled = bool(disabled)
        self.driver = driver or ""
        self.vram_bytes = int(vram_bytes or 0)
        self.service = service or ""
        self.provider = provider or ""
        self.location = location or ""
        self.vendor, self.kind = classify(self.name, self.instance_id, self.service)

    # ---- 兼容字段
    @property
    def enabled(self) -> bool:
        return not self.disabled

    @property
    def status(self) -> str:
        return "Disabled" if self.disabled else "OK"

    @property
    def status_text(self) -> str:
        return "已禁用" if self.disabled else "已启用"

    @property
    def vram_text(self) -> str:
        b = self.vram_bytes
        if b <= 0:
            return "-"
        if b >= 1024 ** 3:
            return "%.1f GB" % (b / 1024 ** 3)
        return "%d MB" % (b // 1024 // 1024)

    def __repr__(self):
        return "<Gpu %s/%s %s %s>" % (self.vendor, self.kind, self.name, self.status_text)


def scan_gpus(emit=None):
    """通过注册表枚举所有显示适配器（无需外呼进程，极快）"""
    devices = []
    try:
        enum = winreg.OpenKey(HKLM, ENUM_ROOT)
    except OSError:
        if emit:
            emit("无法打开设备枚举注册表项。", level="error")
        return []

    for enumerator in _subkeys(enum):
        try:
            ke = winreg.OpenKey(enum, enumerator)
        except OSError:
            continue
        for dev in _subkeys(ke):
            try:
                kd = winreg.OpenKey(ke, dev)
            except OSError:
                continue
            for inst in _subkeys(kd):
                try:
                    ki = winreg.OpenKey(kd, inst)
                except OSError:
                    continue
                drv = _reg(ki, "Driver")
                if not drv or not str(drv).lower().startswith(DISPLAY_CLASS_GUID.lower()):
                    continue

                instance_id = "%s\\%s\\%s" % (enumerator.upper(), dev, inst)
                cfg = _reg(ki, "ConfigFlags")
                try:
                    disabled = bool(int(cfg or 0) & 0x1)
                except Exception:  # noqa: BLE001
                    disabled = False

                name = _clean_desc(_reg(ki, "FriendlyName")) or _clean_desc(_reg(ki, "DeviceDesc"))
                service = _reg(ki, "Service") or ""
                location = _reg(ki, "LocationInformation") or ""

                driver = provider = ""
                vram = 0
                try:
                    dk = winreg.OpenKey(HKLM, CLASS_ROOT + "\\" + str(drv))
                    driver = _reg(dk, "DriverVersion") or ""
                    provider = _reg(dk, "ProviderName") or ""
                    vram = _vram_from_driver_key(dk)
                    if not name:
                        name = _clean_desc(_reg(dk, "DriverDesc"))
                except OSError:
                    pass

                devices.append(GpuDevice(
                    name=name, instance_id=instance_id, disabled=disabled,
                    driver=str(driver), vram_bytes=vram, service=str(service),
                    provider=str(provider), location=str(location),
                ))
    return devices


def pick_discrete(gpus):
    return [g for g in gpus if g.vendor == "NVIDIA" and g.kind == "独显"]


def pick_integrated(gpus):
    return [g for g in gpus if g.kind == "集显"]


# ============================================================ 启用 / 禁用

def device_disabled(instance_id: str):
    """从注册表直接读取某设备的禁用状态；读不到返回 None"""
    parts = (instance_id or "").split("\\", 2)
    if len(parts) < 3:
        return None
    path = ENUM_ROOT + "\\" + "\\".join(parts)
    try:
        k = winreg.OpenKey(HKLM, path)
    except OSError:
        return None
    cfg = _reg(k, "ConfigFlags")
    try:
        return bool(int(cfg or 0) & 0x1)
    except Exception:  # noqa: BLE001
        return None


_NOT_FOUND_TOKENS = ("找不到", "未找到", "无法找到", "no matching", "not found")


def _looks_not_found(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _NOT_FOUND_TOKENS)


def set_device_state(dev: GpuDevice, enable: bool, emit=None):
    """启用/禁用设备：pnputil 优先（快），PnpDevice 命令兜底。返回 (ok, 说明)"""
    action = "启用" if enable else "禁用"
    instance_id = dev.instance_id

    if not is_admin():
        msg = "权限不足：启用/禁用设备需要管理员身份"
        if emit:
            emit("%s %s 失败: %s" % (action, dev.name, msg), level="error")
        return False, msg

    flag = "/enable-device" if enable else "/disable-device"
    last_err = ""
    for extra in ([], ["/force"]):
        rc, out, err = run_cmd(["pnputil.exe", flag, instance_id] + extra, timeout=120)
        combined = (out + " " + err).strip()
        if rc == 0 and not _looks_not_found(combined):
            time.sleep(0.5)
            now = device_disabled(instance_id)
            if now == (not enable):
                if emit:
                    emit("%s %s 成功（pnputil）" % (action, dev.name), level="ok")
                return True, "ok"
            last_err = "命令返回成功但设备状态未变更（可能被系统策略阻止）"
        else:
            lines = combined.splitlines()
            last_err = lines[0] if lines else "pnputil 返回 %d" % rc

    # 兜底：PnpDevice 命令
    verb = "Enable-PnpDevice" if enable else "Disable-PnpDevice"
    script = ("%s -InstanceId %s -Confirm:$false -ErrorAction Stop; "
              "Write-Output 'DONE'" % (verb, ps_quote(instance_id)))
    rc, out, err = run_ps(script, timeout=240)
    if rc == 0 and "DONE" in out:
        time.sleep(0.4)
        now = device_disabled(instance_id)
        if now is None or now == (not enable):
            if emit:
                emit("%s %s 成功（PnpDevice 命令）" % (action, dev.name), level="ok")
            return True, "ok"
        last_err = "命令已执行但状态未变更"

    if not last_err:
        last_err = (err or "未知错误").strip()[:200]
    low = last_err.lower()
    if "拒绝访问" in last_err or "access is denied" in low or "0x5" in low:
        last_err = "权限不足，请以管理员身份运行"
    if emit:
        emit("%s %s 失败: %s" % (action, dev.name, last_err), level="error")
    return False, last_err


# ============================================================ nvidia-smi

SMI_QUERY = (
    "--query-gpu=name,temperature.gpu,utilization.gpu,"
    "memory.used,memory.total,power.draw,power.limit"
    " --format=csv,noheader,nounits"
)


def find_nvidia_smi():
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(folder.strip('"'), "nvidia-smi.exe")
        if os.path.isfile(cand):
            return cand
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for cand in (
        os.path.join(root, "System32", "nvidia-smi.exe"),
        os.path.join(root, "SysWOW64", "nvidia-smi.exe"),
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def smi(args, timeout: int = 30):
    exe = find_nvidia_smi()
    if not exe:
        return False, ""
    rc, out, err = run_cmd([exe] + args, timeout=timeout)
    return rc == 0, out if rc == 0 else (err or out)


def gpu_stats():
    """返回显卡实时状态字典，失败返回 None"""
    ok, out = smi(SMI_QUERY.split(), timeout=25)
    if not ok or not out:
        return None
    line = out.splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 7:
        return None

    def num(v):
        try:
            return float(v)
        except Exception:  # noqa: BLE001
            return None

    return {
        "name": parts[0],
        "temp": num(parts[1]),
        "util": num(parts[2]),
        "mem_used": num(parts[3]),
        "mem_total": num(parts[4]),
        "power": num(parts[5]),
        "power_limit": num(parts[6]),
    }


def gpu_processes():
    """返回正在使用独显的进程列表 [(pid, name, mem), ...]"""
    ok, out = smi(
        ["--query-compute-apps=pid,process_name,used_memory",
         "--format=csv,noheader"],
        timeout=25,
    )
    if not ok or not out:
        return []
    result = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        pid = parts[0]
        name = parts[1] if len(parts) > 1 else ""
        mem = parts[2] if len(parts) > 2 else ""
        # WDDM 模式下显存常显示 [N/A]（只是碰过独显，并非真正占用），过滤掉
        if not mem or "N/A" in mem.upper():
            continue
        result.append((pid, os.path.basename(name), mem))
    return result


def power_limits():
    """读取功耗上下限 (min, max, current)"""
    ok, out = smi(["-q", "-d", "POWER"], timeout=30)
    if not ok:
        return None
    cur = mn = mx = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Power Limit:"):
            cur = _first_float(s)
        elif s.startswith("Min Power Limit:"):
            mn = _first_float(s)
        elif s.startswith("Max Power Limit:"):
            mx = _first_float(s)
    return mn, mx, cur


def _first_float(s: str):
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    return float(m.group(1)) if m else None


# ============================================================ 系统操作

def do_restart(delay=0):
    subprocess.Popen(["shutdown.exe", "/r", "/t", str(delay), "/c",
                      "GPU 切换助手：重启以释放独显"],
                     creationflags=CREATE_NO_WINDOW)


def do_shutdown(delay=0):
    subprocess.Popen(["shutdown.exe", "/s", "/t", str(delay), "/c",
                      "GPU 切换助手：关机"],
                     creationflags=CREATE_NO_WINDOW)


def do_logoff():
    subprocess.Popen(["shutdown.exe", "/l"], creationflags=CREATE_NO_WINDOW)


def do_sleep():
    rc, out, err = run_ps(
        "Add-Type -Namespace Win32 -Name Pw -MemberDefinition "
        "'[DllImport(\"PowrProf.dll\")] public static extern bool SetSuspendState(bool a,bool b,bool c);';"
        "[Win32.Pw]::SetSuspendState($false,$false,$false);"
    )
    return rc == 0


def create_restore_point(emit=None):
    """创建/启用系统还原点"""
    script = r"""
$drive = $env:SystemDrive
$prot = Get-ComputerRestorePoint -ErrorAction SilentlyContinue
try { Enable-ComputerRestore -Drive $drive -ErrorAction SilentlyContinue } catch {}
Checkpoint-Computer -Description 'GPU切换助手 自动还原点' -RestorePointType MODIFY_SETTINGS -ErrorAction Stop
Write-Output 'OK'
"""
    rc, out, err = run_ps(script, timeout=180)
    if rc == 0 and "OK" in out:
        if emit:
            emit("系统还原点已创建", level="ok")
        return True, "已创建还原点"
    msg = (err or out or "未知错误").strip().splitlines()
    msg = msg[0] if msg else "未知错误"
    if emit:
        emit("创建还原点失败: %s" % msg, level="warn")
    return False, msg


def set_autostart(enable: bool, mode: str = "power", emit=None):
    """开机/登录时自动应用节能模式（计划任务）"""
    if not getattr(sys, "frozen", False):
        return False, "仅打包后的 exe 支持此功能"
    exe = sys.executable
    if enable:
        rc, out, err = run_cmd([
            "schtasks.exe", "/Create", "/F",
            "/SC", "ONLOGON",
            "/TN", TASK_NAME,
            "/TR", '"%s" --apply %s --silent' % (exe, mode),
            "/RL", "HIGHEST",
        ], timeout=60)
        ok = rc == 0
    else:
        rc, out, err = run_cmd(["schtasks.exe", "/Delete", "/F", "/TN", TASK_NAME],
                               timeout=60)
        ok = rc == 0
    msg = (out or err or "").strip().splitlines()
    msg = msg[0] if msg else ("成功" if ok else "失败")
    if emit:
        emit("开机自动节能: %s" % ("已开启" if enable else "已关闭"), level="ok" if ok else "error")
    return ok, msg


def autostart_enabled() -> bool:
    rc, out, err = run_cmd(["schtasks.exe", "/Query", "/TN", TASK_NAME], timeout=30)
    return rc == 0


def set_power_limit(watts: float, emit=None):
    ok, out = smi(["-pl", str(watts)], timeout=60)
    if emit:
        if ok:
            emit("功耗上限已设为 %.0f W" % watts, level="ok")
        else:
            emit("设置功耗上限失败: %s" % (out.splitlines()[:1] or [""])[0], level="error")
    return ok


def write_recovery_script():
    """生成应急恢复脚本（内含本机真实设备 ID，优先用 pnputil）"""
    path = os.path.join(app_dir(), "恢复显卡（双击运行）.bat")
    ids = []
    try:
        for g in scan_gpus():
            if g.vendor == "NVIDIA" or g.kind == "集显":
                ids.append((g.name, g.instance_id))
    except Exception:  # noqa: BLE001
        pass

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "title 恢复显卡 - 重新启用所有显示适配器",
        "echo 正在重新启用显示适配器，请稍候 ...",
        "echo.",
    ]
    for name, iid in ids:
        lines.append('echo   %s' % name)
        lines.append('pnputil /enable-device "%s"' % iid)
    lines += [
        "",
        "echo.",
        'echo 若上方提示失败，请右键本文件选择「以管理员身份运行」。',
        "pause",
    ]
    content = "\r\n".join(lines)
    try:
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(content)
        return path
    except Exception:  # noqa: BLE001
        try:
            with open(path, "w", encoding="gbk", errors="replace") as f:
                f.write(content)
            return path
        except Exception:  # noqa: BLE001
            return None


def open_uri(uri: str):
    try:
        os.startfile(uri)  # noqa: S606
        return True
    except Exception:  # noqa: BLE001
        return False


def open_nvidia_panel():
    candidates = [
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvcplui.exe"),
        r"C:\Windows\System32\nvcplui.exe",
        r"C:\Program Files\NVIDIA Corporation\Control Panel Client\nvcplui.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            try:
                subprocess.Popen([c], creationflags=CREATE_NO_WINDOW)
                return True, c
            except Exception:  # noqa: BLE001
                continue
    ok = open_uri("shell:::{A0C3F1E1-9F5A-4E39-9D6B-1C9E7A2C1F3A}")
    return ok, "NVIDIA 控制面板"


def shader_cache_dirs():
    local = os.environ.get("LOCALAPPDATA", "")
    base = local or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return [
        os.path.join(base, "NVIDIA", "DXCache"),
        os.path.join(base, "NVIDIA", "GLCache"),
        os.path.join(base, "D3DSCache"),
    ]


def shader_cache_size():
    total, files = 0, 0
    for d in shader_cache_dirs():
        if not os.path.isdir(d):
            continue
        for root, _dirs, names in os.walk(d):
            for n in names:
                try:
                    total += os.path.getsize(os.path.join(root, n))
                    files += 1
                except Exception:  # noqa: BLE001
                    pass
    return total, files


def clear_shader_cache(emit=None):
    total, files = shader_cache_size()
    removed = 0
    for d in shader_cache_dirs():
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isfile(p) or os.path.islink(p):
                    os.remove(p)
                    removed += 1
                elif os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            except Exception:  # noqa: BLE001
                pass
    if emit:
        emit("着色器缓存已清理: %d 个文件，约 %.1f MB" % (removed, total / 1024 / 1024),
             level="ok")
    return removed, total


def fmt_bytes(n: float) -> str:
    return "%.1f MB" % (n / 1024 / 1024)


# ============================================================ GUI（Apple 风格）

from tkinter import font as tkfont


# ---------------------------------------------------------------- 主题

THEME = {
    "window": "#F5F5F7",
    "sidebar": "#EFEFF2",
    "card": "#FFFFFF",
    "soft": "#FAFAFC",
    "text": "#1D1D1F",
    "text2": "#6E6E73",
    "text3": "#86868B",
    "sep": "#E8E8ED",
    "border": "#D2D2D7",
    "blue": "#007AFF",
    "blue_h": "#0A84FF",
    "blue_p": "#0060DF",
    "green": "#34C759",
    "orange": "#FF9500",
    "red": "#FF3B30",
    "track": "#E9E9EE",
    "shadow": "#DEDEE3",
    "disabled": "#C7C7CC",
}

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"


def F(size=13, weight="normal", mono=False):
    return ((MONO_FONT if mono else UI_FONT), size, weight)


# ---------------------------------------------------------------- 圆角绘制

def rrect_points(x1, y1, x2, y2, r, steps=14):
    r = max(0.0, min(float(r), abs(x2 - x1) / 2.0, abs(y2 - y1) / 2.0))
    pts = [x1 + r, y1]

    def arc(cx, cy, a0, a1):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))

    arc(x2 - r, y1 + r, -90, 0)
    pts += [x2, y2 - r]
    arc(x2 - r, y2 - r, 0, 90)
    pts += [x1 + r, y2]
    arc(x1 + r, y2 - r, 90, 180)
    pts += [x1, y1 + r]
    arc(x1 + r, y1 + r, 180, 270)
    return pts


def rrect(canvas, x1, y1, x2, y2, r, **kw):
    kw.setdefault("outline", "")
    return canvas.create_polygon(rrect_points(x1, y1, x2, y2, r), **kw)


SHADOW_PAD = 5


class RoundedFrame(tk.Canvas):
    """圆角白色卡片，内部 .body 可自由摆放控件"""

    def __init__(self, master, radius=16, fill=None, pad=16, bg=None, shadow=True):
        bg = bg or THEME["window"]
        fill = fill or THEME["card"]
        tk.Canvas.__init__(self, master, bg=bg, highlightthickness=0, bd=0)
        self.radius = radius
        self.fill = fill
        self.pad = pad
        self.shadow = shadow
        self._h = pad * 2 + SHADOW_PAD
        self._last_w = -1
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad, anchor="nw", window=self.body)
        self.bind("<Configure>", self._on_size)
        self.body.bind("<Configure>", self._on_inner)
        self.configure(height=self._h)

    def _on_size(self, event):
        w = max(24, self.winfo_width())
        if w != self._last_w:
            self._last_w = w
            self.itemconfigure(self._win, width=max(12, w - 2 * self.pad))
        self._draw(w)

    def _on_inner(self, event=None):
        h = self.body.winfo_reqheight() + 2 * self.pad + SHADOW_PAD
        if abs(h - self._h) > 0.5:
            self._h = h
            self.configure(height=h)
            self._draw(self.winfo_width())

    def _draw(self, w):
        self.delete("card")
        h = self._h
        if w < 6 or h < 6:
            return
        if self.shadow:
            rrect(self, 3, 4, w - 3, h - 1, self.radius + 1,
                  fill=THEME["shadow"], tags="card")
        rrect(self, 0, 0, w - 1, h - SHADOW_PAD - 1, self.radius,
              fill=self.fill, outline=THEME["sep"], width=1, tags="card")


# ---------------------------------------------------------------- 控件

class AppleButton(tk.Canvas):
    """圆角按钮：primary / secondary / danger / success / plain"""

    STYLES = {
        "primary": (THEME["blue"], THEME["blue_h"], THEME["blue_p"], "#FFFFFF"),
        "secondary": ("#FFFFFF", "#F5F5F7", "#E5E5EA", THEME["text"]),
        "danger": (THEME["red"], "#FF453A", "#D70015", "#FFFFFF"),
        "success": (THEME["green"], "#30D158", "#248A3D", "#FFFFFF"),
        "plain": (None, None, None, THEME["blue"]),
    }

    def __init__(self, master, text="", command=None, style="primary",
                 width=150, height=40, radius=11, bg=None, font=None, state="normal"):
        self.bg = bg if bg is not None else THEME["card"]
        tk.Canvas.__init__(self, master, width=width, height=height,
                           bg=self.bg, highlightthickness=0, bd=0)
        self.text = text
        self.command = command
        self.style = style
        self.width_ = width
        self.height_ = height
        self.radius = radius
        self.font = font or F(11, "bold")
        self.state = state
        self._hover = False
        self._press = False
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._down)
        self.bind("<ButtonRelease-1>", self._up)
        self.draw()

    # -- 状态
    def set_enabled(self, on: bool):
        self.state = "normal" if on else "disabled"
        self.configure(cursor="hand2" if on else "")
        self.draw()

    def set_text(self, t):
        self.text = t
        self.draw()

    def _enter(self, _):
        self._hover = True
        self.configure(cursor="hand2" if self.state == "normal" else "")
        self.draw()

    def _leave(self, _):
        self._hover = False
        self._press = False
        self.draw()

    def _down(self, _):
        if self.state != "normal":
            return
        self._press = True
        self.draw()

    def _up(self, _):
        if self.state != "normal":
            return
        was = self._press
        self._press = False
        self.draw()
        if was and self.command:
            self.command()

    def draw(self):
        self.delete("all")
        w, h, r = self.width_, self.height_, self.radius
        base, hover, press, fg = self.STYLES.get(self.style, self.STYLES["primary"])
        dis = self.state != "normal"

        if dis:
            fill, fg = THEME["disabled"], "#FFFFFF" if self.style != "secondary" else "#AEAEB2"
            if self.style == "plain":
                fill, fg = None, THEME["disabled"]
        else:
            fill = base
            if self._press:
                fill = press
            elif self._hover:
                fill = hover

        if fill:
            outline = THEME["border"] if self.style == "secondary" else ""
            rrect(self, 1, 1, w - 2, h - 2, r, fill=fill, outline=outline, width=1)
        elif self.style == "plain" and self._hover and not dis:
            rrect(self, 1, 1, w - 2, h - 2, r, fill="#EFEFF2", outline="")

        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)


class SegmentedControl(tk.Canvas):
    """iOS 风格分段控件（带滑动 thumb）"""

    def __init__(self, master, options, command=None, width=340, height=38,
                 bg=None, radius=10):
        self.bg = bg or THEME["card"]
        tk.Canvas.__init__(self, master, width=width, height=height,
                           bg=self.bg, highlightthickness=0, bd=0)
        self.options = list(options)
        self.command = command
        self.width_ = width
        self.height_ = height
        self.radius = radius
        self.index = 0
        self._shown = 0.0
        self._anim = None
        self.bind("<Button-1>", self._click)
        self.configure(cursor="hand2")
        self.draw()

    def select(self, i, fire=False):
        i = max(0, min(i, len(self.options) - 1))
        if i == self.index and self._shown == float(i):
            return
        self.index = i
        self._animate_to(float(i))
        if fire and self.command:
            self.command(i, self.options[i])

    def _click(self, event):
        n = len(self.options)
        seg = self.width_ / float(n)
        i = int(event.x // seg)
        if 0 <= i < n and i != self.index:
            self.select(i)
            if self.command:
                self.command(i, self.options[i])

    def _animate_to(self, target):
        if self._anim:
            try:
                self.after_cancel(self._anim)
            except Exception:  # noqa: BLE001
                pass
        start = self._shown
        steps = 8
        self._step = 0

        def tick():
            self._step += 1
            t = self._step / float(steps)
            self._shown = start + (target - start) * t
            self.draw()
            if self._step < steps:
                self._anim = self.after(16, tick)
            else:
                self._shown = target
                self._anim = None
                self.draw()

        tick()

    def draw(self):
        self.delete("all")
        w, h, n = self.width_, self.height_, len(self.options)
        rrect(self, 0, 0, w - 1, h - 1, self.radius, fill=THEME["track"])
        seg = w / float(n)
        x = 3 + self._shown * seg
        tw = seg - 6
        rrect(self, x + 1, 5, x + tw + 1, h - 4, self.radius - 2,
              fill=THEME["shadow"], outline="")
        rrect(self, x, 3, x + tw, h - 6, self.radius - 2,
              fill="#FFFFFF", outline=THEME["sep"], width=1)
        for i, label in enumerate(self.options):
            cx = seg * (i + 0.5)
            self.create_text(cx, h / 2, text=label,
                             fill=THEME["text"] if i == self.index else THEME["text2"],
                             font=F(11, "bold" if i == self.index else "normal"))


class ToggleSwitch(tk.Canvas):
    """iOS 风格开关"""

    def __init__(self, master, variable=None, command=None, bg=None,
                 width=48, height=29):
        self.bg = bg or THEME["card"]
        tk.Canvas.__init__(self, master, width=width, height=height,
                           bg=self.bg, highlightthickness=0, bd=0)
        self.var = variable
        self.command = command
        self.width_, self.height_ = width, height
        self._pos = 1.0 if (variable and variable.get()) else 0.0
        self.bind("<Button-1>", self._click)
        self.configure(cursor="hand2")
        self.draw()

    def _click(self, _):
        if self.var is None:
            return
        self.var.set(not self.var.get())
        self._animate()
        if self.command:
            self.command()

    def _animate(self):
        target = 1.0 if self.var.get() else 0.0
        start, steps = self._pos, 8
        self._i = 0

        def tick():
            self._i += 1
            self._pos = start + (target - start) * (self._i / float(steps))
            self.draw()
            if self._i < steps:
                self.after(16, tick)
            else:
                self._pos = target

        tick()

    def sync(self):
        self._pos = 1.0 if (self.var and self.var.get()) else 0.0
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.width_, self.height_
        on = self._pos
        r = h / 2.0
        col = THEME["green"] if on > 0.5 else THEME["track"]
        rrect(self, 0, 0, w - 1, h - 1, r, fill=col, outline="")
        kx = 3 + on * (w - h + 3 - 3)
        kd = h - 6
        self.create_oval(kx + 1, 4, kx + kd + 1, 4 + kd,
                         fill=THEME["shadow"], outline="")
        self.create_oval(kx, 3, kx + kd, 3 + kd, fill="#FFFFFF", outline="")


class Pill(tk.Canvas):
    """小胶囊标签（状态徽章）"""

    def __init__(self, master, text="", color=None, bg=None, size=11, pad=9, height=24):
        self.bg = bg or THEME["window"]
        self.color = color or THEME["blue"]
        tk.Canvas.__init__(self, master, height=height, bg=self.bg,
                           highlightthickness=0, bd=0)
        self.text = text
        self.size = size
        self.pad = pad
        self.height_ = height
        self.redraw()

    def set(self, text, color=None):
        self.text = text
        if color:
            self.color = color
        self.redraw()

    def redraw(self):
        self.delete("all")
        f = tkfont.Font(family=UI_FONT, size=self.size, weight="bold")
        w = f.measure(self.text) + self.pad * 2
        self.configure(width=w)
        rrect(self, 0, 0, w - 1, self.height_ - 1, self.height_ / 2.0, fill=self.color)
        self.create_text(w / 2, self.height_ / 2 + 0.5, text=self.text,
                         fill="#FFFFFF", font=(UI_FONT, self.size, "bold"))


# ---------------------------------------------------------------- 侧边栏

def draw_icon(c, kind, cx, cy, color, s=15):
    if kind == "switch":
        c.create_line(cx - s / 2, cy - 3.5, cx + s / 2 - 3, cy - 3.5, fill=color,
                      width=1.7, arrow=tk.LAST, arrowshape=(5, 6, 3))
        c.create_line(cx + s / 2, cy + 3.5, cx - s / 2 + 3, cy + 3.5, fill=color,
                      width=1.7, arrow=tk.LAST, arrowshape=(5, 6, 3))
    elif kind == "monitor":
        base = cy + 6
        for dx, hh in ((-6, 7), (-1.5, 12), (3, 5)):
            c.create_rectangle(cx + dx, base - hh, cx + dx + 3, base,
                               fill=color, outline="")
    elif kind == "apps":
        for i in range(3):
            c.create_rectangle(cx - 7, cy - 6 + i * 5.5, cx + 7, cy - 3 + i * 5.5,
                               fill=color, outline="")
    elif kind == "tools":
        c.create_oval(cx - 3.6, cy - 3.6, cx + 3.6, cy + 3.6, outline=color, width=1.5)
        for a in range(0, 360, 45):
            ar = math.radians(a)
            c.create_line(cx + 5 * math.cos(ar), cy + 5 * math.sin(ar),
                          cx + 7.5 * math.cos(ar), cy + 7.5 * math.sin(ar),
                          fill=color, width=1.5)


class Sidebar(tk.Canvas):
    ROW_H = 40

    def __init__(self, master, items, on_select, width=196):
        tk.Canvas.__init__(self, master, width=width, bg=THEME["sidebar"],
                           highlightthickness=0, bd=0)
        self.items = items
        self.on_select = on_select
        self.index = 0
        self.hover = None
        self.width_ = width
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)

    def _row_at(self, y):
        i = int((y - self.top) // self.ROW_H)
        return i if 0 <= i < len(self.items) else None

    def _motion(self, e):
        i = self._row_at(e.y)
        if i != self.hover:
            self.hover = i
            self.draw()
            self.configure(cursor="hand2" if i is not None else "")

    def _leave(self, _):
        if self.hover is not None:
            self.hover = None
            self.draw()

    def _click(self, e):
        i = self._row_at(e.y)
        if i is not None and i != self.index:
            self.index = i
            self.draw()
            self.on_select(i)

    def draw(self):
        self.delete("all")
        w = self.width_
        top = self.top = 96
        # 应用标题
        self.create_text(20, 34, anchor="w", text="GPU 切换助手",
                         fill=THEME["text"], font=F(15, "bold"))
        self.create_text(20, 58, anchor="w", text="NVIDIA 独显 / 集显 一键切换",
                         fill=THEME["text3"], font=F(9))
        self.create_line(14, 80, w - 14, 80, fill=THEME["border"])

        pad = 12
        for i, (icon, label) in enumerate(self.items):
            y = top + i * self.ROW_H
            x1, x2 = pad, w - pad
            if i == self.index:
                rrect(self, x1, y, x2, y + 34, 9, fill=THEME["blue"])
                color = "#FFFFFF"
            elif i == self.hover:
                rrect(self, x1, y, x2, y + 34, 9, fill="#E3E3E8")
                color = THEME["text"]
            else:
                color = THEME["text"]
            draw_icon(self, icon, x1 + 18, y + 17, color)
            self.create_text(x1 + 38, y + 17, anchor="w", text=label,
                             fill=color, font=F(12, "bold" if i == self.index else "normal"))

        # 底部权限徽章
        self.create_text(20, self.winfo_height() - 26 if self.winfo_height() > 200 else 640,
                         anchor="w", text="v" + APP_VERSION, fill=THEME["text3"], font=F(9))


class ScrollFrame(tk.Frame):
    def __init__(self, master, bg):
        tk.Frame.__init__(self, master, bg=bg)
        self.bg = bg
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.body.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.body)

    def _bind_wheel(self, w):
        w.bind("<MouseWheel>", self._wheel)

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

    def bind_wheel_all(self):
        for child in self.body.winfo_children():
            self._recurse(child)

    def _recurse(self, w):
        try:
            w.bind("<MouseWheel>", self._wheel)
        except Exception:  # noqa: BLE001
            pass
        for c in w.winfo_children():
            self._recurse(c)


# ---------------------------------------------------------------- 进程与 GPU 偏好

GPU_PREF_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
PREF_LABELS = {0: "让 Windows 决定", 1: "节能（集显）", 2: "高性能（独显）"}
PREF_SHORT = {0: "默认", 1: "集显", 2: "独显"}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def enum_processes():
    """用原生 API 枚举进程及可执行文件路径，返回 [(pid, path), ...]"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except Exception:  # noqa: BLE001
        return []

    psapi.EnumProcesses.argtypes = [ctypes.POINTER(ctypes.wintypes.DWORD),
                                    ctypes.wintypes.DWORD,
                                    ctypes.POINTER(ctypes.wintypes.DWORD)]
    psapi.EnumProcesses.restype = ctypes.wintypes.BOOL
    k32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                                ctypes.wintypes.DWORD]
    k32.OpenProcess.restype = ctypes.wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [ctypes.wintypes.HANDLE,
                                               ctypes.wintypes.DWORD,
                                               ctypes.c_wchar_p,
                                               ctypes.POINTER(ctypes.wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL

    arr = (ctypes.wintypes.DWORD * 8192)()
    needed = ctypes.wintypes.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr),
                               ctypes.byref(needed)):
        return []
    count = min(needed.value // ctypes.sizeof(ctypes.wintypes.DWORD), 8192)

    out = []
    for i in range(count):
        pid = arr[i]
        if pid <= 4:
            continue
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                out.append((pid, buf.value))
        finally:
            k32.CloseHandle(h)
    return out


def gpu_pref_all():
    """读取所有已保存的 GPU 偏好规则 {exe路径: 0/1/2}"""
    rules = {}
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, GPU_PREF_KEY)
    except OSError:
        return rules
    try:
        i = 0
        while True:
            try:
                name, val, _ = winreg.EnumValue(k, i)
                i += 1
            except OSError:
                break
            m = re.search(r"GpuPreference\s*=\s*(\d+)", str(val))
            rules[name] = int(m.group(1)) if m else 0
    finally:
        winreg.CloseKey(k)
    return rules


def gpu_pref_get(path):
    return gpu_pref_all().get(path, 0)


def gpu_pref_set(path, mode):
    """设置某程序的 GPU 偏好：0 默认 / 1 节能(集显) / 2 高性能(独显)"""
    if not path:
        return False, "路径为空"
    try:
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, GPU_PREF_KEY, 0,
                               winreg.KEY_READ | winreg.KEY_WRITE)
    except OSError as e:  # noqa: BLE001
        return False, str(e)
    try:
        try:
            old, _ = winreg.QueryValueEx(k, path)
            old = str(old)
        except OSError:
            old = ""
        parts = [p.strip() for p in old.split(";")
                 if p.strip() and not p.strip().lower().startswith("gpupreference")]
        if mode in (1, 2):
            parts.insert(0, "GpuPreference=%d" % mode)
        if parts:
            value = "".join(p + ";" for p in parts)
            winreg.SetValueEx(k, path, 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(k, path)
            except OSError:
                winreg.SetValueEx(k, path, 0, winreg.REG_SZ, "")
        return True, "ok"
    except OSError as e:  # noqa: BLE001
        return False, str(e)
    finally:
        winreg.CloseKey(k)


def gpu_pref_remove(path):
    return gpu_pref_set(path, 0)


# ============================================================ 主界面

class App:
    PAGES = [("switch", "切换"), ("monitor", "监控"), ("apps", "应用分配"), ("tools", "更多工具")]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.gpus = []
        self.busy = False
        self.stop_monitor = threading.Event()
        self.auto_restart = tk.BooleanVar(value=False)
        self.make_restore = tk.BooleanVar(value=False)
        self.auto_power_var = tk.BooleanVar(value=False)
        self.power_limit_var = tk.StringVar(value="")
        self.search_var = tk.StringVar()
        self.proc_rows = []
        self.stat_vars = {}

        root.title("%s v%s" % (APP_NAME, APP_VERSION))
        root.geometry("1040x760")
        root.minsize(940, 660)
        root.configure(bg=THEME["window"])

        self._style()
        self._build()

        self.log("程序已启动 · %s" % app_dir())
        if is_admin():
            self.log("当前以管理员权限运行。", "ok")
            self.admin_pill.set("● 管理员", THEME["green"])
        else:
            self.log("当前为普通权限：切换显卡需要管理员权限。", "warn")
            self.admin_pill.set("● 普通权限", THEME["orange"])

        self.refresh(initial=True)
        self.refresh_apps()
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    # ---------------------------------------------------------- 样式

    def _style(self):
        s = ttk.Style()
        if "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("Apple.Treeview", background=THEME["card"],
                    fieldbackground=THEME["card"], foreground=THEME["text"],
                    borderwidth=0, relief="flat", rowheight=32, font=F(10),
                    bordercolor=THEME["card"], lightcolor=THEME["card"],
                    darkcolor=THEME["card"])
        s.configure("Apple.Treeview.Heading", background=THEME["card"],
                    foreground=THEME["text3"], borderwidth=0, relief="flat",
                    font=F(9, "bold"))
        s.map("Apple.Treeview",
              background=[("selected", THEME["blue"])],
              foreground=[("selected", "#FFFFFF")])
        s.configure("Apple.Vertical.TScrollbar", background=THEME["window"],
                    troughcolor=THEME["window"], borderwidth=0, arrowsize=12)

    # ---------------------------------------------------------- 布局

    def _build(self):
        main = tk.Frame(self.root, bg=THEME["window"])
        main.pack(fill="both", expand=True)

        self.sidebar = Sidebar(main, [(k, lbl) for k, lbl in self.PAGES],
                               on_select=self.show_page)
        self.sidebar.pack(side="left", fill="y")

        right = tk.Frame(main, bg=THEME["window"])
        right.pack(side="left", fill="both", expand=True)

        # 顶部标题栏
        head = tk.Frame(right, bg=THEME["window"])
        head.pack(fill="x", padx=30, pady=(22, 10))
        self.page_title = tk.Label(head, text="切换", bg=THEME["window"],
                                   fg=THEME["text"], font=F(24, "bold"))
        self.page_title.pack(side="left")
        self.admin_pill = Pill(head, "● 检测中", THEME["text3"], bg=THEME["window"])
        self.admin_pill.pack(side="right", padx=6)
        self.elevate_btn = AppleButton(head, "获取管理员权限", command=self.on_elevate,
                                       style="secondary", width=132, height=30, radius=9,
                                       bg=THEME["window"], font=F(10))
        self.elevate_btn.pack(side="right")
        if is_admin():
            self.elevate_btn.pack_forget()

        # 页面容器
        self.pages_box = tk.Frame(right, bg=THEME["window"])
        self.pages_box.pack(fill="both", expand=True)
        self.pages = {}
        for key, _ in self.PAGES:
            sc = ScrollFrame(self.pages_box, THEME["window"])
            sc.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = sc
        self.pages_box.grid_rowconfigure(0, weight=1)
        self.pages_box.grid_columnconfigure(0, weight=1)

        self._page_switch()
        self._page_monitor()
        self._page_apps()
        self._page_tools()
        for sc in self.pages.values():
            sc.bind_wheel_all()

        self.show_page(0, silent=True)

        # 底部日志
        self._build_log(right)

    def _card(self, parent, title=None, pad=18, radius=16):
        c = RoundedFrame(parent, radius=radius, fill=THEME["card"], pad=pad)
        c.pack(fill="x", padx=28, pady=(0, 12))
        if title:
            tk.Label(c.body, text=title, bg=THEME["card"], fg=THEME["text"],
                     font=F(13, "bold")).pack(anchor="w", pady=(0, 12))
        return c.body

    def _build_log(self, parent):
        box = tk.Frame(parent, bg=THEME["window"])
        box.pack(fill="x", padx=28, pady=(4, 16))
        card = RoundedFrame(box, radius=14, fill=THEME["card"], pad=12)
        card.pack(fill="x")
        top = tk.Frame(card.body, bg=THEME["card"])
        top.pack(fill="x")
        tk.Label(top, text="操作日志", bg=THEME["card"], fg=THEME["text"],
                 font=F(11, "bold")).pack(side="left")
        AppleButton(top, "清空", command=self.clear_log, style="plain",
                    width=54, height=24, radius=7, bg=THEME["card"],
                    font=F(9)).pack(side="right")
        self.logbox = tk.Text(card.body, height=3, font=F(9, mono=True),
                              relief="flat", bd=0, bg=THEME["card"],
                              fg=THEME["text2"], highlightthickness=0, wrap="word")
        self.logbox.pack(fill="x", pady=(8, 0))
        for tag, color in (("ok", THEME["green"]), ("warn", THEME["orange"]),
                           ("error", THEME["red"]), ("info", THEME["text2"])):
            self.logbox.tag_config(tag, foreground=color)
        self.logbox.configure(state="disabled")

    def show_page(self, i, silent=False):
        key = self.PAGES[i][0]
        self.pages[key].tkraise()
        if not silent:
            self.sidebar.index = i
            self.sidebar.draw()
        self.page_title.configure(text=self.PAGES[i][1])

    # ---------------------------------------------------------- 页面 1：切换

    def _page_switch(self):
        p = self.pages["switch"].body
        tk.Frame(p, bg=THEME["window"], height=4).pack()

        hero = self._card(p)
        self.mode_label = tk.Label(hero, text="检测中…", bg=THEME["card"],
                                   fg=THEME["text"], font=F(18, "bold"))
        self.mode_label.pack(anchor="w")
        self.mode_desc = tk.Label(hero, text="", bg=THEME["card"], fg=THEME["text2"],
                                  font=F(10), justify="left", wraplength=620)
        self.mode_desc.pack(anchor="w", pady=(6, 14))
        self.seg = SegmentedControl(hero, ["🍃 节能（集显）", "⚡ 高性能（独显）"],
                                    command=self.on_segment, width=330, height=38,
                                    bg=THEME["card"])
        self.seg.pack(side="left", anchor="s", pady=(6, 0))

        opts = tk.Frame(hero, bg=THEME["card"])
        opts.pack(side="right", anchor="s", pady=(0, 2))
        r1 = tk.Frame(opts, bg=THEME["card"])
        r1.pack(fill="x", pady=2)
        tk.Label(r1, text="操作前创建还原点", bg=THEME["card"], fg=THEME["text2"],
                 font=F(10)).pack(side="left", padx=(0, 8))
        ToggleSwitch(r1, variable=self.make_restore, bg=THEME["card"]).pack(side="right")
        r2 = tk.Frame(opts, bg=THEME["card"])
        r2.pack(fill="x", pady=(8, 2))
        tk.Label(r2, text="切换后自动重启", bg=THEME["card"], fg=THEME["text2"],
                 font=F(10)).pack(side="left", padx=(0, 8))
        ToggleSwitch(r2, variable=self.auto_restart, bg=THEME["card"]).pack(side="right")

        box = self._card(p, "显示适配器")
        cols = ("name", "vendor", "kind", "status", "driver", "vram")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=4,
                                 style="Apple.Treeview")
        for c, t, w in (("name", "设备", 250), ("vendor", "厂商", 80),
                        ("kind", "类型", 60), ("status", "状态", 70),
                        ("driver", "驱动版本", 110), ("vram", "显存", 70)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x")
        self.tree.tag_configure("disabled", foreground=THEME["text3"])
        self.tree.bind("<Button-3>", self._on_tree_menu)

        row = tk.Frame(box, bg=THEME["card"])
        row.pack(fill="x", pady=(12, 0))
        AppleButton(row, "刷新", command=lambda: self.refresh(False), style="secondary",
                    width=88, height=32, radius=9, font=F(10)).pack(side="left")
        AppleButton(row, "启用选中", command=self._enable_selected, style="secondary",
                    width=88, height=32, radius=9, font=F(10)).pack(side="left", padx=8)
        AppleButton(row, "禁用选中", command=self._disable_selected, style="secondary",
                    width=88, height=32, radius=9, font=F(10)).pack(side="left")

        act = self._card(p, "电源动作")
        row2 = tk.Frame(act, bg=THEME["card"])
        row2.pack(fill="x")
        self.btn_restart = AppleButton(row2, "🔄  重启", command=self.on_restart,
                                       style="secondary", width=118, height=36, font=F(10))
        self.btn_restart.pack(side="left")
        self.btn_logoff = AppleButton(row2, "🚪  注销（温和释放独显）", command=self.on_logoff,
                                      style="secondary", width=200, height=36, font=F(10))
        self.btn_logoff.pack(side="left", padx=10)
        AppleButton(row2, "🌙  睡眠", command=lambda: self._run(self._sleep),
                    style="secondary", width=104, height=36, font=F(10)).pack(side="left")
        AppleButton(row2, "⏻  关机", command=self.on_shutdown,
                    style="secondary", width=104, height=36, font=F(10)).pack(side="left", padx=10)

    def on_segment(self, index, _label):
        if index == 0:
            self.on_power_saving()
        else:
            self.on_performance()

    # ---------------------------------------------------------- 页面 2：监控

    def _page_monitor(self):
        p = self.pages["monitor"].body
        tk.Frame(p, bg=THEME["window"], height=4).pack()

        box = self._card(p)
        head = tk.Frame(box, bg=THEME["card"])
        head.pack(fill="x")
        tk.Label(head, text="独显实时状态", bg=THEME["card"], fg=THEME["text"],
                 font=F(13, "bold")).pack(side="left")
        self.monitor_pill = Pill(head, "每 2 秒刷新", THEME["blue"], bg=THEME["card"], size=9)
        self.monitor_pill.pack(side="right")

        grid = tk.Frame(box, bg=THEME["card"])
        grid.pack(fill="x", pady=(12, 0))
        rows = [("显卡", "name"), ("温度", "temp"), ("利用率", "util"),
                ("显存占用", "mem"), ("功耗", "power"), ("功耗上限", "plimit")]
        for i, (label, key) in enumerate(rows):
            r, c = divmod(i, 3)
            tile = tk.Frame(grid, bg=THEME["soft"], highlightthickness=1,
                            highlightbackground=THEME["sep"], highlightcolor=THEME["sep"])
            tile.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
            grid.grid_columnconfigure(c, weight=1)
            v = tk.StringVar(value="—")
            self.stat_vars[key] = v
            tk.Label(tile, text=label, bg=THEME["soft"], fg=THEME["text3"],
                     font=F(9)).pack(anchor="w", padx=12, pady=(10, 0))
            tk.Label(tile, textvariable=v, bg=THEME["soft"], fg=THEME["text"],
                     font=F(13, "bold"), wraplength=190,
                     justify="left").pack(anchor="w", padx=12, pady=(2, 12))
        self.smi_note = tk.Label(box, text="", bg=THEME["card"], fg=THEME["text3"],
                                 font=F(9))
        self.smi_note.pack(anchor="w", pady=(6, 0))
        if not find_nvidia_smi():
            self.smi_note.configure(text="未找到 nvidia-smi，监控不可用（独显被禁用时同样不可用）。")

        box2 = self._card(p, "系统入口")
        row = tk.Frame(box2, bg=THEME["card"])
        row.pack(fill="x")
        AppleButton(row, "Windows 图形性能首选项",
                    command=lambda: open_uri("ms-settings:display-advancedgraphics"),
                    style="secondary", width=210, height=36, font=F(10)).pack(side="left")
        AppleButton(row, "NVIDIA 控制面板", command=lambda: self._run(self._open_nv),
                    style="secondary", width=140, height=36, font=F(10)).pack(side="left", padx=10)
        AppleButton(row, "设备管理器", command=lambda: open_uri("devmgmt.msc"),
                    style="secondary", width=110, height=36, font=F(10)).pack(side="left")

    # ---------------------------------------------------------- 页面 3：应用 GPU 分配

    def _page_apps(self):
        p = self.pages["apps"].body
        tk.Frame(p, bg=THEME["window"], height=4).pack()

        tip = self._card(p)
        tk.Label(tip, text="为每个程序单独指定用哪块显卡", bg=THEME["card"],
                 fg=THEME["text"], font=F(13, "bold")).pack(anchor="w")
        tk.Label(tip,
                 text="Windows 原生支持按程序分配 GPU。下面列出正在运行的程序，"
                      "选中后点按钮即可指定：\n"
                      "「集显」= 省电模式，程序改用集成显卡；「独显」= 高性能，"
                      "程序使用 NVIDIA 显卡。设置后需重启该程序生效。",
                 bg=THEME["card"], fg=THEME["text2"], font=F(10),
                 justify="left", wraplength=640).pack(anchor="w", pady=(6, 0))

        box = self._card(p, "运行中的程序")
        filt = tk.Frame(box, bg=THEME["card"])
        filt.pack(fill="x", pady=(0, 10))
        e = tk.Entry(filt, textvariable=self.search_var, relief="solid", bd=1,
                     highlightthickness=1, highlightcolor=THEME["blue"],
                     highlightbackground=THEME["border"], font=F(10), width=30)
        e.pack(side="left", ipady=4)
        e.bind("<Return>", lambda _e: self.refresh_apps())
        AppleButton(filt, "搜索", command=self.refresh_apps, style="secondary",
                    width=72, height=30, radius=9, font=F(10)).pack(side="left", padx=8)
        AppleButton(filt, "刷新", command=self.refresh_apps, style="secondary",
                    width=72, height=30, radius=9, font=F(10)).pack(side="left")

        cols = ("name", "pref", "dgpu", "path")
        self.apptree = ttk.Treeview(box, columns=cols, show="headings", height=11,
                                    style="Apple.Treeview")
        for c, t, w in (("name", "程序", 150), ("pref", "GPU 分配", 110),
                        ("dgpu", "独显占用", 90), ("path", "路径", 300)):
            self.apptree.heading(c, text=t)
            self.apptree.column(c, width=w, anchor="w")
        self.apptree.pack(fill="x")
        self.apptree.tag_configure("dgpu", foreground=THEME["red"])
        self.apptree.tag_configure("igpu", foreground=THEME["green"])

        row = tk.Frame(box, bg=THEME["card"])
        row.pack(fill="x", pady=(12, 0))
        AppleButton(row, "🍃 改用集显（省电）", command=lambda: self.set_app_pref(1),
                    style="success", width=170, height=36, font=F(10)).pack(side="left")
        AppleButton(row, "⚡ 改用独显（高性能）", command=lambda: self.set_app_pref(2),
                    style="primary", width=180, height=36, font=F(10)).pack(side="left", padx=10)
        AppleButton(row, "恢复默认", command=lambda: self.set_app_pref(0),
                    style="secondary", width=100, height=36, font=F(10)).pack(side="left")

        row2 = tk.Frame(box, bg=THEME["card"])
        row2.pack(fill="x", pady=(10, 0))
        AppleButton(row2, "结束选中进程", command=self.kill_app, style="danger",
                    width=130, height=34, font=F(10)).pack(side="left")
        AppleButton(row2, "手动添加程序…", command=self.add_app, style="secondary",
                    width=150, height=34, font=F(10)).pack(side="left", padx=10)
        AppleButton(row2, "打开 Windows 设置",
                    command=lambda: open_uri("ms-settings:display-advancedgraphics"),
                    style="secondary", width=170, height=34, font=F(10)).pack(side="left")

        box3 = self._card(p, "已保存的规则")
        self.ruletree = ttk.Treeview(box3, columns=("path", "rule"), show="headings",
                                     height=5, style="Apple.Treeview")
        self.ruletree.heading("path", text="程序")
        self.ruletree.heading("rule", text="分配")
        self.ruletree.column("path", width=430, anchor="w")
        self.ruletree.column("rule", width=120, anchor="w")
        self.ruletree.pack(fill="x")
        r3 = tk.Frame(box3, bg=THEME["card"])
        r3.pack(fill="x", pady=(12, 0))
        AppleButton(r3, "刷新规则", command=self.refresh_rules, style="secondary",
                    width=100, height=32, radius=9, font=F(10)).pack(side="left")
        AppleButton(r3, "删除选中规则", command=self.remove_rule, style="secondary",
                    width=120, height=32, radius=9, font=F(10)).pack(side="left", padx=10)

    # ---------------------------------------------------------- 页面 4：更多工具

    def _page_tools(self):
        p = self.pages["tools"].body
        tk.Frame(p, bg=THEME["window"], height=4).pack()

        box = self._card(p, "功耗上限")
        row = tk.Frame(box, bg=THEME["card"])
        row.pack(fill="x")
        tk.Label(row, text="限制独显功耗 (W)", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        sp = tk.Spinbox(row, from_=10, to=400, width=7, font=F(11),
                        textvariable=self.power_limit_var, relief="solid", bd=1,
                        highlightthickness=1, highlightbackground=THEME["border"])
        sp.pack(side="left", padx=10)
        AppleButton(row, "读取当前", command=lambda: self.read_power(False),
                    style="secondary", width=96, height=32, radius=9, font=F(10)).pack(side="left")
        AppleButton(row, "应用", command=self.apply_power, style="primary",
                    width=76, height=32, radius=9, font=F(10)).pack(side="left", padx=8)
        tk.Label(box, text="需管理员权限；多数笔记本的功耗由固件锁定，可能不支持。",
                 bg=THEME["card"], fg=THEME["text3"], font=F(9)).pack(anchor="w", pady=(10, 0))

        box2 = self._card(p, "开机自动化")
        r = tk.Frame(box2, bg=THEME["card"])
        r.pack(fill="x")
        tk.Label(r, text="登录后自动关闭独显（节能模式）", bg=THEME["card"],
                 fg=THEME["text"], font=F(11)).pack(side="left")
        ToggleSwitch(r, variable=self.auto_power_var, command=self.toggle_autostart,
                     bg=THEME["card"]).pack(side="right")

        box3 = self._card(p, "维护与应急")
        r3 = tk.Frame(box3, bg=THEME["card"])
        r3.pack(fill="x")
        AppleButton(r3, "创建系统还原点", command=lambda: self._run(self._restore_point),
                    style="secondary", width=140, height=34, font=F(10)).pack(side="left")
        AppleButton(r3, "清理着色器缓存", command=self.clear_cache,
                    style="secondary", width=140, height=34, font=F(10)).pack(side="left", padx=10)
        AppleButton(r3, "生成诊断报告", command=self.make_report,
                    style="secondary", width=130, height=34, font=F(10)).pack(side="left")
        AppleButton(r3, "生成恢复脚本", command=self.make_recovery,
                    style="secondary", width=130, height=34, font=F(10)).pack(side="left", padx=10)

        box4 = self._card(p, "电源动作")
        r4 = tk.Frame(box4, bg=THEME["card"])
        r4.pack(fill="x")
        AppleButton(r4, "重启", command=self.on_restart, style="secondary",
                    width=90, height=34, font=F(10)).pack(side="left")
        AppleButton(r4, "注销", command=self.on_logoff, style="secondary",
                    width=90, height=34, font=F(10)).pack(side="left", padx=10)
        AppleButton(r4, "睡眠", command=lambda: self._run(self._sleep),
                    style="secondary", width=90, height=34, font=F(10)).pack(side="left")
        AppleButton(r4, "关机", command=self.on_shutdown, style="secondary",
                    width=90, height=34, font=F(10)).pack(side="left", padx=10)
        AppleButton(r4, "电源设置", command=lambda: open_uri("ms-settings:powersleep"),
                    style="secondary", width=100, height=34, font=F(10)).pack(side="left")

        box5 = self._card(p, "关于")
        tk.Label(box5,
                 text="%s v%s · MIT 开源协议\n"
                      "原理：通过 Windows 即插即用接口启用/禁用 NVIDIA 显示适配器；\n"
                      "应用分配：写入 Windows 原生的「图形性能首选项」"
                      "(HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences)。\n"
                      "若出现黑屏，可重启进入安全模式，或用「恢复显卡」脚本重新启用。"
                 % (APP_NAME, APP_VERSION),
                 bg=THEME["card"], fg=THEME["text2"], font=F(10),
                 justify="left").pack(anchor="w")

        self.auto_power_var.set(autostart_enabled())

    # ---------------------------------------------------------- 日志

    def log(self, msg: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        text = "[%s] %s\n" % (ts, msg)

        def _write():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", text, level)
            self.logbox.see("end")
            self.logbox.configure(state="disabled")

        self.root.after(0, _write)
        try:
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (datetime.now().isoformat(timespec="seconds"), msg))
        except Exception:  # noqa: BLE001
            pass

    def clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    # ---------------------------------------------------------- 刷新

    def refresh(self, initial=False):
        def work():
            gpus = scan_gpus()
            self.root.after(0, lambda: self._render_gpus(gpus, initial))

        threading.Thread(target=work, daemon=True).start()

    def _render_gpus(self, gpus, initial):
        self.gpus = gpus
        for item in self.tree.get_children():
            self.tree.delete(item)
        order = sorted(gpus, key=lambda g: (g.kind != "独显", g.vendor != "NVIDIA"))
        for g in order:
            self.tree.insert("", "end", iid=g.instance_id,
                             values=(g.name, g.vendor, g.kind, g.status_text,
                                     g.driver or "-", g.vram_text),
                             tags=("disabled",) if not g.enabled else ())

        d = pick_discrete(gpus)
        i = pick_integrated(gpus)
        if d and any(g.enabled for g in d):
            self.mode_label.configure(text="⚡ 高性能模式", fg=THEME["blue"])
            desc = "独显 %s 正在工作，适合游戏、渲染与 AI 计算，功耗较高。" \
                   % "、".join(g.name for g in d)
            self.seg.select(1)
        elif d:
            self.mode_label.configure(text="🍃 节能模式", fg=THEME["green"])
            desc = "NVIDIA 独显已关闭，系统由集显承担显示输出，续航更长、发热更低。"
            self.seg.select(0)
        else:
            self.mode_label.configure(text="未检测到 NVIDIA 独显", fg=THEME["text3"])
            desc = "系统里没有找到 NVIDIA 显示适配器，可能已被禁用或不存在。"
        if not i and d:
            desc += "\n⚠ 未检测到集成显卡，禁用独显可能导致黑屏，请谨慎操作。"
        self.mode_desc.configure(text=desc)
        if initial:
            self.log("检测到 %d 个显示适配器：%s" %
                     (len(gpus), "；".join("%s（%s）" % (g.name, g.vendor) for g in gpus) or "无"))

    # ---------------------------------------------------------- 应用列表

    def refresh_apps(self):
        def work():
            procs = enum_processes()
            dgpu = {}
            try:
                for pid, _name, mem in gpu_processes():
                    dgpu[str(pid)] = mem
            except Exception:  # noqa: BLE001
                pass
            rules = gpu_pref_all()
            self.root.after(0, lambda: self._render_apps(procs, dgpu, rules))

        self.log("正在枚举进程 …")
        threading.Thread(target=work, daemon=True).start()

    def _render_apps(self, procs, dgpu, rules):
        kw = self.search_var.get().strip().lower()
        merged = {}
        for pid, path in procs:
            cur = merged.get(path)
            if cur is None:
                merged[path] = {"pids": [pid], "mem": dgpu.get(str(pid))}
            else:
                cur["pids"].append(pid)
                if dgpu.get(str(pid)):
                    cur["mem"] = dgpu.get(str(pid))

        rows = []
        for path, info in merged.items():
            name = os.path.basename(path) or path
            pref = rules.get(path, 0)
            mem = ""
            for p in info["pids"]:
                if dgpu.get(str(p)):
                    mem = dgpu[str(p)]
                    break
            rows.append((name.lower(), path, name, pref, mem, info["pids"]))

        rows.sort(key=lambda r: (not r[4], not (r[3] in (1, 2)), r[0]))
        if kw:
            rows = [r for r in rows if kw in r[0] or kw in str(r[1]).lower()]

        self.proc_rows = rows
        for it in self.apptree.get_children():
            self.apptree.delete(it)
        for _key, path, name, pref, mem, pids in rows:
            tag = "dgpu" if mem else ("igpu" if pref == 1 else "")
            self.apptree.insert("", "end", iid=path,
                                values=(name, PREF_LABELS.get(pref, "默认"),
                                        mem or "—", path), tags=(tag,))
        self.refresh_rules()
        self.log("已加载 %d 个进程，%d 条 GPU 分配规则。" % (len(rows), len(rules)))

    def refresh_rules(self):
        rules = gpu_pref_all()
        for it in self.ruletree.get_children():
            self.ruletree.delete(it)
        for path, pref in sorted(rules.items(), key=lambda kv: os.path.basename(str(kv[0])).lower()):
            self.ruletree.insert("", "end", iid=path,
                                 values=(os.path.basename(path) + "  —  " + path,
                                         PREF_LABELS.get(pref, "默认")))

    def _selected_app(self):
        sel = self.apptree.selection()
        if not sel:
            return None
        path = sel[0]
        for r in self.proc_rows:
            if r[1] == path:
                return r
        return None

    def set_app_pref(self, mode):
        row = self._selected_app()
        if not row:
            messagebox.showinfo("提示", "请先在上方列表里选择一个程序。")
            return
        path, name = row[1], row[2]
        ok, msg = gpu_pref_set(path, mode)
        if ok:
            self.log("已把「%s」的 GPU 分配设为：%s" % (name, PREF_LABELS[mode]), "ok")
            messagebox.showinfo(
                "已保存",
                "「%s」\n\nGPU 分配：%s\n\n⚠ 需要完全退出并重新打开该程序后才会生效。"
                % (name, PREF_LABELS[mode]))
            self.refresh_apps()
        else:
            self.log("设置失败：%s" % msg, "error")
            messagebox.showerror("失败", "写入注册表失败：\n%s" % msg)

    def add_app(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择程序", filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if not path:
            return
        mode = messagebox.askyesnocancel(
            "选择 GPU",
            "为「%s」选择显卡：\n\n是 = 高性能（独显）\n否 = 节能（集显）\n取消 = 放弃"
            % os.path.basename(path))
        if mode is None:
            return
        ok, msg = gpu_pref_set(path, 2 if mode else 1)
        self.log("手动添加规则「%s」→ %s：%s"
                 % (os.path.basename(path), PREF_LABELS[2 if mode else 1], "成功" if ok else msg),
                 "ok" if ok else "error")
        self.refresh_rules()

    def remove_rule(self):
        sel = self.ruletree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一条规则。")
            return
        path = sel[0]
        ok, msg = gpu_pref_remove(path)
        self.log("删除规则「%s」：%s" % (os.path.basename(path), "成功" if ok else msg),
                 "ok" if ok else "error")
        self.refresh_rules()

    def kill_app(self):
        row = self._selected_app()
        if not row:
            messagebox.showinfo("提示", "请先选择一个程序。")
            return
        name = row[2]
        pids = row[5]
        if not messagebox.askyesno("结束进程",
                                   "确定结束「%s」（%d 个进程）吗？\n未保存的数据会丢失。"
                                   % (name, len(pids))):
            return
        ok = 0
        for pid in pids:
            rc, out, err = run_cmd(["taskkill.exe", "/PID", str(pid), "/F"], timeout=30)
            if rc == 0:
                ok += 1
            self.log("结束 PID %s：%s" % (pid, (out or err or "").strip()[:100]))
        self.log("已结束 %d/%d 个进程。" % (ok, len(pids)), "ok" if ok else "error")
        self.refresh_apps()

    # ---------------------------------------------------------- 线程执行器

    def _run(self, fn):
        if self.busy:
            messagebox.showinfo("提示", "正在执行其他操作，请稍候。")
            return
        self.busy = True

        def work():
            try:
                fn()
            except Exception:  # noqa: BLE001
                self.log("内部错误: " + traceback.format_exc(limit=3), "error")
            finally:
                self.busy = False
                self.root.after(0, self.refresh)

        threading.Thread(target=work, daemon=True).start()

    def _ensure_admin(self) -> bool:
        if is_admin():
            return True
        if messagebox.askyesno("需要管理员权限",
                               "启用/禁用显卡必须拥有管理员权限。\n\n"
                               "是否立即以管理员身份重新启动本程序？\n"
                               "（会弹出一次 UAC 确认窗口）"):
            self.log("正在以管理员身份重新启动 …")
            if relaunch_as_admin():
                self.root.destroy()
                return False
            messagebox.showerror("失败", "无法提升权限，请手动右键 exe 选择「以管理员身份运行」。")
        return False

    def on_elevate(self):
        if relaunch_as_admin():
            self.root.destroy()
        else:
            messagebox.showerror("失败", "无法以管理员身份启动。")

    # ---------------------------------------------------------- 主要动作

    def on_power_saving(self):
        if not self._ensure_admin():
            return
        d = pick_discrete(self.gpus)
        if not d:
            messagebox.showinfo("提示", "未检测到 NVIDIA 独显。")
            return
        names = "\n".join("• " + g.name for g in d)
        warn = ""
        if not pick_integrated(self.gpus):
            warn = "\n\n⚠ 未检测到集成显卡，禁用独显后可能黑屏！请确认显示器接在集显输出上。"
        if not messagebox.askyesno(
                "切换到节能模式",
                "将禁用以下独立显卡：\n%s\n\n之后所有程序改用集成显卡运行，"
                "续航更长、发热更低。\n部分正在使用独显的程序可能需要重启后生效。%s\n\n是否继续？"
                % (names, warn)):
            self._sync_segment()
            return
        self._run(self._do_power_saving)

    def _sync_segment(self):
        d = pick_discrete(self.gpus)
        self.seg.select(1 if (d and any(g.enabled for g in d)) else 0)

    def _do_power_saving(self):
        if not is_admin():
            self.log("未获取管理员权限，已取消。", "error")
            return
        if self.make_restore.get():
            create_restore_point(emit=self.log)
        all_ok = True
        for g in pick_discrete(self.gpus):
            if not g.enabled:
                self.log("%s 已经是禁用状态，跳过。" % g.name)
                continue
            ok, _ = set_device_state(g, False, emit=self.log)
            all_ok = all_ok and ok
        if all_ok:
            self.log("已切换到节能模式：独显已关闭，系统使用集显。", "ok")
            p = write_recovery_script()
            if p:
                self.log("应急恢复脚本已更新：%s" % p)
            if self.auto_restart.get():
                self.log("5 秒后自动重启 …", "warn")
                do_restart(5)
        else:
            self.log("部分设备未能禁用，可先到「应用分配」结束占用独显的程序再试。", "error")
        self.root.after(0, self._sync_segment)

    def on_performance(self):
        if not self._ensure_admin():
            return
        d = pick_discrete(self.gpus)
        if not d:
            messagebox.showinfo("提示", "未检测到 NVIDIA 独显。")
            return
        if all(g.enabled for g in d):
            return
        self._run(self._do_performance)

    def _do_performance(self):
        if not is_admin():
            self.log("未获取管理员权限，已取消。", "error")
            return
        if self.make_restore.get():
            create_restore_point(emit=self.log)
        all_ok = True
        for g in pick_discrete(self.gpus):
            if g.enabled:
                continue
            ok, _ = set_device_state(g, True, emit=self.log)
            all_ok = all_ok and ok
        if all_ok:
            self.log("已切换到高性能模式：独显已启用。", "ok")
            if self.auto_restart.get():
                self.log("5 秒后自动重启 …", "warn")
                do_restart(5)
        else:
            self.log("启用失败，请检查设备管理器。", "error")
        self.root.after(0, self._sync_segment)

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        for g in self.gpus:
            if g.instance_id == iid:
                return g
        return None

    def _on_tree_menu(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        dev = self._selected()
        if not dev:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="启用该设备", command=self._enable_selected)
        menu.add_command(label="禁用该设备", command=self._disable_selected)
        menu.add_separator()
        menu.add_command(label="复制设备实例 ID",
                         command=lambda: (self.root.clipboard_clear(),
                                          self.root.clipboard_append(dev.instance_id)))
        menu.tk_popup(event.x_root, event.y_root)

    def _enable_selected(self):
        g = self._selected()
        if not g:
            messagebox.showinfo("提示", "请先选择一个设备。")
            return
        if not self._ensure_admin():
            return
        self._run(lambda: set_device_state(g, True, emit=self.log))

    def _disable_selected(self):
        g = self._selected()
        if not g:
            messagebox.showinfo("提示", "请先选择一个设备。")
            return
        if not self._ensure_admin():
            return
        if not messagebox.askyesno("确认", "确定禁用「%s」吗？" % g.name):
            return
        self._run(lambda: set_device_state(g, False, emit=self.log))

    def on_restart(self):
        if messagebox.askyesno("重启", "确定立即重启电脑吗？\n（重启可彻底释放被占用的独显）"):
            self.log("正在重启 …", "warn")
            do_restart(3)

    def on_shutdown(self):
        if messagebox.askyesno("关机", "确定立即关机吗？"):
            self.log("正在关机 …", "warn")
            do_shutdown(3)

    def on_logoff(self):
        if messagebox.askyesno("注销", "确定注销当前用户吗？\n所有程序会被关闭，独显占用将被释放。"):
            self.log("正在注销 …", "warn")
            do_logoff()

    def _sleep(self):
        self.log("进入睡眠 …", "warn")
        do_sleep()

    def _open_nv(self):
        ok, _p = open_nvidia_panel()
        self.log("打开 NVIDIA 控制面板：%s" % ("成功" if ok else "失败（可能未安装驱动）"),
                 "ok" if ok else "warn")

    def _restore_point(self):
        ok, msg = create_restore_point(emit=self.log)
        messagebox.showinfo("系统还原点", msg if ok else "失败：" + msg)

    # ---------------------------------------------------------- 监控

    def _monitor_loop(self):
        while not self.stop_monitor.is_set():
            stats = gpu_stats()
            self.root.after(0, lambda s=stats: self._render_stats(s))
            time.sleep(2)

    def _render_stats(self, s):
        if not s:
            for k in self.stat_vars:
                self.stat_vars[k].set("—")
            self.smi_note.configure(text="独显当前不可用（已禁用）或 nvidia-smi 未响应。")
            return
        self.smi_note.configure(text="")
        self.stat_vars["name"].set(s["name"])
        self.stat_vars["temp"].set("%s °C" % (s["temp"] if s["temp"] is not None else "N/A"))
        self.stat_vars["util"].set("%s %%" % (s["util"] if s["util"] is not None else "N/A"))
        if s["mem_used"] is not None and s["mem_total"]:
            self.stat_vars["mem"].set("%.0f / %.0f MiB" % (s["mem_used"], s["mem_total"]))
        else:
            self.stat_vars["mem"].set("N/A")
        self.stat_vars["power"].set("%s W" % (s["power"] if s["power"] is not None else "N/A"))
        self.stat_vars["plimit"].set(
            "%s W" % s["power_limit"] if s["power_limit"] is not None else "不支持/未设置")

    # ---------------------------------------------------------- 功耗

    def read_power(self, silent=False):
        lim = power_limits()
        if not lim:
            if not silent:
                messagebox.showinfo("功耗", "无法读取功耗信息（独显已禁用或不支持）。")
            return
        mn, mx, cur = lim
        if cur is not None:
            self.power_limit_var.set("%.0f" % cur)
        if not silent:
            messagebox.showinfo("功耗信息",
                                "当前功耗上限：%s W\n可调范围：%s ~ %s W" % (cur or "-", mn or "-", mx or "-"))
        else:
            self.log("功耗上限：%s W（可调 %s ~ %s W）" % (cur or "-", mn or "-", mx or "-"))

    def apply_power(self):
        try:
            watts = float(self.power_limit_var.get().strip())
        except Exception:  # noqa: BLE001
            messagebox.showwarning("输入错误", "请输入有效的功率数值。")
            return
        if not is_admin():
            messagebox.showwarning("权限不足", "设置功耗上限需要管理员权限。")
            return
        if not messagebox.askyesno("确认", "将功耗上限设置为 %.0f W？\n过低的功耗会明显降低性能。" % watts):
            return
        self._run(lambda: set_power_limit(watts, emit=self.log))

    # ---------------------------------------------------------- 其他工具

    def toggle_autostart(self):
        want = self.auto_power_var.get()
        if want and not is_admin():
            self.auto_power_var.set(False)
            messagebox.showwarning("权限不足", "创建计划任务需要管理员权限。")
            return
        ok, msg = set_autostart(want, "power", emit=self.log)
        if not ok:
            self.auto_power_var.set(not want)
            messagebox.showwarning("失败", msg)

    def clear_cache(self):
        total, files = shader_cache_size()
        if total <= 0:
            messagebox.showinfo("着色器缓存", "没有可清理的缓存。")
            return
        dirs = "\n".join(shader_cache_dirs())
        if not messagebox.askyesno(
                "清理着色器缓存",
                "将删除以下目录中约 %s 的 %d 个缓存文件：\n\n%s\n\n"
                "这些是显卡着色器缓存，删除后会自动重建（首次运行游戏可能变慢）。\n\n是否继续？"
                % (fmt_bytes(total), files, dirs)):
            return
        n, size = clear_shader_cache(emit=self.log)
        messagebox.showinfo("完成", "已清理 %d 项，释放约 %s。" % (n, fmt_bytes(size)))

    def make_report(self):
        path = os.path.join(app_dir(), "诊断报告_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        lines = ["%s 诊断报告  %s" % (APP_NAME, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                 "=" * 60, "", "[显示适配器]"]
        for g in self.gpus:
            lines.append("  %s | %s | %s | %s | 驱动 %s | 显存 %s | %s"
                         % (g.name, g.vendor, g.kind, g.status_text,
                            g.driver or "-", g.vram_text, g.instance_id))
        lines += ["", "[GPU 分配规则]"]
        for k, v in gpu_pref_all().items():
            lines.append("  %s = %s" % (k, PREF_LABELS.get(v, v)))
        lines += ["", "[nvidia-smi]"]
        ok, out = smi(["-q"], timeout=60)
        lines.append(out if ok else "不可用：" + (out or ""))
        lines += ["", "[系统]"]
        rc, out, err = run_cmd(["systeminfo"], timeout=120)
        lines.append(out[:4000] if rc == 0 else (err or "读取失败"))
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失败", str(e))
            return
        self.log("诊断报告已生成：%s" % path, "ok")
        open_uri(path)

    def make_recovery(self):
        p = write_recovery_script()
        if p:
            self.log("恢复脚本已生成：%s" % p, "ok")
            messagebox.showinfo("完成", "已生成：\n%s\n\n若切换后黑屏，双击运行即可重新启用所有显卡。" % p)
        else:
            messagebox.showerror("失败", "无法写入脚本，请检查目录权限。")



# ============================================================ 命令行模式

def cli_main(argv):
    import argparse

    p = argparse.ArgumentParser(prog=APP_NAME, description="GPU 切换助手 命令行模式")
    p.add_argument("--list", action="store_true", help="列出显示适配器")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.add_argument("--apply", choices=["power", "performance"],
                   help="power=节能模式(禁用独显) / performance=高性能模式(启用独显)")
    p.add_argument("--silent", action="store_true", help="不弹窗")
    p.add_argument("--restart", action="store_true", help="应用后重启")
    a = p.parse_args(argv)

    gpus = scan_gpus()
    if a.json or a.list:
        data = [{"name": g.name, "vendor": g.vendor, "kind": g.kind,
                 "status": g.status_text, "instance_id": g.instance_id,
                 "driver": g.driver} for g in gpus]
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if not a.apply:
            return 0

    if a.apply:
        if not is_admin():
            print("需要管理员权限。", file=sys.stderr)
            return 2
        targets = pick_discrete(gpus)
        if not targets:
            print("未检测到 NVIDIA 独显。", file=sys.stderr)
            return 3
        enable = a.apply == "performance"
        ok_all = True
        for g in targets:
            if g.enabled == enable:
                continue
            ok, msg = set_device_state(g, enable)
            print("%s %s: %s" % ("启用" if enable else "禁用", g.name, "成功" if ok else "失败 " + msg))
            ok_all = ok_all and ok
        if a.restart:
            do_restart(5)
        return 0 if ok_all else 1

    p.print_help()
    return 0


def main():
    # 高分屏适配
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass

    args = sys.argv[1:]
    # 只要出现 -- 开头的参数，就进入命令行模式（不启动界面）
    if any(a.startswith("--") for a in args):
        sys.exit(cli_main(args))

    root = tk.Tk()

    def on_error(exc, value, tb):
        msg = "".join(traceback.format_exception(exc, value, tb))
        try:
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write("[CRASH] " + msg + "\n")
        except Exception:  # noqa: BLE001
            pass
        messagebox.showerror("发生错误", msg[-1500:])
        sys.__excepthook__(exc, value, tb)

    root.report_callback_exception = on_error
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
