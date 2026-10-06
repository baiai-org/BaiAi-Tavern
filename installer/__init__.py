"""安装 / 卸载（一个 EXE 搞定）。

* :mod:`installer.common` —— 安装与卸载的全部逻辑（标准库实现，安装包体积小）
* :mod:`installer.installer_main` —— 图形安装向导（也是 ``BaiAi-Tavern V0.2.exe`` 的入口）

卸载不需要单独的程序：安装后在「Windows 设置 → 应用」里点卸载，
调用的是安装目录里的主程序 ``BaiAi-Tavern.exe --uninstall``（逻辑同样在本包里）。
"""

from . import common  # noqa: F401

__all__ = ["common"]
