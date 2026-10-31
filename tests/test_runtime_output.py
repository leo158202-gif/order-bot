"""Exercise native output in disposable Windows processes, never the resident."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest


pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows CRT/handles')
ROOT = Path(__file__).resolve().parents[1]
WRITERS = {
    'crt_stdout': "assert crt._write(1, payload, len(payload)) == len(payload)",
    'crt_stderr': "assert crt._write(2, payload, len(payload)) == len(payload)",
    'crt_vfprintf': "assert crt.__stdio_common_vfprintf(0, crt.__acrt_iob_func(1), b'%s', None, ctypes.byref(ctypes.c_char_p(payload))) == len(payload); assert crt.fflush(None) == 0",
    'crt_fwrite': "assert crt.fwrite(payload, 1, len(payload), crt.__acrt_iob_func(2)) == len(payload); assert crt.fflush(None) == 0",
    'win32_stdout': "assert kernel.WriteFile(kernel.GetStdHandle(-11), payload, len(payload), ctypes.byref(count), None); assert count.value == len(payload)",
    'win32_stderr': "assert kernel.WriteFile(kernel.GetStdHandle(-12), payload, len(payload), ctypes.byref(count), None); assert count.value == len(payload)",
    'python_stdout': "sys.stdout.write(payload.decode()); sys.stdout.flush()",
    'python_stderr': "sys.stderr.write(payload.decode()); sys.stderr.flush()",
}
NATIVE = '''
import ctypes
crt = ctypes.CDLL('ucrtbase')
crt._write.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
crt.__stdio_common_vfprintf.argtypes = [ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
crt.__acrt_iob_func.argtypes = [ctypes.c_uint]
crt.__acrt_iob_func.restype = ctypes.c_void_p
crt.fwrite.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p]
crt.fwrite.restype = ctypes.c_size_t
crt.fflush.argtypes = [ctypes.c_void_p]
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetStdHandle.argtypes = [ctypes.c_int]
kernel.GetStdHandle.restype = ctypes.c_void_p
kernel.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
count = ctypes.c_uint()
payload = b'x' * (256 * 1024)
'''


def spawn(code, tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    return subprocess.Popen([sys.executable, '-c', textwrap.dedent(code)], cwd=tmp_path,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.mark.parametrize('writer', ['crt_stdout', 'win32_stderr'])
def test_undrained_native_output_reproduces_block(writer, tmp_path):
    ready = tmp_path / 'ready'
    child = spawn(NATIVE + "\nfrom pathlib import Path; Path('ready').write_text('ok')\n" + WRITERS[writer], tmp_path)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), 'control child did not reach the native write'
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=1)
    finally:
        child.kill()
        child.communicate(timeout=5)


@pytest.mark.parametrize('writer', WRITERS)
def test_main_runtime_detaches_all_output_before_sdk_work(writer, tmp_path):
    # Real logging setup, no DB, SDK, service, scheduler or app server is started.
    code = '''
import logging
import sys
from pathlib import Path
from app import main
sys.argv = ['test', '--no-window']
main._acquire_single_instance = lambda: False
main.main()
logging.getLogger('proof').warning('file logging survives output isolation')
from app.logbus import BUS
assert any('file logging survives' in line for line in BUS.history)
'''
    child = spawn(code + NATIVE + WRITERS[writer] + "\nPath('done').write_text('ok')", tmp_path)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate(timeout=5)
        pytest.fail(f'{writer} blocked on inherited output after main startup')
    out, err = child.communicate(timeout=5)
    assert child.returncode == 0, (out, err)
    assert (tmp_path / 'done').read_text() == 'ok'
    assert 'file logging survives' in (tmp_path / 'data/logs/order-bot.log').read_text(encoding='utf-8')
    assert not out and not err


@pytest.mark.parametrize('arg', ['--version', '--help'])
def test_cli_information_remains_visible(arg, tmp_path):
    child = spawn(f"import sys; from app.main import main; sys.argv = ['test', {arg!r}]; main()", tmp_path)
    out, err = child.communicate(timeout=10)
    assert child.returncode == 0, err
    assert b'order-bot' in out


def test_output_isolation_module_is_in_launch_proof():
    from scripts.resident_reload_check import SOURCES
    assert 'app/runtime_output.py' in SOURCES


@pytest.mark.parametrize('free_descriptor', [1, 2])
def test_missing_python_streams_low_descriptor_and_repeat_are_safe(free_descriptor, tmp_path):
    code = f'''
import os
import sys
from pathlib import Path
from app.runtime_output import isolate_runtime_output
sys.stdout = sys.stderr = sys.__stdout__ = sys.__stderr__ = None
os.close({free_descriptor})
isolate_runtime_output()
isolate_runtime_output()
'''
    child = spawn(code + NATIVE + '\n'.join(WRITERS.values()) + "\nPath('done').write_text('ok')", tmp_path)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate(timeout=5)
        pytest.fail('no-console/repeated isolation left a blocked output')
    out, err = child.communicate(timeout=5)
    assert child.returncode == 0, (out, err)
    assert (tmp_path / 'done').read_text() == 'ok'


def test_isolation_failure_is_logged_before_services_start(tmp_path):
    code = '''
from pathlib import Path
import sys
from app import main, runtime_output
sys.argv = ['test', '--no-window']
def fail():
    raise OSError('synthetic output failure')
runtime_output.isolate_runtime_output = fail
main._acquire_single_instance = lambda: (_ for _ in ()).throw(AssertionError('must not continue'))
try:
    main.main()
except OSError:
    Path('done').write_text('failed safely')
'''
    child = spawn(code, tmp_path)
    out, err = child.communicate(timeout=10)
    assert child.returncode == 0, (out, err)
    assert (tmp_path / 'done').read_text() == 'failed safely'
    assert 'synthetic output failure' in (tmp_path / 'data/logs/order-bot.log').read_text(encoding='utf-8')
