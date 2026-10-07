"""更新系统核心逻辑：从 GitHub Releases 拉取最新安装包并静默更新。

设计
----
* **更新源**：本项目的 GitHub Releases（``baiai-org/BaiAi-Tavern``）。
  Release 附件里唯一的 ``BaiAi-Tavern*.exe`` 就是单文件安装包
  （安装 + 卸载都在里面，另附 ``SHA256SUMS.txt`` 校验值）。
* **更新流程**：下载新安装包（进度回调）→ 有 ``SHA256SUMS.txt`` 时校验 SHA256 →
  运行 ``<新安装包> --silent --dir <当前安装目录>``（装完自动启动新版本）→ 旧版本退出。
  安装程序本身零改动：它本来就支持静默安装，且会先关掉安装目录里正在运行的旧版本。
* **提示策略**（持久化在 ``config.yaml`` 的 ``app.*``）：
  * ``update_check_enabled``（默认 true）：false = 用户选了「不再提示」，启动不再自动检查，
    但界面里随时可以手动检查；
  * ``update_skipped_version``：「跳过此版本」——该版本仍是最新时启动不再提示；
  * ``update_last_check``：启动自动检查限流（默认 6 小时内不重复），手动检查不受限。
* 本模块**不依赖 Qt**，自检（``tests.installer_smoke``）可直接用；
  界面在 :mod:`app.lifecycle`，启动检查在 :mod:`app.main_window`。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from common.logging_setup import get_logger

log = get_logger("app.updater")

#: 本项目的 GitHub 仓库（更新源）
REPO = "baiai-org/BaiAi-Tavern"
DEFAULT_API_BASE = "https://api.github.com"
RELEASES_URL = "https://github.com/%s/releases" % REPO
LATEST_RELEASE_URL = RELEASES_URL + "/latest"

#: 主程序 EXE 名（Release 附件里不是安装包的那个）
MAIN_EXE_NAME = "BaiAi-Tavern.exe"

#: 启动自动检查的限流窗口（秒）：频繁重启时不去撞 GitHub API 限速
AUTO_CHECK_INTERVAL = 6 * 3600

#: 更新下载目录（%TEMP%\\baiai-update），装完可手动清理
UPDATE_DIR_NAME = "baiai-update"

ProgressFn = Callable[[int, Optional[int]], None]  # (已下载字节, 总字节或 None)


class UpdateError(Exception):
    """更新流程里可以直接给用户看的错误。"""


# ============================================================== 版本解析 ========
def parse_version(text: Any) -> Tuple[int, ...]:
    """从任意版本字符串里抽出数字段：``"V0.2"`` / ``"v0.10.1"`` → 数字元组。

    抽不到数字返回空元组（调用方按「无法比较、不算新版」处理）。
    """
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(item) for item in parts)


def normalize_version(text: Any) -> str:
    """``"v0.3"`` → ``"V0.3"``（界面显示统一用大写 V 开头）。"""
    match = re.search(r"v?(\d+(?:\.\d+)*)", str(text or ""), re.IGNORECASE)
    if not match:
        return str(text or "")
    return "V" + match.group(1)


def is_newer(latest: Any, current: Any) -> bool:
    """latest 是否比 current 新（按数字段比较，0.10 > 0.9；无法比较时返回 False）。"""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    return a > b


# ============================================================== 提示策略 ========
def should_auto_check(config_get: Callable[[str, Any], Any], now: Optional[float] = None) -> bool:
    """启动时是否值得自动检查一次（6 小时限流，避免频繁重启撞 API 限速）。

    ``config_get`` 是 ``ConfigManager.get`` 风格的键值读取函数。
    """
    raw = str(config_get("app.update_last_check", "") or "")
    if not raw:
        return True
    try:
        last = time.mktime(time.strptime(raw[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return True
    return ((now or time.time()) - last) >= AUTO_CHECK_INTERVAL


def record_check(manager) -> None:
    """把当前时间写进 ``app.update_last_check``（manager 是 ConfigManager 本身）。"""
    try:
        manager.patch({"app": {"update_last_check": time.strftime("%Y-%m-%d %H:%M:%S")}}, persist=True)
    except Exception as exc:  # pragma: no cover - 限流标记写失败不影响功能
        log.debug("写入 update_last_check 失败：%s", exc)


# ============================================================== GitHub 接口 ======
def _http_get(url: str, timeout: float = 20.0) -> httpx.Response:
    headers = {
        "User-Agent": "BaiAi-Tavern-Updater",
        "Accept": "application/vnd.github+json",
    }
    try:
        response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise UpdateError("网络请求失败（%s）" % _short(exc)) from exc
    return response


def latest_release(api_base: str = DEFAULT_API_BASE, timeout: float = 20.0) -> Dict[str, Any]:
    """拉取最新 Release（含 assets）。失败抛 :class:`UpdateError`（文案可直接给用户看）。"""
    url = "%s/repos/%s/releases/latest" % (api_base.rstrip("/"), REPO)
    response = _http_get(url, timeout=timeout)
    if response.status_code == 404:
        raise UpdateError("没找到 Release（仓库还是空的？去 %s 发一个吧）" % RELEASES_URL)
    if response.status_code == 403:
        raise UpdateError("GitHub API 限流中（未登录每小时 60 次），稍后再试")
    if response.status_code != 200:
        raise UpdateError("GitHub 返回 %d" % response.status_code)
    try:
        data = response.json()
    except Exception as exc:
        raise UpdateError("Release 内容解析失败") from exc
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise UpdateError("Release 信息不完整（缺 tag_name）")
    return data


def pick_installer_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 Release 附件里挑安装包：``BaiAi-Tavern`` 开头、.exe 结尾、不是主程序本身。

    多个候选时优先带版本号的文件名（``BaiAi-Tavern-V0.3.exe``）。
    """
    assets = release.get("assets") or []
    candidates: List[Dict[str, Any]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        lower = name.lower()
        if not lower.endswith(".exe") or lower == MAIN_EXE_NAME.lower():
            continue
        if not lower.startswith("baiai-tavern"):
            continue
        candidates.append(asset)
    if not candidates:
        return None
    candidates.sort(key=lambda item: 0 if parse_version(str(item.get("name") or "")) else 1)
    return candidates[0]


def pick_sums_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and str(asset.get("name") or "").lower() == "sha256sums.txt":
            return asset
    return None


def check_latest(
    current_version: str,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """一次检查：最新 Release + 是否比当前版本新 + 安装包附件。

    返回::

        {"ok": True, "release": {...}, "latest_tag": "v0.3", "latest_display": "V0.3",
         "current_display": "V0.2", "newer": True, "asset": {...} | None}
    """
    release = latest_release(api_base=api_base, timeout=timeout)
    tag = str(release.get("tag_name") or "")
    return {
        "ok": True,
        "release": release,
        "latest_tag": tag,
        "latest_display": normalize_version(tag),
        "current_display": normalize_version(current_version),
        "newer": is_newer(tag, current_version),
        "asset": pick_installer_asset(release),
        "sums_asset": pick_sums_asset(release),
        "release_url": str(release.get("html_url") or LATEST_RELEASE_URL),
    }


# ============================================================== 下载与校验 ========
def update_dir() -> Path:
    base = os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.home())
    path = Path(base) / UPDATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, sums_text: str) -> Optional[bool]:
    """按 SHA256SUMS.txt 校验文件。找不到对应文件名行时返回 None（跳过校验）。"""
    wanted = str(path.name).lower()
    expected = ""
    for line in sums_text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].strip().lower(), parts[1].strip().lstrip("*").lower()
        if name == wanted:
            expected = digest
            break
    if not expected:
        return None
    return sha256_file(path) == expected


def download(url: str, dest_dir: Optional[Path] = None, progress: Optional[ProgressFn] = None,
             timeout: float = 60.0) -> Path:
    """下载到临时目录（先 .part 再改名，断掉不留下半成品）。

    V0.2.2 起走 :mod:`app.parallel_download`：多线程分段 Range 并发下载
    （GitHub Release 限速时单连接很慢，多线程拉满速度才正常）；
    服务器不支持分段时自动回退整文件下载。

    进度回调可能从工作线程里调用：回调自身要线程安全（界面侧用 Qt 信号转回主线程）。
    """
    from .parallel_download import download_file, MAX_WORKERS

    dest_dir = Path(dest_dir) if dest_dir else update_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _basename_from_url(url)
    dest = dest_dir / name
    try:
        download_file(url, dest, on_progress=progress, max_workers=MAX_WORKERS, timeout=timeout)
    except UpdateError:
        raise
    except Exception as exc:
        _cleanup_part(dest_dir / (name + ".part"))
        raise UpdateError("下载失败（%s）" % _short(exc)) from exc
    return dest


def _cleanup_part(part: Path) -> None:
    try:
        if part.exists():
            part.unlink()
    except OSError:
        pass


def _basename_from_url(url: str) -> str:
    tail = str(url or "").rsplit("/", 1)[-1].split("?", 1)[0]
    tail = re.sub(r"[^\w.\- ]", "_", tail)
    return tail or ("BaiAi-Tavern-%s.exe" % time.strftime("%Y%m%d%H%M%S"))


def _short(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:120] if text else exc.__class__.__name__


# ============================================================== 静默安装 ========
def install_dir_for_update() -> Path:
    """当前程序所在安装目录（打包后 = exe 所在目录；开发模式回退到注册表 / 默认目录）。"""
    from installer import common as ic

    return ic.install_dir_for_self()


def launch_installer(installer_path: Path, install_dir: Path, launch_after: bool = True) -> subprocess.Popen:
    """拉起新安装包静默装到 ``install_dir``。

    ``launch_after=True``：装完自动启动新版本（更新流程）；
    ``launch_after=False``：加 ``--no-run``（重装修复流程，当前程序还在跑）。
    安装程序内部会先关掉安装目录里正在运行的旧版本，所以调用方随后可以安心退出。
    """
    installer_path = Path(installer_path)
    install_dir = Path(install_dir)
    args: List[str] = [str(installer_path), "--silent", "--dir", str(install_dir)]
    if not launch_after:
        args.append("--no-run")
    if installer_path.suffix.lower() == ".bat":  # 自检时用批处理冒充安装包
        args = ["cmd.exe", "/c"] + args
    try:
        return subprocess.Popen(
            args,
            creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS | CREATE_NO_WINDOW
            close_fds=True,
        )
    except OSError as exc:
        raise UpdateError("启动安装程序失败：%s" % exc) from exc


__all__ = [
    "AUTO_CHECK_INTERVAL",
    "DEFAULT_API_BASE",
    "LATEST_RELEASE_URL",
    "RELEASES_URL",
    "REPO",
    "UpdateError",
    "check_latest",
    "download",
    "install_dir_for_update",
    "is_newer",
    "launch_installer",
    "latest_release",
    "normalize_version",
    "parse_version",
    "pick_installer_asset",
    "pick_sums_asset",
    "record_check",
    "sha256_file",
    "should_auto_check",
    "update_dir",
    "verify_sha256",
]
