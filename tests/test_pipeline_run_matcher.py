"""Cleanup matching must not depend on event naming or tenant policy."""
import pytest
from lib.tekton_matchers import is_pipeline_run, make_pipeline_matchers

@pytest.mark.parametrize("name", ["push", "pull-request", "tag", "group-test", "e2e", "arbitrary"])
@pytest.mark.parametrize("namespace", ["open-data-hub-tenant", "rhoai-tenant", None])
def test_all_pipeline_run_identities(name, namespace):
    assert is_pipeline_run(name, {"kind": "PipelineRun", "metadata": {"namespace": namespace}})
    assert make_pipeline_matchers()["is_pipeline_run"] is is_pipeline_run

@pytest.mark.parametrize("data", [None, [], {}, {"kind": "Task"}, {"kind": "Pipeline"}])
def test_other_resources_do_not_match(data):
    assert not is_pipeline_run("on-push", data)
