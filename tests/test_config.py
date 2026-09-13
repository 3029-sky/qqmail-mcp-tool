# tests/test_config.py - 配置层测试
"""
重点验证：凭据只能来自 .env / 环境变量，代码中不存在可用的硬编码默认值。
"""

import pydantic
import pytest

from config import Settings, settings


def test_settings_loaded_from_env():
    assert settings.smtp_email, "SMTP_EMAIL 应从 .env 读入"
    assert settings.smtp_password, "SMTP_PASSWORD 应从 .env 读入"
    assert settings.smtp_server == "smtp.qq.com"
    assert settings.smtp_port == 465


def test_password_has_no_hardcoded_default():
    """
    缺少凭据时必须报错，绝不能悄悄回退到一个内置密码。
    这是「代码中不存在默认硬编码密码」的可执行证明。
    """
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)


def test_email_has_no_hardcoded_default():
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, smtp_password="dummy")


def test_valid_credentials_are_accepted():
    s = Settings(_env_file=None, smtp_email="a@b.com", smtp_password="x")
    assert s.smtp_email == "a@b.com"
    assert s.mcp_port == 8000
    assert s.ollama_model == "qwen2.5:3b"


def test_non_sensitive_fields_have_defaults():
    s = Settings(_env_file=None, smtp_email="a@b.com", smtp_password="x")
    assert s.smtp_server == "smtp.qq.com"
    assert s.smtp_port == 465
    assert s.mcp_host == "0.0.0.0"
