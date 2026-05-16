"""
python_validation.py - Deterministic safety/R:R validator

Purpose:
    Validate Claude trade recommendations using objective Python rules.

    Does NOT call Claude.
    Does NOT execute trades.
    Does NOT place orders.
    Only returns a validation result.

Flow:
    market_snapshot.py builds facts
    claude_reviewer.py reviews setup quality
    python_validation.py validates risk/R:R/levels/safety
    decision_audit.py records everything
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class ValidationConfig:
    instrument: str = "XAU_USD"

    # Trade quality thresholds
    min_rr: float = 2.5
    min_stop_distance: float = 20.0
    max_stop_distance: float = 100.0
    stop_buffer: float = 2.0

    # Session safety
    allow_london: bool = True
    allow_new_york: bool = True
    allow_off_hours: bool = False

    # Extension safety
    block_extended: bool = True
    block_moderately_extended: bool = False


class PythonTradeValidator:
    """
    Final deterministic validator before any paper execution.

    This class is intentionally strict. Claude can suggest a setup, but Python
    decides whether the setup is mathematically valid.
    """

    def __init__(self, config: Optional[ValidationConfig] = None):
        self.config = config or ValidationConfig()
        self._apply_instrument_defaults()

    def validate(
        self,
        market_snapshot: Dict[str, Any],
        claude_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate a Claude trade decision against market facts.

        Returns a dict compatible with DecisionAuditor.
        """
        snapshot = market_snapshot or {}
        decision = claude_decision or {}

        instrument = str(snapshot.get("instrument") or self.config.instrument or "UNKNOWN")
        current_price = self._get_float(snapshot, "current_price")
        claude_action = self._clean_code(decision.get("decision", "NO_TRADE"))
        claude_setup = self._clean_code(decision.get("setup", "NONE"))
        trigger_direction = self._clean_code(snapshot.get("trigger_direction", "NONE"))
        market_state = self._clean_code(snapshot.get("market_state", "UNKNOWN"))
        session = self._clean_code(snapshot.get("session", "UNKNOWN"))
        extension_check = str(snapshot.get("extension_check", "") or "")

        base = {
            "instrument": instrument,
            "entry": current_price,
            "direction": trigger_direction,
            "stop": None,
            "target": None,
            "rr": None,
            "risk_reward": None,
            "stop_distance": None,
            "target_distance": None,
        }

        # Informational outcomes first. These are not Python blocks.
        if claude_action == "NO_TRADE":
            return self._info(
                base,
                reason_code="CLAUDE_DID_NOT_REQUEST_ENTRY",
                reason="Claude did not recommend immediate entry.",
            )

        if claude_action == "WAIT_PULLBACK":
            return self._info(
                base,
                reason_code="CLAUDE_REQUESTED_PULLBACK",
                reason="Claude recommended waiting for a pullback.",
            )

        # From here onward, Claude is asking to enter now.
        if claude_action != "ENTER_NOW":
            return self._block(
                base,
                reason_code="UNKNOWN_CLAUDE_ACTION",
                reason=f"Unknown Claude decision: {claude_action}",
            )

        if current_price is None or current_price <= 0:
            return self._block(
                base,
                reason_code="INVALID_CURRENT_PRICE",
                reason="Current price is missing or invalid.",
            )

        if market_state in {"INSUFFICIENT_DATA", "UNKNOWN"}:
            return self._block(
                base,
                reason_code="INVALID_MARKET_STATE",
                reason=f"Market state is not tradable: {market_state}",
            )

        if bool(snapshot.get("news_nearby", False)):
            return self._block(
                base,
                reason_code="NEWS_RISK",
                reason="Nearby news risk blocks immediate entry.",
            )

        if not self._session_allowed(session):
            return self._block(
                base,
                reason_code="SESSION_NOT_ALLOWED",
                reason=f"Trading session is not allowed by config: {session}",
            )

        if trigger_direction not in {"LONG", "SHORT"}:
            return self._block(
                base,
                reason_code="NO_TRIGGER_DIRECTION",
                reason="No confirmed LONG/SHORT trigger direction.",
            )

        if claude_setup != trigger_direction:
            return self._block(
                base,
                reason_code="SETUP_DIRECTION_MISMATCH",
                reason=f"Claude setup {claude_setup} does not match trigger direction {trigger_direction}.",
            )

        confirmation_error = self._check_confirmation(snapshot, trigger_direction)
        if confirmation_error:
            return self._block(base, **confirmation_error)

        extension_error = self._check_extension(extension_check)
        if extension_error:
            return self._block(base, **extension_error)

        levels = self._calculate_trade_levels(snapshot, trigger_direction)
        if not levels["ok"]:
            return self._block(base, reason_code=levels["reason_code"], reason=levels["reason"])

        entry = levels["entry"]
        stop = levels["stop"]
        target = levels["target"]
        stop_distance = levels["stop_distance"]
        target_distance = levels["target_distance"]
        rr = levels["rr"]

        base.update({
            "entry": entry,
            "stop": stop,
            "target": target,
            "stop_distance": stop_distance,
            "target_distance": target_distance,
            "rr": rr,
            "risk_reward": rr,
            "direction": trigger_direction,
        })

        if stop_distance < self.config.min_stop_distance:
            return self._block(
                base,
                reason_code="STOP_TOO_TIGHT",
                reason=(
                    f"Stop distance {stop_distance:.5g} is below minimum "
                    f"{self.config.min_stop_distance:.5g}."
                ),
            )

        if stop_distance > self.config.max_stop_distance:
            return self._block(
                base,
                reason_code="STOP_TOO_WIDE",
                reason=(
                    f"Stop distance {stop_distance:.5g} exceeds maximum "
                    f"{self.config.max_stop_distance:.5g}."
                ),
            )

        if rr < self.config.min_rr:
            return self._block(
                base,
                reason_code="RR_TOO_LOW",
                reason=(
                    f"R:R {rr:.2f} is below minimum {self.config.min_rr:.2f}. "
                    f"Entry={entry}, stop={stop}, target={target}."
                ),
            )

        return self._pass(
            base,
            reason_code="VALIDATION_PASSED",
            reason=(
                f"Validation passed. Direction={trigger_direction}, "
                f"entry={entry}, stop={stop}, target={target}, R:R={rr:.2f}."
            ),
        )

    # ── Core checks ───────────────────────────────────────────────────────────

    def _check_confirmation(
        self,
        snapshot: Dict[str, Any],
        direction: str,
    ) -> Optional[Dict[str, str]]:
        breakout_confirmed = bool(snapshot.get("breakout_confirmed", False))
        breakdown_confirmed = bool(snapshot.get("breakdown_confirmed", False))

        if direction == "LONG" and not breakout_confirmed:
            return {
                "reason_code": "LONG_BREAKOUT_NOT_CONFIRMED",
                "reason": "LONG setup requested but breakout_confirmed is false.",
            }

        if direction == "SHORT" and not breakdown_confirmed:
            return {
                "reason_code": "SHORT_BREAKDOWN_NOT_CONFIRMED",
                "reason": "SHORT setup requested but breakdown_confirmed is false.",
            }

        return None

    def _check_extension(self, extension_check: str) -> Optional[Dict[str, str]]:
        extension_text = extension_check.upper().strip()

        is_extended = extension_text.startswith("EXTENDED")
        is_moderately_extended = extension_text.startswith("MODERATELY EXTENDED")

        if is_extended and self.config.block_extended:
            return {
                "reason_code": "PRICE_EXTENDED",
                "reason": f"Price is too extended for immediate entry: {extension_check}",
            }

        if is_moderately_extended and self.config.block_moderately_extended:
            return {
                "reason_code": "PRICE_MODERATELY_EXTENDED",
                "reason": f"Price is moderately extended: {extension_check}",
            }

        return None

    def _calculate_trade_levels(
        self,
        snapshot: Dict[str, Any],
        direction: str,
    ) -> Dict[str, Any]:
        entry = self._get_float(snapshot, "current_price")
        breakout_level = self._get_float(snapshot, "breakout_level")
        next_resistance = self._get_float(snapshot, "next_resistance")
        nearest_support = self._get_float(snapshot, "nearest_support")

        if entry is None or entry <= 0:
            return self._level_error("INVALID_ENTRY", "Entry price is missing or invalid.")

        if breakout_level is None:
            return self._level_error("MISSING_TRIGGER_LEVEL", "Breakout/breakdown trigger level is missing.")

        if direction == "LONG":
            if next_resistance is None:
                return self._level_error("MISSING_TARGET", "LONG target resistance is missing.")

            if next_resistance <= entry:
                return self._level_error(
                    "INVALID_LONG_TARGET",
                    f"LONG target {next_resistance} is not above entry {entry}.",
                )

            support_ref = nearest_support if nearest_support is not None else breakout_level
            technical_stop = min(support_ref, breakout_level) - self.config.stop_buffer

            # Enforce minimum practical stop distance.
            min_distance_stop = entry - self.config.min_stop_distance
            stop = min(technical_stop, min_distance_stop)

            target = next_resistance
            stop_distance = entry - stop
            target_distance = target - entry

        elif direction == "SHORT":
            if nearest_support is None:
                return self._level_error("MISSING_TARGET", "SHORT target support is missing.")

            if nearest_support >= entry:
                return self._level_error(
                    "INVALID_SHORT_TARGET",
                    f"SHORT target {nearest_support} is not below entry {entry}.",
                )

            resistance_ref = next_resistance if next_resistance is not None else breakout_level
            technical_stop = max(resistance_ref, breakout_level) + self.config.stop_buffer

            # Enforce minimum practical stop distance.
            min_distance_stop = entry + self.config.min_stop_distance
            stop = max(technical_stop, min_distance_stop)

            target = nearest_support
            stop_distance = stop - entry
            target_distance = entry - target

        else:
            return self._level_error("INVALID_DIRECTION", f"Invalid direction: {direction}")

        if stop_distance <= 0:
            return self._level_error("INVALID_STOP_DISTANCE", "Stop distance is zero or negative.")

        if target_distance <= 0:
            return self._level_error("INVALID_TARGET_DISTANCE", "Target distance is zero or negative.")

        rr = target_distance / stop_distance

        return {
            "ok": True,
            "entry": round(entry, 5),
            "stop": round(stop, 5),
            "target": round(target, 5),
            "stop_distance": round(stop_distance, 5),
            "target_distance": round(target_distance, 5),
            "rr": round(rr, 4),
        }

    # ── Result helpers ────────────────────────────────────────────────────────

    def _pass(self, base: Dict[str, Any], reason_code: str, reason: str) -> Dict[str, Any]:
        result = dict(base)
        result.update({
            "passed": True,
            "reason_code": reason_code,
            "reason": reason,
            "block_reason_code": "",
        })
        return result

    def _block(self, base: Dict[str, Any], reason_code: str, reason: str) -> Dict[str, Any]:
        result = dict(base)
        result.update({
            "passed": False,
            "reason_code": reason_code,
            "block_reason_code": reason_code,
            "reason": reason,
        })
        return result

    def _info(self, base: Dict[str, Any], reason_code: str, reason: str) -> Dict[str, Any]:
        result = dict(base)
        result.update({
            "passed": None,
            "reason_code": reason_code,
            "reason": reason,
            "block_reason_code": "",
        })
        return result

    def _level_error(self, reason_code: str, reason: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "reason_code": reason_code,
            "reason": reason,
        }

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _apply_instrument_defaults(self) -> None:
        """
        Apply sensible defaults based on instrument.

        Important:
            Dataclass defaults are XAU_USD-style, so JPY instruments must be
            explicitly overridden unless the user supplied a custom config.
        """
        instrument = (self.config.instrument or "").upper()

        if instrument == "XAU_USD":
            # Keep XAU defaults unless user explicitly customized them.
            return

        elif "JPY" in instrument:
            # Conservative defaults for JPY FX pairs.
            self.config.min_rr = 2.0
            self.config.min_stop_distance = 0.20
            self.config.max_stop_distance = 1.50
            self.config.stop_buffer = 0.03

    def _session_allowed(self, session: str) -> bool:
        session = self._clean_code(session)

        if session == "LONDON":
            return self.config.allow_london

        if session in {"NEW_YORK", "NEWYORK", "NY"}:
            return self.config.allow_new_york

        if session in {"OFF_HOURS", "OFFHOURS"}:
            return self.config.allow_off_hours

        return False

    def _get_float(self, data: Dict[str, Any], key: str) -> Optional[float]:
        try:
            value = data.get(key)
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _clean_code(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip().upper().replace(" ", "_")


def validate_trade(
    market_snapshot: Dict[str, Any],
    claude_decision: Dict[str, Any],
    config: Optional[ValidationConfig] = None,
) -> Dict[str, Any]:
    """
    Convenience function for bot integration.
    """
    validator = PythonTradeValidator(config=config)
    return validator.validate(market_snapshot, claude_decision)


# ── TESTS ─────────────────────────────────────────────────────────────────────

def _base_snapshot() -> Dict[str, Any]:
    return {
        "instrument": "XAU_USD",
        "current_price": 4725.0,
        "session": "LONDON",
        "regime": "BULLISH",
        "daily_trend": "RANGING",
        "market_state": "BULLISH_TREND_IGNITION",
        "breakout_level": 4720.0,
        "next_resistance": 4785.0,
        "nearest_support": 4715.0,
        "level_name": "today_high",
        "trigger_direction": "LONG",
        "breakout_confirmed": True,
        "breakdown_confirmed": False,
        "candles_above_level": 3,
        "candles_below_level": 0,
        "consecutive_bullish_candles": 3,
        "consecutive_bearish_candles": 0,
        "ema_alignment": "bullish",
        "price_vs_ema50": "above",
        "extension_check": "OK - within 1.1x ATR of EMA9",
        "distance_from_entry": 5.0,
        "news_nearby": False,
    }


def _enter_long() -> Dict[str, Any]:
    return {
        "decision": "ENTER_NOW",
        "setup": "LONG",
        "confidence": "HIGH",
        "entry_style": "BREAKOUT",
        "reason": "Clean long breakout.",
        "risk_comment": "Validate with Python.",
        "is_late_chase": False,
        "needs_pullback": False,
    }


def _enter_short() -> Dict[str, Any]:
    return {
        "decision": "ENTER_NOW",
        "setup": "SHORT",
        "confidence": "HIGH",
        "entry_style": "BREAKOUT",
        "reason": "Clean short breakdown.",
        "risk_comment": "Validate with Python.",
        "is_late_chase": False,
        "needs_pullback": False,
    }


def _no_trade() -> Dict[str, Any]:
    return {
        "decision": "NO_TRADE",
        "setup": "NONE",
        "confidence": "LOW",
        "entry_style": "NONE",
        "reason": "No clean setup.",
        "risk_comment": "No trade.",
        "is_late_chase": False,
        "needs_pullback": False,
    }


def _assert(name: str, condition: bool, result: Dict[str, Any]) -> None:
    if not condition:
        raise AssertionError(f"{name} failed. Result: {result}")


def run_tests() -> None:
    import json

    print("=" * 80)
    print("TESTING PYTHON TRADE VALIDATOR")
    print("=" * 80)

    validator = PythonTradeValidator(ValidationConfig(instrument="XAU_USD"))

    tests = []

    # Test 1: clean long passes
    snap = _base_snapshot()
    tests.append(("Clean LONG passes", snap, _enter_long(), True, "VALIDATION_PASSED"))

    # Test 2: extended long blocks
    snap = _base_snapshot()
    snap["extension_check"] = "EXTENDED - 2.3x ATR from EMA9"
    tests.append(("Extended LONG blocks", snap, _enter_long(), False, "PRICE_EXTENDED"))

    # Test 3: low R:R blocks
    snap = _base_snapshot()
    snap["next_resistance"] = 4750.0
    tests.append(("Low RR blocks", snap, _enter_long(), False, "RR_TOO_LOW"))

    # Test 4: direction mismatch blocks
    snap = _base_snapshot()
    tests.append(("Direction mismatch blocks", snap, _enter_short(), False, "SETUP_DIRECTION_MISMATCH"))

    # Test 5: clean short passes
    snap = {
        "instrument": "XAU_USD",
        "current_price": 4693.0,
        "session": "NEW YORK",
        "regime": "BEARISH",
        "daily_trend": "RANGING",
        "market_state": "BEARISH_BREAKDOWN",
        "breakout_level": 4700.0,
        "next_resistance": 4705.0,
        "nearest_support": 4625.0,
        "level_name": "today_low",
        "trigger_direction": "SHORT",
        "breakout_confirmed": False,
        "breakdown_confirmed": True,
        "candles_above_level": 0,
        "candles_below_level": 3,
        "consecutive_bullish_candles": 0,
        "consecutive_bearish_candles": 3,
        "ema_alignment": "bearish",
        "price_vs_ema50": "below",
        "extension_check": "OK - within 1.1x ATR of EMA9",
        "distance_from_entry": 7.0,
        "news_nearby": False,
    }
    tests.append(("Clean SHORT passes", snap, _enter_short(), True, "VALIDATION_PASSED"))

    # Test 6: Claude NO_TRADE is informational, not blocked
    snap = _base_snapshot()
    tests.append(("Claude NO_TRADE informational", snap, _no_trade(), None, "CLAUDE_DID_NOT_REQUEST_ENTRY"))

    # Test 7: news blocks
    snap = _base_snapshot()
    snap["news_nearby"] = True
    tests.append(("News blocks", snap, _enter_long(), False, "NEWS_RISK"))

    # Test 8: off-hours blocks
    snap = _base_snapshot()
    snap["session"] = "OFF_HOURS"
    tests.append(("Off-hours blocks", snap, _enter_long(), False, "SESSION_NOT_ALLOWED"))

    for i, (name, snapshot, decision, expected_passed, expected_code) in enumerate(tests, start=1):
        print(f"\n[TEST {i}] {name}")
        print("-" * 80)

        result = validator.validate(snapshot, decision)
        print(json.dumps(result, indent=2))

        _assert(name, result["passed"] == expected_passed, result)
        _assert(name, result["reason_code"] == expected_code, result)

        if expected_passed is False:
            _assert(name, result["block_reason_code"] == expected_code, result)

        if expected_passed is None:
            _assert(name, result["block_reason_code"] == "", result)

        print(f"✅ {name}")

    print("\n" + "=" * 80)
    print("VALIDATOR TESTS COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    run_tests()
