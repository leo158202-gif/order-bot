import pytest
from scripts.autostart_limit import unlimited_task_xml, verify_limit_only

XML = '''<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings><ExecutionTimeLimit>PT72H</ExecutionTimeLimit><Enabled>true</Enabled></Settings><Triggers><LogonTrigger/></Triggers><Actions><Exec><Command>E:\\order-bot\\.venv\\Scripts\\python.exe</Command><Arguments>-m app.main --minimized</Arguments><WorkingDirectory>E:\\order-bot</WorkingDirectory></Exec></Actions></Task>'''


def test_limit_only():
    changed = unlimited_task_xml(XML)
    assert 'PT0S' in changed
    verify_limit_only(XML, changed)


def test_default_limit():
    old = XML.replace('<ExecutionTimeLimit>PT72H</ExecutionTimeLimit>', '')
    verify_limit_only(old, unlimited_task_xml(old))


@pytest.mark.parametrize('old,new', [('LogonTrigger', 'BootTrigger'), ('--minimized', '--no-window'), ('<Enabled>true', '<Enabled>false')])
def test_other_change_rejected(old, new):
    with pytest.raises(ValueError):
        verify_limit_only(XML, unlimited_task_xml(XML).replace(old, new))


def test_wrong_action_rejected():
    with pytest.raises(ValueError):
        unlimited_task_xml(XML.replace('python.exe', 'other.exe'))
