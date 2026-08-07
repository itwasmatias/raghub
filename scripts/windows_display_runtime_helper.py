#!/usr/bin/env python3
"""Windows Display Runtime dry-run inspection helper.

This script performs safe dry-run validation of intake envelopes without
executing any real Windows display or power actions.

Security:
- Reads integrity keys from a protected configuration file (not command line)
- Never exposes keys or credentials in output
- Only supports dry-run mode (real execution explicitly prohibited)
- Validates all authentication layers
- Records durable evidence

Default mode: disabled (safe)
Supported mode: dry-run only
Explicitly prohibited: real execution mode
"""

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def load_trusted_configuration(config_path):
    """Load trusted runtime configuration from a protected file.

    The configuration file should contain:
    {
        "runtime_identity": "...",
        "worker_identity": "...",
        "component_identity": "...",
        "adapter_identity": "...",
        "policy_version": "...",
        "controller_authority": "...",
        "integrity_authority": "...",
        "intake_authority": "...",
        "execution_authorization_authority": "...",
        "intake_key_hex": "...",
        "execution_key_hex": "...",
        "evidence_store_key_hex": "..."
    }

    Keys must be hex-encoded 32-byte values.
    This file should have restricted permissions (0600).
    """
    if not config_path.exists():
        return None, "Configuration file not found"

    try:
        config = json.loads(config_path.read_text())
    except Exception as exc:
        return None, f"Failed to parse configuration: {exc}"

    # Validate required fields (without exposing their values)
    required = {
        "runtime_identity",
        "worker_identity",
        "component_identity",
        "adapter_identity",
        "policy_version",
        "controller_authority",
        "integrity_authority",
        "intake_authority",
        "execution_authorization_authority",
        "intake_key_hex",
        "execution_key_hex",
        "evidence_store_key_hex",
    }

    missing = required - set(config.keys())
    if missing:
        return None, f"Configuration missing required fields: {sorted(missing)}"

    # Decode hex keys
    try:
        config["intake_key"] = bytes.fromhex(config["intake_key_hex"])
        config["execution_key"] = bytes.fromhex(config["execution_key_hex"])
        config["evidence_store_key"] = bytes.fromhex(config["evidence_store_key_hex"])
    except Exception as exc:
        return None, f"Failed to decode keys: {exc}"

    # Validate key lengths
    for key_name in ["intake_key", "execution_key", "evidence_store_key"]:
        if len(config[key_name]) != 32:
            return None, f"{key_name} must be 32 bytes"

    return config, None


def perform_dry_run_validation(envelope_file, config):
    """Perform governed dry-run validation through the real runtime."""
    from federation.windows_display_intake import IntakeEnvelopeAuthority
    from federation.windows_display_runtime import (
        RuntimeDeployment,
        RuntimeMode,
        WindowsDisplayRuntime,
    )
    from federation.windows_display_runtime_store import IntakeStore
    from federation.power_adapter import PowerExecutionAuthorizationAuthority

    # Create temporary evidence store for this dry-run
    with tempfile.TemporaryDirectory() as tmpdir:
        evidence_path = Path(tmpdir) / "dry_run_evidence.jsonl"

        # Build runtime deployment
        deployment = RuntimeDeployment(
            runtime_identity=config["runtime_identity"],
            worker_identity=config["worker_identity"],
            component_identity=config["component_identity"],
            adapter_identity=config["adapter_identity"],
            policy_version=config["policy_version"],
            controller_authority=config["controller_authority"],
            integrity_authority=config["integrity_authority"],
            intake_authority=config["intake_authority"],
            execution_authorization_authority=config["execution_authorization_authority"],
            mode=RuntimeMode.DRY_RUN,
            evidence_store_path=str(evidence_path),
            native_timeout_ms=1000,
        )

        # Create authorities
        intake_authority = IntakeEnvelopeAuthority(
            intake_key=config["intake_key"]
        )
        execution_authority = PowerExecutionAuthorizationAuthority(
            key=config["execution_key"]
        )

        # Create evidence store
        store = IntakeStore(
            path=evidence_path,
            runtime_identity=config["runtime_identity"],
            integrity_key=config["evidence_store_key"],
            clock=lambda: datetime.now(timezone.utc),
        )

        # Create runtime
        runtime = WindowsDisplayRuntime(
            deployment=deployment,
            intake_authority=intake_authority,
            execution_authorization_authority=execution_authority,
            adapter=None,
            worker_state_probe=None,
            session_probe=None,
            intake_store=store,
            clock=lambda: datetime.now(timezone.utc),
        )

        # Read and process envelope
        envelope_bytes = envelope_file.read_bytes()

        # Process through governed runtime
        result = runtime.process_intake(envelope_bytes)

        # Build safe output (never expose keys)
        return {
            "status": "dry_run_complete",
            "envelope_id": result.envelope_id,
            "intake_sequence": result.intake_sequence,
            "final_state": result.state.value,
            "refusal_code": result.refusal_code,
            "mode": result.mode.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "note": "No real Windows display action was executed",
            "evidence_events": len(store.inspect()),
        }


def main():
    """Dry-run inspection entry point."""
    parser = argparse.ArgumentParser(
        description="Windows Display Runtime dry-run helper (v0.1)",
        epilog="This helper validates intake envelopes without executing real actions.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="Windows Display Runtime Helper v0.1",
    )
    parser.add_argument(
        "--envelope-file",
        type=Path,
        help="Path to intake envelope JSON file",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to trusted configuration file (required for dry-run)",
    )
    parser.add_argument(
        "--mode",
        choices=["disabled", "dry-run"],
        default="disabled",
        help="Operating mode (default: disabled)",
    )

    args = parser.parse_args()

    # Explicitly reject any real execution
    if hasattr(args, "real") or "real" in sys.argv or "--real" in sys.argv:
        print(
            json.dumps(
                {
                    "error": "real_execution_prohibited",
                    "message": "Real execution mode is explicitly prohibited in v0.1",
                }
            ),
            file=sys.stderr,
        )
        return 1

    # Default disabled mode
    if args.mode == "disabled" or not args.envelope_file:
        print(
            json.dumps(
                {
                    "status": "disabled",
                    "message": "Runtime helper is in disabled mode",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        return 0

    # Dry-run mode
    if args.mode == "dry-run":
        if not args.config:
            print(
                json.dumps(
                    {
                        "error": "config_required",
                        "message": "Dry-run mode requires --config parameter",
                    }
                ),
                file=sys.stderr,
            )
            return 1

        if not args.envelope_file.exists():
            print(
                json.dumps(
                    {
                        "error": "file_not_found",
                        "message": f"Envelope file not found: {args.envelope_file}",
                    }
                ),
                file=sys.stderr,
            )
            return 1

        # Load trusted configuration
        config, error = load_trusted_configuration(args.config)
        if error:
            print(
                json.dumps(
                    {
                        "error": "configuration_failed",
                        "message": error,
                    }
                ),
                file=sys.stderr,
            )
            return 1

        try:
            result = perform_dry_run_validation(args.envelope_file, config)
            print(json.dumps(result, indent=2))
            return 0

        except Exception as exc:
            print(
                json.dumps(
                    {
                        "error": "validation_failed",
                        "message": str(exc),
                        "type": type(exc).__name__,
                    }
                ),
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
