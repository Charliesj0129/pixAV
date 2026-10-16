"""Role provisioning accepts private file references and refuses ambiguity."""

import pytest

from scripts.provision_workflow_roles import _password

VARIABLE = "PIXAV_AUTHORITY_ROLE_PASSWORD"


def test_role_password_is_loaded_from_private_file(tmp_path, monkeypatch):
    path = tmp_path / "role-secret"
    path.write_text(" synthetic-file-password \n")
    path.chmod(0o600)
    monkeypatch.delenv(VARIABLE, raising=False)
    monkeypatch.setenv(VARIABLE + "_FILE", str(path))
    assert _password(VARIABLE) == " synthetic-file-password "


def test_ambiguous_secret_sources_are_refused_without_echo(tmp_path, monkeypatch):
    monkeypatch.setenv(VARIABLE, "synthetic-env-password")
    monkeypatch.setenv(VARIABLE + "_FILE", str(tmp_path / "role-secret"))
    with pytest.raises(SystemExit, match="ROLE_SECRET_SOURCE_AMBIGUOUS") as error:
        _password(VARIABLE)
    assert "synthetic-env-password" not in str(error.value)


@pytest.mark.parametrize("defect", ["public", "missing", "symlink"])
def test_role_secret_must_be_an_exact_private_file(tmp_path, monkeypatch, defect):
    path = tmp_path / "role-secret"
    if defect != "missing":
        path.write_text("synthetic-file-password")
        path.chmod(0o644 if defect == "public" else 0o600)
    if defect == "symlink":
        link = tmp_path / "alias"
        link.symlink_to(path)
        path = link
    monkeypatch.delenv(VARIABLE, raising=False)
    monkeypatch.setenv(VARIABLE + "_FILE", str(path))
    with pytest.raises(SystemExit, match="ROLE_SECRET_FILE_INVALID"):
        _password(VARIABLE)
