# tests/test_open_window.py - 独立窗口启动器的测试
"""
只测「命令怎么拼」，不真的启动浏览器。

`--app` 模式的两个参数是必须的，缺任何一个都会让体验退化成
「普通浏览器标签页」或者「和你日常浏览器抢进程」：

  --app=<url>            去掉地址栏与标签页，看起来才像应用
  --user-data-dir=<dir>  用独立配置目录，否则窗口会带出你日常的
                         标签页与扩展，关掉时还可能把整个浏览器一起关
"""

import subprocess
from pathlib import Path

import pytest

import open_window


@pytest.fixture
def captured_popen(monkeypatch):
    """拦下 subprocess.Popen，记录命令而不真的执行。"""
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append(cmd)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return calls


def test_find_browser_returns_path_or_none():
    result = open_window.find_browser()
    assert result is None or isinstance(result, Path)


def test_candidate_paths_point_at_known_install_locations():
    """候选路径应覆盖 Edge 与 Chrome 的常见安装位置。"""
    text = " ".join(str(p) for p in open_window._candidate_paths())
    assert "msedge.exe" in text or "chrome.exe" in text or not text


def test_profile_dir_is_inside_project(monkeypatch, tmp_path):
    """
    独立配置目录必须放在项目内，不能污染用户的浏览器配置目录。
    """
    monkeypatch.setattr(open_window, "__file__", str(tmp_path / "open_window.py"))
    directory = open_window._profile_dir()
    assert directory.is_dir()
    assert directory.parent == tmp_path


def test_open_app_window_uses_app_and_user_data_dir(monkeypatch, captured_popen):
    monkeypatch.setattr(open_window, "find_browser", lambda: Path(r"C:\fake\msedge.exe"))

    ok = open_window.open_app_window("http://127.0.0.1:8765/")

    assert ok is True
    assert len(captured_popen) == 1
    command = captured_popen[0]
    assert command[0] == r"C:\fake\msedge.exe"
    assert "--app=http://127.0.0.1:8765/" in command, "缺 --app 就会带地址栏"
    assert any(a.startswith("--user-data-dir=") for a in command), (
        "缺 --user-data-dir 会和日常浏览器共用进程"
    )


def test_open_app_window_returns_false_without_browser(monkeypatch, captured_popen):
    """找不到浏览器时返回 False，让调用方退回默认浏览器。"""
    monkeypatch.setattr(open_window, "find_browser", lambda: None)

    assert open_window.open_app_window("http://127.0.0.1:8765/") is False
    assert captured_popen == [], "没有浏览器就不该尝试启动"


def test_open_app_window_survives_launch_failure(monkeypatch):
    """启动失败不能让程序崩——网页界面本身还在跑，退回浏览器即可。"""
    monkeypatch.setattr(open_window, "find_browser", lambda: Path(r"C:\fake\msedge.exe"))

    def boom(*a, **k):
        raise OSError("无法启动进程")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert open_window.open_app_window("http://127.0.0.1:8765/") is False
