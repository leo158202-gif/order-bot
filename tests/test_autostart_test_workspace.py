import xml.etree.ElementTree as ET
import pytest
from scripts.autostart_limit import bind_test_workspace_xml,NS


def original():
    return f'''<Task xmlns="{NS}"><Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers><Settings><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings><Actions><Exec><Command>E:\\order-bot\\.venv\\Scripts\\python.exe</Command><Arguments>-m app.main --minimized</Arguments><WorkingDirectory>E:\\order-bot</WorkingDirectory></Exec></Actions></Task>'''


def test_test_workspace_binding_preserves_trigger_and_unlimited_runtime(tmp_path):
    before=original();work=tmp_path/'test run'
    after=bind_test_workspace_xml(before,work)
    old,new=ET.fromstring(before),ET.fromstring(after)
    assert new.findtext(f'{{{NS}}}Actions/{{{NS}}}Exec/{{{NS}}}Arguments')==f'-m app.main --minimized --test-workspace "{work}"'
    assert ET.tostring(old.find(f'{{{NS}}}Triggers'))==ET.tostring(new.find(f'{{{NS}}}Triggers'))
    assert ET.tostring(old.find(f'{{{NS}}}Settings'))==ET.tostring(new.find(f'{{{NS}}}Settings'))
    assert bind_test_workspace_xml(after,work)==after


def test_unknown_existing_action_is_not_rewritten(tmp_path):
    with pytest.raises(ValueError):bind_test_workspace_xml(original().replace('-m app.main --minimized','other.exe'),tmp_path/'test')
