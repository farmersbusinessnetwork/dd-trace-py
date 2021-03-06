# 3rd party
from nose.tools import eq_

from django.test import modify_settings
from django.db import connections

# project
from ddtrace.constants import SAMPLING_PRIORITY_KEY
from ddtrace.contrib.django.db import unpatch_conn
from ddtrace.ext import errors

# testing
from tests.opentracer.utils import init_tracer
from .compat import reverse
from .utils import DjangoTraceTestCase, override_ddtrace_settings


class DjangoMiddlewareTest(DjangoTraceTestCase):
    """
    Ensures that the middleware traces all Django internals
    """
    def test_middleware_trace_request(self):
        # ensures that the internals are properly traced
        url = reverse('users-list')
        response = self.client.get(url)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 3)
        sp_request = spans[0]
        sp_template = spans[1]
        sp_database = spans[2]
        eq_(sp_database.get_tag('django.db.vendor'), 'sqlite')
        eq_(sp_template.get_tag('django.template_name'), 'users_list.html')
        eq_(sp_request.get_tag('http.status_code'), '200')
        eq_(sp_request.get_tag('http.url'), '/users/')
        eq_(sp_request.get_tag('django.user.is_authenticated'), 'False')
        eq_(sp_request.get_tag('http.method'), 'GET')
        eq_(sp_request.span_type, 'http')

    def test_database_patch(self):
        # We want to test that a connection-recreation event causes connections
        # to get repatched. However since django tests are a atomic transaction
        # we can't change the connection. Instead we test that the connection
        # does get repatched if it's not patched.
        for conn in connections.all():
            unpatch_conn(conn)
        # ensures that the internals are properly traced
        url = reverse('users-list')
        response = self.client.get(url)
        eq_(response.status_code, 200)

        # We would be missing span #3, the database span, if the connection
        # wasn't patched.
        spans = self.tracer.writer.pop()
        eq_(len(spans), 3)
        eq_(spans[0].name, 'django.request')
        eq_(spans[1].name, 'django.template')
        eq_(spans[2].name, 'sqlite.query')

    def test_middleware_trace_errors(self):
        # ensures that the internals are properly traced
        url = reverse('forbidden-view')
        response = self.client.get(url)
        eq_(response.status_code, 403)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.get_tag('http.status_code'), '403')
        eq_(span.get_tag('http.url'), '/fail-view/')
        eq_(span.resource, 'tests.contrib.django.app.views.ForbiddenView')

    def test_middleware_trace_function_based_view(self):
        # ensures that the internals are properly traced when using a function views
        url = reverse('fn-view')
        response = self.client.get(url)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.get_tag('http.status_code'), '200')
        eq_(span.get_tag('http.url'), '/fn-view/')
        eq_(span.resource, 'tests.contrib.django.app.views.function_view')

    def test_middleware_trace_error_500(self):
        # ensures we trace exceptions generated by views
        url = reverse('error-500')
        response = self.client.get(url)
        eq_(response.status_code, 500)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.error, 1)
        eq_(span.get_tag('http.status_code'), '500')
        eq_(span.get_tag('http.url'), '/error-500/')
        eq_(span.resource, 'tests.contrib.django.app.views.error_500')
        assert "Error 500" in span.get_tag('error.stack')

    def test_middleware_trace_callable_view(self):
        # ensures that the internals are properly traced when using callable views
        url = reverse('feed-view')
        response = self.client.get(url)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.get_tag('http.status_code'), '200')
        eq_(span.get_tag('http.url'), '/feed-view/')
        eq_(span.resource, 'tests.contrib.django.app.views.FeedView')

    def test_middleware_trace_partial_based_view(self):
        # ensures that the internals are properly traced when using a function views
        url = reverse('partial-view')
        response = self.client.get(url)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.get_tag('http.status_code'), '200')
        eq_(span.get_tag('http.url'), '/partial-view/')
        eq_(span.resource, 'partial')

    def test_middleware_trace_lambda_based_view(self):
        # ensures that the internals are properly traced when using a function views
        url = reverse('lambda-view')
        response = self.client.get(url)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.get_tag('http.status_code'), '200')
        eq_(span.get_tag('http.url'), '/lambda-view/')
        eq_(span.resource, 'tests.contrib.django.app.views.<lambda>')

    @modify_settings(
        MIDDLEWARE={
            'remove': 'django.contrib.auth.middleware.AuthenticationMiddleware',
        },
        MIDDLEWARE_CLASSES={
            'remove': 'django.contrib.auth.middleware.AuthenticationMiddleware',
        },
    )
    def test_middleware_without_user(self):
        # remove the AuthenticationMiddleware so that the ``request``
        # object doesn't have the ``user`` field
        url = reverse('users-list')
        response = self.client.get(url)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 3)
        sp_request = spans[0]
        sp_template = spans[1]
        sp_database = spans[2]
        eq_(sp_request.get_tag('http.status_code'), '200')
        eq_(sp_request.get_tag('django.user.is_authenticated'), None)

    @override_ddtrace_settings(DISTRIBUTED_TRACING=True)
    def test_middleware_propagation(self):
        # ensures that we properly propagate http context
        url = reverse('users-list')
        headers = {
            'x-datadog-trace-id': '100',
            'x-datadog-parent-id': '42',
            'x-datadog-sampling-priority': '2',
        }
        response = self.client.get(url, **headers)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 3)
        sp_request = spans[0]
        sp_template = spans[1]
        sp_database = spans[2]

        # Check for proper propagated attributes
        eq_(sp_request.trace_id, 100)
        eq_(sp_request.parent_id, 42)
        eq_(sp_request.get_metric(SAMPLING_PRIORITY_KEY), 2)

    def test_middleware_no_propagation(self):
        # ensures that we properly propagate http context
        url = reverse('users-list')
        headers = {
            'x-datadog-trace-id': '100',
            'x-datadog-parent-id': '42',
            'x-datadog-sampling-priority': '2',
        }
        response = self.client.get(url, **headers)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 3)
        sp_request = spans[0]
        sp_template = spans[1]
        sp_database = spans[2]

        # Check that propagation didn't happen
        assert sp_request.trace_id != 100
        assert sp_request.parent_id != 42
        assert sp_request.get_metric(SAMPLING_PRIORITY_KEY) != 2

    @modify_settings(
        MIDDLEWARE={
            'append': 'tests.contrib.django.app.middlewares.HandleErrorMiddlewareSuccess',
        },
        MIDDLEWARE_CLASSES={
            'append': 'tests.contrib.django.app.middlewares.HandleErrorMiddlewareSuccess',
        },
    )
    def test_middleware_handled_view_exception_success(self):
        """ Test when an exception is raised in a view and then handled, that
            the resulting span does not possess error properties.
        """
        url = reverse('error-500')
        response = self.client.get(url)
        eq_(response.status_code, 200)

        spans = self.tracer.writer.pop()
        eq_(len(spans), 1)

        sp_request = spans[0]

        eq_(sp_request.error, 0)
        assert sp_request.get_tag(errors.ERROR_STACK) is None
        assert sp_request.get_tag(errors.ERROR_MSG) is None
        assert sp_request.get_tag(errors.ERROR_TYPE) is None

    @modify_settings(
        MIDDLEWARE={
            'append': 'tests.contrib.django.app.middlewares.HandleErrorMiddlewareClientError',
        },
        MIDDLEWARE_CLASSES={
            'append': 'tests.contrib.django.app.middlewares.HandleErrorMiddlewareClientError',
        },
    )
    def test_middleware_handled_view_exception_client_error(self):
        """ Test the case that when an exception is raised in a view and then
            handled, that the resulting span does not possess error properties.
        """
        url = reverse('error-500')
        response = self.client.get(url)
        eq_(response.status_code, 404)

        spans = self.tracer.writer.pop()
        eq_(len(spans), 1)

        sp_request = spans[0]

        eq_(sp_request.error, 0)
        assert sp_request.get_tag(errors.ERROR_STACK) is None
        assert sp_request.get_tag(errors.ERROR_MSG) is None
        assert sp_request.get_tag(errors.ERROR_TYPE) is None

    def test_middleware_trace_request_ot(self):
        """OpenTracing version of test_middleware_trace_request."""
        ot_tracer = init_tracer('my_svc', self.tracer)

        # ensures that the internals are properly traced
        url = reverse('users-list')
        with ot_tracer.start_active_span('ot_span'):
            response = self.client.get(url)
        eq_(response.status_code, 200)

        # check for spans
        spans = self.tracer.writer.pop()
        eq_(len(spans), 4)
        ot_span = spans[0]
        sp_request = spans[1]
        sp_template = spans[2]
        sp_database = spans[3]

        # confirm parenting
        eq_(ot_span.parent_id, None)
        eq_(sp_request.parent_id, ot_span.span_id)

        eq_(ot_span.resource, 'ot_span')
        eq_(ot_span.service, 'my_svc')

        eq_(sp_database.get_tag('django.db.vendor'), 'sqlite')
        eq_(sp_template.get_tag('django.template_name'), 'users_list.html')
        eq_(sp_request.get_tag('http.status_code'), '200')
        eq_(sp_request.get_tag('http.url'), '/users/')
        eq_(sp_request.get_tag('django.user.is_authenticated'), 'False')
        eq_(sp_request.get_tag('http.method'), 'GET')
