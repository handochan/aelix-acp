import unittest

from aelix_acp.runtime.provider_errors import provider_failure_kind


class ProviderCauseTests(unittest.TestCase):
    def test_http_status_prefix(self):
        for status, kind in ((400, "provider_bad_request"), (404, "provider_bad_request"),
                             (422, "provider_bad_request"), (401, "provider_auth"), (403, "provider_auth"),
                             (429, "provider_rate_limit"), (500, "provider_unavailable"),
                             (529, "provider_unavailable"), (599, "provider_unavailable"), (499, "unknown")):
            with self.subTest(status=status):
                self.assertEqual(provider_failure_kind(None, f"Error code: {status}"), kind)
                self.assertEqual(provider_failure_kind(None, f"OpenAI Codex request failed ({status}): detail"), kind)

    def test_type_in_complete_error_envelope(self):
        for error_type, kind in (("invalid_request_error", "provider_bad_request"),
                                 ("authentication_error", "provider_auth"), ("permission_error", "provider_auth"),
                                 ("rate_limit_error", "provider_rate_limit"),
                                 ("overloaded_error", "provider_unavailable"), ("api_error", "provider_unavailable"),
                                 ("connection_error", "unknown"), ("arbitrary", "unknown")):
            with self.subTest(error_type=error_type):
                body = repr({"type": "error", "error": {"message": "diagnostic", "type": error_type}})
                self.assertEqual(provider_failure_kind(None, body), kind)
                self.assertEqual(provider_failure_kind(None, f"Error code: 418 - {body}"), kind)

    def test_only_anchored_native_formats_count(self):
        for text in ("upstream Error code: 401", "Error code: 4010", "Error code: 401 anything",
                     "HTTP 401 authentication_error", "authentication_error", "not a Connection error.",
                     "upstream OpenAI Codex request failed (401)", "OpenAI Codex request failed (401) prose",
                     "Connection error. unrelated prose", repr({"error": {"message": "'type': 'authentication_error'"}}),
                     "{'error': {'type': 'authentication_error'}} trailing text",
                     "{'error': {'type': 'authentication_error', 'message': " + repr("x" * 512) + "}}"):
            with self.subTest(text=text):
                self.assertEqual(provider_failure_kind(None, text), "unknown")
        self.assertEqual(provider_failure_kind(None, "Connection error."), "provider_network")
        self.assertEqual(provider_failure_kind(None, "Connection error. — All connection attempts failed"), "provider_network")

    def test_structured_error_is_authoritative(self):
        for structured, kind in (({"status": 403, "type": "rate_limit_error"}, "provider_auth"),
                                 ({"status": None, "type": "connection_error"}, "provider_network"),
                                 ({"status": 200, "type": "overloaded_error"}, "provider_unavailable"),
                                 ({"status": "401", "type": "invalid_request_error"}, "provider_bad_request"),
                                 ({"status": True, "type": {}}, "unknown"), ({}, "unknown")):
            with self.subTest(structured=structured):
                self.assertEqual(provider_failure_kind(structured, "Error code: 429"), kind)
