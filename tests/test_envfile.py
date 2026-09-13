# tests/test_envfile.py - .env 读写器的单元测试
"""
`.env` 是**用户自己也在编辑**的文件，注释里写着「授权码怎么申请」这类说明。
测试重点因此是「改动要克制」：

  - 只动指定的键，注释与其他键原样保留
  - 写坏了还有备份
  - 敏感项读出来只有长度，没有内容
  - 白名单之外的键改不动（这个入口是从网页来的）
"""

import io

import pytest

from envfile import EDITABLE_KEYS, EnvFile


@pytest.fixture
def env(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# 注释：这是给我自己看的说明\n"
        "SMTP_EMAIL=me@qq.com\n"
        "SMTP_PASSWORD=sixteenchars1234\n"
        "\n"
        "# 另一段注释\n"
        "OLLAMA_MODEL=qwen2.5:3b\n",
        encoding="utf-8",
        newline="",
    )
    return EnvFile(path)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def test_as_dict_parses_pairs(env):
    data = env.as_dict()
    assert data["SMTP_EMAIL"] == "me@qq.com"
    assert data["OLLAMA_MODEL"] == "qwen2.5:3b"


def test_as_dict_ignores_comments_and_blank_lines(env):
    data = env.as_dict()
    assert all(not k.startswith("#") for k in data)
    assert "" not in data


def test_as_dict_strips_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text('A="hello world"\nB=\'x y\'\n', encoding="utf-8")
    data = EnvFile(path).as_dict()
    assert data["A"] == "hello world"
    assert data["B"] == "x y"


def test_as_dict_tolerates_export_prefix(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export SMTP_EMAIL=a@b.com\n", encoding="utf-8")
    assert EnvFile(path).as_dict()["SMTP_EMAIL"] == "a@b.com"


def test_missing_file_reads_as_empty(tmp_path):
    env = EnvFile(tmp_path / "不存在")
    assert env.as_dict() == {}
    assert env.read_raw() == ""


# ---------------------------------------------------------------------------
# 脱敏视图
# ---------------------------------------------------------------------------

def test_masked_hides_secret_content_but_reports_length(env):
    """
    敏感项**只回长度，绝不回内容**。

    界面拿不到内容，就不会因为前端被注入、误截图或日志而泄露。
    """
    view = env.masked()
    pw = view["SMTP_PASSWORD"]

    assert pw["secret"] is True
    assert pw["set"] is True
    assert pw["length"] == len("sixteenchars1234")
    assert pw["value"] is None, "授权码绝不能回给界面"


def test_masked_exposes_non_secret_values(env):
    """非敏感项要回内容，界面才能回显当前邮箱。"""
    view = env.masked()
    assert view["SMTP_EMAIL"]["value"] == "me@qq.com"
    assert view["SMTP_EMAIL"]["secret"] is False


def test_masked_reports_unset_keys(env):
    view = env.masked()
    assert view["DEEPSEEK_API_KEY"]["set"] is False
    assert view["DEEPSEEK_API_KEY"]["length"] == 0


def test_masked_includes_defaults_for_placeholders(env):
    view = env.masked()
    assert view["SMTP_SERVER"]["default"] == "smtp.qq.com"


# ---------------------------------------------------------------------------
# 写：克制与安全
# ---------------------------------------------------------------------------

def test_update_only_touches_target_key(env):
    """这是本模块存在的理由：注释不能被抹掉。"""
    before = env.read_raw()
    env.update({"SMTP_EMAIL": "new@qq.com"})
    after = env.read_raw()

    assert "new@qq.com" in after
    assert "# 注释：这是给我自己看的说明" in after, "注释必须保留"
    assert "# 另一段注释" in after
    assert "OLLAMA_MODEL=qwen2.5:3b" in after, "其他键必须原样保留"
    assert len(after.splitlines()) == len(before.splitlines())


def test_update_appends_new_key(env):
    result = env.update({"DEEPSEEK_API_KEY": "sk-test"})
    assert result["added"] == ["DEEPSEEK_API_KEY"]
    assert env.as_dict()["DEEPSEEK_API_KEY"] == "sk-test"
    # 追加不应破坏已有内容
    assert "# 注释：这是给我自己看的说明" in env.read_raw()


def test_update_reports_no_change_when_value_identical(env):
    result = env.update({"SMTP_EMAIL": "me@qq.com"})
    assert result["changed"] == []
    assert result["added"] == []


def test_update_quotes_values_with_spaces(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    env = EnvFile(path)

    env.update({"OLLAMA_MODEL": "my model:3b"})
    assert env.as_dict()["OLLAMA_MODEL"] == "my model:3b", "含空格的值必须能原样读回"


def test_update_clears_value(env):
    env.update({"OLLAMA_MODEL": ""})
    assert env.as_dict()["OLLAMA_MODEL"] == ""


def test_update_creates_backup(env):
    result = env.update({"SMTP_EMAIL": "new@qq.com"})
    assert result["backup"], "改动前必须备份"
    backup = env.path.with_suffix(env.path.suffix + ".bak")
    assert backup.is_file()
    assert "me@qq.com" in backup.read_text(encoding="utf-8"), "备份应是改动前的内容"


def test_update_rejects_keys_outside_whitelist(env):
    """
    白名单之外一律拒绝。

    这个入口是从网页来的：没有白名单的话，一个构造出来的请求
    就能改掉 MCP_AUTH_TOKEN 之类的东西。
    """
    with pytest.raises(ValueError) as e:
        env.update({"MCP_AUTH_TOKEN": "x"})
    assert "不允许修改" in str(e.value)
    assert "MCP_AUTH_TOKEN" in env.read_raw() or "MCP_AUTH_TOKEN" not in env.as_dict()


def test_update_rejects_mixed_whitelist_violation(env):
    """只要有一个越权键，整批都不写——避免「一半写进去」。"""
    with pytest.raises(ValueError):
        env.update({"SMTP_EMAIL": "new@qq.com", "PATH": "/evil"})
    assert env.as_dict()["SMTP_EMAIL"] == "me@qq.com", "越权时不应有任何写入"


def test_update_is_atomic_leaves_no_temp_files(env):
    """原子写入不应在目录里留下临时文件。"""
    env.update({"SMTP_EMAIL": "new@qq.com"})
    leftovers = [p.name for p in env.path.parent.iterdir() if p.name.startswith(".env-")]
    assert leftovers == []


def test_update_works_when_file_does_not_exist(tmp_path):
    env = EnvFile(tmp_path / "新的.env")
    env.update({"SMTP_EMAIL": "a@b.com"})
    assert env.as_dict()["SMTP_EMAIL"] == "a@b.com"


def test_update_preserves_crlf(tmp_path):
    """Windows 上 .env 可能是 CRLF，改写后不应变成混合行尾。"""
    path = tmp_path / ".env"
    path.write_bytes(b"SMTP_EMAIL=a@b.com\r\nOLLAMA_MODEL=x\r\n")
    env = EnvFile(path)

    env.update({"SMTP_EMAIL": "c@d.com"})
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"c@d.com\r\n" in raw


# ---------------------------------------------------------------------------
# 白名单本身
# ---------------------------------------------------------------------------

def test_editable_keys_cover_the_settings_we_need():
    for key in ("SMTP_EMAIL", "SMTP_PASSWORD", "OLLAMA_MODEL",
                "DEEPSEEK_API_KEY", "ACTIVE_MODEL"):
        assert key in EDITABLE_KEYS


def test_editable_keys_do_not_allow_dangerous_settings():
    """认证令牌、监听地址这类东西不该能从网页改。"""
    for key in ("MCP_AUTH_TOKEN", "MCP_HOST", "MCP_PORT"):
        assert key not in EDITABLE_KEYS
