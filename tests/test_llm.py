import asyncio
import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from reltrade.agents.llm import NUDGE, LLMMerchant
from reltrade.engine import Engine
from reltrade.llm_client import AuthError, LLMClient
from reltrade.physics import LIGHT, Relativistic
from reltrade.prompts import render_observation, system_prompt, tools
from reltrade.world import Rules, make_world

pytestmark = pytest.mark.integration


def call(cid: str, name: str, **args: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def reply(*calls: dict[str, Any], content: str = "", reasoning: str = "") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = list(calls)
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {
        "choices": [{"message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        "_meta": {"cache_hit": False, "cost_usd": 0.001},
    }


class FakeClient:
    """按顺序吐出预设回复；记录每次请求时的消息快照。"""

    model = "fake"
    temperature = 1.0

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[list[dict[str, Any]]] = []

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tag: str
    ) -> dict[str, Any]:
        self.requests.append(json.loads(json.dumps(messages)))
        if not self.replies:
            return reply(call("w", "wait", years=19, reason="script over"))
        return self.replies.pop(0)


def run_single(
    replies: list[dict[str, Any]], tmp_path: Path, t_end: float = 3.0, max_rounds: int = 8
) -> tuple[Engine, LLMMerchant, FakeClient]:
    w = make_world(7, Rules(t_end=t_end))
    name = next(m for m, s in w.starts.items() if s == "Sol")
    client = FakeClient(replies)
    kin = Relativistic()

    class Idle:
        async def decide(self, ctx: Any) -> None:
            ctx.wait(19.0, "idle")

    agents: dict[str, Any] = {n: Idle() for n in w.merchant_names}
    agent = LLMMerchant(
        name,
        client,
        w,
        kin,
        LIGHT,
        tag="test",  # type: ignore[arg-type]
        transcript=tmp_path / "t.jsonl",
        max_rounds=max_rounds,
    )
    agents[name] = agent
    eng = Engine(w, agents, c_info=LIGHT, kin=kin)
    asyncio.run(eng.run())
    return eng, agent, client


def test_tool_calls_execute_in_order_and_reasoning_is_stripped(tmp_path: Path) -> None:
    replies = [
        reply(
            call("a", "buy", good="tech", quantity=10),
            call("b", "quote_trip", destination="Wolf 359", speeds=[0.9, 0.99]),
            reasoning="think 1",
        ),
        reply(
            call("c", "write_notebook", text="tech run"),
            call("d", "depart", destination="Wolf 359", speed=0.9, plan="sell tech"),
            call("e", "buy", good="ore", quantity=1),
            reasoning="think 2",
        ),
    ]
    eng, agent, client = run_single(replies, tmp_path, t_end=3.0)
    msgs = agent.messages
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs[0]["content"].startswith("Bought 10.00 tech")
    assert "Trip Sol → Wolf 359" in tool_msgs[1]["content"]
    assert tool_msgs[2]["content"].startswith("Notebook saved")
    assert tool_msgs[3]["content"].startswith("Departed for Wolf 359")
    assert "turn already ended" in tool_msgs[4]["content"]
    # 第二轮请求时，同一停靠内的推理仍在上下文里；停靠结束后被剥掉
    assert client.requests[1][-3]["reasoning_content"] == "think 1"
    assert all("reasoning_content" not in m for m in msgs)
    entry = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert entry["system"].startswith("You are")
    assert [m.get("reasoning_content") for m in entry["messages"] if m["role"] == "assistant"] == [
        "think 1",
        "think 2",
    ]
    assert agent.stats["tool_errors"] == 1
    assert eng.merchants[agent.name].notebook == "tech run"
    assert eng.merchants[agent.name].transit is not None


def test_text_only_reply_gets_nudged(tmp_path: Path) -> None:
    replies = [
        reply(content="Let me think about it."),
        reply(call("a", "wait", years=19, reason="patience")),
    ]
    _, agent, client = run_single(replies, tmp_path)
    assert client.requests[1][-1] == {"role": "user", "content": NUDGE}
    assert agent.stats["nudges"] == 1


def test_bad_arguments_and_actions_return_errors(tmp_path: Path) -> None:
    bad_json = {"id": "x", "type": "function", "function": {"name": "buy", "arguments": "{oops"}}
    replies = [
        reply(
            bad_json,
            call("y", "sell", good="med", quantity=5),
            call("z", "teleport", destination="Sirius"),
        ),
        reply(call("w", "wait", years=19, reason="ok")),
    ]
    _, agent, _ = run_single(replies, tmp_path)
    tool_msgs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert tool_msgs[0].startswith("ERROR: could not parse")
    assert "you hold only" in tool_msgs[1]
    assert "unknown tool" in tool_msgs[2]
    assert agent.stats["tool_errors"] == 3


def test_exhausted_rounds_force_a_wait(tmp_path: Path) -> None:
    replies = [
        reply(call(f"q{i}", "quote_trip", destination="Sirius", speeds=[0.5])) for i in range(3)
    ]
    eng, agent, _ = run_single(replies, tmp_path, max_rounds=3, t_end=1.5)
    assert agent.stats["turns_exhausted"] == 1
    assert any(r["type"] == "wait" and r["forced"] for r in eng.records)


def test_prompts_render() -> None:
    w = make_world(7)
    kin = Relativistic()
    sp = system_prompt(w, "Asel", kin, LIGHT)
    assert "trade in the same network: Bram, Chen, Dara, Emre, Faye." in sp
    assert "8.6 years old" in sp
    names = [t["function"]["name"] for t in tools(w, kin, "Asel")]
    assert names == [
        "buy",
        "sell",
        "quote_trip",
        "depart",
        "wait",
        "send_message",
        "write_notebook",
    ]


# ---------------- LLM 客户端 ----------------


def make_client(tmp_path: Path, handler: Any, monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_GO_SESSION", "test-session")

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("reltrade.llm_client.asyncio.sleep", no_sleep)
    return LLMClient(
        "deepseek-v4.1-flash", tmp_path / "cache", transport=httpx.MockTransport(handler)
    )


OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
    "usage": {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 400},
    },
}


def test_client_caches_raw_response_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req)
        assert req.headers["x-opencode-session"] == "test-session"
        return httpx.Response(200, json=OK_BODY, headers={"x-opencode-log-id": "abc"})

    client = make_client(tmp_path, handler, monkeypatch)
    msgs = [{"role": "user", "content": "hello"}]

    async def go() -> None:
        first = await client.chat(msgs, [], tag="r0")
        again = await client.chat(msgs, [], tag="r0")
        other = await client.chat(msgs, [], tag="r1")  # 另一次重复：独立采样
        assert first["_meta"]["cache_hit"] is False and again["_meta"]["cache_hit"] is True
        assert other["_meta"]["cache_hit"] is False
        await client.aclose()

    asyncio.run(go())
    assert len(hits) == 2
    files = list((tmp_path / "cache").rglob("*.json.gz"))
    assert len(files) == 2
    entry = json.loads(gzip.decompress(files[0].read_bytes()))
    assert json.loads(entry["raw"]) == OK_BODY and entry["log_id"] == "abc"
    assert client.cost_usd > 0


def test_client_retries_then_aborts_on_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(req.content)
        if body["messages"][0]["content"] == "auth":
            return httpx.Response(401, json={"error": "expired"})
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "slow down"}, headers={"retry-after": "1"})
        return httpx.Response(200, json=OK_BODY)

    client = make_client(tmp_path, handler, monkeypatch)

    async def go() -> None:
        data = await client.chat([{"role": "user", "content": "x"}], [], tag="t")
        assert data["choices"][0]["message"]["content"] == "hi"
        with pytest.raises(AuthError):
            await client.chat([{"role": "user", "content": "auth"}], [], tag="t")
        await client.aclose()

    asyncio.run(go())
    assert calls["n"] == 3


def test_observation_renders_ages_and_events() -> None:
    w = make_world(7)
    kin = Relativistic()

    class Probe:
        def __init__(self) -> None:
            self.text = ""

        async def decide(self, ctx: Any) -> None:
            if not self.text:
                self.text = render_observation(ctx.observe(), w, kin, LIGHT, 1)
            ctx.wait(19.0, "probe")

    probe = Probe()
    agents: dict[str, Any] = {n: Probe() for n in w.merchant_names}
    agents[w.merchant_names[0]] = probe
    asyncio.run(Engine(w, agents, c_info=LIGHT, kin=kin).run())
    assert "REPORT #1" in probe.text and "y old)" in probe.text
    assert "Before year 0" in probe.text
