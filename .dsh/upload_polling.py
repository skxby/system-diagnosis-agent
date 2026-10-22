"""验收 CLI 的真实 202 上传轮询，保留完成结果的既有返回约定。"""
import json
import time
from urllib.request import Request, urlopen


def wait_upload(base: str, project_id: int, token: str, code: int, payload: dict,
                timeout: float = 600) -> tuple[int, dict]:
    if code != 202:
        return code, payload
    deadline = time.monotonic() + timeout
    if payload.get("status") == "completed":
        return 200, payload
    while time.monotonic() < deadline:
        request = Request(f"{base}/api/projects/{project_id}/uploads/{payload['job_id']}",
                          headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        if payload["status"] == "completed":
            return 200, payload
        if payload["status"] == "failed":
            return 500, payload
        time.sleep(.2)
    return 504, {**payload, "error": "等待上传解析超时"}
