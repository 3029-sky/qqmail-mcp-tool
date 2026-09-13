# clipboard.py - 从系统剪贴板导入附件
"""
把剪贴板里的内容变成 attachments/ 目录下的真实文件，供发信时当附件使用。

**为什么需要它**：终端里按 Ctrl+V **粘不出图片，也粘不出文件路径**——
实测在资源管理器里复制文件后，剪贴板里只有 CF_HDROP 格式，
文本格式（CF_UNICODETEXT / CF_TEXT）根本不存在，所以 Ctrl+V 什么都得不到。
本模块直接读剪贴板，把这两类内容落地成文件。

支持两类内容：

  1. **复制的文件**（资源管理器里 Ctrl+C）
     剪贴板格式 CF_HDROP，能直接拿到完整路径列表。
     若文件已经在附件目录里则原样使用（不复制）；
     否则复制一份到附件目录，这样后续发信只需给文件名。

  2. **复制的图片**（截图工具、网页图片右键复制、微信/QQ 截图）
     剪贴板格式 CF_DIB，是**裸位图数据**，没有文件头，所以要转换。
     转换链路刻意只用已装依赖，不引入 Pillow：
         CF_DIB -> 内存里拼出 PPM -> tkinter.PhotoImage -> PNG
     tkinter 是标准库，Windows 版自带 Tcl/Tk，Tk 8.6 支持写 PNG。

只做 Windows。其他平台 import 后所有函数返回空结果，不会抛异常。
"""

import os
import shutil
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import List, NamedTuple, Optional

__all__ = ["ClipboardItem", "import_clipboard", "clipboard_summary", "is_supported"]

#: 剪贴板格式常量（不依赖 pywin32 也能被测试引用）
CF_BITMAP = 2
CF_DIB = 8
CF_DIBV5 = 17
CF_HDROP = 15

#: DIB 压缩方式。0=BI_RGB（未压缩），3=BI_BITFIELDS（仍有原始像素）
_BI_RGB = 0
_BI_BITFIELDS = 3


class ClipboardItem(NamedTuple):
    """一条导入结果。"""

    name: str          # 落地后的文件名（不含目录）
    path: str          # 完整路径
    size: int          # 字节数
    kind: str          # "file"（文件）或 "image"（图片）
    copied: bool       # True=复制进附件目录，False=原本就在那里


# ---------------------------------------------------------------------------
# 依赖与可用性
# ---------------------------------------------------------------------------

def _win32clipboard():
    """返回 win32clipboard 模块；不可用时返回 None。"""
    try:
        import win32clipboard  # noqa: PLC0415

        return win32clipboard
    except Exception:  # noqa: BLE001
        return None


def is_supported() -> bool:
    """
    当前环境能否读剪贴板。

    非 Windows 或缺少 pywin32 时返回 False，
    调用方据此给出「此功能不可用」的提示，而不是崩掉。
    """
    if os.name != "nt":
        return False
    return _win32clipboard() is not None


class _Clipboard:
    """
    剪贴板开关的上下文管理器。

    剪贴板是全局独占资源：打开后不关会让其他程序读不到。
    Windows 上多进程可能同时抢，因此打开失败时做短暂重试。

    `cb` 是「形如 win32clipboard 的对象」——只要求有 OpenClipboard /
    CloseClipboard。这样测试可以传入同形状的替身，不必碰真实剪贴板。
    """

    def __init__(self, cb, attempts: int = 8, delay: float = 0.05):
        self._cb = cb
        self._attempts = attempts
        self._delay = delay
        self._opened = False

    def __enter__(self):
        last_error: Optional[Exception] = None
        for _ in range(self._attempts):
            try:
                self._cb.OpenClipboard()
                self._opened = True
                return self._cb
            except Exception as e:  # noqa: BLE001 - pywin32 抛的是通用 error
                last_error = e
                time.sleep(self._delay)
        raise RuntimeError("无法打开剪贴板（可能被其他程序占用）") from last_error

    def __exit__(self, *exc):
        if self._opened:
            try:
                self._cb.CloseClipboard()
            except Exception:  # noqa: BLE001
                pass
        return False


def _open_clipboard(cb):
    """返回可用的剪贴板上下文管理器。"""
    return _Clipboard(cb)


# ---------------------------------------------------------------------------
# 读取剪贴板
# ---------------------------------------------------------------------------

def _available_formats(cb) -> List[int]:
    formats: List[int] = []
    fmt = 0
    while True:
        try:
            fmt = cb.EnumClipboardFormats(fmt)
        except Exception:  # noqa: BLE001
            break
        if fmt == 0:
            break
        formats.append(fmt)
    return formats


def _read_file_paths(cb) -> List[str]:
    """
    读取 CF_HDROP：在资源管理器里复制的文件。

    实测返回形如 ('E:\\\\path\\\\a.zip',)；多个文件时是一个元组。
    """
    try:
        if not cb.IsClipboardFormatAvailable(CF_HDROP):
            return []
        data = cb.GetClipboardData(CF_HDROP)
    except Exception:  # noqa: BLE001
        return []

    if isinstance(data, str):
        return [data]
    if isinstance(data, (tuple, list)):
        return [str(p) for p in data if p]
    return []


def _read_dib(cb) -> Optional[bytes]:
    """读取 CF_DIB（裸位图数据）。"""
    try:
        if not cb.IsClipboardFormatAvailable(CF_DIB):
            return None
        return cb.GetClipboardData(CF_DIB)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# DIB -> PNG
# ---------------------------------------------------------------------------

def _dib_to_ppm(dib: bytes) -> tuple:
    """
    把 CF_DIB 数据转成 PPM(P6) 字节流，返回 (ppm, width, height)。

    为什么转 PPM：Tk 8.6 的 PhotoImage 能直接吃 PPM 与 PNG，
    但不接受 HBITMAP；而 PPM 是**无压缩、格式极简**的位图，
    直接用 struct 拼即可，不需要 Pillow。

    CF_DIB 的结构是 BITMAPINFOHEADER + 像素数据，
    比 .bmp 文件少了开头 14 字节的文件头。
    """
    if len(dib) < 40:
        raise ValueError("剪贴板位图数据不完整")

    header_size, width, height, planes, bpp, compression = struct.unpack(
        "<IiiHHI", dib[:20]
    )

    if compression not in (_BI_RGB, _BI_BITFIELDS):
        raise ValueError("不支持的位图压缩方式（%d）" % compression)
    if bpp not in (24, 32):
        raise ValueError("暂不支持的位深：%d 位" % bpp)
    if width <= 0 or height == 0:
        raise ValueError("位图尺寸无效：%dx%d" % (width, height))

    # 像素数据紧跟在 DIB 头之后。头长度由 header_size 给出：
    # BITMAPINFOHEADER 是 40，BITMAPV5HEADER 是 124。
    # 颜色掩码（BI_BITFIELDS 时存在）也算在 header_size 里。
    pixels = dib[header_size:]
    if len(pixels) < 1:
        raise ValueError("剪贴板位图没有像素数据")

    stride = ((width * bpp + 31) // 32) * 4      # 每行按 4 字节对齐
    if len(pixels) < stride * abs(height):
        raise ValueError("剪贴板位图数据长度不足")

    step = bpp // 8
    rows = []
    for y in range(abs(height)):
        # DIB 默认自下而上存储（height > 0 时）
        src_y = (abs(height) - 1 - y) if height > 0 else y
        line = pixels[src_y * stride: src_y * stride + width * step]
        # 32 位是 BGRA，24 位是 BGR；PPM 要 RGB
        rows.append(bytes(
            b for i in range(0, width * step, step)
            for b in (line[i + 2], line[i + 1], line[i])
        ))

    ppm = b"P6\n%d %d\n255\n" % (width, abs(height)) + b"".join(rows)
    return ppm, width, abs(height)


def _ppm_to_png(ppm: bytes, target: Path) -> None:
    """用 tkinter 把 PPM 写成 PNG。"""
    import tkinter as tk  # noqa: PLC0415 - 只在使用时才需要 Tk

    root = tk.Tk()
    root.withdraw()
    try:
        image = tk.PhotoImage(data=ppm)
        image.write(str(target), format="png")
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _normalize_ppm_pixels(ppm: bytes) -> List[List[tuple]]:
    """
    把 PPM(P6) 解析成 [[(r, g, b), ...], ...]。

    测试用它逐像素断言转换结果——位图解析错了不会抛异常，
    只会发出一张歪掉或颜色颠倒的图，所以必须能精确比对。
    """
    if not ppm.startswith(b"P6"):
        raise ValueError("不是 P6 格式的 PPM")

    # 头部是三行：magic、宽 高、最大色值
    parts = ppm.split(b"\n", 3)
    if len(parts) < 4:
        raise ValueError("PPM 头部不完整")
    width, height = (int(v) for v in parts[1].split())
    pixels = parts[3]

    rows: List[List[tuple]] = []
    for y in range(height):
        row = []
        for x in range(width):
            i = (y * width + x) * 3
            row.append((pixels[i], pixels[i + 1], pixels[i + 2]))
        rows.append(row)
    return rows


def _unique_path(directory: Path, name: str) -> Path:
    """避免覆盖同名文件，必要时加序号。"""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for i in range(2, 1000):
        candidate = directory / ("%s_%d%s" % (stem, i, suffix))
        if not candidate.exists():
            return candidate
    raise RuntimeError("同名文件过多：%s" % name)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def clipboard_summary() -> str:
    """
    用一句话描述剪贴板里有什么，用于提示用户。

    返回 "空" 表示剪贴板里既没有文件也没有图片。
    """
    cb = _win32clipboard()
    if cb is None:
        return "不可用"

    try:
        with _open_clipboard(cb):
            paths = _read_file_paths(cb)
            if paths:
                if len(paths) == 1:
                    return "1 个文件：%s" % Path(paths[0]).name
                return "%d 个文件" % len(paths)

            dib = _read_dib(cb)
            if dib:
                try:
                    _, width, height = _dib_to_ppm(dib)
                    return "图片 %d×%d" % (width, height)
                except Exception:  # noqa: BLE001
                    return "图片（无法解析）"

            if cb.IsClipboardFormatAvailable(13):        # CF_UNICODETEXT
                return "文本（不是文件或图片）"
            return "空"
    except Exception as e:  # noqa: BLE001
        return "读取失败：%s" % str(e)[:60]


def import_clipboard(attachment_dir: Path, max_bytes: int = 0) -> tuple:
    """
    把剪贴板内容导入附件目录。

    返回 (items, problems)：
      items    —— List[ClipboardItem]，已成功落地的附件
      problems —— List[str]，可直接展示给用户的失败原因

    优先处理「复制的文件」：它是用户明确选中的东西，意图最清楚。
    只有剪贴板里没有文件时，才看是不是图片。
    """
    items: List[ClipboardItem] = []
    problems: List[str] = []

    cb = _win32clipboard()
    if cb is None:
        return items, ["当前环境无法读取剪贴板（需要 Windows + pywin32）"]

    attachment_dir = Path(attachment_dir)
    attachment_dir.mkdir(parents=True, exist_ok=True)

    try:
        with _open_clipboard(cb):
            paths = _read_file_paths(cb)
            dib = None if paths else _read_dib(cb)
    except Exception as e:  # noqa: BLE001
        return items, ["无法打开剪贴板：%s" % str(e)[:80]]

    # ---- 情况一：复制的文件 ----
    if paths:
        for raw in paths:
            source = Path(raw)
            if not source.is_file():
                problems.append("跳过（不是文件）：%s" % source.name)
                continue

            try:
                size = source.stat().st_size
            except OSError as e:
                problems.append("读不到 %s：%s" % (source.name, e))
                continue

            if max_bytes and size > max_bytes:
                problems.append(
                    "跳过（过大）：%s 约 %.1f MB，超过单件上限 %.1f MB"
                    % (source.name, size / 1048576, max_bytes / 1048576)
                )
                continue

            # 已经在附件目录里 -> 原样使用，不复制
            try:
                already_there = source.parent.resolve() == attachment_dir.resolve()
            except OSError:
                already_there = False

            if already_there:
                items.append(ClipboardItem(source.name, str(source), size, "file", False))
                continue

            target = _unique_path(attachment_dir, source.name)
            try:
                shutil.copy2(source, target)
            except OSError as e:
                problems.append("复制失败 %s：%s" % (source.name, e))
                continue
            items.append(ClipboardItem(target.name, str(target), size, "file", True))
        return items, problems

    # ---- 情况二：复制的图片 ----
    if not dib:
        return items, ["剪贴板里没有文件或图片"]

    try:
        ppm, width, height = _dib_to_ppm(dib)
    except Exception as e:  # noqa: BLE001
        return items, ["无法解析剪贴板图片：%s" % e]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = _unique_path(attachment_dir, "剪贴板图片_%s.png" % stamp)

    try:
        _ppm_to_png(ppm, target)
    except Exception as e:  # noqa: BLE001
        return items, ["图片转换失败（需要 Tk）：%s" % str(e)[:80]]

    size = target.stat().st_size
    if max_bytes and size > max_bytes:
        target.unlink(missing_ok=True)
        return items, [
            "图片过大：%d×%d 约 %.1f MB，超过单件上限 %.1f MB"
            % (width, height, size / 1048576, max_bytes / 1048576)
        ]

    items.append(ClipboardItem(target.name, str(target), size, "image", True))
    return items, problems
