"""Adversarial tests for the governed Windows display adapter."""

import inspect
import json
import sys
from copy import copy, deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation import (
    ComponentKind,
    GovernedPowerComponent,
    LivenessState,
    NodeCapability,
    NodeRecord,
    NodeRegistry,
    PowerAction,
    PowerCoordinator,
    PowerPolicy,
    PowerProposal,
    PowerStatus,
    WindowsDisplayAdapter,
    WindowsDisplayDeployment,
    WindowsDisplayFailureCode,
    WindowsDisplayResult,
)
from federation.windows_display_native import (
    NativeDisplayApi,
    PlatformProbe,
    WindowsNativeDisplayApi,
    WindowsPlatformError,
)
from federation.windows_session import (
    NativeWindowsSessionProbe,
    WindowsSessionProbe,
    WindowsSessionSnapshot,
)
from federation.windows_display_adapter import WorkerStateProbe
from federation.power_adapter import (
    PowerExecutionAuthorization,
    PowerExecutionAuthorizationAuthority,
)
from scripts import windows_display_helper


NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
KEY = b"k" * 32
AUTHORIZATION_KEY = b"a" * 32


class FakePlatform(PlatformProbe):
    def __init__(self, is_windows):
        self._is_windows = is_windows

    def is_windows(self):
        return self._is_windows


class FakeNative(NativeDisplayApi):
    def __init__(self, succeeded=True):
        self.succeeded = succeeded
        self.actions = []
        self.timeout_ms = 750

    def invoke(self, action):
        self.actions.append(action)
        return self.succeeded


class FakeSessionProbe(WindowsSessionProbe):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.observations = 0

    def observe(self):
        self.observations += 1
        return self.snapshot


class FakeWorkerStateProbe(WorkerStateProbe):
    def __init__(
        self,
        state=LivenessState.ONLINE,
        capabilities=("display_control",),
    ):
        self.state = state
        self.capabilities = tuple(capabilities)

    def observe(self, worker_id):
        return self.state, self.capabilities


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class FakeHeartbeatRegistry:
    def __init__(self, worker_probe):
        self.worker_probe = worker_probe

    def inspect(self, worker_id):
        state, capabilities = self.worker_probe.observe(worker_id)
        return type(
            "Lease",
            (),
            {
                "state": state,
                "power_capabilities": capabilities,
                "authentication_tag": "authenticated-heartbeat",
            },
        )()


def session(
    *,
    process_session_id=4,
    active_console_session_id=4,
    visible=True,
    idle=60.0,
    prohibited=False,
    locked=False,
):
    return WindowsSessionSnapshot(
        process_session_id=process_session_id,
        active_console_session_id=active_console_session_id,
        process_in_active_console_session=(
            process_session_id == active_console_session_id
        ),
        visible_interactive_window_station=visible,
        idle_seconds=idle,
        locally_prohibited=prohibited,
        session_locked=locked,
        observed_at=NOW,
    )


def deployment(**changes):
    values = {
        "enabled": True,
        "adapter_id": "windows-display-1",
        "worker_id": "worker-1",
        "component_id": "display-1",
        "policy_version": "policy-v1",
        "controller_authority": "controller-1",
        "integrity_authority": "integrity-1",
        "minimum_idle_seconds": 30.0,
        "native_timeout_ms": 750,
        "allow_display_off": True,
        "allow_display_on": True,
        "allow_when_locked": False,
    }
    values.update(changes)
    return WindowsDisplayDeployment(**values)


def component(**changes):
    values = {
        "worker_id": "worker-1",
        "component_id": "display-1",
        "kind": ComponentKind.DISPLAY,
        "supported_actions": (
            PowerAction.DISPLAY_OFF,
            PowerAction.DISPLAY_ON,
        ),
        "policy": PowerPolicy(
            version="policy-v1",
            automatic_display_control=True,
        ),
        "controller_authority": "controller-1",
        "integrity_authority": "integrity-1",
        "adapter_id": "windows-display-1",
    }
    values.update(changes)
    return GovernedPowerComponent(**values)


def adapter(
    *,
    config=None,
    governed_component=None,
    native=None,
    session_probe=None,
    worker_probe=None,
    authorization_verifier=None,
):
    return WindowsDisplayAdapter(
        deployment=config if config is not None else deployment(),
        component=governed_component if governed_component is not None else component(),
        native_api=native if native is not None else FakeNative(),
        session_probe=(
            session_probe if session_probe is not None else FakeSessionProbe(session())
        ),
        worker_state_probe=(
            worker_probe if worker_probe is not None else FakeWorkerStateProbe()
        ),
        authorization_verifier=(
            authorization_verifier
            if authorization_verifier is not None
            else PowerExecutionAuthorizationAuthority(AUTHORIZATION_KEY)
        ),
        clock=MutableClock(),
    )


def authorization(action=PowerAction.DISPLAY_OFF, **changes):
    values = {
        "proposal_id": "proposal-1",
        "adapter_id": "windows-display-1",
        "worker_id": "worker-1",
        "component_id": "display-1",
        "action": action,
        "policy_version": "policy-v1",
        "controller_authority": "controller-1",
        "integrity_authority": "integrity-1",
        "authorization_sequence": 1,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(changes)
    return PowerExecutionAuthorizationAuthority(AUTHORIZATION_KEY).issue(**values)


def test_repository_modules_import_safely_on_fedora():
    assert "federation.windows_display_native" in sys.modules
    assert "federation.windows_session" in sys.modules


def test_real_native_backend_refuses_non_windows_before_loading_dll(monkeypatch):
    loaded = []
    monkeypatch.setattr(
        "federation.windows_display_native._load_user32",
        lambda: loaded.append("user32"),
    )

    with pytest.raises(WindowsPlatformError) as exc:
        WindowsNativeDisplayApi(
            platform=FakePlatform(False),
            timeout_ms=750,
        )

    assert exc.value.code is WindowsDisplayFailureCode.UNSUPPORTED_PLATFORM
    assert loaded == []


@pytest.mark.parametrize(
    ("field", "value", "exception"),
    [
        ("enabled", None, TypeError),
        ("enabled", "yes", TypeError),
        ("minimum_idle_seconds", -1, ValueError),
        ("minimum_idle_seconds", float("inf"), ValueError),
        ("native_timeout_ms", 0, ValueError),
        ("native_timeout_ms", 5001, ValueError),
        ("policy_version", "", ValueError),
        ("worker_id", "", ValueError),
        ("component_id", "", ValueError),
    ],
)
def test_deployment_configuration_is_explicit_and_strict(field, value, exception):
    with pytest.raises(exception):
        deployment(**{field: value})


def test_adapter_requires_deployment_and_is_disabled_when_explicitly_disabled():
    with pytest.raises(TypeError):
        WindowsDisplayAdapter()

    result = adapter(config=deployment(enabled=False)).attempt_authorized(
        authorization()
    )
    assert result.code is WindowsDisplayFailureCode.DISABLED
    assert result.attempted is False


@pytest.mark.parametrize(
    "action",
    [
        PowerAction.SLEEP,
        PowerAction.HIBERNATE,
        PowerAction.SHUTDOWN,
        PowerAction.WAKE_ON_LAN,
        PowerAction.SCHEDULED_WAKE,
    ],
)
def test_only_typed_display_actions_are_accepted(action):
    native = FakeNative()
    result = adapter(native=native).attempt_authorized(authorization(action))
    assert result.code is WindowsDisplayFailureCode.UNSUPPORTED_ACTION
    assert native.actions == []


def test_arbitrary_action_string_and_direct_attempt_are_rejected():
    display = adapter()
    with pytest.raises(TypeError):
        authorization("display_off")
    with pytest.raises(TypeError):
        display.attempt("display_off", "worker-1", "display-1")
    result = display.attempt(
        PowerAction.DISPLAY_OFF,
        "worker-1",
        "display-1",
    )
    assert result.code is WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED


def test_public_constructor_without_valid_authentication_cannot_invoke_native():
    values = {
        field: getattr(authorization(), field)
        for field in (
            "proposal_id",
            "adapter_id",
            "worker_id",
            "component_id",
            "action",
            "policy_version",
            "controller_authority",
            "integrity_authority",
            "authorization_sequence",
            "issued_at",
            "expires_at",
        )
    }
    values["authentication_tag"] = "0" * 64
    native = FakeNative()
    result = adapter(native=native).attempt_authorized(
        PowerExecutionAuthorization(**values)
    )
    assert result.code is WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED
    assert native.actions == []


def test_external_dictionary_reconstruction_without_valid_tag_fails_closed():
    accepted = authorization()
    values = asdict(accepted)
    values["authentication_tag"] = "f" * 64
    reconstructed = PowerExecutionAuthorization(**values)

    assert adapter().attempt_authorized(reconstructed).code is (
        WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED
    )
    values.pop("authentication_tag")
    with pytest.raises(TypeError):
        PowerExecutionAuthorization(**values)


def test_copy_and_deepcopy_preserve_same_authenticated_evidence():
    accepted = authorization()
    verifier = PowerExecutionAuthorizationAuthority(AUTHORIZATION_KEY)

    copied = copy(accepted)
    deepcopied = deepcopy(accepted)

    assert copied == accepted
    assert deepcopied == accepted
    assert verifier.verify(copied)
    assert verifier.verify(deepcopied)


def test_sentinel_and_accepted_factory_no_longer_exist():
    import federation.power_adapter as power_adapter

    assert not hasattr(power_adapter, "_COORDINATOR_AUTHORIZATION_SEAL")
    assert not hasattr(PowerExecutionAuthorization, "_accepted")
    assert "accepted" not in inspect.signature(PowerExecutionAuthorization).parameters
    assert "_coordinator_seal" not in inspect.signature(
        PowerExecutionAuthorization
    ).parameters


def test_authorization_authority_requires_explicit_strong_bytes_key():
    with pytest.raises(TypeError):
        PowerExecutionAuthorizationAuthority("a" * 32)
    with pytest.raises(ValueError):
        PowerExecutionAuthorizationAuthority(b"short")
    with pytest.raises(TypeError):
        PowerExecutionAuthorizationAuthority()


def test_wrong_key_and_separately_constructed_authority_fail_verification():
    accepted = authorization()

    assert PowerExecutionAuthorizationAuthority(AUTHORIZATION_KEY).verify(accepted)
    assert not PowerExecutionAuthorizationAuthority(b"b" * 32).verify(accepted)


def test_missing_and_malformed_authentication_tags_fail_closed():
    values = asdict(authorization())
    values.pop("authentication_tag")
    with pytest.raises(TypeError):
        PowerExecutionAuthorization(**values)

    malformed = replace(authorization(), authentication_tag="not-a-sha256-tag")
    native = FakeNative()
    result = adapter(native=native).attempt_authorized(malformed)
    assert result.code is WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED
    assert native.actions == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proposal_id", "other-proposal"),
        ("worker_id", "other-worker"),
        ("component_id", "other-component"),
        ("adapter_id", "other-adapter"),
        ("action", PowerAction.DISPLAY_ON),
        ("policy_version", "other-policy"),
        ("controller_authority", "other-controller"),
        ("integrity_authority", "other-integrity"),
        ("authorization_sequence", 2),
        ("issued_at", NOW + timedelta(seconds=1)),
        ("expires_at", NOW + timedelta(minutes=6)),
    ],
)
def test_changing_any_bound_authorization_field_invalidates_tag(field, value):
    accepted = authorization()
    changed = replace(accepted, **{field: value})
    verifier = PowerExecutionAuthorizationAuthority(AUTHORIZATION_KEY)
    native = FakeNative()

    assert not verifier.verify(changed)
    result = adapter(native=native).attempt_authorized(changed)
    assert result.code is WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED
    assert native.actions == []


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"worker_id": "other"}, WindowsDisplayFailureCode.IDENTITY_MISMATCH),
        ({"component_id": "other"}, WindowsDisplayFailureCode.IDENTITY_MISMATCH),
        ({"adapter_id": "other"}, WindowsDisplayFailureCode.IDENTITY_MISMATCH),
        ({"policy_version": "other"}, WindowsDisplayFailureCode.POLICY_MISMATCH),
        (
            {"controller_authority": "other"},
            WindowsDisplayFailureCode.AUTHORITY_MISMATCH,
        ),
        (
            {"integrity_authority": "other"},
            WindowsDisplayFailureCode.AUTHORITY_MISMATCH,
        ),
    ],
)
def test_authorization_binding_is_exact(change, code):
    native = FakeNative()
    result = adapter(native=native).attempt_authorized(authorization(**change))
    assert result.code is code
    assert native.actions == []


def test_expired_authorization_is_rejected():
    native = FakeNative()
    result = adapter(native=native).attempt_authorized(
        authorization(
            issued_at=NOW - timedelta(minutes=10),
            expires_at=NOW,
        )
    )
    assert result.code is WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED
    assert native.actions == []


def test_component_binding_kind_and_adapter_identity_are_exact():
    with pytest.raises(ValueError):
        adapter(
            governed_component=replace(
                component(),
                kind=ComponentKind.SYSTEM_POWER,
                supported_actions=(PowerAction.SLEEP,),
            )
        )
    with pytest.raises(ValueError):
        adapter(governed_component=replace(component(), adapter_id="other"))


@pytest.mark.parametrize(
    "state",
    [
        LivenessState.OFFLINE,
        LivenessState.STALE,
        LivenessState.INTENTIONALLY_SLEEPING,
        LivenessState.WAKING,
    ],
)
def test_worker_must_remain_online(state):
    native = FakeNative()
    result = adapter(
        native=native,
        worker_probe=FakeWorkerStateProbe(state=state),
    ).attempt_authorized(authorization())
    assert result.code is WindowsDisplayFailureCode.WORKER_UNAVAILABLE
    assert native.actions == []


def test_display_control_capability_must_remain_advertised():
    native = FakeNative()
    result = adapter(
        native=native,
        worker_probe=FakeWorkerStateProbe(capabilities=()),
    ).attempt_authorized(authorization())
    assert result.code is WindowsDisplayFailureCode.CAPABILITY_UNAVAILABLE
    assert native.actions == []


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (
            session(active_console_session_id=None),
            WindowsDisplayFailureCode.NO_ACTIVE_SESSION,
        ),
        (
            session(process_session_id=3, active_console_session_id=4),
            WindowsDisplayFailureCode.WRONG_SESSION,
        ),
        (
            session(visible=False),
            WindowsDisplayFailureCode.NONINTERACTIVE_SESSION,
        ),
        (
            session(prohibited=True),
            WindowsDisplayFailureCode.LOCALLY_PROHIBITED,
        ),
    ],
)
def test_active_interactive_session_is_mandatory(snapshot, code):
    native = FakeNative()
    result = adapter(
        native=native,
        session_probe=FakeSessionProbe(snapshot),
    ).attempt_authorized(authorization())
    assert result.code is code
    assert native.actions == []


def test_display_off_requires_configured_idle_threshold_but_display_on_does_not():
    native = FakeNative()
    display = adapter(
        native=native,
        session_probe=FakeSessionProbe(session(idle=29.9)),
    )
    off = display.attempt_authorized(authorization(PowerAction.DISPLAY_OFF))
    on = display.attempt_authorized(authorization(PowerAction.DISPLAY_ON))
    assert off.code is WindowsDisplayFailureCode.IDLE_THRESHOLD_NOT_MET
    assert on.succeeded is True
    assert native.actions == [PowerAction.DISPLAY_ON]


def test_display_off_at_idle_threshold_can_execute():
    native = FakeNative()
    result = adapter(
        native=native,
        session_probe=FakeSessionProbe(session(idle=30.0)),
    ).attempt_authorized(authorization())
    assert result.succeeded is True
    assert native.actions == [PowerAction.DISPLAY_OFF]


def test_locked_session_behavior_is_explicit_policy():
    denied = adapter(
        session_probe=FakeSessionProbe(session(locked=True)),
    ).attempt_authorized(authorization(PowerAction.DISPLAY_ON))
    allowed = adapter(
        config=deployment(allow_when_locked=True),
        session_probe=FakeSessionProbe(session(locked=True)),
    ).attempt_authorized(authorization(PowerAction.DISPLAY_ON))
    assert denied.code is WindowsDisplayFailureCode.LOCALLY_PROHIBITED
    assert allowed.succeeded is True


def test_native_zero_or_timeout_is_typed_durable_failure():
    result = adapter(native=FakeNative(succeeded=False)).attempt_authorized(
        authorization()
    )
    assert isinstance(result, WindowsDisplayResult)
    assert result.code is WindowsDisplayFailureCode.NATIVE_CALL_FAILED
    assert result.attempted is True
    assert result.native_call_succeeded is False
    assert result.native_timeout_ms == 750


def test_native_success_is_typed_and_does_not_claim_observed_display_state():
    result = adapter(native=FakeNative()).attempt_authorized(authorization())
    assert result.code is WindowsDisplayFailureCode.NONE
    assert result.succeeded is True
    assert result.native_call_succeeded is True
    assert not hasattr(result, "display_state_changed")


def test_native_backend_uses_only_fixed_win32_mapping(monkeypatch):
    calls = []

    class Function:
        argtypes = None
        restype = None

        def __call__(self, *args):
            calls.append(args)
            return 1

    class User32:
        SendMessageTimeoutW = Function()

    monkeypatch.setattr(
        "federation.windows_display_native._load_user32",
        lambda: User32(),
    )
    native = WindowsNativeDisplayApi(
        platform=FakePlatform(True),
        timeout_ms=750,
    )
    assert native.invoke(PowerAction.DISPLAY_OFF) is True
    assert native.invoke(PowerAction.DISPLAY_ON) is True

    assert [call[3] for call in calls] == [2, -1]
    assert {call[0] for call in calls} == {0xFFFF}
    assert {call[1] for call in calls} == {0x0112}
    assert {call[2] for call in calls} == {0xF170}
    assert {call[4] for call in calls} == {0x0002}
    assert {call[5] for call in calls} == {750}


def test_native_zero_return_is_failure(monkeypatch):
    class Function:
        argtypes = None
        restype = None

        def __call__(self, *args):
            return 0

    class User32:
        SendMessageTimeoutW = Function()

    monkeypatch.setattr(
        "federation.windows_display_native._load_user32",
        lambda: User32(),
    )
    native = WindowsNativeDisplayApi(
        platform=FakePlatform(True),
        timeout_ms=750,
    )
    assert native.invoke(PowerAction.DISPLAY_OFF) is False


def test_production_session_probe_refuses_fedora_before_dll_loading(monkeypatch):
    loaded = []
    monkeypatch.setattr(
        "federation.windows_session.ctypes.WinDLL",
        lambda *args, **kwargs: loaded.append(args),
        raising=False,
    )
    with pytest.raises(WindowsPlatformError):
        NativeWindowsSessionProbe(
            platform=FakePlatform(False),
            clock=MutableClock(),
        )
    assert loaded == []


def test_adapter_rejects_untyped_dependency_substitutes():
    with pytest.raises(TypeError, match="native_api"):
        WindowsDisplayAdapter(
            deployment=deployment(),
            component=component(),
            native_api=type(
                "DuckNative",
                (),
                {"timeout_ms": 750, "invoke": lambda self, action: True},
            )(),
            session_probe=FakeSessionProbe(session()),
            worker_state_probe=FakeWorkerStateProbe(),
            authorization_verifier=PowerExecutionAuthorizationAuthority(
                AUTHORIZATION_KEY
            ),
            clock=MutableClock(),
        )


def test_adapter_requires_exact_authorization_verifier():
    values = {
        "deployment": deployment(),
        "component": component(),
        "native_api": FakeNative(),
        "session_probe": FakeSessionProbe(session()),
        "worker_state_probe": FakeWorkerStateProbe(),
        "clock": MutableClock(),
    }
    with pytest.raises(TypeError, match="authorization_verifier"):
        WindowsDisplayAdapter(**values)
    with pytest.raises(TypeError, match="authorization_verifier"):
        WindowsDisplayAdapter(
            **values,
            authorization_verifier=type(
                "DuckVerifier",
                (),
                {"verify": lambda self, value: True},
            )(),
        )


def coordinator(tmp_path, native=None, worker_probe=None):
    native = native or FakeNative()
    worker_probe = worker_probe or FakeWorkerStateProbe()
    authorization_authority = PowerExecutionAuthorizationAuthority(
        AUTHORIZATION_KEY
    )
    display = adapter(
        native=native,
        worker_probe=worker_probe,
        authorization_verifier=authorization_authority,
    )
    nodes = NodeRegistry()
    nodes.register(
        NodeRecord(
            node_id="worker-1",
            hostname="worker",
            operating_system="windows",
            capabilities={NodeCapability("display_control")},
        )
    )
    power = PowerCoordinator(
        tmp_path / "power.evidence",
        node_registry=nodes,
        heartbeat_registry=FakeHeartbeatRegistry(worker_probe),
        controller_authority="controller-1",
        integrity_authority="integrity-1",
        integrity_key=KEY,
        execution_authorization_authority=authorization_authority,
        adapters={display.adapter_id: display},
        clock=MutableClock(),
    )
    power.register_component(component())
    return power, native


def proposal(action=PowerAction.DISPLAY_OFF, sequence=1):
    return PowerProposal.create(
        worker_id="worker-1",
        component_id="display-1",
        action=action,
        sequence=sequence,
        controller_authority="controller-1",
        integrity_authority="integrity-1",
        policy_version="policy-v1",
        requested_at=NOW,
        integrity_key=KEY,
    )


def execute(power, action=PowerAction.DISPLAY_OFF, sequence=1):
    snapshot = power.propose(proposal(action, sequence))
    power.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=5))
    return power.execute(snapshot.proposal_id)


def test_proposal_and_approval_cannot_directly_invoke_native(tmp_path):
    power, native = coordinator(tmp_path)
    proposed = power.propose(proposal())
    assert proposed.status is PowerStatus.AUTO_APPROVED
    assert native.actions == []
    assert not hasattr(proposed, "execute")


def test_only_execution_authorized_coordinator_path_invokes_native(tmp_path):
    power, native = coordinator(tmp_path)
    snapshot = power.propose(proposal())
    with pytest.raises(Exception, match="authorization"):
        power.execute(snapshot.proposal_id)
    assert native.actions == []
    power.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=5))
    result = power.execute(snapshot.proposal_id)
    assert result.status is PowerStatus.SUCCEEDED
    assert native.actions == [PowerAction.DISPLAY_OFF]


def test_coordinator_issues_authorization_that_adapter_verifies(tmp_path, monkeypatch):
    issued = []
    original = PowerExecutionAuthorizationAuthority.issue

    def capture(authority, **values):
        result = original(authority, **values)
        issued.append(result)
        return result

    monkeypatch.setattr(PowerExecutionAuthorizationAuthority, "issue", capture)
    power, native = coordinator(tmp_path)

    result = execute(power)

    assert result.status is PowerStatus.SUCCEEDED
    assert len(issued) == 1
    assert PowerExecutionAuthorizationAuthority(AUTHORIZATION_KEY).verify(issued[0])
    assert native.actions == [PowerAction.DISPLAY_OFF]


def test_exact_repeated_and_concurrent_execution_invokes_native_once(tmp_path):
    power, native = coordinator(tmp_path)
    snapshot = power.propose(proposal())
    power.authorize(snapshot.proposal_id, expires_at=NOW + timedelta(minutes=5))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: power.execute(snapshot.proposal_id), range(8))
        )
    assert {item.status for item in results} == {PowerStatus.SUCCEEDED}
    assert native.actions == [PowerAction.DISPLAY_OFF]


@pytest.mark.parametrize("native_success", [True, False])
def test_native_terminal_result_is_durable_across_restart(tmp_path, native_success):
    native = FakeNative(succeeded=native_success)
    power, _ = coordinator(tmp_path, native=native)
    result = execute(power)
    restarted, _ = coordinator(tmp_path, native=FakeNative())
    recovered = restarted.inspect(result.proposal_id)
    expected = PowerStatus.SUCCEEDED if native_success else PowerStatus.FAILED
    assert recovered.status is expected
    assert len(native.actions) == 1


def test_conflicting_display_actions_serialize_without_second_native_call(tmp_path):
    power, native = coordinator(tmp_path)
    first = power.propose(proposal())
    second = power.propose(proposal(PowerAction.DISPLAY_ON, sequence=2))
    assert second.status is PowerStatus.REFUSED
    power.authorize(first.proposal_id, expires_at=NOW + timedelta(minutes=5))
    power.execute(first.proposal_id)
    assert native.actions == [PowerAction.DISPLAY_OFF]


def test_helper_defaults_disabled_refuses_fedora_and_help_version_are_safe(
    capsys,
    monkeypatch,
):
    native_constructions = []
    monkeypatch.setattr(
        "federation.windows_display_native.WindowsNativeDisplayApi",
        lambda **kwargs: native_constructions.append(kwargs),
    )

    assert windows_display_helper.main([], platform=FakePlatform(False)) == 2
    assert windows_display_helper.main(["--help"]) == 0
    assert windows_display_helper.main(["--version"]) == 0
    assert native_constructions == []
    output = capsys.readouterr().out
    assert "disabled" in output.casefold()


def test_helper_dry_run_is_safe_and_never_constructs_native(capsys, monkeypatch):
    native_constructions = []
    monkeypatch.setattr(
        "federation.windows_display_native.WindowsNativeDisplayApi",
        lambda **kwargs: native_constructions.append(kwargs),
    )
    result = windows_display_helper.main(
        ["--dry-run", "--action", "display_on"],
        platform=FakePlatform(True),
    )
    assert result == 0
    assert native_constructions == []
    assert json.loads(capsys.readouterr().out) == {
        "action": "display_on",
        "attempted": False,
        "code": "dry_run",
        "ok": True,
    }


def test_helper_real_flags_return_stable_governed_composition_refusal(
    capsys,
    monkeypatch,
):
    native_constructions = []
    monkeypatch.setattr(
        "federation.windows_display_native.WindowsNativeDisplayApi",
        lambda **kwargs: native_constructions.append(kwargs),
    )
    args = [
        "--action",
        "display_on",
        "--enable",
        "--confirm-real-execution",
    ]
    result = windows_display_helper.main(
        args,
        platform=FakePlatform(True),
    )
    assert result == 2
    assert native_constructions == []
    assert json.loads(capsys.readouterr().out) == {
        "code": "governed_composition_required",
        "ok": False,
    }


@pytest.mark.parametrize(
    ("dependency_name", "dependency"),
    [
        ("invoke", lambda action: {"ok": True}),
        ("invoke", FakeNative().invoke),
        ("native_api", FakeNative()),
    ],
)
def test_helper_rejects_generic_callbacks_and_raw_native_api(
    dependency_name,
    dependency,
):
    with pytest.raises(TypeError):
        windows_display_helper.main(
            [
                "--action",
                "display_on",
                "--enable",
                "--confirm-real-execution",
            ],
            platform=FakePlatform(True),
            **{dependency_name: dependency},
        )


def test_helper_accepts_no_command_or_native_parameter_surface():
    parameters = inspect.signature(windows_display_helper.main).parameters
    assert "command" not in parameters
    assert "invoke" not in parameters
    assert "native_api" not in parameters
    assert "adapter" not in parameters
    assert "authorization" not in parameters
    source = inspect.getsource(windows_display_helper)
    forbidden = (
        "subprocess",
        "shell=True",
        "os.system",
        "powershell",
        "cmd.exe",
        "rundll32",
        "eval(",
        "exec(",
        "pyautogui",
    )
    assert not any(value in source.casefold() for value in forbidden)
    assert ".invoke(" not in source


def test_native_backend_and_authorization_docs_state_trusted_process_boundary():
    from federation import power_adapter
    import federation.windows_display_native as native_module

    authorization_source = inspect.getsource(power_adapter).casefold()
    native_source = inspect.getsource(native_module).casefold()
    documentation = "\n".join(
        (
            Path("WINDOWS_DISPLAY_ADAPTER_V0_1_REPORT.md").read_text(),
            Path("docs/windows_display_adapter_v0_1_smoke_test.md").read_text(),
        )
    ).casefold()

    assert "not an in-process sandbox" in authorization_source
    assert "trusted low-level primitive" in native_source
    assert "does not perform authorization" in native_source
    assert "malicious python" in documentation
    assert "outside" in documentation
    assert "inspect process memory" in documentation
    assert "monkeypatch" in documentation
    assert "ctypes" in documentation
    assert "not an in-process sandbox" in documentation
    assert "trusted low-level primitive" in documentation
    assert "governed_composition_required" in documentation
    assert "windowsnativedisplayapi.invoke" not in documentation
    assert "unforgeable in-process seal" not in documentation
    assert "real windows smoke test must not be run yet" in documentation


def test_windows_sources_have_no_shell_elevation_or_dynamic_native_route():
    import federation.windows_display_adapter as adapter_module
    import federation.windows_display_native as native_module
    import federation.windows_session as session_module

    source = "\n".join(
        inspect.getsource(module)
        for module in (
            adapter_module,
            native_module,
            session_module,
            windows_display_helper,
        )
    ).casefold()
    forbidden = (
        "subprocess",
        "shell=true",
        "os.system",
        "powershell",
        "cmd.exe",
        "rundll32",
        "wmi",
        "eval(",
        "exec(",
        "pyautogui",
        "runas",
        "shellexecutew",
    )
    assert not any(value in source for value in forbidden)
    assert "sendmessagetimeoutw" in source
    assert 'windll("user32"' in source


def test_result_safe_record_contains_no_raw_native_or_secret_values():
    record = adapter().attempt_authorized(authorization()).record()
    encoded = json.dumps(record)
    assert set(record) == {
        "adapter_id",
        "worker_id",
        "component_id",
        "action",
        "attempted",
        "native_call_succeeded",
        "code",
        "reason",
        "session_id",
        "observed_idle_seconds",
        "controller_timestamp",
        "native_timeout_ms",
    }
    assert "0x" not in encoded
    assert "key" not in encoded.casefold()
