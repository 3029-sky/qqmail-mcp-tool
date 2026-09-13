# userdata.py - 用户自己的数据：联系人、邮件模板、签名
"""
存在 `data/userdata.json`（已被 .gitignore 忽略），与 `.env` 分开：

  - `.env` 是**配置**：邮箱、授权码、模型。改错会导致程序跑不起来。
  - `userdata.json` 是**内容**：联系人、模板、签名。改错最多是发错一次。

分开的理由不是洁癖：这两类东西的读写风险完全不同。配置写坏要重新申请
授权码，内容写坏只需改回来。混在一个文件里，任何一次「改签名」都要
碰含授权码的文件，出问题的代价被放大了。

写入策略与 envfile 一致：**同目录临时文件 + os.replace**，并留一份
`.bak`。整体是「读-改-写」，所以调用方不需要关心并发——
本应用是单用户本地程序，用一把进程内锁足够。
"""

import io
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["UserData", "userdata"]

#: 上限。这些只是防呆，不是安全边界——本地单用户应用，用户想填多少都行，
#: 但无上限会让界面与提示词变得难以收拾。
MAX_CONTACTS = 500
MAX_TEMPLATES = 200
MAX_TEXT = 20000

#: 邮箱格式的宽松校验。刻意不比这个更严：
#: 过度严格的正则会拒掉合法地址（例如带 + 的、中文域名的）。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserDataError(ValueError):
    """内容不合法。消息可直接展示给用户。"""


class UserData:
    """联系人 / 模板 / 签名的读写。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- 底层 ---------------------------------------------------------------

    def _empty(self) -> Dict[str, Any]:
        return {"contacts": [], "templates": [], "signature": ""}

    def load(self) -> Dict[str, Any]:
        """读全部数据。文件不存在或损坏时返回空结构，不抛异常。"""
        if not self.path.is_file():
            return self._empty()
        try:
            raw = io.open(self.path, encoding="utf-8").read()
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError):
            # 损坏时不要让整个应用起不来。返回空结构，用户可以在界面上重填。
            return self._empty()

        if not isinstance(data, dict):
            return self._empty()

        result = self._empty()
        contacts = data.get("contacts")
        if isinstance(contacts, list):
            result["contacts"] = [c for c in contacts if self._valid_contact(c)]
        templates = data.get("templates")
        if isinstance(templates, list):
            result["templates"] = [t for t in templates if self._valid_template(t)]
        signature = data.get("signature")
        if isinstance(signature, str):
            result["signature"] = signature
        return result

    def save(self, data: Dict[str, Any]) -> None:
        """原子写入，并留一份 .bak。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            try:
                shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
            except OSError:
                pass

        body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".userdata-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, mutate) -> Dict[str, Any]:
        """
        读-改-写。mutate(data) 就地修改并返回结果。

        加锁是为了让「读-改-写」不被并发请求插队——
        例如同时保存联系人和模板时，后写的会覆盖先写的。
        """
        with self._lock:
            data = self.load()
            result = mutate(data)
            self.save(data)
            return result if result is not None else data

    # -- 校验 ---------------------------------------------------------------

    @staticmethod
    def _valid_contact(item: Any) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("name"), str) and item["name"].strip()
            and isinstance(item.get("email"), str) and item["email"].strip()
        )

    @staticmethod
    def _valid_template(item: Any) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("name"), str) and item["name"].strip()
        )

    # -- 联系人 -------------------------------------------------------------

    def add_contact(self, name: str, email: str, note: str = "") -> Dict[str, Any]:
        name = (name or "").strip()
        email = (email or "").strip()
        note = (note or "").strip()

        if not name:
            raise UserDataError("联系人姓名不能为空。")
        if not _EMAIL_RE.match(email):
            raise UserDataError("邮箱地址看起来不对：%s" % email)

        def mutate(data):
            if len(data["contacts"]) >= MAX_CONTACTS:
                raise UserDataError("联系人已达上限（%d 个）。" % MAX_CONTACTS)
            # 同名视为「更新」而不是新增：用户重复添加时想改的是同一个
            for item in data["contacts"]:
                if item["name"] == name:
                    item["email"] = email
                    item["note"] = note
                    return item
            item = {"name": name, "email": email, "note": note}
            data["contacts"].append(item)
            return item

        return self.update(mutate)

    def remove_contact(self, name: str) -> bool:
        def mutate(data):
            before = len(data["contacts"])
            data["contacts"] = [c for c in data["contacts"] if c["name"] != name]
            return len(data["contacts"]) != before

        return self.update(mutate)

    def find_contacts(self, text: str) -> List[Dict[str, Any]]:
        """
        从一句话里找出提到的联系人。

        为什么需要它：用户会说「发给张三」，而模型只认识邮箱地址。
        把联系人名单写进系统提示词后，模型就能自己把「张三」换成地址——
        但那是**模型的**判断。这里提供确定性的查找，用于界面提示与批量发送。
        """
        if not text:
            return []
        data = self.load()
        hits = []
        for item in data["contacts"]:
            name = item["name"]
            if name and name in text:
                hits.append(item)
        # 名字长的优先：避免「张三」先于「张三丰」命中
        hits.sort(key=lambda c: len(c["name"]), reverse=True)
        return hits

    def resolve_recipient(self, token: str) -> Optional[Dict[str, Any]]:
        """把「张三」解析成联系人。找不到返回 None。"""
        token = (token or "").strip()
        if not token:
            return None
        for item in self.load()["contacts"]:
            if item["name"] == token:
                return item
        return None

    # -- 模板 ---------------------------------------------------------------

    def add_template(self, name: str, subject: str = "", body: str = "",
                     to: str = "") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise UserDataError("模板名称不能为空。")
        if len(name) > 60:
            raise UserDataError("模板名称太长（最多 60 字）。")

        subject = (subject or "").strip()[:200]
        body = (body or "")[:MAX_TEXT]
        to = (to or "").strip()

        def mutate(data):
            if len(data["templates"]) >= MAX_TEMPLATES:
                raise UserDataError("模板已达上限（%d 个）。" % MAX_TEMPLATES)
            for item in data["templates"]:
                if item["name"] == name:
                    item.update(subject=subject, body=body, to=to)
                    return item
            item = {"name": name, "subject": subject, "body": body, "to": to}
            data["templates"].append(item)
            return item

        return self.update(mutate)

    def remove_template(self, name: str) -> bool:
        def mutate(data):
            before = len(data["templates"])
            data["templates"] = [t for t in data["templates"] if t["name"] != name]
            return len(data["templates"]) != before

        return self.update(mutate)

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        for item in self.load()["templates"]:
            if item["name"] == name:
                return item
        return None

    # -- 签名 ---------------------------------------------------------------

    def set_signature(self, text: str) -> str:
        text = (text or "")[:MAX_TEXT]
        self.update(lambda data: data.__setitem__("signature", text))
        return text

    def get_signature(self) -> str:
        return self.load()["signature"]

    def with_signature(self, body: str) -> str:
        """
        在正文末尾追加签名；**已经带上就不重复加**。

        为什么必须幂等：模型经常自己把签名写进正文（提示词里告诉了它），
        若这里再无脑追加，用户就会收到两个签名。发出去的东西收不回来，
        所以宁可多一层判断。

        判定方式用「正文里是否已包含签名文本」而不是精确比对末尾——
        模型可能把签名放在引号里或调整了换行，精确比对会漏判。
        """
        signature = self.get_signature().strip()
        if not signature:
            return body
        text = body or ""
        if signature in text:
            return text
        if text and not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        return text + signature


#: 全局实例。放在 data/ 下，与配置分开。
userdata = UserData(Path(__file__).resolve().parent / "data" / "userdata.json")
