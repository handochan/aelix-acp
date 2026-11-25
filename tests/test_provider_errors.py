import unittest

from aelix_acp.runtime.provider_errors import provider_failure_kind


class ProviderCauseTests(unittest.TestCase):
    def test_structured_status(self):
        for status, kind in ((400, "provider_bad_request"), (404, "provider_bad_request"),
                             (422, "provider_bad_request"), (401, "provider_auth"), (403, "provider_auth"),
                             (429, "provider_rate_limit"), (500, "provider_unavailable"),
                             (529, "provider_unavailable"), (599, "provider_unavailable"), (499, "unknown")):
            with self.subTest(status=status):
                self.assertEqual(provider_failure_kind({"status": status}), kind)

    def test_structured_type(self):
        for error_type, kind in (("invalid_request_error", "provider_bad_request"),
                                 ("authentication_error", "provider_auth"), ("permission_error", "provider_auth"),
                                 ("rate_limit_error", "provider_rate_limit"),
                                 ("overloaded_error", "provider_unavailable"), ("api_error", "provider_unavailable"),
                                 ("connection_error", "provider_network"), ("arbitrary", "unknown")):
            with self.subTest(error_type=error_type):
                self.assertEqual(provider_failure_kind({"type": error_type}), kind)
                self.assertEqual(provider_failure_kind({"status": 418, "type": error_type}), kind)

    def test_absent_or_malformed_structured_error_is_unknown(self):
        for value in (None, [], {"message": "Error code: 401"}, "Error code: 429", "Connection error."):
            with self.subTest(value=value):
                self.assertEqual(provider_failure_kind(value), "unknown")

    def test_structured_error_is_authoritative(self):
        for structured, kind in (({"status": 403, "type": "rate_limit_error"}, "provider_auth"),
                                 ({"status": None, "type": "connection_error"}, "provider_network"),
                                 ({"status": 200, "type": "overloaded_error"}, "provider_unavailable"),
                                 ({"status": "401", "type": "invalid_request_error"}, "provider_bad_request"),
                                 ({"status": True, "type": {}}, "unknown"), ({}, "unknown")):
            with self.subTest(structured=structured):
                self.assertEqual(provider_failure_kind(structured), kind)
