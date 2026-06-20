from blossa.privacy import (
    mask_value,
    masked_samples,
    structural_pattern,
    summarize_patterns,
)


def test_email_pattern_and_mask():
    assert structural_pattern("carmen.i@mail.example") == "email"
    masked = mask_value("carmen.i@mail.example")
    assert "@" in masked and masked.endswith(".example")
    assert "carmen" not in masked  # local part is hidden


def test_structural_pattern_letters_digits():
    assert structural_pattern("SKU-ABX-001") == "AAA-AAA-999"


def test_structural_pattern_collapses_long_runs():
    # 7 letters collapse to A{7}, not seven A's.
    assert structural_pattern("ABCDEFG") == "A{7}"


def test_iso_date_pattern():
    assert structural_pattern("2023-01-15") == "date"


def test_mask_keeps_edges_hides_middle():
    assert mask_value("Acme Trading SRL").startswith("A")
    assert mask_value("Acme Trading SRL").endswith("L")
    assert "*" in mask_value("Acme Trading SRL")
    assert mask_value("xy") == "**"  # short values fully masked


def test_summarize_patterns_orders_by_frequency():
    values = ["AB-01", "CD-02", "EF-03", "single@mail.x"]
    patterns = summarize_patterns(values)
    assert patterns[0] == "AA-99"  # most common structural pattern first


def test_masked_samples_are_distinct_and_bounded():
    samples = masked_samples(["aaa@x.com", "bbb@y.com", "ccc@z.com", "ddd@w.com"], limit=2)
    assert len(samples) == 2
    assert len(set(samples)) == 2
