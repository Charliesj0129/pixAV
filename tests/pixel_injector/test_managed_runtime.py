"""The retained upload guest as a journalled, reconcilable fact."""

from __future__ import annotations

import json
import uuid

import pytest

from pixav.pixel_injector.canary import OWNER_LABEL, TOOLS_IMAGE, CanaryBlockedError
from pixav.pixel_injector.managed_runtime import GUEST_DATA_MOUNT, GUEST_STAGING_MOUNT, ManagedRuntime

OWNER = str(uuid.UUID("22222222-2222-4222-8222-222222222222"))
ACCOUNT = uuid.UUID("33333333-3333-4333-8333-333333333333")
OTHER_ACCOUNT = uuid.UUID("44444444-4444-4444-8444-444444444444")
GUEST_IMAGE = "pixav/redroid-pixel-xl:test"


class Container:
    def __init__(self, identifier, role, *, source, destination, writable, image, network=None):
        self.id = identifier
        self.labels = {OWNER_LABEL: OWNER, "pixav.photos_canary.role": role}
        self.status = "running"
        self.attrs = {
            "Config": {"Image": image},
            "Mounts": [{"Destination": destination, "Source": str(source), "RW": writable, "Type": "bind"}],
            "HostConfig": {"NetworkMode": network or "default"},
        }
        self.stopped = False
        self.removed = False

    def exec_run(self, command):
        return type("Result", (), {"exit_code": 0, "output": b"1"})()

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


class Containers:
    def __init__(self, client):
        self._client = client

    def list(self, all=False, filters=None):  # noqa: A002 - docker's own keyword
        return list(self._client.items.values())

    def get(self, identifier):
        return self._client.items[identifier]

    def run(self, image, **kwargs):
        self._client.created.append({"image": image, **kwargs})
        role = kwargs["labels"]["pixav.photos_canary.role"]
        destination = GUEST_DATA_MOUNT if role == "guest" else GUEST_STAGING_MOUNT
        source = next(iter(kwargs["volumes"]))
        item = Container(
            f"{role}-{len(self._client.items)}",
            role,
            source=source,
            destination=destination,
            writable=role == "guest",
            image=image,
            network=kwargs.get("network_mode"),
        )
        self._client.items[item.id] = item
        return item


class Client:
    def __init__(self):
        self.items: dict = {}
        self.created: list = []
        self.containers = Containers(self)


class Pool:
    """Just enough of the journal functions to exercise their contract."""

    def __init__(self):
        self.row: dict | None = None
        self.rejected = False

    async def fetchval(self, query, *args):
        if "journal_runtime" in query:
            if self.rejected:
                return False
            report = json.loads(args[0])
            if self.row and self.row["state"] == "REVIEW_REQUIRED" and report["state"] != "REVIEW_REQUIRED":
                return False
            base = dict(self.row or {})
            for key in ("account_id", "guest_id", "tools_id"):
                if report.get(key) is None:
                    report[key] = base.get(key)
            self.row = {**base, **report}
            return True
        if "forget_runtime_containers" in query:
            if not self.row or self.row["state"] != "RETIRING":
                return False
            self.row = {**self.row, "state": "RETIRED", "guest_id": None, "tools_id": None, "account_id": None}
            return True
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        return self.row


def runtime(pool, client, tmp_path, monkeypatch, **changes) -> ManagedRuntime:
    monkeypatch.setattr(
        "pixav.pixel_injector.managed_runtime.get_profile",
        lambda name, path=None: type("Profile", (), {"image": GUEST_IMAGE, "args": ()})(),
    )
    return ManagedRuntime(
        pool,
        client,
        owner=OWNER,
        staging_root=tmp_path / "staging",
        guest_data_root=tmp_path / "guest",
        profile_name="gphotos_pixel_xl_v1",
        **changes,
    )


async def test_creation_intent_is_journalled_before_any_container_bdd_003(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    states: list[str] = []
    original = pool.fetchval

    async def record(query, *args):
        if "journal_runtime" in query:
            states.append(json.loads(args[0])["state"])
            assert len(client.created) <= {"INTENT": 0, "GUEST_CREATED": 1, "READY": 2}[states[-1]]
        return await original(query, *args)

    pool.fetchval = record
    await runtime(pool, client, tmp_path, monkeypatch).acquire(ACCOUNT)

    assert states == ["INTENT", "GUEST_CREATED", "READY"]


async def test_provisioned_runtime_mounts_the_staging_root_read_only(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    guest, tools = await runtime(pool, client, tmp_path, monkeypatch).acquire(ACCOUNT)

    tools_spec = client.created[1]
    assert tools_spec["image"] == TOOLS_IMAGE
    assert tools_spec["network_mode"] == f"container:{guest.id}"
    assert tools_spec["volumes"][str(tmp_path / "staging")] == {"bind": GUEST_STAGING_MOUNT, "mode": "ro"}
    assert tools.labels["pixav.photos_canary.role"] == "tools"


async def test_a_retained_runtime_is_reused_rather_than_recreated_bdd_003(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    subject = runtime(pool, client, tmp_path, monkeypatch)
    first = await subject.acquire(ACCOUNT)

    second = await subject.acquire(ACCOUNT)

    assert [item.id for item in first] == [item.id for item in second]
    assert len(client.created) == 2, "no second guest was built for the same account"


async def test_an_interrupted_creation_is_handed_to_an_operator(tmp_path, monkeypatch):
    """A half-created guest may already hold credentials, so nothing replaces it."""
    pool, client = Pool(), Client()
    subject = runtime(pool, client, tmp_path, monkeypatch)
    pool.row = {"state": "INTENT", "guest_id": None, "tools_id": None, "account_id": None}
    client.items["guest-0"] = Container(
        "guest-0",
        "guest",
        source=tmp_path / "guest",
        destination=GUEST_DATA_MOUNT,
        writable=True,
        image=GUEST_IMAGE,
    )

    with pytest.raises(CanaryBlockedError):
        await subject.acquire(ACCOUNT)
    assert pool.row["state"] == "REVIEW_REQUIRED"
    assert client.items["guest-0"].removed is False


async def test_a_runtime_held_for_review_stays_held(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    pool.row = {"state": "REVIEW_REQUIRED", "review_reason": "operator", "guest_id": None, "tools_id": None}

    with pytest.raises(CanaryBlockedError):
        await runtime(pool, client, tmp_path, monkeypatch).acquire(ACCOUNT)
    assert not client.created


async def test_owned_containers_without_a_journal_stop_the_worker(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    client.items["stray"] = Container(
        "stray",
        "guest",
        source=tmp_path / "guest",
        destination=GUEST_DATA_MOUNT,
        writable=True,
        image=GUEST_IMAGE,
    )

    with pytest.raises(CanaryBlockedError):
        await runtime(pool, client, tmp_path, monkeypatch).acquire(ACCOUNT)
    assert pool.row["state"] == "REVIEW_REQUIRED"


async def test_a_different_account_retires_the_guest_instead_of_sharing_it_bdd_043(tmp_path, monkeypatch):
    """Two accounts on one device makes original-quality attribution unprovable."""
    pool, client = Pool(), Client()
    subject = runtime(pool, client, tmp_path, monkeypatch)
    first_guest, first_tools = await subject.acquire(ACCOUNT)

    second_guest, _ = await subject.acquire(OTHER_ACCOUNT)

    assert first_guest.removed and first_tools.removed
    assert second_guest.id != first_guest.id
    assert pool.row["account_id"] == str(OTHER_ACCOUNT)


async def test_a_changed_mount_refuses_the_retained_runtime_bdd_052(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    subject = runtime(pool, client, tmp_path, monkeypatch)
    _, tools = await subject.acquire(ACCOUNT)
    tools.attrs["Mounts"][0]["Source"] = str(tmp_path / "somewhere-else")

    with pytest.raises(CanaryBlockedError):
        await subject.acquire(ACCOUNT)


async def test_a_changed_network_namespace_refuses_the_retained_runtime(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    subject = runtime(pool, client, tmp_path, monkeypatch)
    _, tools = await subject.acquire(ACCOUNT)
    tools.attrs["HostConfig"]["NetworkMode"] = "bridge"

    with pytest.raises(CanaryBlockedError):
        await subject.acquire(ACCOUNT)


async def test_a_foreign_label_refuses_the_retained_runtime(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    subject = runtime(pool, client, tmp_path, monkeypatch)
    guest, _ = await subject.acquire(ACCOUNT)
    guest.labels["pixav.task_id"] = "legacy"

    with pytest.raises(CanaryBlockedError):
        await subject.acquire(ACCOUNT)


async def test_a_rejected_journal_stops_before_creating_anything(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    pool.rejected = True

    with pytest.raises(CanaryBlockedError):
        await runtime(pool, client, tmp_path, monkeypatch).acquire(ACCOUNT)
    assert not client.created


async def test_bind_sources_are_the_daemon_s_host_paths_bdd_003(tmp_path, monkeypatch):
    """A containerised worker's own path means something else on the host.

    Docker resolves a bind source on the daemon's filesystem and silently
    creates it when it is missing, so an untranslated path gives the guest an
    empty ``/parts`` instead of the staged artifact — with no error anywhere.
    """
    pool, client = Pool(), Client()
    subject = runtime(
        pool, client, tmp_path, monkeypatch, host_project_root="/home/operator/pixAV", project_root=tmp_path
    )

    await subject.acquire(ACCOUNT)

    assert client.created[0]["volumes"] == {"/home/operator/pixAV/guest": {"bind": GUEST_DATA_MOUNT, "mode": "rw"}}
    assert client.created[1]["volumes"] == {"/home/operator/pixAV/staging": {"bind": GUEST_STAGING_MOUNT, "mode": "ro"}}


async def test_a_retained_runtime_is_verified_against_the_same_host_paths(tmp_path, monkeypatch):
    pool, client = Pool(), Client()
    subject = runtime(
        pool, client, tmp_path, monkeypatch, host_project_root="/home/operator/pixAV", project_root=tmp_path
    )
    first = await subject.acquire(ACCOUNT)

    second = await subject.acquire(ACCOUNT)

    assert [item.id for item in first] == [item.id for item in second]
    assert len(client.created) == 2
