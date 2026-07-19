# Copyright (c) 2024, Frappe and Contributors
# See LICENSE

"""Tests for Shopify order sync (orders/create webhook handler).

Covers:
- Creating a Sales Order from a Shopify order payload
- Duplicate order detection (idempotency)
- Order cancellation
- Order items, taxes, and pricing
"""

import json

import frappe
from frappe.utils import getdate

from ecommerce_integrations.shopify.constants import (
	MODULE_NAME,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.order import (
	_get_item_price,
	_get_total_discount,
	cancel_order,
	create_sales_order,
	get_order_items,
	sync_sales_order,
)
from ecommerce_integrations.shopify.product import ShopifyProduct

from .utils import TestCase


class TestOrderSync(TestCase):
	"""Tests for syncing Shopify orders to ERPNext Sales Orders."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Ensure the product used in order fixture exists
		fake_instance = cls()
		fake_instance.setUp()
		fake_instance.fake("products/6732194021530", body=fake_instance.load_fixture("single_product"))

		product = ShopifyProduct(product_id="6732194021530", variant_id="39933951901850")
		if not product.is_synced():
			product.sync_product()

		# Ensure tax account exists
		if not frappe.db.exists("Account", {"account_name": "GST", "company": "_Test Company"}):
			parent = frappe.db.get_value(
				"Account",
				{"company": "_Test Company", "is_group": 1, "root_type": "Liability"},
				"name",
			)
			if parent:
				frappe.get_doc(
					{
						"doctype": "Account",
						"account_name": "GST",
						"parent_account": parent,
						"company": "_Test Company",
						"account_type": "Tax",
					}
				).insert(ignore_if_duplicate=True)

		# Set up tax mapping
		setting = frappe.get_doc(SETTING_DOCTYPE)
		setting.default_sales_tax_account = frappe.db.get_value(
			"Account", {"account_name": "GST", "company": "_Test Company"}, "name"
		) or ""
		setting.flags.ignore_validate = True
		setting.save(ignore_permissions=True)

	def test_sync_creates_sales_order(self):
		"""A valid Shopify order should create a Sales Order in ERPNext."""
		order = json.loads(self.load_fixture("order"))

		# Use a unique order ID for this test
		order["id"] = 9999900001
		order["name"] = "#T-001"

		sync_sales_order(order)

		so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: str(order["id"])}, "name")
		self.assertTrue(so, "Sales Order should have been created")

		so_doc = frappe.get_doc("Sales Order", so)
		self.assertEqual(so_doc.get(ORDER_NUMBER_FIELD), "#T-001")
		self.assertEqual(len(so_doc.items), 1)

	def test_duplicate_order_skipped(self):
		"""Syncing the same order ID twice should not create a duplicate."""
		order = json.loads(self.load_fixture("order"))
		order["id"] = 9999900002
		order["name"] = "#T-002"

		sync_sales_order(order)
		so_count_before = frappe.db.count("Sales Order", {ORDER_ID_FIELD: str(order["id"])})

		# Sync again
		sync_sales_order(order)
		so_count_after = frappe.db.count("Sales Order", {ORDER_ID_FIELD: str(order["id"])})

		self.assertEqual(so_count_before, so_count_after, "Duplicate order should be skipped")

	def test_cancel_order_without_invoice(self):
		"""Cancelling an order without invoice/delivery note should cancel the Sales Order."""
		order = json.loads(self.load_fixture("order"))
		order["id"] = 9999900003
		order["name"] = "#T-003"

		sync_sales_order(order)
		so = frappe.get_doc("Sales Order", {ORDER_ID_FIELD: str(order["id"])})
		self.assertEqual(so.docstatus, 1, "Order should be submitted")

		# Cancel
		cancelled = json.loads(self.load_fixture("cancelled_order"))
		cancelled["id"] = 9999900003
		cancel_order(cancelled)

		so.reload()
		self.assertEqual(so.docstatus, 2, "Order should be cancelled")


class TestOrderItems(TestCase):
	"""Tests for order item parsing and price calculation."""

	def test_item_price_without_tax(self):
		"""Item price should be returned as-is when taxes are not inclusive."""
		line_item = {
			"price": "100.00",
			"quantity": 1,
			"tax_lines": [{"price": "10.00"}],
			"discount_allocations": [],
		}
		price = _get_item_price(line_item, taxes_inclusive=False)
		self.assertEqual(price, 100.0)

	def test_item_price_with_inclusive_tax(self):
		"""Item price should subtract tax when taxes are inclusive."""
		line_item = {
			"price": "110.00",
			"quantity": 1,
			"tax_lines": [{"price": "10.00"}],
			"discount_allocations": [],
		}
		price = _get_item_price(line_item, taxes_inclusive=True)
		self.assertEqual(price, 100.0)

	def test_item_price_with_discount(self):
		"""Item price should subtract per-unit discount."""
		line_item = {
			"price": "100.00",
			"quantity": 2,
			"tax_lines": [],
			"discount_allocations": [{"amount": "20.00"}],
		}
		price = _get_item_price(line_item, taxes_inclusive=False)
		self.assertEqual(price, 90.0)  # 100 - (20/2)

	def test_total_discount_calculation(self):
		"""Total discount should sum all discount allocations."""
		line_item = {
			"discount_allocations": [
				{"amount": "5.00"},
				{"amount": "10.00"},
			]
		}
		self.assertEqual(_get_total_discount(line_item), 15.0)

	def test_total_discount_empty(self):
		"""Total discount should be 0 when no allocations."""
		self.assertEqual(_get_total_discount({"discount_allocations": []}), 0.0)
		self.assertEqual(_get_total_discount({"discount_allocations": None}), 0.0)
