# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

"""
OAuth 2.0 Client Credentials Flow for Shopify Apps
Implements token generation and refresh for apps created via Shopify Dev Dashboard
"""

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta

import requests

import frappe
from frappe import _
from frappe.utils import get_datetime, get_datetime_str, now_datetime
from frappe.utils.password import set_encrypted_password

from ecommerce_integrations.shopify.utils import create_shopify_log


def get_oauth_token_endpoint(shopify_url: str) -> str:
	"""
	Construct the OAuth token endpoint URL for a given shop.

	Args:
	        shopify_url: The shop URL (e.g., 'example.myshopify.com')

	Returns:
	        Full OAuth token endpoint URL
	"""
	shop_url = shopify_url.replace("https://", "").replace("http://", "")
	return f"https://{shop_url}/admin/oauth/access_token"


def generate_oauth_token(shopify_url: str, client_id: str, client_secret: str) -> dict:
	"""
	Generate a new OAuth 2.0 access token using client credentials flow.

	Args:
	        shopify_url: The shop URL
	        client_id: OAuth client ID from Shopify Partner Dashboard
	        client_secret: OAuth client secret from Shopify Partner Dashboard

	Returns:
	        Dictionary containing:
	                - access_token: The OAuth access token
	                - expires_in: Token validity duration in seconds (86399 = 24 hours)
	                - scope: Granted scopes

	Raises:
	        frappe.ValidationError: If token generation fails
	"""
	token_endpoint = get_oauth_token_endpoint(shopify_url)

	payload = {
		"grant_type": "client_credentials",
		"client_id": client_id,
		"client_secret": client_secret,
	}

	headers = {
		"Content-Type": "application/x-www-form-urlencoded",
	}

	try:
		response = requests.post(token_endpoint, data=payload, headers=headers, timeout=30)
		response.raise_for_status()

		token_data = response.json()

		# Log successful token generation
		create_shopify_log(
			status="Success",
			method="ecommerce_integrations.shopify.oauth.generate_oauth_token",
			message=_("OAuth token generated successfully"),
		)

		return token_data

	except requests.exceptions.RequestException as e:
		error_message = str(e)
		error_response = None

		if hasattr(e, "response") and e.response is not None:
			try:
				error_response = e.response.json()
				error_message = error_response.get("error_description", error_response.get("error", str(e)))
			except json.JSONDecodeError:
				error_message = e.response.text or str(e)

		# Sanitize payload before logging - remove sensitive credentials
		sanitized_payload = payload.copy()
		sanitized_payload["client_secret"] = "REDACTED"

		# Log the error
		create_shopify_log(
			status="Error",
			method="ecommerce_integrations.shopify.oauth.generate_oauth_token",
			message=_("Failed to generate OAuth token"),
			exception=error_message,
			request_data=sanitized_payload,
			response_data=error_response,
		)

		frappe.throw(
			_("Failed to generate OAuth token: {0}").format(error_message),
			title=_("OAuth Authentication Error"),
		)


def is_token_valid(token_expires_at: datetime, buffer_minutes: int = 5) -> bool:
	"""
	Check if the OAuth token is still valid with a buffer period.

	Args:
	        token_expires_at: Datetime when the token expires
	        buffer_minutes: Minutes before expiry to consider token invalid (default: 5)

	Returns:
	        True if token is valid and not expiring soon, False otherwise
	"""
	if not token_expires_at:
		return False

	expiry_datetime = get_datetime(token_expires_at)
	buffer_time = now_datetime() + timedelta(minutes=buffer_minutes)

	return expiry_datetime > buffer_time


def calculate_token_expiry(expires_in_seconds: int) -> datetime:
	"""
	Calculate the exact expiry datetime for a token.

	Args:
	        expires_in_seconds: Validity duration in seconds (typically 86399 for Shopify)

	Returns:
	        Datetime when the token will expire
	"""
	return now_datetime() + timedelta(seconds=expires_in_seconds)


def refresh_oauth_token(setting) -> str:
	"""
	Refresh the OAuth token and update the setting document.
	This is called when the token is expired or about to expire.

	Args:
	        setting: ShopifySetting document instance

	Returns:
	        The new access token

	Raises:
	        frappe.ValidationError: If token refresh fails
	"""
	if setting.authentication_method != "OAuth 2.0 Client Credentials":
		frappe.throw(
			_("Token refresh is only applicable for OAuth 2.0 authentication"),
			title=_("Invalid Authentication Method"),
		)

	# Check one more time with fresh data
	setting.reload()

	# Get fresh token
	token_data = generate_oauth_token(
		setting.shopify_url,
		setting.client_id,
		setting.get_password("client_secret"),
	)

	# Calculate expiry time
	expires_at = calculate_token_expiry(token_data.get("expires_in", 86399))

	set_encrypted_password(
		"Shopify Setting",
		setting.name,
		token_data["access_token"],
		fieldname="oauth_access_token",
	)

	frappe.db.set_value(
		"Shopify Setting",
		setting.name,
		"token_expires_at",
		get_datetime_str(expires_at),
		update_modified=False,
	)

	setting.reload()

	return token_data["access_token"]


def get_valid_access_token(setting) -> str:
	"""
	Get a valid OAuth access token, refreshing if necessary.

	Args:
	        setting: ShopifySetting document instance

	Returns:
	        A valid access token ready to use

	Raises:
	        frappe.ValidationError: If unable to get a valid token
	"""
	if setting.authentication_method != "OAuth 2.0 Client Credentials":
		frappe.throw(
			_("This method is only for OAuth 2.0 authentication"),
			title=_("Invalid Authentication Method"),
		)

	# Check if we already have a valid token
	if is_token_valid(setting.token_expires_at):
		current_token = setting.get_password("oauth_access_token", raise_exception=False)
		if current_token:
			return current_token

	# Token is invalid/missing - refresh it
	try:
		return refresh_oauth_token(setting)
	except Exception as e:
		# Single retry for transient network issues
		create_shopify_log(
			status="Warning",
			method="ecommerce_integrations.shopify.oauth.get_valid_access_token",
			message=_("Token refresh failed, retrying once..."),
			exception=str(e),
		)
		time.sleep(1)  # Brief pause
		return refresh_oauth_token(setting)  # Let this throw if it fails


def validate_oauth_credentials(shopify_url: str, client_id: str, client_secret: str) -> bool:
	"""
	Validate OAuth credentials by attempting to generate a token.
	Used during setup to verify credentials are correct.

	Args:
	        shopify_url: The shop URL
	        client_id: OAuth client ID
	        client_secret: OAuth client secret

	Returns:
	        True if credentials are valid

	Raises:
	        frappe.ValidationError: If credentials are invalid
	"""
	try:
		token_data = generate_oauth_token(shopify_url, client_id, client_secret)
		return bool(token_data.get("access_token"))
	except Exception:
		# Error is already logged and thrown by generate_oauth_token
		raise


# ============================================================================
# Authorization Code Grant Flow
# ============================================================================


def get_authorization_url(shopify_url: str, client_id: str, redirect_uri: str, scopes: str) -> str:
	"""
	Build the Shopify authorization URL for the Authorization Code Grant flow.

	Args:
	        shopify_url: The shop URL (e.g., 'example.myshopify.com')
	        client_id: OAuth client ID from Shopify Partner Dashboard
	        redirect_uri: Callback URL to receive the authorization code
	        scopes: Comma-separated list of scopes

	Returns:
	        Full authorization URL to redirect the user to
	"""
	shop_url = shopify_url.replace("https://", "").replace("http://", "")
	state = secrets.token_hex(16)

	# Store state in cache for validation
	frappe.cache().set_value(f"shopify_oauth_state_{state}", state, expires_in_sec=600)

	auth_url = (
		f"https://{shop_url}/admin/oauth/authorize"
		f"?client_id={client_id}"
		f"&scope={scopes}"
		f"&redirect_uri={redirect_uri}"
		f"&state={state}"
	)

	return auth_url


def exchange_code_for_token(shopify_url: str, client_id: str, client_secret: str, code: str) -> dict:
	"""
	Exchange an authorization code for a permanent access token.

	Args:
	        shopify_url: The shop URL
	        client_id: OAuth client ID
	        client_secret: OAuth client secret
	        code: Authorization code received from Shopify callback

	Returns:
	        Dictionary containing access_token and scope

	Raises:
	        frappe.ValidationError: If token exchange fails
	"""
	token_endpoint = get_oauth_token_endpoint(shopify_url)

	payload = {
		"client_id": client_id,
		"client_secret": client_secret,
		"code": code,
	}

	headers = {
		"Content-Type": "application/x-www-form-urlencoded",
	}

	try:
		response = requests.post(token_endpoint, data=payload, headers=headers, timeout=30)
		response.raise_for_status()

		token_data = response.json()

		create_shopify_log(
			status="Success",
			method="ecommerce_integrations.shopify.oauth.exchange_code_for_token",
			message=_("Authorization Code token exchange successful"),
		)

		return token_data

	except requests.exceptions.RequestException as e:
		error_message = str(e)

		if hasattr(e, "response") and e.response is not None:
			try:
				error_response = e.response.json()
				error_message = error_response.get("error_description", error_response.get("error", str(e)))
			except json.JSONDecodeError:
				error_message = e.response.text or str(e)

		create_shopify_log(
			status="Error",
			method="ecommerce_integrations.shopify.oauth.exchange_code_for_token",
			message=_("Failed to exchange authorization code for token"),
			exception=error_message,
		)

		frappe.throw(
			_("Failed to exchange authorization code: {0}").format(error_message),
			title=_("OAuth Token Exchange Error"),
		)


# OAuth callback must be public; user is not yet authenticated.
@frappe.whitelist(allow_guest=True)  # nosemgrep
def shopify_oauth_callback():
	"""
	Callback endpoint for Shopify OAuth Authorization Code flow.
	Shopify redirects here after user authorizes the app.
	"""
	code = frappe.form_dict.get("code")
	state = frappe.form_dict.get("state")
	shop = frappe.form_dict.get("shop")

	if not code or not state or not shop:
		frappe.throw(_("Invalid OAuth callback: missing parameters"))

	# Validate state
	stored_state = frappe.cache().get_value(f"shopify_oauth_state_{state}")
	if not stored_state:
		frappe.throw(_("Invalid OAuth callback: state mismatch or expired"))

	# Clear used state
	frappe.cache().delete_value(f"shopify_oauth_state_{state}")

	# Get settings
	setting = frappe.get_doc("Shopify Setting")

	# Exchange code for token
	token_data = exchange_code_for_token(
		setting.shopify_url,
		setting.client_id,
		setting.get_password("client_secret"),
		code,
	)

	access_token = token_data.get("access_token")
	if not access_token:
		frappe.throw(_("No access token received from Shopify"))

	create_shopify_log(
		status="Info",
		method="ecommerce_integrations.shopify.oauth.shopify_oauth_callback",
		message=_("Token received from Shopify: {0}").format(
			access_token[:20] + "..." if len(access_token) > 20 else access_token
		),
	)

	create_shopify_log(
		status="Info",
		method="ecommerce_integrations.shopify.oauth.shopify_oauth_callback",
		message=_("About to save token to database"),
	)

	# Store the token - authenticate as Administrator for password save
	frappe.set_user("Administrator")
	try:
		set_encrypted_password(
			"Shopify Setting",
			setting.name,
			access_token,
			fieldname="authorization_code_token",
		)
		frappe.db.commit()

		# Verify the save
		saved_token = setting.get_password("authorization_code_token", raise_exception=False)
		create_shopify_log(
			status="Info",
			method="ecommerce_integrations.shopify.oauth.shopify_oauth_callback",
			message=_("Token saved to database. Verification: {0}").format(
				saved_token[:20] + "..." if saved_token and len(saved_token) > 20 else saved_token
			),
		)
	except Exception as e:
		create_shopify_log(
			status="Error",
			method="ecommerce_integrations.shopify.oauth.shopify_oauth_callback",
			message=_("Failed to save token to database: {0}").format(str(e)),
			exception=str(e),
		)
		frappe.throw(_("Failed to save token: {0}").format(str(e)))

	# Redirect back to Shopify Settings
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = "/app/shopify-setting"


@frappe.whitelist()
def get_auth_code_url():
	"""
	Generate the authorization URL for the Authorization Code flow.
	Called from the 'Authorize App' button in Shopify Settings.
	"""
	setting = frappe.get_doc("Shopify Setting")

	if not setting.client_id:
		frappe.throw(_("Client ID is required for Authorization Code Grant"))

	if not setting.shopify_url:
		frappe.throw(_("Shop URL is required"))

	# Build redirect URI
	site_url = frappe.utils.get_url()
	redirect_uri = f"{site_url}/api/method/ecommerce_integrations.shopify.oauth.shopify_oauth_callback"

	# Scopes - match what the app has configured
	scopes = "read_customers,write_customers,read_orders,write_orders,read_products,write_products,read_inventory,write_inventory,read_locations"

	auth_url = get_authorization_url(
		setting.shopify_url,
		setting.client_id,
		redirect_uri,
		scopes,
	)

	return auth_url
