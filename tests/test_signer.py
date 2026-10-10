"""Signatures verify against the public key, for both key types Kalshi accepts.
Keys are generated in memory per test run — no key material is stored in the repo."""
import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshi.client import RequestSigner


def test_rsa_pss_signature_verifies():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s = RequestSigner("test-key-id", private_key=key)
    h = s.headers("GET", "/trade-api/v2/markets?limit=1", ts="1700000000000")
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            b"1700000000000GET/trade-api/v2/markets",
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())
    assert h["KALSHI-ACCESS-KEY"] == "test-key-id"


def test_ed25519_signature_verifies():
    key = ed25519.Ed25519PrivateKey.generate()
    h = RequestSigner("k", private_key=key).headers("get", "/trade-api/v2/portfolio/balance", ts="1")
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1GET/trade-api/v2/portfolio/balance")


def test_query_string_not_signed():
    s = RequestSigner("k", private_key=ed25519.Ed25519PrivateKey.generate())
    assert s.message("1", "GET", "/a?b=c") == s.message("1", "GET", "/a")
