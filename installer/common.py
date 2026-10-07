"""安装 / 卸载的公共逻辑（只依赖标准库，安装包体积更小）。

设计
----
* **只有一个 EXE**：``BaiAi-Tavern V0.2.2.exe`` 既是安装程序，也负责卸载 ——
  安装后在「Windows 设置 → 应用」里点卸载，或再次运行安装程序选择「卸载」都能卸干净；
* 默认安装到 ``%LOCALAPPDATA%\\Programs\\BaiAi-Tavern``（按用户安装，不需要管理员权限）；
* 桌面与开始菜单快捷方式默认都创建，可在安装界面取消；
* 卸载信息写在 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\BaiAi-Tavern``，
  卸载命令指向安装目录里的主程序（``BaiAi-Tavern.exe --uninstall``）。

用户数据（``<安装目录>\\data``、``config.yaml``）在卸载时默认保留，
只有用户勾选「同时删除我的配置与数据」才会一起删掉。
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

APP_NAME = "BaiAi-Tavern"
APP_VERSION = "V0.2.2"
APP_AUTHOR = "baiai.org"
APP_HOMEPAGE = "https://baiai.org"
APP_DESCRIPTION = "QQ 多角色 AI 主动消息桌面应用（接入 QQ 官方机器人）"

EXE_NAME = "BaiAi-Tavern.exe"
BOT_EXE_NAME = "bot.exe"
LAUNCHER_NAME = "启动.bat"

#: 旧版本留下的独立卸载程序与 NapCat 目录，安装/卸载时顺手清理掉
LEGACY_UNINSTALLER_NAME = "卸载 BaiAi-Tavern.exe"
LEGACY_DIR_NAMES = ("napcat",)

_DEFAULT_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BaiAi-Tavern"
#: 卸载信息所在注册表项。可用环境变量 ``BAIAI_UNINSTALL_KEY`` 覆盖 ——
#: 自检就靠它把安装/卸载指到临时项上，不会碰到机器上真实的安装。
REG_PATH = os.environ.get("BAIAI_UNINSTALL_KEY") or _DEFAULT_REG_PATH
KEEP_NAMES = ("data", "config.yaml")

ProgressFn = Callable[[int, str], None]


# ============================================================== 路径 ========
def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / APP_NAME


def self_exe_path() -> Optional[Path]:
    """当前正在运行的程序路径（打包后就是 exe；源码模式下返回 None）。"""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable)


def install_dir_for_self() -> Path:
    """当前程序所在的安装目录（主程序自己执行卸载时用）。"""
    exe = self_exe_path()
    if exe is not None:
        return exe.parent
    entry = read_uninstall_entry()
    if entry.get("InstallLocation"):
        return Path(str(entry["InstallLocation"]))
    return default_install_dir()


def desktop_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"


def startmenu_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def payload_dir() -> Path:
    """安装包内携带的程序文件目录。

    可用环境变量 ``BAIAI_PAYLOAD`` 覆盖（组装脚本与自检用）。
    """
    override = os.environ.get("BAIAI_PAYLOAD")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "payload"
        if candidate.is_dir():
            return candidate
        return Path(sys.executable).parent / "payload"
    # 开发模式：用组装脚本产出的目录
    root = Path(__file__).resolve().parent.parent
    return root / "build" / "payload"


# ============================================================== 快捷方式 ====
#: 最近一次创建快捷方式使用的方式（``com`` / ``binary`` / ``failed``），仅供日志与自检查看。
LAST_SHORTCUT_METHOD = ""

_LNK_CLSID = bytes([0x01, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46])
_LNK_FLAG_HAS_LINK_INFO = 0x00000002
_LNK_FLAG_HAS_NAME = 0x00000004
_LNK_FLAG_HAS_RELATIVE_PATH = 0x00000008
_LNK_FLAG_HAS_WORKING_DIR = 0x00000010
_LNK_FLAG_HAS_ARGUMENTS = 0x00000020
_LNK_FLAG_HAS_ICON_LOCATION = 0x00000040
_LNK_FLAG_IS_UNICODE = 0x00000080


def _ansi_bytes(text: str) -> bytes:
    """按系统本地编码转 ANSI（快捷方式里的 ANSI 字段用得上）。"""
    for encoding in ("mbcs", "gbk", "utf-8"):
        try:
            return text.encode(encoding, "replace")
        except (LookupError, UnicodeError):
            continue
    return text.encode("utf-8", "replace")


def _lnk_string_block(text: str) -> bytes:
    """快捷方式 StringData 里的一个字符串（UTF-16LE，含长度与结尾空字符）。"""
    return struct.pack("<H", len(text) + 1) + text.encode("utf-16-le") + b"\x00\x00"


def build_lnk_bytes(
    target: Path,
    arguments: str = "",
    workdir: Optional[Path] = None,
    icon: Optional[Path] = None,
    description: str = "",
) -> bytes:
    """按 MS-SHLLINK 规范生成一个最小 .lnk 内容（不依赖 COM）。

    这是**兜底方案**：个别机器上安全软件会拦掉 ``WScript.Shell`` 的 ``Save()``
    （返回 E_FAIL），这时直接用文件写入把快捷方式落盘。
    注意这种最小结构没有 ITEMIDLIST，个别 Windows 版本可能不认，
    所以 :func:`create_shortcut` 写完后会用系统接口读回目标验证，不认就删掉重试。
    """
    target = Path(target)
    workdir = Path(workdir) if workdir else target.parent
    icon = Path(icon) if icon else target

    flags = (
        _LNK_FLAG_HAS_LINK_INFO
        | _LNK_FLAG_IS_UNICODE
        | _LNK_FLAG_HAS_RELATIVE_PATH
        | _LNK_FLAG_HAS_WORKING_DIR
    )
    if description:
        flags |= _LNK_FLAG_HAS_NAME
    if arguments:
        flags |= _LNK_FLAG_HAS_ARGUMENTS
    if icon:
        flags |= _LNK_FLAG_HAS_ICON_LOCATION

    try:
        file_size = target.stat().st_size if target.exists() else 0
    except OSError:
        file_size = 0

    header = (
        struct.pack("<I", 0x4C)
        + _LNK_CLSID
        + struct.pack("<I", flags)
        + struct.pack("<I", 0x20)  # FILE_ATTRIBUTE_ARCHIVE
        + struct.pack("<Q", 0)
        + struct.pack("<Q", 0)
        + struct.pack("<Q", 0)
        + struct.pack("<I", file_size)
        + struct.pack("<I", 0)  # IconIndex
        + struct.pack("<I", 1)  # SW_SHOWNORMAL
        + struct.pack("<H", 0)  # HotKey
        + struct.pack("<H", 0)
        + struct.pack("<I", 0)
        + struct.pack("<I", 0)
    )

    base = str(target.parent)
    if not base.endswith("\\"):
        base += "\\"
    suffix = target.name
    volume_id = struct.pack("<III", 3, 0, 0x10) + b"\x00"  # 固定磁盘，无卷标
    volume_block = struct.pack("<I", len(volume_id)) + volume_id
    ansi_base = _ansi_bytes(base) + b"\x00"
    ansi_suffix = _ansi_bytes(suffix) + b"\x00"
    wide_base = (base + "\x00").encode("utf-16-le")
    wide_suffix = (suffix + "\x00").encode("utf-16-le")

    volume_offset = 0x24
    base_offset = volume_offset + len(volume_block)
    suffix_offset = base_offset + len(ansi_base)
    base_wide_offset = suffix_offset + len(ansi_suffix)
    suffix_wide_offset = base_wide_offset + len(wide_base)
    body = volume_block + ansi_base + ansi_suffix + wide_base + wide_suffix
    link_info = struct.pack(
        "<IIIIIIIII",
        0x24 + len(body),
        0x24,
        0x01,  # VolumeIDAndLocalBasePath
        volume_offset,
        base_offset,
        0,  # CommonNetworkRelativeLinkOffset
        suffix_offset,
        base_wide_offset,
        suffix_wide_offset,
    ) + body

    strings = b""
    if flags & _LNK_FLAG_HAS_NAME:
        strings += _lnk_string_block(description)
    strings += _lnk_string_block(target.name)
    strings += _lnk_string_block(str(workdir))
    if flags & _LNK_FLAG_HAS_ARGUMENTS:
        strings += _lnk_string_block(arguments)
    if flags & _LNK_FLAG_HAS_ICON_LOCATION:
        strings += _lnk_string_block(str(icon))

    return header + link_info + strings + struct.pack("<I", 0)  # ExtraData 结束块


def write_lnk(
    lnk_path: Path,
    target: Path,
    arguments: str = "",
    workdir: Optional[Path] = None,
    icon: Optional[Path] = None,
    description: str = "",
) -> bool:
    """不依赖 COM，直接写一个 .lnk 文件（兜底方案，见 :func:`build_lnk_bytes`）。"""
    try:
        lnk_path.parent.mkdir(parents=True, exist_ok=True)
        data = build_lnk_bytes(target, arguments, workdir, icon, description)
        with open(str(lnk_path), "wb") as handle:
            handle.write(data)
        return lnk_path.is_file() and lnk_path.stat().st_size > 0
    except Exception:
        return False


def lnk_target(lnk_path: Path) -> str:
    """读回快捷方式指向的目标（用系统自带的 WScript.Shell）。

    读不回来（或者返回空）说明这个 .lnk 不能被系统识别，等于失效快捷方式。
    """
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "try { $sc = $ws.CreateShortcut('%s'); $sc.TargetPath } catch { '' }"
        % str(lnk_path).replace("'", "''")
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            timeout=40,
            creationflags=0x08000000,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return (result.stdout or b"").decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return ""


def create_shortcut(
    lnk_path: Path,
    target: Path,
    arguments: str = "",
    workdir: Optional[Path] = None,
    icon: Optional[Path] = None,
    description: str = "",
) -> bool:
    """创建 .lnk 快捷方式。

    先用系统自带的 ``WScript.Shell``（COM，最标准）；少数机器上 COM 保存会被
    安全软件拦掉，这时退回直接写文件的方式。两条路都失败就返回 ``False``，
    并删掉半成品，避免留下点不开的快捷方式。
    """
    global LAST_SHORTCUT_METHOD
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    workdir = workdir or target.parent
    icon = icon or target
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut('%s'); "
        "$sc.TargetPath = '%s'; "
        "$sc.Arguments = '%s'; "
        "$sc.WorkingDirectory = '%s'; "
        "$sc.IconLocation = '%s'; "
        "$sc.Description = '%s'; "
        "$sc.Save()"
    ) % (
        str(lnk_path).replace("'", "''"),
        str(target).replace("'", "''"),
        str(arguments or "").replace("'", "''"),
        str(workdir).replace("'", "''"),
        str(icon).replace("'", "''"),
        str(description or APP_NAME).replace("'", "''"),
    )
    for attempt in (1, 2):
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True,
                timeout=40,
                creationflags=0x08000000,
            )
        except Exception:
            continue
        if result.returncode == 0 and lnk_path.exists():
            LAST_SHORTCUT_METHOD = "com"
            return True
    # 兜底：直接按 MS-SHLLINK 写文件，并确认系统能读回目标
    if write_lnk(lnk_path, target, arguments, workdir, icon, description):
        resolved = lnk_target(lnk_path)
        if resolved and Path(resolved).name.lower() == Path(target).name.lower():
            LAST_SHORTCUT_METHOD = "binary"
            return True
        try:
            lnk_path.unlink()
        except OSError:
            pass
    LAST_SHORTCUT_METHOD = "failed"
    return False


# ============================================================== 注册表 ======
def _winreg():
    if os.name != "nt":  # pragma: no cover
        return None
    try:
        import winreg  # type: ignore[import-not-found]

        return winreg
    except Exception:  # pragma: no cover
        return None


def directory_size_kb(directory: Path) -> int:
    total = 0
    try:
        for path in directory.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except Exception:
                    continue
    except Exception:  # pragma: no cover
        return 0
    return int(total / 1024)


def write_uninstall_entry(
    install_dir: Path,
    shortcuts: List[Path],
    reg_path: str = REG_PATH,
) -> bool:
    winreg = _winreg()
    if winreg is None:  # pragma: no cover
        return False
    exe = install_dir / EXE_NAME
    icon = exe
    values = {
        "DisplayName": APP_NAME,
        "DisplayVersion": APP_VERSION,
        "Publisher": APP_AUTHOR,
        "DisplayIcon": "%s,0" % icon,
        "InstallLocation": str(install_dir),
        # 卸载由主程序自己完成：不需要单独的卸载 EXE
        "UninstallString": '"%s" --uninstall' % exe,
        "QuietUninstallString": '"%s" --uninstall --silent' % exe,
        "InstallDate": time.strftime("%Y%m%d"),
        "EstimatedSize": directory_size_kb(install_dir),
        "NoModify": 1,
        "NoRepair": 1,
        "HelpLink": APP_HOMEPAGE,
        "URLInfoAbout": APP_HOMEPAGE,
        "Shortcuts": ";".join(str(item) for item in shortcuts),
    }
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_WRITE
        ) as key:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ if isinstance(value, str) else winreg.REG_DWORD, value)
        return True
    except Exception:  # pragma: no cover
        return False


def _decode_console(data: bytes) -> str:
    for encoding in ("mbcs", "gbk", "utf-8"):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def reg_exe_read(reg_path: str) -> Dict[str, Any]:
    """用系统自带的 reg.exe 读一遍卸载信息。

    ``winreg`` 与 ``reg.exe`` 在个别环境里会落在**不同的注册表视图**上
    （典型是 32/64 位视图不同，或系统对某些进程做了注册表虚拟化），
    只靠 ``winreg`` 会出现「明明装好了却读不到卸载信息」——所以再兜底读一次。
    """
    try:
        result = subprocess.run(
            ["reg", "query", "HKCU\\" + reg_path],
            capture_output=True,
            timeout=30,
            creationflags=0x08000000,
        )
    except Exception:  # pragma: no cover
        return {}
    if result.returncode != 0:
        return {}
    values: Dict[str, Any] = {}
    for line in _decode_console(result.stdout or b"").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[1].startswith("REG_"):
            continue
        name, kind, value = parts
        if kind == "REG_DWORD":
            try:
                value = int(value, 16)
            except ValueError:
                pass
        values[name] = value
    return values


def reg_exe_delete(reg_path: str) -> bool:
    """用 reg.exe 删除卸载信息（两个注册表视图各删一次，覆盖视图差异）。"""
    ok = False
    for view in ("/reg:64", "/reg:32"):
        try:
            result = subprocess.run(
                ["reg", "delete", "HKCU\\" + reg_path, "/f", view],
                capture_output=True,
                timeout=30,
                creationflags=0x08000000,
            )
        except Exception:  # pragma: no cover
            continue
        ok = ok or result.returncode == 0
    return ok


def read_uninstall_entry(reg_path: str = REG_PATH) -> Dict[str, Any]:
    values = read_uninstall_entry_winreg(reg_path)
    if values:
        return values
    return reg_exe_read(reg_path)


def read_uninstall_entry_winreg(reg_path: str) -> Dict[str, Any]:
    """只用 winreg 读一遍（诊断「视图差异」时对照 reg_exe_read 用）。"""
    winreg = _winreg()
    if winreg is None:  # pragma: no cover
        return {}
    values: Dict[str, Any] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = value
                index += 1
    except Exception:
        return {}
    return values


def remove_uninstall_entry(reg_path: str = REG_PATH) -> bool:
    """删除卸载信息（两个注册表视图都清一遍，避免残留「已卸载却还显示在应用列表里」）。"""
    removed = False
    winreg = _winreg()
    if winreg is not None:
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, reg_path, 0, 0)
            removed = True
        except FileNotFoundError:
            removed = True
        except Exception:
            pass
    if reg_exe_read(reg_path):
        removed = reg_exe_delete(reg_path) or removed
    return removed or not reg_exe_read(reg_path)


# ========================================================= 正在运行的程序 =====
def current_process_tree_pids() -> List[int]:
    """当前进程 + 父进程的 pid。

    打包成单文件 exe 时，父进程是 PyInstaller 的引导程序，它同样位于安装目录里，
    卸载（主程序 ``--uninstall``）时绝不能把它当成"需要关掉的旧版本"给杀了，
    否则刚启动的卸载流程会被自己打断。
    """
    pids = [os.getpid()]
    try:
        pids.append(os.getppid())
    except Exception:  # pragma: no cover
        pass
    return [pid for pid in pids if int(pid) > 0]


def running_processes_in(directory: Path, exclude_pids: Optional[List[int]] = None) -> List[int]:
    """找出可执行文件位于 ``directory`` 里的正在运行的进程 id。

    升级安装时如果上一版还在运行（界面或 ``bot.exe``），它的 exe 会被系统锁住，
    复制就会出现 ``Permission denied``。安装前需要先把它们关掉。
    """
    directory = Path(directory)
    if not directory.exists():
        return []
    script = (
        "$dir = '%s'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($dir, 'OrdinalIgnoreCase') } | "
        "Select-Object -ExpandProperty ProcessId"
        % str(directory).replace("'", "''")
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            timeout=40,
            creationflags=0x08000000,
        )
    except Exception:  # pragma: no cover
        return []
    if result.returncode != 0:
        return []
    skip = {int(item) for item in (exclude_pids or [])}
    pids: List[int] = []
    for line in (result.stdout or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.isdigit():
            pid = int(line)
            if pid not in skip:
                pids.append(pid)
    return pids


def stop_running_app(directory: Path, exclude_pids: Optional[List[int]] = None) -> List[str]:
    """关掉安装目录里正在运行的程序，返回被结束的进程说明。

    只结束**位于该目录内**的进程，不会波及用户其它程序；结束失败时返回空列表，
    由调用方给出可读的错误提示。
    """
    stopped: List[str] = []
    for pid in running_processes_in(directory, exclude_pids=exclude_pids):
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=30,
                creationflags=0x08000000,
            )
        except Exception:  # pragma: no cover
            continue
        if result.returncode == 0:
            stopped.append("pid %d" % pid)
    if stopped:
        # 给系统一点时间释放文件句柄
        time.sleep(0.8)
    return stopped


# ============================================================== 安装 ========
def copy_payload(
    source: Path,
    target: Path,
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    """把程序文件复制到安装目录（保留用户已有的 data/config.yaml）。

    单个文件失败会短暂重试（目标是杀毒软件扫描/句柄尚未释放这类瞬时占用），
    仍然失败则在错误里说明原因并提示先退出程序。
    """
    files: List[Path] = []
    for path in sorted(source.rglob("*")):
        if path.is_file():
            files.append(path)
    total = len(files) or 1
    copied = 0
    failed: List[str] = []
    target.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(files, 1):
        relative = path.relative_to(source)
        destination = target / relative
        error: Optional[Exception] = None
        for attempt in range(3):
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                error = None
                break
            except Exception as exc:
                error = exc
                time.sleep(0.6 * (attempt + 1))
        if error is None:
            copied += 1
        else:
            hint = ""
            if isinstance(error, PermissionError):
                hint = "（文件被占用：请先完全退出 %s —— 含托盘图标右键退出，然后再试；" \
                       "安装程序已尝试自动关闭它）" % APP_NAME
            failed.append("%s：%s%s" % (relative, error, hint))
        if progress is not None and (index % 5 == 0 or index == total):
            progress(int(index * 100 / total), "正在复制 %s" % relative.name)
    if progress is not None:
        progress(100, "文件复制完成")
    return {"copied": copied, "failed": failed}


def install(
    install_dir: Optional[Path] = None,
    create_desktop: bool = True,
    create_startmenu: bool = True,
    progress: Optional[ProgressFn] = None,
    desktop_path: Optional[Path] = None,
    startmenu_path: Optional[Path] = None,
    reg_path: str = REG_PATH,
) -> Dict[str, Any]:
    """执行安装：关掉正在运行的旧版本 → 复制文件 → 建快捷方式 → 写卸载信息。"""
    source = payload_dir()
    target = Path(install_dir) if install_dir else default_install_dir()
    if not source.is_dir():
        return {"ok": False, "error": "安装包缺少 payload 目录：%s" % source}

    # 安装目录里还在运行的旧版本会锁住 exe（bot.exe / 主程序），
    # 必须先关掉，否则复制会报 Permission denied。
    if progress is not None:
        progress(2, "正在检查是否有正在运行的程序…")
    stopped = stop_running_app(target, exclude_pids=current_process_tree_pids())

    result = copy_payload(source, target, progress)
    if result["failed"]:
        message = "以下文件复制失败：\n%s" % "\n".join(result["failed"][:6])
        return {
            "ok": False,
            "error": message,
            "stopped": stopped,
        }

    exe = target / EXE_NAME
    if not exe.exists():
        return {"ok": False, "error": "复制完成但缺少 %s，安装包可能不完整" % EXE_NAME}

    shortcuts: List[Path] = []
    if create_desktop:
        lnk = (desktop_path or desktop_dir()) / ("%s.lnk" % APP_NAME)
        if create_shortcut(lnk, exe, workdir=target, description=APP_DESCRIPTION):
            shortcuts.append(lnk)
    if create_startmenu:
        folder = startmenu_path or startmenu_dir()
        lnk = folder / APP_NAME / ("%s.lnk" % APP_NAME)
        if create_shortcut(lnk, exe, workdir=target, description=APP_DESCRIPTION):
            shortcuts.append(lnk)

    write_uninstall_entry(target, shortcuts, reg_path=reg_path)
    cleaned = clean_legacy_files(target)
    if progress is not None:
        progress(100, "安装完成")
    return {
        "ok": True,
        "install_dir": str(target),
        "shortcuts": [str(item) for item in shortcuts],
        "copied": result["copied"],
        "cleaned": cleaned,
        "stopped": stopped,
    }


def clean_legacy_files(install_dir: Path) -> List[str]:
    """清理旧版本遗留的文件：独立卸载程序、NapCat 目录（第三方组件已不再支持）。"""
    removed: List[str] = []
    targets = [install_dir / LEGACY_UNINSTALLER_NAME]
    for name in LEGACY_DIR_NAMES:
        targets.append(install_dir / name)
    for path in targets:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path.name)
            elif path.exists():
                path.unlink()
                removed.append(path.name)
        except Exception:
            continue
    return removed


def is_installed(reg_path: str = REG_PATH) -> bool:
    entry = read_uninstall_entry(reg_path)
    if not entry:
        return False
    target = Path(str(entry.get("InstallLocation") or ""))
    return bool(target and (target / EXE_NAME).exists())


def installed_info(reg_path: str = REG_PATH) -> Dict[str, Any]:
    """已安装时的信息（安装向导用来显示「已安装 V0.2.2」并提供卸载）。"""
    entry = read_uninstall_entry(reg_path)
    if not entry:
        return {}
    target = Path(str(entry.get("InstallLocation") or default_install_dir()))
    return {
        "installed": bool((target / EXE_NAME).exists()),
        "install_dir": str(target),
        "version": str(entry.get("DisplayVersion") or ""),
        "publisher": str(entry.get("Publisher") or ""),
    }


# ============================================================== 卸载 ========
def _write_cleanup_batch(install_dir: Path, self_exe: Path, remove_data: bool, reg_path: str) -> Optional[Path]:
    """把「延迟清理」写成一个小 .bat 文件。

    为什么必须用批处理：直接把一整串命令交给 ``cmd /c`` 是不行的，
    ``subprocess`` 会按 C 运行库规则加引号，cmd.exe 拿到后会把引号认错，
    报「文件名、目录名或卷标语法不正确」，于是文件删不掉、目录也留了下来。

    另一个现实问题：正在运行的主程序删不掉自己，所以要等本进程退出后再删，
    因此这里带**重试**（最多约 30 秒），而不是简单地等两秒。
    """
    try:
        batch_path = Path(tempfile.gettempdir()) / (
            "baiai-uninstall-cleanup-%d-%d.bat" % (os.getpid(), int(time.time() * 1000) % 100000)
        )
        lines = [
            "@echo off",
            "setlocal",
            "ping -n 3 127.0.0.1 >nul",  # 先等本进程退出
            "set /a tries=0",
            ":wait_exit",
            'del /f /q "%s" >nul 2>nul' % self_exe,
            'if exist "%s" (' % self_exe,
            "  set /a tries+=1",
            "  if %tries% lss 15 ( ping -n 2 127.0.0.1 >nul & goto wait_exit )",
            ")",
            ('rd /s /q "%s"' % install_dir) if remove_data else ('rd "%s" >nul 2>nul' % install_dir),
            'reg delete "HKCU\\%s" /f >nul 2>nul' % reg_path,
            # 顺手删掉自己：先 (goto) 一下让 cmd 释放批处理文件句柄
            '(goto) 2>nul & del /f /q "%~f0" >nul 2>nul',
        ]
        text = "\r\n".join(lines) + "\r\n"
        try:
            batch_path.write_text(text, encoding="mbcs")
        except (LookupError, UnicodeEncodeError):
            batch_path.write_text(text, encoding="utf-8")
        return batch_path
    except Exception:  # pragma: no cover
        return None


def _schedule_self_delete(
    install_dir: Path,
    self_exe: Path,
    remove_data: bool = False,
    reg_path: str = REG_PATH,
) -> None:
    """正在运行的程序删不掉自己：交给一个延迟批处理收尾。

    延迟命令做三件事：删掉正在运行的主程序、按需删除安装目录、
    **再删一次卸载信息**（双保险，避免「卸载完还留在设置→应用里」）。
    """
    batch_path = _write_cleanup_batch(install_dir, self_exe, remove_data, reg_path)
    if batch_path is None:  # pragma: no cover
        return
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", str(batch_path)],
            creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS | CREATE_NO_WINDOW
        )
    except Exception:  # pragma: no cover
        pass


def uninstall(
    install_dir: Optional[Path] = None,
    remove_data: bool = False,
    progress: Optional[ProgressFn] = None,
    reg_path: str = REG_PATH,
    self_exe: Optional[Path] = None,
) -> Dict[str, Any]:
    """执行卸载：删卸载信息 → 删快捷方式 → 删文件（可选保留用户数据）。

    ``self_exe`` 是**调用方自己正在运行的程序**（主程序 ``--uninstall`` 时传入）：
    它删不掉自己，会留给延迟批处理收尾。安装程序代为卸载时传 ``None``，
    此时所有文件都在前台删干净。
    """
    entry = read_uninstall_entry(reg_path)
    target = Path(install_dir) if install_dir else Path(str(entry.get("InstallLocation") or default_install_dir()))
    running = Path(self_exe) if self_exe else None
    if running is not None and running.parent != target:
        running = None
    if not target.exists() and not entry:
        return {"ok": False, "error": "没有找到安装信息（可能已经卸载过了）"}

    # 目录里可能还有别的进程在跑（例如旧界面的 bot.exe），先关掉再删
    stopped = stop_running_app(target, exclude_pids=current_process_tree_pids())

    removed: List[str] = []
    errors: List[str] = []

    # 卸载信息**最先删**：任何一步失败都不该让它留在「设置 → 应用」里。
    # （之前走图形卸载时这里排在延迟自删除之后，被提前 return 跳过了，
    #  结果就是快捷方式和文件都没了、应用列表里却还在。）
    entry_removed = remove_uninstall_entry(reg_path)

    # 快捷方式
    shortcut_values = str(entry.get("Shortcuts") or "").split(";")
    candidates = [Path(item) for item in shortcut_values if item.strip()]
    candidates.append(desktop_dir() / ("%s.lnk" % APP_NAME))
    startmenu_folder = startmenu_dir() / APP_NAME
    candidates.append(startmenu_folder / ("%s.lnk" % APP_NAME))
    candidates.append(startmenu_folder / ("%s.lnk" % LEGACY_UNINSTALLER_NAME.replace(".exe", "")))
    for item in candidates:
        try:
            if item.is_file():
                item.unlink()
                removed.append(str(item))
        except Exception as exc:
            errors.append("删除快捷方式失败 %s：%s" % (item, exc))
    try:
        if startmenu_folder.is_dir() and not any(startmenu_folder.iterdir()):
            startmenu_folder.rmdir()
            removed.append(str(startmenu_folder))
    except Exception:
        pass

    # 程序文件（保留用户数据；正在运行的主程序自己删不掉，留给延迟命令）
    keep = {name.lower() for name in KEEP_NAMES} if not remove_data else set()
    deferred_self = bool(running is not None and running.is_file())
    if target.is_dir():
        for child in sorted(target.iterdir()):
            try:
                name = child.name.lower()
                if name in keep:
                    continue
                if deferred_self and name == running.name.lower():
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=False)
                else:
                    child.unlink()
                removed.append(str(child))
            except Exception as exc:
                errors.append("删除失败 %s：%s" % (child, exc))

    if deferred_self:
        # 需要延迟收尾：立刻返回，但要保证卸载信息已经删掉了
        remove_uninstall_entry(reg_path)
        _schedule_self_delete(target, running, remove_data=remove_data, reg_path=reg_path)
        if progress is not None:
            progress(100, "正在清理程序文件…")
        return {
            "ok": True,
            "install_dir": str(target),
            "removed": removed[:20],
            "errors": errors,
            "deferred": True,
            "registry_removed": entry_removed or not read_uninstall_entry(reg_path),
            "data_kept": not remove_data,
            "stopped": stopped,
        }

    if target.is_dir():
        if remove_data:
            for name in KEEP_NAMES:
                path = target / name
                try:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    elif path.is_file():
                        path.unlink()
                except Exception:
                    pass
        try:
            if not any(target.iterdir()):
                target.rmdir()
                removed.append(str(target))
        except Exception:
            pass
    remove_uninstall_entry(reg_path)
    if progress is not None:
        progress(100, "卸载完成")
    return {
        "ok": True,
        "install_dir": str(target),
        "removed": removed[:20],
        "errors": errors,
        "deferred": False,
        "data_kept": not remove_data,
    }


__all__ = [
    "APP_AUTHOR",
    "APP_DESCRIPTION",
    "APP_HOMEPAGE",
    "APP_NAME",
    "APP_VERSION",
    "BOT_EXE_NAME",
    "EXE_NAME",
    "KEEP_NAMES",
    "LAST_SHORTCUT_METHOD",
    "LAUNCHER_NAME",
    "LEGACY_UNINSTALLER_NAME",
    "REG_PATH",
    "build_lnk_bytes",
    "clean_legacy_files",
    "copy_payload",
    "create_shortcut",
    "default_install_dir",
    "desktop_dir",
    "directory_size_kb",
    "install",
    "install_dir_for_self",
    "installed_info",
    "is_installed",
    "lnk_target",
    "payload_dir",
    "read_uninstall_entry",
    "read_uninstall_entry_winreg",
    "reg_exe_delete",
    "reg_exe_read",
    "remove_uninstall_entry",
    "running_processes_in",
    "self_exe_path",
    "current_process_tree_pids",
    "stop_running_app",
    "startmenu_dir",
    "uninstall",
    "write_lnk",
    "write_uninstall_entry",
]
