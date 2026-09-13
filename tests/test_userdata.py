# tests/test_userdata.py - 联系人 / 模板 / 签名的读写测试
"""
`userdata.json` 与 `.env` 分开的理由是风险不同，但两者都要求同一件事：
**写坏了不能丢用户的数据**。因此重点覆盖：

  - 原子写 + 备份
  - 文件损坏时不崩、不静默清空
  - 校验拦住明显错误（空姓名、非法邮箱）
  - 签名的幂等追加（模型已写签名时不能变成两个）
"""

import io
import json

import pytest

from userdata import MAX_CONTACTS, UserData, UserDataError


@pytest.fixture
def store(tmp_path):
    return UserData(tmp_path / "userdata.json")


# ---------------------------------------------------------------------------
# 基础读写
# ---------------------------------------------------------------------------

def test_load_returns_empty_structure_when_missing(store):
    data = store.load()
    assert data == {"contacts": [], "templates": [], "signature": ""}


def test_save_then_load_roundtrip(store):
    store.add_contact("张三", "zs@example.com")
    assert store.load()["contacts"][0]["name"] == "张三"


def test_load_survives_corrupted_file(tmp_path):
    """
    文件坏了不能让整个应用起不来。

    宁可丢掉内容（用户能在界面上重填），也不要因为一个 JSON 语法错误
    导致程序完全无法启动——那时用户连「去修好它」的界面都没有。
    """
    path = tmp_path / "userdata.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")

    data = UserData(path).load()
    assert data == {"contacts": [], "templates": [], "signature": ""}


def test_load_survives_wrong_top_level_type(tmp_path):
    path = tmp_path / "userdata.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert UserData(path).load()["contacts"] == []


def test_load_drops_malformed_entries(tmp_path):
    """个别条目缺字段时只丢掉它，不丢整份文件。"""
    path = tmp_path / "userdata.json"
    path.write_text(json.dumps({
        "contacts": [
            {"name": "好的", "email": "a@b.com"},
            {"name": "", "email": "bad"},          # 空名字
            {"email": "no-name@b.com"},            # 缺 name
            "完全不是对象",
        ],
        "templates": [{"name": "有效"}, {"no_name": 1}],
        "signature": 123,
    }), encoding="utf-8")

    data = UserData(path).load()
    assert [c["name"] for c in data["contacts"]] == ["好的"]
    assert [t["name"] for t in data["templates"]] == ["有效"]
    assert data["signature"] == ""      # 类型不对 -> 当成没设


def test_save_creates_backup(store):
    store.add_contact("张三", "zs@example.com")
    store.add_contact("李四", "ls@example.com")

    backup = store.path.with_suffix(store.path.suffix + ".bak")
    assert backup.is_file()
    assert "张三" in backup.read_text(encoding="utf-8"), "备份应是上一次的内容"


def test_save_leaves_no_temp_files(store):
    store.add_contact("张三", "zs@example.com")
    leftovers = [p.name for p in store.path.parent.iterdir()
                 if p.name.startswith(".userdata-")]
    assert leftovers == []


def test_saved_file_is_utf8_readable(store):
    """中文不能变成转义序列——用户可能自己去编辑这个文件。"""
    store.add_contact("张三", "zs@example.com")
    raw = io.open(store.path, encoding="utf-8").read()
    assert "张三" in raw
    assert "\\u5f20" not in raw


# ---------------------------------------------------------------------------
# 联系人
# ---------------------------------------------------------------------------

def test_add_contact(store):
    item = store.add_contact("张三", "zs@example.com", note="同事")
    assert item["name"] == "张三"
    assert item["email"] == "zs@example.com"
    assert item["note"] == "同事"


def test_add_contact_trims_whitespace(store):
    item = store.add_contact("  张三  ", "  zs@example.com  ")
    assert item["name"] == "张三"
    assert item["email"] == "zs@example.com"


def test_add_same_name_updates_instead_of_duplicating(store):
    """重复添加同名联系人，用户的意图是「改掉它」，不是「要两个张三」。"""
    store.add_contact("张三", "old@example.com")
    store.add_contact("张三", "new@example.com")

    contacts = store.load()["contacts"]
    assert len(contacts) == 1
    assert contacts[0]["email"] == "new@example.com"


@pytest.mark.parametrize("bad", ["", "   "])
def test_add_contact_rejects_empty_name(store, bad):
    with pytest.raises(UserDataError) as e:
        store.add_contact(bad, "a@b.com")
    assert "姓名" in str(e.value)


@pytest.mark.parametrize("bad", ["不是邮箱", "a@b", "@b.com", "a@", "a b@c.com", ""])
def test_add_contact_rejects_bad_email(store, bad):
    """地址编错了邮件就发不出去，且报错会很难懂，所以在入口就拦住。"""
    with pytest.raises(UserDataError) as e:
        store.add_contact("张三", bad)
    assert "邮箱" in str(e.value)


def test_add_contact_rejects_when_full(store, monkeypatch):
    import userdata

    monkeypatch.setattr(userdata, "MAX_CONTACTS", 2)
    store.add_contact("A", "a@b.com")
    store.add_contact("B", "b@b.com")
    with pytest.raises(UserDataError) as e:
        store.add_contact("C", "c@b.com")
    assert "上限" in str(e.value)


def test_remove_contact(store):
    store.add_contact("张三", "zs@example.com")
    assert store.remove_contact("张三") is True
    assert store.load()["contacts"] == []


def test_remove_missing_contact_returns_false(store):
    assert store.remove_contact("不存在") is False


def test_resolve_recipient(store):
    store.add_contact("张三", "zs@example.com")
    assert store.resolve_recipient("张三")["email"] == "zs@example.com"
    assert store.resolve_recipient("李四") is None
    assert store.resolve_recipient("") is None


def test_find_contacts_matches_substring(store):
    store.add_contact("张三", "zs@example.com")
    store.add_contact("李四", "ls@example.com")

    hits = store.find_contacts("帮我发给张三一封邮件")
    assert [c["name"] for c in hits] == ["张三"]


def test_find_contacts_prefers_longer_name(store):
    """
    「张三」和「张三丰」都在名单里时，「张三丰」应优先。

    否则用户说「发给张三丰」会先命中「张三」——发错人。
    """
    store.add_contact("张三", "zs@example.com")
    store.add_contact("张三丰", "zsf@example.com")

    hits = store.find_contacts("发给张三丰")
    assert hits[0]["name"] == "张三丰"


def test_find_contacts_returns_empty_for_no_match(store):
    store.add_contact("张三", "zs@example.com")
    assert store.find_contacts("发给王五") == []
    assert store.find_contacts("") == []


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

def test_add_template(store):
    item = store.add_template("周报", subject="本周周报", body="本周完成了…")
    assert item["name"] == "周报"
    assert item["subject"] == "本周周报"


def test_add_template_requires_name(store):
    with pytest.raises(UserDataError):
        store.add_template("   ")


def test_add_same_template_name_updates(store):
    store.add_template("周报", subject="旧")
    store.add_template("周报", subject="新")

    templates = store.load()["templates"]
    assert len(templates) == 1
    assert templates[0]["subject"] == "新"


def test_get_template(store):
    store.add_template("周报", subject="本周周报")
    assert store.get_template("周报")["subject"] == "本周周报"
    assert store.get_template("没有") is None


def test_remove_template(store):
    store.add_template("周报")
    assert store.remove_template("周报") is True
    assert store.load()["templates"] == []


def test_template_subject_is_length_capped(store):
    item = store.add_template("长主题", subject="主" * 500)
    assert len(item["subject"]) == 200


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------

def test_set_and_get_signature(store):
    store.set_signature("—— 李四\n研发部")
    assert store.get_signature() == "—— 李四\n研发部"


def test_signature_defaults_to_empty(store):
    assert store.get_signature() == ""


def test_with_signature_appends(store):
    store.set_signature("—— 李四")
    assert store.with_signature("正文") == "正文\n\n—— 李四"


def test_with_signature_is_idempotent(store):
    """
    已经有签名就不再追加。

    模型常自己把签名写进正文（提示词里告诉了它），若这里无脑追加，
    用户会收到两个签名——而发出去的邮件收不回来。
    """
    store.set_signature("—— 李四")
    body = "正文\n\n—— 李四"
    assert store.with_signature(body) == body


def test_with_signature_detects_signature_in_middle(store):
    """
    签名出现在正文中间（模型把它插错位置）也算「已有」。

    判定用「包含」而不是「末尾精确匹配」：模型可能调整换行或加引号，
    精确比对会漏判，于是变成两个签名。
    """
    store.set_signature("—— 李四")
    body = "开头\n—— 李四\n结尾"
    assert store.with_signature(body) == body


def test_with_signature_noop_when_unset(store):
    assert store.with_signature("正文") == "正文"


def test_with_signature_handles_empty_body(store):
    store.set_signature("—— 李四")
    assert store.with_signature("") == "—— 李四"


def test_with_signature_normalizes_trailing_newlines(store):
    """正文末尾已有换行时不要堆成一堆空行。"""
    store.set_signature("—— 李四")
    assert store.with_signature("正文\n") == "正文\n\n—— 李四"
    assert store.with_signature("正文\n\n") == "正文\n\n—— 李四"


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------

def test_concurrent_updates_do_not_lose_data(store):
    """
    「读-改-写」必须加锁。

    不加锁时两个线程可能读到同一份旧数据，后写的覆盖先写的——
    表现就是「刚加的联系人莫名其妙没了」。
    """
    import threading

    def add(i):
        store.add_contact("联系人%d" % i, "u%d@example.com" % i)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.load()["contacts"]) == 20
