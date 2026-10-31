"""E-Mobile 客户端进程管理:发现 / 带调试端口启动。

分发时的最大障碍:同事不会特意带 `--remote-debugging-port` 启动 E-Mobile。
这里负责:
  * 在常见端口里探测已带调试端口运行的实例;
  * 探测不到就自己拉一个(避开用户正在用的无端口实例会被单例锁拦截的问题);
  * 端口冲突时自动顺延,不用手工指定。

⚠ 安装位置差异(2026-09-24 实测):
    开发机:   `C:\\Program Files (x86)\\E-Mobile\\E-Mobile.exe`(7.2.6)
    正式环境: `C:\\软件\\E-Mobile\\E-Mobile\\E-Mobile.exe`(7.1.4)
  ⇉ 传统 Program Files 候选覆盖不到正式环境,故补齐:注册表 Uninstall 项 /
    桌面与开始菜单快捷方式 / 运行中进程镜像路径。

两件配套能力(2026-09-24 增补):
  * **自动兜底重启**(`ensure_port(auto_restart=True)`):用户正常点图标启动的
    实例没带调试端口时,不再要求用户手动"托盘退出",而是关掉它再自己带端口
    拉起来;可用 `EMCLI_NO_RESTART=1` 退回旧行为。
    关闭是**两级**的(2026-10-09 真机实测后收紧,见下):
      1) 优雅:`WM_CLOSE` + `taskkill /PID`(**不带 `/F`**),等
         `restart_wait_s`(默认 15s);
      2) 强制:第一级超时且**未**设 `EMCLI_NO_FORCE` → 打印中文提示后,对
         **按进程名复核后仍属 `E-Mobile.exe`** 的剩余 pid 执行
         `taskkill /F /PID`,再等 `force_wait_s`(默认 10s)。
    实测 E-Mobile 7.2.6 是**托盘应用**(`hideOnClose: true`),**忽略
    WM_CLOSE**,`taskkill /PID`(不带 `/F`)也不足以让它退出 —— 只有第二级
    才能真的关掉它。可用 `EMCLI_NO_FORCE=1` 禁用第二级(完全退回旧行为)。
  * **启动入口持久化**(`configure_launcher()`):给公共桌面快捷方式**追加**
    `--remote-debugging-port=<port>`(先备份,写不动就降级到用户桌面启动器),
    让客户端"以后默认带端口启动"。

⚠ 启动必读(2026-09-30 真机实测):DSH 宿主会向子进程注入
  `ELECTRON_RUN_AS_NODE=1`,E-Mobile 是 Electron 应用,继承该变量后会以
  **Node 模式**运行并立即退出(无参 ExitCode=1、带 `--remote-debugging-port`
  ExitCode=9),`ensure_port()` 便永远等不到调试端口。所以所有拉起客户端的
  `Popen` 都必须用 `clean_child_env()` 摘掉 Electron 相关变量(见
  `ELECTRON_ENV_KEYS`);其余环境变量(代理等)原样保留。
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Windows 上 E-Mobile 的常见安装位置(按优先级)
CANDIDATE_EXES: List[str] = [
    r"C:\Program Files (x86)\E-Mobile\E-Mobile.exe",
    r"C:\Program Files\E-Mobile\E-Mobile.exe",
    r"%LOCALAPPDATA%\Programs\E-Mobile\E-Mobile.exe",
    # 正式环境实测位置(桌面软件员工"软件"盘根目录的安装习惯)
    r"C:\软件\E-Mobile\E-Mobile\E-Mobile.exe",
    r"C:\Software\E-Mobile\E-Mobile\E-Mobile.exe",
    r"D:\软件\E-Mobile\E-Mobile\E-Mobile.exe",
    r"D:\E-Mobile\E-Mobile.exe",
]
#: 依次尝试的调试端口
CANDIDATE_PORTS: List[int] = [9333, 9334, 9335, 9222, 9223]

ENV_EXE = "EMCLI_EMOBILE_EXE"
ENV_PORT = "EMCLI_CDP_PORT"

#: 置 1/true 时禁用"自动兜底重启",退回"提示用户手动退出"的旧行为
ENV_NO_RESTART = "EMCLI_NO_RESTART"
#: 置 1/true/yes/on 时**禁用**"优雅关闭超时后强制结束进程"(即退回旧行为:
#: 绝不强杀,超时只报错)。默认**开启**强制结束 —— 实测 E-Mobile 7.2.6 是
#: 托盘应用(hideOnClose),忽略 WM_CLOSE,不带 /F 的 taskkill 也关不掉它。
ENV_NO_FORCE = "EMCLI_NO_FORCE"
#: 覆盖公共桌面快捷方式路径(测试用)
ENV_LNK = "EMCLI_EMOBILE_LNK"
#: 覆盖"用户桌面启动器"所在目录(测试用,默认 %USERPROFILE%\\Desktop)
ENV_LAUNCHER_DIR = "EMCLI_LAUNCHER_DIR"
#: 覆盖 .lnk 备份目录(测试用,默认 %LOCALAPPDATA%\\emcli\\backup)
ENV_BACKUP_DIR = "EMCLI_BACKUP_DIR"

#: 拉起 E-Mobile 前**必须**从子进程环境里摘掉的 Electron 变量。
#:
#: 真机实测(2026-09-30):DSH 宿主注入的 `ELECTRON_RUN_AS_NODE=1` 会让
#: Electron 以 Node 模式启动并立即退出(无参 ExitCode=1;带
#: `--remote-debugging-port` 时 ExitCode=9),于是 `ensure_port()` 在
#: 20s/45s 内永远等不到端口。对照实验:清掉该变量后同样代码成功
#: (port=9333,45.3s)。其余变量一并清掉,避免同类"以调试/开发模式启动"
#: 的干扰。
ELECTRON_ENV_KEYS: Tuple[str, ...] = (
    "ELECTRON_RUN_AS_NODE",
    "ELECTRON_NO_ATTACH_CONSOLE",
    "ELECTRON_FORCE_IS_PACKAGED",
    "ELECTRON_ENABLE_LOGGING",
    "ELECTRON_IS_DEV",
    "ELECTRON_DEFAULT_ERROR_MODE",
)

#: 公共桌面上的唯一启动入口(实测用户就是点它启动的)
#: 不写死 C: 盘:优先用 PUBLIC 环境变量,回退标准位置。
DEFAULT_PUBLIC_LNK = os.path.join(
    os.environ.get("PUBLIC") or r"C:\Users\Public", "Desktop", "E-Mobile.lnk")
#: 降级时在用户桌面创建的带参数启动器文件名
CLI_LAUNCHER_NAME = "E-Mobile (CLI).lnk"
#: 写进 .lnk Description 的标记 —— 用于判断"这个启动器是本工具创建的"
CLI_LAUNCHER_MARKER = "emcli-launcher"
#: 调试端口参数名
DEBUG_PORT_FLAG = "--remote-debugging-port"
#: 关闭无端口实例的默认等待秒数(第一级:优雅)
DEFAULT_RESTART_WAIT_S = 15.0
#: 强制结束(第二级)之后等待进程退出的默认秒数
DEFAULT_FORCE_WAIT_S = 10.0
#: 启动尝试的候选端口个数上限(端口被占/没就绪时顺延)
MAX_LAUNCH_ATTEMPTS = 2

#: 注册表里反查的 Uninstall 键
_UNINSTALL_HIVES = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


class AppError(RuntimeError):
    """E-Mobile 进程管理失败。"""


# --------------------------------------------------------------------------- #
# exe 发现
# --------------------------------------------------------------------------- #
def discover_exes() -> List[str]:
    """按优先级列出所有可能存在的 E-Mobile.exe 绝对路径。

    顺序:环境变量 → 传统候选 → 注册表 Uninstall → 快捷方式 → 进程镜像。
    返回已去重、**已确认存在**的路径列表;不存在的候选被丢弃。
    """
    found: List[str] = []

    def _add(path: Optional[str]) -> None:
        if not path:
            return
        path = os.path.normpath(path.strip())
        if path.lower().endswith(".lnk"):
            target = _lnk_target(path)
            if target:
                _add(target)
            return
        if not path.lower().endswith(".exe"):
            return
        if not os.path.isfile(path):
            return
        if path not in found:
            found.append(path)

    # 1) 环境变量
    override = os.environ.get(ENV_EXE)
    if override:
        _add(override)

    # 2) 传统候选
    for template in CANDIDATE_EXES:
        _add(os.path.expandvars(template))

    # 3) 注册表(可能已装到任意盘,如 C:\\软件\\...)
    for path in _registry_install_locations():
        _add(path)

    # 4) 快捷方式(桌面/开始菜单)
    for path in _shortcut_locations():
        _add(path)

    # 5) 正在运行的进程镜像路径
    running = running_process_image()
    if running:
        _add(running)

    return found


def executable_from_env_or_default() -> Optional[str]:
    """取第一个能用的 E-Mobile.exe;找不到返回 None。"""
    found = discover_exes()
    return found[0] if found else None


def _registry_install_locations() -> List[str]:
    """从 Uninstall 注册表项反查 E-Mobile 安装位置。"""
    paths: List[str] = []
    try:
        import winreg
    except ImportError:  # 非 Windows
        return paths
    hives = ((winreg.HKEY_LOCAL_MACHINE, sub) for sub in _UNINSTALL_HIVES)
    hives = list(hives) + [(winreg.HKEY_CURRENT_USER, sub)
                          for sub in _UNINSTALL_HIVES]
    for hive, subkey in hives:
        try:
            root = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        try:
            paths.extend(_scan_uninstall_key(winreg, root))
        finally:
            winreg.CloseKey(root)
    return paths


def _scan_uninstall_key(winreg: object, root: object) -> List[str]:
    paths: List[str] = []
    index = 0
    while True:
        try:
            name = winreg.EnumKey(root, index)
        except OSError:
            break
        index += 1
        try:
            app_key = winreg.OpenKey(root, name)
        except OSError:
            continue
        try:
            if _looks_like_emobile(winreg, app_key):
                paths.extend(_install_location_from(winreg, app_key))
        finally:
            winreg.CloseKey(app_key)
    return paths


def _looks_like_emobile(winreg: object, app_key: object) -> bool:
    """该 Uninstall 项是否 E-Mobile(按 DisplayName / DisplayIcon 判)。"""
    for value_name in ("DisplayName", "DisplayIcon"):
        try:
            value = winreg.QueryValueEx(app_key, value_name)[0]
        except OSError:
            continue
        if isinstance(value, str) and "e-mobile" in value.lower():
            return True
    return False


def _install_location_from(winreg: object, app_key: object) -> List[str]:
    """从 Uninstall 注册表项里挑出 E-Mobile.exe 的候选路径。

    只认文件名恰为 `E-Mobile.exe` 的项 —— 否则 uninstall.exe /
    Snipaste.exe 之类的也会被带进来(实测开发机注册表 DisplayIcon 指向
    uninstall.exe)。
    """
    out: List[str] = []

    def _consider(raw: str) -> None:
        raw = raw.strip().strip('"')
        if not raw:
            return
        if raw.lower().endswith("e-mobile.exe") and os.path.isfile(raw):
            if raw not in out:
                out.append(raw)
        elif os.path.isdir(raw):
            base = Path(raw)
            direct = base / "E-Mobile.exe"
            nested = sorted(base.rglob("E-Mobile.exe"))
            for candidate in ([direct] if direct.is_file() else []) + nested:
                text = str(candidate)
                if text not in out:
                    out.append(text)

    for value_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
        try:
            raw = winreg.QueryValueEx(app_key, value_name)[0]
        except OSError:
            continue
        if not isinstance(raw, str):
            continue
        # UninstallString 可能带参数:取第一个形如 "X:\...\*.exe" 的片段
        if value_name == "UninstallString" and " " in raw.strip():
            for token in raw.strip().split():
                if token.lower().endswith(".exe"):
                    _consider(token)
        else:
            _consider(raw)
    return out


def _shortcut_locations() -> List[str]:
    """扫桌面与开始菜单里的 E-Mobile 快捷方式。"""
    roots: List[str] = []
    for var in ("USERPROFILE", "APPDATA", "ProgramData", "ALLUSERSPROFILE"):
        base = os.environ.get(var)
        if not base or not os.path.isdir(base):
            continue
        roots.append(os.path.join(base, "Desktop"))
        roots.append(os.path.join(base, "Microsoft", "Windows",
                                  "Start Menu", "Programs"))
        roots.append(os.path.join(base, "Microsoft", "Windows", "Start Menu"))
    paths: List[str] = []
    seen: set = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        for pattern in ("E-Mobile*.lnk", "*E-Mobile*.lnk", "*emobile*.lnk"):
            for match in glob.glob(os.path.join(root, "**", pattern),
                                   recursive=True):
                if match not in paths:
                    paths.append(match)
    return paths


def _lnk_target(lnk_path: str) -> Optional[str]:
    """解析 .lnk 的 TargetPath(优先 win32com,失败回落 PowerShell COM)。"""
    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("WScript.Shell")
        return shell.CreateShortcut(lnk_path).TargetPath
    except Exception:  # noqa: BLE001
        pass
    try:
        script = ("$s=(New-Object -ComObject WScript.Shell)"
                  ".CreateShortcut('{p}');$s.TargetPath"
                  ).format(p=lnk_path.replace("'", "''"))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20)
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line:
                return line
    except Exception:  # noqa: BLE001
        pass
    return None


def running_process_image() -> Optional[str]:
    """正在运行的 E-Mobile.exe 镜像路径(E-Mobile 已启动时优先复用)。"""
    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(["name", "exe"]):
            if (proc.info.get("name") or "").lower() == "e-mobile.exe":
                exe = proc.info.get("exe") or ""
                if exe and os.path.isfile(exe):
                    return exe
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    # 退路:WMIC
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='E-Mobile.exe'",
             "get", "ExecutablePath"],
            capture_output=True, text=True, timeout=20)
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.lower().endswith(".exe") and os.path.isfile(line):
                return line
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------- #
# 端口 / 进程
# --------------------------------------------------------------------------- #
def port_from_env() -> Optional[int]:
    raw = os.environ.get(ENV_PORT)
    if raw and raw.isdigit():
        return int(raw)
    return None


def cdp_alive(port: int, host: str = "127.0.0.1",
              timeout: float = 1.2) -> bool:
    """该端口上是否有**活的 CDP**服务(非 CDP 的 HTTP 服务会返回 /json/list
    非 JSON 或非 list,故不算)。"""
    for path in ("json/version", "json/list"):
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/{path}",
                                        timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                body = resp.read().decode("utf-8", "replace").strip()
                if not body:
                    continue
                data = json.loads(body)
                if path == "json/list" and not isinstance(data, list):
                    continue
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def find_live_port() -> Optional[int]:
    """已经在跑的带调试端口实例(逐个候选端口体检)。"""
    for port in CANDIDATE_PORTS:
        if cdp_alive(port):
            return port
    return None


def is_app_running() -> bool:
    """E-Mobile 主进程是否在跑(不管有没有调试端口)。"""
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None
    else:
        try:
            for proc in psutil.process_iter(["name"]):
                if (proc.info.get("name") or "").lower() == "e-mobile.exe":
                    return True
            return False
        except Exception:  # noqa: BLE001
            pass
    # 退路:tasklist
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq E-Mobile.exe"],
                             capture_output=True, text=True, timeout=15)
        return "E-Mobile.exe" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def _env_flag(name: str) -> bool:
    """环境变量是否被显式置真(1/true/yes/on,大小写不敏感)。"""
    raw = os.environ.get(name)
    return bool(raw) and raw.strip().lower() in ("1", "true", "yes", "on")


def restart_disabled() -> bool:
    """`EMCLI_NO_RESTART=1/true` 时禁用自动兜底重启。"""
    return _env_flag(ENV_NO_RESTART)


def force_disabled() -> bool:
    """`EMCLI_NO_FORCE=1/true/yes/on` 时禁用"超时后强制结束进程"。

    禁用后关闭链路**完全退回旧行为**:只发 WM_CLOSE 与不带 `/F` 的
    `taskkill /PID`,超时即报错(绝不强杀)。
    """
    return _env_flag(ENV_NO_FORCE)


def _default_notify(message: str) -> None:
    """默认提示通道:写 stderr(不依赖 output 模块,避免循环导入)。"""
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _notify(callback: Optional[Callable[[str], None]], message: str) -> None:
    """把提示交给调用方回调;未提供时走默认 stderr 通道。"""
    (callback or _default_notify)(message)


def candidate_ports(preferred: Optional[int] = None) -> List[int]:
    """按优先级去重后的候选端口:`preferred` → 环境变量 → 内置候选。"""
    out: List[int] = []
    for port in [preferred, port_from_env()] + list(CANDIDATE_PORTS):
        if port and int(port) not in out:
            out.append(int(port))
    return out


def tcp_in_use(port: int, host: str = "127.0.0.1",
               timeout: float = 0.4) -> bool:
    """该端口是否已被别的进程监听(非 CDP 的普通服务也算占用)。

    客户端要 `bind` 调试端口,被占就会静默起不来,所以选端口时要避开。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False


def free_launch_ports(preferred: Optional[int] = None) -> List[int]:
    """可用来启动的候选端口:既没有活 CDP,也没被普通监听占用。"""
    return [p for p in candidate_ports(preferred)
            if not cdp_alive(p) and not tcp_in_use(p)]


def running_process_pids() -> List[int]:
    """所有 E-Mobile.exe 进程 pid(psutil 优先,tasklist 回落)。"""
    try:
        import psutil  # type: ignore

        pids: List[int] = []
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() == "e-mobile.exe":
                pids.append(int(proc.pid))
        if pids:
            return pids
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq E-Mobile.exe",
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace")
        pids = []
        for line in (out.stdout or "").splitlines():
            cells = [c.strip().strip('"') for c in line.split('","')]
            if len(cells) >= 2 and cells[0].lower() == "e-mobile.exe":
                try:
                    pids.append(int(cells[1]))
                except ValueError:
                    continue
        return pids
    except Exception:  # noqa: BLE001
        return []


def post_wm_close(pids: List[int]) -> int:
    """向这些 pid 的可见顶层窗口发 WM_CLOSE,返回成功条数。

    这是最温和的一步:等价于用户点窗口右上角的 ×,客户端有机会正常收尾。
    """
    if not pids:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        targets = set(int(p) for p in pids)
        sent = 0
        wm_close = 0x0010
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                       wintypes.LPARAM)

        def _callback(hwnd: int, _lparam: int) -> bool:
            nonlocal sent
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in targets and user32.IsWindowVisible(hwnd):
                if user32.PostMessageW(hwnd, wm_close, 0, 0):
                    sent += 1
            return True

        user32.EnumWindows(enum_proc(_callback), 0)
        return sent
    except Exception:  # noqa: BLE001
        return 0


def request_graceful_close(pids: List[int]) -> List[Dict[str, Any]]:
    """**优雅**(第一级)关闭请求:先 WM_CLOSE,再 `taskkill /PID`(**不带 `/F`**)。

    本函数绝不使用 `/F` —— 强杀属于**第二级**,在 `force_kill_pids()` 里,
    只由 `close_running_app()` 在优雅超时且未设 `EMCLI_NO_FORCE` 时调用。
    返回每一步的结果,便于上层如实汇报。
    """
    steps: List[Dict[str, Any]] = []
    sent = post_wm_close(pids)
    steps.append({"step": "wm_close", "pids": list(pids), "sent": sent})
    for pid in pids:
        try:
            out = subprocess.run(
            ["taskkill", "/PID", str(pid)],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace")
            steps.append({"step": "taskkill", "pid": int(pid),
                          "returncode": out.returncode,
                          "output": (out.stdout or out.stderr or "").strip()})
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": "taskkill", "pid": int(pid),
                          "returncode": None, "output": str(exc)})
    return steps


def force_kill_pids(pids: List[int]) -> List[Dict[str, Any]]:
    """**强制**结束这些 pid(`taskkill /F /PID`),返回每一步结果。

    这是**第二级**动作,只在优雅关闭超时、且未设 `EMCLI_NO_FORCE` 时才会被
    `close_running_app()` 调用。强杀会丢掉用户未发送的草稿,所以护栏从严:

      * 执行前用 `running_process_pids()`(**按进程名 `E-Mobile.exe` 过滤**)
        复核每个 pid —— 不在名单里的一律**跳过**并如实记录,避免误杀别的程序;
      * 只用 `subprocess` 调 `taskkill`,**不用** `os.kill`;
      * 单个 pid 的失败/异常只记录、不向上抛 —— 强杀失败不该让调用方崩溃。
    """
    steps: List[Dict[str, Any]] = []
    if not pids:
        return steps
    allowed = set(running_process_pids())
    for raw_pid in pids:
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid not in allowed:
            steps.append({"step": "force_kill", "pid": pid, "skipped": True,
                          "returncode": None,
                          "output": "该 pid 当前不是 E-Mobile.exe,已跳过(避免误杀)"})
            continue
        try:
            out = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace")
            steps.append({"step": "force_kill", "pid": pid, "skipped": False,
                          "returncode": out.returncode,
                          "output": (out.stdout or out.stderr or "").strip()})
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": "force_kill", "pid": pid, "skipped": False,
                          "returncode": None, "output": str(exc)})
    return steps


def _wait_until_closed(timeout_s: float) -> bool:
    """等 E-Mobile 退出;`timeout_s` 秒内退出返回 True,否则 False。"""
    deadline = time.time() + max(0.0, float(timeout_s))
    while True:
        if not is_app_running():
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        time.sleep(min(0.5, max(0.05, remaining)))


def _graceful_failed_message(timeout_s: float) -> str:
    """`EMCLI_NO_FORCE=1`(或调用方拿到 False)时的报错文案:两条可执行选项。"""
    return (
        f"E-Mobile 在 {timeout_s:g}s 内没有退出,已放弃自动重启。\n"
        "本工具不会强制结束进程(以免丢失未发送的内容)。可选做法:\n"
        "  1) 手动退出 E-Mobile(托盘右键退出)后重试;\n"
        f"  2) 设置 {ENV_NO_RESTART}=1 后重试,"
        "本工具会改为只提示、不自动处理。")


def _force_failed_message(timeout_s: float, force_wait_s: float) -> str:
    """强杀之后进程仍存活时的报错文案(附手动处理建议)。"""
    return (
        f"E-Mobile 在优雅关闭 {timeout_s:g}s + 强制结束 {force_wait_s:g}s 后仍在运行。\n"
        "可能原因:当前账号权限不足以结束该进程,或客户端被别的程序(守护/计划任务)"
        "重新拉起。可选做法:\n"
        "  1) 用管理员身份重开一个终端后重试;\n"
        "  2) 打开任务管理器结束 `E-Mobile.exe`,再重试;\n"
        f"  3) 设置 {ENV_NO_RESTART}=1 后重试,本工具会改为只提示、不自动处理。")


def close_running_app(timeout_s: float = DEFAULT_RESTART_WAIT_S,
                      notify: Optional[Callable[[str], None]] = None,
                      force_wait_s: float = DEFAULT_FORCE_WAIT_S) -> bool:
    """关闭正在运行的无端口实例并等它退出(**两级关闭**)。

    第一级(**优雅**,与旧行为一致):`WM_CLOSE` + `taskkill /PID`(**不带
    `/F`**),最多等 `timeout_s` 秒;进程退出即返回 True。

    第一级超时后:
      * **未**设 `EMCLI_NO_FORCE` → 第二级(**强制**):先经 `notify` 打印中文
        提示,再对**按进程名复核后仍属 E-Mobile.exe** 的剩余 pid 执行
        `taskkill /F /PID`,再等 `force_wait_s` 秒;仍未退出则抛 `AppError`
        (附手动处理建议)。⚠ 强杀会丢掉用户未发送的草稿。
      * 设了 `EMCLI_NO_FORCE=1/true/yes/on` → **完全退回旧行为**:不发任何
        `/F`,抛 `AppError`,文案给出「手动退出」与「`EMCLI_NO_RESTART=1`」
        两条选项。

    返回 True 表示进程确实退出了。
    """
    pids = running_process_pids()
    request_graceful_close(pids)
    if _wait_until_closed(timeout_s):
        return True

    if force_disabled():
        raise AppError(_graceful_failed_message(timeout_s))

    remaining = running_process_pids() or pids
    _notify(notify,
            "优雅关闭未生效,正在强制结束 E-Mobile 进程"
            "(未发送的草稿可能丢失)…")
    force_kill_pids(remaining)
    if _wait_until_closed(force_wait_s):
        return True
    raise AppError(_force_failed_message(timeout_s, force_wait_s))


def clean_child_env() -> Dict[str, str]:
    """构造拉起 E-Mobile 用的**干净**环境变量字典。

    复制当前进程环境后,只摘掉 Electron 相关键(见 `ELECTRON_ENV_KEYS`)——
    DSH 宿主注入的 `ELECTRON_RUN_AS_NODE=1` 会让 Electron 以 Node 模式启动
    并立即退出,导致调试端口永远等不到。其余变量(代理 `HTTP_PROXY`、
    `EMCLI_*` 等)原样保留;比较按大小写不敏感处理(Windows 环境变量即如此)。
    """
    banned = {key.upper() for key in ELECTRON_ENV_KEYS}
    return {key: value for key, value in os.environ.items()
            if key.upper() not in banned}


def launch_app(port: int, exe: Optional[str] = None) -> str:
    """以 `--remote-debugging-port=<port>` 启动 E-Mobile,返回 exe 路径。"""
    if exe is None:
        exe = executable_from_env_or_default()
    if exe is None:
        raise AppError(
            "找不到 E-Mobile.exe。已尝试:环境变量 "
            f"{ENV_EXE}、注册表反查、桌面/开始菜单快捷方式、"
            "常见安装目录、运行中进程镜像路径。\n"
            f"请设置 {ENV_EXE} 指向它的完整路径,例如:\n"
            f"  $env:{ENV_EXE} = 'C:\\软件\\E-Mobile\\E-Mobile\\E-Mobile.exe'")
    try:
        # env 必须是**干净**的:见 clean_child_env() / ELECTRON_ENV_KEYS
        subprocess.Popen([exe, f"{DEBUG_PORT_FLAG}={int(port)}"],
                         close_fds=True, env=clean_child_env())
    except OSError as exc:
        raise AppError(f"启动 E-Mobile 失败:{exc}") from exc
    return exe


def ensure_port(preferred: Optional[int] = None,
                launch_if_needed: bool = True,
                wait_s: int = 45,
                auto_restart: bool = True,
                restart_wait_s: float = DEFAULT_RESTART_WAIT_S,
                force_wait_s: float = DEFAULT_FORCE_WAIT_S,
                notify: Optional[Callable[[str], None]] = None,
                state: Optional[Dict[str, Any]] = None) -> int:
    """确保有一个带调试端口的 E-Mobile 可用,返回端口。

    行为:
      1. preferred / 环境变量 / 常见端口里已有活的 → 直接返回;
      2. 客户端在跑但没有调试端口:
         - `auto_restart=True` 且未设 `EMCLI_NO_RESTART=1` → 关闭它(两级:
           优雅 → 超时后强制,见 `close_running_app()`)后自己带端口重启
           (登录态在 userData 里,保留);
         - 否则 → 抛 `AppError`(提示手动退出);
      3. 客户端没在跑 → 直接带端口启动;
      4. 端口被别的程序占用 / 启动后没就绪 → 顺延下一个候选端口再试一次
         (最多 `MAX_LAUNCH_ATTEMPTS` 个候选)。

    参数:
      auto_restart: 是否允许"关掉无端口实例再带端口拉起"(默认开)。
      restart_wait_s: 第一级(优雅)关闭的等待秒数,超时进入第二级。
      force_wait_s: 第二级(强制 `taskkill /F`)之后的等待秒数。设
        `EMCLI_NO_FORCE=1` 时不会走到第二级(超时即报错,绝不强杀)。
      notify: 提示回调;不传时默认写 stderr(不依赖 output 模块)。
      launch_if_needed=False 时**不会启动任何进程**,也不会关闭正在运行的实例。
      state: 可选的**结果回传**字典(返回值只能是端口,这里补足"本次到底做了
        什么")。函数会写入两个键:
          * ``state["restarted"]`` (bool): 本次是否**由本工具启动了客户端**,
            即"启动前客户端没在跑或没带调试端口"(含冷启动与兜底重启);
            调用方据此判断"刚启动的客户端可能还没加载完登录态"。
          * ``state["port"]`` (int|None): 最终生效的端口(失败时为 None)。
    """
    if state is not None:
        state["restarted"] = False
        state["port"] = None

    for port in candidate_ports(preferred):
        if cdp_alive(port):
            if state is not None:
                state["port"] = port
            return port

    if is_app_running():
        if not (auto_restart and not restart_disabled()):
            raise AppError(
                "检测到 E-Mobile 正在运行,但没有开放调试端口。\n"
                "请完全退出 E-Mobile(托盘右键退出),然后重试;\n"
                "本工具会自动以 --remote-debugging-port 重新启动它。\n"
                f"(自动重启当前被禁用:auto_restart={auto_restart}、"
                f"{ENV_NO_RESTART}={os.environ.get(ENV_NO_RESTART, '')!r})")
        if not launch_if_needed:
            raise AppError(
                "检测到 E-Mobile 正在运行,但没有开放调试端口,"
                "且调用方不允许启动新进程(launch_if_needed=False)。\n"
                "为不影响你正在使用的窗口,本工具没有做任何关闭动作。")
        _notify(notify,
                "检测到 E-Mobile 正在运行但未开放调试端口,"
                "正在为你重启客户端以启用(登录态会保留)…")
        # 两级关闭:优雅超时后(默认)强制结束;EMCLI_NO_FORCE=1 时退回旧行为。
        # 走到强杀必然意味着"自动重启"确实要发生(上面已排除 auto_restart=False /
        # EMCLI_NO_RESTART / launch_if_needed=False 三种情况)。
        if not close_running_app(timeout_s=restart_wait_s,
                                 force_wait_s=force_wait_s,
                                 notify=notify):
            # 只有调用方把 close_running_app 打桩成返回 False 才会到这里;
            # 真实实现要么返回 True,要么抛 AppError。
            raise AppError(_graceful_failed_message(restart_wait_s))
        _notify(notify, "E-Mobile 已退出,正在以调试端口重新启动…")

    if not launch_if_needed:
        raise AppError("E-Mobile 未运行,且未允许自动启动。")

    exe = executable_from_env_or_default()
    if exe is None:
        raise AppError(
            "找不到 E-Mobile.exe。已尝试:环境变量 "
            f"{ENV_EXE}、注册表反查、桌面/开始菜单快捷方式、"
            "常见安装目录、运行中进程镜像路径。\n"
            f"请设置 {ENV_EXE} 指向它的完整路径,例如:\n"
            f"  $env:{ENV_EXE} = 'C:\\软件\\E-Mobile\\E-Mobile\\E-Mobile.exe'")

    ports = free_launch_ports(preferred)[:MAX_LAUNCH_ATTEMPTS]
    if not ports:
        raise AppError("没有可用的调试端口(已尝试: "
                       + ", ".join(str(p) for p in candidate_ports(preferred))
                       + ")")

    tried: List[str] = []
    for index, chosen in enumerate(ports):
        if index:
            # 上一次启动后端口没就绪:如果进程确实起来了(例如端口在探测后被别的
            # 程序抢走),**不能**再拉第二个实例 —— 两个实例共用同一 userData
            # (LevelDB/SQLite)会并发写、有数据损坏风险。此时如实报错,让用户重试
            # (重试时上面那段"自动兜底重启"会把这个无端口实例优雅收掉)。
            if is_app_running():
                raise AppError(
                    f"端口 {ports[index - 1]} 上启动的实例没有开放调试端口,"
                    "为避免同时运行两个实例(会并发写同一 userData),"
                    "已停止继续换端口尝试。\n"
                    "请重试一次:本工具会自动优雅关闭这个无端口实例,"
                    "再带调试端口重新启动。")
            _notify(notify, f"端口 {ports[index - 1]} 未就绪,"
                            f"改用候选端口 {chosen} 再试一次…")
        launch_app(chosen, exe=exe)
        if state is not None:
            # 走到这里说明客户端是本工具拉起来的(冷启动或兜底重启)
            state["restarted"] = True
        deadline = time.time() + max(0.0, float(wait_s))
        while True:
            if cdp_alive(chosen):
                if state is not None:
                    state["port"] = chosen
                return chosen
            if time.time() >= deadline:
                break
            time.sleep(1.0)
        tried.append(str(chosen))
    raise AppError(f"已启动 E-Mobile,但 {wait_s}s 内调试端口未就绪"
                   f"(已尝试: {', '.join(tried)})")


# --------------------------------------------------------------------------- #
# 启动入口持久化:让客户端"以后默认带端口启动"
# --------------------------------------------------------------------------- #
def public_lnk_path() -> str:
    """公共桌面 E-Mobile 快捷方式路径(可用 `EMCLI_EMOBILE_LNK` 覆盖)。"""
    return os.environ.get(ENV_LNK) or DEFAULT_PUBLIC_LNK


def launcher_dir() -> str:
    """本工具创建的启动器所在目录(可用 `EMCLI_LAUNCHER_DIR` 覆盖)。"""
    override = os.environ.get(ENV_LAUNCHER_DIR)
    if override:
        return override
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "Desktop")


def cli_launcher_path() -> str:
    """用户桌面上"带参数启动器"的完整路径。"""
    return os.path.join(launcher_dir(), CLI_LAUNCHER_NAME)


def backup_dir() -> str:
    """备份目录:默认 `%LOCALAPPDATA%\\emcli\\backup`(可覆盖)。"""
    override = os.environ.get(ENV_BACKUP_DIR)
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "emcli", "backup")


def _debug_ports_in(arguments: str) -> List[int]:
    """参数串里**所有** `--remote-debugging-port=<n>` 的端口(按出现顺序去重)。

    回滚时要如实告知"还剩哪些端口",所以不能只取第一个。
    """
    out: List[int] = []
    for match in re.finditer(re.escape(DEBUG_PORT_FLAG) + r"=(\d+)",
                             arguments or ""):
        port = int(match.group(1))
        if port not in out:
            out.append(port)
    return out


def _debug_port_in(arguments: str) -> Optional[int]:
    """从参数串里取出第一个 `--remote-debugging-port=<n>` 的端口;没有返回 None。"""
    ports = _debug_ports_in(arguments)
    return ports[0] if ports else None


def _append_debug_arg(arguments: str, port: int) -> str:
    """**追加**(不覆盖)调试端口参数;已含则原样返回(幂等)。"""
    args = (arguments or "").strip()
    if _debug_port_in(args) is not None:
        return args
    token = f"{DEBUG_PORT_FLAG}={int(port)}"
    return f"{args} {token}".strip() if args else token


def _strip_debug_arg(arguments: str, port: Optional[int] = None) -> str:
    """去掉本工具加过的调试端口参数(只动这一部分,保留其余参数)。

    `port` 为 `None` 或 `0` 时去掉所有 `--remote-debugging-port=` 参数。
    """
    pattern = re.escape(DEBUG_PORT_FLAG) + (
        r"=(\d+)" if not port else r"=" + str(int(port)) + r"(?!\d)")
    stripped = re.sub(pattern, " ", arguments or "")
    return " ".join(stripped.split())


def _ps_quote(text: str) -> str:
    """PowerShell 单引号字符串转义。"""
    return (text or "").replace("'", "''")


def _lnk_fields(path: str) -> Dict[str, str]:
    """读 .lnk 的字段(win32com 优先,失败回落 PowerShell COM)。

    读取失败返回空 dict —— 调用方按"读不到"处理,不要当空参数。
    """
    try:
        import win32com.client  # type: ignore

        shortcut = win32com.client.Dispatch(
            "WScript.Shell").CreateShortcut(path)
        return {
            "TargetPath": shortcut.TargetPath or "",
            "Arguments": shortcut.Arguments or "",
            "WorkingDirectory": shortcut.WorkingDirectory or "",
            "IconLocation": shortcut.IconLocation or "",
            "Description": shortcut.Description or "",
        }
    except Exception:  # noqa: BLE001
        pass
    try:
        script = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{p}');"
            "[pscustomobject]@{{TargetPath=$s.TargetPath;"
            "Arguments=$s.Arguments;"
            "WorkingDirectory=$s.WorkingDirectory;"
            "IconLocation=$s.IconLocation;"
            "Description=$s.Description}} | ConvertTo-Json -Compress"
        ).format(p=_ps_quote(path))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        body = (out.stdout or "").strip()
        if body:
            data = json.loads(body)
            if isinstance(data, dict):
                return {k: (v or "") for k, v in data.items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _write_lnk(path: str, target: str, arguments: str = "",
               working_directory: str = "", icon: str = "",
               description: str = "") -> None:
    """写 .lnk(win32com 优先,失败回落 PowerShell COM);两条路都失败即抛错。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    errors: List[str] = []
    try:
        import win32com.client  # type: ignore

        shortcut = win32com.client.Dispatch(
            "WScript.Shell").CreateShortcut(path)
        shortcut.TargetPath = target
        shortcut.Arguments = arguments or ""
        if working_directory:
            shortcut.WorkingDirectory = working_directory
        shortcut.IconLocation = icon or target
        shortcut.Description = description or ""
        shortcut.Save()
        return
    except Exception as exc:  # noqa: BLE001
        errors.append(f"win32com: {exc}")
    try:
        script = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{p}');"
            "$s.TargetPath='{t}';$s.Arguments='{a}';$s.IconLocation='{i}';"
            "$s.Description='{d}';"
            "{w}$s.Save()"
        ).format(p=_ps_quote(path), t=_ps_quote(target),
                 a=_ps_quote(arguments or ""), i=_ps_quote(icon or target),
                 d=_ps_quote(description or ""),
                 w=("$s.WorkingDirectory='{}';".format(
                     _ps_quote(working_directory))
                    if working_directory else ""))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        if out.returncode == 0 and os.path.isfile(path):
            return
        errors.append(f"powershell: {(out.stderr or out.stdout or '').strip()}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"powershell: {exc}")
    raise AppError("; ".join(errors) or "写快捷方式失败")


def _lnk_writable(path: str) -> bool:
    """该 .lnk(或它所在目录,当文件还不存在时)是否可写。

    对已存在的文件用 `open(path, "r+b")` 做**真实权限探测** —— `os.access`
    只看"只读属性",看不出 ACL 层面的提权要求(实测公共桌面
    `E-Mobile.lnk` 的 `os.access(W_OK)` 返回 True,但真写会
    `PermissionError`)。该探测不写入任何字节,不产生副作用。
    """
    if os.path.isfile(path):
        try:
            with open(path, "r+b"):
                pass
            return True
        except OSError:
            return False
    parent = os.path.dirname(path) or "."
    # 文件还不存在时只能退而求其次:目录层面的可写性判断(无法反映 ACL 提权)
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


def _backup_lnk(path: str) -> Tuple[Optional[str], Optional[str]]:
    """改之前先把 .lnk 复制到备份目录,返回 (备份路径, 错误信息)。"""
    try:
        dest_dir = backup_dir()
        os.makedirs(dest_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0] or "shortcut"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(dest_dir, f"{stem}.{stamp}.lnk")
        index = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{stem}.{stamp}.{index}.lnk")
            index += 1
        shutil.copy2(path, dest)
        # copy2 会连只读属性一起复制过来,备份件要能覆盖写
        try:
            os.chmod(dest, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        return dest, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _recycle_allowed(path: str) -> bool:
    """回收站护栏:只允许**精确匹配** `cli_launcher_path()`(其余一律拒绝)。

    全代码只有"用户桌面启动器"会走回收(`remove_launcher()`);公共桌面
    快捷方式是**改写参数**而不是回收,所以这里不再放行 `public_lnk_path()`
    —— 白名单越窄越好。

    之前的实现允许回收"启动器目录下的任意文件",那等于把整个用户桌面都
    纳入了可回收范围,过宽。这里改用 `Path.resolve()` 做精确比较(Windows
    下大小写不敏感、软链接也会被解析到真实路径)。
    """
    if not path:
        return False
    try:
        target = Path(path).resolve()
        allowed = Path(cli_launcher_path()).resolve()
    except OSError:
        return False
    return target == allowed


def _recycle_file(path: str) -> bool:
    """把文件**移到回收站**(绝不直删),成功返回 True。

    优先 `send2trash`(若已安装),否则用 PowerShell 的
    `Microsoft.VisualBasic.FileIO.FileSystem.DeleteFile(..., SendToRecycleBin)`。
    安全护栏见 `_recycle_allowed()`:只允许本工具在用户桌面创建的那个精确路径。
    """
    if not path or not os.path.isfile(path):
        return False
    if not _recycle_allowed(path):
        return False
    try:
        import send2trash  # type: ignore

        send2trash.send2trash(path)
        if not os.path.exists(path):
            return True
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    try:
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic;"
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('{p}',"
            "'OnlyErrorDialogs','SendToRecycleBin')"
        ).format(p=_ps_quote(path))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        return out.returncode == 0 and not os.path.exists(path)
    except Exception:  # noqa: BLE001
        return False


def _resolve_launcher_target() -> Optional[str]:
    """带参数启动器要指向的 exe:公共 lnk 的 Target → 发现结果 → 运行中镜像。"""
    public = public_lnk_path()
    if os.path.isfile(public):
        target = (_lnk_fields(public).get("TargetPath") or "").strip('"')
        if target and os.path.isfile(target):
            return target
    exe = executable_from_env_or_default()
    if exe:
        return exe
    return running_process_image()


def launcher_status() -> Dict[str, Any]:
    """**只读**检查启动入口现状(不做任何修改)。

    返回:
      public_lnk / user_launcher: 各自的 path、是否存在、Arguments、
        是否已带调试端口、端口、是否可写(公共 lnk)/ 是否本工具创建(用户启动器);
      configured: 两条路径里是否已有一条能带端口启动;
      port / launcher: 生效的端口与启动器路径;
      rollback: 回滚方法的一句话说明。
    """
    public = public_lnk_path()
    user = cli_launcher_path()
    public_exists = os.path.isfile(public)
    user_exists = os.path.isfile(user)
    public_fields = _lnk_fields(public) if public_exists else {}
    user_fields = _lnk_fields(user) if user_exists else {}
    public_args = public_fields.get("Arguments", "")
    user_args = user_fields.get("Arguments", "")
    public_port = _debug_port_in(public_args)
    user_port = _debug_port_in(user_args)

    public_info: Dict[str, Any] = {
        "path": public,
        "exists": public_exists,
        "arguments": public_args,
        "configured": public_port is not None,
        "port": public_port,
        "writable": _lnk_writable(public),
        "reason": "" if public_exists else "公共桌面快捷方式不存在",
    }
    if public_exists and not public_info["writable"]:
        public_info["reason"] = "公共桌面快捷方式不可写,修改需要管理员权限"
    user_info: Dict[str, Any] = {
        "path": user,
        "exists": user_exists,
        "arguments": user_args,
        "configured": user_port is not None,
        "port": user_port,
        "created_by_us": CLI_LAUNCHER_MARKER
        in (user_fields.get("Description") or ""),
        "reason": "" if user_exists else "尚未创建带参数的启动器",
    }
    launcher = public if public_port is not None else (
        user if user_port is not None else None)
    return {
        "public_lnk": public_info,
        "user_launcher": user_info,
        "configured": launcher is not None,
        "port": public_port if public_port is not None else user_port,
        "launcher": launcher,
        "rollback": ("回滚:调用本工具的 remove_launcher() —— 会去掉本工具追加的 "
                     f"{DEBUG_PORT_FLAG} 参数,并把本工具创建的启动器移入回收站"
                     "(不直接删除)。"),
    }


def _create_cli_launcher(port: int) -> Dict[str, Any]:
    """在用户桌面创建带调试端口的启动器(降级路径),返回结构化结果。

    护栏:目标文件已存在但**不含本工具 marker**(`CLI_LAUNCHER_MARKER`)时
    **拒绝写入** —— 用户桌面上可能有同名自有快捷方式,覆盖它属于破坏性操作。
    """
    path = cli_launcher_path()
    exists = os.path.isfile(path)
    fields = _lnk_fields(path) if exists else {}
    if exists and CLI_LAUNCHER_MARKER not in (fields.get("Description") or ""):
        return {"ok": False, "changed": False, "action": "none",
                "launcher": path, "arguments": fields.get("Arguments", ""),
                "port": None,
                "reason": (f"桌面已存在同名快捷方式且非本工具创建,请先处理:{path}"
                           "(确认不是你要用的就改名或删除,本工具不覆盖它)")}
    existing_port = _debug_port_in(fields.get("Arguments", ""))
    if existing_port is not None:
        return {"ok": True, "changed": False, "action": "already-configured",
                "launcher": path, "arguments": fields.get("Arguments", ""),
                "port": existing_port, "reason": "用户桌面启动器已存在且已带参数"}
    target = _resolve_launcher_target()
    if not target:
        return {"ok": False, "changed": False, "action": "none",
                "launcher": path, "arguments": "", "port": None,
                "reason": "找不到 E-Mobile.exe,无法创建带参数的启动器"}
    arguments = _append_debug_arg(fields.get("Arguments", ""), port)
    try:
        _write_lnk(path, target=target, arguments=arguments,
                   working_directory=os.path.dirname(target), icon=target,
                   description=f"{CLI_LAUNCHER_MARKER}: {DEBUG_PORT_FLAG}")
    except AppError as exc:
        return {"ok": False, "changed": False, "action": "none",
                "launcher": path, "arguments": arguments, "port": port,
                "reason": f"创建用户桌面启动器失败:{exc}"}
    return {"ok": True, "changed": True, "action": "created-user-launcher",
            "launcher": path, "arguments": arguments, "port": port,
            "target": target, "reason": ""}


def configure_launcher(port: int = 9333,
                       prefer_public: bool = True) -> Dict[str, Any]:
    """让客户端"以后默认带端口启动"。

    步骤:
      1. 先**备份**要改的 .lnk 到 `%LOCALAPPDATA%\\emcli\\backup\\<时间戳>.lnk`;
      2. 首选给公共桌面 `E-Mobile.lnk` **追加** `--remote-debugging-port=<port>`
         (幂等:已含该参数就不重复追加,返回既有端口);
      3. 写公共 lnk 需要管理员权限 —— 捕获权限/COM 错误后**自动降级**为在
         用户桌面创建 `E-Mobile (CLI).lnk`;
      4. 返回结构化结果(改了什么/建了什么、是否成功、是否降级、失败原因、
         备份路径、回滚说明)。

    两条硬护栏(读不到就**一个字节都不写**,绝不虚报成功):
      * `_lnk_fields(public)` 必须读得到**非空**且 `TargetPath` 非空、指向
        存在的 exe —— 否则会把快捷方式的 TargetPath / Description 清空;
      * `_backup_lnk()` 失败时必须**中止**对公共 lnk 的写入(否则改坏了
        无法回滚)。

    注意:本函数只写快捷方式,**不会**启动或关闭任何进程。
    """
    result: Dict[str, Any] = {
        "ok": False,
        "action": "none",
        "changed": False,
        "degraded": False,
        "port": int(port),
        "lnk": None,
        "arguments": "",
        "backup": None,
        "reason": "",
        "rollback": ("回滚:再次运行 remove_launcher()"
                     "(或 `launcher --remove`)即可去掉本工具追加的参数 / "
                     "把本工具创建的启动器移入回收站。"),
    }
    public = public_lnk_path()
    if prefer_public and os.path.isfile(public):
        fields = _lnk_fields(public)
        target = (fields.get("TargetPath") or "").strip().strip('"')
        if not fields or not target or not os.path.isfile(target):
            # 读不到公共 lnk 的有效字段 → 绝不按空值写回(那会清空
            # TargetPath / Description 并虚报 appended)。直接走降级。
            result["reason"] = (
                "读不到公共桌面快捷方式的有效字段(TargetPath 为空或指向不存在的"
                "目标),为避免把快捷方式写坏,本次**未做任何写入**;")
        else:
            arguments = fields.get("Arguments", "")
            existing_port = _debug_port_in(arguments)
            if existing_port is not None:
                result.update({"ok": True, "action": "already-configured",
                               "lnk": public, "arguments": arguments,
                               "port": existing_port,
                               "reason": "公共桌面快捷方式已带调试端口参数"})
                return result
            backup, backup_error = _backup_lnk(public)
            result["backup"] = backup
            if backup_error:
                # 备份失败 → 中止写入(改坏了无法回滚)
                result["reason"] = (
                    f"备份失败({backup_error});为避免改坏后无法回滚,"
                    "本次**未修改**公共桌面快捷方式;")
            else:
                new_args = _append_debug_arg(arguments, port)
                try:
                    _write_lnk(
                        public,
                        target=target,
                        arguments=new_args,
                        working_directory=fields.get("WorkingDirectory", ""),
                        icon=fields.get("IconLocation", ""),
                        description=fields.get("Description", ""))
                except AppError as exc:
                    result["reason"] += (
                        f"公共桌面快捷方式不可写({exc}),已改为在用户桌面创建"
                        "带参数的启动器。")
                else:
                    result.update({"ok": True, "action": "appended",
                                   "changed": True, "lnk": public,
                                   "arguments": new_args})
                    return result
    elif prefer_public:
        result["reason"] = "公共桌面快捷方式不存在,改为在用户桌面创建带参数的启动器。"
    else:
        result["reason"] = "调用方要求不使用公共桌面快捷方式。"

    # 降级:用户桌面启动器
    created = _create_cli_launcher(port)
    result["degraded"] = True
    result["lnk"] = created.get("launcher")
    result["arguments"] = created.get("arguments", "")
    result["action"] = created.get("action", "none")
    result["changed"] = bool(created.get("changed"))
    result["ok"] = bool(created.get("ok"))
    if created.get("reason"):
        result["reason"] = (result["reason"] + " " + created["reason"]).strip()
    if created.get("target"):
        result["target"] = created["target"]
    return result


def remove_launcher(port: int = 0) -> Dict[str, Any]:
    """回滚 `configure_launcher()` 的改动(**只动本工具加的那部分**)。

    * 公共 lnk:先**校验 TargetPath**,再备份,最后去掉调试端口参数
      (其余参数原样保留);
    * 用户桌面启动器:仅当 Description 带本工具标记时才移除,且**走回收站**;
      不是本工具创建的一律不动。

    端口语义(`port`):
      * ``0``(默认)→ 去掉**所有** `--remote-debugging-port=<n>` 参数。默认
        就该这样:`configure_launcher()` 写进去的端口来自 `ensure_port()`,
        可能是 9333/9334/9335/9222/9223,用户按文档跑默认 `remove` 不能漏;
      * 显式端口 → 只去该端口,但**剥离结果与原文相同(即没改动)时
        `changed=False`**,并如实报告"未发现该端口参数",绝不虚报回滚成功。

    返回里的 `remaining_ports` 列出剥离后**仍残留**的调试端口参数(可能为空
    列表):只要它非空,就说明"没有完全回滚",调用方必须如实告知用户。

    返回里的 `clean` 是"启动入口**本来**就干净、无需回滚"的**实测**结论:
    仅在本次零改动、且两条 lnk 要么不存在、要么不含任何调试端口参数时为
    True。调用方**只有**在 `clean` 为 True 时才可宣称"本来就是干净的"。

    读不到公共 lnk 字段 / TargetPath 无效 / 备份失败时**一个字节都不写**
    并返回 `ok=False`;用户桌面启动器字段读不出来时同样返回 `ok=False`
    (无法确认归属,不得虚报"不是本工具创建的")。绝不按空值写回,也不虚报
    "已回滚"。
    """
    result: Dict[str, Any] = {
        "ok": True,
        "changed": False,
        "clean": False,
        "actions": [],
        "backup": None,
        "reason": "",
        "recycled": [],
        "remaining_ports": [],
        "rollback": "无需回滚:本操作本身就是回滚动作。",
    }
    public = public_lnk_path()
    public_clean = True
    if os.path.isfile(public):
        fields = _lnk_fields(public)
        arguments = fields.get("Arguments", "")
        new_args = _strip_debug_arg(arguments, port or None)
        result["remaining_ports"] = _debug_ports_in(new_args)
        # "本来就干净"必须是**实测**结论:字段读不出来时一律不算干净。
        public_clean = bool(fields) and not _debug_ports_in(arguments)
        if not fields:
            # 字段读不出来 → 连"有没有端口参数"都无法确认,更不能写回
            # (按空值写回会清空 TargetPath / Description,并虚报已回滚)。
            result["ok"] = False
            result["reason"] += (
                "读不到公共桌面快捷方式的字段(无法确认是否带调试端口参数),"
                "为避免把快捷方式写坏,本次**未做任何写入**。")
        elif new_args == arguments:
            # 没改动:要么本来就没有调试端口参数,要么指定的端口不在这条 lnk 里
            if port:
                result["reason"] += (
                    f"公共桌面快捷方式里没有 {DEBUG_PORT_FLAG}={int(port)} "
                    "参数,未做任何改动。")
            else:
                result["reason"] += "公共桌面快捷方式里没有调试端口参数。"
        else:
            target = (fields.get("TargetPath") or "").strip().strip('"')
            if not target or not os.path.isfile(target):
                # 顺序与 configure_launcher 一致:**先校验 TargetPath,再备份**。
                # 这条路径"未做任何写入",就不该先留下一个备份文件。
                result["ok"] = False
                result["reason"] += (
                    "读不到公共桌面快捷方式的有效 TargetPath(为空或指向"
                    "不存在的目标),为避免把快捷方式写坏,本次**未做任何写入**。")
            else:
                backup, backup_error = _backup_lnk(public)
                result["backup"] = backup
                if backup_error:
                    # 备份失败 → 中止写入(改坏了无法回滚)
                    result["ok"] = False
                    result["reason"] += (
                        f"备份失败({backup_error});为避免改坏后无法回滚,"
                        "本次**未修改**公共桌面快捷方式。")
                else:
                    try:
                        _write_lnk(
                            public,
                            target=target,
                            arguments=new_args,
                            working_directory=fields.get("WorkingDirectory", ""),
                            icon=fields.get("IconLocation", ""),
                            description=fields.get("Description", ""))
                    except AppError as exc:
                        result["ok"] = False
                        result["reason"] += f"公共桌面快捷方式回滚失败:{exc}"
                    else:
                        result["changed"] = True
                        result["actions"].append("removed-arg-from-public-lnk")
                        result["arguments"] = new_args
                        if result["remaining_ports"]:
                            result["reason"] += (
                                "已去掉目标端口,但快捷方式里仍残留其它调试端口参数:"
                                + ", ".join(str(p) for p in result["remaining_ports"])
                                + "(如需一并去掉,用 `launcher remove --port 0`)。")

    user = cli_launcher_path()
    user_clean = True
    if os.path.isfile(user):
        user_fields = _lnk_fields(user)
        if not user_fields:
            # 字段读不出来 → 连"是不是本工具建的"都无法确认:既不能回收,
            # 也不能虚报"不是本工具创建的"(那会让调用方误以为无需回滚)。
            user_clean = False
            result["ok"] = False
            result["reason"] += (
                f"读不到用户桌面启动器 {user} 的字段,"
                "无法确认该启动器归属,未做处理。")
        else:
            description = user_fields.get("Description") or ""
            user_clean = not _debug_ports_in(user_fields.get("Arguments", ""))
            if CLI_LAUNCHER_MARKER in description:
                if _recycle_file(user):
                    result["changed"] = True
                    result["actions"].append("recycled-user-launcher")
                    result["recycled"].append(user)
                else:
                    result["ok"] = False
                    result["reason"] += (
                        f"用户桌面启动器 {user} 移入回收站失败,已保留原文件"
                        "(本工具不做直接删除)。")
            else:
                result["reason"] += (
                    f"用户桌面启动器 {user} 不是本工具创建的,未做处理。")
    # 只有"确实确认干净"才允许调用方宣称"本来就是干净的":
    # 本次没有任何改动,且两条 lnk 要么不存在、要么不含任何调试端口参数。
    result["clean"] = bool(result["ok"] and not result["changed"]
                           and public_clean and user_clean)
    return result

