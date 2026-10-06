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


# ============================================================ GUI

PALETTE = {
    "bg": "#f4f6f9",
    "card": "#ffffff",
    "text": "#1f2328",
    "muted": "#656d76",
    "border": "#d8dee4",
    "accent": "#2f6feb",
    "green": "#1a7f37",
    "orange": "#bc4c00",
    "red": "#cf222e",
}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.gpus: list[GpuDevice] = []
        self.busy = False
        self.stop_monitor = threading.Event()
        self.auto_restart = tk.BooleanVar(value=False)
        self.make_restore = tk.BooleanVar(value=False)
        self.auto_power_var = tk.BooleanVar(value=False)
        self.power_limit_var = tk.StringVar(value="")

        root.title("%s v%s" % (APP_NAME, APP_VERSION))
        root.geometry("940x760")
        root.minsize(860, 660)
        root.configure(bg=PALETTE["bg"])

        self._style()
        self._build_ui()

        self.log("程序已启动。目录: %s" % app_dir())
        if is_admin():
            self.log("当前以管理员权限运行。", "ok")
            self.admin_label.config(text="● 管理员", foreground=PALETTE["green"])
        else:
            self.log("当前为普通权限：切换显卡需要管理员权限。", "warn")
            self.admin_label.config(text="● 普通权限", foreground=PALETTE["orange"])
            self.elevate_btn.config(state="normal")

        self.refresh(initial=True)
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    # ---------------- 样式

    def _style(self):
        s = ttk.Style()
        if "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("TFrame", background=PALETTE["bg"])
        s.configure("Card.TFrame", background=PALETTE["card"], relief="solid", borderwidth=1)
        s.configure("TLabel", background=PALETTE["bg"], foreground=PALETTE["text"],
                    font=("Microsoft YaHei UI", 10))
        s.configure("Card.TLabel", background=PALETTE["card"])
        s.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        s.configure("Sub.TLabel", foreground=PALETTE["muted"],
                    font=("Microsoft YaHei UI", 9))
        s.configure("Mode.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        s.configure("TNotebook", background=PALETTE["bg"])
        s.configure("TNotebook.Tab", padding=(14, 6), font=("Microsoft YaHei UI", 10))
        s.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=26)
        s.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        s.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
        s.configure("Action.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=8)
        s.configure("TCheckbutton", background=PALETTE["card"])

    # ---------------- 界面

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(16, 12, 16, 6))
        top.pack(fill="x")
        ttk.Label(top, text="GPU 切换助手", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  NVIDIA 独显 / 集显 一键切换", style="Sub.TLabel").pack(side="left", pady=(6, 0))
        self.admin_label = ttk.Label(top, text="●", font=("Microsoft YaHei UI", 9, "bold"))
        self.admin_label.pack(side="right", padx=8)
        self.elevate_btn = ttk.Button(top, text="获取管理员权限", state="disabled",
                                      command=self.on_elevate)
        self.elevate_btn.pack(side="right")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=(4, 8))

        self._tab_switch(nb)
        self._tab_monitor(nb)
        self._tab_process(nb)
        self._tab_tools(nb)

        bottom = ttk.Frame(self.root, padding=(14, 0, 14, 10))
        bottom.pack(fill="both", expand=False)
        head = ttk.Frame(bottom)
        head.pack(fill="x")
        ttk.Label(head, text="操作日志", font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        ttk.Button(head, text="清空", width=6, command=self.clear_log).pack(side="right")
        self.logbox = scrolledtext.ScrolledText(
            bottom, height=9, font=("Consolas", 9), relief="solid", bd=1,
            bg="#ffffff", fg=PALETTE["text"], wrap="word"
        )
        self.logbox.pack(fill="both", expand=True, pady=(4, 0))
        for tag, color in (("ok", PALETTE["green"]), ("warn", PALETTE["orange"]),
                           ("error", PALETTE["red"]), ("info", PALETTE["text"])):
            self.logbox.tag_config(tag, foreground=color)
        self.logbox.configure(state="disabled")

    def _card(self, parent, title=""):
        outer = ttk.Frame(parent)
        outer.pack(fill="x", padx=12, pady=8)
        if title:
            ttk.Label(outer, text=title, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        inner = ttk.Frame(outer, style="Card.TFrame", padding=12)
        inner.pack(fill="x", pady=(4, 0))
        return inner

    def _tab_switch(self, nb):
        tab = ttk.Frame(nb, padding=4)
        nb.add(tab, text="  切换  ")

        # 模式卡片
        mode = self._card(tab, "当前模式")
        self.mode_label = ttk.Label(mode, text="检测中 ...", style="Mode.TLabel")
        self.mode_label.pack(anchor="w")
        self.mode_desc = ttk.Label(mode, text="", style="Card.TLabel",
                                   foreground=PALETTE["muted"], wraplength=780,
                                   font=("Microsoft YaHei UI", 9))
        self.mode_desc.pack(anchor="w", pady=(2, 0))

        # 显卡列表
        box = self._card(tab, "显示适配器")
        cols = ("name", "vendor", "kind", "status", "driver", "vram")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=5)
        heads = {"name": "设备名称", "vendor": "厂商", "kind": "类型",
                 "status": "状态", "driver": "驱动版本", "vram": "显存"}
        widths = {"name": 300, "vendor": 80, "kind": 60, "status": 70, "driver": 110, "vram": 70}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="x")
        self.tree.tag_configure("disabled", foreground=PALETTE["muted"])
        self.tree.bind("<Button-3>", self._on_tree_menu)

        btnrow = ttk.Frame(box)
        btnrow.pack(fill="x", pady=(8, 0))
        ttk.Button(btnrow, text="刷新状态", command=lambda: self.refresh(False)).pack(side="left")
        ttk.Button(btnrow, text="启用选中设备", command=self._enable_selected).pack(side="left", padx=6)
        ttk.Button(btnrow, text="禁用选中设备", command=self._disable_selected).pack(side="left")

        # 一键操作
        acts = self._card(tab, "一键操作")
        grid = ttk.Frame(acts, style="Card.TFrame")
        grid.pack(fill="x")
        self.btn_power = ttk.Button(grid, text="🍃  节能模式（关闭独显，用集显）",
                                    style="Action.TButton", command=self.on_power_saving)
        self.btn_power.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=3)
        self.btn_perf = ttk.Button(grid, text="⚡  高性能模式（启用独显）",
                                   style="Action.TButton", command=self.on_performance)
        self.btn_perf.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=3)
        self.btn_restart = ttk.Button(grid, text="🔄  重启电脑（释放独显）",
                                      command=self.on_restart)
        self.btn_restart.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=3)
        self.btn_logoff = ttk.Button(grid, text="🚪  注销（温和释放独显）",
                                     command=self.on_logoff)
        self.btn_logoff.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=3)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        opts = ttk.Frame(acts, style="Card.TFrame")
        opts.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(opts, text="切换后自动重启（立即生效）",
                        variable=self.auto_restart, style="TCheckbutton").pack(side="left")
        ttk.Checkbutton(opts, text="操作前创建系统还原点",
                        variable=self.make_restore, style="TCheckbutton").pack(side="left", padx=18)

    def _tab_monitor(self, nb):
        tab = ttk.Frame(nb, padding=4)
        nb.add(tab, text="  监控  ")
        box = self._card(tab, "独显实时状态（每 2 秒刷新）")
        self.stat_vars = {}
        rows = [("显卡", "name"), ("温度", "temp"), ("利用率", "util"),
                ("显存占用", "mem"), ("功耗", "power"), ("功耗上限", "plimit")]
        for i, (label, key) in enumerate(rows):
            ttk.Label(box, text=label + "：", style="Card.TLabel",
                      font=("Microsoft YaHei UI", 10)).grid(row=i, column=0, sticky="w", pady=4)
            v = tk.StringVar(value="-")
            self.stat_vars[key] = v
            ttk.Label(box, textvariable=v, style="Card.TLabel",
                      font=("Consolas", 11, "bold")).grid(row=i, column=1, sticky="w", pady=4)
        self.smi_note = ttk.Label(box, text="", style="Card.TLabel",
                                  foreground=PALETTE["muted"],
                                  font=("Microsoft YaHei UI", 9))
        self.smi_note.grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(8, 0))
        if not find_nvidia_smi():
            self.smi_note.config(text="未找到 nvidia-smi，监控不可用（独显被禁用时也会不可用）。")

        tools = self._card(tab, "其他系统入口")
        f = ttk.Frame(tools, style="Card.TFrame")
        f.pack(fill="x")
        ttk.Button(f, text="Windows 图形性能首选项",
                   command=lambda: open_uri("ms-settings:display-advancedgraphics")).pack(side="left")
        ttk.Button(f, text="NVIDIA 控制面板",
                   command=lambda: self._run(self._open_nv)).pack(side="left", padx=6)
        ttk.Button(f, text="设备管理器",
                   command=lambda: open_uri("devmgmt.msc")).pack(side="left")

    def _tab_process(self, nb):
        tab = ttk.Frame(nb, padding=4)
        nb.add(tab, text="  占用进程  ")
        box = self._card(tab, "正在占用独显的进程")
        cols = ("pid", "name", "mem")
        self.ptree = ttk.Treeview(box, columns=cols, show="headings", height=9)
        for c, t, w in (("pid", "PID", 90), ("name", "进程", 320), ("mem", "显存占用", 120)):
            self.ptree.heading(c, text=t)
            self.ptree.column(c, width=w, anchor="w")
        self.ptree.pack(fill="x")
        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="刷新列表", command=self.refresh_processes).pack(side="left")
        ttk.Button(row, text="结束选中进程", command=self.kill_process).pack(side="left", padx=6)
        ttk.Label(row, text="结束进程可释放被占用的独显，从而切换到集显。",
                  style="Card.TLabel", foreground=PALETTE["muted"],
                  font=("Microsoft YaHei UI", 9)).pack(side="left", padx=6)
        self.refresh_processes()

    def _tab_tools(self, nb):
        tab = ttk.Frame(nb, padding=4)
        nb.add(tab, text="  更多工具  ")

        box = self._card(tab, "省电与功耗")
        r = ttk.Frame(box, style="Card.TFrame")
        r.pack(fill="x")
        ttk.Label(r, text="功耗上限 (W)：", style="Card.TLabel").pack(side="left")
        self.plim_spin = ttk.Spinbox(r, from_=10, to=400, width=8,
                                     textvariable=self.power_limit_var)
        self.plim_spin.pack(side="left", padx=4)
        ttk.Button(r, text="读取当前", command=self.read_power).pack(side="left", padx=6)
        ttk.Button(r, text="应用", command=self.apply_power).pack(side="left")
        ttk.Label(r, text="需管理员权限，笔记本机型可能不支持。", style="Card.TLabel",
                  foreground=PALETTE["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left", padx=8)

        box2 = self._card(tab, "开机自动化")
        r2 = ttk.Frame(box2, style="Card.TFrame")
        r2.pack(fill="x")
        ttk.Checkbutton(r2, text="登录后自动应用节能模式（关闭独显）",
                        variable=self.auto_power_var, style="TCheckbutton",
                        command=self.toggle_autostart).pack(side="left")

        box3 = self._card(tab, "维护与应急")
        r3 = ttk.Frame(box3, style="Card.TFrame")
        r3.pack(fill="x")
        ttk.Button(r3, text="创建系统还原点",
                   command=lambda: self._run(self._restore_point)).pack(side="left")
        ttk.Button(r3, text="清理着色器缓存",
                   command=self.clear_cache).pack(side="left", padx=6)
        ttk.Button(r3, text="生成诊断报告",
                   command=self.make_report).pack(side="left", padx=6)
        ttk.Button(r3, text="生成恢复脚本",
                   command=self.make_recovery).pack(side="left")

        box4 = self._card(tab, "电源动作")
        r4 = ttk.Frame(box4, style="Card.TFrame")
        r4.pack(fill="x")
        ttk.Button(r4, text="重启", command=self.on_restart).pack(side="left")
        ttk.Button(r4, text="注销", command=self.on_logoff).pack(side="left", padx=6)
        ttk.Button(r4, text="睡眠", command=lambda: self._run(self._sleep)).pack(side="left")
        ttk.Button(r4, text="关机", command=self.on_shutdown).pack(side="left", padx=6)
        ttk.Button(r4, text="打开电源设置",
                   command=lambda: open_uri("ms-settings:powersleep")).pack(side="left", padx=6)

        box5 = self._card(tab, "关于")
        ttk.Label(box5,
                  text="%s v%s  ·  MIT 开源协议\n"
                       "原理：通过 Windows 即插即用接口启用/禁用 NVIDIA 显示适配器。\n"
                       "切换显卡不会损坏硬件；若出现黑屏，可重启进入安全模式，或用「恢复显卡」脚本重新启用。"
                       % (APP_NAME, APP_VERSION),
                  style="Card.TLabel", foreground=PALETTE["muted"],
                  font=("Microsoft YaHei UI", 9), justify="left").pack(anchor="w")

        self.auto_power_var.set(autostart_enabled())

    # ---------------- 日志

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

    # ---------------- 数据刷新

    def refresh(self, initial=False):
        def work():
            gpus = scan_gpus(emit=None)
            self.root.after(0, lambda: self._render_gpus(gpus, initial))

        threading.Thread(target=work, daemon=True).start()

    def _render_gpus(self, gpus, initial):
        self.gpus = gpus
        for item in self.tree.get_children():
            self.tree.delete(item)
        for g in gpus:
            tags = ("disabled",) if not g.enabled else ()
            self.tree.insert("", "end", iid=g.instance_id,
                             values=(g.name, g.vendor, g.kind, g.status_text,
                                     g.driver or "-", g.vram_text),
                             tags=tags)

        d = pick_discrete(gpus)
        i = pick_integrated(gpus)
        if d and any(g.enabled for g in d):
            self.mode_label.config(text="⚡ 高性能模式（独显工作中）", foreground=PALETTE["accent"])
            names = "、".join(g.name for g in d)
            self.mode_desc.config(
                text="独显：%s。适合游戏、渲染、AI 计算；功耗较高。\n"
                     "切换到节能模式会禁用独显，屏幕改由集显（如存在）输出。" % names)
        elif d:
            self.mode_label.config(text="🍃 节能模式（独显已关闭）", foreground=PALETTE["green"])
            self.mode_desc.config(
                text="所有 NVIDIA 独显均已被禁用，系统由集显承担显示输出，续航更长、发热更低。")
        else:
            self.mode_label.config(text="未检测到 NVIDIA 独显", foreground=PALETTE["muted"])
            self.mode_desc.config(text="未在系统里找到 NVIDIA 显示适配器，可能已被禁用或不存在。")

        if not i and d:
            self.mode_desc.config(
                text=self.mode_desc.cget("text") + "\n⚠ 未检测到集成显卡，禁用独显可能导致黑屏，请谨慎操作。")
        if initial:
            self.log("检测到 %d 个显示适配器：%s" %
                     (len(gpus), "；".join("%s(%s)" % (g.name, g.vendor) for g in gpus) or "无"))
            self.read_power(silent=True)

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

    # ---------------- 线程执行器

    def _run(self, fn):
        if self.busy:
            messagebox.showinfo("提示", "正在执行其他操作，请稍候。")
            return
        self.busy = True
        self._set_buttons(False)

        def work():
            try:
                fn()
            except Exception:  # noqa: BLE001
                self.log("内部错误: " + traceback.format_exc(limit=3), "error")
            finally:
                self.busy = False
                self.root.after(0, lambda: self._set_buttons(True))
                self.root.after(0, self.refresh)

        threading.Thread(target=work, daemon=True).start()

    def _set_buttons(self, on: bool):
        state = "normal" if on else "disabled"
        for b in (self.btn_power, self.btn_perf, self.btn_restart, self.btn_logoff):
            b.config(state=state)

    # ---------------- 主要动作

    def on_elevate(self):
        self.log("正在请求管理员权限 ...")
        if not relaunch_as_admin():
            messagebox.showerror("失败", "无法以提升的权限启动程序。")
        else:
            self.root.destroy()

    def _ensure_admin(self) -> bool:
        """在主线程中检查权限，不足时询问是否自动提权重启"""
        if is_admin():
            return True
        if messagebox.askyesno(
                "需要管理员权限",
                "启用/禁用显卡必须拥有管理员权限。\n\n"
                "是否立即以管理员身份重新启动本程序？\n"
                "（会弹出一次 UAC 确认窗口）"):
            self.log("正在以管理员身份重新启动 ...")
            if relaunch_as_admin():
                self.root.destroy()
                return False
            messagebox.showerror("失败", "无法提升权限，请手动右键 exe 选择「以管理员身份运行」。")
        return False

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
            warn = "\n\n⚠ 未检测到集成显卡，禁用独显后可能黑屏！请确认你的显示器接在集显输出上。"
        if not messagebox.askyesno(
                "切换到节能模式",
                "将禁用以下独立显卡：\n%s\n\n之后所有程序改用集成显卡运行，"
                "续航更长、发热更低。\n部分正在使用独显的程序可能需要重启后生效。%s\n\n是否继续？"
                % (names, warn)):
            return
        self._run(self._do_power_saving)

    def _do_power_saving(self):
        if not is_admin():
            self.log("未获取管理员权限，已取消。", "error")
            return

        if self.make_restore.get():
            create_restore_point(emit=self.log)

        d = pick_discrete(self.gpus)
        all_ok = True
        for g in d:
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
                self.log("5 秒后自动重启 ...", "warn")
                do_restart(5)
            else:
                messagebox.showinfo("完成",
                                    "已切换为节能模式。\n\n若屏幕无变化或应用异常，"
                                    "建议注销或重启一次以完全释放独显。")
        else:
            self.log("部分设备未能禁用，可能是被占用或系统不允许。可先结束占用进程再试。", "error")

    def on_performance(self):
        if not self._ensure_admin():
            return
        d = pick_discrete(self.gpus)
        if not d:
            messagebox.showinfo("提示", "未检测到 NVIDIA 独显。")
            return
        if all(g.enabled for g in d):
            messagebox.showinfo("提示", "独显已处于启用状态。")
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
                self.log("5 秒后自动重启 ...", "warn")
                do_restart(5)
            else:
                messagebox.showinfo("完成", "独显已启用。\n部分程序可能需要重启后才能识别到独显。")
        else:
            self.log("启用失败，请检查设备管理器。", "error")

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
            self.log("正在重启 ...", "warn")
            do_restart(3)

    def on_shutdown(self):
        if messagebox.askyesno("关机", "确定立即关机吗？"):
            self.log("正在关机 ...", "warn")
            do_shutdown(3)

    def on_logoff(self):
        if messagebox.askyesno("注销", "确定注销当前用户吗？\n所有程序会被关闭，独显占用将被释放。"):
            self.log("正在注销 ...", "warn")
            do_logoff()

    def _sleep(self):
        self.log("进入睡眠 ...", "warn")
        do_sleep()

    def _open_nv(self):
        ok, p = open_nvidia_panel()
        self.log("打开 NVIDIA 控制面板: %s" % ("成功" if ok else "失败（可能未安装驱动）"),
                 "ok" if ok else "warn")

    def _restore_point(self):
        ok, msg = create_restore_point(emit=self.log)
        messagebox.showinfo("系统还原点", msg if ok else "失败：" + msg)

    # ---------------- 监控

    def _monitor_loop(self):
        while not self.stop_monitor.is_set():
            stats = gpu_stats()
            self.root.after(0, lambda s=stats: self._render_stats(s))
            time.sleep(2)

    def _render_stats(self, s):
        if not s:
            for k in self.stat_vars:
                self.stat_vars[k].set("-")
            self.smi_note.config(text="独显当前不可用（已禁用）或 nvidia-smi 未响应。")
            return
        self.smi_note.config(text="")
        self.stat_vars["name"].set(s["name"])
        self.stat_vars["temp"].set("%s °C" % (s["temp"] if s["temp"] is not None else "N/A"))
        self.stat_vars["util"].set("%s %%" % (s["util"] if s["util"] is not None else "N/A"))
        if s["mem_used"] is not None and s["mem_total"]:
            self.stat_vars["mem"].set("%.0f / %.0f MiB (%.0f%%)" %
                                      (s["mem_used"], s["mem_total"],
                                       100.0 * s["mem_used"] / max(s["mem_total"], 1)))
        else:
            self.stat_vars["mem"].set("N/A")
        self.stat_vars["power"].set("%s W" % (s["power"] if s["power"] is not None else "N/A"))
        self.stat_vars["plimit"].set(
            "%s W" % s["power_limit"] if s["power_limit"] is not None else "不支持/未设置")

    # ---------------- 进程

    def refresh_processes(self):
        for i in self.ptree.get_children():
            self.ptree.delete(i)
        procs = gpu_processes()
        if not procs:
            self.ptree.insert("", "end", values=("-", "未检测到占用独显的进程", "-"))
            return
        for pid, name, mem in procs:
            self.ptree.insert("", "end", values=(pid, name, mem))

    def kill_process(self):
        sel = self.ptree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一个进程。")
            return
        vals = self.ptree.item(sel[0], "values")
        pid = vals[0]
        if not str(pid).isdigit():
            return
        if not messagebox.askyesno("结束进程", "确定结束 PID %s（%s）吗？\n未保存的数据会丢失。"
                                   % (pid, vals[1])):
            return
        rc, out, err = run_cmd(["taskkill.exe", "/PID", pid, "/F"], timeout=30)
        self.log("结束进程 %s: %s" % (pid, (out or err or "").strip()[:120]),
                 "ok" if rc == 0 else "error")
        self.refresh_processes()

    # ---------------- 功耗

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
                                "当前功耗上限: %s W\n可调范围: %s ~ %s W" %
                                (cur or "-", mn or "-", mx or "-"))
        else:
            self.log("功耗上限: %s W（可调 %s ~ %s W）" % (cur or "-", mn or "-", mx or "-"))

    def apply_power(self):
        val = self.power_limit_var.get().strip()
        try:
            watts = float(val)
        except Exception:  # noqa: BLE001
            messagebox.showwarning("输入错误", "请输入有效的功率数值。")
            return
        if not is_admin():
            messagebox.showwarning("权限不足", "设置功耗上限需要管理员权限。")
            return
        if not messagebox.askyesno("确认", "将功耗上限设置为 %.0f W？\n"
                                           "过低的功耗会明显降低性能。" % watts):
            return
        self._run(lambda: set_power_limit(watts, emit=self.log))

    # ---------------- 其他工具

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
                 "=" * 60, ""]
        lines.append("[显示适配器]")
        for g in self.gpus:
            lines.append("  %s | %s | %s | %s | 驱动 %s | 显存 %s | %s"
                         % (g.name, g.vendor, g.kind, g.status_text,
                            g.driver or "-", g.vram_text, g.instance_id))
        lines.append("")
        lines.append("[nvidia-smi]")
        ok, out = smi(["-q"], timeout=60)
        lines.append(out if ok else "不可用：" + (out or ""))
        lines.append("")
        lines.append("[系统]")
        rc, out, err = run_cmd(["systeminfo"], timeout=120)
        lines.append(out[:4000] if rc == 0 else (err or "读取失败"))
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失败", str(e))
            return
        self.log("诊断报告已生成: %s" % path, "ok")
        open_uri(path)

    def make_recovery(self):
        p = write_recovery_script()
        if p:
            self.log("恢复脚本已生成: %s" % p, "ok")
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
