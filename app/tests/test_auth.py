from app.auth import hash_password, make_session_token, read_session_token, verify_password


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong password", h)


def test_hashes_are_salted_and_unique():
    a, b = hash_password("same-password"), hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a) and verify_password("same-password", b)


def test_verify_rejects_garbage_stored_value():
    assert not verify_password("x", "not-a-real-hash")
    assert not verify_password("x", "")


def test_session_token_roundtrip():
    token = make_session_token(user_id=42)
    assert read_session_token(token) == 42


def test_session_token_tampered_rejected():
    token = make_session_token(user_id=42)
    # Flip two adjacent characters in the middle of the token, not the very last character: base64's
    # final partial-byte group has spare (unused) bits, so tampering only the last character can
    # occasionally decode to the *same* bytes it started with, making that one tamper flaky.
    mid = len(token) // 2
    a = "a" if token[mid] != "a" else "b"
    b = "a" if token[mid + 1] != "a" else "b"
    tampered = token[:mid] + a + b + token[mid + 2 :]
    assert tampered != token
    assert read_session_token(tampered) is None


def test_session_token_garbage_rejected():
    assert read_session_token("not-a-token") is None
