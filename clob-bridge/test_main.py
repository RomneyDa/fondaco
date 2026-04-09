"""Tests for clob-bridge HTTP server with mocked CLOB client."""
import json
from unittest.mock import MagicMock, patch
import pytest

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client():
    """Create a MagicMock standing in for ClobClient."""
    return MagicMock()


@pytest.fixture
def client(mock_client):
    """Flask test client with _get_client mocked out."""
    with patch("main._get_client", return_value=mock_client):
        from main import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /create_and_post_order
# ---------------------------------------------------------------------------

class TestCreateAndPostOrder:
    def _base_payload(self, **overrides):
        payload = {
            "token_id": "0xabc123",
            "price": "0.50",
            "size": "10",
            "side": "BUY",
        }
        payload.update(overrides)
        return payload

    def test_successful_order(self, client, mock_client):
        mock_client.create_order.return_value = {"signed": True}
        mock_client.post_order.return_value = {"orderID": "order-1", "status": "MATCHED"}

        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(self._base_payload()),
            content_type="application/json",
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["response"]["orderID"] == "order-1"
        mock_client.create_order.assert_called_once()
        mock_client.post_order.assert_called_once()

    def test_order_raises_returns_500(self, client, mock_client):
        mock_client.create_order.side_effect = RuntimeError("nonce too low")

        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(self._base_payload()),
            content_type="application/json",
        )

        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False
        assert "nonce too low" in body["error"]

    def test_zero_fill_response(self, client, mock_client):
        mock_client.create_order.return_value = {"signed": True}
        mock_client.post_order.return_value = {
            "orderID": "order-2",
            "status": "MATCHED",
            "matchedAmount": "0",
        }

        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(self._base_payload()),
            content_type="application/json",
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["response"]["matchedAmount"] == "0"

    def test_order_type_fak_default(self, client, mock_client):
        """When order_type is omitted, FAK is used."""
        mock_client.create_order.return_value = {"signed": True}
        mock_client.post_order.return_value = {}

        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(self._base_payload()),
            content_type="application/json",
        )
        assert resp.status_code == 200
        # post_order should have been called; the order_type is resolved inside
        # the handler — we just verify no crash and success.
        mock_client.post_order.assert_called_once()

    def test_order_type_gtd(self, client, mock_client):
        mock_client.create_order.return_value = {"signed": True}
        mock_client.post_order.return_value = {}

        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(self._base_payload(order_type="GTD")),
            content_type="application/json",
        )
        assert resp.status_code == 200
        mock_client.post_order.assert_called_once()

    def test_order_type_gtc(self, client, mock_client):
        mock_client.create_order.return_value = {"signed": True}
        mock_client.post_order.return_value = {}

        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(self._base_payload(order_type="GTC")),
            content_type="application/json",
        )
        assert resp.status_code == 200
        mock_client.post_order.assert_called_once()

    def test_expiration_parameter_passed(self, client, mock_client):
        """When expiration is set, it should be forwarded to OrderArgs."""
        mock_client.create_order.return_value = {"signed": True}
        mock_client.post_order.return_value = {}

        payload = self._base_payload(expiration="1700000000")
        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        # Verify create_order was called (expiration flows through OrderArgs)
        mock_client.create_order.assert_called_once()

    def test_missing_token_id_returns_500(self, client, mock_client):
        payload = {"price": "0.50", "size": "10", "side": "BUY"}
        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False

    def test_missing_price_returns_500(self, client, mock_client):
        payload = {"token_id": "0xabc", "size": "10", "side": "BUY"}
        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False

    def test_missing_side_returns_500(self, client, mock_client):
        payload = {"token_id": "0xabc", "price": "0.50", "size": "10"}
        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False

    def test_missing_size_returns_500(self, client, mock_client):
        payload = {"token_id": "0xabc", "price": "0.50", "side": "BUY"}
        resp = client.post(
            "/create_and_post_order",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False


# ---------------------------------------------------------------------------
# GET /get_order
# ---------------------------------------------------------------------------

class TestGetOrder:
    def test_get_order_success(self, client, mock_client):
        mock_client.get_order.return_value = {
            "id": "order-1",
            "status": "MATCHED",
            "size_matched": "10",
        }

        resp = client.get("/get_order?order_id=order-1")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["id"] == "order-1"
        mock_client.get_order.assert_called_once_with("order-1")

    def test_get_order_failure(self, client, mock_client):
        mock_client.get_order.side_effect = RuntimeError("not found")

        resp = client.get("/get_order?order_id=bad-id")
        assert resp.status_code == 500
        body = resp.get_json()
        assert "not found" in body["error"]


# ---------------------------------------------------------------------------
# POST /cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrder:
    def test_cancel_order_success(self, client, mock_client):
        mock_client.cancel.return_value = None

        resp = client.post(
            "/cancel_order",
            data=json.dumps({"order_id": "order-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        mock_client.cancel.assert_called_once_with("order-1")

    def test_cancel_order_failure(self, client, mock_client):
        mock_client.cancel.side_effect = RuntimeError("cancel rejected")

        resp = client.post(
            "/cancel_order",
            data=json.dumps({"order_id": "order-1"}),
            content_type="application/json",
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False
        assert "cancel rejected" in body["error"]

    def test_cancel_order_missing_id(self, client, mock_client):
        resp = client.post(
            "/cancel_order",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False


# ---------------------------------------------------------------------------
# POST /cancel_orders
# ---------------------------------------------------------------------------

class TestCancelOrders:
    def test_cancel_orders_success(self, client, mock_client):
        mock_client.cancel_orders.return_value = None

        resp = client.post(
            "/cancel_orders",
            data=json.dumps({"order_ids": ["order-1", "order-2"]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        mock_client.cancel_orders.assert_called_once_with(["order-1", "order-2"])

    def test_cancel_orders_failure(self, client, mock_client):
        mock_client.cancel_orders.side_effect = RuntimeError("batch cancel failed")

        resp = client.post(
            "/cancel_orders",
            data=json.dumps({"order_ids": ["order-1"]}),
            content_type="application/json",
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False
        assert "batch cancel failed" in body["error"]

    def test_cancel_orders_missing_ids(self, client, mock_client):
        resp = client.post(
            "/cancel_orders",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False


# ---------------------------------------------------------------------------
# GET /balance
# ---------------------------------------------------------------------------

class TestBalance:
    def test_balance_success(self, client, mock_client):
        mock_client.get_balance_allowance.return_value = {
            "balance": "100.0",
            "allowance": "999999",
        }

        resp = client.get("/balance?token_id=0xabc")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["balance"] == "100.0"
        mock_client.update_balance_allowance.assert_called_once()
        mock_client.get_balance_allowance.assert_called_once()

    def test_balance_failure(self, client, mock_client):
        mock_client.update_balance_allowance.side_effect = RuntimeError("rpc error")

        resp = client.get("/balance?token_id=0xabc")
        assert resp.status_code == 500
        body = resp.get_json()
        assert "rpc error" in body["error"]
