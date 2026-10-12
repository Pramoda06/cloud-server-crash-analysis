"""
Tests for the network delay, packet loss, and response latency models.
Validates behavior across boundary conditions including 0%, 5%, 20%, 50%, 90%, 99%, and 100% loss.
"""
import math
import unittest

from simulator import servers as server_catalog
from simulator.servers import build_spec
from simulator.lab.model import SystemModel


class TestNetworkModel(unittest.TestCase):
    def setUp(self):
        self.server = build_spec(server_catalog.DEFAULT_SERVER)
        self.model = SystemModel(self.server, seed=2026)

    def test_network_loss_spectrum(self):
        """Verify delivery, give-up probability, retransmission, and response time across the loss spectrum."""
        losses = [0.0, 5.0, 20.0, 50.0, 90.0, 99.0, 100.0]
        prev_response = 0.0

        for loss in losses:
            self.model.set_control("net_loss", loss)
            self.model.eff["net_loss"] = loss
            V, P, X = self.model.step()

            # Invariant: Packet loss matches requested clamped range
            self.assertAlmostEqual(V["packet_loss"], loss, delta=1.0)

            # Invariant: Availability and error_rate sum to 100%
            self.assertAlmostEqual(V["availability"] + V["error_rate"], 100.0, delta=1e-5)

            # Invariant: All probabilities in extra are in [0, 1]
            self.assertGreaterEqual(X["giveup"], 0.0)
            self.assertLessEqual(X["giveup"], 1.0)
            self.assertGreaterEqual(X["p_timeout"], 0.0)
            self.assertLessEqual(X["p_timeout"], 1.0)

            # Invariant: Response time is finite and bounded by timeout_ms
            self.assertTrue(math.isfinite(V["response_ms"]))
            self.assertLessEqual(V["response_ms"], self.model.controls["timeout_ms"])

            # Invariant: Monotonic response latency growth
            self.assertGreaterEqual(V["response_ms"], prev_response)
            prev_response = V["response_ms"]

    def test_packet_loss_100_percent_boundary(self):
        """At 100% packet loss, conditional retrans delay is unavailable and client latency is timeout_ms."""
        self.model.set_control("net_loss", 100.0)
        self.model.eff["net_loss"] = 100.0
        V, P, X = self.model.step()

        self.assertEqual(V["packet_loss"], 100.0)
        self.assertEqual(X["giveup"], 1.0)
        self.assertEqual(V["throughput"], 0.0)
        self.assertEqual(V["availability"], 0.0)
        self.assertEqual(V["error_rate"], 100.0)

        # Retransmission delay for delivered packets is undefined (None)
        self.assertIsNone(V["retrans_ms"])

        # Client-observed response latency must reflect client timeout, NOT zero
        self.assertEqual(V["response_ms"], self.model.controls["timeout_ms"])

    def test_zero_packet_loss_baseline(self):
        """At 0% added loss, retrans_ms is near zero and response_ms equals normal service + RTT."""
        self.model.set_control("net_loss", 0.0)
        self.model.set_control("net_delay", 0.0)
        self.model.eff["net_loss"] = 0.0
        self.model.eff["net_delay"] = 0.0
        V, P, X = self.model.step()

        self.assertAlmostEqual(V["retrans_ms"], 0.0, delta=1.0)
        self.assertGreaterEqual(V["availability"], 99.0)
        self.assertLessEqual(V["error_rate"], 1.0)
        self.assertAlmostEqual(V["response_ms"], V["service_ms"] + V["rtt"], delta=2.0)


if __name__ == "__main__":
    unittest.main()
