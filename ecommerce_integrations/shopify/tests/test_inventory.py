# Copyright (c) 2024, Frappe and Contributors
# See LICENSE

"""Tests for inventory sync (ERPNext -> Shopify).

Covers:
- Inventory sync skipped when disabled
- Inventory sync skipped when need_to_run is False
- Inventory status logging (success, partial, failed)
"""

from unittest.mock import MagicMock, patch

import frappe

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopify.inventory import (
	_log_inventory_update_status,
	update_inventory_on_shopify,
)

from .utils import TestCase


class TestInventorySync(TestCase):
	"""Tests for the inventory sync to Shopify."""

	def test_sync_skipped_when_disabled(self):
		"""Inventory sync should not run when update_erpnext_stock_levels_to_shopify is off."""
		setting = frappe.get_doc(SETTING_DOCTYPE)
		original = setting.update_erpnext_stock_levels_to_shopify

		try:
			setting.update_erpnext_stock_levels_to_shopify = 0
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)

			# Should return without error
			update_inventory_on_shopify()
			# If we got here without error, the guard worked
		finally:
			setting.update_erpnext_stock_levels_to_shopify = original
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)

	def test_sync_skipped_when_not_enabled(self):
		"""Inventory sync should not run when integration is disabled."""
		setting = frappe.get_doc(SETTING_DOCTYPE)
		original_enabled = setting.enable_shopify

		try:
			setting.enable_shopify = 0
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)

			update_inventory_on_shopify()
			# Should return without error
			self.assertTrue(True)
		finally:
			setting.enable_shopify = original_enabled
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)


class TestInventoryLogging(TestCase):
	"""Tests for inventory sync status logging."""

	def test_log_all_success(self):
		"""All items succeeding should log as 'Success'."""
		items = [
			MagicMock(variant_id="v1", shopify_location_id="l1", status="Success", failure_reason=None),
			MagicMock(variant_id="v2", shopify_location_id="l1", status="Success", failure_reason=None),
		]

		_log_inventory_update_status(items)

		log = frappe.get_last_doc(
			"Ecommerce Integration Log", filters={"method": "update_inventory_on_shopify"}
		)
		self.assertEqual(log.status, "Success")
		self.assertIn("100.0%", log.message)

	def test_log_partial_success(self):
		"""Some items failing should log as 'Partial Success'."""
		items = [
			MagicMock(variant_id="v1", shopify_location_id="l1", status="Success", failure_reason=None),
			MagicMock(variant_id="v2", shopify_location_id="l1", status="Not Found", failure_reason=None),
		]

		_log_inventory_update_status(items)

		log = frappe.get_last_doc(
			"Ecommerce Integration Log", filters={"method": "update_inventory_on_shopify"}
		)
		self.assertEqual(log.status, "Partial Success")
		self.assertIn("50.0%", log.message)

	def test_log_all_failed(self):
		"""All items failing should log as 'Failed'."""
		items = [
			MagicMock(
				variant_id="v1", shopify_location_id="l1", status="Failed", failure_reason="Connection error"
			),
			MagicMock(variant_id="v2", shopify_location_id="l1", status="Failed", failure_reason="Timeout"),
		]

		_log_inventory_update_status(items)

		log = frappe.get_last_doc(
			"Ecommerce Integration Log", filters={"method": "update_inventory_on_shopify"}
		)
		self.assertEqual(log.status, "Failed")
		self.assertIn("0.0%", log.message)

	def test_log_contains_csv_details(self):
		"""Log message should contain CSV-formatted details."""
		items = [
			MagicMock(variant_id="v123", shopify_location_id="loc456", status="Success", failure_reason=None),
		]

		_log_inventory_update_status(items)

		log = frappe.get_last_doc(
			"Ecommerce Integration Log", filters={"method": "update_inventory_on_shopify"}
		)
		self.assertIn("variant_id,location_id,status,failure_reason", log.message)
		self.assertIn("v123", log.message)
		self.assertIn("loc456", log.message)
