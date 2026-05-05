from __future__ import annotations

import asyncio
import logging

import httpx

from agent.db import async_store_poll
from agent.polling.base import BasePoller
from agent.schemas.monitor import Monitor, DepositEarnScope

logger = logging.getLogger(__name__)

_JUP_BASE    = "https://api.jup.ag/lend/v1"
_KAMINO_BASE = "https://api.kamino.finance"

# Minimum USD TVL for a Kamino vault to be considered a real candidate.
_MIN_TVL_USD = 100_000.0

# How many top vaults (by shares issued) to fetch metrics for per discovery.
_KAMINO_CANDIDATE_LIMIT = 15

# Re-discover best vault every N polls (avoids hammering /kvaults/vaults).
_VAULT_CACHE_TTL = 10


# ---------------------------------------------------------------------------
# Jupiter Lend
# ---------------------------------------------------------------------------

class _JupiterFetcher:
    async def fetch(self, client: httpx.AsyncClient, token_mint: str) -> dict | None:
        resp = await client.get(f"{_JUP_BASE}/earn/tokens", timeout=15.0)
        resp.raise_for_status()
        for entry in resp.json():
            if entry.get("assetAddress") == token_mint:
                return entry
        logger.warning("Jupiter: token %s not found in /earn/tokens", token_mint)
        return None


# ---------------------------------------------------------------------------
# Kamino KVaults
# ---------------------------------------------------------------------------

class _KaminoFetcher:
    """
    Discovers the best Kamino vault for a given token mint by:
      1. Listing all vaults and filtering by token mint.
      2. Taking the top N by raw sharesIssued (proxy for TVL).
      3. Fetching metrics in parallel and filtering by minimum TVL.
      4. Returning the vault with the highest current net APY.

    Results are cached for _VAULT_CACHE_TTL poll cycles to reduce API load.
    """

    def __init__(self) -> None:
        # mint → (best_vault_address, poll_count_at_last_discovery)
        self._cache: dict[str, tuple[str, int]] = {}
        self._poll_count: dict[str, int] = {}

    async def fetch(
        self, client: httpx.AsyncClient, token_mint: str
    ) -> dict | None:
        self._poll_count[token_mint] = self._poll_count.get(token_mint, 0) + 1
        count = self._poll_count[token_mint]

        cached_addr, cached_at = self._cache.get(token_mint, (None, 0))
        if cached_addr and (count - cached_at) < _VAULT_CACHE_TTL:
            return await self._vault_metrics(client, cached_addr)

        # Re-discover
        best_addr = await self._discover_best_vault(client, token_mint)
        if best_addr is None:
            return None

        self._cache[token_mint] = (best_addr, count)
        return await self._vault_metrics(client, best_addr)

    async def _discover_best_vault(
        self, client: httpx.AsyncClient, token_mint: str
    ) -> str | None:
        resp = await client.get(f"{_KAMINO_BASE}/kvaults/vaults", timeout=20.0)
        resp.raise_for_status()
        vaults = resp.json()

        candidates = [
            v for v in vaults
            if v.get("state", {}).get("tokenMint") == token_mint
            and int(v.get("state", {}).get("sharesIssued", "0")) > 0
        ]
        candidates.sort(
            key=lambda v: int(v["state"].get("sharesIssued", "0")), reverse=True
        )
        candidates = candidates[:_KAMINO_CANDIDATE_LIMIT]

        if not candidates:
            logger.warning("Kamino: no vaults found for mint %s", token_mint)
            return None

        metrics_results = await asyncio.gather(
            *[self._vault_metrics(client, v["address"]) for v in candidates],
            return_exceptions=True,
        )

        best_addr: str | None = None
        best_net_apy: float = -1.0

        for v, result in zip(candidates, metrics_results):
            if isinstance(result, Exception):
                logger.debug("Kamino metrics error for %s: %s", v["address"], result)
                continue
            tvl = float(result.get("tokensInvestedUsd") or 0)
            if tvl < _MIN_TVL_USD:
                continue
            net_apy = self._net_apy(result)
            if net_apy > best_net_apy:
                best_net_apy = net_apy
                best_addr = v["address"]

        if best_addr is None:
            logger.warning(
                "Kamino: no vault for mint %s passes TVL threshold", token_mint
            )
        return best_addr

    async def _vault_metrics(
        self, client: httpx.AsyncClient, vault_addr: str
    ) -> dict:
        resp = await client.get(
            f"{_KAMINO_BASE}/kvaults/vaults/{vault_addr}/metrics", timeout=15.0
        )
        resp.raise_for_status()
        data = resp.json()
        data["_vault_address"] = vault_addr
        return data

    @staticmethod
    def _net_apy(metrics: dict) -> float:
        base       = float(metrics.get("apy") or 0)
        incentives = float(metrics.get("apyIncentives") or 0)
        farm       = float(metrics.get("apyFarmRewards") or 0)
        return base + incentives + farm


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

class DepositEarnPoller(BasePoller):
    """
    Polls Jupiter Lend and the best Kamino KVault for the monitored token.
    Persists each protocol's data to SQLite and returns a combined dict.

    Return shape:
        {
            "token_symbol": str,
            "token_mint":   str,
            "jupiter":      dict | None,
            "kamino":       dict | None,   # has "_vault_address" key injected
        }
    """

    def __init__(self) -> None:
        self._jup    = _JupiterFetcher()
        self._kamino = _KaminoFetcher()

    async def poll(self, monitor: Monitor) -> dict:
        scope: DepositEarnScope = monitor.scope

        async with httpx.AsyncClient() as client:
            jup_data, kamino_data = await asyncio.gather(
                self._jup.fetch(client, scope.token_mint),
                self._kamino.fetch(client, scope.token_mint),
                return_exceptions=False,
            )

        await self._persist(monitor.id, scope.token_symbol, jup_data, kamino_data)

        return {
            "token_symbol": scope.token_symbol,
            "token_mint":   scope.token_mint,
            "jupiter":      jup_data,
            "kamino":       kamino_data,
        }

    # ------------------------------------------------------------------

    async def _persist(
        self,
        monitor_id: str,
        token_symbol: str,
        jup: dict | None,
        kamino: dict | None,
    ) -> None:
        tasks = []

        if jup:
            supply_bps  = int(jup.get("supplyRate")  or 0)
            rewards_bps = int(jup.get("rewardsRate") or 0)
            total_bps   = int(jup.get("totalRate")   or 0)
            decimals    = jup.get("decimals", 6)
            raw_assets  = int(jup.get("totalAssets") or 0)
            price       = float((jup.get("asset") or {}).get("price") or 0)
            tvl_usd     = (raw_assets / 10 ** decimals) * price

            tasks.append(async_store_poll(
                monitor_id=monitor_id,
                token=token_symbol,
                protocol="jupiter",
                vault_addr=jup.get("address"),
                apy_base=supply_bps  / 10_000,
                apy_rewards=rewards_bps / 10_000,
                apy_net=total_bps   / 10_000,
                tvl_usd=tvl_usd,
                apy_7d=None,
                apy_30d=None,
                raw=jup,
            ))

        if kamino:
            base       = float(kamino.get("apy") or 0)
            incentives = float(kamino.get("apyIncentives") or 0)
            farm       = float(kamino.get("apyFarmRewards") or 0)
            net        = base + incentives + farm

            raw_7d  = kamino.get("apy7d")
            raw_30d = kamino.get("apy30d")

            tasks.append(async_store_poll(
                monitor_id=monitor_id,
                token=token_symbol,
                protocol="kamino",
                vault_addr=kamino.get("_vault_address"),
                apy_base=base,
                apy_rewards=incentives + farm,
                apy_net=net,
                tvl_usd=float(kamino.get("tokensInvestedUsd") or 0),
                apy_7d=float(raw_7d)  if raw_7d  else None,
                apy_30d=float(raw_30d) if raw_30d else None,
                raw={k: v for k, v in kamino.items() if not k.startswith("_")},
            ))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
