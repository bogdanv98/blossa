"""Log-content analysis: redaction, deterministic SQL builders, the local-provider gate, parsing."""

from blossa.logsense import (
    LOCAL_PROVIDERS,
    TIME_GRAINS,
    Spike,
    TimeBucket,
    bucket_entries_sql,
    build_root_cause_prompt,
    build_spike_report,
    choose_log_table,
    detect_spikes,
    is_local_provider,
    parse_root_cause_response,
    parse_since,
    recent_entries_sql,
    redact_entries,
    redact_text,
    severity_breakdown_sql,
    source_breakdown_sql,
    source_onsets,
    source_time_bucket_sql,
    time_bucket_sql,
    to_buckets,
)
from blossa.models import ConfidenceLevel, LogColumn, LogKind, LogRole, LogTable


def _error_log(owner="APPLOG"):
    return LogTable(
        table="ERROR_LOG",
        owner=owner,
        kind=LogKind.ERROR,
        confidence=ConfidenceLevel.HIGH,
        columns=[
            LogColumn(column="LOG_TIME", role=LogRole.EVENT_TIME),
            LogColumn(column="SEVERITY", role=LogRole.SEVERITY),
            LogColumn(column="MODULE", role=LogRole.SOURCE),
            LogColumn(column="MESSAGE", role=LogRole.MESSAGE),
        ],
    )


# --------------------------------------------------------------- redaction


def test_redact_text_masks_email_keeps_short_codes():
    out = redact_text("ORA-00600 while posting invoice for jane.doe@initech.com")
    assert "jane.doe@initech.com" not in out
    assert "ORA-00600" in out  # short diagnostic codes are preserved


def test_redact_text_masks_long_numbers_keeps_card_tail():
    out = redact_text("charged card 4111 1111 1111 1111 (ending 4321)")
    assert "4111 1111 1111 1111" not in out  # full number redacted
    assert "4321" in out  # last-4 tail is not, on its own, identifying


def test_redact_entries_only_touches_message_column():
    rows = [{"SEVERITY": "ERROR", "MESSAGE": "fail for a@b.com", "MODULE": "X"}]
    out = redact_entries(rows, "MESSAGE")
    assert out[0]["SEVERITY"] == "ERROR" and out[0]["MODULE"] == "X"
    assert "a@b.com" not in out[0]["MESSAGE"]


# --------------------------------------------------------------- SQL builders


def test_severity_breakdown_sql_uses_severity_column():
    sql = severity_breakdown_sql(_error_log())
    assert sql is not None
    assert "SEVERITY" in sql and "COUNT(*)" in sql and "GROUP BY SEVERITY" in sql
    assert "APPLOG.ERROR_LOG" in sql


def test_source_breakdown_filters_to_errors_by_default():
    sql = source_breakdown_sql(_error_log())
    assert "GROUP BY MODULE" in sql
    assert "UPPER(SEVERITY) IN" in sql  # only-errors filter applied
    assert "'FATAL'" in sql


def test_recent_entries_sql_orders_by_time_and_caps():
    sql = recent_entries_sql(_error_log(), limit=25)
    assert "ORDER BY LOG_TIME DESC" in sql
    assert "FETCH FIRST 25 ROWS ONLY" in sql
    assert "SUBSTR(MESSAGE, 1, 2000)" in sql  # message bounded, never the whole CLOB


def test_breakdowns_return_none_without_the_needed_column():
    plain = LogTable(
        table="EVENT_LOG",
        columns=[LogColumn(column="WHEN_TS", role=LogRole.EVENT_TIME),
                 LogColumn(column="BODY", role=LogRole.MESSAGE)],
    )
    assert severity_breakdown_sql(plain) is None  # no severity column
    assert source_breakdown_sql(plain) is None  # no source column
    # recent still works off the event-time + message it does have
    assert "FETCH FIRST" in recent_entries_sql(plain)


# --------------------------------------------------------------- local gate


class _Provider:
    def __init__(self, name):
        self.name = name
        self.model = "m"

    def generate(self, system, user):  # pragma: no cover - not called here
        return "{}"


def test_local_provider_gate():
    assert is_local_provider(_Provider("ollama")) is True
    assert is_local_provider(_Provider("openai_compatible")) is False
    assert "ollama" in LOCAL_PROVIDERS


# --------------------------------------------------------------- choose / parse


def test_choose_prefers_error_log_when_multiple():
    audit = LogTable(table="AUDIT_TRAIL", kind=LogKind.AUDIT, confidence=ConfidenceLevel.HIGH)
    err = _error_log()
    assert choose_log_table([audit, err], None).table == "ERROR_LOG"
    assert choose_log_table([audit, err], "AUDIT_TRAIL").table == "AUDIT_TRAIL"
    assert choose_log_table([audit, err], "NOPE") is None
    assert choose_log_table([], None) is None


def test_prompt_contains_entries_and_parse_clusters():
    entries = [{"SEVERITY": "ERROR", "MESSAGE": "gateway timeout"}]
    prompt = build_root_cause_prompt("ERROR_LOG", entries)
    assert "ERROR_LOG" in prompt and "gateway timeout" in prompt

    rc = parse_root_cause_response(
        "ERROR_LOG",
        '{"clusters":[{"cause":"Payment gateway timeout","count":3,"severity":"ERROR",'
        '"suggested_action":"Add retry/backoff","example":"gateway timeout"}]}',
        sample_size=5,
    )
    assert rc.sample_size == 5 and len(rc.clusters) == 1
    c = rc.clusters[0]
    assert c.cause == "Payment gateway timeout" and c.count == 3
    assert c.suggested_action == "Add retry/backoff"


def test_parse_handles_garbage():
    rc = parse_root_cause_response("ERROR_LOG", "sorry no json", sample_size=2)
    assert rc.clusters == [] and rc.note


# --------------------------------------------------------------- time-bucket SQL


def test_time_bucket_sql_hour_groups_and_counts():
    sql = time_bucket_sql(_error_log(), grain="hour")
    assert sql is not None
    assert "COUNT(*) AS ENTRIES" in sql
    assert "TRUNC(CAST(LOG_TIME AS DATE), 'HH24')" in sql  # zeroes the minutes safely
    assert "GROUP BY" in sql and "ORDER BY 1" in sql
    assert "UPPER(SEVERITY) IN" in sql  # errors-only by default
    assert "APPLOG.ERROR_LOG" in sql


def test_time_bucket_sql_all_severities_and_since_window():
    sql = time_bucket_sql(_error_log(), grain="day", only_errors=False, since_hours=48)
    assert "UPPER(SEVERITY) IN" not in sql  # no error filter when all severities
    assert "NUMTODSINTERVAL(48, 'HOUR')" in sql  # since window applied
    assert "TRUNC(CAST(LOG_TIME AS DATE), 'DD')" in sql


def test_time_bucket_sql_none_without_timestamp_or_bad_grain():
    no_time = LogTable(table="X", columns=[LogColumn(column="MSG", role=LogRole.MESSAGE)])
    assert time_bucket_sql(no_time) is None
    assert time_bucket_sql(_error_log(), grain="week") is None


def test_source_time_bucket_sql_groups_by_source():
    sql = source_time_bucket_sql(_error_log())
    assert sql is not None
    assert "MODULE AS SOURCE" in sql and "GROUP BY" in sql and "MODULE" in sql
    # no source column -> None
    no_src = LogTable(table="X", columns=[
        LogColumn(column="TS", role=LogRole.EVENT_TIME),
        LogColumn(column="MSG", role=LogRole.MESSAGE)])
    assert source_time_bucket_sql(no_src) is None


def test_bucket_entries_sql_filters_to_one_bucket():
    sql = bucket_entries_sql(_error_log(), "2026-06-29 00:00", grain="hour", limit=10)
    assert "= '2026-06-29 00:00'" in sql
    assert "FETCH FIRST 10 ROWS ONLY" in sql
    assert "SUBSTR(MESSAGE, 1, 2000)" in sql


def test_bucket_entries_sql_escapes_quotes():
    # The label is server-generated, but the filter still single-quote-escapes defensively.
    sql = bucket_entries_sql(_error_log(), "x' OR '1'='1", grain="hour")
    assert "''" in sql and "OR '1'='1" not in sql.replace("''", "")


# --------------------------------------------------------------- spike math


def test_detect_spikes_flags_bucket_towering_over_median():
    buckets = [TimeBucket(bucket=f"h{i}", count=1) for i in range(10)]
    buckets.append(TimeBucket(bucket="burst", count=18))
    spikes, baseline = detect_spikes(buckets, factor=3.0, min_count=5)
    assert baseline == 1.0
    assert [s.bucket for s in spikes] == ["burst"]
    assert spikes[0].count == 18 and spikes[0].ratio == 18.0


def test_detect_spikes_min_count_floor_kills_tiny_noise():
    # 2 vs a baseline of 0.x would be >=3x but is below the absolute floor -> not a spike.
    buckets = [TimeBucket(bucket="a", count=0), TimeBucket(bucket="b", count=0),
               TimeBucket(bucket="c", count=2)]
    spikes, _ = detect_spikes(buckets, factor=3.0, min_count=5)
    assert spikes == []


def test_detect_spikes_empty():
    assert detect_spikes([]) == ([], 0.0)


def test_to_buckets_tolerates_lowercase_columns():
    rows = [{"bucket": "2026-06-29 00:00", "entries": 19}]
    buckets = to_buckets(rows)
    assert buckets == [TimeBucket(bucket="2026-06-29 00:00", count=19)]


def test_source_onsets_catches_new_source_against_overall_baseline():
    rows = [
        {"BUCKET": "h1", "SOURCE": "ORDER_API", "ENTRIES": 1},
        {"BUCKET": "h2", "SOURCE": "PAYMENT_GATEWAY", "ENTRIES": 18},
        {"BUCKET": "h3", "SOURCE": "PAYMENT_GATEWAY", "ENTRIES": 12},
    ]
    onsets = source_onsets(rows, baseline=1.0, factor=3.0, min_count=5)
    # earliest spiking bucket per source, against overall baseline (not the source's own history)
    assert len(onsets) == 1
    assert onsets[0].source == "PAYMENT_GATEWAY" and onsets[0].bucket == "h2"


def test_build_spike_report_end_to_end():
    bucket_rows = [{"BUCKET": f"2026-06-29 {h:02d}:00", "ENTRIES": 1} for h in range(1, 6)]
    bucket_rows.append({"BUCKET": "2026-06-29 00:00", "ENTRIES": 19})
    source_rows = [
        {"BUCKET": "2026-06-29 00:00", "SOURCE": "PAYMENT_GATEWAY.CHARGE", "ENTRIES": 18},
        {"BUCKET": "2026-06-29 01:00", "SOURCE": "ORDER_API.SUBMIT_ORDER", "ENTRIES": 1},
    ]
    report = build_spike_report(_error_log(), bucket_rows, source_rows, grain="hour")
    assert report.has_spikes
    assert report.baseline == 1.0
    assert [s.bucket for s in report.spikes] == ["2026-06-29 00:00"]
    assert report.total == 24 and report.bucket_count == 6
    assert report.onsets and report.onsets[0].source == "PAYMENT_GATEWAY.CHARGE"


def test_build_spike_report_quiet_window_has_note_no_spikes():
    rows = [{"BUCKET": f"h{i}", "ENTRIES": 2} for i in range(6)]
    report = build_spike_report(_error_log(), rows)
    assert not report.has_spikes and "normal range" in report.note


def test_build_spike_report_empty_window():
    report = build_spike_report(_error_log(), [])
    assert report.buckets == [] and "No entries" in report.note


# --------------------------------------------------------------- parse_since


def test_parse_since_variants():
    assert parse_since("48h") == 48
    assert parse_since("7d") == 168
    assert parse_since("12") == 12  # bare number = hours
    assert parse_since(None) is None
    assert parse_since("soon") is None
    assert parse_since("") is None


def test_time_grains_exposed():
    assert "hour" in TIME_GRAINS and "day" in TIME_GRAINS


def test_spike_model_defaults():
    assert Spike(bucket="b").count == 0
