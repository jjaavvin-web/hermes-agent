"""Decision-only packet6 controls with actual per-session bypass active."""
import pytest
from tools import approval as a

@pytest.mark.parametrize('command',[
    'shut$()down -h now','re${EMPTY}boot','mkfs$().ext4 /dev/sda',
    'rm -rf ~/.her$()mes','rm -rf /ho$()me',
])
def test_real_session_bypass_cannot_pass_spliced_hardline(command,monkeypatch):
    key='packet6-isolated-floor'
    token=a.set_current_session_key(key)
    a.enable_session_yolo(key)
    monkeypatch.setattr(a,'_get_approval_config',lambda:{'mode':'off','deny':[]})
    try:
        assert a.is_current_session_yolo_enabled()
        result=a.check_all_command_guards(command,'local')
        assert result['approved'] is False and result.get('hardline') is True
    finally:
        a.disable_session_yolo(key);a.reset_current_session_key(token)

@pytest.mark.parametrize('command',[
    'echo "$(date) shutdown"','echo "$(date) reboot"',
    'echo \'re$()boot\'','printf "%s" "shut$()down"',
])
def test_quoted_argument_stays_data(command):
    assert a.detect_hardline_command(command)[0] is False

@pytest.mark.parametrize('command',['su$()do -n id -u','su${EMPTY}do -n id -u'])
def test_deny_stays_before_real_session_bypass(command,monkeypatch):
    key='packet6-isolated-deny';token=a.set_current_session_key(key)
    a.enable_session_yolo(key)
    monkeypatch.setattr(a,'_get_approval_config',lambda:{'mode':'off','deny':['sudo *']})
    try:
        assert a.is_current_session_yolo_enabled()
        assert a.check_all_command_guards(command,'local')['approved'] is False
    finally:
        a.disable_session_yolo(key);a.reset_current_session_key(token)

def test_unknown_substitution_not_resolved_to_empty():
    assert a._literal_command_substitution_output('date') is None
    assert a._delete_provably_empty_expansions('re$(date)boot')=='re$(date)boot'
