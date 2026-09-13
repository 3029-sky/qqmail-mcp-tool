# open_window.py - 把网页界面开成独立窗口
"""
用 Edge/Chrome 的 `--app` 模式把本地网页打开成一个**没有地址栏、
没有标签页**的独立窗口，看起来就是一个普通桌面应用。

为什么这么做，而不是原生窗口或 pywebview：

  1. **不引入新依赖**。pywebview 在 Windows 上要拖一个 pythonnet，
     装起来重且容易和别的包打架；而 Edge 几乎每台 Windows 都有。
  2. **粘贴图片靠浏览器原生能力**。无论外壳是谁，真正的粘贴都是
     网页事件在做，所以外壳换成 Edge 并不损失功能。
  3. `--app` 模式会用一个独立的窗口与独立的任务栏图标，
     `--user-data-dir` 让它与用户日常浏览器**互不干扰**
     （不会共用标签页、扩展、登录态）。

找不到 Edge/Chrome 时返回 False，调用方退回到默认浏览器打开——
那样功能完全一样，只是多了地址栏。
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

__all__ = ["find_browser", "open_app_window"]


def _candidate_paths() -> List[Path]:
    """按优先级列出可能的浏览器可执行文件。"""
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    names = ["msedge.exe", "chrome.exe"]
    roots = [
        Path(program_files_x86) / "Microsoft" / "Edge" / "Application",
        Path(program_files) / "Microsoft" / "Edge" / "Application",
        Path(program_files) / "Google" / "Chrome" / "Application",
        Path(program_files_x86) / "Google" / "Chrome" / "Application",
    ]
    if local:
        roots.append(Path(local) / "Microsoft" / "Edge" / "Application")
        roots.append(Path(local) / "Google" / "Chrome" / "Application")

    found = []
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                found.append(candidate)
    return found


def find_browser() -> Optional[Path]:
    """返回第一个可用的 Edge/Chrome 路径；都没有则返回 None。"""
    if os.name != "nt":
        # 其他平台交给系统默认浏览器，不做特殊处理
        return None
    candidates = _candidate_paths()
    return candidates[0] if candidates else None


def _profile_dir() -> Path:
    """
    独立窗口专用的用户数据目录。

    为什么必须指定：不指定的话，`--app` 会挂到你日常浏览器的进程上，
    窗口会带出你的标签页与扩展，关掉时还会把整个浏览器一起关掉。
    放在项目的 .webview 目录下，与仓库一起被 gitignore。
    """
    directory = Path(__file__).resolve().parent / ".webview"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def open_app_window(url: str) -> bool:
    """
    用独立窗口打开 url。

    成功启动返回 True；找不到浏览器或启动失败返回 False，
    由调用方退回默认浏览器。
    """
    browser = find_browser()
    if browser is None:
        return False

    command = [
        str(browser),
        "--app=%s" % url,
        "--user-data-dir=%s" % _profile_dir(),
        # 独立窗口不需要这些，关掉可以少占内存、也避免弹提示
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"
    browser = find_browser()
    print("浏览器: %s" % (browser or "未找到"))
    if open_app_window(target):
        print("已打开窗口: %s" % target)
    else:
        print("无法打开独立窗口")
        sys.exit(1)
