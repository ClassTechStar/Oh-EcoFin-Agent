"""安全与合规：密钥三级读取优先级 / 日志脱敏 golden / 局域网令牌。"""

from __future__ import annotations

import logging

import pytest

from backend.security.logmask import mask_result_for_storage, mask_text
from backend.security.tokens import generate_token, verify_token


# ---------- 日志脱敏 ----------

GOLDEN_CASES = [
    # (输入, 是否包含掩码)
    ("api_key=sk-abcdefgh12345678", True),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y", True),
    ("联系手机号 13812345678 确认", True),
    ("secret: mysecretvalue123", True),
    ("token=ghu_abcdef1234567890abcdef", True),
    ("普通日志：600519 涨幅 1.2%", False),
    ("sk-proj-xxxxxxxxxxxxxxxxxxxx", True),
]


@pytest.mark.parametrize("text,should_mask", GOLDEN_CASES)
def test_mask_text_golden(text, should_mask):
    out = mask_text(text)
    if should_mask:
        assert "***MASKED***" in out, f"未掩蔽: {text}"
        assert "sk-abcdefgh" not in out
    else:
        assert "***MASKED***" not in out


def test_mask_phone_standalone():
    out = mask_text("13912345678")
    assert "13912345678" not in out
    # 非 11 位手机号不受影响
    assert mask_text("1391234567") == "1391234567"


def test_mask_result_recursive():
    obj = {"name": "贵州茅台", "note": "call 13812345678", "nested": {"k": "sk-abcdef12345678"},
           "list": ["普通", "Bearer abcdefghijk"]}
    out = mask_result_for_storage(obj)
    assert "13812345678" not in str(out)
    assert "sk-abcdef12345678" not in str(out)
    assert out["name"] == "贵州茅台"


def test_logging_filter_masks():
    logger = logging.getLogger("test.mask")
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger.addHandler(Capture())
    from backend.security.logmask import SensitiveFilter

    logger.addFilter(SensitiveFilter())
    logger.warning("api_key=sk-abcdef12345678 leaked")
    assert records and "sk-abcdef12345678" not in records[0]


# ---------- 局域网令牌 ----------

def test_token_generate_unique():
    t1, t2 = generate_token(), generate_token()
    assert t1 != t2 and len(t1) >= 20


def test_token_verify_constant_time():
    expected = generate_token()
    assert verify_token(expected, expected) is True
    assert verify_token("wrong-token-value-here", expected) is False
    assert verify_token(None, expected) is False  # 未携带 → 拒绝（403 语义）
    # expected 为空 → 未开启校验（仅 127.0.0.1 场景）
    assert verify_token(None, None) is True
    assert verify_token("anything", None) is True


# ---------- 密钥三级读取 ----------

def test_key_env_priority(tmp_path, monkeypatch):
    from backend.security import keyring_loader as kl

    monkeypatch.setenv("FINAGENT_TEST_KEY", "from-env")
    monkeypatch.setattr(kl, "KEY_ENV_MAP", {"test_key": "FINAGENT_TEST_KEY"})
    monkeypatch.setattr(kl, "ENV_FALLBACK_FILE", tmp_path / "no.env")
    import backend.security.keyring_loader as k2

    monkeypatch.setattr(k2, "_read_dotenv", lambda k: "")
    assert kl.load_key("test_key") == "from-env"


def test_key_dotenv_fallback(tmp_path, monkeypatch):
    from backend.security import keyring_loader as kl

    env_file = tmp_path / ".env"
    env_file.write_text("FINAGENT_DEEPSEEK_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("FINAGENT_DEEPSEEK_KEY", raising=False)
    monkeypatch.setattr(kl, "ENV_FALLBACK_FILE", env_file)
    monkeypatch.setattr(kl, "load_key", kl.load_key)
    # keyring 层抛异常/返回空 → 落到 .env
    import backend.security.keyring_loader as k2

    def fake_keyring(name):
        raise RuntimeError("no keyring")

    monkeypatch.setattr(k2, "load_key", k2.load_key)
    out = kl.load_key("deepseek_key")
    assert out == "from-dotenv"


def test_missing_required_keys(tmp_path, monkeypatch):
    from backend.security import keyring_loader as kl

    monkeypatch.delenv("FINAGENT_GLM_KEY", raising=False)
    monkeypatch.delenv("FINAGENT_DEEPSEEK_KEY", raising=False)
    monkeypatch.setattr(kl, "ENV_FALLBACK_FILE", tmp_path / "no.env")
    missing = kl.missing_required_keys()
    assert "glm_key" in missing and "deepseek_key" in missing
