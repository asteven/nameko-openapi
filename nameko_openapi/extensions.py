import collections
from functools import partial
import datetime
import inspect
import json
import logging

import yaml

import six
from six.moves import urllib

from nameko.extensions import Entrypoint, DependencyProvider, SharedExtension
from nameko.web.handlers import HttpRequestHandler

from werkzeug.routing import Rule
from werkzeug.wrappers import Request, Response
from werkzeug.exceptions import HTTPException

from eventlet.event import Event

import openapi_core
from openapi_core.schema.exceptions import OpenAPIMappingError
from openapi_core.shortcuts import RequestValidator, ResponseValidator
from openapi_core.wrappers.flask import FlaskOpenAPIRequest, FlaskOpenAPIResponse
import openapi_core.extensions.models.factories


log = logging.getLogger('nameko_openapi')


class OpenApiSpecManager(SharedExtension):

    _loaded = Event()

    def setup(self):
        log.info('   ###   OpenApiSpecManager.setup')
        super().setup()

    def load_spec(self, spec_file):
        log.debug('%s.load_spec: %s' % (self.__class__.__name__, spec_file))
        # TODO: supporting loading from url instead of just file
        # TODO: How to handle/interpret/respect spec.servers[].url's?
        # TODO: Or should this be generated/injected into the spec_dict on startup?
        #spec_file = '/home/sar/vcs/nameko-openapi/petstore.yaml'
        spec_dict = yaml.safe_load(open(spec_file))
        self.spec = openapi_core.create_spec(spec_dict)
        self.request_validator = RequestValidator(self.spec)
        self.response_validator = ResponseValidator(self.spec)
        self._loaded.send(self.spec)

    def wait_for_spec(self):
        """Allow other extensions to wait until the spec is loaded."""
        return self._loaded.wait()

    def get_operation_by_id(self, operation_id):
        self.wait_for_spec()
        for path_name, path in six.iteritems(self.spec.paths):
            for http_method, operation in six.iteritems(path.operations):
                if operation.operation_id == operation_id:
                    return operation

    def validate_request(self, request, raise_for_errors=True):
        result = self.request_validator.validate(request)
        if raise_for_errors:
            result.raise_for_errors()
        return result

    def validate_response(self, response, openapi_request, raise_for_errors=True):
        result = self.response_validator.validate(openapi_request, response)
        if raise_for_errors:
            result.raise_for_errors()
        return result


from openapi_core.schema.schemas.util import format_datetime
from datetime import datetime, timezone

class OpenAPIJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime):
            return o.astimezone().isoformat()
        if isinstance(o, openapi_core.extensions.models.factories.Model):
            return o.__dict__
        return super().default(o)


class OpenAPIRequest(FlaskOpenAPIRequest):

    def __init__(self, request, operation):
        self.operation = operation
        self.result = None
        # Patch our request to look like Flasks so we can re-use most
        # of FlaskOpenAPIRequest.
        request.view_args = request.path_values
        super().__init__(request)

    def __repr__(self):
        return '<%s %s>' % (
            self.__class__.__name__,
            self.operation.operation_id,
        )

    @FlaskOpenAPIRequest.path_pattern.getter
    def path_pattern(self):
        """Override to return openapi path_name instead of url_rule."""
        return self.operation.path_name


class OpenAPIResponse(FlaskOpenAPIResponse):

    def __init__(self, response, operation):
        self.operation = operation
        self.result = None
        super().__init__(response)

    def __repr__(self):
        return '<%s %s [%s]>' % (
            self.__class__.__name__,
            self.operation.operation_id,
            self.status_code,
        )


class OpenAPIRequestHandler(HttpRequestHandler):

    spec_manager = OpenApiSpecManager()

    def __init__(self, operation_id, body_name=None, **kwargs):
        self.operation_id = operation_id
        self.body_name = body_name
        # Don't use url and method ourself but keep superclass happy.
        url = method = None
        super().__init__(url, method, **kwargs)

    def setup(self):
        log.info('   ###   OpenAPIRequestHandler.setup')
        #self.spec = self.spec_manager.wait_for_spec()
        self.operation = self.spec_manager.get_operation_by_id(self.operation_id)
        method = getattr(self.container.service_cls, self.method_name)
        self.method_signature = inspect.signature(method)
        super().setup()

    def get_url_rule(self):
        """Create a url rule for werkzeug based on the openapi path name."""
        log.info('   ###   get_url_rule')
        rule = self.operation.path_name.replace('{', '<').replace('}', '>')
        methods = [self.operation.http_method]
        return Rule(rule, methods=methods)

    def get_entrypoint_parameters(self, result):
        log.info('   ###   get_entrypoint_parameters')
        args = []
        kwargs = {}

        result_parameters = {}
        result_parameters.update(result.parameters['path'])
        result_parameters.update(result.parameters['query'])
        if self.body_name:
            result_parameters[self.body_name] = result.body

        unhandled_method_params = {}
        for name, method_param in six.iteritems(self.method_signature.parameters):
            if name == 'self':
                continue
            try:
                value = result_parameters.pop(name)
                is_kwarg = method_param.default != method_param.empty
                if is_kwarg:
                    kwargs[name] = value
                else:
                    args.append(value)
            except KeyError as ex:
                unhandled_method_params[name] = method_param

        # Special case for unknown body var name.
        if all((result.body, not self.body_name, len(unhandled_method_params) == 1)):
            name, method_param = unhandled_method_params.popitem()
            value = result.body
            is_kwarg = method_param.default != method_param.empty
            if is_kwarg:
                kwargs[name] = value
            else:
                args.append(value)

        # TODO: raise exception if any result_parameters are left. It means the
        #   method is not consuming something that the api spec says it should.
        log.debug('unhandled_method_params: %s' % unhandled_method_params)
        log.debug('result_parameters: %s' % result_parameters)

        return args, kwargs

    def handle_request(self, request):
        log.info('   ###   handle_request: %s' % request)
        request.shallow = False
        try:
            context_data = self.server.context_data_from_headers(request)
            openapi_request = OpenAPIRequest(request, self.operation)
            log.info('openapi_request: %r' % openapi_request)
            openapi_request_result = self.spec_manager.validate_request(openapi_request)
            args, kwargs = self.get_entrypoint_parameters(openapi_request_result)

            event = Event()
            self.container.spawn_worker(
                self, args, kwargs, context_data=context_data,
                handle_result=partial(self.handle_result, event))
            result = event.wait()

            log.info('handle_request: result: %s', result)
            response = self.response_from_result(result, openapi_request)
            log.info('handle_request: %r' % response)

        except Exception as exc:
            raise exc
            response = self.response_from_exception(exc, openapi_request)
        return response

    def response_from_result(self, result, openapi_request):
        log.info('   ###   response_from_result: result: %s', result)

        if isinstance(result, tuple):
            if len(result) == 3:
                # FIXME: should it even be possible to define headers from an
                #   operation handler?
                status, headers, payload = result
            else:
                status, payload = result
        else:
            payload = result
            status = 200

        # TODO: exception handling
        operation_response = self.operation.get_response(str(status))

        response = Response(
            status=operation_response.http_status,
            headers=operation_response.headers,
        )

        if operation_response.content:
            # FIXME: hardcoded to json
            mimetype = 'application/json'
            payload = json.dumps(payload, cls=OpenAPIJSONEncoder)

            errors = []
            data = None
            try:
                media_type = operation_response.get_content_type(mimetype)
            except OpenAPIMappingError as exc:
                errors.append(exc)
            else:
                try:
                    # Validate result by unmarschalling it.
                    data = media_type.unmarshal(payload)
                except OpenAPIMappingError as exc:
                    errors.append(exc)

            print('data: %s' % data)
            print('errors: %s' % errors)
        else:
            mimetype = ''
            payload = ''

        response.mimetype = mimetype
        response.data = payload

        log.info('response: %s', response)
        return response


    def response_from_exception(self, exc, openapi_request):
        log.info('   ###   response_from_exception')
        return super().response_from_exception(exc)

        if (
            isinstance(exc, OpenAPIMappingError) or
            isinstance(exc, BadRequest)
        ):
            status_code = 400
        else:
            status_code = 500

        log.info('response_from_exception')
        log.info(type(exc))
        log.info('%s' % status_code)

        payload = {
            'code': status_code,
            'message': str(exc),
        }

        response = Response(
            json.dumps(payload),
            status=status_code,
            mimetype='application/json'
        )
        return response


class OpenApi(DependencyProvider):

    spec_manager = OpenApiSpecManager()

    def __init__(self, spec_url):
        self.spec_url = spec_url

    def setup(self):
        log.info('   ###   OpenApi.setup')
        self.spec_manager.load_spec(self.spec_url)
        #print(self.container.config.get(constants.CONFIG_KEY, None))
        super().setup()

    def worker_setup(self, worker_ctx):
        log.info('   ###   OpenApi.worker_setup: %s' % worker_ctx)
        print('worker_ctx.entrypoint: %s' % worker_ctx.entrypoint)
        print('worker_ctx.args: %s' % worker_ctx.args)
        print('worker_ctx.kwargs: %s' % worker_ctx.kwargs)
        print('worker_ctx.data: %s' % worker_ctx.data)
        print('worker_ctx.context_data: %s' % worker_ctx.context_data)

    def worker_result(self, worker_ctx, result=None, exc_info=None):
        log.info('   ###   OpenApi.worker_result: %s, %s, %s' % (worker_ctx, result, exc_info))

    def worker_teardown(self, worker_ctx):
        log.info('   ###   OpenApi.worker_teardown: %s' % worker_ctx)
        print('worker_ctx.entrypoint: %s' % worker_ctx.entrypoint)
        print('worker_ctx.args: %s' % worker_ctx.args)
        print('worker_ctx.kwargs: %s' % worker_ctx.kwargs)
        print('worker_ctx.data: %s' % worker_ctx.data)
        print('worker_ctx.context_data: %s' % worker_ctx.context_data)

    #def stop(self):
    #    pass

    def get_dependency(self, worker_ctx):
        log.info('   ###   OpenApi.get_dependency: %s' % worker_ctx)
        return self

#    @property
#    def operation(self):
#        print('OpenApi.operation')
#        return OpenAPIRequestHandler.decorator
    operation = OpenAPIRequestHandler.decorator
