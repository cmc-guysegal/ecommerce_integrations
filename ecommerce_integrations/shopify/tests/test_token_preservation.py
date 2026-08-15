# Copyright (c) 2024, Frappe and Contributors
# See LICENSE

"""Tests for OAuth token preservation across saves.

Covers:
- Token stashed in before_save and restored in on_update
- Token survives save when integration is disabled
- Token survives multiple consecutive saves
"""

from unittest.mock import patch

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE

from .utils import TestCase


class TestTokenPreservation(TestCase):
	"""Tests for authorization_code_token preservation across DocType saves."""

	def _set_test_token(self, token_value):
		"""Helper to set a test token directly in __Auth."""
		set_encrypted_password(
			"Shopify Setting", "Shopify Setting", token_value, fieldname="authorization_code_token"
		)
		frappe.db.commit()

	def _get_current_token(self):
		"""Helper to read the current token from __Auth."""
		return get_decrypted_password(
			"Shopify Setting", "Shopify Setting", "authorization_code_token", raise_exception=False
		)

	def test_token_preserved_after_save(self):
		"""Token should survive a save cycle (before_save stash + on_update restore)."""
		test_token = "shpca_test_preserve_" + frappe.generate_hash(length=10)
		setting = frappe.get_doc(SETTING_DOCTYPE)

		# Set auth method and token
		original_method = setting.authentication_method
		setting.authentication_method = "Authorization Code Grant"
		setting.flags.ignore_validate = True
		setting.save(ignore_permissions=True)

		self._set_test_token(test_token)

		try:
			# Verify token is set
			self.assertEqual(self._get_current_token(), test_token)

			# Save again (this is where Frappe clears __Auth for Single doctypes)
			setting.reload()
			setting.authentication_method = "Authorization Code Grant"
			setting.flags.ignore_validate = True
			setting.before_save()
			setting.save(ignore_permissions=True)
			setting.on_update()

			# Token should still be there
			preserved = self._get_current_token()
			self.assertEqual(preserved, test_token, "Token should be preserved after save")
		finally:
			setting.authentication_method = original_method
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)

	def test_token_preserved_when_disabled(self):
		"""Token should be preserved even when integration is disabled."""
		test_token = "shpca_test_disabled_" + frappe.generate_hash(length=10)
		setting = frappe.get_doc(SETTING_DOCTYPE)

		original_method = setting.authentication_method
		original_enabled = setting.enable_shopify

		setting.authentication_method = "Authorization Code Grant"
		setting.flags.ignore_validate = True
		setting.save(ignore_permissions=True)

		self._set_test_token(test_token)

		try:
			# Disable integration
			setting.reload()
			setting.enable_shopify = 0
			setting.authentication_method = "Authorization Code Grant"
			setting.flags.ignore_validate = True
			setting.before_save()
			setting.save(ignore_permissions=True)
			setting.on_update()

			preserved = self._get_current_token()
			self.assertEqual(preserved, test_token, "Token should be preserved even when disabled")
		finally:
			setting.enable_shopify = original_enabled
			setting.authentication_method = original_method
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)

	def test_token_not_stashed_for_static_token_auth(self):
		"""Token stashing should only happen for Authorization Code Grant."""
		setting = frappe.get_doc(SETTING_DOCTYPE)

		original_method = setting.authentication_method
		setting.authentication_method = "Static Token"
		setting.flags.ignore_validate = True
		setting.save(ignore_permissions=True)

		try:
			setting.reload()
			setting.before_save()

			# Should NOT have stashed token
			self.assertFalse(
				getattr(setting, "_preserved_auth_code_token", None),
				"Token should not be stashed for Static Token auth",
			)
		finally:
			setting.authentication_method = original_method
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)

	def test_multiple_saves_preserve_token(self):
		"""Token should survive multiple consecutive saves."""
		test_token = "shpca_test_multi_" + frappe.generate_hash(length=10)
		setting = frappe.get_doc(SETTING_DOCTYPE)

		original_method = setting.authentication_method
		setting.authentication_method = "Authorization Code Grant"
		setting.flags.ignore_validate = True
		setting.save(ignore_permissions=True)

		self._set_test_token(test_token)

		try:
			for _ in range(3):
				setting.reload()
				setting.authentication_method = "Authorization Code Grant"
				setting.flags.ignore_validate = True
				setting.before_save()
				setting.save(ignore_permissions=True)
				setting.on_update()

			preserved = self._get_current_token()
			self.assertEqual(preserved, test_token, "Token should survive multiple saves")
		finally:
			setting.authentication_method = original_method
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)
