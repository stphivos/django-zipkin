from unittest2.case import TestCase
from mock import Mock, call
from mock import patch

import logging
import types

from django.test import RequestFactory
from django.http import HttpResponse

from helpers import DjangoZipkinTestHelpers

import django_zipkin.middleware
from django_zipkin.api import ZipkinApi
from django_zipkin.zipkin_data import ZipkinData, ZipkinId
from django_zipkin.data_store import BaseDataStore
from django_zipkin.id_generator import BaseIdGenerator
from django_zipkin.middleware import ZipkinMiddleware, ZipkinDjangoRequestParser
from django_zipkin import constants


__all__ = ['ZipkinMiddlewareTestCase', 'ZipkinDjangoRequestProcessorTestCase']


class ZipkinMiddlewareTestCase(TestCase):
    def setUp(self):
        self.store = Mock(spec=BaseDataStore)
        self.request_processor = Mock(spec=ZipkinDjangoRequestParser)
        self.generator = Mock(spec=BaseIdGenerator)
        self.api = Mock(spec=ZipkinApi)
        self.middleware = ZipkinMiddleware(self.store, self.request_processor, self.generator, self.api)
        self.request_factory = RequestFactory()

    def test_resolve_request_on_django_lt_15(self):
        with patch('django.VERSION', new=(1, 4)):
            reload(django_zipkin.middleware)
            with patch('django_zipkin.middleware.resolve') as mock_resolve:
                self.assertEqual(django_zipkin.middleware.resolve_request(Mock()),
                                 mock_resolve.return_value)
        reload(django_zipkin.middleware)

    def test_resolve_request_on_django_ge_15(self):
        with patch('django.VERSION', new=(1, 5)):
            reload(django_zipkin.middleware)
            request = Mock()
            self.assertEqual(django_zipkin.middleware.resolve_request(request), request.resolver_match)
        reload(django_zipkin.middleware)

    def test_intercepts_incoming_trace_id(self):
        self.middleware.process_request(Mock())
        self.store.set.assert_called_once_with(self.request_processor.get_zipkin_data.return_value)

    def test_generates_ids_if_no_incoming(self):
        self.request_processor.get_zipkin_data.return_value = ZipkinData()
        self.middleware.process_request(Mock())
        self.generator.generate_trace_id.assert_called_once_with()
        self.generator.generate_span_id.assert_called_once_with()
        data = self.store.set.call_args[0][0]
        self.assertEqual(data.span_id, self.generator.generate_span_id.return_value)
        self.assertEqual(data.trace_id, self.generator.generate_trace_id.return_value)

    def test_annotates_uri(self):
        uri = '/foo/bar?x=y'
        request = self.request_factory.get(uri, HTTP_X_B3_SAMPLED='true')
        self.middleware.process_request(request)
        self.api.record_key_value.assert_has_calls([call('http.uri', uri)])

    def test_annotates_responsecode(self):
        self.store.get.return_value = ZipkinData()
        self.middleware.process_response(self.request_factory.get('/', HTTP_X_B3_SAMPLED='true'), HttpResponse(status=420))
        self.api.record_key_value.assert_has_calls([call('http.statuscode', 420)])

    def test_annotates_view_name_and_arguments_of_view_function(self):
        request, view, args, kwargs = Mock(), Mock(spec=types.FunctionType), (1, 2), {'kw': 'arg'}
        self.middleware.process_view(request, view, args, kwargs)
        self.api.record_key_value.assert_has_calls([
            call('django.view.kwargs', '{"kw": "arg"}'),
            call('django.view.func_name', view.func_name),
            call('django.view.args', '[1, 2]')
        ], any_order=True)

    def test_annotates_view_name_and_arguments_of_view_method(self):
        request, view, args, kwargs = Mock(), Mock(spec=types.MethodType, im_class=Mock(__name__=Mock())), (3, 4), {'more': 'kwargs'}
        self.middleware.process_view(request, view, args, kwargs)
        self.api.record_key_value.assert_has_calls([
            call('django.view.class', view.im_class.__name__),
            call('django.view.func_name', view.im_func.func_name),
            call('django.view.args', '[3, 4]'),
            call('django.view.kwargs', '{"more": "kwargs"}')
        ], any_order=True)

    def test_adds_tastypie_specific_annotation(self):
        request, view, args, kwargs = Mock(), Mock(spec=types.FunctionType), (1, 2), {'resource_name': 'test-resource'}
        self.middleware.process_view(request, view, args, kwargs)
        self.api.record_key_value.assert_has_calls([
            call('django.tastypie.resource_name', 'test-resource'),
            call('django.view.kwargs', '{}')
        ], any_order=True)

    def test_with_defaults(self):
        self.middleware = ZipkinMiddleware()
        self.middleware.process_request(self.request_factory.get('/', HTTP_X_B3_TRACEID='000000000000002a'))
        self.assertEqual(self.middleware.store.get().trace_id.get_binary(), 42)
        self.assertIsInstance(self.middleware.store.get().span_id, ZipkinId)

    def test_incoming_span_id_to_parent_span_id(self):
        self.middleware.request_parser = ZipkinDjangoRequestParser()
        self.middleware.process_request(self.request_factory.get('/', HTTP_X_B3_SPANID='000000000000002a'))
        self.assertEqual(self.store.set.call_args[0][0].parent_span_id.get_binary(), 42)

    def test_logs_iff_sampled_or_flagged(self):
        for sampled in [True, False]:
            for flags in [True, False]:
                self.middleware.logger = Mock(spec=logging.Logger)
                self.middleware.store.get.return_value = ZipkinData(sampled=sampled, flags=flags)
                self.middleware.process_response(Mock(), HttpResponse())
                if sampled or flags:
                    self.middleware.logger.info.assert_called_once_with(self.api.build_log_message.return_value)
                else:
                    self.assertListEqual(self.middleware.logger.info.mock_calls, [])

    def test_process_response_without_process_request(self):
        # This happens when a middleware before us returns a response in process_request
        self.store.get.return_value = ZipkinData()
        request = Mock()
        self.middleware.process_request = Mock()
        self.middleware.process_response(request, HttpResponse())
        self.middleware.process_request.assert_called_once_with(request)
        self.middleware.api.record_event(constants.ANNOTATION_NO_DATA_IN_LOCAL_STORE)


class ZipkinDjangoRequestProcessorTestCase(DjangoZipkinTestHelpers, TestCase):
    def setUp(self):
        self.request_factory = RequestFactory()
        self.processor = ZipkinDjangoRequestParser()

    def test_header_keys(self):
        transform = lambda s: 'HTTP_' + s.upper().replace('-', '_')
        self.assertEqual(ZipkinDjangoRequestParser.trace_id_hdr_name, transform("X-B3-TraceId"))
        self.assertEqual(ZipkinDjangoRequestParser.span_id_hdr_name, transform("X-B3-SpanId"))
        self.assertEqual(ZipkinDjangoRequestParser.parent_span_id_hdr_name, transform("X-B3-ParentSpanId"))
        self.assertEqual(ZipkinDjangoRequestParser.sampled_hdr_name, transform("X-B3-Sampled"))
        self.assertEqual(ZipkinDjangoRequestParser.flags_hdr_name, transform("X-B3-Flags"))

    def test_all_fields_filled(self):
        trace_id = ZipkinId.from_binary(42)
        span_id = ZipkinId.from_binary(-42)
        parent_span_id = ZipkinId.from_binary(53)
        request = self.request_factory.get('/', **{
            ZipkinDjangoRequestParser.trace_id_hdr_name: trace_id.get_hex(),
            ZipkinDjangoRequestParser.span_id_hdr_name:  span_id.get_hex(),
            ZipkinDjangoRequestParser.parent_span_id_hdr_name:  parent_span_id.get_hex(),
            ZipkinDjangoRequestParser.sampled_hdr_name: 'true',
            ZipkinDjangoRequestParser.flags_hdr_name: '0'
        })
        self.assertZipkinDataEquals(
            self.processor.get_zipkin_data(request),
            ZipkinData(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                sampled=True,
                flags=False
            )
        )

    def test_no_fields_filled(self):
        self.assertZipkinDataEquals(
            self.processor.get_zipkin_data(self.request_factory.get('/')),
            ZipkinData()
        )
