cd /home/ec2-user/moneymaker

cat > pipeline_test.py <<'PY'
"""
pipeline_test.py - Full wiring test for the AI-assisted trading brain

Purpose:
    Test that the standalone modules talk to each other correctly before
    integrating anything into gold_monitor.py or eurjpy_monitor.py.

Pipeline:
    fake candles
    -> market_snapshot.py
    -> claude_reviewer.py or mock Claude
    -> Python validation stub
    -> decision_audit.py
    -> SQLite audit checks

Default:
    Uses mock Claude for deterministic testing.

Optional:
    Use real Claude with:
        python3 pipeline_test.py --real-claude
"""

import argparse
import inspect
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from market_snapshot import MarketSnapshotBuilder
from decision_audit import DecisionAuditor


# ──────────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────────

def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}. Expected {expected!r}, got {actual!r}")


def print_header(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def print_section(title: str) -> None:
    print("\n" + title)
    print("-" * 80)


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def clean_test_dir(log_dir: Path, keep_existing: bool = False) -> None:
    if log_dir.exists() and not keep_existing:
        shutil.rmtree(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Fake candle generators
# ──────────────────────────────────────────────────────────────────────────────

def generate_bullish_breakout_candles() -> List[Dict[str, Any]]:
    """
    Build fake XAU/USD candles:
        First 50 candles: range between 4700 and 4720
        Last 3 candles: break and close above 4720

    Expected snapshot:
        breakout_level = 4720
        trigger_direction = LONG
        breakout_confirmed = True
    """
    candles = []

    # Phase 1: range below 4720
    for i in range(50):
        base = 4710
        noise = ((i * 37) % 20) - 10

        open_price = base + noise
        close_price = open_price + ((i % 3) - 1)
        high_price = min(max(open_price, close_price) + 2, 4720)
        low_price = max(min(open_price, close_price) - 2, 4700)

        candles.append({
            "mid": {
                "o": str(open_price),
                "h": str(high_price),
                "l": str(low_price),
                "c": str(close_price),
            },
            "time": f"2026-05-15T{7 + (i // 12):02d}:{(i % 12) * 5:02d}:00Z",
        })

    # Phase 2: breakout sequence
    breakout_candles = [
        {"open": 4718, "high": 4723, "low": 4717, "close": 4722},
        {"open": 4722, "high": 4726, "low": 4721, "close": 4725},
        {"open": 4725, "high": 4728, "low": 4724, "close": 4727},
    ]

    for i, prices in enumerate(breakout_candles):
        candles.append({
            "mid": {
                "o": str(prices["open"]),
                "h": str(prices["high"]),
                "l": str(prices["low"]),
                "c": str(prices["close"]),
            },
            "time": f"2026-05-15T11:{15 + i * 5:02d}:00Z",
        })

    return candles


def generate_bearish_breakdown_candles() -> List[Dict[str, Any]]:
    """
    Build fake XAU/USD candles:
        First 50 candles: range between 4700 and 4720
        Last 3 candles: break and close below 4700

    Expected snapshot:
        breakout_level = 4700
        trigger_direction = SHORT
        breakdown_confirmed = True
    """
    candles = []

    # Phase 1: range above 4700
    for i in range(50):
        base = 4710
        noise = ((i * 29) % 20) - 10

        open_price = base + noise
        close_price = open_price - ((i % 3) - 1)
        high_price = min(max(open_price, close_price) + 2, 4720)
        low_price = max(min(open_price, close_price) - 2, 4700)

        candles.append({
            "mid": {
                "o": str(open_price),
                "h": str(high_price),
                "l": str(low_price),
                "c": str(close_price),
            },
            "time": f"2026-05-15T{7 + (i // 12):02d}:{(i % 12) * 5:02d}:00Z",
        })

    # Phase 2: breakdown sequence
    breakdown_candles = [
        {"open": 4702, "high": 4703, "low": 4697, "close": 4698},
        {"open": 4698, "high": 4699, "low": 4694, "close": 4695},
        {"open": 4695, "high": 4696, "low": 4692, "close": 4693},
    ]

    for i, prices in enumerate(breakdown_candles):
        candles.append({
            "mid": {
                "o": str(prices["open"]),
                "h": str(prices["high"]),
                "l": str(prices["low"]),
                "c": str(prices["close"]),
            },
            "time": f"2026-05-15T14:{15 + i * 5:02d}:00Z",
        })

    return candles


def generate_insufficient_candles() -> List[Dict[str, Any]]:
    """Only 10 candles, intentionally insufficient."""
    candles = []

    for i in range(10):
        price = 4710 + i * 0.2
        candles.append({
            "mid": {
                "o": str(price),
                "h": str(price + 1),
                "l": str(price - 1),
                "c": str(price + 0.5),
            },
            "time": f"2026-05-15T07:{i * 5:02d}:00Z",
        })

    return candles


# ──────────────────────────────────────────────────────────────────────────────
# Claude layer
# ──────────────────────────────────────────────────────────────────────────────

def mock_claude_review(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic fake Claude decision.

    This lets us test the pipeline wiring without spending API calls.
    """
    market_state = snapshot.get("market_state", "")
    trigger_direction = snapshot.get("trigger_direction", "NONE")
    extension_check = snapshot.get("extension_check", "")

    if market_state == "INSUFFICIENT_DATA":
        return {
            "decision": "NO_TRADE",
            "setup": "NONE",
            "confidence": "LOW",
            "entry_style": "NONE",
            "reason": "Insufficient candle data for reliable setup review.",
            "risk_comment": "Need at least 50 candles before considering trades.",
            "is_late_chase": False,
            "needs_pullback": False,
        }

    if trigger_direction == "LONG" and snapshot.get("breakout_confirmed"):
        return {
            "decision": "ENTER_NOW",
            "setup": "LONG",
            "confidence": "MEDIUM" if extension_check.startswith("EXTENDED") else "HIGH",
            "entry_style": "BREAKOUT",
            "reason": "Mock Claude: bullish breakout confirmed by Python facts.",
            "risk_comment": "Python must validate extension, stop distance, and R:R before execution.",
            "is_late_chase": extension_check.startswith("EXTENDED"),
            "needs_pullback": extension_check.startswith("EXTENDED"),
        }

    if trigger_direction == "SHORT" and snapshot.get("breakdown_confirmed"):
        return {
            "decision": "ENTER_NOW",
            "setup": "SHORT",
            "confidence": "MEDIUM" if extension_check.startswith("EXTENDED") else "HIGH",
            "entry_style": "BREAKOUT",
            "reason": "Mock Claude: bearish breakdown confirmed by Python facts.",
            "risk_comment": "Python must validate extension, stop distance, and R:R before execution.",
            "is_late_chase": extension_check.startswith("EXTENDED"),
            "needs_pullback": extension_check.startswith("EXTENDED"),
        }

    return {
        "decision": "NO_TRADE",
        "setup": "NONE",
        "confidence": "LOW",
        "entry_style": "NONE",
        "reason": "Mock Claude: no confirmed trigger direction.",
        "risk_comment": "No actionable setup.",
        "is_late_chase": False,
        "needs_pullback": False,
    }


def real_claude_review(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Optional real Claude review.

    Requires claude_reviewer.py and valid Anthropic/API environment.
    """
    from claude_reviewer import ClaudeReviewer

    reviewer = ClaudeReviewer()
    return reviewer.review_setup(snapshot)


def get_claude_decision(snapshot: Dict[str, Any], use_real_claude: bool) -> Dict[str, Any]:
    if use_real_claude:
        return real_claude_review(snapshot)

    return mock_claude_review(snapshot)


# ──────────────────────────────────────────────────────────────────────────────
# Python validation stub
# ──────────────────────────────────────────────────────────────────────────────

def python_validation_stub(
    snapshot: Dict[str, Any],
    claude_decision: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Temporary validation layer.

    This is NOT the final risk engine.
    Later this becomes python_validation.py.

    Current checks:
        - Claude must say ENTER_NOW
        - setup must match trigger direction
        - extension blocks entry
        - R:R and stop distance are placeholder values
    """
    claude_action = claude_decision.get("decision", "NO_TRADE")
    claude_setup = claude_decision.get("setup", "NONE")
    trigger_direction = snapshot.get("trigger_direction", "NONE")
    extension_check = snapshot.get("extension_check", "")
    distance_from_entry = float(snapshot.get("distance_from_entry") or 0)

    if claude_action != "ENTER_NOW":
        return {
            "passed": None,
            "reason_code": "CLAUDE_DID_NOT_REQUEST_ENTRY",
            "reason": "Claude did not recommend immediate entry.",
            "rr": None,
            "stop_distance": None,
        }

    if trigger_direction == "NONE":
        return {
            "passed": False,
            "reason_code": "NO_TRIGGER_DIRECTION",
            "block_reason_code": "NO_TRIGGER_DIRECTION",
            "reason": "No confirmed LONG/SHORT trigger direction.",
            "rr": None,
            "stop_distance": None,
        }

    if claude_setup != trigger_direction:
        return {
            "passed": False,
            "reason_code": "SETUP_DIRECTION_MISMATCH",
            "block_reason_code": "SETUP_DIRECTION_MISMATCH",
            "reason": f"Claude setup {claude_setup} does not match trigger direction {trigger_direction}.",
            "rr": None,
            "stop_distance": None,
        }

    if extension_check.startswith("EXTENDED"):
        return {
            "passed": False,
            "reason_code": "PRICE_EXTENDED",
            "block_reason_code": "PRICE_EXTENDED",
            "reason": f"Price is too extended for immediate entry: {extension_check}",
            "rr": 1.4,
            "stop_distance": max(20.0, distance_from_entry + 15.0),
        }

    return {
        "passed": True,
        "reason_code": "VALIDATION_PASSED",
        "reason": "Placeholder validation passed.",
        "rr": 2.8,
        "stop_distance": max(20.0, distance_from_entry + 15.0),
    }


def decide_final_action(
    claude_decision: Dict[str, Any],
    python_validation: Dict[str, Any],
) -> Tuple[str, str, str]:
    """
    Convert Claude + Python validation into final bot action.
    """
    claude_action = claude_decision.get("decision", "NO_TRADE")
    validation_passed = python_validation.get("passed")

    if claude_action == "NO_TRADE":
        return (
            "NO_TRADE",
            claude_decision.get("reason", "Claude recommended no trade."),
            python_validation.get("reason_code", "CLAUDE_NO_TRADE"),
        )

    if claude_action == "WAIT_PULLBACK":
        return (
            "WAIT_PULLBACK",
            claude_decision.get("reason", "Claude recommended waiting for pullback."),
            python_validation.get("reason_code", "WAIT_PULLBACK"),
        )

    if claude_action == "ENTER_NOW" and validation_passed is True:
        return (
            "ENTER_NOW",
            python_validation.get("reason", "Python validation passed."),
            python_validation.get("reason_code", "VALIDATION_PASSED"),
        )

    if claude_action == "ENTER_NOW" and validation_passed is False:
        return (
            "BLOCKED",
            python_validation.get("reason", "Python validation blocked the trade."),
            python_validation.get("block_reason_code")
            or python_validation.get("reason_code")
            or "PYTHON_BLOCKED",
        )

    return (
        "NO_TRADE",
        "Unrecognized action combination. Defaulting to no trade.",
        "SAFE_DEFAULT_NO_TRADE",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Auditor compatibility wrapper
# ──────────────────────────────────────────────────────────────────────────────

def log_decision_compat(
    auditor: DecisionAuditor,
    market_snapshot: Dict[str, Any],
    claude_decision: Dict[str, Any],
    python_validation: Dict[str, Any],
    final_action: str,
    final_reason: str,
    final_reason_code: str,
    source: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Call DecisionAuditor.log_decision safely even if the exact parameter names
    change slightly during development.
    """
    metadata = metadata or {}

    candidates = {
        "market_snapshot": market_snapshot,
        "snapshot": market_snapshot,
        "claude_decision": claude_decision,
        "review_decision": claude_decision,
        "python_validation": python_validation,
        "validation": python_validation,
        "final_action": final_action,
        "final_reason": final_reason,
        "final_reason_code": final_reason_code,
        "source": source,
        "metadata": metadata,
    }

    sig = inspect.signature(auditor.log_decision)
    params = sig.parameters

    accepts_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in params.values()
    )

    if accepts_kwargs:
        return auditor.log_decision(
            market_snapshot=market_snapshot,
            claude_decision=claude_decision,
            python_validation=python_validation,
            final_action=final_action,
            final_reason=final_reason,
            final_reason_code=final_reason_code,
            source=source,
            metadata=metadata,
        )

    kwargs = {
        name: value
        for name, value in candidates.items()
        if name in params
    }

    try:
        return auditor.log_decision(**kwargs)
    except TypeError:
        # Fallback for a positional-only/simple signature.
        return auditor.log_decision(
            market_snapshot,
            claude_decision,
            python_validation,
            final_action,
            final_reason,
            final_reason_code,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Scenario runner
# ──────────────────────────────────────────────────────────────────────────────

def run_scenario(
    name: str,
    candles: List[Dict[str, Any]],
    current_time: datetime,
    expected: Dict[str, Any],
    auditor: DecisionAuditor,
    use_real_claude: bool,
) -> Dict[str, Any]:
    print_section(name)

    builder = MarketSnapshotBuilder(instrument="XAU_USD")
    snapshot = builder.build_snapshot(
        candles_5m=candles,
        candles_daily=None,
        current_time=current_time,
    )

    print("Snapshot:")
    print(pretty(snapshot))

    # Strict snapshot checks
    for key, expected_value in expected.items():
        if callable(expected_value):
            assert_true(
                expected_value(snapshot.get(key)),
                f"{name}: expected condition failed for {key}. Actual={snapshot.get(key)!r}",
            )
        else:
            assert_equal(
                snapshot.get(key),
                expected_value,
                f"{name}: unexpected value for {key}",
            )

    claude_decision = get_claude_decision(snapshot, use_real_claude=use_real_claude)

    print("\nClaude decision:")
    print(pretty(claude_decision))

    python_validation = python_validation_stub(snapshot, claude_decision)

    print("\nPython validation:")
    print(pretty(python_validation))

    final_action, final_reason, final_reason_code = decide_final_action(
        claude_decision,
        python_validation,
    )

    print("\nFinal action:")
    print(pretty({
        "final_action": final_action,
        "final_reason": final_reason,
        "final_reason_code": final_reason_code,
    }))

    log_decision_compat(
        auditor=auditor,
        market_snapshot=snapshot,
        claude_decision=claude_decision,
        python_validation=python_validation,
        final_action=final_action,
        final_reason=final_reason,
        final_reason_code=final_reason_code,
        source="pipeline_test",
        metadata={"scenario": name, "use_real_claude": use_real_claude},
    )

    print("✅ Logged to audit database")

    return {
        "snapshot": snapshot,
        "claude_decision": claude_decision,
        "python_validation": python_validation,
        "final_action": final_action,
        "final_reason": final_reason,
        "final_reason_code": final_reason_code,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Database assertions
# ──────────────────────────────────────────────────────────────────────────────

def assert_audit_database(db_path: Path, expected_rows: int) -> None:
    print_section("AUDIT DATABASE CHECKS")

    assert_true(db_path.exists(), f"Audit database was not created: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM decision_audit").fetchone()["n"]
        blocked = conn.execute(
            "SELECT COUNT(*) AS n FROM decision_audit WHERE final_action = 'BLOCKED'"
        ).fetchone()["n"]
        overrides = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM decision_audit
            WHERE claude_decision = 'ENTER_NOW'
              AND final_action != 'ENTER_NOW'
            """
        ).fetchone()["n"]

        print(f"Rows in audit DB: {total}")
        print(f"Blocked decisions: {blocked}")
        print(f"Claude ENTER_NOW overridden/not-entered: {overrides}")

        assert_equal(total, expected_rows, "Unexpected number of audit rows")
        assert_true(blocked >= 1, "Expected at least one BLOCKED decision")
        assert_true(overrides >= 1, "Expected at least one Claude ENTER_NOW override")

        print("✅ Audit database checks passed")

    finally:
        conn.close()


def print_recent_and_summary(auditor: DecisionAuditor) -> None:
    print_section("RECENT AUDIT DECISIONS")

    try:
        rows = auditor.get_recent_decisions(count=10)
        for row in rows:
            print(
                f"ID {row['id']} | {row['instrument']} @ {row['price']} | "
                f"Claude={row['claude_decision']} {row['claude_setup']} | "
                f"Final={row['final_action']} | code={row['final_reason_code']}"
            )
    except Exception as e:
        print(f"[WARN] Could not print recent decisions through auditor: {e}")

    print_section("SUMMARY")

    try:
        summary = auditor.get_summary()
        print(pretty(summary))
    except Exception as e:
        print(f"[WARN] Could not print summary through auditor: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Test full trading brain pipeline.")
    parser.add_argument(
        "--real-claude",
        action="store_true",
        help="Use real ClaudeReviewer instead of deterministic mock Claude.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep existing logs/pipeline_test database instead of deleting it first.",
    )

    args = parser.parse_args()

    print_header("TESTING FULL PIPELINE")
    print(f"Claude mode: {'REAL' if args.real_claude else 'MOCK'}")

    log_dir = Path("logs/pipeline_test")
    clean_test_dir(log_dir, keep_existing=args.keep)

    auditor = DecisionAuditor(
        log_dir=str(log_dir),
        instrument="XAU_USD",
        db_name="decision_audit.db",
    )

    results = []

    # Scenario 1: bullish breakout
    results.append(run_scenario(
        name="[TEST 1] Bullish breakout pipeline",
        candles=generate_bullish_breakout_candles(),
        current_time=datetime(2026, 5, 15, 11, 30, 0, tzinfo=timezone.utc),
        expected={
            "current_price": 4727.0,
            "breakout_level": 4720.0,
            "trigger_direction": "LONG",
            "breakout_confirmed": True,
            "candles_above_level": lambda x: x >= 2,
            "level_name": "today_high",
            "market_state": lambda x: isinstance(x, str) and "BULLISH" in x,
        },
        auditor=auditor,
        use_real_claude=args.real_claude,
    ))

    # Scenario 2: bearish breakdown
    results.append(run_scenario(
        name="[TEST 2] Bearish breakdown pipeline",
        candles=generate_bearish_breakdown_candles(),
        current_time=datetime(2026, 5, 15, 14, 30, 0, tzinfo=timezone.utc),
        expected={
            "current_price": 4693.0,
            "breakout_level": 4700.0,
            "trigger_direction": "SHORT",
            "breakdown_confirmed": True,
            "candles_below_level": lambda x: x >= 2,
            "level_name": "today_low",
        },
        auditor=auditor,
        use_real_claude=args.real_claude,
    ))

    # Scenario 3: insufficient data
    results.append(run_scenario(
        name="[TEST 3] Insufficient data pipeline",
        candles=generate_insufficient_candles(),
        current_time=datetime(2026, 5, 15, 11, 30, 0, tzinfo=timezone.utc),
        expected={
            "current_price": 0,
            "market_state": "INSUFFICIENT_DATA",
            "trigger_direction": "NONE",
            "breakout_confirmed": False,
            "breakdown_confirmed": False,
        },
        auditor=auditor,
        use_real_claude=args.real_claude,
    ))

    db_path = log_dir / "decision_audit.db"
    assert_audit_database(db_path, expected_rows=3)
    print_recent_and_summary(auditor)

    print_header("PIPELINE TESTS COMPLETE")
    print("✅ market_snapshot.py works in pipeline")
    print("✅ Claude review layer works in pipeline")
    print("✅ Python validation stub works in pipeline")
    print("✅ decision_audit.py logs pipeline decisions")
    print(f"Audit DB saved to: {db_path}")


if __name__ == "__main__":
    main()
PY

python3 pipeline_test.py
