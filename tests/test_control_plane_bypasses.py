from pathlib import Path


def test_existing_bypasses_are_explicitly_documented():
    docs = Path("docs/control_plane_bypass_register.md").read_text(encoding="utf-8")

    for token in (
        "B001",
        "B003",
        "B005",
        "B007",
        "B009",
        "B013",
        "save_position",
        "save_ledger_transaction",
        "save_synthetic_position_valuation",
        "MoneylineQualificationService.evaluate",
    ):
        assert token in docs


def test_authority_matrix_and_phase_doc_capture_the_target_boundaries():
    authority = Path("docs/control_plane_authority_matrix.md").read_text(
        encoding="utf-8"
    )
    phase = Path("docs/quant_control_plane_v1.md").read_text(encoding="utf-8")

    assert "Probability reconciliation" in authority
    assert "Ledger changes" in authority
    assert "Order-intent state transitions" in authority
    assert "direct wager, position, ledger, and forecast paths" in phase
    assert "Only the reconciled execution probability" in phase
    assert "Valid lifecycle" in phase
