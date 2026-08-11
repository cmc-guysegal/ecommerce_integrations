import json
import os

import shopify

import frappe

from ecommerce_integrations.shopify.product import ShopifyProduct

from ...tests.utils import TestCase
from .shopify_import_products import queue_sync_all_products


class TestShopifyImportProducts(TestCase):
	def __init__(self, obj):
		with open(os.path.join(os.path.dirname(__file__), "../../tests/data/bulk_products.json"), "rb") as f:
			products_json = json.loads(f.read())
			self._products = products_json["products"]

		super().__init__(obj)

	def test_import_all_products(self):
		required_products = {
			str(p["id"]): ([str(v["id"]) for v in p.get("variants", [])] if len(p.get("variants", [])) > 1 else [])
			for p in self._products
		}

		# fake shopify endpoints
		self.fake("products", body=self.load_fixture("bulk_products"), extension="json?limit=100")
		self.fake("products/count", body='{"count": 10}')

		for product in required_products:
			self.fake_single_product_from_bulk(product)

		queue_sync_all_products()

		for product, required_variants in required_products.items():
			# has_variants is needed to avoid get_erpnext_item()
			# fetching the variant instead of template because of
			# matching integration_item_code
			shopify_product = ShopifyProduct(
				product_id=product, has_variants=1 if bool(required_variants) else 0
			)

			# product is synced
			self.assertTrue(shopify_product.is_synced())

			item = shopify_product.get_erpnext_item()

			self.assertEqual(bool(item.has_variants), bool(required_variants))
			# self.assertEqual(item.name, str(shopify_product.product_id))

			variants = frappe.db.get_list("Item", filters={"variant_of": item.name})
			ecom_variants = frappe.db.get_list(
				"Ecommerce Item", filters={"variant_of": item.name}, fields="erpnext_item_code"
			)

			created_variants = [v.name for v in variants]
			created_ecom_variants = [e.erpnext_item_code for e in ecom_variants]

			# variants are created right
			self.assertEqual(sorted(required_variants), sorted(created_variants))

			self.assertEqual(len(created_ecom_variants), len(required_variants))
			self.assertEqual(sorted(required_variants), sorted(created_ecom_variants))

	def fake_single_product_from_bulk(self, product):
		item = next(p for p in self._products if str(p["id"]) == product)

		product_json = json.dumps({"product": item})

		self.fake("products/%s" % product, body=product_json)
