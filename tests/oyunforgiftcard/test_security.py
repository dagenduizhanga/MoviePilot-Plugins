"""
安全测试：加密本身的性质，以及几种模拟攻击。

- 拿到整个配置目录（数据库 + 插件数据 + 日志）但没有口令：解不开；
- 口令猜错 / 常见口令暴力猜：解不开，且每次猜测都要付出 scrypt 的代价；
- 密文被改动一位：解密失败（不会悄悄得到错误的数据）；
- 跑完一次完整的银行卡购买后，整个配置目录里逐字节搜不到卡号、CVV、密码、Cookie、兑换码的明文。
"""
import base64
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from app.plugins.oyunforgiftcard.vault import ENV_NAME, SCRYPT_N, Vault, VaultError

from .conftest import CARD, TEST_SECRET, codes_of, make_config, set_secrets, wait_idle, wait_message

CONFIG_DIR = Path(os.environ["CONFIG_DIR"])

# 用很有辨识度的值，扫描时不会误报
SECRETS = {
    "cookie": "oyf_session=VALID; trk=LEAKCHECK-COOKIE-7f3a9c",
    "username": "leakcheck.user@example.com",
    "password": "Pw-Unique-9f3K!xLeak",
    **CARD,
    "card_holder": "LEAKCHECK HOLDER",
}
CODE = "X01001QZ1ABCDEFG"


def _vault(directory, secret=TEST_SECRET):
    return Vault(directory, aad="OyunforGiftCard", env={ENV_NAME: secret} if secret is not None else {})


# ---------------------------------------------------------------- 加密本身
def test_roundtrip_and_ciphertext_reveals_nothing(tmp_path):
    vault = _vault(tmp_path)
    payload = {"card_number": "4242424242424242", "card_cvv": "123", "password": "Pw-Unique-9f3K!xLeak"}
    blob = vault.encrypt(payload, purpose="secrets")
    assert vault.decrypt(blob, purpose="secrets") == payload
    raw = json.dumps(blob)
    for needle in ("4242", "Pw-Unique", "card_number", "card_cvv", "password"):
        assert needle not in raw
    assert blob["alg"] == "AES-256-GCM" and blob["kdf"] == "scrypt"
    assert len(base64.b64decode(blob["nonce"])) == 12
    # 同样的明文每次加密结果都不同（随机 nonce），看不出两次保存的是不是同一张卡
    again = vault.encrypt(payload, purpose="secrets")
    assert again["ct"] != blob["ct"] and again["nonce"] != blob["nonce"]


def test_key_is_256_bit_and_scrypt_is_expensive(tmp_path):
    vault = _vault(tmp_path)
    salt = vault._salt()
    assert len(vault._master(salt)) == 32
    assert SCRYPT_N >= 2 ** 16


def test_tampering_is_detected(tmp_path):
    vault = _vault(tmp_path)
    blob = vault.encrypt({"card_number": "4242424242424242"}, purpose="secrets")
    cipher = bytearray(base64.b64decode(blob["ct"]))
    cipher[3] ^= 0x01
    tampered = dict(blob, ct=base64.b64encode(bytes(cipher)).decode())
    with pytest.raises(VaultError):
        vault.decrypt(tampered, purpose="secrets")
    data = vault.encrypt_bytes(b"secret screenshot", purpose="run")
    flipped = data[:-1] + bytes([data[-1] ^ 0x80])
    with pytest.raises(VaultError):
        vault.decrypt_bytes(flipped, purpose="run")


def test_ciphertext_bound_to_purpose_and_plugin(tmp_path):
    vault = _vault(tmp_path)
    blob = vault.encrypt({"x": 1}, purpose="secrets")
    with pytest.raises(VaultError):
        vault.decrypt(blob, purpose="codes")
    other_plugin = Vault(tmp_path, aad="OtherPlugin", env={ENV_NAME: TEST_SECRET})
    with pytest.raises(VaultError):
        other_plugin.decrypt(blob, purpose="secrets")


def test_no_secret_or_weak_secret_refuses(tmp_path):
    for secret, status in ((None, "missing"), ("", "missing"), ("short-123", "weak")):
        vault = _vault(tmp_path, secret)
        assert vault.status == status and not vault.ready
        with pytest.raises(VaultError):
            vault.encrypt({"card_number": "4242424242424242"})


def test_wrong_secret_and_common_password_guessing(tmp_path):
    blob = _vault(tmp_path).encrypt({"card_number": "4242424242424242"}, purpose="secrets")
    guesses = ["123456", "password", "12345678", "qwerty123", "oyunfor", "moviepilot", "admin123", "letmein",
               "OYUNFOR_SECRET", TEST_SECRET[:-1], TEST_SECRET + "x", TEST_SECRET.upper(), "unit-test-secret"]
    guesses = [g.ljust(16, "0") for g in guesses]  # 太短的口令本来就会被拒绝，补齐后再猜
    started = time.perf_counter()
    for guess in guesses:
        with pytest.raises(VaultError):
            _vault(tmp_path, guess).decrypt(blob, purpose="secrets")
    per_guess = (time.perf_counter() - started) / len(guesses)
    print(f"\n每猜一次口令耗时 {per_guess * 1000:.0f} ms（scrypt N=2^16, 64 MB 内存）")
    # 每次猜测都要重新跑一遍 scrypt：暴力猜测的成本被放大到几十毫秒一次
    assert per_guess > 0.02


def test_fingerprint_is_keyed_and_not_reversible(tmp_path):
    import hashlib

    vault = _vault(tmp_path)
    fp = vault.fingerprint(CODE)
    assert fp == vault.fingerprint(CODE.lower())
    assert fp != hashlib.sha256(CODE.encode()).hexdigest()
    assert CODE not in fp
    assert _vault(tmp_path, "another-secret-0123456789").fingerprint(CODE) != fp


def test_whole_config_dir_without_secret_cannot_decrypt(oyun, site):
    """攻击者拿到整个配置目录（数据库 + 插件数据目录，含盐和所有密文），但没有环境变量里的口令"""
    set_secrets(oyun, **SECRETS)
    blob = oyun.get_data("vault")
    assert blob and "4242" not in json.dumps(blob)
    stolen = Path(tempfile.mkdtemp(prefix="stolen-"))
    shutil.copytree(oyun.get_data_path(), stolen / "data")
    for secret in (None, "", "wrong-secret-0123456789", TEST_SECRET[::-1]):
        with pytest.raises(VaultError):
            _vault(stolen / "data", secret).decrypt(blob, purpose="secrets")
    # 有正确口令才能解开
    assert _vault(stolen / "data").decrypt(blob, purpose="secrets")["card_number"] == "4242424242424242"


# ---------------------------------------------------------------- 落盘扫描
def _scan(root: Path, needles):
    hits = []
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
            continue
        data = path.read_bytes()
        for needle in needles:
            if needle in data:
                hits.append((str(path.relative_to(root)), needle))
    return hits


def _needles():
    card = SECRETS["card_number"]
    spaced = " ".join(card[i:i + 4] for i in range(0, 16, 4))
    values = [card, spaced, base64.b64encode(card.encode()).decode(), "LEAKCHECK-COOKIE-7f3a9c",
              "Pw-Unique-9f3K!xLeak", "leakcheck.user@example.com", "LEAKCHECK HOLDER", CODE,
              '"card_cvv": "123"', '"card_cvv":"123"', "cvv=123", '"cvv": "123"', TEST_SECRET]
    out = []
    for value in values:
        out.append(value.encode("utf-8"))
        out.append(value.encode("utf-16-le"))
    return out


def test_no_plaintext_anywhere_after_full_card_purchase(oyun, site):
    """真实流程跑一遍（填卡 → 3DS 验证码 → 取码），然后逐字节扫描整个配置目录"""
    oyun.init_plugin(make_config(pay_mode="card"))
    set_secrets(oyun, **SECRETS)
    ok, message = oyun.start_purchase()
    assert ok, message
    assert wait_message(oyun, "付款验证")
    oyun.submit_code("123456")
    assert wait_idle(oyun, 120)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert codes_of(oyun, record) == [CODE]
    # 设置（配置表）里没有任何敏感字段
    config = oyun.get_config() or {}
    for key in ("cookie", "username", "password", "card_number", "card_cvv", "card_expiry", "card_holder"):
        assert key not in config or not config[key], key
    hits = _scan(CONFIG_DIR, _needles())
    assert not hits, hits


def test_changed_secret_resets_saved_hints(oyun, site, monkeypatch):
    """换了口令：旧数据解不开，重新录入后只保留新内容，旧的「已保存」提示不能残留"""
    set_secrets(oyun, card_number="4242424242424242", password="Pw-Unique-9f3K!xLeak")
    monkeypatch.setenv(ENV_NAME, "a-completely-different-secret-123")
    oyun.init_plugin(make_config())
    with pytest.raises(VaultError):
        oyun.read_secrets()
    oyun.store_secrets({"cookie": "oyf_session=VALID"})
    assert set(oyun.get_data("vault_meta")) == {"cookie"}
    assert oyun.read_secrets() == {"cookie": "oyf_session=VALID"}
