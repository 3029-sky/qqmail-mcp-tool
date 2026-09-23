# tests/test_clipboard.py - 剪贴板导入的单元测试
"""
覆盖 /粘贴 的两条路径：复制的文件（CF_HDROP）与复制的图片（CF_DIB）。

**_dib_to_ppm 是重点**：位图解析（行对齐、自下而上存储、BGRA->RGB）
是最容易静默出错的地方——错了不会抛异常，只会发出一张歪掉或者
颜色不对的图。因此这里构造**已知像素**的 DIB，逐像素断言结果。

所有用例都不碰真实剪贴板：读取函数被替换成返回构造好的数据。
"""

import struct

import pytest

from clipboard import (
    ClipboardItem,
    _dib_to_ppm,
    _normalize_ppm_pixels,
    _unique_path,
    clipboard_summary,
    import_clipboard,
)


# ---------------------------------------------------------------------------
# 构造测试用的 DIB
# ---------------------------------------------------------------------------

def make_dib(pixel_rows, bpp=32, top_down=False):
    """
    按给定像素构造 CF_DIB 数据。

    pixel_rows 按**视觉从上到下**给出，每行是 (r, g, b) 元组列表。
    DIB 默认自下而上存储（height > 0），这里自动翻转，
    这样断言时可以直接按视觉顺序比对。
    """
    height = len(pixel_rows)
    width = len(pixel_rows[0])
    step = bpp // 8
    stride = ((width * bpp + 31) // 32) * 4

    stored_rows = pixel_rows if top_down else list(reversed(pixel_rows))
    body = bytearray()
    for row in stored_rows:
        line = bytearray()
        for (r, g, b) in row:
            if bpp == 32:
                line += bytes((b, g, r, 255))      # BGRA
            else:
                line += bytes((b, g, r))           # BGR
        line += b"\x00" * (stride - len(line))     # 行对齐填充
        body += line

    header = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, bpp, 0, len(body), 2835, 2835, 0, 0
    )
    return header + bytes(body)


def ppm_pixels(ppm):
    """把 PPM(P6) 解析成 [[(r,g,b), ...], ...]，便于逐像素断言。"""
    return _normalize_ppm_pixels(ppm)


# ---------------------------------------------------------------------------
# _dib_to_ppm：位图解析
# ---------------------------------------------------------------------------

def test_dib_32bit_pixels_are_converted_correctly():
    """
    32 位 DIB 的像素顺序是 BGRA，PPM 要 RGB。

    若忘了交换 R/B，图会变成蓝红颠倒——不会报错，只会发错。
    """
    rows = [
        [(255, 0, 0), (0, 255, 0)],      # 红、绿
        [(0, 0, 255), (255, 255, 0)],    # 蓝、黄
    ]
    ppm, width, height = _dib_to_ppm(make_dib(rows, bpp=32))

    assert (width, height) == (2, 2)
    assert ppm_pixels(ppm) == rows, "颜色与位置都必须一致"


def test_dib_24bit_pixels_are_converted_correctly():
    rows = [[(10, 20, 30), (40, 50, 60)], [(70, 80, 90), (100, 110, 120)]]
    ppm, width, height = _dib_to_ppm(make_dib(rows, bpp=24))

    assert (width, height) == (2, 2)
    assert ppm_pixels(ppm) == rows


def test_dib_row_padding_is_handled():
    """
    24 位图每行按 4 字节对齐，宽度不是 4 的倍数时会有填充字节。

    忽略填充会导致**整张图向右斜切**——典型的「看起来就是不对」。
    宽度 3 时每行 9 字节，补 3 字节到 12。若把填充当像素，
    第二行就会错位。
    """
    rows = [
        [(1, 1, 1), (2, 2, 2), (3, 3, 3)],
        [(4, 4, 4), (5, 5, 5), (6, 6, 6)],
        [(7, 7, 7), (8, 8, 8), (9, 9, 9)],
    ]
    ppm, width, height = _dib_to_ppm(make_dib(rows, bpp=24))

    assert (width, height) == (3, 3)
    assert ppm_pixels(ppm) == rows


def test_dib_bottom_up_order_is_flipped():
    """
    DIB 默认自下而上存储。若忘了翻转，图会上下颠倒。

    这里只给两行且颜色差别明显，翻转与否一目了然。
    """
    top_row = [(255, 255, 255), (255, 255, 255)]     # 白
    bottom_row = [(0, 0, 0), (0, 0, 0)]              # 黑
    ppm, _, _ = _dib_to_ppm(make_dib([top_row, bottom_row], bpp=32))

    parsed = ppm_pixels(ppm)
    assert parsed[0] == top_row, "第一行应是视觉上的顶行（白）"
    assert parsed[1] == bottom_row


def test_dib_top_down_image_is_not_flipped():
    """height 为负表示自上而下存储，此时不应再翻转。"""
    top_row = [(255, 255, 255)]
    bottom_row = [(0, 0, 0)]
    dib = make_dib([top_row, bottom_row], bpp=32, top_down=True)
    # 把 height 改成负数，表示自上而下
    header = bytearray(dib[:40])
    _, w, h, planes, bpp, comp = struct.unpack("<IiiHHI", header[:20])
    header[:20] = struct.pack("<IiiHHI", 40, w, -h, planes, bpp, comp)

    ppm, _, height = _dib_to_ppm(bytes(header) + dib[40:])
    assert height == 2
    assert ppm_pixels(ppm)[0] == top_row


@pytest.mark.parametrize(
    "bad, message",
    [
        (b"", "不完整"),
        (struct.pack("<IiiHHI", 40, 2, 2, 1, 16, 0) + b"\x00" * 64, "位深"),
        (struct.pack("<IiiHHI", 40, 2, 2, 1, 32, 99) + b"\x00" * 64, "压缩"),
        (struct.pack("<IiiHHI", 40, 0, 2, 1, 32, 0) + b"\x00" * 64, "尺寸无效"),
        # header_size 声称 48 字节，但数据只有 40，像素区被截断为空
        (struct.pack("<IiiHHI", 48, 2, 2, 1, 32, 0) + b"\x00" * 20, "没有像素"),
        (struct.pack("<IiiHHI", 40, 100, 100, 1, 32, 0) + b"\x00" * 32, "长度不足"),
    ],
)
def test_dib_rejects_unsupported_input(bad, message):
    """
    不能解析时必须**明确报错**，而不是产出一张歪图。

    宁可让用户看到「无法解析剪贴板图片」，也不要发出去一张错的。
    """
    with pytest.raises(ValueError) as e:
        _dib_to_ppm(bad)
    assert message in str(e.value)


# ---------------------------------------------------------------------------
# _unique_path
# ---------------------------------------------------------------------------

def test_unique_path_avoids_overwriting(tmp_path):
    original = tmp_path / "报表.zip"
    original.write_bytes(b"x")

    got = _unique_path(tmp_path, "报表.zip")
    assert got.name == "报表_2.zip"
    assert not got.exists()


def test_unique_path_increments_until_free(tmp_path):
    for name in ("a.zip", "a_2.zip", "a_3.zip"):
        (tmp_path / name).write_bytes(b"x")

    assert _unique_path(tmp_path, "a.zip").name == "a_4.zip"


def test_unique_path_returns_plain_name_when_free(tmp_path):
    assert _unique_path(tmp_path, "新的.zip").name == "新的.zip"


# ---------------------------------------------------------------------------
# import_clipboard：文件路径
# ---------------------------------------------------------------------------

@pytest.fixture
def no_clipboard(monkeypatch):
    """
    让 clipboard 模块认为剪贴板可用，并把读取函数换成可控的替身。

    这样用例完全不碰真实剪贴板——CI 上没有剪贴板，也不该依赖它。

    注意替身要提供 _Clipboard 上下文管理器所需的方法
    （OpenClipboard / CloseClipboard），因为它会接管真实对象。
    """
    import clipboard

    monkeypatch.setattr(clipboard, "_win32clipboard", lambda: object())

    state = {"files": [], "dib": None}

    class FakeClipboardModule:
        """形如 win32clipboard 模块：只实现被用到的那几个方法。"""

        def OpenClipboard(self):
            pass

        def CloseClipboard(self):
            pass

        def EnumClipboardFormats(self, fmt):
            return 0

        def IsClipboardFormatAvailable(self, fmt):
            if fmt == clipboard.CF_HDROP:
                return bool(state["files"])
            if fmt == clipboard.CF_DIB:
                return state["dib"] is not None
            return False

    fake = FakeClipboardModule()
    # _win32clipboard() 返回替身；_open_clipboard 仍用真实的上下文管理器逻辑，
    # 只是喂给它替身，于是重试/关闭语义也被一并测到。
    monkeypatch.setattr(clipboard, "_win32clipboard", lambda: fake)
    monkeypatch.setattr(clipboard, "_read_file_paths", lambda cb: list(state["files"]))
    monkeypatch.setattr(clipboard, "_read_dib", lambda cb: state["dib"])
    return state


def test_import_copies_outer_file_into_attachment_dir(no_clipboard, tmp_path):
    """外部文件应被复制进附件目录，之后给文件名就能发。"""
    source = tmp_path / "外部"
    source.mkdir()
    archive = source / "项目资料.zip"
    archive.write_bytes(b"PK\x03\x04" + b"x" * 200)

    att = tmp_path / "attachments"
    no_clipboard["files"] = [str(archive)]

    items, problems = import_clipboard(att)

    assert problems == []
    assert len(items) == 1
    item = items[0]
    assert item.name == "项目资料.zip"
    assert item.kind == "file"
    assert item.copied is True
    assert (att / "项目资料.zip").is_file(), "应真的复制进来"


def test_import_file_already_in_attachment_dir_is_not_copied(no_clipboard, tmp_path):
    """已经在附件目录里的文件原样使用，不重复复制。"""
    att = tmp_path / "attachments"
    att.mkdir()
    inside = att / "已有.zip"
    inside.write_bytes(b"x")

    no_clipboard["files"] = [str(inside)]

    items, problems = import_clipboard(att)

    assert problems == []
    assert len(items) == 1
    assert items[0].copied is False, "不应再复制一份"
    assert items[0].path == str(inside)


def test_import_reports_oversized_file(no_clipboard, tmp_path):
    """超过单件上限的文件应被跳过并说明原因，而不是照样塞进去。"""
    source = tmp_path / "巨大.zip"
    source.write_bytes(b"x" * 5000)

    att = tmp_path / "attachments"
    no_clipboard["files"] = [str(source)]

    items, problems = import_clipboard(att, max_bytes=1000)

    assert items == []
    assert len(problems) == 1
    assert "过大" in problems[0]
    assert not (att / "巨大.zip").exists()


def test_import_skips_missing_file(no_clipboard, tmp_path):
    """剪贴板里残留的不存在路径应被跳过，不能让整次导入失败。"""
    att = tmp_path / "attachments"
    good = tmp_path / "好文件.zip"
    good.write_bytes(b"x")

    no_clipboard["files"] = [str(tmp_path / "已删除.zip"), str(good)]

    items, problems = import_clipboard(att)

    assert len(items) == 1 and items[0].name == "好文件.zip"
    assert len(problems) == 1 and "跳过" in problems[0]


def test_import_multiple_files(no_clipboard, tmp_path):
    att = tmp_path / "attachments"
    names = []
    for n in ("a.zip", "b.pdf"):
        p = tmp_path / n
        p.write_bytes(b"x")
        names.append(str(p))

    no_clipboard["files"] = names

    items, problems = import_clipboard(att)

    assert problems == []
    assert [i.name for i in items] == ["a.zip", "b.pdf"]


# ---------------------------------------------------------------------------
# import_clipboard：图片
# ---------------------------------------------------------------------------

def test_import_image_writes_png(no_clipboard, tmp_path, monkeypatch):
    """
    剪贴板图片应被写成 PNG 文件。

    这里把「PPM -> PNG」那一步换成替身：真实实现要走 tkinter + Tk 窗口，
    在没有显示服务的环境（CI 的 ubuntu runner）里连 tk.Tk() 都建不起来。
    本用例要验的是**导入流程**——文件名、类型、落盘位置，
    真正的 PNG 编码由下面 test_ppm_to_png_encodes_real_png 在支持的平台上验。
    """
    import clipboard

    written = []

    def fake_ppm_to_png(ppm, target):
        written.append(target)
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"stub")

    monkeypatch.setattr(clipboard, "_ppm_to_png", fake_ppm_to_png)

    rows = [[(255, 0, 0), (0, 255, 0)], [(0, 0, 255), (255, 255, 0)]]
    no_clipboard["dib"] = make_dib(rows, bpp=32)

    att = tmp_path / "attachments"
    items, problems = import_clipboard(att)

    assert problems == [], "图片导入不该报错：%s" % problems
    assert len(items) == 1
    item = items[0]
    assert item.kind == "image"
    assert item.name.startswith("剪贴板图片_")
    assert item.name.endswith(".png")

    assert len(written) == 1, "应恰好写一次 PNG"
    assert written[0] == att / item.name
    data = (att / item.name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "必须是真正的 PNG"


def _tk_available() -> bool:
    """tkinter 能用吗？（缺模块，或没有显示服务时都返回 False）"""
    try:
        import tkinter as tk
    except Exception:  # noqa: BLE001 - 缺 python3-tk 时 ImportError
        return False
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - 无 DISPLAY 时 TclError
        return False


@pytest.mark.skipif(not _tk_available(),
                    reason="需要 tkinter 与显示服务（Linux 上装 python3-tk 并设 DISPLAY）")
def test_ppm_to_png_encodes_real_png(tmp_path):
    """
    _ppm_to_png 真的能写出 PNG（Tk 8.6 的 PhotoImage 支持写 PNG）。

    这条用例依赖本机 Tk，所以在无显示环境会被跳过——
    跳过是**如实反映环境限制**，不是掩盖失败。
    """
    from clipboard import _ppm_to_png

    # 2x2 纯红
    ppm = b"P6\n2 2\n255\n" + bytes([255, 0, 0] * 4)
    target = tmp_path / "out.png"

    _ppm_to_png(ppm, target)

    assert target.exists()
    data = target.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 20


def test_import_broken_image_reports_problem(no_clipboard, tmp_path):
    """无法解析的位图要给出可读原因，不能让异常冒到对话循环里。"""
    no_clipboard["dib"] = b"\x00" * 8      # 太短

    items, problems = import_clipboard(tmp_path / "attachments")

    assert items == []
    assert len(problems) == 1
    assert "无法解析" in problems[0]


def test_import_empty_clipboard(no_clipboard, tmp_path):
    items, problems = import_clipboard(tmp_path / "attachments")
    assert items == []
    assert problems == ["剪贴板里没有文件或图片"]


def test_import_without_pywin32_reports_clearly(monkeypatch, tmp_path):
    """没有 pywin32 时应给出明确提示，而不是崩掉。"""
    import clipboard

    monkeypatch.setattr(clipboard, "_win32clipboard", lambda: None)

    items, problems = import_clipboard(tmp_path)

    assert items == []
    assert len(problems) == 1
    assert "无法读取剪贴板" in problems[0]


# ---------------------------------------------------------------------------
# clipboard_summary
# ---------------------------------------------------------------------------

def test_summary_reports_single_file(no_clipboard):
    no_clipboard["files"] = [r"E:\某处\项目资料.zip"]
    assert clipboard_summary() == "1 个文件：项目资料.zip"


def test_summary_reports_multiple_files(no_clipboard):
    no_clipboard["files"] = [r"E:\a.zip", r"E:\b.pdf"]
    assert clipboard_summary() == "2 个文件"


def test_summary_reports_image_size(no_clipboard):
    no_clipboard["dib"] = make_dib([[(1, 2, 3)] * 4] * 3, bpp=32)
    assert clipboard_summary() == "图片 4×3"


def test_summary_empty(no_clipboard):
    assert clipboard_summary() == "空"


def test_summary_unavailable_without_pywin32(monkeypatch):
    import clipboard

    monkeypatch.setattr(clipboard, "_win32clipboard", lambda: None)
    assert clipboard_summary() == "不可用"


# ---------------------------------------------------------------------------
# ClipboardItem
# ---------------------------------------------------------------------------

def test_clipboard_item_exposes_expected_fields():
    item = ClipboardItem("a.zip", r"E:\att\a.zip", 123, "file", True)
    assert item.name == "a.zip"
    assert item.size == 123
    assert item.kind == "file"
    assert item.copied is True
