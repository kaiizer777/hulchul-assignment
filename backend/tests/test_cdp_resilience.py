"""Issue #31: mid-run CDP drop classification, bounded reattach, distinct session_lost."""

import asyncio
import json
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock

from backend.tools import (
    is_session_lost_error,
    navigate,
    read_page,
    PlaywrightTools,
)
from backend.browser import (
    is_browser_session_alive,
    check_browser_session_health,
)
from backend.agent import ReActAgent


class TestClosedTargetClassifier(unittest.TestCase):
    def test_exact_playwright_closed_message(self):
        self.assertTrue(
            is_session_lost_error("Target page, context or browser has been closed")
        )

    def test_variants(self):
        self.assertTrue(is_session_lost_error("Target closed"))
        self.assertTrue(is_session_lost_error(Exception("Browser has been closed")))
        self.assertTrue(is_session_lost_error("Connection closed while reading"))
        self.assertTrue(is_session_lost_error("Protocol error: Session closed"))

    def test_narrowed_excludes_broad_patterns(self):
        self.assertFalse(is_session_lost_error("websocket connection failed"))
        self.assertFalse(is_session_lost_error("websocket"))
        self.assertFalse(is_session_lost_error("Protocol error: timeout exceeded"))
        self.assertFalse(is_session_lost_error("Something has been closed"))
        self.assertFalse(is_session_lost_error("Element not found: Submit"))

    def test_exact_transport_strings(self):
        self.assertTrue(is_session_lost_error("websocket is not open: readyState 3 (CLOSED)"))
        self.assertTrue(is_session_lost_error("websocket closed: connection dropped"))

    def test_non_session_errors(self):
        self.assertFalse(is_session_lost_error("Element not found: Submit"))
        self.assertFalse(is_session_lost_error("Timeout 5000ms exceeded"))
        self.assertFalse(is_session_lost_error(""))
        self.assertFalse(is_session_lost_error(None))
        self.assertFalse(is_session_lost_error("Simulated ERP 500 Error"))


class TestBrowserToolsTagSessionLost(unittest.IsolatedAsyncioTestCase):
    async def test_navigate_marks_session_lost(self):
        page = MagicMock()
        page.goto = AsyncMock(
            side_effect=Exception("Target page, context or browser has been closed")
        )
        page.url = "about:blank"
        res = await navigate(page, "about:blank")
        self.assertFalse(res["success"])
        self.assertTrue(res.get("session_lost"))

    async def test_navigate_generic_not_session_lost(self):
        page = MagicMock()
        page.goto = AsyncMock(side_effect=Exception("Element not found"))
        page.url = "about:blank"
        res = await navigate(page, "about:blank")
        self.assertFalse(res["success"])
        self.assertFalse(res.get("session_lost"))

    async def test_read_page_marks_session_lost(self):
        page = MagicMock()
        page.aria_snapshot = AsyncMock(
            side_effect=Exception("Target page, context or browser has been closed")
        )
        res = await read_page(page)
        self.assertFalse(res["success"])
        self.assertTrue(res.get("session_lost"))


class TestBrowserSessionHealth(unittest.IsolatedAsyncioTestCase):
    async def test_alive_false_when_page_closed(self):
        page = MagicMock()
        page.is_closed.return_value = True
        browser = MagicMock()
        browser.is_connected.return_value = True
        sess = MagicMock(page=page, browser=browser)
        self.assertFalse(is_browser_session_alive(sess))
        self.assertFalse(await check_browser_session_health(sess))

    async def test_alive_false_when_disconnected(self):
        page = MagicMock()
        page.is_closed.return_value = False
        browser = MagicMock()
        browser.is_connected.return_value = False
        sess = MagicMock(page=page, browser=browser)
        self.assertFalse(is_browser_session_alive(sess))

    async def test_alive_true_when_healthy(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.evaluate = AsyncMock(return_value="complete")
        browser = MagicMock()
        browser.is_connected.return_value = True
        sess = MagicMock(page=page, browser=browser)
        self.assertTrue(is_browser_session_alive(sess))
        self.assertTrue(await check_browser_session_health(sess))

    async def test_health_false_on_closed_target_evaluate(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.evaluate = AsyncMock(
            side_effect=Exception("Target page, context or browser has been closed")
        )
        browser = MagicMock()
        browser.is_connected.return_value = True
        sess = MagicMock(page=page, browser=browser)
        self.assertFalse(await check_browser_session_health(sess))


def _mock_redis():
    redis = MagicMock()
    redis.is_configured = False
    redis.set_session_state = AsyncMock(return_value=True)
    redis.get_pause_flag = AsyncMock(return_value=False)
    return redis


def _groq_for_tool_calls(calls):
    """Build a mock Groq client yielding tool-call messages then a done message."""
    mock_groq = MagicMock()
    mock_completions = MagicMock()
    responses = []
    for cid, name, args in calls:
        msg = MagicMock()
        tc = MagicMock()
        tc.id = cid
        tc.function.name = name
        tc.function.arguments = json.dumps(args)
        msg.tool_calls = [tc]
        msg.content = None
        responses.append(MagicMock(choices=[MagicMock(message=msg)]))
    done_msg = MagicMock()
    done_msg.tool_calls = None
    done_msg.content = "All work is done."
    responses.append(MagicMock(choices=[MagicMock(message=done_msg)]))
    mock_completions.create = AsyncMock(side_effect=responses)
    mock_groq.chat = MagicMock(completions=mock_completions)
    return mock_groq


class TestAgentReattach(unittest.IsolatedAsyncioTestCase):
    async def _make_agent(self, mock_tools, mock_groq, reconnect, events, max_reattaches=2, max_iterations=10):
        run_id = str(uuid.uuid4())

        async def _on_event(evt):
            events.append(evt)

        agent = ReActAgent(
            run_id=run_id,
            tools=mock_tools,
            groq_client=mock_groq,
            redis_client=_mock_redis(),
            max_iterations=max_iterations,
            on_event=_on_event,
            reconnect=reconnect,
            max_reattaches=max_reattaches,
        )
        agent.ensure_run_record = AsyncMock(return_value=None)
        agent.get_last_successful_step_index = AsyncMock(return_value=0)
        agent.persist_step = AsyncMock(return_value=str(uuid.uuid4()))
        agent.update_run_status = AsyncMock(return_value=None)
        return agent

    async def test_act_session_loss_reattaches_and_resumes(self):
        events = []
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.page = MagicMock()
        mock_tools.page.is_closed = MagicMock(return_value=False)
        mock_tools.set_run_id = MagicMock()
        mock_tools.set_page = MagicMock()
        mock_tools.read_page = AsyncMock(
            return_value={"success": True, "snapshot": "- button 'Submit'", "url": "http://x", "title": "T"}
        )
        mock_tools.execute = AsyncMock(
            side_effect=[
                {"success": True, "url": "http://x/invoices"},
                {"success": False, "error": "Target page, context or browser has been closed", "session_lost": True},
                {"success": True, "url": "http://x/invoices/new", "status": 200},
            ]
        )
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})
        new_page = MagicMock()
        new_page.is_closed = MagicMock(return_value=False)
        reconnect = AsyncMock(return_value=new_page)
        mock_groq = _groq_for_tool_calls(
            [("c1", "navigate", {"url": "/invoices"}), ("c2", "navigate", {"url": "/invoices/new"})]
        )
        agent = await self._make_agent(mock_tools, mock_groq, reconnect, events)
        result = await agent.run(goal="Process invoices")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(reconnect.await_count, 1)
        mock_tools.set_page.assert_called_once_with(new_page)
        types = [e["type"] for e in events]
        self.assertIn("session_lost", types)
        self.assertIn("session_reattached", types)
        # The recovered safe-tool replay must not surface as a generic step_failed.
        failed_navigates = [e for e in events if e["type"] == "step_failed" and e.get("action") == "navigate"]
        self.assertEqual(failed_navigates, [])

    async def test_transient_reattach_failure_then_success_resumes(self):
        events = []
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.page = MagicMock()
        mock_tools.page.is_closed = MagicMock(return_value=False)
        mock_tools.set_run_id = MagicMock()
        mock_tools.set_page = MagicMock()
        mock_tools.read_page = AsyncMock(
            return_value={"success": True, "snapshot": "- button 'Submit'", "url": "http://x", "title": "T"}
        )
        mock_tools.execute = AsyncMock(
            side_effect=[
                {"success": False, "error": "Target page, context or browser has been closed", "session_lost": True},
                {"success": True, "url": "http://x/invoices/new", "status": 200},
            ]
        )
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})
        new_page = MagicMock()
        new_page.is_closed = MagicMock(return_value=False)
        reconnect = AsyncMock(side_effect=[Exception("transient CDP failure"), new_page])
        mock_groq = _groq_for_tool_calls(
            [("c1", "navigate", {"url": "/invoices/new"})]
        )
        agent = await self._make_agent(mock_tools, mock_groq, reconnect, events)
        result = await agent.run(goal="Process invoices")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(reconnect.await_count, 2)
        types = [e["type"] for e in events]
        self.assertIn("session_reattached", types)
        terminal = [e for e in events if e["type"] == "session_lost" and e.get("terminal") is True]
        self.assertEqual(terminal, [])

    async def test_click_session_loss_does_not_replay(self):
        events = []
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.page = MagicMock()
        mock_tools.page.is_closed = MagicMock(return_value=False)
        mock_tools.set_run_id = MagicMock()
        mock_tools.set_page = MagicMock()
        mock_tools.read_page = AsyncMock(
            return_value={"success": True, "snapshot": "- button 'Submit'", "url": "http://x", "title": "T"}
        )
        click_args = {"selector": "Submit"}
        mock_tools.execute = AsyncMock(
            return_value={"success": False, "error": "Target page, context or browser has been closed", "session_lost": True}
        )
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True, "screenshot_b64": ""})
        new_page = MagicMock()
        new_page.is_closed = MagicMock(return_value=False)
        reconnect = AsyncMock(return_value=new_page)
        mock_groq = _groq_for_tool_calls(
            [("c1", "click", click_args)]
        )
        agent = await self._make_agent(mock_tools, mock_groq, reconnect, events, max_iterations=3)
        result = await agent.run(goal="Process invoices")
        self.assertEqual(reconnect.await_count, 1)
        mock_tools.set_page.assert_called_once_with(new_page)
        # Non-idempotent click must not be redispatched after reattach.
        self.assertEqual(mock_tools.execute.await_count, 1)
        dispatched_args = mock_tools.execute.await_args_list[0].args[1]
        self.assertEqual(dispatched_args, click_args)
        # Original session-loss result preserved; terminal abort bypassed after reattach.
        terminal = [e for e in events if e["type"] == "session_lost" and e.get("terminal") is True]
        self.assertEqual(terminal, [])
        self.assertNotEqual(result["status"], "session_lost")
        # Outcome-unknown, not failure: step_unknown emitted, no step_failed, no failure screenshot.
        unknown = [e for e in events if e["type"] == "step_unknown" and e.get("action") == "click"]
        self.assertEqual(len(unknown), 1)
        self.assertTrue(unknown[0].get("outcome_unknown"))
        failed_clicks = [e for e in events if e["type"] == "step_failed" and e.get("action") == "click"]
        self.assertEqual(failed_clicks, [])
        self.assertEqual(mock_tools.take_screenshot.await_count, 0)

    async def test_observe_session_loss_reattaches_and_resumes(self):
        events = []
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.page = MagicMock()
        mock_tools.page.is_closed = MagicMock(return_value=False)
        mock_tools.set_run_id = MagicMock()
        mock_tools.set_page = MagicMock()
        mock_tools.read_page = AsyncMock(
            side_effect=[
                {"success": True, "snapshot": "- button 'A'", "url": "http://x", "title": "T"},
                {"success": False, "error": "Target page, context or browser has been closed", "session_lost": True},
                {"success": True, "snapshot": "- button 'B'", "url": "http://x", "title": "T"},
                {"success": True, "snapshot": "- button 'B'", "url": "http://x", "title": "T"},
            ]
        )
        mock_tools.execute = AsyncMock(return_value={"success": True, "url": "http://x"})
        mock_tools.take_screenshot = AsyncMock(return_value={"success": True})
        new_page = MagicMock()
        reconnect = AsyncMock(return_value=new_page)
        mock_groq = _groq_for_tool_calls([("c1", "navigate", {"url": "/invoices"})])
        agent = await self._make_agent(mock_tools, mock_groq, reconnect, events, max_iterations=5)
        result = await agent.run(goal="Process invoices")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(reconnect.await_count, 1)
        self.assertIn("session_lost", [e["type"] for e in events])

    async def test_reattach_cap_aborts_with_distinct_session_lost(self):
        events = []
        mock_tools = MagicMock(spec=PlaywrightTools)
        mock_tools.page = MagicMock()
        mock_tools.page.is_closed = MagicMock(return_value=False)
        mock_tools.set_run_id = MagicMock()
        mock_tools.set_page = MagicMock()
        mock_tools.read_page = AsyncMock(
            return_value={"success": True, "snapshot": "x", "url": "http://x", "title": "T"}
        )
        mock_tools.execute = AsyncMock(
            return_value={"success": False, "error": "Target page, context or browser has been closed", "session_lost": True}
        )
        mock_tools.take_screenshot = AsyncMock(return_value={"success": False})
        reconnect = AsyncMock(return_value=MagicMock())
        msg = MagicMock()
        tc = MagicMock()
        tc.id = "loop"
        tc.function.name = "navigate"
        tc.function.arguments = json.dumps({"url": "/invoices"})
        msg.tool_calls = [tc]
        msg.content = None
        mock_groq = MagicMock()
        mock_groq.chat = MagicMock(
            completions=MagicMock(
                create=AsyncMock(return_value=MagicMock(choices=[MagicMock(message=msg)]))
            )
        )
        agent = await self._make_agent(
            mock_tools, mock_groq, reconnect, events, max_reattaches=2, max_iterations=5
        )
        result = await agent.run(goal="Process invoices")
        self.assertEqual(result["status"], "session_lost")
        self.assertLessEqual(reconnect.await_count, 2)
        self.assertEqual(reconnect.await_count, 2)
        types = [e["type"] for e in events]
        self.assertIn("session_lost", types)
        terminal = [e for e in events if e["type"] == "session_lost" and e.get("terminal") is True]
        self.assertTrue(terminal)


if __name__ == "__main__":
    unittest.main()
