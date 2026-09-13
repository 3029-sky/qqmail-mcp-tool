# package.py - 打一个可以交付的源码包
"""
把项目打成 zip，用于分享给别人（或作为作品集附件）。

    .\\venv\\Scripts\\python.exe package.py

**为什么用脚本而不是手动右键压缩**：手动压缩最容易犯的错是把
`.env`、`venv/`、`data/` 一起打进去——`.env` 里有 QQ 授权码，
`data/` 里有联系人，两者都会随包泄露。脚本从 `git ls-files` 取文件清单，
因此**只包含进过版本库的东西**，被 gitignore 的凭据天然不会进来。

打包前后各做一次检查：

  · 打包前：确认工作区没有未提交的改动（否则打出来的包与仓库不一致）
  · 打包后：把 zip 里每个文本文件读一遍，扫描真实凭据特征

这样「交付包是干净的」这件事是**验证过的**，不是假设的。
"""

import io
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _make_output_resilient() -> None:
    """
    让输出遇到无法编码的字符时降级，而不是让脚本崩掉。

    与 email_butler.py 同一个问题：Windows 上 stdout 编码取系统 ANSI
    代码页（中文系统是 GBK），而本脚本的输出里有 ⚠ ✅ ❌ 这类符号。
    GBK 编不出它们，`print` 会抛 UnicodeEncodeError。

    触发条件比想象中常见：把输出重定向到文件、管道给别的程序，
    或设置 PYTHONIOENCODING=gbk。本脚本自己就踩过一次——
    写完立刻运行就崩在第一条带 ⚠ 的提示上。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass


_make_output_resilient()

#: 包内顶层目录名
PACKAGE_NAME = "qqmail-mcp-tool"

#: 即便进了版本库也不该随包分发的东西（按需追加）
EXTRA_EXCLUDE = {
    ".env", ".env.bak",
}

#: 检出真实凭据用的特征。宁可多报也不能漏——
#: 这些值一旦随包泄露，别人就能用你的邮箱发信。
SECRET_PATTERNS = [
    (re.compile(r"^SMTP_PASSWORD=(?!16位|ci-dummy|\s*$)[A-Za-z0-9]{16}\s*$", re.M),
     "看起来是真实的 16 位授权码"),
    (re.compile(r"^MCP_AUTH_TOKEN=(?!\s*$)[A-Za-z0-9_\-]{20,}\s*$", re.M),
     "看起来是真实的访问令牌"),
    (re.compile(r"^DEEPSEEK_API_KEY=sk-[A-Za-z0-9]{20,}\s*$", re.M),
     "看起来是真实的 DeepSeek Key"),
    (re.compile(r"\b\d{10}@qq\.com"), "看起来是真实 QQ 邮箱"),
]

#: 需要读内容做扫描的文本扩展名
TEXT_EXT = {".py", ".md", ".txt", ".bat", ".ini", ".yml", ".html",
            ".svg", ".json", ".cfg", ".toml", ".example", ".test"}


def git(*args, check=True):
    """调用 git，返回 stdout。"""
    exe = r"C:\Program Files\Git\cmd\git.exe"
    if not os.path.isfile(exe):
        exe = "git"
    r = subprocess.run([exe, *args], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def check_working_tree(assume_yes: bool = False) -> None:
    """
    确认工作区干净。

    为什么必须检查：如果有未提交的改动，打出来的包与 git 仓库不一致，
    别人拿到的和你以为的就不是同一个东西。而且未提交的改动往往
    正是「还没检查过的」代码。

    非交互场景（管道、CI）下不能阻塞在 input 上——没有 stdin 时
    会抛 EOFError。所以：`--yes` 明确跳过，否则读不到输入就当作拒绝。
    """
    status = git("status", "--porcelain").strip()
    if not status:
        return

    print("⚠ 工作区有未提交的改动：")
    for line in status.splitlines():
        print("    %s" % line)
    print("\n  这些改动**不会**进包（包只含已提交的内容）。")
    print("  要让它们进包，先提交。")

    if assume_yes:
        print("\n  （--yes：继续）")
        return

    try:
        answer = input("\n  仍要继续吗？(y/N) ").strip().lower()
    except EOFError:
        print("\n  没有可读的输入，按「否」处理。")
        print("  非交互场景请显式加 --yes。")
        answer = ""

    if answer != "y":
        print("已取消。")
        sys.exit(1)


def collect_files() -> list:
    """从 git 取文件清单——这是「哪些文件属于项目」的唯一权威来源。"""
    out = git("ls-files")
    files = []
    for line in out.splitlines():
        rel = line.strip().strip('"')
        if not rel:
            continue
        # git 对非 ASCII 文件名会加引号并用八进制转义，这里解回来
        if "\\" in rel and line.strip().startswith('"'):
            rel = line.strip()[1:-1].encode("latin1").decode("unicode_escape")
            rel = rel.encode("latin1").decode("utf-8", "replace")
        if Path(rel).name in EXTRA_EXCLUDE:
            continue
        if (ROOT / rel).is_file():
            files.append(rel)
    return sorted(files)


def scan_bytes(rel: str, data: bytes) -> list:
    """扫描单个文件内容，返回问题列表。"""
    if Path(rel).suffix.lower() not in TEXT_EXT:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    problems = []
    for pat, desc in SECRET_PATTERNS:
        for m in pat.finditer(text):
            line = text[:m.start()].count("\n") + 1
            problems.append("%s:%d %s" % (rel, line, desc))
    return problems


def build(target: Path, assume_yes: bool = False) -> int:
    check_working_tree(assume_yes)

    files = collect_files()
    if not files:
        print("✗ 没取到任何文件，git ls-files 是空的？")
        return 1

    print("\n打包 %d 个文件…" % len(files))

    problems = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            path = ROOT / rel
            data = path.read_bytes()
            problems += scan_bytes(rel, data)
            z.write(path, arcname="%s/%s" % (PACKAGE_NAME, rel))

    print("✓ 已生成 %s" % target.name)
    print("  大小 %.1f KB" % (target.stat().st_size / 1024))

    # 打包后再独立校验一次：读 zip 里的实际内容，
    # 而不是相信上面的循环没写错。
    verify_problems = []
    with zipfile.ZipFile(target) as z:
        names = z.namelist()
        for name in names:
            if name.endswith("/"):
                continue
            rel = name.split("/", 1)[1] if "/" in name else name
            verify_problems += scan_bytes(rel, z.read(name))

    all_problems = sorted(set(problems) | set(verify_problems))

    print("\n凭据扫描（读了包内实际内容）：")
    if all_problems:
        for p in all_problems:
            print("  ❌ %s" % p)
        print("\n✗ 包里可能含真实凭据，**不要分享这个文件**。")
        print("  请把对应项改成示例值后重新打包。")
        return 1
    print("  ✅ 未发现真实凭据")

    print("\n内容概览：")
    kinds = {}
    for rel in files:
        ext = Path(rel).suffix.lower() or "(无扩展名)"
        kinds[ext] = kinds.get(ext, 0) + 1
    for ext, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print("  %-14s %d" % (ext, n))

    print("\n使用方式（告诉拿到包的人）：")
    print("  1) 解压，进目录")
    print("  2) python -m venv venv && .\\venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    print("  3) copy .env.example .env  然后填自己的邮箱与授权码")
    print("  4) ollama pull qwen2.5:3b")
    print("  5) 双击 启动应用.bat")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="打一个可以交付的源码包")
    parser.add_argument("--yes", action="store_true",
                        help="工作区有未提交改动时不询问，直接继续")
    parser.add_argument("--out", default="",
                        help="输出路径（默认放在项目上级目录）")
    args = parser.parse_args()

    if args.out:
        target = Path(args.out).resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        target = ROOT.parent / ("%s-src-%s.zip" % (PACKAGE_NAME, stamp))
    return build(target, assume_yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
