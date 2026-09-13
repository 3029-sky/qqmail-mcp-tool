# envfile.py - 读写 .env 配置
"""
让应用内能改配置，而不是要求用户手写 .env。

三个必须守住的原则：

1. **保留注释与未知键**。`.env` 是用户自己也在编辑的文件，
   注释里写着「授权码怎么申请」这类说明。整文件重写会把它们抹掉。
   因此这里只做「按行替换/追加」，不动其他内容。

2. **凭据不回显**。读取时只回「是否已设置」与长度，绝不回内容——
   界面拿不到内容，就不会因为前端 XSS、误截图、日志而泄露。

3. **写入前备份**。改坏了用户至少还能找回上一版。
"""

import io
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

__all__ = [
    "EnvFile",
    "RESTART_KEYS",
    "SECRET_KEYS",
    "EDITABLE_KEYS",
]

#: 这些值改动后需要重建会话才生效（模型、地址等与连接相关的）。
#: 触类字段改了立刻生效（例如邮箱地址会进提示词，但重建更保险）。
RESTART_KEYS = {
    "SMTP_EMAIL", "SMTP_PASSWORD", "SMTP_SERVER", "SMTP_PORT",
    "OLLAMA_BASE_URL", "OLLAMA_MODEL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "ACTIVE_MODEL",
}

#: 这些键的内容**绝不回显**给界面，只报「是否已设置」。
SECRET_KEYS = {"SMTP_PASSWORD", "DEEPSEEK_API_KEY", "MCP_AUTH_TOKEN"}

#: 界面上允许编辑的键。刻意用白名单：
#: 否则一个构造出来的请求就能改掉 MCP_AUTH_TOKEN 之类的东西。
EDITABLE_KEYS = {
    "SMTP_EMAIL", "SMTP_PASSWORD", "SMTP_SERVER", "SMTP_PORT",
    "OLLAMA_BASE_URL", "OLLAMA_MODEL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "ACTIVE_MODEL",
}

#: 默认值，用于界面显示占位符
DEFAULTS = {
    "SMTP_SERVER": "smtp.qq.com",
    "SMTP_PORT": "465",
    "OLLAMA_BASE_URL": "http://localhost:11434",
    "OLLAMA_MODEL": "qwen2.5:3b",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    "DEEPSEEK_MODEL": "deepseek-chat",
}


def _needs_quotes(value: str) -> bool:
    """含空格或特殊字符的值要加引号，否则 dotenv 解析会截断。"""
    if value == "":
        return False
    return any(ch in value for ch in ' #"\'\t')


def _format_line(key: str, value: str) -> str:
    if _needs_quotes(value):
        escaped = value.replace('"', '\\"')
        return '%s="%s"' % (key, escaped)
    return "%s=%s" % (key, value)


class EnvFile:
    """
    一个 .env 文件的最小读写器。

    刻意不引入 python-dotenv 的写入能力：它的 `set_key` 会重排文件、
    丢掉注释。而这里的注释是有用的用户文档。
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    # -- 读 -----------------------------------------------------------------

    def read_raw(self) -> str:
        if not self.path.is_file():
            return ""
        return io.open(self.path, encoding="utf-8", newline="").read()

    def as_dict(self) -> Dict[str, str]:
        """解析出「键 -> 值」。忽略注释与空行，不做变量展开。"""
        result: Dict[str, str] = {}
        for line in self.read_raw().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export "):].strip()
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            # 去掉成对的引号
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].replace('\\"', '"')
            if key:
                result[key] = value
        return result

    def masked(self, keys: Optional[List[str]] = None) -> Dict[str, Dict[str, object]]:
        """
        给界面用的配置视图：**不含任何凭据内容**。

        返回 {key: {"set": bool, "length": int, "value": str|None, "secret": bool}}。
        非敏感键会带上 value，方便界面回显；敏感键只报长度。
        """
        current = self.as_dict()
        wanted = keys if keys is not None else sorted(EDITABLE_KEYS)

        view: Dict[str, Dict[str, object]] = {}
        for key in wanted:
            raw = current.get(key, "")
            is_secret = key in SECRET_KEYS
            view[key] = {
                "set": bool(raw),
                "secret": is_secret,
                "length": len(raw),
                "value": None if is_secret else raw,
                "default": DEFAULTS.get(key, ""),
            }
        return view

    # -- 写 -----------------------------------------------------------------

    def backup(self) -> Optional[Path]:
        """写入前备份一份，返回备份路径。文件不存在时不备份。"""
        if not self.path.is_file():
            return None
        target = self.path.with_suffix(self.path.suffix + ".bak")
        try:
            shutil.copy2(self.path, target)
            return target
        except OSError:
            return None

    def update(self, changes: Dict[str, str]) -> Dict[str, object]:
        """
        按键更新 .env 并原子落盘。

        只允许改白名单里的键；其他键一律拒绝——
        这个入口是从网页来的，不能让它改掉任意配置。
        值为空字符串表示「清空这一项」（例如去掉 DeepSeek Key）。

        返回 {"changed": [...], "added": [...], "backup": str|None}。
        """
        rejected = [k for k in changes if k not in EDITABLE_KEYS]
        if rejected:
            raise ValueError("不允许修改这些配置项：%s" % "、".join(sorted(rejected)))

        original = self.read_raw()
        # 保留原来的行尾风格。Windows 上用户可能用记事本编辑过 .env（CRLF），
        # 我们改写后若变成 LF，整份文件的行尾就混杂了——虽然 dotenv 两种都认，
        # 但下次用 git 看 diff 会显示「整个文件都改了」。
        newline = "\r\n" if "\r\n" in original else "\n"
        lines = original.splitlines()
        # 只解析一次：循环里反复调用会把整个文件重读重解析几十遍
        before = self.as_dict()

        changed: List[str] = []
        added: List[str] = []
        seen = set()

        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            probe = stripped[len("export "):] if stripped.startswith("export ") else stripped
            if "=" not in probe:
                continue
            key = probe.split("=", 1)[0].strip()
            if key not in changes:
                continue

            new_value = changes[key]
            if before.get(key, "") == new_value:
                seen.add(key)
                continue          # 值没变，不制造无意义的 diff
            # 保留行首缩进
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = indent + _format_line(key, new_value)
            changed.append(key)
            seen.add(key)

        for key, value in changes.items():
            if key in seen:
                continue
            if value == "" and key not in before:
                continue          # 本来就没有、又要清空 -> 不必追加
            lines.append(_format_line(key, value))
            added.append(key)

        if not changed and not added:
            return {"changed": [], "added": [], "backup": None}

        backup = self.backup()
        body = newline.join(lines)
        if body and not body.endswith(newline):
            body += newline
        self._write_atomic(body, newline=newline)
        return {
            "changed": sorted(changed),
            "added": sorted(added),
            "backup": str(backup) if backup else None,
        }

    def _write_atomic(self, body: str, newline: str = "\n") -> None:
        """
        先写同目录临时文件再替换。

        为什么必须同目录：跨文件系统的 `os.replace` 不是原子操作。
        为什么必须原子：写到一半被打断会留下半个文件，
        那里面是用户的授权码——丢了就得重新申请。

        newline="" 表示不做任何换行转换，由调用方决定行尾，
        这样文件里是什么行尾，写回去就还是什么行尾。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".env-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
