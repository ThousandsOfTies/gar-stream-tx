from __future__ import annotations

import json
import socket
import threading
import unittest

from source_advertiser import PROTOCOL, SourceAdvertiser


class SourceAdvertiserTest(unittest.TestCase):
    def test_rejects_non_ascii_protocol_identity(self) -> None:
        with self.assertRaises(ValueError):
            SourceAdvertiser("tx-\N{SNOWMAN}", "Kitchen TX", control_port=0)

    def test_query_and_receiver_lease(self) -> None:
        changed = threading.Event()
        snapshots = []

        def on_clients_changed(clients):
            snapshots.append(clients)
            changed.set()

        advertiser = SourceAdvertiser(
            "tx-one",
            "Kitchen TX",
            control_port=0,
            announce_targets=(),
            announce_interval=60,
            on_clients_changed=on_clients_changed,
        )
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        client.settimeout(2)
        advertiser.start()
        try:
            client.sendto(
                json.dumps({"protocol": PROTOCOL, "type": "source_query"}).encode(),
                ("127.0.0.1", advertiser.control_port),
            )
            payload, _address = client.recvfrom(4096)
            announcement = json.loads(payload)
            self.assertEqual("source_announce", announcement["type"])
            self.assertEqual("tx-one", announcement["source_id"])
            self.assertEqual("Kitchen TX", announcement["source_name"])
            self.assertGreaterEqual(advertiser.diagnostics()["announce_count"], 1)

            request = {
                "protocol": PROTOCOL,
                "type": "stream_request",
                "source_id": "tx-one",
                "receiver_id": "rx-one",
                "stream_port": 6200,
                "lease_seconds": 5,
            }
            client.sendto(
                json.dumps(request).encode(), ("127.0.0.1", advertiser.control_port)
            )
            self.assertTrue(changed.wait(2))
            self.assertEqual(1, len(snapshots[-1]))
            self.assertEqual("rx-one", snapshots[-1][0].receiver_id)
            self.assertEqual("127.0.0.1", snapshots[-1][0].host)
            self.assertEqual(6200, snapshots[-1][0].port)
            diagnostics = advertiser.diagnostics()
            self.assertEqual(1, diagnostics["lease_count"])
            self.assertEqual("rx-one", diagnostics["leases"][0]["receiver_id"])

            # A stop packet only owns the lease created from the same host.
            # The protocol never accepts a destination IP from JSON, and it
            # likewise must not let a third host revoke another RX's lease.
            attacker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                attacker.bind(("127.0.0.2", 0))
                changed.clear()
                stop = dict(request, type="stream_stop")
                attacker.sendto(
                    json.dumps(stop).encode(),
                    ("127.0.0.1", advertiser.control_port),
                )
                self.assertFalse(changed.wait(0.3))
                self.assertEqual(1, len(advertiser.clients))
            finally:
                attacker.close()

            changed.clear()
            request["type"] = "stream_stop"
            client.sendto(
                json.dumps(request).encode(), ("127.0.0.1", advertiser.control_port)
            )
            self.assertTrue(changed.wait(2))
            self.assertEqual((), snapshots[-1])
        finally:
            advertiser.stop()
            client.close()


if __name__ == "__main__":
    unittest.main()
