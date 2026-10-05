"""Queue engine: scheduling, retries, pause semantics, progress provenance.

The etaHEN client is replaced with a scripted fake rather than a mock of the queue's
own internals, so these tests exercise the real state machine.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from server import config as config_module
from server.etahen import EtaHenError
from server.library import Library
from server.transfers import TransferQueue
from tests import synthetic_pkg


class FakeClient:
    """Stands in for EtaHenClient with controllable behaviour."""

    def __init__(self):
        self.installs: List[str] = []
        self.fail_installs = 0
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.progress_raises = False
        self.counter = 0
        self.control_calls: List[Dict[str, Any]] = []

    def install(self, url, title_id=None):
        if self.fail_installs > 0:
            self.fail_installs -= 1
            raise EtaHenError("installer refused (scripted failure)")
        self.counter += 1
        task_id = "task-%d" % self.counter
        self.installs.append(url)
        self.tasks[task_id] = {
            "id": task_id,
            "url": url,
            "state": "installing",
            "installed_bytes": 0,
            "total_bytes": 1000,
            "progress": 0.0,
        }
        return {"ok": True, "task_id": task_id}

    def progress(self):
        if self.progress_raises:
            raise EtaHenError("no progress endpoint")
        return {"ok": True, "tasks": list(self.tasks.values())}

    def advance(self, task_id, percent, state="installing"):
        task = self.tasks[task_id]
        task["progress"] = percent
        task["installed_bytes"] = int(task["total_bytes"] * percent / 100)
        task["state"] = state

    def call(self, name, tokens=None, payload_override=None):
        self.control_calls.append({"name": name, "payload": payload_override})
        return {"ok": True}

    def state(self):
        return {"connected": True}


@pytest.fixture
def queue_env(packages_dir):
    synthetic_pkg.build_ps4_pkg(packages_dir / "one.pkg", title="Game One", content_id="UP0001-CUSA00011_00-000000000000001A")
    synthetic_pkg.build_ps4_pkg(packages_dir / "two.pkg", title="Game Two", content_id="UP0001-CUSA00012_00-000000000000001B")
    library = Library()
    library.scan()
    client = FakeClient()
    queue = TransferQueue(library, client)
    return library, client, queue


def ids(library):
    return [item["id"] for item in sorted(library.all_items(), key=lambda i: i["title"])]


def test_enqueue_adds_one_entry(queue_env):
    library, _client, queue = queue_env
    entry = queue.enqueue(ids(library)[0])
    assert entry["state"] == "queued"
    assert len(queue.snapshot()["queue"]) == 1


def test_enqueue_is_idempotent_while_pending(queue_env):
    library, _client, queue = queue_env
    item_id = ids(library)[0]
    first = queue.enqueue(item_id)
    second = queue.enqueue(item_id)
    assert first["id"] == second["id"]
    assert len(queue.snapshot()["queue"]) == 1


def test_enqueue_unknown_item_raises(queue_env):
    _library, _client, queue = queue_env
    with pytest.raises(KeyError):
        queue.enqueue("nonexistent")


def test_batch_enqueue_reports_missing(queue_env):
    library, _client, queue = queue_env
    result = queue.enqueue_many(ids(library) + ["bogus"])
    assert len(result["added"]) == 2
    assert result["missing"] == ["bogus"]


def test_tick_starts_one_install_and_sends_a_hub_url(queue_env):
    library, client, queue = queue_env
    queue.enqueue_many(ids(library))

    queue.tick()

    snapshot = queue.snapshot()
    assert snapshot["active"] == 1, "max_concurrent defaults to 1"
    assert len(client.installs) == 1
    assert client.installs[0].startswith(config_module.base_url() + "/pkg/")


def test_concurrency_setting_is_honoured(queue_env):
    library, client, queue = queue_env
    config_module.save({"transfers": {"max_concurrent": 2}})
    queue.enqueue_many(ids(library))

    queue.tick()
    queue.tick()

    assert len(client.installs) == 2


def test_daemon_progress_is_labelled_as_such(queue_env):
    library, client, queue = queue_env
    queue.enqueue(ids(library)[0])
    queue.tick()

    task_id = list(client.tasks.keys())[0]
    client.advance(task_id, 45.0)
    queue._last_progress_poll = 0  # force a poll this tick
    queue.tick()

    entry = queue.snapshot()["queue"][0]
    assert entry["progress"] == pytest.approx(45.0)
    assert entry["progress_source"] == "daemon"


def test_daemon_completion_marks_done_and_archives(queue_env):
    library, client, queue = queue_env
    queue.enqueue(ids(library)[0])
    queue.tick()

    task_id = list(client.tasks.keys())[0]
    client.advance(task_id, 100.0, state="done")
    queue._last_progress_poll = 0
    queue.tick()

    snapshot = queue.snapshot()
    assert snapshot["queue"][0]["state"] == "done"
    assert snapshot["queue"][0]["progress"] == 100.0
    assert len(snapshot["history"]) == 1


def test_inferred_progress_from_served_bytes(queue_env):
    library, client, queue = queue_env
    client.progress_raises = True
    item_id = ids(library)[0]
    queue.enqueue(item_id)
    queue.tick()

    size = library.item(item_id)["size_bytes"]
    queue.note_bytes(item_id, size // 2)
    queue._last_progress_poll = 0
    queue.tick()

    entry = queue.snapshot()["queue"][0]
    assert entry["progress_source"] == "inferred"
    assert 40 < entry["progress"] < 60
    assert queue.snapshot()["daemon_progress"] is False


def test_serving_every_byte_completes_an_inferred_transfer(queue_env):
    library, client, queue = queue_env
    client.progress_raises = True
    item_id = ids(library)[0]
    queue.enqueue(item_id)
    queue.tick()

    queue.note_bytes(item_id, library.item(item_id)["size_bytes"])
    queue._last_progress_poll = 0
    queue.tick()

    entry = queue.snapshot()["queue"][0]
    assert entry["state"] == "done"
    assert "console-side install may still be finalising" in (entry["note"] or "")


def test_install_failure_schedules_a_retry_then_gives_up(queue_env):
    library, client, queue = queue_env
    config_module.save({"transfers": {"max_retries": 1, "retry_backoff_seconds": 1}})
    client.fail_installs = 5

    queue.enqueue(ids(library)[0])
    queue.tick()
    assert queue.snapshot()["queue"][0]["state"] == "retrying"

    # Make the retry due, then let it fail again past the limit.
    queue._queue[0]["retry_at"] = 0
    queue.tick()

    entry = queue.snapshot()["queue"][0]
    assert entry["state"] == "failed"
    assert entry["error"]
    assert "Gave up" in entry["note"]


def test_manual_retry_resets_the_entry(queue_env):
    library, client, queue = queue_env
    config_module.save({"transfers": {"max_retries": 0}})
    client.fail_installs = 1
    queue.enqueue(ids(library)[0])
    queue.tick()
    assert queue.snapshot()["queue"][0]["state"] == "failed"

    queue.retry(queue.snapshot()["queue"][0]["id"])

    entry = queue.snapshot()["queue"][0]
    assert entry["state"] == "queued"
    assert entry["attempts"] == 0
    assert entry["error"] is None


def test_pause_blocks_byte_serving_and_resume_requeues(queue_env):
    library, _client, queue = queue_env
    item_id = ids(library)[0]
    queue.enqueue(item_id)
    queue.tick()
    transfer_id = queue.snapshot()["queue"][0]["id"]

    queue.pause(transfer_id)
    assert queue.is_paused_item(item_id) is True, "the package route must refuse while paused"
    assert queue.snapshot()["queue"][0]["state"] == "paused"

    queue.resume(transfer_id)
    assert queue.is_paused_item(item_id) is False
    assert queue.snapshot()["queue"][0]["state"] == "queued"


def test_pause_calls_daemon_task_control_when_configured(queue_env):
    library, client, queue = queue_env
    queue.enqueue(ids(library)[0])
    queue.tick()
    queue.pause(queue.snapshot()["queue"][0]["id"])

    assert any(call["name"] == "task_control" for call in client.control_calls)


def test_cancel_moves_to_history(queue_env):
    library, _client, queue = queue_env
    queue.enqueue(ids(library)[0])
    transfer_id = queue.snapshot()["queue"][0]["id"]

    queue.cancel(transfer_id)

    assert queue.snapshot()["queue"][0]["state"] == "cancelled"
    assert queue.history()[0]["state"] == "cancelled"


def test_move_reorders(queue_env):
    library, _client, queue = queue_env
    item_ids = ids(library)
    queue.enqueue_many(item_ids)
    second = queue.snapshot()["queue"][1]["id"]

    assert queue.move(second, -1) is True
    assert queue.snapshot()["queue"][0]["id"] == second
    assert queue.move(second, -1) is False, "already first"


def test_clear_finished_removes_only_terminal_entries(queue_env):
    library, _client, queue = queue_env
    item_ids = ids(library)
    queue.enqueue_many(item_ids)
    queue.cancel(queue.snapshot()["queue"][0]["id"])

    removed = queue.clear_finished()

    assert removed == 1
    assert len(queue.snapshot()["queue"]) == 1


def test_queue_survives_a_restart_and_requeues_in_flight_work(queue_env):
    library, client, queue = queue_env
    queue.enqueue_many(ids(library))
    queue.tick()
    assert queue.snapshot()["active"] == 1

    # A fresh instance reads the same transfers.json.
    revived = TransferQueue(library, client)
    snapshot = revived.snapshot()

    assert len(snapshot["queue"]) == 2
    assert snapshot["active"] == 0, "an in-flight transfer cannot survive the process"
    assert snapshot["queue"][0]["state"] == "queued"
    assert snapshot["queue"][0]["note"] == "Requeued after hub restart"


def test_url_items_are_sent_verbatim(queue_env):
    library, client, queue = queue_env
    entry = library.store.add_url("http://example.invalid/remote.pkg")
    library.scan()

    queue.enqueue(entry["id"])
    queue.tick()

    assert client.installs == ["http://example.invalid/remote.pkg"]


def test_clear_all_wipes_queue_and_history(queue_env):
    library, _client, queue = queue_env
    queue.enqueue(ids(library)[0])
    queue.clear_all()
    snapshot = queue.snapshot()
    assert snapshot["queue"] == []
    assert snapshot["history"] == []


def test_public_snapshot_hides_internal_fields(queue_env):
    library, _client, queue = queue_env
    queue.enqueue(ids(library)[0])
    entry = queue.snapshot()["queue"][0]
    assert not any(key.startswith("_") for key in entry)
    assert "served_label" in entry
