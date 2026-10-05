"""The mock daemons and the etaHEN client, exercised against each other.

This is the integration test for the console boundary. Because the mock speaks real
HTTP on real sockets, the client's URL building, payload templating, JSON decoding
and error handling are all genuinely covered -- which is the point of having a mock
daemon rather than monkeypatching the client.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from server import config as config_module, mock_etahen
from server.etahen import EtaHenClient, EtaHenError, Watchdog


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def mock_pair(sandbox):
    installer_port = free_port()
    control_port = free_port()
    config_module.save(
        {
            "etahen": {
                "mode": "mock",
                "mock_bind_ip": "127.0.0.1",
                "installer_port": installer_port,
                "control_port": control_port,
                "timeout_seconds": 3,
            }
        }
    )
    mock_etahen.reset_state()
    servers = mock_etahen.start_in_process()
    assert servers, "the mock daemons should bind on free ports"
    yield EtaHenClient()
    for server in servers:
        server.shutdown()
        server.server_close()


def test_heartbeat_round_trip(mock_pair):
    client = mock_pair
    assert client.heartbeat() is True
    state = client.state()
    assert state["connected"] is True
    assert state["handshakes"] >= 1
    assert state["last_error"] is None


def test_install_creates_a_task_and_progress_advances(mock_pair):
    client = mock_pair
    response = client.install("http://127.0.0.1:1/pkg/abc")

    assert response["ok"] is True
    assert response["task_id"]

    tasks = client.progress()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["id"] == response["task_id"]
    assert tasks[0]["state"] == "installing"

    first = tasks[0]["installed_bytes"]
    time.sleep(0.15)
    second = client.progress()["tasks"][0]["installed_bytes"]
    assert second >= first, "a mock install should make progress over time"


def test_storage_endpoint_shape(mock_pair):
    storage = mock_pair.storage()
    assert storage["ok"] is True
    for key in ("internal", "external"):
        assert storage[key]["total_bytes"] > 0
        assert storage[key]["free_bytes"] >= 0
    assert storage.get("simulated") is None or storage.get("simulated") is True


def test_hardware_endpoint_is_labelled_simulated(mock_pair):
    hardware = mock_pair.hardware()
    assert hardware["simulated"] is True
    assert 0 < hardware["soc_temp_c"] < 120
    assert hardware["fan_rpm"] > 0


def test_klog_returns_lines(mock_pair):
    lines = mock_pair.klog()
    assert isinstance(lines, list)
    assert any("etaHEN" in line for line in lines)


def test_task_control_pauses_and_resumes(mock_pair):
    client = mock_pair
    task_id = client.install("http://127.0.0.1:1/pkg/abc")["task_id"]

    client.call("task_control", payload_override={"task_id": task_id, "state": "paused"})
    paused = [task for task in client.progress()["tasks"] if task["id"] == task_id][0]
    assert paused["state"] == "paused"
    assert paused["rate_bps"] == 0

    client.call("task_control", payload_override={"task_id": task_id, "state": "installing"})
    resumed = [task for task in client.progress()["tasks"] if task["id"] == task_id][0]
    assert resumed["state"] == "installing"


def test_unknown_endpoint_name_raises(mock_pair):
    with pytest.raises(EtaHenError):
        mock_pair.call("no_such_endpoint")


def test_unreachable_console_raises_and_is_recorded(sandbox):
    config_module.save(
        {"etahen": {"mode": "live", "console_ip": "127.0.0.1", "control_port": free_port(), "timeout_seconds": 1}}
    )
    client = EtaHenClient()

    assert client.heartbeat() is False
    with pytest.raises(EtaHenError):
        client.call("heartbeat")

    state = client.state()
    assert state["connected"] is False
    assert state["consecutive_failures"] >= 1
    assert state["last_error"]


def test_missing_console_ip_is_reported_clearly(sandbox):
    config_module.save({"etahen": {"mode": "live", "console_ip": ""}})
    client = EtaHenClient()
    with pytest.raises(EtaHenError) as excinfo:
        client.call("heartbeat")
    assert "console_ip" in str(excinfo.value)


def test_payload_template_substitution(mock_pair):
    """{url} in the configured template must reach the daemon as a real URL."""
    client = mock_pair
    url = "http://127.0.0.1:1/pkg/substituted"
    client.install(url)
    tasks = client.progress()["tasks"]
    assert [task["url"] for task in tasks] == [url]


def test_endpoint_config_changes_without_touching_code(mock_pair):
    """Repointing an endpoint path in config must change where the client calls."""
    endpoints = dict(config_module.get("etahen.endpoints"))
    endpoints["heartbeat"] = dict(endpoints["heartbeat"], path="/api/does-not-exist")
    config_module.save({"etahen": {"endpoints": endpoints}})

    client = EtaHenClient()
    assert client.heartbeat() is False, "the client should now be calling the wrong path"


def test_mode_switch_changes_the_target(sandbox):
    config_module.save({"etahen": {"mode": "live", "console_ip": "192.168.9.9"}})
    assert EtaHenClient.target_ip() == "192.168.9.9"

    config_module.save({"etahen": {"mode": "mock", "mock_bind_ip": "127.0.0.1"}})
    assert EtaHenClient.target_ip() == "127.0.0.1"


def test_watchdog_reconnects_quietly(mock_pair):
    client = mock_pair
    config_module.save({"etahen": {"watchdog_interval_seconds": 2}})
    watchdog = Watchdog(client)
    watchdog.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not client.state()["connected"]:
            time.sleep(0.05)
        assert client.state()["connected"] is True
    finally:
        watchdog.stop()

    # The watchdog thread must be a daemon so it never blocks shutdown.
    assert all(thread.daemon for thread in threading.enumerate() if thread.name == "etahen-watchdog")


def test_mock_actually_downloads_the_package(sandbox, tmp_path):
    """The mock pulls the URL for real, which is what exercises the hub's own file
    server, its Range support and its throttle without a console present."""
    import http.server
    import threading as _threading

    payload = b"\xa5" * 48_000
    served_dir = tmp_path / "served"
    served_dir.mkdir()
    (served_dir / "pkg.bin").write_bytes(payload)

    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(served_dir), **kw)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    _threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/pkg.bin" % httpd.server_address[1]

    try:
        mock_etahen.reset_state()
        task = mock_etahen.STATE.create_task(url)

        # Generous deadline: this is a real socket transfer on a machine that may be
        # running the rest of the suite at the same time.
        deadline = time.time() + 20
        snapshot = task
        while time.time() < deadline:
            snapshot = mock_etahen.STATE.snapshot_task(task["id"])
            if snapshot["state"] == "done":
                break
            time.sleep(0.05)

        assert snapshot["state"] == "done"
        assert snapshot["real_download"] is True
        assert snapshot["total_bytes"] == len(payload)
        assert snapshot["installed_bytes"] == len(payload)
        assert snapshot["progress"] == 100.0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mock_falls_back_to_simulation_for_an_unfetchable_url(sandbox):
    """A link the mock cannot reach falls back to the simulated clock.

    The fallback waits for the connect attempt to resolve rather than racing it: a
    refused connection to a dead local port takes about two seconds on Windows, and
    reporting invented progress before then would be exactly the behaviour the
    pending state exists to prevent.
    """
    mock_etahen.reset_state()
    task = mock_etahen.STATE.create_task("http://127.0.0.1:1/unreachable.pkg")

    # While the connect is outstanding, progress must stay at zero.
    early = mock_etahen.STATE.snapshot_task(task["id"])
    assert early["installed_bytes"] == 0
    assert early["rate_bps"] == 0

    deadline = time.time() + 15
    while time.time() < deadline:
        with mock_etahen.STATE._lock:
            resolved = mock_etahen.STATE._tasks[task["id"]].get("fetch") != "pending"
        if resolved:
            break
        time.sleep(0.1)
    assert resolved, "the fetch attempt should have resolved by now"

    time.sleep(0.2)
    snapshot = mock_etahen.STATE.snapshot_task(task["id"])

    assert snapshot["real_download"] is False
    assert snapshot["total_bytes"] > 0
    assert snapshot["installed_bytes"] > 0, "the simulated clock should be advancing"
