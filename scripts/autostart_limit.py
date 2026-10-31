"""纯离线计划任务 XML 校验；不查询、注册或启动任务。"""
import xml.etree.ElementTree as ET
from pathlib import Path

NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
ET.register_namespace('', NS)


def bind_test_workspace_xml(xml,workspace):
    """只把已知登录动作绑定隔离测试目录，不改触发器、时限或其它设置。"""
    workspace=Path(workspace)
    if not workspace.is_absolute() or any(c in str(workspace) for c in ('"','\r','\n')):
        raise ValueError('测试目录须为明确绝对路径')
    root=ET.fromstring(xml);args=root.find(f'{{{NS}}}Actions/{{{NS}}}Exec/{{{NS}}}Arguments')
    base='-m app.main --minimized';desired=base+' --test-workspace "'+str(workspace)+'"'
    if args is None or args.text not in (base,desired):raise ValueError('任务动作不是已知启动入口')
    args.text=base;_parse(ET.tostring(root,encoding='unicode'));args.text=desired
    return ET.tostring(root,encoding='unicode')


def _parse(xml):
    root = ET.fromstring(xml)
    actions = root.find(f'{{{NS}}}Actions')
    expected = {'Command': r'E:\order-bot\.venv\Scripts\python.exe',
                'Arguments': '-m app.main --minimized', 'WorkingDirectory': r'E:\order-bot'}
    if actions is None or len(actions) != 1 or actions[0].tag != f'{{{NS}}}Exec':
        raise ValueError('任务动作不符合已知路径')
    for name, value in expected.items():
        if actions[0].findtext(f'{{{NS}}}{name}') != value:
            raise ValueError('任务动作不符合已知路径')
    return root


def unlimited_task_xml(xml):
    root = _parse(xml)
    settings = root.find(f'{{{NS}}}Settings')
    if settings is None:
        raise ValueError('缺少任务设置')
    limit = settings.find(f'{{{NS}}}ExecutionTimeLimit')
    if limit is None:
        limit = ET.SubElement(settings, f'{{{NS}}}ExecutionTimeLimit')
    limit.text = 'PT0S'
    return ET.tostring(root, encoding='unicode')


def verify_limit_only(before, after):
    old, new = _parse(before), _parse(after)
    if new.findtext(f'{{{NS}}}Settings/{{{NS}}}ExecutionTimeLimit') != 'PT0S':
        raise ValueError('执行时限尚未设为无限')

    def signature(node):
        return (node.tag, sorted(node.attrib.items()), (node.text or '').strip(),
                sorted((signature(child) for child in node
                        if child.tag != f'{{{NS}}}ExecutionTimeLimit'), key=repr))

    if signature(old) != signature(new):
        raise ValueError('除执行时限外存在其它变更')
