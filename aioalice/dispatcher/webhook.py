import asyncio
import logging
import functools
from aiohttp import web
from ..utils import json, generate_json_payload
from ..types import AliceRequest, AliceResponse, Response

DEFAULT_WEB_PATH = '/alicewh/'
ALICE_DISPATCHER_KEY = 'ALICE_DISPATCHER'
ERROR_RESPONSE_KEY = 'ALICE_ERROR_RESPONSE'

DEFAULT_ERROR_RESPONSE_TEXT = 'Server error. Developer has to check logs.'
# Max time to response to API is 1.5s
# with server on Aruba (Italy) 1.2s is a critical timeout for whole processing
# NOTE that this timeout can help only if using non-blocking IO
# in e.g use asyncio.sleep instead of time.sleep, aiohttp instead of requests, etc
# Whole processing usually takes from 0.0004 до 0.001 (depends on system IO),
# but Yandex starts countdown as user asks a question, request processing takes some time
RESPONSE_TIMEOUT = 1.2


class WebhookRequestHandler(web.View):
    """
    Simple Wehhook request handler for aiohttp web server.

    You need to register that in app:

    .. code-block:: python3

        app.router.add_route('*', '/your/webhook/path', WebhookRequestHadler, name='webhook_handler')

    But first you need to configure application for getting Dispatcher instance from request handler!
    It must always be with key 'ALICE_DISPATCHER'

    .. code-block:: python3

        dp = Dispatcher()
        app['ALICE_DISPATCHER'] = dp

    """

    def get_dispatcher(self):
        """
        Get Dispatcher instance from environment
        """
        return self.request.app[ALICE_DISPATCHER_KEY]

    async def parse_request(self):
        """
        Read request from stream and deserialize it.
        :return: :class:`aioalice.types.AliceRequest`
        """
        data = await self.request.json()
        return AliceRequest(**data)

    async def process_request(self, request):
        """
        You have to respond in less than 1.5 seconds to webhook.

        So... If you process longer than 1.2 (RESPONSE_TIMEOUT) seconds
        webhook automatically respond with FALLBACK VALUE (ERROR_RESPONSE_KEY)

        :param request:
        :return:
        """
        dispatcher = self.get_dispatcher()
        loop = dispatcher.loop

        # Analog of `asyncio.wait_for` but without cancelling task
        waiter = loop.create_future()
        timeout_handle = loop.call_later(RESPONSE_TIMEOUT, asyncio.tasks._release_waiter, waiter)
        done_cb = functools.partial(asyncio.tasks._release_waiter, waiter)

        fut = asyncio.ensure_future(dispatcher.process_request(request), loop=loop)
        fut.add_done_callback(done_cb)

        try:
            try:
                await waiter
            except asyncio.futures.CancelledError:
                fut.remove_done_callback(done_cb)
                fut.cancel()
                raise

            if fut.done():
                return fut.result()
            else:
                fut.remove_done_callback(done_cb)
                fut.add_done_callback(self.warn_slow_process(request))
        finally:
            timeout_handle.cancel()

    def warn_slow_process(self, request):
        """
        Wrapper for slow requests warning
        """

        def slow_request_processor(task):
            """
            Handle response after 1.2 sec (RESPONSE_TIMEOUT)

            :param task:
            :return:
            """
            logging.warning('Long request processing detected.\n'
                            f'Request was {request}\n'
                            f'You have to process request in {RESPONSE_TIMEOUT}s\n'
                            'request was automatically responded with `ERROR_RESPONSE_KEY`')

            dispatcher = self.get_dispatcher()
            loop = dispatcher.loop

            try:
                result = task.result()
            except Exception as e:
                logging.info('Slow request processor raised an error, passing to errors_handlers')
                loop.create_task(dispatcher.errors_handlers.notify(dispatcher, request, e))
            else:
                logging.warning(f'Result is {result}')

        return slow_request_processor

    def default_error_response(self, alice_request):
        """
        Default error response will be called on timeout
        if processing of the request will take more than 1.2s (RESPONSE_TIMEOUT)

        :param result: dict or AliceRequest
        :return: AliceResponse
        """
        default_response = self.request.app[ERROR_RESPONSE_KEY]
        response = alice_request.response(default_response)
        return generate_json_payload(**response.to_json())

    def get_response(self, result, request):
        """
        Make response object from result.

        :param result: dict or AliceResponse
        :return:
        """
        if isinstance(result, AliceResponse):
            return generate_json_payload(**result.to_json())
        if result is None:
            logging.critical('Got `None` instead of a response!\n'
                             f'Generating default error response based on {request}')
            return self.default_error_response(request)
        if not isinstance(result, dict):
            # If result is not a dict, it may cause an error. Warn developer
            logging.warning(f'Result expected `AliceResponse` or dict, got {type(result)} ({result})')
        return result

    async def post(self):
        """
        Process POST response

        :return: :class:`aiohttp.web.Response`
        """
        request = await self.parse_request()
        result = await self.process_request(request)
        # request has to be passed to generate fallback value
        # if None is returned from process_request or on timeout
        response = self.get_response(result, request)
        return web.json_response(response, dumps=json.dumps)


def configure_app(dispatcher, app: web.Application, path=DEFAULT_WEB_PATH,
                  default_response_or_text=DEFAULT_ERROR_RESPONSE_TEXT):
    """
    You can prepare web.Application for working with webhook handler.

    :param dispatcher: Dispatcher instance
    :param app: :class:`aiohttp.web.Application`
    :param path: Path to your webhook.
    :default_response_or_text: `aioalice.types.Response` OR text to answer user on fail or timeout
    :return:
    """
    app.router.add_route('*', path, WebhookRequestHandler, name='alice_webhook_handler')
    app[ALICE_DISPATCHER_KEY] = dispatcher
    # Prepare default Response
    if isinstance(default_response_or_text, Response):
        app[ERROR_RESPONSE_KEY] = default_response_or_text
    elif isinstance(default_response_or_text, str):
        app[ERROR_RESPONSE_KEY] = Response(default_response_or_text)
    else:
        response_text = str(default_response_or_text)
        app[ERROR_RESPONSE_KEY] = Response(response_text)
        logging.warning('Automatically converted default_response_or_text to str\n'
                        f'It\'ll be {response_text}\n'
                        'Consider using string or `aioalice.types.Response` next time')


def get_new_configured_app(dispatcher, path=DEFAULT_WEB_PATH,
                           default_response_or_text=DEFAULT_ERROR_RESPONSE_TEXT):
    """
    Create new :class:`aiohttp.web.Application` and configure it.

    :param dispatcher: Dispatcher instance
    :param path: Path to your webhook.
    :default_response_or_text: `aioalice.types.Response` OR text to answer user on fail or timeout
    :return:
    """
    app = web.Application()
    configure_app(dispatcher, app, path, default_response_or_text)
    return app
