"""Private Maestro invocation safety, independent of Google live acceptance."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.pixel_injector.canary_maestro import run_flow


@pytest.fixture
def runner():
    return Mock(labels={OWNER_LABEL: "owner", "pixav.photos_canary.role": "tools"})


@pytest.fixture
def flow(tmp_path):
    path = tmp_path / "login.yaml"
    path.write_text("appId: com.google.android.gms\n---\n- inputText: ${MAESTRO_PASSWORD}\n")
    return path


def test_credentials_only_in_exec_environment_and_private_traces_removed(runner, flow):
    runner.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"untrusted credentials in CLI output")
    run_flow(runner, "owner", flow, credentials={"email": "fake@example.test", "password": "private-password"})
    stage, execute, cleanup = runner.exec_run.call_args_list
    assert "private-password" not in str(stage)
    assert "private-password" not in str(execute.args)
    assert execute.kwargs["environment"]["MAESTRO_PASSWORD"] == "private-password"
    assert execute.kwargs["environment"]["JAVA_TOOL_OPTIONS"].startswith("-Duser.home=/tmp/canary-flow-")
    assert execute.args[0][:3] == ["timeout", "--kill-after=5s", "90"]
    assert cleanup.args[0] == ["rm", "-rf", "--", execute.kwargs["workdir"]]


def test_failure_does_not_expose_output_and_still_cleans_traces(runner, flow):
    runner.exec_run.side_effect = [
        SimpleNamespace(exit_code=0),
        SimpleNamespace(exit_code=124, output=b"private-password signed-url"),
        SimpleNamespace(exit_code=0),
    ]
    with pytest.raises(CanaryBlockedError, match="inspect UI before retry") as error:
        run_flow(runner, "owner", flow)
    assert "private-password" not in str(error.value)
    assert runner.exec_run.call_count == 3


def test_failed_staging_does_not_delete_unowned_directory(runner, flow):
    runner.exec_run.return_value = SimpleNamespace(exit_code=1)
    with pytest.raises(CanaryBlockedError, match="stage"):
        run_flow(runner, "owner", flow)
    assert runner.exec_run.call_count == 1


def test_wrong_owner_cannot_run(runner, flow):
    with pytest.raises(CanaryBlockedError, match="ownership"):
        run_flow(runner, "different", flow)
    runner.exec_run.assert_not_called()


def test_cleanup_failure_is_visible(runner, flow):
    runner.exec_run.side_effect = [
        SimpleNamespace(exit_code=0),
        SimpleNamespace(exit_code=0),
        SimpleNamespace(exit_code=1),
    ]
    with pytest.raises(CanaryBlockedError, match="cleanup failed"):
        run_flow(runner, "owner", flow)


def test_hierarchy_retains_only_attributes_in_memory_and_cleans(runner):
    from pixav.pixel_injector.canary_maestro import hierarchy

    runner.exec_run.side_effect = [
        SimpleNamespace(exit_code=0),
        SimpleNamespace(
            exit_code=0, output=b'log\n{"attributes":{},"children":[{"attributes":{"text":"Backed up"}}]}\ntrailing log'
        ),
        SimpleNamespace(exit_code=0),
    ]
    assert hierarchy(runner, "owner") == [{}, {"text": "Backed up"}]
    assert runner.exec_run.call_args.args[0][:3] == ["rm", "-rf", "--"]
