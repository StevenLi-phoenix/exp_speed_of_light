"""OpenAI 兼容的 chat completions 客户端（OpenCode Go），带磁盘缓存。

- 连接参数全部来自环境变量（由 `go.sh exec --` 注入）：OPENAI_BASE_URL、OPENAI_API_KEY、
  OPENCODE_GO_SESSION。代码和产物里不出现 key。
- 付费 API 的原始响应先落盘再解析：成功响应以 gzip JSON 写入 cache/llm/<hash>.json.gz，
  键 = sha256(请求体 + tag)。同一状态重跑命中缓存、免费且确定；tag 区分不同重复实验，
  避免两次重复拿到同一个采样。
- 401/403 视为 key 失效直接中止（不换 key、不兜底）；402 视为额度用完中止；
  429/5xx/网络错误指数退避重试。
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import random
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("reltrade.llm")

# deepseek-v4.1-flash 单价（$/1M token），来自 opencode.ai/v2/docs/console/go（2026-10-20）
PRICES = {
    "deepseek-v4.1-flash": {"off": (0.15, 0.003, 0.60), "peak": (0.30, 0.006, 1.20)},
    "deepseek-v4-pro": {"off": (0.66, 0.022, 1.98), "peak": (1.32, 0.044, 3.96)},
}


class LLMError(RuntimeError):
    pass


class AuthError(LLMError):
    """key 失效（401/403）：停下来向用户要新 key。"""


class QuotaError(LLMError):
    """额度用完（402）。"""


def is_peak(ts: datetime) -> bool:
    """高峰时段：周一至周五 UTC 01–04 点、06–10 点。"""
    return ts.weekday() < 5 and (1 <= ts.hour < 4 or 6 <= ts.hour < 10)


def cache_key(body: dict[str, Any], tag: str) -> str:
    blob = json.dumps({"tag": tag, "body": body}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class LLMClient:
    def __init__(
        self,
        model: str,
        cache_dir: Path,
        *,
        temperature: float = 1.0,
        max_tokens: int = 16384,
        extra_body: dict[str, Any] | None = None,
        concurrency: int = 8,
        timeout: float = 600.0,
        offline: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.cache_dir = cache_dir
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        self.offline = offline  # True：只读缓存，未命中即报错（用于确定性重放）
        self._sem = asyncio.Semaphore(concurrency)
        self._timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self.usage: Counter[str] = Counter()
        self.cost_usd = 0.0

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = {
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                "x-opencode-session": os.environ["OPENCODE_GO_SESSION"],
                "User-Agent": "exp-speed-of-light/0.1 (reltrade)",
                "Content-Type": "application/json",
            }
            self._http = httpx.AsyncClient(
                base_url=os.environ["OPENAI_BASE_URL"].rstrip("/"),
                headers=headers,
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json.gz"

    def _read_cache(self, path: Path) -> tuple[dict[str, Any], str | None] | None:
        """返回（解析后的响应，当初请求的时刻）。"""
        if not path.exists():
            return None
        try:
            entry = json.loads(gzip.decompress(path.read_bytes()))
            data = json.loads(entry["raw"])
            data["choices"][0]["message"]
            return data, entry.get("ts")
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            bad = path.with_suffix(".bad")
            path.rename(bad)
            log.warning("unparseable cache entry moved to %s; re-requesting", bad)
            return None

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tag: str
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **self.extra_body,
        }
        key = cache_key(body, tag)
        path = self._path(key)
        hit = self._read_cache(path)
        if hit is not None:
            cached, ts = hit
            self.usage["cache_hits"] += 1
            # 重放不花钱（self.cost_usd 不变），但带上当初的花费：整局花费与是否续跑无关
            cost = self._price(cached, ts) if ts else 0.0
            cached["_meta"] = {"cache_hit": True, "cost_usd": cost, "key": key}
            return cached
        if self.offline:
            raise LLMError(f"offline mode: cache miss for {tag} ({key[:12]})")
        async with self._sem:
            text, meta = await self._post(body, tag)
        # 原始响应先落盘（原子写），再解析
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"tag": tag, "key": key, **meta, "request": body, "raw": text}
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(entry, ensure_ascii=False).encode()))
        tmp.replace(path)
        try:
            data = json.loads(text)
            data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            path.rename(path.with_suffix(".bad"))
            raise LLMError(f"unparseable response for {tag}: {text[:300]}") from e
        cost = self._account(data, meta)
        data["_meta"] = {
            "cache_hit": False,
            "cost_usd": cost,
            "key": key,
            "latency_s": meta["latency_s"],
        }
        return data

    async def _post(self, body: dict[str, Any], tag: str) -> tuple[str, dict[str, Any]]:
        delay = 2.0
        for attempt in range(1, 9):
            t0 = time.monotonic()
            try:
                resp = await self._client().post("/chat/completions", json=body)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                log.warning(
                    "%s: %s (attempt %d), retrying in %.0fs", tag, type(e).__name__, attempt, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            latency = time.monotonic() - t0
            log_id = resp.headers.get("x-opencode-log-id", "")
            if resp.status_code == 200:
                return resp.text, {
                    "status": 200,
                    "latency_s": latency,
                    "log_id": log_id,
                    "ts": datetime.now(UTC).isoformat(),
                }
            snippet = resp.text[:400]
            if resp.status_code in (401, 403):
                raise AuthError(f"HTTP {resp.status_code} (log-id {log_id}): {snippet}")
            if resp.status_code == 402:
                raise QuotaError(f"HTTP 402 (log-id {log_id}): {snippet}")
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("retry-after") or delay)
                log.warning(
                    "%s: HTTP %d (attempt %d, log-id %s), retrying in %.0fs",
                    tag,
                    resp.status_code,
                    attempt,
                    log_id,
                    wait,
                )
                await asyncio.sleep(wait + random.random())
                delay = min(delay * 2, 60.0)
                continue
            raise LLMError(f"HTTP {resp.status_code} for {tag} (log-id {log_id}): {snippet}")
        raise LLMError(f"{tag}: giving up after repeated failures")

    def _account(self, data: dict[str, Any], meta: dict[str, Any]) -> float:
        u = data.get("usage") or {}
        prompt = int(u.get("prompt_tokens") or 0)
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        out = int(u.get("completion_tokens") or 0)
        reasoning = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        self.usage.update(
            {
                "requests": 1,
                "prompt_tokens": prompt,
                "cached_tokens": cached,
                "completion_tokens": out,
                "reasoning_tokens": reasoning,
            }
        )
        cost = self._price(data, meta["ts"])
        self.cost_usd += cost
        log.debug(
            "llm %s: %.1fs in=%d (cached %d) out=%d (reasoning %d)",
            self.model,
            meta["latency_s"],
            prompt,
            cached,
            out,
            reasoning,
        )
        return cost

    def _price(self, data: dict[str, Any], ts: str) -> float:
        """按响应里的 usage 和请求时刻（高峰 / 平时）估算美元花费；价目表里没有的模型记 0。"""
        price = PRICES.get(self.model)
        if not price:
            return 0.0
        u = data.get("usage") or {}
        prompt = int(u.get("prompt_tokens") or 0)
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        out = int(u.get("completion_tokens") or 0)
        p_in, p_cached, p_out = price["peak" if is_peak(datetime.fromisoformat(ts)) else "off"]
        return ((prompt - cached) * p_in + cached * p_cached + out * p_out) / 1e6
