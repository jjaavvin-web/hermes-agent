"""Real call-site shaped regression fixtures, no live DB or notification calls."""
import importlib.util
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
PATH = Path(os.environ.get('SLO_EXPORTER_TEST_TARGET', ROOT/'scripts/observability/slo_exporter.py'))
spec = importlib.util.spec_from_file_location('slo_event_repair', PATH)
assert spec is not None and spec.loader is not None
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def line(second, body, pid=1):
    return f'2026-09-12T05:11:{second:02d}+00:00 host python[{pid}]: {body}'


PAIRS = [
    line(10, '❌ Non-retryable client error (HTTP None). Aborting.'),
    line(10, '• Configure a fallback provider so future blocks route automatically:'),
    line(10, 'hermes fallback add (interactive picker)'),
    line(10, 'ERROR agent.conversation_loop: Non-retryable client error: provider policy rejection'),
    line(20, '❌ Non-retryable client error (HTTP None). Aborting.'),
    line(20, '• Configure a fallback provider so future blocks route automatically:'),
    line(20, 'hermes fallback add (interactive picker)'),
    line(20, 'ERROR agent.conversation_loop: Non-retryable client error: provider policy rejection'),
]


def test_double_output_and_help_text():
    c=m.parse_journal_counts(PAIRS)
    assert c.turn_error_events == 2
    assert c.fallback_events == 0
    assert c.error_events >= 2  # diagnostics not erased


def test_legacy_session_recovery_not_provider_fallback():
    c=m.parse_journal_counts([line(1,'WARNING gateway.run: Unclean gateway shutdown detected but zero in-flight crash markers were found; running legacy recent-session suspension fallback once')])
    assert c.fallback_events == 0


def test_real_activation_counts_once_not_console_or_followup():
    c=m.parse_journal_counts([
        line(1,'INFO agent.chat_completion_helpers: Fallback activated: primary → secondary (provider)'),
        line(1,'↻ Switched to fallback: secondary (provider)'),
        line(1,'INFO agent.conversation_loop: Fallback activated after empty responses: now using secondary on provider'),
        line(2,'INFO agent.chat_completion_helpers: Fallback activated: secondary → tertiary (provider)'),
    ])
    assert c.fallback_events == 2


def test_standalone_console_failure_retained():
    assert m.parse_journal_counts([PAIRS[0]]).turn_error_events == 1


def test_two_failures_same_second_not_time_collapsed():
    assert m.parse_journal_counts([PAIRS[0],PAIRS[0],PAIRS[3],PAIRS[3]]).turn_error_events == 2


def test_pair_different_process_not_merged():
    assert m.parse_journal_counts([PAIRS[0],line(10,'ERROR agent.conversation_loop: Non-retryable client error: refusal',pid=2)]).turn_error_events == 2


def test_bucket_boundary_pair_consistent():
    lines=[line(59,'❌ Non-retryable client error (HTTP None). Aborting.').replace('05:11:', '05:14:'),line(0,'ERROR agent.conversation_loop: Non-retryable client error: refusal').replace('05:11:', '05:15:')]
    assert m.parse_journal_counts(lines).turn_error_events == 1
    assert sum(x['turn_error_events'] for x in m.build_bucket_series([],lines)) == 1


def test_evidence_ids_stable_distinct_and_no_content():
    a=m.event_evidence(PAIRS)
    b=m.event_evidence(PAIRS+[line(40,'ordinary diagnostic')])
    assert a == b
    assert len(a['turn_error_rate']) == 2
    assert len(set(a['turn_error_rate'])) == 2
    assert all(len(x)==64 for x in a['turn_error_rate'])
    assert a['fallback_trigger_rate'] == []


def test_error_payload_cannot_pair_with_itself():
    x=line(1,'ERROR agent.conversation_loop: Non-retryable client error: ❌ Non-retryable client error (HTTP None). Aborting.')
    assert m.parse_journal_counts([x]).turn_error_events == 1
    assert len(m.event_evidence([x])['turn_error_rate']) == 1


def test_earlier_logger_does_not_consume_later_console():
    assert m.parse_journal_counts([PAIRS[3],PAIRS[0]]).turn_error_events == 2
