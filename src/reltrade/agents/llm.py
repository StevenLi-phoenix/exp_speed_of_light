"""LLM 商人：每个商人一份独立、持续增长的对话；只在停靠时决策，只通过 tool use 行动。

每次停靠：追加一条报告（user）→ 循环调用模型、执行它给出的 tool calls（按顺序）、回填结果，
直到它 depart 或 wait，或用完回合数（之后引擎强制等待一年）。
同一次停靠内保留模型的 reasoning_content（DeepSeek thinking + tool call 的推荐做法），
停靠结束后从上下文里剥掉以节省 token；完整内容（含推理）另写进 transcript 供事后回看。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from reltrade.engine import ActionError, DecisionContext
from reltrade.llm_client import LLMClient, LLMError
from reltrade.physics import Kinematics
from reltrade.prompts import render_observation, render_result, system_prompt, tools
from reltrade.world import ENERGY, World

NUDGE = (
    "Your reply contained no tool call. Act only through the tools; end your turn with "
    "depart or wait."
)


class LLMMerchant:
    def __init__(
        self,
        name: str,
        client: LLMClient,
        world: World,
        kin: Kinematics,
        c_info: float,
        tag: str,
        transcript: Path | None = None,
        max_rounds: int = 8,
        logger: logging.Logger | None = None,
    ) -> None:
        self.name, self.client, self.world, self.kin, self.c_info = name, client, world, kin, c_info
        self.tag = tag
        self.transcript = transcript
        self.max_rounds = max_rounds
        self.log = logger or logging.getLogger("reltrade.llm")
        self.tools = tools(world, kin, name)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(world, name, kin, c_info)}
        ]
        self.reports = 0
        self.stats: dict[str, float] = {
            "calls": 0,
            "cache_hits": 0,
            "cost_usd": 0.0,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "tool_calls": 0,
            "tool_errors": 0,
            "nudges": 0,
            "turns_exhausted": 0,
        }

    async def decide(self, ctx: DecisionContext) -> None:
        self.reports += 1
        obs = ctx.observe()
        start = len(self.messages)
        self.messages.append(
            {
                "role": "user",
                "content": render_observation(obs, self.world, self.kin, self.c_info, self.reports),
            }
        )
        rounds = 0
        while not ctx.done and rounds < self.max_rounds:
            rounds += 1
            data = await self._chat(start)
            self._account(data)
            choice = data["choices"][0]
            msg = choice["message"]
            calls = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["function"]["name"],
                        "arguments": c["function"].get("arguments") or "{}",
                    },
                }
                for c in (msg.get("tool_calls") or [])
            ]
            assistant: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                assistant["tool_calls"] = calls
            if msg.get("reasoning_content"):
                assistant["reasoning_content"] = msg["reasoning_content"]
            self.messages.append(assistant)
            if not calls:
                self.stats["nudges"] += 1
                cut = (
                    " (Your previous reply was cut off.)"
                    if choice.get("finish_reason") == "length"
                    else ""
                )
                self.messages.append({"role": "user", "content": NUDGE + cut})
                continue
            for call in calls:
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": self._dispatch(ctx, call),
                    }
                )
        if not ctx.done:
            self.stats["turns_exhausted"] += 1
            self.log.warning(
                "%s used %d rounds without ending the turn at y%.2f", self.name, rounds, ctx.t
            )
        self._write_transcript(ctx, start, rounds)
        for m in self.messages[start:]:
            m.pop("reasoning_content", None)

    async def _chat(self, start: int) -> dict[str, Any]:
        tag = f"{self.tag}/{self.name}"
        try:
            return await self.client.chat(self.messages, self.tools, tag=tag)
        except LLMError as e:
            # 模型某一步没有思考时，响应里没有 reasoning_content；有的上游会因此拒绝同一停靠内
            # 之后的请求（HTTP 400，时有时无）。只在被拒时给缺字段的 assistant 消息补空串再试一次：
            # 没被拒的请求体保持原样，已缓存的对话照旧逐字节重放。
            missing = [
                m
                for m in self.messages[start:]
                if m["role"] == "assistant" and "reasoning_content" not in m
            ]
            if "reasoning_content" not in str(e) or not missing:
                raise
            for m in missing:
                m["reasoning_content"] = ""
            self.log.warning(
                "%s: upstream wants reasoning_content back; retrying with %d empty one(s)",
                tag,
                len(missing),
            )
            return await self.client.chat(self.messages, self.tools, tag=tag)

    def _account(self, data: dict[str, Any]) -> None:
        meta = data.get("_meta", {})
        self.stats["calls"] += 1
        if meta.get("cache_hit"):
            self.stats["cache_hits"] += 1
        self.stats["cost_usd"] += meta.get("cost_usd", 0.0)
        u = data.get("usage") or {}
        self.stats["prompt_tokens"] += u.get("prompt_tokens") or 0
        self.stats["cached_tokens"] += (u.get("prompt_tokens_details") or {}).get(
            "cached_tokens"
        ) or 0
        self.stats["completion_tokens"] += u.get("completion_tokens") or 0
        self.stats["reasoning_tokens"] += (u.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        ) or 0

    def _dispatch(self, ctx: DecisionContext, call: dict[str, Any]) -> str:
        self.stats["tool_calls"] += 1
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"])
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            self.stats["tool_errors"] += 1
            return f"ERROR: could not parse arguments for {name}: {e}"
        try:
            if name == "buy":
                res = ctx.buy(args.get("good", ""), args.get("quantity"))
            elif name == "sell":
                res = ctx.sell(args.get("good", ""), args.get("quantity"))
            elif name == "quote_trip":
                dest = args.get("destination", "")
                speeds = args.get("speeds") or []
                if not isinstance(speeds, list):
                    speeds = [speeds]
                quotes = ctx.quote(dest, speeds)
                energy = ctx.local_board().quotes[ENERGY].ask
                dist = self.world.distance(ctx.station, dest)
                res = {
                    "header": (
                        f"Trip {ctx.station} → {dest} ({dist:.2f} ly), current mass "
                        f"{ctx.mass():.3f} M, energy ask here {energy:.2f}¢, cash "
                        f"{ctx.m.cash:,.0f}¢:"
                    ),
                    "quotes": quotes,
                }
            elif name == "depart":
                res = ctx.depart(
                    args.get("destination", ""), args.get("speed"), str(args.get("plan", ""))
                )
            elif name == "wait":
                res = ctx.wait(args.get("years"), str(args.get("reason", "")))
            elif name == "send_message":
                res = ctx.send_message(str(args.get("to", "")), str(args.get("text", "")))
            elif name == "write_notebook":
                res = ctx.write_notebook(str(args.get("text", "")))
            else:
                raise ActionError(f"unknown tool {name!r}")
        except ActionError as e:
            self.stats["tool_errors"] += 1
            return f"ERROR: {e}"
        return render_result(name, res, self.kin, ctx.station)

    def _write_transcript(self, ctx: DecisionContext, start: int, rounds: int) -> None:
        if self.transcript is None:
            return
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "report": self.reports,
            "t": ctx.t,
            "station": ctx.station,
            "rounds": rounds,
            "outcome": ctx.outcome,
            "forced": not ctx.done,
            "messages": self.messages[start:],
        }
        if self.reports == 1:
            entry["system"] = self.messages[0]["content"]
            entry["tools"] = self.tools
        with self.transcript.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
