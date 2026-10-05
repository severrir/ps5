"""Package serving: Range parsing, the token bucket, and byte accounting."""

from __future__ import annotations

import time

import pytest
from flask import Flask

from server import config as config_module
from server.fileserve import TokenBucket, apply_configured_throttle, bucket, parse_range, send_package_file


# ---------------------------------------------------------------- Range ----- #


def test_parse_range_full_and_open_ended():
    assert parse_range("bytes=0-99", 1000) == (0, 99)
    assert parse_range("bytes=500-", 1000) == (500, 999)


def test_parse_range_suffix_form():
    assert parse_range("bytes=-200", 1000) == (800, 999)


def test_parse_range_clamps_to_size():
    assert parse_range("bytes=900-5000", 1000) == (900, 999)


def test_parse_range_rejects_unsatisfiable():
    assert parse_range("bytes=1000-", 1000) is None
    assert parse_range("bytes=600-500", 1000) is None
    assert parse_range("bytes=-0", 1000) is None


def test_parse_range_ignores_junk_and_multirange():
    assert parse_range(None, 1000) is None
    assert parse_range("", 1000) is None
    assert parse_range("items=0-5", 1000) is None
    # Multi-range is intentionally unsupported; it must fall back, not misparse.
    assert parse_range("bytes=0-10,20-30", 1000) is None


def test_parse_range_is_case_insensitive():
    assert parse_range("BYTES=0-9", 100) == (0, 9)


# ----------------------------------------------------------- TokenBucket ---- #


def test_unlimited_bucket_never_waits():
    unlimited = TokenBucket(0)
    started = time.monotonic()
    unlimited.consume(50 * 1024 * 1024)
    assert time.monotonic() - started < 0.05


def test_bucket_limits_throughput():
    """Draining 4x the per-second rate must take roughly the expected time."""
    rate = 200_000.0  # bytes/sec
    limited = TokenBucket(rate, burst_seconds=0.1)

    started = time.monotonic()
    for _ in range(4):
        limited.consume(int(rate // 2))  # 2x rate in total
    elapsed = time.monotonic() - started

    # Expect ~2s of work minus the small initial burst allowance. Bounds are loose
    # because this is wall-clock on a shared machine, but tight enough to catch a
    # bucket that does not throttle at all.
    assert 1.2 < elapsed < 3.5, f"unexpected elapsed time {elapsed:.2f}s"


def test_bucket_is_accurate_when_a_chunk_exceeds_the_burst_window():
    """Regression: a chunk larger than the burst allowance must not over-throttle.

    A token counter clamped to burst capacity throws away earned time whenever a
    single request is bigger than the capacity, which silently halves the effective
    rate at low limits with a large chunk size.
    """
    rate = 100_000.0
    chunk = 20_000  # 4x the 5,000-byte burst allowance below
    limited = TokenBucket(rate, burst_seconds=0.05)

    started = time.monotonic()
    for _ in range(5):
        limited.consume(chunk)
    elapsed = time.monotonic() - started

    # The shaper grants the first chunk immediately and charges it forward, so five
    # chunks cost four intervals: 4 * 20000/100000 = 0.8s. The old token-counter
    # implementation discarded earned time on every chunk larger than the burst
    # capacity and took substantially longer than this.
    assert 0.6 < elapsed < 1.15, f"expected ~0.8s of shaping, got {elapsed:.2f}s"


def test_bucket_rate_can_change_live():
    adjustable = TokenBucket(1000)
    assert adjustable.rate == 1000
    adjustable.set_rate(0)
    assert adjustable.rate == 0
    started = time.monotonic()
    adjustable.consume(10_000_000)
    assert time.monotonic() - started < 0.05


def test_apply_configured_throttle_reads_config(sandbox):
    config_module.save({"transfers": {"throttle_mbps": 7}})
    rate = apply_configured_throttle()
    assert rate == pytest.approx(7 * 1024 * 1024)
    assert bucket().rate == pytest.approx(7 * 1024 * 1024)

    config_module.save({"transfers": {"throttle_mbps": 0}})
    assert apply_configured_throttle() == 0


# ------------------------------------------------------------- streaming ---- #


@pytest.fixture
def serving_app(tmp_path):
    app = Flask(__name__)
    payload = bytes(range(256)) * 40  # 10,240 bytes of known content
    target = tmp_path / "sample.pkg"
    target.write_bytes(payload)

    served = {"bytes": 0}

    @app.get("/pkg")
    def route():
        return send_package_file(target, "sample.pkg", lambda count: served.update(bytes=served["bytes"] + count))

    return app, payload, served


def test_full_body_is_served_with_accept_ranges(serving_app):
    app, payload, served = serving_app
    client = app.test_client()

    response = client.get("/pkg")

    assert response.status_code == 200
    assert response.headers["Accept-Ranges"] == "bytes"
    assert int(response.headers["Content-Length"]) == len(payload)
    assert response.data == payload
    assert served["bytes"] == len(payload), "every served byte must be reported"


def test_range_request_returns_206_and_the_right_slice(serving_app):
    app, payload, _served = serving_app
    client = app.test_client()

    response = client.get("/pkg", headers={"Range": "bytes=1000-1999"})

    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 1000-1999/%d" % len(payload)
    assert int(response.headers["Content-Length"]) == 1000
    assert response.data == payload[1000:2000]


def test_resume_from_offset_serves_the_tail(serving_app):
    app, payload, _served = serving_app
    client = app.test_client()

    response = client.get("/pkg", headers={"Range": "bytes=10000-"})

    assert response.status_code == 206
    assert response.data == payload[10000:]


def test_unsatisfiable_range_answers_416_not_the_whole_file(serving_app):
    app, payload, _served = serving_app
    client = app.test_client()

    response = client.get("/pkg", headers={"Range": "bytes=999999-"})

    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */%d" % len(payload)
    assert response.data != payload


def test_missing_file_returns_404(tmp_path):
    app = Flask(__name__)

    @app.get("/gone")
    def route():
        return send_package_file(tmp_path / "nope.pkg")

    response = app.test_client().get("/gone")
    assert response.status_code == 404


def test_content_disposition_uses_the_basename(serving_app):
    app, _payload, _served = serving_app
    response = app.test_client().get("/pkg")
    assert 'filename="sample.pkg"' in response.headers["Content-Disposition"]
