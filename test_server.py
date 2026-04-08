"""
Comprehensive test suite for Chock approval queue server.

Uses FastAPI TestClient with httpx. Each test gets a fresh temporary database
via the `client` fixture, so tests are fully independent.
"""

import json
import os
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    """
    Provide a TestClient backed by a fresh temp database for every test.

    We set the CHOCK_DB env var before importing the app module so that
    init_db() creates its tables in an isolated SQLite file.
    """
    db_path = str(tmp_path / "test_approvals.db")
    os.environ["CHOCK_DB"] = db_path
    # Also clear webhooks unless a test sets them explicitly
    os.environ.pop("CHOCK_WEBHOOKS", None)

    # Reload the module so DB_PATH picks up the new env var
    import importlib
    import server as srv
    importlib.reload(srv)

    # Manually trigger startup (TestClient sends startup event, but after
    # reload we need to make sure init_db uses the fresh DB_PATH)
    with TestClient(srv.app) as tc:
        yield tc


def _create_request(client, **overrides):
    """Helper: create a request and return the JSON response."""
    payload = {
        "title": "Deploy to production",
        "plan": "Run deploy script",
        "requester": "jim",
        "context": "Sprint 42 release",
        "tags": ["deploy", "prod"],
        "priority": "normal",
    }
    payload.update(overrides)
    resp = client.post("/api/requests", json=payload)
    return resp


# ---------------------------------------------------------------------------
# 1. Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_body(self, client):
        data = client.get("/health").json()
        assert data["status"] == "ok"
        assert data["service"] == "chock"
        assert "timestamp" in data


# ---------------------------------------------------------------------------
# 2. Request CRUD (create, list, get, cancel)
# ---------------------------------------------------------------------------

class TestRequestCRUD:
    def test_create_request_returns_201(self, client):
        resp = _create_request(client)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "pending"
        assert data["id"].startswith("chk-")

    def test_create_request_minimal(self, client):
        """Only title is truly required."""
        resp = client.post("/api/requests", json={"title": "Minimal"})
        assert resp.status_code == 201

    def test_get_request(self, client):
        req_id = _create_request(client).json()["id"]
        resp = client.get(f"/api/requests/{req_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == req_id
        assert data["title"] == "Deploy to production"
        assert data["status"] == "pending"
        assert data["requester"] == "jim"
        assert data["tags"] == ["deploy", "prod"]
        assert data["priority"] == "normal"

    def test_get_nonexistent_returns_404(self, client):
        resp = client.get("/api/requests/chk-doesnotexist")
        assert resp.status_code == 404

    def test_list_requests(self, client):
        _create_request(client, title="First")
        _create_request(client, title="Second")
        items = client.get("/api/requests").json()
        assert len(items) == 2
        # Newest first (ORDER BY created_at DESC)
        assert items[0]["title"] == "Second"
        assert items[1]["title"] == "First"

    def test_list_requests_empty(self, client):
        items = client.get("/api/requests").json()
        assert items == []

    def test_cancel_pending_request(self, client):
        req_id = _create_request(client).json()["id"]
        resp = client.delete(f"/api/requests/{req_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_removes_from_list(self, client):
        req_id = _create_request(client).json()["id"]
        client.delete(f"/api/requests/{req_id}")
        items = client.get("/api/requests").json()
        assert len(items) == 0

    def test_cancel_nonexistent_returns_404(self, client):
        resp = client.delete("/api/requests/chk-doesnotexist")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. Priority validation
# ---------------------------------------------------------------------------

class TestPriorityValidation:
    @pytest.mark.parametrize("priority", ["low", "normal", "high", "critical"])
    def test_valid_priorities_accepted(self, client, priority):
        resp = _create_request(client, priority=priority)
        assert resp.status_code == 201

    def test_invalid_priority_rejected(self, client):
        resp = _create_request(client, priority="urgent")
        assert resp.status_code == 400

    def test_invalid_priority_error_message(self, client):
        resp = _create_request(client, priority="mega")
        assert "Invalid priority" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 4. Respond to request (approve, deny, conditional)
# ---------------------------------------------------------------------------

class TestRespondToRequest:
    def test_approve(self, client):
        req_id = _create_request(client).json()["id"]
        resp = client.post(f"/api/requests/{req_id}/respond", json={
            "status": "approved",
            "responder": "jon",
            "comment": "Ship it",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert data["response"]["responder"] == "jon"
        assert data["response"]["comment"] == "Ship it"
        assert "responded_at" in data["response"]

    def test_deny(self, client):
        req_id = _create_request(client).json()["id"]
        resp = client.post(f"/api/requests/{req_id}/respond", json={
            "status": "denied",
            "responder": "jon",
            "comment": "Not yet",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "denied"

    def test_approve_updates_stored_status(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})
        data = client.get(f"/api/requests/{req_id}").json()
        assert data["status"] == "approved"
        assert data["response"]["status"] == "approved"

    def test_deny_updates_stored_status(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={"status": "denied"})
        data = client.get(f"/api/requests/{req_id}").json()
        assert data["status"] == "denied"

    def test_respond_invalid_status(self, client):
        req_id = _create_request(client).json()["id"]
        resp = client.post(f"/api/requests/{req_id}/respond", json={
            "status": "maybe",
        })
        assert resp.status_code == 400

    def test_respond_nonexistent_request(self, client):
        resp = client.post("/api/requests/chk-nope/respond", json={
            "status": "approved",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. Conditional approval with required conditions
# ---------------------------------------------------------------------------

class TestConditionalApproval:
    def test_conditional_with_conditions(self, client):
        req_id = _create_request(client, conditions_schema=[
            {"key": "max_budget", "label": "Max budget", "type": "number", "required": True},
            {"key": "notes", "label": "Notes", "type": "text", "required": False},
        ]).json()["id"]

        resp = client.post(f"/api/requests/{req_id}/respond", json={
            "status": "conditional",
            "responder": "jon",
            "comment": "OK but capped",
            "conditions": {"max_budget": 5000, "notes": "Keep it lean"},
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "conditional"
        assert resp.json()["response"]["conditions"]["max_budget"] == 5000

    def test_conditional_missing_required_condition(self, client):
        req_id = _create_request(client, conditions_schema=[
            {"key": "max_budget", "label": "Max budget", "type": "number", "required": True},
            {"key": "deadline", "label": "Deadline", "type": "text", "required": True},
        ]).json()["id"]

        resp = client.post(f"/api/requests/{req_id}/respond", json={
            "status": "conditional",
            "conditions": {"max_budget": 1000},
            # deadline is missing
        })
        assert resp.status_code == 400
        assert "deadline" in resp.json()["detail"]

    def test_conditional_all_required_present(self, client):
        req_id = _create_request(client, conditions_schema=[
            {"key": "max_budget", "label": "Max budget", "type": "number", "required": True},
            {"key": "deadline", "label": "Deadline", "type": "text", "required": True},
        ]).json()["id"]

        resp = client.post(f"/api/requests/{req_id}/respond", json={
            "status": "conditional",
            "conditions": {"max_budget": 1000, "deadline": "Friday"},
        })
        assert resp.status_code == 200

    def test_conditional_no_schema_still_works(self, client):
        """A request without conditions_schema can still be conditionally approved."""
        req_id = _create_request(client).json()["id"]
        resp = client.post(f"/api/requests/{req_id}/respond", json={
            "status": "conditional",
            "conditions": {"whatever": "value"},
        })
        assert resp.status_code == 200

    def test_conditional_stored_on_request(self, client):
        req_id = _create_request(client, conditions_schema=[
            {"key": "env", "label": "Environment", "type": "text", "required": True},
        ]).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={
            "status": "conditional",
            "conditions": {"env": "staging"},
        })
        data = client.get(f"/api/requests/{req_id}").json()
        assert data["status"] == "conditional"
        assert data["response"]["conditions"]["env"] == "staging"


# ---------------------------------------------------------------------------
# 6. Can't respond twice
# ---------------------------------------------------------------------------

class TestNoDoubleResponse:
    def test_cannot_approve_twice(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})
        resp = client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})
        assert resp.status_code == 400
        assert "Already responded" in resp.json()["detail"]

    def test_cannot_deny_after_approve(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})
        resp = client.post(f"/api/requests/{req_id}/respond", json={"status": "denied"})
        assert resp.status_code == 400

    def test_cannot_approve_after_deny(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={"status": "denied"})
        resp = client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})
        assert resp.status_code == 400

    def test_cannot_respond_after_conditional(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={
            "status": "conditional", "conditions": {"x": 1}
        })
        resp = client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 7. Can't cancel non-pending
# ---------------------------------------------------------------------------

class TestCancelNonPending:
    def test_cannot_cancel_approved(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})
        resp = client.delete(f"/api/requests/{req_id}")
        assert resp.status_code == 400
        assert "Cannot cancel" in resp.json()["detail"]

    def test_cannot_cancel_denied(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={"status": "denied"})
        resp = client.delete(f"/api/requests/{req_id}")
        assert resp.status_code == 400

    def test_cannot_cancel_conditional(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={
            "status": "conditional", "conditions": {}
        })
        resp = client.delete(f"/api/requests/{req_id}")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 8. Search parameters (q, tag, priority, status, requester)
# ---------------------------------------------------------------------------

class TestSearchParameters:
    def _seed(self, client):
        """Seed 4 requests with different attributes."""
        _create_request(client, title="Deploy frontend", requester="jim",
                        tags=["deploy", "frontend"], priority="high")
        _create_request(client, title="Deploy backend", requester="jim",
                        tags=["deploy", "backend"], priority="normal")
        _create_request(client, title="Update docs", requester="pam",
                        tags=["docs"], priority="low")
        _create_request(client, title="Security audit", requester="dwight",
                        tags=["security"], priority="critical")

    def test_filter_by_status(self, client):
        self._seed(client)
        items = client.get("/api/requests", params={"status": "pending"}).json()
        assert len(items) == 4  # all are pending

    def test_filter_by_requester(self, client):
        self._seed(client)
        items = client.get("/api/requests", params={"requester": "jim"}).json()
        assert len(items) == 2
        assert all(i["requester"] == "jim" for i in items)

    def test_filter_by_priority(self, client):
        self._seed(client)
        items = client.get("/api/requests", params={"priority": "critical"}).json()
        assert len(items) == 1
        assert items[0]["title"] == "Security audit"

    def test_filter_by_tag(self, client):
        self._seed(client)
        items = client.get("/api/requests", params={"tag": "deploy"}).json()
        assert len(items) == 2

    def test_filter_by_tag_single_match(self, client):
        self._seed(client)
        items = client.get("/api/requests", params={"tag": "security"}).json()
        assert len(items) == 1
        assert items[0]["title"] == "Security audit"

    def test_text_search_title(self, client):
        self._seed(client)
        items = client.get("/api/requests", params={"q": "Security"}).json()
        assert len(items) == 1
        assert items[0]["title"] == "Security audit"

    def test_text_search_plan(self, client):
        _create_request(client, title="Boring title", plan="Run the migration scripts")
        items = client.get("/api/requests", params={"q": "migration"}).json()
        assert len(items) == 1

    def test_text_search_context(self, client):
        _create_request(client, title="Boring", context="Related to the billing system")
        items = client.get("/api/requests", params={"q": "billing"}).json()
        assert len(items) == 1

    def test_text_search_no_match(self, client):
        self._seed(client)
        items = client.get("/api/requests", params={"q": "nonexistent"}).json()
        assert len(items) == 0

    def test_combined_filters(self, client):
        self._seed(client)
        items = client.get("/api/requests", params={
            "requester": "jim", "priority": "high"
        }).json()
        assert len(items) == 1
        assert items[0]["title"] == "Deploy frontend"

    def test_invalid_status_filter(self, client):
        resp = client.get("/api/requests", params={"status": "bogus"})
        assert resp.status_code == 400

    def test_invalid_priority_filter(self, client):
        resp = client.get("/api/requests", params={"priority": "bogus"})
        assert resp.status_code == 400

    def test_filter_by_status_after_approval(self, client):
        self._seed(client)
        # Approve the first request we can find
        items = client.get("/api/requests").json()
        req_id = items[0]["id"]
        client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})

        approved = client.get("/api/requests", params={"status": "approved"}).json()
        assert len(approved) == 1
        assert approved[0]["id"] == req_id

        pending = client.get("/api/requests", params={"status": "pending"}).json()
        assert len(pending) == 3


# ---------------------------------------------------------------------------
# 9. Pending queue ordering (by priority then age)
# ---------------------------------------------------------------------------

class TestPendingQueueOrdering:
    def test_pending_ordered_by_priority_then_age(self, client):
        """Critical first, then high, normal, low. Within same priority: oldest first."""
        _create_request(client, title="Low 1", priority="low")
        _create_request(client, title="Normal 1", priority="normal")
        _create_request(client, title="Critical 1", priority="critical")
        _create_request(client, title="High 1", priority="high")
        _create_request(client, title="Normal 2", priority="normal")
        _create_request(client, title="Critical 2", priority="critical")

        items = client.get("/api/pending").json()
        assert len(items) == 6
        titles = [i["title"] for i in items]

        # Critical first (oldest first within tier)
        assert titles[0] == "Critical 1"
        assert titles[1] == "Critical 2"
        # Then high
        assert titles[2] == "High 1"
        # Then normal (oldest first)
        assert titles[3] == "Normal 1"
        assert titles[4] == "Normal 2"
        # Then low
        assert titles[5] == "Low 1"

    def test_pending_excludes_responded(self, client):
        req_id = _create_request(client, title="Will approve").json()["id"]
        _create_request(client, title="Still pending")
        client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})

        items = client.get("/api/pending").json()
        assert len(items) == 1
        assert items[0]["title"] == "Still pending"

    def test_pending_excludes_cancelled(self, client):
        req_id = _create_request(client, title="Will cancel").json()["id"]
        _create_request(client, title="Still pending")
        client.delete(f"/api/requests/{req_id}")

        items = client.get("/api/pending").json()
        assert len(items) == 1
        assert items[0]["title"] == "Still pending"

    def test_pending_empty_queue(self, client):
        items = client.get("/api/pending").json()
        assert items == []


# ---------------------------------------------------------------------------
# 10. Activity log
# ---------------------------------------------------------------------------

class TestActivityLog:
    def test_create_logs_activity(self, client):
        _create_request(client, title="Log me", requester="jim")
        activity = client.get("/api/activity").json()
        assert len(activity) >= 1
        entry = activity[0]
        assert entry["action"] == "created"
        assert entry["actor"] == "jim"
        assert "Log me" in entry["details"]

    def test_approve_logs_activity(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={
            "status": "approved", "responder": "jon", "comment": "LGTM"
        })
        activity = client.get("/api/activity").json()
        # Most recent first
        assert activity[0]["action"] == "responded_approved"
        assert activity[0]["actor"] == "jon"
        assert "approved" in activity[0]["details"]

    def test_deny_logs_activity(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={
            "status": "denied", "responder": "jon", "comment": "Nope"
        })
        activity = client.get("/api/activity").json()
        assert activity[0]["action"] == "responded_denied"

    def test_conditional_logs_activity(self, client):
        req_id = _create_request(client).json()["id"]
        client.post(f"/api/requests/{req_id}/respond", json={
            "status": "conditional", "responder": "jon", "conditions": {"x": 1}
        })
        activity = client.get("/api/activity").json()
        assert activity[0]["action"] == "responded_conditional"

    def test_cancel_logs_activity(self, client):
        req_id = _create_request(client).json()["id"]
        client.delete(f"/api/requests/{req_id}", params={"requester": "jim"})
        activity = client.get("/api/activity").json()
        assert activity[0]["action"] == "cancelled"
        assert activity[0]["actor"] == "jim"

    def test_activity_limit(self, client):
        for i in range(5):
            _create_request(client, title=f"Request {i}")
        activity = client.get("/api/activity", params={"limit": 3}).json()
        assert len(activity) == 3

    def test_activity_default_limit(self, client):
        """Default limit is 50, but we just verify it returns everything when under."""
        for i in range(3):
            _create_request(client, title=f"Request {i}")
        activity = client.get("/api/activity").json()
        assert len(activity) == 3

    def test_activity_ordering(self, client):
        """Activity is returned newest first."""
        _create_request(client, title="First")
        _create_request(client, title="Second")
        activity = client.get("/api/activity").json()
        assert activity[0]["details"] == "Second"
        assert activity[1]["details"] == "First"

    def test_activity_has_request_id(self, client):
        req_id = _create_request(client).json()["id"]
        activity = client.get("/api/activity").json()
        assert activity[0]["request_id"] == req_id


# ---------------------------------------------------------------------------
# 11. Webhook firing (mock urllib)
# ---------------------------------------------------------------------------

class TestWebhooks:
    def test_webhook_fires_on_create(self, client, tmp_path):
        """Webhooks fire when a request is created."""
        # We need to reload the server module with CHOCK_WEBHOOKS set
        os.environ["CHOCK_WEBHOOKS"] = "http://hook.example.com/create"
        import importlib
        import server as srv
        importlib.reload(srv)

        with patch.object(srv.urllib.request, "urlopen") as mock_urlopen, \
             TestClient(srv.app) as wh_client:
            resp = wh_client.post("/api/requests", json={
                "title": "Webhook test", "requester": "jim"
            })
            assert resp.status_code == 201

            # Give the background thread a moment to fire
            import time
            time.sleep(0.3)

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req_obj = call_args[0][0]
            body = json.loads(req_obj.data)
            assert body["event"] == "request_created"
            assert body["details"]["title"] == "Webhook test"
            assert body["details"]["requester"] == "jim"

    def test_webhook_fires_on_approve(self, client, tmp_path):
        os.environ["CHOCK_WEBHOOKS"] = "http://hook.example.com/respond"
        import importlib
        import server as srv
        importlib.reload(srv)

        with patch.object(srv.urllib.request, "urlopen") as mock_urlopen, \
             TestClient(srv.app) as wh_client:
            req_id = wh_client.post("/api/requests", json={
                "title": "Approve me", "requester": "jim"
            }).json()["id"]

            # Reset mock to isolate the approve webhook call
            mock_urlopen.reset_mock()

            wh_client.post(f"/api/requests/{req_id}/respond", json={
                "status": "approved", "responder": "jon"
            })

            time.sleep(0.3)

            mock_urlopen.assert_called_once()
            req_obj = mock_urlopen.call_args[0][0]
            body = json.loads(req_obj.data)
            assert body["event"] == "request_approved"
            assert body["details"]["responder"] == "jon"

    def test_webhook_fires_on_deny(self, client, tmp_path):
        os.environ["CHOCK_WEBHOOKS"] = "http://hook.example.com/respond"
        import importlib
        import server as srv
        importlib.reload(srv)

        with patch.object(srv.urllib.request, "urlopen") as mock_urlopen, \
             TestClient(srv.app) as wh_client:
            req_id = wh_client.post("/api/requests", json={
                "title": "Deny me", "requester": "jim"
            }).json()["id"]
            mock_urlopen.reset_mock()

            wh_client.post(f"/api/requests/{req_id}/respond", json={
                "status": "denied", "responder": "jon"
            })

            time.sleep(0.3)

            mock_urlopen.assert_called_once()
            req_obj = mock_urlopen.call_args[0][0]
            body = json.loads(req_obj.data)
            assert body["event"] == "request_denied"

    def test_webhook_fires_conditional_with_conditions(self, client, tmp_path):
        os.environ["CHOCK_WEBHOOKS"] = "http://hook.example.com/respond"
        import importlib
        import server as srv
        importlib.reload(srv)

        with patch.object(srv.urllib.request, "urlopen") as mock_urlopen, \
             TestClient(srv.app) as wh_client:
            req_id = wh_client.post("/api/requests", json={
                "title": "Conditional me", "requester": "jim"
            }).json()["id"]
            mock_urlopen.reset_mock()

            wh_client.post(f"/api/requests/{req_id}/respond", json={
                "status": "conditional",
                "responder": "jon",
                "conditions": {"budget": 500},
            })

            time.sleep(0.3)

            mock_urlopen.assert_called_once()
            req_obj = mock_urlopen.call_args[0][0]
            body = json.loads(req_obj.data)
            assert body["event"] == "request_conditional"
            assert body["details"]["conditions"]["budget"] == 500

    def test_multiple_webhook_urls(self, client, tmp_path):
        os.environ["CHOCK_WEBHOOKS"] = "http://hook1.example.com,http://hook2.example.com"
        import importlib
        import server as srv
        importlib.reload(srv)

        with patch.object(srv.urllib.request, "urlopen") as mock_urlopen, \
             TestClient(srv.app) as wh_client:
            wh_client.post("/api/requests", json={"title": "Multi-hook"})

            time.sleep(0.3)

            assert mock_urlopen.call_count == 2

    def test_no_webhooks_when_empty(self, client, tmp_path):
        """No webhook calls when CHOCK_WEBHOOKS is not set."""
        os.environ.pop("CHOCK_WEBHOOKS", None)
        import importlib
        import server as srv
        importlib.reload(srv)

        with patch.object(srv.urllib.request, "urlopen") as mock_urlopen, \
             TestClient(srv.app) as wh_client:
            wh_client.post("/api/requests", json={"title": "No hooks"})
            time.sleep(0.2)
            mock_urlopen.assert_not_called()

    def test_callback_fires_on_respond(self, client, tmp_path):
        """When a request has a callback_url, it fires on response."""
        os.environ.pop("CHOCK_WEBHOOKS", None)
        import importlib
        import server as srv
        importlib.reload(srv)

        with patch.object(srv.urllib.request, "urlopen") as mock_urlopen, \
             TestClient(srv.app) as wh_client:
            req_id = wh_client.post("/api/requests", json={
                "title": "Callback test",
                "requester": "jim",
                "callback_url": "http://agent.local/callback",
            }).json()["id"]

            mock_urlopen.reset_mock()

            wh_client.post(f"/api/requests/{req_id}/respond", json={
                "status": "approved", "responder": "jon"
            })

            time.sleep(0.3)

            # Should have fired the callback (no webhooks configured)
            mock_urlopen.assert_called_once()
            req_obj = mock_urlopen.call_args[0][0]
            assert req_obj.full_url == "http://agent.local/callback"
            body = json.loads(req_obj.data)
            assert body["request_id"] == req_id
            assert body["result"]["status"] == "approved"


# ---------------------------------------------------------------------------
# Edge cases and data integrity
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_tags_stored_and_returned_as_list(self, client):
        req_id = _create_request(client, tags=["a", "b", "c"]).json()["id"]
        data = client.get(f"/api/requests/{req_id}").json()
        assert data["tags"] == ["a", "b", "c"]

    def test_empty_tags(self, client):
        req_id = client.post("/api/requests", json={"title": "No tags"}).json()["id"]
        data = client.get(f"/api/requests/{req_id}").json()
        assert data["tags"] == []

    def test_conditions_schema_round_trip(self, client):
        schema = [
            {"key": "budget", "label": "Budget", "type": "number", "required": True,
             "options": [], "description": "Max spend"},
        ]
        req_id = _create_request(client, conditions_schema=schema).json()["id"]
        data = client.get(f"/api/requests/{req_id}").json()
        assert len(data["conditions_schema"]) == 1
        assert data["conditions_schema"][0]["key"] == "budget"
        assert data["conditions_schema"][0]["required"] is True

    def test_response_is_none_when_pending(self, client):
        req_id = _create_request(client).json()["id"]
        data = client.get(f"/api/requests/{req_id}").json()
        assert data["response"] is None

    def test_created_at_and_updated_at_set(self, client):
        req_id = _create_request(client).json()["id"]
        data = client.get(f"/api/requests/{req_id}").json()
        assert data["created_at"] is not None
        assert data["updated_at"] is not None

    def test_updated_at_changes_on_respond(self, client):
        req_id = _create_request(client).json()["id"]
        data_before = client.get(f"/api/requests/{req_id}").json()
        client.post(f"/api/requests/{req_id}/respond", json={"status": "approved"})
        data_after = client.get(f"/api/requests/{req_id}").json()
        assert data_after["updated_at"] >= data_before["updated_at"]

    def test_id_format(self, client):
        req_id = _create_request(client).json()["id"]
        assert req_id.startswith("chk-")
        assert len(req_id) == 12  # "chk-" + 8 hex chars

    def test_cancel_with_requester_param(self, client):
        req_id = _create_request(client, requester="jim").json()["id"]
        resp = client.delete(f"/api/requests/{req_id}", params={"requester": "jim"})
        assert resp.status_code == 200
        activity = client.get("/api/activity").json()
        cancel_entry = [a for a in activity if a["action"] == "cancelled"][0]
        assert cancel_entry["actor"] == "jim"


# ---------------------------------------------------------------------------
# Auth middleware tests
# ---------------------------------------------------------------------------

class TestAuth:
    """Test API key authentication middleware.

    These tests set CHOCK_API_KEY and reload the server module to activate
    the auth middleware, following the same pattern as TestWebhooks.
    """

    def _make_auth_client(self, tmp_path, api_key="test-secret-key"):
        """Create a TestClient with API key auth enabled."""
        db_path = str(tmp_path / "test_auth.db")
        os.environ["CHOCK_DB"] = db_path
        os.environ["CHOCK_API_KEY"] = api_key
        os.environ.pop("CHOCK_WEBHOOKS", None)

        import importlib
        import server as srv
        importlib.reload(srv)

        return TestClient(srv.app)

    def _cleanup_env(self):
        """Remove CHOCK_API_KEY from env after test."""
        os.environ.pop("CHOCK_API_KEY", None)

    def test_auth_blocks_write_without_key(self, tmp_path):
        """POST to /api/requests without API key should return 401."""
        tc = self._make_auth_client(tmp_path)
        try:
            with tc:
                resp = tc.post("/api/requests", json={"title": "Should fail"})
                assert resp.status_code == 401
                assert "Invalid or missing API key" in resp.json()["detail"]
        finally:
            self._cleanup_env()

    def test_auth_allows_write_with_key(self, tmp_path):
        """POST to /api/requests with correct X-API-Key header should return 201."""
        tc = self._make_auth_client(tmp_path)
        try:
            with tc:
                resp = tc.post("/api/requests",
                               json={"title": "Should work"},
                               headers={"X-API-Key": "test-secret-key"})
                assert resp.status_code == 201
        finally:
            self._cleanup_env()

    def test_auth_bearer_header(self, tmp_path):
        """Authorization: Bearer header should authenticate writes."""
        tc = self._make_auth_client(tmp_path)
        try:
            with tc:
                resp = tc.post("/api/requests",
                               json={"title": "Bearer test"},
                               headers={"Authorization": "Bearer test-secret-key"})
                assert resp.status_code == 201
        finally:
            self._cleanup_env()

    def test_auth_wrong_key(self, tmp_path):
        """Wrong API key should return 401."""
        tc = self._make_auth_client(tmp_path)
        try:
            with tc:
                resp = tc.post("/api/requests",
                               json={"title": "Wrong key"},
                               headers={"X-API-Key": "wrong-key"})
                assert resp.status_code == 401
        finally:
            self._cleanup_env()

    def test_auth_reads_work_without_key(self, tmp_path):
        """GET /api/requests and GET /health should work without an API key."""
        tc = self._make_auth_client(tmp_path)
        try:
            with tc:
                resp = tc.get("/api/requests")
                assert resp.status_code == 200

                resp = tc.get("/health")
                assert resp.status_code == 200
        finally:
            self._cleanup_env()

    def test_auth_health_exempt(self, tmp_path):
        """/health is always accessible, even without any key."""
        tc = self._make_auth_client(tmp_path)
        try:
            with tc:
                resp = tc.get("/health")
                assert resp.status_code == 200
                assert resp.json()["status"] == "ok"
        finally:
            self._cleanup_env()
