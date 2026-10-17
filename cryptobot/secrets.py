"""API keys live in the operating system keychain, never in files or logs."""
from __future__ import annotations

import keyring

SERVICE = "CryptoBot"


def _u(exchange: str, part: str) -> str:
    return f"{exchange}:{part}"


def save_api_key(exchange: str, key: str, secret: str, passphrase: str | None = None) -> None:
    keyring.set_password(SERVICE, _u(exchange, "key"), key)
    keyring.set_password(SERVICE, _u(exchange, "secret"), secret)
    if passphrase:
        keyring.set_password(SERVICE, _u(exchange, "passphrase"), passphrase)
    else:
        delete_entry(exchange, "passphrase")


def load_api_key(exchange: str) -> tuple[str, str, str | None] | None:
    key = keyring.get_password(SERVICE, _u(exchange, "key"))
    secret = keyring.get_password(SERVICE, _u(exchange, "secret"))
    if not key or not secret:
        return None
    return key, secret, keyring.get_password(SERVICE, _u(exchange, "passphrase"))


def has_api_key(exchange: str) -> bool:
    return load_api_key(exchange) is not None


def delete_entry(exchange: str, part: str) -> None:
    try:
        keyring.delete_password(SERVICE, _u(exchange, part))
    except keyring.errors.PasswordDeleteError:
        pass


def delete_api_key(exchange: str) -> None:
    for part in ("key", "secret", "passphrase"):
        delete_entry(exchange, part)


def save_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def load_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "•" * 8 + value[-2:] if len(value) > 8 else "•" * len(value)
