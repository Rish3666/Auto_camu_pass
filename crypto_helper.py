"""Encryption utilities for safely storing multi-user Camu credentials."""

import argparse
import json
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


def generate_key() -> str:
    """Generate a new URL-safe base64-encoded Fernet key."""
    return Fernet.generate_key().decode("utf-8")


def encrypt_users(users_data: list[dict[str, Any]], key: str) -> bytes:
    """Encrypt a list of user dicts into Fernet ciphertext bytes."""
    fernet = Fernet(key.encode("utf-8"))
    payload = json.dumps(users_data, indent=2).encode("utf-8")
    return fernet.encrypt(payload)


def decrypt_users(encrypted_bytes: bytes, key: str) -> list[dict[str, Any]]:
    """Decrypt Fernet ciphertext bytes back into a list of user dicts."""
    fernet = Fernet(key.encode("utf-8"))
    decrypted = fernet.decrypt(encrypted_bytes)
    data = json.loads(decrypted.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("Decrypted users payload must be a JSON array.")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypt or decrypt Camu user roster.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("keygen", help="Generate a new CAMU_SECRET_KEY.")

    enc_parser = subparsers.add_parser("encrypt", help="Encrypt users.json to users.enc.")
    enc_parser.add_argument("--input", default="users.json", help="Input plaintext JSON file.")
    enc_parser.add_argument("--output", default="users.enc", help="Output encrypted file.")
    enc_parser.add_argument("--key", required=True, help="Fernet key (CAMU_SECRET_KEY).")

    dec_parser = subparsers.add_parser("decrypt", help="Decrypt users.enc to stdout.")
    dec_parser.add_argument("--input", default="users.enc", help="Input encrypted file.")
    dec_parser.add_argument("--key", required=True, help="Fernet key (CAMU_SECRET_KEY).")

    args = parser.parse_args()

    if args.command == "keygen":
        print(generate_key())
    elif args.command == "encrypt":
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        encrypted = encrypt_users(raw, args.key)
        Path(args.output).write_bytes(encrypted)
        print(f"Encrypted {len(raw)} user(s) -> {args.output}")
    elif args.command == "decrypt":
        encrypted = Path(args.input).read_bytes()
        users = decrypt_users(encrypted, args.key)
        print(json.dumps(users, indent=2))


if __name__ == "__main__":
    main()
