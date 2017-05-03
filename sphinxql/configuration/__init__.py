import subprocess
import time
import os

from .configurators import Configurator

indexes_configurator = Configurator()


def call_process(args, fail_silently=False):
    _fix_sphinx_binary_path(args)

    p = subprocess.Popen(args,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,
                         cwd=indexes_configurator.sphinx_path)
    p.wait()

    out = p.stdout.read().decode('UTF-8')
    if p.returncode != 0 and not fail_silently:
        raise Exception('Process `{0}` failed.\n\n{1}'.format(' '.join(args), out))
    return out


def call_process_no_wait(args, output=None):
    _fix_sphinx_binary_path(args)
    return subprocess.Popen(args, stdout=output, cwd=indexes_configurator.sphinx_path)


def _fix_sphinx_binary_path(args):
    if indexes_configurator.sphinx_bin_path:
        args[0] = os.path.join(indexes_configurator.sphinx_bin_path, args[0])


def index():
    _make_index_directory()
    return call_process(['indexer', '--all', '--config',
                         indexes_configurator.sphinx_file])


def reindex():
    _make_index_directory()
    out = call_process(['indexer', '--all', '--rotate', '--config',
                        indexes_configurator.sphinx_file])
    # it is not immediately available; wait a bit
    # see http://sphinxsearch.com/bugs/view.php?id=2350
    time.sleep(0.5)
    return out


def _make_index_directory():
    if not os.path.isdir(indexes_configurator.index_path):
        os.makedirs(indexes_configurator.index_path)


def start(output=None):
    return call_process_no_wait(['searchd', '--config', indexes_configurator.sphinx_file], output)


def stop(silent_fail=False):
    return call_process(['searchd', '--stopwait', '--config', indexes_configurator.sphinx_file], silent_fail)


def statistics():
    return call_process(
        ['indextool', '--dumpheader', 'query_authorindex', '--config', indexes_configurator.sphinx_file],
        fail_silently=True)


def restart():
    stop(silent_fail=True)
    start()
