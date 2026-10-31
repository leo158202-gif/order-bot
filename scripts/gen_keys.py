"""生成 RSA2048 密钥对到 data/keys/，打印公钥（用于填入企业微信后台）。

用法：.venv\\Scripts\\python.exe scripts\\gen_keys.py
密钥开发和正式共用，只生成一次；已存在时会提示，不覆盖。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEYS_DIR = Path("data/keys")
PRIVATE_PATH = KEYS_DIR / "private.pem"
PUBLIC_PATH = KEYS_DIR / "public.pem"


def main() -> None:
    if PRIVATE_PATH.exists():
        print(f"私钥已存在：{PRIVATE_PATH}（密钥只生成一次，不覆盖）")
        print(f"公钥在：{PUBLIC_PATH}")
    else:
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        PRIVATE_PATH.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        PUBLIC_PATH.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        print(f"已生成密钥对：{PRIVATE_PATH} / {PUBLIC_PATH}")

    pub_lines = PUBLIC_PATH.read_text().strip().splitlines()
    # 企业微信后台要的是去掉头尾和换行的 base64 主体
    body = "".join(pub_lines[1:-1])
    print()
    print("把下面这段粘贴到企业微信管理后台 → 会话内容存档 → 消息加密公钥：")
    print("-" * 60)
    print(body)
    print("-" * 60)
    print("粘贴保存后，后台会显示公钥版本号（一般是 1），请记下来。")


if __name__ == "__main__":
    main()
