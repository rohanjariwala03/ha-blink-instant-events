"""Pushes whose crypto-key or salt come without base64 padding (or with more parameters) still decrypt."""

from __future__ import annotations

from base64 import urlsafe_b64encode
import binascii
import logging
import os
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from firebase_messaging import FcmPushClient
import http_ece
import pytest

from custom_components.blink_instant_events.listener import _FcmPushClient


def b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).decode().rstrip("=")


def push(body: bytes) -> tuple[dict, str, str, bytes]:
    """Encrypt `body` the way Google does for this client: its credentials, crypto-key, salt and the raw data."""
    receiver, sender = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    secret, salt = os.urandom(16), os.urandom(16)
    raw = http_ece.encrypt(
        body,
        salt=salt,
        private_key=sender,
        dh=receiver.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint),
        auth_secret=secret,
        version="aesgcm",
    )
    credentials = {
        "keys": {
            "private": b64(receiver.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())),
            "secret": b64(secret),
        }
    }
    crypto_key = b64(sender.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))
    return credentials, crypto_key, b64(salt), raw


def test_unpadded_keys_decrypt() -> None:
    """A 65-byte key and 16-byte salt without their "=" padding: the library alone fails, ours reads them."""
    credentials, key, salt, raw = push(b'{"type": "motion"}')
    with pytest.raises(binascii.Error):
        FcmPushClient._decrypt_raw_data(credentials, key, salt, raw)
    assert _FcmPushClient._decrypt_raw_data(credentials, key, salt, raw) == b'{"type": "motion"}'
    # More parameters after the key, and already-padded values, work too.
    assert (
        _FcmPushClient._decrypt_raw_data(credentials, f"{key}=;p256ecdsa=abc", f"{salt}==;rs=4096", raw)
        == b'{"type": "motion"}'
    )


def test_unreadable_push_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """A push that still can't be read is logged and skipped instead of shutting the client down."""
    client = _FcmPushClient.__new__(_FcmPushClient)
    with (
        patch.object(FcmPushClient, "_handle_data_message", side_effect=binascii.Error("Incorrect padding")),
        caplog.at_level(logging.WARNING),
    ):
        client._handle_data_message(object())
    assert "Skipped a Blink push that couldn't be read: Incorrect padding" in caplog.text
