"""Tests for the Proxy Auto Config evaluator."""

import unittest

from proxy_auto_config import PACFile, ProxyConfig
from proxy_auto_config.core import EvaluationError


class TestSimpleProxy(unittest.TestCase):
    def test_direct(self):
        pac = PACFile(
            'function FindProxyForURL(url, host) { return "DIRECT"; }'
        )
        result = pac.find_proxy_for_url("http://example.com/")
        self.assertIsInstance(result, ProxyConfig)
        self.assertEqual(result.proxies, ("DIRECT",))
        self.assertEqual(result.raw, "DIRECT")

    def test_single_proxy(self):
        pac = PACFile(
            'function FindProxyForURL(url, host) { return "PROXY proxy1:8080"; }'
        )
        result = pac.find_proxy_for_url("http://example.com/")
        self.assertEqual(result.proxies, ("PROXY proxy1:8080",))

    def test_multiple_proxies(self):
        pac = PACFile(
            'function FindProxyForURL(url, host) '
            '{ return "PROXY proxy1:8080; PROXY proxy2:8080"; }'
        )
        result = pac.find_proxy_for_url("http://example.com/")
        self.assertEqual(
            result.proxies,
            ("PROXY proxy1:8080", "PROXY proxy2:8080"),
        )

    def test_direct_with_fallback_proxy(self):
        pac = PACFile(
            'function FindProxyForURL(url, host) '
            '{ return "PROXY proxy1:8080; DIRECT"; }'
        )
        result = pac.find_proxy_for_url("http://example.com/")
        self.assertEqual(result.proxies, ("PROXY proxy1:8080", "DIRECT"))


class TestConditions(unittest.TestCase):
    def test_if_else(self):
        pac = PACFile(
            """
            function FindProxyForURL(url, host) {
                if (host == "internal.example.com") {
                    return "DIRECT";
                } else {
                    return "PROXY proxy1:8080";
                }
            }
            """
        )
        internal = pac.find_proxy_for_url("http://internal.example.com/")
        self.assertEqual(internal.proxies, ("DIRECT",))
        external = pac.find_proxy_for_url("http://external.example.com/")
        self.assertEqual(external.proxies, ("PROXY proxy1:8080",))

    def test_logical_operators(self):
        pac = PACFile(
            """
            function FindProxyForURL(url, host) {
                if (host == "a.example.com" || host == "b.example.com") {
                    return "DIRECT";
                }
                if (host == "c.example.com") {
                    return "PROXY proxy2:8080";
                }
                return "PROXY proxy1:8080";
            }
            """
        )
        # Logical OR
        result_a = pac.find_proxy_for_url("http://a.example.com/")
        self.assertEqual(result_a.proxies, ("DIRECT",))
        result_b = pac.find_proxy_for_url("http://b.example.com/")
        self.assertEqual(result_b.proxies, ("DIRECT",))

        # Simple host match without unsupported string method
        result_c = pac.find_proxy_for_url("https://c.example.com/")
        self.assertEqual(result_c.proxies, ("PROXY proxy2:8080",))


class TestPACHelpers(unittest.TestCase):
    def test_is_plain_host_name(self):
        pac = PACFile(
            """
            function FindProxyForURL(url, host) {
                if (isPlainHostName(host)) {
                    return "DIRECT";
                }
                return "PROXY proxy1:8080";
            }
            """
        )
        # URL with no dot in host
        result = pac.find_proxy_for_url("http://intranet/")
        self.assertEqual(result.proxies, ("DIRECT",))
        result = pac.find_proxy_for_url("http://example.com/")
        self.assertEqual(result.proxies, ("PROXY proxy1:8080",))

    def test_dns_domain_is(self):
        pac = PACFile(
            """
            function FindProxyForURL(url, host) {
                if (dnsDomainIs(host, ".example.com")) {
                    return "DIRECT";
                }
                return "PROXY proxy1:8080";
            }
            """
        )
        result = pac.find_proxy_for_url("http://sub.example.com/")
        self.assertEqual(result.proxies, ("DIRECT",))
        result = pac.find_proxy_for_url("http://other.org/")
        self.assertEqual(result.proxies, ("PROXY proxy1:8080",))

    def test_sh_exp_match(self):
        pac = PACFile(
            """
            function FindProxyForURL(url, host) {
                if (shExpMatch(host, "*.example.com")) {
                    return "DIRECT";
                }
                return "PROXY proxy1:8080";
            }
            """
        )
        result = pac.find_proxy_for_url("http://sub.example.com/")
        self.assertEqual(result.proxies, ("DIRECT",))
        result = pac.find_proxy_for_url("http://example.org/")
        self.assertEqual(result.proxies, ("PROXY proxy1:8080",))


class TestEdgeCases(unittest.TestCase):
    def test_missing_function(self):
        pac = PACFile("var x = 1;")
        with self.assertRaises(EvaluationError):
            pac.find_proxy_for_url("http://example.com/")

    def test_empty_proxy_string(self):
        pac = PACFile(
            'function FindProxyForURL(url, host) { return ""; }'
        )
        result = pac.find_proxy_for_url("http://example.com/")
        self.assertEqual(result.proxies, ())
        self.assertEqual(result.raw, "")

    def test_no_host_in_url(self):
        pac = PACFile(
            'function FindProxyForURL(url, host) { return host; }'
        )
        result = pac.find_proxy_for_url("not-a-url")
        self.assertEqual(result.raw, "")

    def test_nested_function_call(self):
        pac = PACFile(
            """
            function isInternal(host) {
                return dnsDomainIs(host, ".example.com");
            }
            function FindProxyForURL(url, host) {
                if (isInternal(host)) {
                    return "DIRECT";
                }
                return "PROXY proxy1:8080";
            }
            """
        )
        result = pac.find_proxy_for_url("http://sub.example.com/")
        self.assertEqual(result.proxies, ("DIRECT",))
        result = pac.find_proxy_for_url("http://other.org/")
        self.assertEqual(result.proxies, ("PROXY proxy1:8080",))


if __name__ == "__main__":
    unittest.main()
