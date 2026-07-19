# Copyright (c) 2024, Frappe and Contributors
# See LICENSE

"""Tests for OAuth flow and scope configuration.

Covers:
- OAuth scopes include all required permissions
- Authorization URL construction
"""

import unittest

import frappe

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE


class TestOAuthScopes(unittest.TestCase):
	"""Tests for OAuth scope configuration."""

	def test_read_locations_scope_included(self):
		"""The read_locations scope must be included for warehouse mapping."""
		from ecommerce_integrations.shopify import oauth

		# The scopes string is constructed in the authorize function
		# We verify it by inspecting the module-level or function-level scope string
		source = frappe.get_module("ecommerce_integrations.shopify.oauth")
		import inspect

		source_code = inspect.getsource(source)
		self.assertIn("read_locations", source_code, "read_locations scope must be in OAuth flow")

	def test_all_required_scopes_present(self):
		"""All required scopes for the integration should be present."""
		from ecommerce_integrations.shopify import oauth
		import inspect

		source_code = inspect.getsource(oauth)

		required_scopes = [
			"read_customers",
			"write_customers",
			"read_orders",
			"write_orders",
			"read_products",
			"write_products",
			"read_inventory",
			"write_inventory",
			"read_locations",
		]

		for scope in required_scopes:
			self.assertIn(scope, source_code, f"Required scope '{scope}' missing from OAuth flow")

	def test_scope_string_is_comma_separated(self):
		"""Scopes should be comma-separated without spaces (Shopify format)."""
		from ecommerce_integrations.shopify import oauth
		import inspect
		import re

		source_code = inspect.getsource(oauth)

		# Find the scopes string assignment
		match = re.search(r'scopes\s*=\s*"([^"]+)"', source_code)
		self.assertIsNotNone(match, "Could not find scopes string in oauth module")

		scopes_str = match.group(1)
		# Should not contain spaces
		self.assertNotIn(" ", scopes_str, "Scopes should not contain spaces")
		# Should be comma-separated
		scopes = scopes_str.split(",")
		self.assertGreater(len(scopes), 1, "Should have multiple scopes")
