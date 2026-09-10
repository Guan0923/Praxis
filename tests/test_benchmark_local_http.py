from __future__ import annotations

import socket
from threading import Thread

import httpx
import uvicorn

from tests.benchmark_local_support import acceptance_app, local_model, local_tasks
from tests.test_benchmark_service import until


def test_real_http_runtime_file_tool_and_grading(tmp_path_factory, monkeypatch, local_sandbox_runtime):
    import benchmarks.tasks

    tasks = local_tasks()
    monkeypatch.setattr(benchmarks.tasks, "ALL_TASKS", tasks)
    monkeypatch.setattr(benchmarks.tasks, "TASKS_BY_NAME", {task.name: task for task in tasks})
    with local_model(delay=0.05) as (config, calls):
        app = acceptance_app(tmp_path_factory.mktemp("http") / "web", config)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            until(lambda: server.started)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                response = client.post("/benchmark/run", json={"task": tasks[0].name})
                assert response.status_code == 202
                batch = response.json()
                assert batch["finished"] == 0
                assert client.delete(f"/benchmark/tasks/{tasks[0].name}/resources").status_code == 409
                until(lambda: client.get(f"/benchmark/runs/{batch['id']}").json()["finished"] == 1, timeout=30)
                finished = client.get(f"/benchmark/runs/{batch['id']}").json()
                result = finished["tasks"][0]["result"]
                assert finished["status"] == "completed", result.get("error")
                assert result["passed"] is True
                assert result["metrics"]["tool_calls"] == 1
                assert result["metrics"]["model_calls"] == 2
                trace = client.get(f"/benchmark/runs/{batch['id']}/tasks/{batch['tasks'][0]['id']}/trace").json()
                assert any(event["kind"] == "tool_call" for event in trace)
                assert len(calls) == 2
                batch = client.post("/benchmark/run-all", json={}).json()
                until(lambda: client.get(f"/benchmark/runs/{batch['id']}").json()["finished"] == 9, timeout=30)
                finished = client.get(f"/benchmark/runs/{batch['id']}").json()
                assert finished["status"] == "completed"
                assert sum(item["result"]["passed"] for item in finished["tasks"]) == 8
                assert len(calls) == 20
                cancelled = client.post("/benchmark/run-all", json={}).json()
                client.post(f"/benchmark/runs/{cancelled['id']}/cancel", json={})
                until(
                    lambda: client.get(f"/benchmark/runs/{cancelled['id']}").json()["status"] == "cancelled", timeout=30
                )
                assert (
                    client.post(
                        "/benchmark/run-all", json={}, headers={"Origin": "https://untrusted.invalid"}
                    ).status_code
                    == 403
                )
        finally:
            server.should_exit = True
            worker.join(timeout=15)
            listener.close()
            assert not worker.is_alive()
