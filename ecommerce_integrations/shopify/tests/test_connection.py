# Copyright (c) 2024, Frappe and Contributors
# See LICENSE

"""Tests for Shopify connection, webhook handling, and HMAC validation.

Covers:
- HMAC request validation
- Webhook event processing
- Access token retrieval for different auth methods
- temp_shopify_session decorator behavior in test mode
"""

import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import MagicMock, patch

from shopify.resources import Webhook
from shopify.session import Session

import frappe

from ecommerce_integrations.shopify import connection
from ecommerce_integrations.shopify.connection import (
	_get_access_token,
	_validate_request,
	process_request,
)
from ecommerce_integrations.shopify.constants import (
	API_VERSION,
	EVENT_MAPPER,
	SETTING_DOCTYPE,
	WEBHOOK_EVENTS,
)

from .utils import TestCase


class TestShopifyConnection(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.setting = frappe.get_doc(SETTING_DOCTYPE)

	@unittest.skip("Can't run these tests in CI")
	def test_register_webhooks(self):
		webhooks = connection.register_webhooks(
			self.setting.shopify_url, self.setting.get_password("password")
		)

		self.assertEqual(len(webhooks), len(connection.WEBHOOK_EVENTS))

		wh_topics = [wh.topic for wh in webhooks]
		self.assertEqual(sorted(wh_topics), sorted(connection.WEBHOOK_EVENTS))

	@unittest.skip("Can't run these tests in CI")
	def test_unregister_webhooks(self):
		connection.unregister_webhooks(self.setting.shopify_url, self.setting.get_password("password"))

		callback_url = connection.get_callback_url()

		with Session.temp(self.setting.shopify_url, API_VERSION, self.setting.get_password("password")):
			for wh in Webhook.find():
				self.assertNotEqual(wh.address, callback_url)


class TestHMACValidation(TestCase):
	"""Tests for webhook HMAC signature validation."""

	def test_valid_hmac_passes(self):
		"""Valid HMAC signature should not raise."""
		secret = "supersecret"
		data = b'{"id": 123}'
		sig = base64.b64encode(hmac.new(secret.encode("utf8"), data, hashlib.sha256).digest()).decode()

		req = MagicMock()
		req.data = data

		# Should not raise
		_validate_request(req, sig)

	def test_invalid_hmac_raises(self):
		"""Invalid HMAC signature should raise."""
		req = MagicMock()
		req.data = b'{"id": 123}'

		with self.assertRaises(Exception):
			_validate_request(req, "invalid_signature")


class TestEventMapping(TestCase):
	"""Tests for webhook event to handler mapping."""

	def test_all_webhook_events_have_handlers(self):
		"""Every webhook event should have a corresponding handler in EVENT_MAPPER."""
		for event in WEBHOOK_EVENTS:
			self.assertIn(event, EVENT_MAPPER, f"No handler mapped for event: {event}")

	def test_event_mapper_methods_are_importable(self):
		"""All methods referenced in EVENT_MAPPER should be valid Python paths."""
		for event, method_path in EVENT_MAPPER.items():
			parts = method_path.rsplit(".", 1)
			self.assertEqual(len(parts), 2, f"Invalid method path for {event}: {method_path}")
			module_path, func_name = parts
			try:
				module = frappe.get_module(module_path)
				self.assertTrue(
					hasattr(module, func_name),
					f"Function {func_name} not found in {module_path}",
				)
			except ImportError:
				self.fail(f"Module {module_path} for event {event} could not be imported")


class TestAccessToken(TestCase):
	"""Tests for _get_access_token with different auth methods."""

	def test_static_token_returns_password(self):
		"""Static Token auth should return the password field."""
		setting = frappe.get_doc(SETTING_DOCTYPE)
		setting.authentication_method = "Static Token"

		token = _get_access_token(setting)
		self.assertTrue(token, "Static token should return a value")

	def test_authorization_code_grant_without_token_throws(self):
		"""Authorization Code Grant without token should throw."""
		setting = frappe.get_doc(SETTING_DOCTYPE)
		setting.authentication_method = "Authorization Code Grant"

		# Ensure no token exists
		with patch.object(setting, "get_password", return_value=None):
			with self.assertRaises(Exception):
				_get_access_token(setting)


class TestTempShopifySession(TestCase):
	"""Tests for the temp_shopify_session decorator."""

	def test_decorator_passes_through_in_test_mode(self):
		"""In test mode (frappe.flags.in_test), decorator should call function directly."""
		self.assertTrue(frappe.flags.in_test, "Tests should run with in_test flag")

		@connection.temp_shopify_session
		def test_func():
			return "success"

		result = test_func()
		self.assertEqual(result, "success")
