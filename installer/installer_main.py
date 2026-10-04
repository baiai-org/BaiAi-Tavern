"""BaiAi-Tavern 安装向导（Tkinter 界面，无额外依赖）。

**只有一个 EXE**：既是安装程序，也能卸载 ——

* 双击运行 = 图形向导：未安装时是「安装」，已安装时显示已安装版本并提供
  「重新安装 / 卸载」；
* 自动化安装：``BaiAi-Tavern V0.1.exe --silent --dir "D:\\Apps\\BaiAi-Tavern" --no-run``；
* 自动化卸载：``BaiAi-Tavern V0.1.exe --silent --uninstall [--remove-data]``。

安装后「Windows 设置 → 应用」里的卸载项指向安装目录里的主程序
（``BaiAi-Tavern.exe --uninstall``），所以不需要单独发布卸载 EXE。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer import common as ic  # noqa: E402


def launch_after_install(install_dir: Path) -> None:
    exe = install_dir / ic.EXE_NAME
    try:
        os.startfile(str(exe))  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        try:
            subprocess.Popen([str(exe)], cwd=str(install_dir))
        except Exception:
            pass


# ================================================================ 命令行模式 ==
def run_silent_install(args: argparse.Namespace) -> int:
    def _progress(percent: int, text: str) -> None:
        print("[%3d%%] %s" % (percent, text), flush=True)

    result = ic.install(
        install_dir=Path(args.dir) if args.dir else None,
        create_desktop=not args.no_desktop,
        create_startmenu=not args.no_startmenu,
        progress=_progress,
        desktop_path=Path(args.desktop_dir) if args.desktop_dir else None,
        startmenu_path=Path(args.startmenu_dir) if args.startmenu_dir else None,
        reg_path=args.app_key or ic.REG_PATH,
    )
    if not result.get("ok"):
        print("安装失败：%s" % result.get("error"))
        return 1
    print("安装完成：%s" % result["install_dir"])
    for item in result.get("shortcuts") or []:
        print("  快捷方式：%s" % item)
    if not args.no_run:
        launch_after_install(Path(result["install_dir"]))
    return 0


def run_silent_uninstall(args: argparse.Namespace) -> int:
    result = ic.uninstall(
        install_dir=Path(args.dir) if args.dir else None,
        remove_data=bool(args.remove_data),
        reg_path=args.app_key or ic.REG_PATH,
        self_exe=None,  # 由安装程序代为卸载：没有正在运行的主程序，可以全部前台删掉
    )
    if not result.get("ok"):
        print("卸载失败：%s" % result.get("error"))
        return 1
    print("卸载完成：%s" % result["install_dir"])
    if result.get("data_kept"):
        print("已保留用户数据（data / config.yaml）")
    for item in result.get("errors") or []:
        print("提示：%s" % item)
    return 0


# ================================================================ 图形界面 ==
def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    installed = ic.installed_info()
    root = tk.Tk()
    root.title("安装 %s %s" % (ic.APP_NAME, ic.APP_VERSION))
    root.geometry("620x470")
    root.resizable(False, False)

    header = tk.Frame(root, bg="#2b3550", height=76)
    header.pack(fill="x")
    tk.Label(
        header,
        text="  %s  %s" % (ic.APP_NAME, ic.APP_VERSION),
        bg="#2b3550",
        fg="white",
        font=("Microsoft YaHei UI", 15, "bold"),
    ).place(x=12, y=12)
    tk.Label(
        header,
        text="  作者：%s　·　%s" % (ic.APP_AUTHOR, ic.APP_HOMEPAGE),
        bg="#2b3550",
        fg="#cfe0ff",
        font=("Microsoft YaHei UI", 9),
    ).place(x=14, y=44)

    body = tk.Frame(root, padx=18, pady=14)
    body.pack(fill="both", expand=True)

    tk.Label(body, text=ic.APP_DESCRIPTION, anchor="w", wraplength=560, justify="left").pack(fill="x")
    if installed.get("installed"):
        tk.Label(
            body,
            text="已安装 %s（%s）" % (installed.get("version") or ic.APP_VERSION, installed.get("install_dir")),
            anchor="w",
            fg="#1a7f37",
            wraplength=560,
            justify="left",
        ).pack(fill="x", pady=(6, 2))
        tk.Label(
            body,
            text="可以直接「重新安装」（升级 / 修复，用户数据不动），或「卸载」。",
            anchor="w",
            fg="#555555",
        ).pack(fill="x", pady=(0, 10))
    else:
        tk.Label(
            body,
            text="安装后可在「Windows 设置 → 应用」里卸载；用户数据默认保留。",
            anchor="w",
            fg="#555555",
        ).pack(fill="x", pady=(6, 12))

    dir_row = tk.Frame(body)
    dir_row.pack(fill="x")
    tk.Label(dir_row, text="安装位置：", width=9, anchor="w").pack(side="left")
    dir_var = tk.StringVar(value=str(installed.get("install_dir") or ic.default_install_dir()))
    tk.Entry(dir_row, textvariable=dir_var, width=52).pack(side="left", fill="x", expand=True)

    def browse() -> None:
        chosen = filedialog.askdirectory(initialdir=dir_var.get() or str(Path.home()))
        if chosen:
            dir_var.set(chosen)

    tk.Button(dir_row, text="浏览…", command=browse).pack(side="left", padx=(6, 0))

    desktop_var = tk.BooleanVar(value=True)
    startmenu_var = tk.BooleanVar(value=True)
    run_var = tk.BooleanVar(value=True)
    remove_data_var = tk.BooleanVar(value=False)
    options = tk.Frame(body)
    options.pack(fill="x", pady=10)
    tk.Checkbutton(options, text="创建桌面快捷方式", variable=desktop_var).pack(anchor="w")
    tk.Checkbutton(options, text="创建开始菜单快捷方式", variable=startmenu_var).pack(anchor="w")
    tk.Checkbutton(options, text="安装完成后立即运行", variable=run_var).pack(anchor="w")
    tk.Checkbutton(
        options,
        text="卸载时同时删除我的配置与数据（data 目录、config.yaml，不可恢复）",
        variable=remove_data_var,
    ).pack(anchor="w")

    status = tk.Label(body, text="准备就绪", anchor="w", fg="#444444")
    status.pack(fill="x")
    bar = ttk.Progressbar(body, maximum=100)
    bar.pack(fill="x", pady=(4, 10))

    buttons = tk.Frame(body)
    buttons.pack(fill="x")

    def _install() -> None:
        install_button.configure(state="disabled")
        target = Path(dir_var.get().strip() or str(ic.default_install_dir()))

        def _worker() -> None:
            def _progress(percent: int, text: str) -> None:
                root.after(0, lambda: (bar.configure(value=percent), status.configure(text=text)))

            result = ic.install(
                install_dir=target,
                create_desktop=desktop_var.get(),
                create_startmenu=startmenu_var.get(),
                progress=_progress,
            )
            root.after(0, lambda: _finish(result, result.get("cleaned") or []))

        def _finish(result: Dict[str, Any], cleaned: List[str]) -> None:
            if not result.get("ok"):
                messagebox.showerror("安装失败", str(result.get("error")))
                install_button.configure(state="normal")
                return
            status.configure(text="安装完成")
            bar.configure(value=100)
            lines = [
                "已安装到：%s" % result["install_dir"],
                "",
                "快捷方式：%d 个（可在设置 → 应用里卸载）" % len(result.get("shortcuts") or []),
                "用户数据（data 目录、config.yaml）不会被删除。",
            ]
            if cleaned:
                lines.append("已清理旧版本残留：%s" % "、".join(cleaned))
            messagebox.showinfo("安装完成", "\n".join(lines))
            if run_var.get():
                launch_after_install(Path(result["install_dir"]))
            root.destroy()

        threading.Thread(target=_worker, daemon=True).start()

    def _uninstall() -> None:
        target = Path(dir_var.get().strip() or str(ic.default_install_dir()))
        if not messagebox.askyesno("确认卸载", "确定要卸载 %s 吗？" % ic.APP_NAME):
            return
        uninstall_button.configure(state="disabled")
        install_button.configure(state="disabled")

        def _worker() -> None:
            result = ic.uninstall(install_dir=target, remove_data=remove_data_var.get())
            root.after(0, lambda: _finish(result))

        def _finish(result: Dict[str, Any]) -> None:
            if not result.get("ok"):
                messagebox.showerror("卸载失败", str(result.get("error")))
                uninstall_button.configure(state="normal")
                install_button.configure(state="normal")
                return
            messagebox.showinfo(
                "卸载完成",
                "程序文件、快捷方式与卸载信息都已清理。\n\n%s"
                % ("用户数据已保留（data / config.yaml）。" if result.get("data_kept", True) else "用户数据也已删除。"),
            )
            root.destroy()

        threading.Thread(target=_worker, daemon=True).start()

    install_button = tk.Button(
        buttons, text="重新安装" if installed.get("installed") else "开始安装", width=12, command=_install
    )
    install_button.pack(side="right", padx=(8, 0))
    uninstall_button = tk.Button(buttons, text="卸载", width=10, command=_uninstall)
    uninstall_button.pack(side="right", padx=(8, 0))
    tk.Button(buttons, text="退出", width=10, command=root.destroy).pack(side="right")

    root.mainloop()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="%s 安装 / 卸载程序" % ic.APP_NAME)
    parser.add_argument("--silent", action="store_true", help="静默执行（不显示界面）")
    parser.add_argument("--uninstall", action="store_true", help="执行卸载")
    parser.add_argument("--remove-data", action="store_true", help="卸载时同时删除配置与数据")
    parser.add_argument("--dir", help="安装目录")
    parser.add_argument("--no-desktop", action="store_true", help="不创建桌面快捷方式")
    parser.add_argument("--no-startmenu", action="store_true", help="不创建开始菜单快捷方式")
    parser.add_argument("--no-run", action="store_true", help="安装完成后不自动运行")
    parser.add_argument("--desktop-dir", help="桌面目录（自检时重定向）")
    parser.add_argument("--startmenu-dir", help="开始菜单目录（自检时重定向）")
    parser.add_argument("--app-key", help="卸载信息注册表项（自检时用独立项，避免影响真实安装）")
    args = parser.parse_args(argv)
    if args.silent:
        return run_silent_uninstall(args) if args.uninstall else run_silent_install(args)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
