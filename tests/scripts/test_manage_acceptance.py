"""Validate synthetic legacy deployment contracts supplied by the operator."""

import argparse
import copy
import json

import pytest

from pixav.shared.backup_files import create_backup_file
from scripts.manage_acceptance import _containers, configuration_hashes, preflight, validate_config


@pytest.fixture
def deployment(tmp_path):
    private = tmp_path / "secrets"
    private.mkdir(mode=0o700)
    env = private / "deployment.env"
    with create_backup_file(env) as handle:
        handle.write("PIXAV_ACCEPTANCE_ADMIN_PASSWORD=synthetic-password\n")
    common = {
        "PIXAV_LOCAL_CLEANUP_APPLY": "false",
        "PIXAV_MANAGED_MEDIA_WORKFLOW": "true",
        "PIXAV_PIXEL_INJECTOR_MODE": "redroid",
        "PIXAV_REDROID_PROFILE": "gphotos_pixel_xl_v1",
        "PIXAV_STORAGE_READBACK_IMAGE": "sha256:" + "2" * 64,
        "PIXAV_STORAGE_UPLOAD_TOOLS_IMAGE": "sha256:" + "7" * 64,
    }
    application = (
        "migrate",
        "provision",
        "maxwell_core",
        "media_loader",
        "storage_worker",
        "playback_worker",
        "strm_resolver",
        "sht_probe",
    )
    services = {name: {"image": "sha256:" + "1" * 64, "environment": dict(common)} for name in application}
    for name, digit in (
        ("postgres", "3"),
        ("redis", "4"),
        ("qbittorrent", "5"),
        ("playback-gateway", "8"),
        ("jellyfin", "9"),
        ("jellyfin-client-gateway", "8"),
        ("av_capture", "6"),
    ):
        services[name] = {"image": "sha256:" + digit * 64}
    services["media_loader"]["environment"]["PIXAV_QBIT_URL"] = "http://qbittorrent:8080"
    services["playback_worker"]["depends_on"] = {"provision": {"condition": "service_completed_successfully"}}
    services["sht_probe"].update(
        restart="no",
        command=["source", "--once"],
        volumes=[
            {"type": "bind", "source": str(private / "source"), "target": "/run/secrets/source", "read_only": True}
        ],
    )
    services["sht_probe"]["environment"].update(
        PIXAV_SOURCE_MIN_QUALITY_SCORE="0",
        PIXAV_CRAWL_QUERIES="",
        PIXAV_CRAWL_COHORT_FILE="/run/secrets/source/cohort.json",
    )
    services["strm_resolver"]["environment"].update(
        PIXAV_RESOLVER_REQUIRE_READER_ROLE="true", PIXAV_RESOLVER_AUTH_REQUIRED="true"
    )
    services["strm_resolver"]["volumes"] = [
        {"type": "bind", "source": str(tmp_path / "data/playback-cache"), "target": "/app/data/playback-cache"},
        {"type": "bind", "source": str(private / "resolver_token"), "target": "/run/secrets/resolver_token"},
    ]
    services["jellyfin"]["networks"] = {"playback": {}}
    services["jellyfin-client-gateway"].update(
        networks={"playback": {}, "client_capture": {}}, ports=[{"host_ip": "127.0.0.1", "published": "18096"}]
    )
    services["av_capture"].update(
        networks={"client_capture": {}},
        volumes=[
            {"type": "bind", "source": str(tmp_path / "evidence/client-capture"), "target": "/work"},
            {
                "type": "bind",
                "source": str(private / "capture-config.json"),
                "target": "/run/secrets/capture-config.json",
            },
        ],
    )
    config = {"services": services, "networks": {"playback": {}, "client_capture": {"internal": True}}}
    with create_backup_file(tmp_path / "compose.json") as handle:
        json.dump(config, handle)
    return tmp_path, env, config


def test_isolated_manifest_is_complete_and_has_no_production_mounts(deployment):
    root, _, config = deployment
    validate_config(config, root)
    assert config["services"]["media_loader"]["environment"]["PIXAV_QBIT_URL"] == "http://qbittorrent:8080"
    assert config["services"]["storage_worker"]["environment"]["PIXAV_PIXEL_INJECTOR_MODE"] == "redroid"
    assert (
        config["services"]["playback_worker"]["depends_on"]["provision"]["condition"]
        == "service_completed_successfully"
    )
    source = config["services"]["sht_probe"]
    assert source["restart"] == "no" and source["command"][-1] == "--once"
    assert source["environment"]["PIXAV_CRAWL_COHORT_FILE"] == "/run/secrets/source/cohort.json"


@pytest.mark.parametrize("violation", ["writable-session", "different-source-image", "source-policy"])
def test_native_source_cannot_bypass_private_scope_or_policy(deployment, violation):
    root, _, original = deployment
    config = copy.deepcopy(original)
    source = config["services"]["sht_probe"]
    if violation == "writable-session":
        source["volumes"][0]["read_only"] = False
    elif violation == "different-source-image":
        source["image"] = "sha256:" + "9" * 64
    else:
        source["environment"]["PIXAV_SOURCE_MIN_QUALITY_SCORE"] = "-10000"
    with pytest.raises(ValueError):
        validate_config(config, root)


@pytest.mark.parametrize(
    "violation",
    [
        "mutable-image",
        "different-app",
        "container-name",
        "host-network",
        "public-port",
        "socket-reader",
        "outside-root",
        "cleanup",
        "pixel",
        "reader-check",
        "auth",
        "staging-reader",
    ],
)
def test_unsafe_deployment_is_refused(deployment, violation):  # noqa: C901 -- independent refusal cases
    root, _, original = deployment
    config = copy.deepcopy(original)
    resolver = config["services"]["strm_resolver"]
    if violation == "mutable-image":
        config["services"]["redis"]["image"] = "redis:latest"
    elif violation == "different-app":
        resolver["image"] = "sha256:" + "6" * 64
    elif violation == "container-name":
        resolver["container_name"] = "pixav-strm-resolver"
    elif violation == "host-network":
        resolver["network_mode"] = "host"
    elif violation == "public-port":
        config["services"]["jellyfin-client-gateway"]["ports"][0][
            "host_ip"
        ] = "0.0.0.0"  # noqa: S104 -- deliberate rejection fixture
    elif violation == "socket-reader":
        resolver["volumes"].append({"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"})
    elif violation == "outside-root":
        resolver["volumes"][0]["source"] = "/home/operator/production/data"
    elif violation == "cleanup":
        resolver["environment"]["PIXAV_LOCAL_CLEANUP_APPLY"] = "true"
    elif violation == "pixel":
        resolver["environment"]["PIXAV_PIXEL_INJECTOR_MODE"] = "local"
    elif violation == "reader-check":
        resolver["environment"]["PIXAV_RESOLVER_REQUIRE_READER_ROLE"] = "false"
    elif violation == "auth":
        resolver["environment"]["PIXAV_RESOLVER_AUTH_REQUIRED"] = "false"
    else:
        resolver["volumes"].append(
            {"type": "bind", "source": str(root / "data/downloads"), "target": "/app/data/downloads"}
        )
    with pytest.raises(ValueError):
        validate_config(config, root)


@pytest.mark.parametrize("violation", ["origin-port", "origin-client-network", "client-origin-network"])
def test_client_cannot_bypass_jellyfin_authentication_gateway(deployment, violation):
    root, _, original = deployment
    config = copy.deepcopy(original)
    if violation == "origin-port":
        config["services"]["jellyfin"]["ports"] = [{"host_ip": "127.0.0.1", "published": "8096"}]
    elif violation == "origin-client-network":
        config["services"]["jellyfin"]["networks"]["client_capture"] = {}
    else:
        config["services"]["av_capture"]["networks"]["playback"] = {}
    with pytest.raises(ValueError):
        validate_config(config, root)


def test_preflight_receipt_has_no_resolved_credentials_or_fake_runtime_pass(deployment, monkeypatch):
    root, env, config = deployment

    def run(command):
        if "info" in command:
            return "synthetic-daemon"
        if "config" in command:
            return json.dumps(config)
        if "image" in command:
            return json.dumps([{"Id": command[-1]}])
        if "ps" in command:
            return "[]"
        raise AssertionError(command)

    monkeypatch.setattr("scripts.manage_acceptance._run", run)
    receipt = preflight(
        argparse.Namespace(
            project="pixav-acceptance-contract",
            env_file=env,
            compose_file=root / "compose.json",
            root=root,
            context="default",
            daemon_id="synthetic-daemon",
        )
    )
    assert "synthetic-password" not in json.dumps(receipt)
    assert receipt["runtime"] == receipt["golden_path"] == "OPEN"
    assert receipt["instance"] == {}


@pytest.mark.parametrize("payload", ["", "[]", "{}", "{}\n{}\n", "[{},{}]"])
def test_compose_ps_json_formats(payload):
    assert len(_containers(payload)) == payload.count("{")


def test_support_configuration_hash_detects_changes_and_refuses_symlinks(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    template = config / "gateway.conf"
    template.write_text("first")
    initial = configuration_hashes(tmp_path)
    template.write_text("changed")
    assert configuration_hashes(tmp_path) != initial
    template.unlink()
    template.symlink_to(tmp_path / "outside.conf")
    with pytest.raises(ValueError, match="symlinks"):
        configuration_hashes(tmp_path)


@pytest.mark.parametrize("violation", ["image", "duplicate", "unknown"])
def test_preflight_rejects_runtime_drift(deployment, monkeypatch, violation):
    root, env, config = deployment
    rows = [{"ID": "synthetic-container", "Service": "maxwell_core", "State": "running"}]
    info = {
        "Id": "synthetic-container",
        "Image": config["services"]["maxwell_core"]["image"],
        "State": {"Status": "running"},
        "Config": {
            "Labels": {
                "com.docker.compose.project": "pixav-acceptance-contract",
                "com.docker.compose.project.config_files": str(root / "compose.json"),
            }
        },
        "Mounts": [],
    }
    if violation == "image":
        info["Image"] = "sha256:" + "9" * 64
    elif violation == "duplicate":
        rows.append(dict(rows[0]))
    else:
        rows[0]["Service"] = "unmanaged"

    def run(command):
        if "info" in command:
            return "synthetic-daemon"
        if "config" in command:
            return json.dumps(config)
        if "image" in command:
            return json.dumps([{"Id": command[-1]}])
        if "ps" in command:
            return json.dumps(rows)
        if "inspect" in command:
            return json.dumps([info])
        raise AssertionError(command)

    monkeypatch.setattr("scripts.manage_acceptance._run", run)
    with pytest.raises(ValueError):
        preflight(
            argparse.Namespace(
                project="pixav-acceptance-contract",
                env_file=env,
                compose_file=root / "compose.json",
                root=root,
                context="default",
                daemon_id="synthetic-daemon",
            )
        )
