import base64
import functools
import hashlib
import hmac
import json

from shopify.resources import Webhook
from shopify.session import Session

import frappe
from frappe import _

from ecommerce_integrations.shopify.constants import (
	API_VERSION,
	EVENT_MAPPER,
	SETTING_DOCTYPE,
	WEBHOOK_EVENTS,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


def temp_shopify_session(func):
	"""Any function that needs to access shopify api needs this decorator.
	The decorator starts a temp session that's destroyed when function returns.

	Supports both Static Token and OAuth 2.0 Client Credentials authentication methods.
	For OAuth, automatically refreshes token if expired or expiring soon.
	"""

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		# no auth in testing
		if frappe.flags.in_test:
			return func(*args, **kwargs)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		if setting.is_enabled():
			# Get access token based on authentication method
			access_token = _get_access_token(setting)
			auth_details = (setting.shopify_url, API_VERSION, access_token)

			with Session.temp(*auth_details):
				return func(*args, **kwargs)

	return wrapper


def _get_access_token(setting):
	"""
	Get the appropriate access token based on authentication method.
	For OAuth, ensures token is valid and refreshes if needed.

	Args:
	        setting: ShopifySetting document instance

	Returns:
	        Valid access token
	"""
	if setting.authentication_method == "OAuth 2.0 Client Credentials":
		# Import here to avoid circular dependency
		from ecommerce_integrations.shopify.oauth import get_valid_access_token

		try:
			return get_valid_access_token(setting)
		except Exception as e:
			# Log the error and re-raise with context
			create_shopify_log(
				status="Error",
				method="ecommerce_integrations.shopify.connection._get_access_token",
				message=_("Failed to get valid OAuth access token"),
				exception=str(e),
			)
			frappe.throw(
				_("Failed to authenticate with Shopify using OAuth 2.0: {0}").format(str(e)),
				title=_("Authentication Error"),
			)
	elif setting.authentication_method == "Authorization Code Grant":
		token = setting.get_password("authorization_code_token", raise_exception=False)
		if not token:
			frappe.throw(
				_("No access token found. Please click 'Authorize App' to complete the OAuth flow."),
				title=_("Authentication Error"),
			)
		return token
	else:
		# Static Token authentication
		return setting.get_password("password")


def register_webhooks(shopify_url: str, password: str) -> list[Webhook]:
	"""Register required webhooks with shopify and return registered webhooks."""
	new_webhooks = []

	create_shopify_log(
		status="Info",
		method="ecommerce_integrations.shopify.connection.register_webhooks",
		message=_("Starting webhook registration for {0} events").format(len(WEBHOOK_EVENTS)),
	)

	# clear all stale webhooks matching current site url before registering new ones
	unregister_webhooks(shopify_url, password)

	callback_url = get_callback_url()
	create_shopify_log(
		status="Info",
		method="ecommerce_integrations.shopify.connection.register_webhooks",
		message=_("Callback URL: {0}").format(callback_url),
	)

	with Session.temp(shopify_url, API_VERSION, password):
		for topic in WEBHOOK_EVENTS:
			create_shopify_log(
				status="Info",
				method="ecommerce_integrations.shopify.connection.register_webhooks",
				message=_("Creating webhook for topic: {0} at {1}").format(topic, callback_url),
			)

			webhook = Webhook.create({"topic": topic, "address": callback_url, "format": "json"})

			create_shopify_log(
				status="Info",
				method="ecommerce_integrations.shopify.connection.register_webhooks",
				message=_("Webhook create response: {0}, is_valid: {1}").format(
					webhook.to_dict(), webhook.is_valid()
				),
			)

			if webhook.is_valid():
				new_webhooks.append(webhook)
				create_shopify_log(
					status="Success",
					method="ecommerce_integrations.shopify.connection.register_webhooks",
					message=_("Webhook registered successfully for topic: {0}, ID: {1}").format(
						topic, webhook.id
					),
				)
			else:
				create_shopify_log(
					status="Error",
					method="ecommerce_integrations.shopify.connection.register_webhooks",
					message=_("Webhook creation failed for topic: {0}").format(topic),
					response_data=webhook.to_dict(),
					exception=webhook.errors.full_messages(),
				)

	create_shopify_log(
		status="Info",
		method="ecommerce_integrations.shopify.connection.register_webhooks",
		message=_("Webhook registration completed. Total registered: {0}").format(len(new_webhooks)),
	)

	return new_webhooks


def unregister_webhooks(shopify_url: str, password: str) -> None:
	"""Unregister all webhooks from shopify that correspond to current site url."""
	url = get_current_domain_name()

	with Session.temp(shopify_url, API_VERSION, password):
		for webhook in Webhook.find():
			if url in webhook.address:
				webhook.destroy()


def get_current_domain_name() -> str:
	"""Get current site domain name. E.g. test.erpnext.com

	If developer_mode is enabled and localtunnel_url is set in site config then domain  is set to localtunnel_url.
	"""
	if frappe.conf.developer_mode and frappe.conf.localtunnel_url:
		return frappe.conf.localtunnel_url
	else:
		return frappe.request.host


def get_callback_url() -> str:
	"""Shopify calls this url when new events occur to subscribed webhooks.

	If developer_mode is enabled and localtunnel_url is set in site config then callback url is set to localtunnel_url.
	"""
	url = get_current_domain_name()

	return f"https://{url}/api/method/ecommerce_integrations.shopify.connection.store_request_data"


@frappe.whitelist(allow_guest=True)
def store_request_data() -> None:
	if frappe.request:
		create_shopify_log(
			status="Info",
			method="ecommerce_integrations.shopify.connection.store_request_data",
			message=_("Hit from Shopify"),
		)

		hmac_header = frappe.get_request_header("X-Shopify-Hmac-Sha256")

		_validate_request(frappe.request, hmac_header)

		data = json.loads(frappe.request.data)
		event = frappe.request.headers.get("X-Shopify-Topic")

		create_shopify_log(
			status="Info",
			method="ecommerce_integrations.shopify.connection.store_request_data",
			message=_("Webhook received: {0} for order_id: {1}").format(event, data.get("id", "unknown")),
		)

		process_request(data, event)


def process_request(data, event):
	# create log
	log = create_shopify_log(method=EVENT_MAPPER[event], request_data=data)

	create_shopify_log(
		status="Info",
		method="ecommerce_integrations.shopify.connection.process_request",
		message=_("Enqueueing background job for event: {0} with request_id: {1}").format(event, log.name),
	)

	# enqueue backround job
	frappe.enqueue(
		method=EVENT_MAPPER[event],
		queue="short",
		timeout=300,
		is_async=True,
		**{"payload": data, "request_id": log.name},
	)


def _validate_request(req, hmac_header):
	settings = frappe.get_doc(SETTING_DOCTYPE)

	# Get the appropriate secret key based on authentication method
	if settings.authentication_method == "OAuth 2.0 Client Credentials":
		# For OAuth apps, use client_secret for HMAC validation
		secret_key = settings.get_password("client_secret")
	elif settings.authentication_method == "Authorization Code Grant":
		# For Authorization Code Grant apps, use client_secret for HMAC validation
		secret_key = settings.get_password("client_secret")
	else:
		# For static token apps, use shared_secret
		secret_key = settings.shared_secret

	if not secret_key:
		create_shopify_log(status="Error", request_data=req.data, exception="Secret key not configured")
		frappe.throw(_("Webhook validation failed: Secret key not configured"))

	sig = base64.b64encode(hmac.new(secret_key.encode("utf8"), req.data, hashlib.sha256).digest())

	if sig != bytes(hmac_header.encode()):
		create_shopify_log(status="Error", request_data=req.data)
		frappe.throw(_("Unverified Webhook Data"))
