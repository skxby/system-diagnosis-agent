"""真实 HTTP 202→轮询→完成契约；不使用 eager 或直接调用任务来冒充异步。"""
import time
from typing import Any
from fastapi.testclient import TestClient
from httpx import Response


def upload_and_wait(client: TestClient, path: str, **kwargs: Any) -> Response:
    response = client.post(path, **kwargs)
    if response.status_code != 202:
        return response
    accepted = response.json()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.get(path.rsplit("/upload", 1)[0] + "/uploads/" + accepted["job_id"],
                              headers=kwargs.get("headers"))
        assert response.status_code == 200, response.text
        if response.json()["status"] == "completed":
            return response
        assert response.json()["status"] != "failed", response.text
        time.sleep(.05)
    raise AssertionError("真实上传 Worker 在 60 秒内没有完成任务")
