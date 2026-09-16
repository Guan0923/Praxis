"""Run with the Harbor Python environment: python -m unittest discover -s tests -p test_harbor_agent.py."""

import asyncio
import base64
import importlib.util
import shlex
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

if importlib.util.find_spec("harbor") is None:
    raise unittest.SkipTest("Harbor is optional; run these tests in the Harbor tool environment.")

from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from backend.tools.base import ToolError, ToolInvocationContext
from benchmarks.harbor_agent import PraxisAgent
from benchmarks.harbor_tools import HarborTools


class HarborToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = Mock()
        self.environment.exec = AsyncMock(return_value=ExecResult(stdout="hello", stderr="!", return_code=7))
        self.stopped = Event()
        self.bridge = HarborTools(self.environment, asyncio.get_running_loop(), self.stopped)

    async def test_exec_and_read_use_harbor_environment(self):
        context = ToolInvocationContext()
        result = await asyncio.to_thread(self.bridge.execute, context, "pwd", 12)
        self.assertEqual(result, "exit_code=7\nhello!")
        self.environment.exec.assert_awaited_once_with(command="pwd", timeout_sec=12)
        await asyncio.to_thread(self.bridge.read, context, "/app/a b; touch BAD")
        self.environment.exec.assert_awaited_with(command="head -c 50000 -- '/app/a b; touch BAD'", timeout_sec=60)

    async def test_write_uses_bounded_commands_under_the_task_user(self):
        self.environment.exec.return_value = ExecResult(return_code=0)
        content = "hello\nworld" * 3000
        await asyncio.to_thread(self.bridge.write, ToolInvocationContext(), "/app/a b; BAD", content)
        commands = [call.kwargs["command"] for call in self.environment.exec.await_args_list]
        chunks = [shlex.split(command) for command in commands]
        self.assertEqual(b"".join(base64.b64decode(chunk[2]) for chunk in chunks), content.encode())
        self.assertEqual([chunk[-2] for chunk in chunks], [">"] + [">>"] * (len(chunks) - 1))
        self.assertTrue(all(chunk[-1] == "/app/a b; BAD" for chunk in chunks))
        self.assertTrue(all(len(command) < 13000 for command in commands))
        self.environment.upload_file.assert_not_called()

    async def test_write_reports_permission_errors(self):
        self.environment.exec.return_value = ExecResult(return_code=1, stderr="Permission denied")
        with self.assertRaisesRegex(ToolError, "Permission denied"):
            await asyncio.to_thread(self.bridge.write, ToolInvocationContext(), "/root/file", "data")

    async def test_stopped_trial_does_not_execute(self):
        self.stopped.set()
        with self.assertRaises(ToolError):
            await asyncio.to_thread(self.bridge.execute, ToolInvocationContext(), "pwd")
        self.environment.exec.assert_not_awaited()

    async def test_stop_cancels_inflight_environment_call(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def execute(**kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.environment.exec = execute
        worker = asyncio.create_task(asyncio.to_thread(self.bridge.execute, ToolInvocationContext(), "sleep 100"))
        await asyncio.wait_for(started.wait(), 2)
        self.stopped.set()
        with self.assertRaises(ToolError):
            await asyncio.wait_for(worker, 2)
        await asyncio.wait_for(cancelled.wait(), 2)

    async def test_command_timeout_is_not_retried(self):
        self.environment.exec.side_effect = TimeoutError("command timed out")
        with self.assertRaises(ToolError):
            await asyncio.wait_for(asyncio.to_thread(self.bridge.execute, ToolInvocationContext(), "slow"), 2)
        self.environment.exec.assert_awaited_once()


class HarborAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_records_selected_model_without_exposing_config(self):
        with TemporaryDirectory() as temporary:
            agent = PraxisAgent(logs_dir=Path(temporary))
            agent._model_config = Mock(return_value=SimpleNamespace(model="test-model"))
            agent._reasoning_effort = Mock(return_value="medium")
            await agent.setup(Mock())
            self.assertEqual(agent.reasoning_effort, "medium")
            self.assertEqual(agent.model_name, "test-model")
            self.assertEqual(agent.to_agent_info().model_info.name, "test-model")

    async def test_reasoning_effort_from_local_toml(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            agent = PraxisAgent(logs_dir=Path(temporary))
            with patch("benchmarks.harbor_agent.ClientPaths.from_home", return_value=SimpleNamespace(config_file=path)):
                self.assertEqual(agent._reasoning_effort(), "max")
                path.write_text('[benchmark]\nreasoning_effort = "medium"\n', encoding="utf-8")
                self.assertEqual(agent._reasoning_effort(), "medium")
            explicit = PraxisAgent(logs_dir=Path(temporary), config_path=str(path))
            self.assertEqual(explicit._reasoning_effort(), "medium")
            path.write_text('[benchmark]\nreasoning_effort = "invalid"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "benchmark.reasoning_effort"):
                explicit._reasoning_effort()

    async def test_invalid_options_fail_before_running(self):
        with self.assertRaises(ValueError):
            PraxisAgent(logs_dir=Path("unused"), max_tool_calls=0)
        with self.assertRaises(ValueError):
            PraxisAgent(logs_dir=Path("unused"), misspelled_option=True)

    async def test_cancellation_joins_runtime_before_returning(self):
        with TemporaryDirectory() as temporary:
            agent = PraxisAgent(logs_dir=Path(temporary))
            entered, exited = Event(), Event()

            def run(instruction, bridge, context):
                entered.set()
                agent.stopped.wait(5)
                exited.set()

            agent._run_sync = run
            task = asyncio.create_task(agent.run("task", Mock(), AgentContext()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(exited.is_set())

    async def test_records_token_usage(self):
        agent = PraxisAgent(logs_dir=Path("unused"))
        agent.collector.prompt_tokens = 123
        agent.collector.completion_tokens = 45
        context = AgentContext()
        agent.populate_context_post_run(context)
        self.assertEqual(context.n_input_tokens, 123)
        self.assertEqual(context.n_output_tokens, 45)
