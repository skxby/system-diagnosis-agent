import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.api.schemas import CreateDataSourceRequest
from app.config import Settings, get_settings
from app.worker_supervisor import healthy, read_state, state_path, stop_owned_worker


def test_invalid_and_stale_health_never_reports_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    path = state_path("analysis")
    path.parent.mkdir()
    for text in ["[]", "broken", json.dumps({"healthy": True, "updated_at": time.time() - 30}),
                 json.dumps({"healthy": True, "updated_at": time.time() + 30}),
                 json.dumps({"healthy": True, "updated_at": "invalid"})]:
        path.write_text(text, encoding="utf-8")
        assert not healthy("analysis")
    path.write_text(json.dumps({"healthy": True, "updated_at": time.time()}), encoding="utf-8")
    assert healthy("analysis")


def test_recycled_pid_is_not_killed():
    with patch("app.worker_supervisor.psutil.Process") as process, patch("app.worker_supervisor.terminate_tree") as kill:
        process.return_value.create_time.return_value = 200
        stop_owned_worker(123, 100, {})
        kill.assert_not_called()


def test_non_celery_process_is_never_killed():
    with patch("app.worker_supervisor.psutil.Process") as process, patch("app.worker_supervisor.terminate_tree") as kill:
        process.return_value.create_time.return_value = 100
        process.return_value.cmdline.return_value = ["browser.exe"]
        with pytest.raises(RuntimeError):
            stop_owned_worker(123, 100, {})
        kill.assert_not_called()


@pytest.mark.parametrize("field", ["worker_task_timeout_seconds", "worker_probe_timeout_seconds", "upload_queue_timeout_seconds"])
@pytest.mark.parametrize("value", [0, float("inf"), float("nan")])
def test_timeout_configuration_cannot_disable_supervision(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("field", ["upload_jobs", "uploads"])
def test_public_datasource_metadata_cannot_inject_runtime_jobs(field):
    with pytest.raises(ValidationError):
        CreateDataSourceRequest(format="txt", metadata={field: {"fake": {"uploads_dir": "/etc"}}})
