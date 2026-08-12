# Copyright (c) 2024, Frappe and Contributors
# See LICENSE

"""Tests for ERPNext -> Shopify product upload/update (upload_erpnext_item).

Covers:
- New simple product upload
- Variant update fix (find existing variant by ID instead of appending duplicate)
- Template item skipping
- Disabled integration skipping
"""

import json
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_integrations.shopify.constants import (
	ITEM_SELLING_RATE_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.product import (
	map_erpnext_item_to_shopify,
	upload_erpnext_item,
	write_upload_log,
)

from .utils import TestCase


class TestProductUpload(TestCase):
	"""Tests for uploading/updating ERPNext items to Shopify."""

	def test_template_item_skipped(self):
		"""Template items (has_variants=1) should be skipped."""
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": frappe.generate_hash(length=10),
				"item_name": "Test Template",
				"item_group": "Products",
				"has_variants": 1,
				"attributes": [{"attribute": "Test Sync Size"}],
			}
		)
		item.insert()

		# Should return without error (skipped)
		upload_erpnext_item(item)

		# No ecommerce item should be created
		exists = frappe.db.exists(
			"Ecommerce Item", {"erpnext_item_code": item.name, "integration": MODULE_NAME}
		)
		self.assertFalse(exists)

	def test_upload_skipped_when_disabled(self):
		"""Upload should be skipped if integration is disabled."""
		setting = frappe.get_doc(SETTING_DOCTYPE)
		original_upload = setting.upload_erpnext_items
		original_update = setting.update_shopify_item_on_update

		try:
			setting.upload_erpnext_items = 0
			setting.update_shopify_item_on_update = 0
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)

			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": frappe.generate_hash(length=10),
					"item_name": "Test Skip Upload",
					"item_group": "Products",
				}
			)
			item.insert()

			upload_erpnext_item(item)

			exists = frappe.db.exists(
				"Ecommerce Item", {"erpnext_item_code": item.name, "integration": MODULE_NAME}
			)
			self.assertFalse(exists)
		finally:
			setting.upload_erpnext_items = original_upload
			setting.update_shopify_item_on_update = original_update
			setting.flags.ignore_validate = True
			setting.save(ignore_permissions=True)

	def test_integration_item_skipped(self):
		"""Items coming from integration (flags.from_integration) should be skipped."""
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": frappe.generate_hash(length=10),
				"item_name": "Test From Integration",
				"item_group": "Products",
			}
		)
		item.flags.from_integration = True
		item.insert()

		upload_erpnext_item(item)

		exists = frappe.db.exists(
			"Ecommerce Item", {"erpnext_item_code": item.name, "integration": MODULE_NAME}
		)
		self.assertFalse(exists)

	def test_map_erpnext_item_to_shopify_fields(self):
		"""Test that ERPNext item fields are correctly mapped to Shopify product."""
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": frappe.generate_hash(length=10),
				"item_name": "Test Mapping Item",
				"description": "<p>A test description</p>",
				"item_group": "Products",
				"weight_per_unit": 0.5,
				"weight_uom": "Kg",
			}
		)
		item.flags.from_integration = True
		item.insert()

		product = MagicMock()
		map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=item)

		self.assertEqual(product.title, "Test Mapping Item")
		self.assertEqual(product.body_html, "<p>A test description</p>")
		self.assertEqual(product.product_type, "Products")
		self.assertEqual(product.weight, 0.5)
		self.assertEqual(product.weight_unit, "kg")

	def test_disabled_item_maps_to_draft(self):
		"""Disabled ERPNext item should map to Shopify draft status."""
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": frappe.generate_hash(length=10),
				"item_name": "Test Disabled Item",
				"item_group": "Products",
				"disabled": 1,
			}
		)
		item.flags.from_integration = True
		item.insert()

		product = MagicMock()
		map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=item)

		self.assertEqual(product.status, "draft")
		self.assertFalse(product.published)


class TestVariantUpdate(TestCase):
	"""Tests for the variant update fix - finding existing variants instead of appending duplicates."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# First sync the variant product to create template + variants in ERPNext
		cls.fake_instance = cls()
		cls.fake_instance.setUp()
		cls.fake_instance.fake("products/6704435495065", body=cls.fake_instance.load_fixture("variant_product"))

		from ecommerce_integrations.shopify.product import ShopifyProduct

		product = ShopifyProduct(product_id="6704435495065")
		product.sync_product()

		# Enable only the update setting so upload_erpnext_item uses the update path
		setting = frappe.get_doc(SETTING_DOCTYPE)
		cls._original_update = setting.update_shopify_item_on_update
		cls._original_upload = setting.upload_erpnext_items
		frappe.db.set_value(SETTING_DOCTYPE, SETTING_DOCTYPE, "update_shopify_item_on_update", 1)
		frappe.db.set_value(SETTING_DOCTYPE, SETTING_DOCTYPE, "upload_erpnext_items", 0)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		frappe.db.set_value(SETTING_DOCTYPE, SETTING_DOCTYPE, "update_shopify_item_on_update", cls._original_update)
		frappe.db.set_value(SETTING_DOCTYPE, SETTING_DOCTYPE, "upload_erpnext_items", cls._original_upload)
		frappe.db.commit()

	def test_variant_update_finds_existing_by_id(self):
		"""When updating a variant, the code should find the existing Shopify variant
		by its stored variant_id rather than appending a new one."""
		from shopify.resources import Product, Variant

		# Get a synced variant item
		ecom_item = frappe.get_doc(
			"Ecommerce Item",
			{"integration_item_code": "6704435495065", "variant_id": "39845261443225"},
		)
		item = frappe.get_doc("Item", ecom_item.erpnext_item_code)
		template_item = frappe.get_doc("Item", item.variant_of)

		# Create a mock Shopify product with existing variants
		mock_variant = MagicMock(spec=Variant)
		mock_variant.id = 39845261443225
		mock_variant.sku = "TSHIRT-001"
		mock_variant.price = "1000.00"
		mock_variant.option1 = "S"
		mock_variant.option2 = "Red"
		mock_variant.option3 = None

		mock_product = MagicMock(spec=Product)
		mock_product.variants = [mock_variant]
		mock_product.save.return_value = True
		mock_product.id = 6704435495065
		mock_product.to_dict.return_value = {}
		mock_product.errors = MagicMock()

		with patch("ecommerce_integrations.shopify.product.Product") as MockProduct:
			MockProduct.find.return_value = mock_product

			# Simulate updating variant item
			item.description = "Updated description"
			upload_erpnext_item(item)

			# The variant should have been updated in-place, not appended
			# Check that product.variants still has only 1 variant (not 2)
			self.assertEqual(len(mock_product.variants), 1)
			# product.save() should have been called
			mock_product.save.assert_called()

	def test_variant_update_fallback_to_option_match(self):
		"""If variant_id is not found in Ecommerce Item, fall back to matching by option values."""
		from shopify.resources import Product, Variant

		# Get a synced variant
		ecom_item = frappe.get_doc(
			"Ecommerce Item",
			{"integration_item_code": "6704435495065", "variant_id": "39845261475993"},
		)
		item = frappe.get_doc("Item", ecom_item.erpnext_item_code)

		# Create mock with a variant that has different ID but matching options
		mock_variant = MagicMock(spec=Variant)
		mock_variant.id = 99999999999  # Different ID
		mock_variant.sku = "TSHIRT-002"
		mock_variant.price = "1000.00"
		mock_variant.option1 = "S"
		mock_variant.option2 = "Blue"
		mock_variant.option3 = None

		mock_product = MagicMock(spec=Product)
		mock_product.variants = [mock_variant]
		mock_product.save.return_value = True
		mock_product.id = 6704435495065
		mock_product.to_dict.return_value = {}
		mock_product.errors = MagicMock()

		# Temporarily change the stored variant_id so ID match fails
		original_variant_id = ecom_item.variant_id
		frappe.db.set_value("Ecommerce Item", ecom_item.name, "variant_id", "00000000000")

		try:
			with patch("ecommerce_integrations.shopify.product.Product") as MockProduct:
				MockProduct.find.return_value = mock_product

				item.description = "Updated via option match"
				upload_erpnext_item(item)

				# Should still have 1 variant (matched by options, not appended)
				self.assertEqual(len(mock_product.variants), 1)
				mock_product.save.assert_called()
		finally:
			frappe.db.set_value("Ecommerce Item", ecom_item.name, "variant_id", original_variant_id)

	def test_new_variant_appended_when_no_match(self):
		"""If no existing variant matches by ID or options, a new one should be appended."""
		from shopify.resources import Product, Variant

		ecom_item = frappe.get_doc(
			"Ecommerce Item",
			{"integration_item_code": "6704435495065", "variant_id": "39845261443225"},
		)
		item = frappe.get_doc("Item", ecom_item.erpnext_item_code)

		# Create mock with a variant that has different ID AND different options
		mock_variant = MagicMock(spec=Variant)
		mock_variant.id = 99999999999
		mock_variant.option1 = "XL"
		mock_variant.option2 = "Purple"
		mock_variant.option3 = None

		mock_product = MagicMock(spec=Product)
		mock_product.variants = [mock_variant]
		mock_product.save.return_value = True
		mock_product.id = 6704435495065
		mock_product.to_dict.return_value = {}
		mock_product.errors = MagicMock()

		# Change stored variant_id so ID match also fails
		original_variant_id = ecom_item.variant_id
		frappe.db.set_value("Ecommerce Item", ecom_item.name, "variant_id", "00000000000")

		try:
			with patch("ecommerce_integrations.shopify.product.Product") as MockProduct, \
				patch("ecommerce_integrations.shopify.product.Variant") as MockVariantClass:
				MockProduct.find.return_value = mock_product
				MockVariantClass.return_value = MagicMock()

				item.description = "Append new variant"
				upload_erpnext_item(item)

				# A new variant should have been appended
				self.assertEqual(len(mock_product.variants), 2)
		finally:
			frappe.db.set_value("Ecommerce Item", ecom_item.name, "variant_id", original_variant_id)
