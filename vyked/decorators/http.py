from asyncio import iscoroutine, coroutine, wait_for, TimeoutError, shield
from functools import wraps
from vyked import HTTPServiceClient, HTTPService
from ..exceptions import VykedServiceException
from aiohttp.web import Response
from ..utils.stats import Stats, Aggregator
from ..utils.common_utils import json_file_to_dict, valid_timeout, X_REQUEST_ID
import logging
import setproctitle
import socket
import json
import time
import traceback
from ..config import CONFIG
from ..shared_context import SharedContext
_http_timeout = CONFIG.HTTP_TIMEOUT

def make_request(func, self, args, kwargs, method):
    params = func(self, *args, **kwargs)
    entity = params.pop('entity', None)
    app_name = params.pop('app_name', None)
    self = params.pop('self')
    response = yield from self._send_http_request(app_name, method, entity, params)
    return response


def get_decorated_fun(method, path, required_params, timeout, suppressed_errors):
    def decorator(func):
        @wraps(func)
        def f(self, *args, **kwargs):
            if isinstance(self, HTTPServiceClient):
                return (yield from make_request(func, self, args, kwargs, method))
            elif isinstance(self, HTTPService):
                Stats.http_stats['total_requests'] += 1
                if required_params is not None:
                    req = args[0]
                    if req.method in ["POST", "DELETE", "PUT", "PATCH"]:
                        query_params = yield from req.post()
                        if not query_params:
                            query_params = yield from req.json()
                    elif req.method == "GET":
                        query_params = req.GET
                    params = required_params
                    if not isinstance(required_params, list):
                        params = [required_params]
                    missing_params = list(filter(lambda x: x not in query_params, params))
                    if len(missing_params) > 0:
                        res_d = {'error': 'Required params {} not found'.format(','.join(missing_params))}
                        Stats.http_stats['total_responses'] += 1
                        Aggregator.update_stats(endpoint=func.__name__, status=400, success=False,
                                                server_type='http', time_taken=0, process_time_taken=0)
                        return Response(status=400, content_type='application/json', body=json.dumps(res_d).encode())

                t1 = time.time()
                tp1 = time.process_time()

                # Support for multi request body encodings
                req = args[0]

                try:
                    yield from req.json()
                except:
                    pass
                else:
                    req.post = req.json

                wrapped_func = func
                success = True
                _logger = logging.getLogger()
                api_timeout = _http_timeout

                if valid_timeout(timeout):
                    api_timeout = timeout

                if not iscoroutine(func):
                    wrapped_func = coroutine(func)

                tracking_id = SharedContext.get(X_REQUEST_ID)

                try:
                    result = yield from wait_for(shield(wrapped_func(self, *args, **kwargs)), api_timeout)

                except TimeoutError as e:
                    Stats.http_stats['timedout'] += 1
                    status = 'timeout'
                    success = False
                    _logger.exception("HTTP request had a timeout for method %s", func.__name__)
                    timeout_log = {
                        'time_taken': api_timeout,
                        'type': 'http',
                        'hostname': socket.gethostbyname(socket.gethostname()),
                        'service_name': self._service_name,
                        'endpoint': func.__name__,
                        'api_execution_threshold_exceed': True,
                        'api_timeout': True,
                        X_REQUEST_ID: tracking_id
                    }

                    logging.getLogger('stats').info(timeout_log)
                    raise e

                except VykedServiceException as e:
                    Stats.http_stats['total_responses'] += 1
                    status = 'handled_exception'
                    _logger.info('Handled exception %s for method %s ', e.__class__.__name__, func.__name__)
                    raise e

                except Exception as e:
                    status = 'unhandled_exception'
                    success = False
                    if suppressed_errors:
                        for _error in suppressed_errors:
                            if isinstance(e, _error):
                                status = 'handled_exception'
                                raise e
                    Stats.http_stats['total_errors'] += 1
                    _logger.exception('Unhandled exception %s for method %s ', e.__class__.__name__, func.__name__)
                    _stats_logger = logging.getLogger('stats')
                    d = {"exception_type": e.__class__.__name__, "method_name": func.__name__, "message": str(e),
                         "service_name": self._service_name, "hostname": socket.gethostbyname(socket.gethostname()),
                         X_REQUEST_ID: tracking_id}
                    _stats_logger.info(dict(d))
                    _exception_logger = logging.getLogger('exceptions')
                    d["message"] = traceback.format_exc()
                    _exception_logger.info(dict(d))
                    raise e
                    
                else:
                    t2 = time.time()
                    tp2 = time.process_time()
                    hostname = socket.gethostname()
                    service_name = '_'.join(setproctitle.getproctitle().split('_')[:-1])
                    status = result.status

                    logd = {
                        'status': result.status,
                        'time_taken': int((t2 - t1) * 1000),
                        'process_time_taken': int((tp2-tp1) * 1000),
                        'type': 'http',
                        'hostname': hostname,
                        'service_name': service_name,
                        'endpoint': func.__name__,
                        'api_execution_threshold_exceed': False,
                        X_REQUEST_ID: tracking_id
                    }

                    method_execution_time = (t2 - t1)

                    if method_execution_time > CONFIG.SLOW_API_THRESHOLD:
                        logd['api_execution_threshold_exceed'] = True
                        logging.getLogger('stats').info(logd)
                    else:
                        logging.getLogger('stats').debug(logd)

                    Stats.http_stats['total_responses'] += 1
                    return result

                finally:
                    t2 = time.time()
                    tp2 = time.process_time()
                    Aggregator.update_stats(endpoint=func.__name__, status=status, success=success,
                                            server_type='http', time_taken=int((t2 - t1) * 1000),
                                            process_time_taken=int((tp2 - tp1) * 1000))

        f.is_http_method = True
        f.method = method
        f.paths = path
        if not isinstance(path, list):
            f.paths = [path]
        return f

    return decorator


def get(path=None, required_params=None, timeout=None, is_internal=False, suppressed_errors=None):
    return get_decorated_fun('get', get_path(path, is_internal), required_params, timeout, suppressed_errors)


def head(path=None, required_params=None, timeout=None, is_internal=False, suppressed_errors=None):
    return get_decorated_fun('head', get_path(path, is_internal), required_params, timeout, suppressed_errors)


def options(path=None, required_params=None, timeout=None, is_internal=False, suppressed_errors=None):
    return get_decorated_fun('options', get_path(path, is_internal), required_params, timeout, suppressed_errors)


def patch(path=None, required_params=None, timeout=None, is_internal=False, suppressed_errors=None):
    return get_decorated_fun('patch', get_path(path, is_internal), required_params, timeout, suppressed_errors)


def post(path=None, required_params=None, timeout=None, is_internal=False, suppressed_errors=None):
    return get_decorated_fun('post', get_path(path, is_internal), required_params, timeout, suppressed_errors)


def put(path=None, required_params=None, timeout=None, is_internal=False, suppressed_errors=None):
    return get_decorated_fun('put', get_path(path, is_internal), required_params, timeout, suppressed_errors)


def trace(path=None, required_params=None, timeout=None, is_internal=False, suppressed_errors=None):
    return get_decorated_fun('put', get_path(path, is_internal), required_params, timeout, suppressed_errors)


def delete(path=None, required_params=None, timeout=None, is_internal=False, suppressed_errors=None):
    return get_decorated_fun('delete', get_path(path, is_internal), required_params, timeout, suppressed_errors)


def get_path(path, is_internal=False):
    if is_internal:
        path = CONFIG.INTERNAL_HTTP_PREFIX + path

    return path
