import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from motor.transport import BridgeTransport, TransportError, linux_transport


class TransportTests(unittest.TestCase):
    def test_linux_uses_stable_native_identity_for_cross_device_maps(self):
        identity = "b683d45a-e298-40b3-90c2-3bcd8f59cb84"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.chmod(0o700)
            file = root / "capability"
            file.write_text("c" * 32)
            file.chmod(0o600)
            with patch.object(BridgeTransport, "request", return_value={"device_id": identity}) as request:
                with linux_transport(root) as client:
                    self.assertEqual(client.serial, identity)
                request.assert_called_once_with("/api/motor/status")
            with patch.object(BridgeTransport, "request", return_value={}):
                with self.assertRaisesRegex(TransportError, "stable_device_identity_unavailable"):
                    linux_transport(root)

    def test_authenticated_transport_rejects_external_or_unknown_routes(self):
        with BridgeTransport(18883, "x" * 32) as client:
            for path in ("https://example.org/api/device/status", "//example.org/api/device/status", "/api/provider"):
                with self.assertRaisesRegex(TransportError, "unsupported_route"):
                    client.request(path)


if __name__ == "__main__":
    unittest.main()
