import asyncio
import os
from contextlib import suppress

try:
    import uvloop
except ImportError:
    uvloop = None

from socket import socket

import pytest

from aiomisc.context import get_context
from aiomisc.thread_pool import ThreadPoolExecutor
from .entrypoint import entrypoint

try:
    from inspect import isasyncgenfunction
except ImportError:
    def isasyncgenfunction(_):
        return False


def isasyncgenerator(func):
    if isasyncgenfunction(func):
        return True
    elif asyncio.iscoroutinefunction(func):
        return False


def pytest_addoption(parser):
    group = parser.getgroup('aiomisc plugin options')
    group.addoption('--aiomisc', action='store_true', default=True,
                    help='Use aiomisc entrypoint for run async tests')

    group.addoption('--aiomisc-debug', action='store_true', default=False,
                    help='Set debug for event loop')

    group.addoption('--aiomisc-pool-size', type=int, default=4,
                    help='Default thread pool size')

    group.addoption('--aiomisc-test-timeout', type=float, default=None,
                    help='Test timeout')


def pytest_fixture_setup(fixturedef):  # type: ignore
    func = fixturedef.func

    is_async_gen = isasyncgenerator(func)

    if is_async_gen is None:
        return

    strip_request = False
    if 'request' not in fixturedef.argnames:
        fixturedef.argnames += ('request',)
        strip_request = True

    def wrapper(*args, **kwargs):  # type: ignore
        if strip_request:
            request = kwargs.pop('request')
        else:
            request = kwargs['request']

        if 'loop' not in request.fixturenames:
            raise Exception('`loop` fixture required')

        event_loop = request.getfixturevalue('loop')

        if not is_async_gen:
            return event_loop.run_until_complete(func(*args, **kwargs))

        gen = func(*args, **kwargs)

        def finalizer():  # type: ignore
            try:
                return event_loop.run_until_complete(gen.__anext__())
            except StopAsyncIteration:  # NOQA
                pass

        request.addfinalizer(finalizer)
        return event_loop.run_until_complete(gen.__anext__())

    fixturedef.func = wrapper


@pytest.fixture
def loop_debug(request):
    return request.config.getoption('--aiomisc-debug')


@pytest.fixture(autouse=True)
def aiomisc_test_timeout(request):
    return request.config.getoption('--aiomisc-test-timeout')


def pytest_pycollect_makeitem(collector, name, obj):  # type: ignore
    if collector.funcnamefilter(name) and asyncio.iscoroutinefunction(obj):
        return list(collector._genfunctions(name, obj))


def pytest_pyfunc_call(pyfuncitem):  # type: ignore
    if not asyncio.iscoroutinefunction(pyfuncitem.function):
        return

    event_loop = pyfuncitem.funcargs.get('loop', None)
    aiomisc_test_timeout = pyfuncitem.funcargs.get(
        'aiomisc_test_timeout', None
    )

    kwargs = {
        arg: pyfuncitem.funcargs[arg]
        for arg in pyfuncitem._fixtureinfo.argnames
    }

    event_loop.run_until_complete(
        asyncio.wait_for(
            pyfuncitem.obj(**kwargs),
            timeout=aiomisc_test_timeout,
            loop=event_loop
        )
    )

    return True


@pytest.fixture
def thread_pool_size(request):
    return request.config.getoption('--aiomisc-pool-size')


@pytest.fixture
def services():
    return []


@pytest.fixture
def default_context():
    return {}


loop_autouse = os.getenv('AIOMISC_LOOP_AUTOUSE', '1') == '1'


@pytest.fixture
def thread_pool_executor():
    return ThreadPoolExecutor


@pytest.fixture
def event_loop_policy():
    if uvloop:
        return uvloop.EventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(autouse=loop_autouse)
def loop(services, loop_debug, default_context,
         thread_pool_size, thread_pool_executor, event_loop_policy):

    asyncio.set_event_loop_policy(event_loop_policy)

    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    pool = thread_pool_executor(thread_pool_size)
    event_loop.set_default_executor(pool)

    try:
        with entrypoint(*services, pool_size=thread_pool_size,
                        debug=loop_debug, loop=event_loop) as event_loop:
            ctx = get_context(event_loop)

            for key, value in default_context.items():
                ctx[key] = value

            yield event_loop
    finally:
        with suppress(Exception):
            pool.shutdown(True)


def get_unused_port():
    sock = socket()
    sock.bind(('', 0))
    port = sock.getsockname()[-1]
    sock.close()
    return port


@pytest.fixture
def aiomisc_unused_port():
    return get_unused_port()
