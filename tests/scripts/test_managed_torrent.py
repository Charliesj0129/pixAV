"""Private qBittorrent provisioning and managed torrent/media boundaries."""

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import manage_managed_deployment as deployment
from scripts import prepare_managed_qbittorrent as provision


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    data.mkdir(mode=0o700)
    secrets.mkdir(mode=0o700)
    monkeypatch.setattr(provision, "_require_stopped", lambda *_args: None)
    return data, secrets


def prepare(roots, *, apply=False):
    return provision.prepare(*roots, context="synthetic", daemon="synthetic-daemon", apply=apply)


def test_dry_run_creates_no_directory_config_or_credential(roots):
    assert prepare(roots) == {"result": "DRY_RUN", "planned_files": 2, "files_created": 0, "data_changed": False}
    assert all(not list(path.iterdir()) for path in roots)


def test_apply_is_private_from_creation_and_repeated_apply_changes_nothing(roots, monkeypatch, capsys):
    open_file = provision.os.open
    observed = []

    def observe_creation(path, flags, mode=0o777):
        descriptor = open_file(path, flags, mode)
        observed.append(os.fstat(descriptor).st_mode & 0o777)
        return descriptor

    monkeypatch.setattr(provision.os, "open", observe_creation)
    assert prepare(roots, apply=True)["files_created"] == 2
    assert observed == [0o600, 0o600]
    paths = [path for root in roots for path in root.rglob("*") if path.is_file()]
    before = [(path, path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    assert prepare(roots, apply=True)["files_created"] == 0
    assert before == [(path, path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "original,replacement",
    [
        ("Session\\Interface=wg0", "Session\\Interface=eth0"),
        ("WebUI\\CSRFProtection=true", "WebUI\\CSRFProtection=false"),
        ("WebUI\\HostHeaderValidation=true", "WebUI\\HostHeaderValidation=false"),
        ("WebUI\\AuthSubnetWhitelistEnabled=false", "WebUI\\AuthSubnetWhitelistEnabled=true"),
        ("WebUI\\LocalHostAuth=true", "WebUI\\LocalHostAuth=false"),
        ("Connection\\UPnP=false", "Connection\\UPnP=true"),
    ],
)
def test_unsafe_config_is_rejected_without_rewriting_it(roots, original, replacement):
    prepare(roots, apply=True)
    path = roots[0] / "qbittorrent/qBittorrent/qBittorrent.conf"
    value = path.read_text()
    assert original in value
    path.write_text(value.replace(original, replacement))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        prepare(roots, apply=True)
    assert path.read_bytes() == before


def test_password_drift_cannot_silently_reinitialize_existing_server(roots):
    prepare(roots, apply=True)
    secret = roots[1] / "qbit_password"
    secret.write_text("synthetic-different-credential-000000000000")
    with pytest.raises(ValueError, match="PASSWORD_BINDING"):
        prepare(roots, apply=True)
    assert secret.read_text() == "synthetic-different-credential-000000000000"


@pytest.mark.parametrize("existing", ["credential", "state", "symlink"])
def test_partial_or_foreign_data_requires_reconciliation(roots, existing):
    data, secrets = roots
    if existing == "credential":
        (secrets / "qbit_password").write_text("synthetic-existing-value")
    elif existing == "state":
        config = data / "qbittorrent"
        config.mkdir(mode=0o700)
        (config / "retained-state").write_text("synthetic-retained-state")
    else:
        (data / "qbittorrent").symlink_to(secrets, target_is_directory=True)
    before = sorted(str(p.relative_to(data.parent)) for p in data.parent.rglob("*"))
    with pytest.raises(ValueError):
        prepare(roots, apply=True)
    assert before == sorted(str(p.relative_to(data.parent)) for p in data.parent.rglob("*"))


def test_initializer_refuses_a_live_container_even_when_config_is_mounted_through_a_parent(tmp_path, monkeypatch):
    def query(argv, **_kwargs):
        if "info" in argv:
            value = json.dumps("synthetic-daemon")
        elif "ps" in argv:
            value = "a" * 64
        else:
            assert "inspect" in argv
            value = json.dumps([{"Source": str(tmp_path), "RW": True}])
        return SimpleNamespace(stdout=value)

    monkeypatch.setattr(provision.subprocess, "run", query)
    with pytest.raises(ValueError, match="STILL_MOUNTED"):
        provision._require_stopped("synthetic", "synthetic-daemon", tmp_path / "data/qbittorrent")


@pytest.fixture
def manifests(roots):
    data, secrets = roots
    prepare(roots, apply=True)
    for name in ["downloads", "remuxed"]:
        (data / name).mkdir(mode=0o700)
    for name in [
        "worker_password",
        "redis_client_url",
        "windows_ca",
        "windows_certificate",
        "windows_key",
        "download_pause_token",
    ]:
        path = secrets / name
        path.write_text("synthetic-file-secret")
        path.chmod(0o600)
    wireguard, resolver = secrets / "wg0.conf", secrets / "resolv.conf"
    wireguard.write_text("synthetic-unstarted-wireguard-config")
    resolver.write_text("nameserver 127.0.0.1\n")
    for path in [wireguard, resolver]:
        path.chmod(0o600)
    auth = secrets / "vpn_control_auth"
    auth.write_text(
        '[[roles]]\nname = "pixav-runtime"\nroutes = ["GET /v1/vpn/status", "PUT /v1/vpn/status"]\n'
        'auth = "apikey"\napikey = "' + "a" * 44 + '"\n'
    )
    auth.chmod(0o600)
    replacements = {
        "PIXAV_MANAGED_APP_IMAGE": "sha256:" + "a" * 64,
        "PIXAV_MANAGED_UID": str(os.getuid()),
        "PIXAV_MANAGED_GID": str(os.getgid()),
        "PIXAV_MANAGED_DATA_ROOT": str(data),
        "PIXAV_MANAGED_SECRETS_ROOT": str(secrets),
        "PIXAV_MANAGED_DATABASE": "synthetic_database",
        "PIXAV_EXPECTED_DB_IDENTITY": "123",
        "PIXAV_EXPECTED_REDIS_RUN_ID": "b" * 40,
        "PIXAV_MANAGED_WIREGUARD_CONFIG": str(wireguard),
        "PIXAV_MANAGED_VPN_RESOLV_CONF": str(resolver),
        "PIXAV_STORAGE_WORKER_OWNER": "11111111-1111-4111-8111-111111111111",
        "PIXAV_STORAGE_WINDOWS_RUNTIME_ID": "22222222-2222-4222-8222-222222222222",
        "PIXAV_STORAGE_WINDOWS_RUNTIME_GENERATION": "7",
        "PIXAV_WINDOWS_CONTROL_DOCKER_DAEMON_ID": "synthetic-daemon",
        "PIXAV_WINDOWS_CONTROL_IDENTITY_SHA256": "c" * 64,
        "PIXAV_WINDOWS_CAPACITY_VOLUME_DEVICE": "123",
    }
    text = (Path(__file__).resolve().parents[2] / "docker-compose.managed.yml").read_text()
    text = re.sub(
        r"\$\{([^}:]+)(?::([-?])([^}]*))?\}",
        lambda m: replacements.get(m[1], m[3] if m[2] == "-" else "synthetic-binding"),
        text,
    )
    manifest = yaml.safe_load(text)
    manifest["secrets"]["redis_client_url"]["file"] = str(secrets / "redis_client_url")
    for service in manifest["services"].values():
        if service.get("environment"):
            service["environment"] = {key: str(value) for key, value in service["environment"].items()}
        if isinstance(service.get("networks"), list):
            service["networks"] = {name: None for name in service["networks"]}
        for key in ["mem_limit", "memswap_limit"]:
            value = str(service[key])
            service[key] = int(value[:-1]) * {"m": 1024**2, "g": 1024**3}[value[-1]]
        if service.get("devices"):
            service["devices"] = [{"source": "/dev/net/tun", "target": "/dev/net/tun", "permissions": "rw"}]
        if service.get("secrets"):
            service["secrets"] = [
                row if isinstance(row, dict) else {"source": row, "target": "/run/secrets/" + row}
                for row in service["secrets"]
            ]
    args = SimpleNamespace(
        data_root=data,
        secrets_root=secrets,
        wireguard_config=wireguard,
        vpn_resolv_conf=resolver,
        db_identity="123",
        database="synthetic_database",
        redis_run_id="b" * 40,
        daemon_id="synthetic-daemon",
    )
    return manifest, args


def select(manifests, profile):
    manifest, args = manifests
    args.profile = profile
    manifest["services"] = {name: s for name, s in manifest["services"].items() if profile in s.get("profiles", [])}
    names = {row["source"] for s in manifest["services"].values() for row in s.get("secrets", [])}
    manifest["secrets"] = {name: s for name, s in manifest["secrets"].items() if name in names}
    return manifest, args


def test_torrent_uses_only_its_vpn_namespace_and_private_config(manifests):
    config, args = select(manifests, "torrent")
    deployment.validate_torrent(config, args)
    assert set(config["services"]) == {"gluetun", "qbittorrent"}


@pytest.mark.parametrize("defect", ["bridge", "dns", "port", "credential", "mutable", "resolver", "firewall", "umask"])
def test_torrent_refuses_network_and_secret_bypasses(manifests, defect):
    config, args = select(manifests, "torrent")
    qbit = config["services"]["qbittorrent"]
    if defect == "bridge":
        qbit["network_mode"] = "bridge"
    elif defect == "dns":
        qbit["dns"] = ["1.1.1.1"]
    elif defect == "port":
        qbit["ports"] = [{"target": 8080, "published": "8080"}]
    elif defect == "credential":
        qbit["environment"]["PASSWORD"] = "synthetic-private-value"
    elif defect == "mutable":
        qbit["image"] = "qbittorrent:latest"
    elif defect == "resolver":
        qbit["volumes"][-1]["read_only"] = False
    elif defect == "firewall":
        config["services"]["gluetun"]["environment"]["FIREWALL_OUTBOUND_SUBNETS"] = "0.0.0.0/0"
    else:
        qbit["command"] = [qbit["command"][0].replace("umask 077; ", "")]
    with pytest.raises(ValueError):
        deployment.validate_torrent(config, args)


def test_media_has_worker_role_and_no_socket_guest_or_cookie(manifests):
    config, args = select(manifests, "media")
    deployment.validate_application(config, args)
    service = config["services"]["media_loader"]
    assert {m["target"] for m in service["volumes"]} == {"/app/data/downloads", "/app/data/remuxed"}
    assert service["environment"]["PIXAV_DB_USER"] == "pixav_worker"


def test_media_accepts_exact_separate_redis_reference(manifests, tmp_path):
    config, args = select(manifests, "media")
    private = tmp_path / "candidate-private"
    private.mkdir(mode=0o700)
    reference = private / "redis-url"
    reference.write_text("synthetic-private-reference")
    reference.chmod(0o600)
    config["secrets"]["redis_client_url"]["file"] = str(reference)
    args.redis_client_url_secret = reference
    deployment.validate_application(config, args)


@pytest.mark.parametrize("defect", ["legacy", "qbit-url", "socket", "cookie", "role", "network"])
def test_media_refuses_legacy_execution_or_credential_expansion(manifests, defect):
    config, args = select(manifests, "media")
    service = config["services"]["media_loader"]
    if defect == "legacy":
        service["environment"]["PIXAV_MANAGED_MEDIA_WORKFLOW"] = "false"
    elif defect == "qbit-url":
        service["environment"]["PIXAV_QBIT_URL"] = "http://localhost:8080"
    elif defect == "socket":
        service["volumes"].append({"target": "/var/run/docker.sock"})
    elif defect == "cookie":
        service["secrets"].append({"source": "photos_auth", "target": "/run/photos-auth/cookies.json"})
    elif defect == "role":
        service["environment"]["PIXAV_DB_USER"] = "pixav_authority"
    else:
        config["networks"]["download_control"]["internal"] = False
    with pytest.raises(ValueError):
        deployment.validate_application(config, args)
