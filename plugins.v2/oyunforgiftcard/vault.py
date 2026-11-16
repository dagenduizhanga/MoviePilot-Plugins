"""
敏感信息保险箱。

加密范围：账号、密码、Cookie、卡号 / 有效期 / CVV / 持卡人、浏览器登录状态、兑换码、每次运行的截图与网页。

- 算法：AES-256-GCM（认证加密：密钥不对或密文被改动一位都会解密失败），每次加密用新的 96 位随机 nonce。
- 密钥：只来自环境变量 OYUNFOR_SECRET（至少 16 个字符），经 scrypt（N=2^16, r=8, p=1，约 64 MB 内存）
  派生出 256 位主密钥；不同用途再用 HKDF 派生子密钥（加密 / 兑换码指纹互不相同）。
  口令只存在于容器环境变量里，不进 MoviePilot 数据库、不进 /config 目录，
  所以数据库、配置目录或备份被整个拿走也解不开。没有设置口令时拒绝保存任何敏感信息。
- 兑换码查重只保存 HMAC-SHA256 指纹，不可逆。
- 明文只在用到的那一刻存在于内存里，不写日志、不回显到设置页。

上限：插件要无人值守地自动登录、付款，就必须能解密；能拿到运行中的容器（含环境变量和内存）的人
理论上也能拿到明文，这是所有自动化方案的共同上限。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ENV_NAME = "OYUNFOR_SECRET"
MIN_SECRET_LENGTH = 16
SALT_FILE = ".vault.salt"
SCRYPT_N = 2 ** 16
SCRYPT_R = 8
SCRYPT_P = 1
VERSION = 2
FILE_MAGIC = b"OYV2"

_KEY_CACHE: Dict[Tuple[str, bytes], bytes] = {}
_KEY_LOCK = threading.Lock()


class VaultError(Exception):
    """没有密钥、密钥不对、数据被改动或格式错误"""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def generate_secret() -> str:
    """给用户建议的随机口令（43 个字符，256 位熵）"""
    return secrets.token_urlsafe(32)


class Vault:
    """
    :param directory: 插件数据目录（只放随机盐和加密文件，盐不是秘密）
    :param aad: 绑定到每段密文的附加数据（插件 ID），防止把别处的密文挪过来用
    :param env: 读取口令的环境（测试时可替换）
    """

    def __init__(self, directory: Path, aad: str = "OyunforGiftCard", env: Optional[Dict[str, str]] = None):
        self.directory = Path(directory)
        self.aad = aad.encode("utf-8")
        self.env = os.environ if env is None else env

    # ------------------------------------------------------------ 状态
    def _passphrase(self) -> str:
        return (self.env.get(ENV_NAME) or "").strip()

    @property
    def status(self) -> str:
        """ready：可用；missing：没设置口令；weak：口令太短"""
        secret = self._passphrase()
        if not secret:
            return "missing"
        if len(secret) < MIN_SECRET_LENGTH:
            return "weak"
        return "ready"

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def status_text(self) -> str:
        return {
            "ready": "已启用加密（AES-256-GCM，密钥来自环境变量 OYUNFOR_SECRET）",
            "missing": f"没有设置环境变量 {ENV_NAME}：为了安全，插件不会保存任何账号、密码、Cookie 或卡信息",
            "weak": f"环境变量 {ENV_NAME} 太短（至少 {MIN_SECRET_LENGTH} 个字符）：插件拒绝使用",
        }[self.status]

    # ------------------------------------------------------------ 密钥
    def _salt(self) -> bytes:
        path = self.directory / SALT_FILE
        if path.exists():
            data = path.read_bytes()
            if len(data) == 16:
                return data
        self.directory.mkdir(parents=True, exist_ok=True)
        salt = secrets.token_bytes(16)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(salt)
        return salt

    def _master(self, salt: bytes) -> bytes:
        if not self.ready:
            raise VaultError(self.status_text())
        secret = self._passphrase()
        cache_key = (hashlib.sha256(secret.encode("utf-8")).hexdigest(), salt)
        with _KEY_LOCK:
            key = _KEY_CACHE.get(cache_key)
            if key is None:
                key = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(secret.encode("utf-8"))
                _KEY_CACHE[cache_key] = key
        return key

    def _subkey(self, salt: bytes, purpose: str) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                    info=f"oyunfor/{purpose}".encode("utf-8")).derive(self._master(salt))

    # ------------------------------------------------------------ 加解密（JSON）
    def encrypt(self, payload: Any, purpose: str = "data") -> Dict[str, str]:
        salt = self._salt()
        nonce = secrets.token_bytes(12)
        plain = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        cipher = AESGCM(self._subkey(salt, "enc")).encrypt(nonce, plain, self.aad + b"|" + purpose.encode())
        return {"v": VERSION, "alg": "AES-256-GCM", "kdf": "scrypt", "salt": _b64(salt), "nonce": _b64(nonce),
                "ct": _b64(cipher)}

    def decrypt(self, blob: Optional[Dict[str, Any]], purpose: str = "data") -> Any:
        if not blob:
            return None
        if not isinstance(blob, dict) or blob.get("v") != VERSION:
            raise VaultError("加密数据格式不对")
        try:
            salt = _unb64(blob["salt"])
            plain = AESGCM(self._subkey(salt, "enc")).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]),
                                                               self.aad + b"|" + purpose.encode())
        except InvalidTag:
            raise VaultError(f"解密失败：{ENV_NAME} 和加密时不一样，或数据被改动。请重新录入")
        except (KeyError, ValueError) as err:
            raise VaultError(f"加密数据损坏：{err}")
        return json.loads(plain.decode("utf-8"))

    # ------------------------------------------------------------ 加解密（二进制文件）
    def encrypt_bytes(self, data: bytes, purpose: str = "file") -> bytes:
        salt = self._salt()
        nonce = secrets.token_bytes(12)
        cipher = AESGCM(self._subkey(salt, "enc")).encrypt(nonce, data, self.aad + b"|" + purpose.encode())
        return FILE_MAGIC + salt + nonce + cipher

    def decrypt_bytes(self, blob: bytes, purpose: str = "file") -> bytes:
        if not blob.startswith(FILE_MAGIC) or len(blob) < len(FILE_MAGIC) + 28 + 16:
            raise VaultError("加密文件格式不对")
        body = blob[len(FILE_MAGIC):]
        salt, nonce, cipher = body[:16], body[16:28], body[28:]
        try:
            return AESGCM(self._subkey(salt, "enc")).decrypt(nonce, cipher, self.aad + b"|" + purpose.encode())
        except InvalidTag:
            raise VaultError(f"解密失败：{ENV_NAME} 和加密时不一样，或文件被改动")

    def write_file(self, path: Path, data: bytes, purpose: str = "file") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(self.encrypt_bytes(data, purpose))
        os.replace(tmp, path)
        return path

    def read_file(self, path: Path, purpose: str = "file") -> bytes:
        return self.decrypt_bytes(Path(path).read_bytes(), purpose)

    # ------------------------------------------------------------ 指纹
    def fingerprint(self, text: str) -> str:
        """不可逆指纹（HMAC-SHA256），用于兑换码查重"""
        return hmac.new(self._subkey(self._salt(), "fingerprint"), text.strip().upper().encode("utf-8"),
                        hashlib.sha256).hexdigest()
