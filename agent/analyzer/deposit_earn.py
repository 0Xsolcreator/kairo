from __future__ import annotations

from agent.analyzer.base import BaseAnalyzer, Decision
from agent.schemas.monitor import Monitor, DepositEarnScope

# APY delta below which the two protocols are considered equivalent.
_EQUAL_THRESHOLD = 0.001          # 0.1 percentage points (as decimal)

# Relative difference between current and 7-day APY that flags volatility.
_VOLATILITY_THRESHOLD = 0.20      # 20 %


def _bps_to_dec(bps: int) -> float:
    return bps / 10_000.0


def _pct(decimal: float) -> float:
    return decimal * 100.0


class DepositEarnAnalyzer(BaseAnalyzer):
    """
    Compares Jupiter Lend vs the best Kamino KVault for the monitored token.

    Signals:
      JUPITER — Jupiter net APY is meaningfully higher
      KAMINO  — Kamino net APY is meaningfully higher
      EQUAL   — difference is within 0.1 percentage points
      ERROR   — one or both protocols returned no data
    """

    def analyze(self, monitor: Monitor, raw: dict) -> Decision:
        scope: DepositEarnScope = monitor.scope
        jup    = raw.get("jupiter")
        kamino = raw.get("kamino")

        if jup is None and kamino is None:
            return Decision(
                monitor_id=monitor.id,
                monitor_type=monitor.type,
                signal="ERROR",
                reason="Both Jupiter and Kamino returned no data for this token.",
            )

        jup_apy    = self._jup_net_apy(jup)    if jup    else None
        kamino_apy = self._kamino_net_apy(kamino) if kamino else None

        signal, reason, meta = self._decide(scope, jup, kamino, jup_apy, kamino_apy)

        return Decision(
            monitor_id=monitor.id,
            monitor_type=monitor.type,
            signal=signal,
            reason=reason,
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # APY extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _jup_net_apy(jup: dict) -> float:
        return _bps_to_dec(int(jup.get("totalRate") or 0))

    @staticmethod
    def _kamino_net_apy(kamino: dict) -> float:
        return (
            float(kamino.get("apy") or 0)
            + float(kamino.get("apyIncentives") or 0)
            + float(kamino.get("apyFarmRewards") or 0)
        )

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    def _decide(
        self,
        scope: DepositEarnScope,
        jup: dict | None,
        kamino: dict | None,
        jup_apy: float | None,
        kamino_apy: float | None,
    ) -> tuple[str, str, dict]:
        meta: dict = {"token": scope.token_symbol}
        sym = scope.token_symbol

        # Build metadata
        if jup is not None and jup_apy is not None:
            decimals   = jup.get("decimals", 6)
            raw_assets = int(jup.get("totalAssets") or 0)
            price      = float((jup.get("asset") or {}).get("price") or 0)
            meta.update(
                jupiter_apy_pct=round(_pct(jup_apy), 4),
                jupiter_supply_apy_pct=round(_pct(_bps_to_dec(int(jup.get("supplyRate") or 0))), 4),
                jupiter_rewards_apy_pct=round(_pct(_bps_to_dec(int(jup.get("rewardsRate") or 0))), 4),
                jupiter_tvl_usd=round((raw_assets / 10 ** decimals) * price, 2),
            )

        if kamino is not None and kamino_apy is not None:
            apy7d_raw  = kamino.get("apy7d")
            apy30d_raw = kamino.get("apy30d")
            apy7d  = float(apy7d_raw)  if apy7d_raw  else None
            apy30d = float(apy30d_raw) if apy30d_raw else None

            volatile = (
                apy7d is not None
                and kamino_apy > 0
                and abs(kamino_apy - apy7d) / kamino_apy > _VOLATILITY_THRESHOLD
            )

            meta.update(
                kamino_apy_pct=round(_pct(kamino_apy), 4),
                kamino_base_apy_pct=round(_pct(float(kamino.get("apy") or 0)), 4),
                kamino_rewards_apy_pct=round(
                    _pct(
                        float(kamino.get("apyIncentives") or 0)
                        + float(kamino.get("apyFarmRewards") or 0)
                    ),
                    4,
                ),
                kamino_tvl_usd=round(float(kamino.get("tokensInvestedUsd") or 0), 2),
                kamino_vault=kamino.get("_vault_address", ""),
                kamino_yield_volatile=volatile,
            )
            if apy7d is not None:
                meta["kamino_apy_7d_pct"] = round(_pct(apy7d), 4)
            if apy30d is not None:
                meta["kamino_apy_30d_pct"] = round(_pct(apy30d), 4)

        # Only one protocol available
        if jup_apy is None:
            return (
                "KAMINO",
                f"Jupiter has no data for {sym} — Kamino: {_pct(kamino_apy):.2f}% APY.",
                meta,
            )
        if kamino_apy is None:
            return (
                "JUPITER",
                f"Kamino has no data for {sym} — Jupiter: {_pct(jup_apy):.2f}% APY.",
                meta,
            )

        # Both available — compare
        delta = jup_apy - kamino_apy   # positive → Jupiter is better

        if abs(delta) < _EQUAL_THRESHOLD:
            note = ""
            if meta.get("kamino_yield_volatile"):
                note = " Kamino yield has been volatile (7d avg differs >20%)."
            return (
                "EQUAL",
                (
                    f"Near-equal yields for {sym}: "
                    f"Jupiter {_pct(jup_apy):.2f}% vs Kamino {_pct(kamino_apy):.2f}%.{note}"
                ),
                meta,
            )

        if delta > 0:
            return (
                "JUPITER",
                (
                    f"Jupiter leads for {sym}: {_pct(jup_apy):.2f}% "
                    f"vs Kamino {_pct(kamino_apy):.2f}% "
                    f"(+{_pct(delta):.2f}% advantage)."
                ),
                meta,
            )

        volatile_note = ""
        if meta.get("kamino_yield_volatile"):
            volatile_note = " Note: Kamino yield has been volatile (7d avg differs >20%)."
        return (
            "KAMINO",
            (
                f"Kamino leads for {sym}: {_pct(kamino_apy):.2f}% "
                f"vs Jupiter {_pct(jup_apy):.2f}% "
                f"(+{_pct(-delta):.2f}% advantage).{volatile_note}"
            ),
            meta,
        )
