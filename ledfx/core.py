import asyncio
import logging
import sys
import json
import yaml
import threading
from pathlib import Path
import voluptuous as vol
from concurrent.futures import ThreadPoolExecutor
from ledfx.utils import async_fire_and_forget
from ledfx.http import HttpServer
from ledfx.devices import Devices
from ledfx.effects import Effects
from ledfx.config import load_config, load_config_from_path
from ledfx.config import save_config, save_config_to_path
from ledfx.events import Events, LedFxShutdownEvent

_LOGGER = logging.getLogger(__name__)


class LedFxCore(object):
    def __init__(self, config):
        self.config = config

        # Allows for saving a configuration if specified in the creation of this
        # object.
        self.config_dir = None
        self.config_path = None

        if sys.platform == 'win32':
            self.loop = asyncio.ProactorEventLoop()
        else:
            self.loop = asyncio.get_event_loop()
        executor_opts = {'max_workers': self.config.get('max_workers')}

        self.executor = ThreadPoolExecutor(**executor_opts)
        self.loop.set_default_executor(self.executor)
        self.loop.set_exception_handler(self.loop_exception_handler)

        self.events = Events(self)
        self.http = HttpServer(
            ledfx=self, host=self.config['host'], port=self.config['port'])
        self.exit_code = None

    @classmethod
    def from_config_directory(cls, config_dir):
        config = load_config(config_dir)
        ledfx_core = cls(config)
        ledfx_core.config_dir = config_dir

    @classmethod
    def from_config_path(cls, config_path):
        config = load_config_from_path(config_path)
        ledfx_core = cls(config)
        ledfx_core.config_path = config_path

    def dev_enabled(self):
        return self.config['dev_mode'] == True

    def loop_exception_handler(self, loop, context):
        kwargs = {}
        exception = context.get('exception')
        if exception:
            kwargs['exc_info'] = (type(exception), exception,
                                  exception.__traceback__)

        _LOGGER.error(
            'Exception in core event loop: {}'.format(context['message']),
            **kwargs)

    async def flush_loop(self):
        await asyncio.sleep(0, loop=self.loop)

    def start(self, open_ui=False):
        async_fire_and_forget(self.async_start(open_ui=open_ui), self.loop)

        # Windows does not seem to handle Ctrl+C well so as a workaround
        # register a handler and manually stop the app
        if sys.platform == 'win32':
            import win32api

            def handle_win32_interrupt(sig, func=None):
                self.stop()
                return True

            win32api.SetConsoleCtrlHandler(handle_win32_interrupt, 1)

        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            self.loop.call_soon_threadsafe(self.loop.create_task,
                                           self.async_stop())
            self.loop.run_forever()
        except:
            # Catch all other exceptions and terminate the application. The loop
            # exeception handler will take care of logging the actual error and
            # LedFx will cleanly shutdown.
            self.loop.run_until_complete(self.async_stop(exit_code = -1))
            pass
        finally:
            self.loop.stop()
        return self.exit_code

    async def async_start(self, open_ui=False):
        _LOGGER.info("Starting ledfx")
        await self.http.start()

        self.devices = Devices(self)
        self.effects = Effects(self)

        # TODO: Deferr
        self.devices.create_from_config(self.config['devices'])

        if open_ui:
            import webbrowser
            webbrowser.open(self.http.base_url)

        await self.flush_loop()

    def stop(self, exit_code=0):
        async_fire_and_forget(self.async_stop(exit_code), self.loop)

    async def async_stop(self, exit_code=0):
        if not self.loop:
            return

        print('Stopping LedFx.')

        # Fire a shutdown event and flush the loop
        self.events.fire_event(LedFxShutdownEvent())
        await asyncio.sleep(0, loop=self.loop)

        await self.http.stop()

        # Cancel all the remaining task and wait
        tasks = [task for task in asyncio.Task.all_tasks() if task is not
             asyncio.tasks.Task.current_task()] 
        list(map(lambda task: task.cancel(), tasks))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Save the configuration before shutting down
        if self.config_dir is not None:
            save_config(config=self.config, config_dir=self.config_dir)
        if self.config_path is not None:
            save_config_to_path(config=self.config, config_path=self.config_path)

        await self.flush_loop()
        self.executor.shutdown()
        self.exit_code = exit_code
        self.loop.stop()
