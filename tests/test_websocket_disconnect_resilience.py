import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from spotdl.utils.web import Client


class _FakeConnectionClosed(Exception):
    pass


class WebSocketDisconnectResilienceTest(unittest.TestCase):
    def test_send_update_clears_socket_when_connection_closed_mid_send(self):
        client = Client.__new__(Client)
        client.client_id = "test-client"
        client.websocket = AsyncMock()
        client.websocket.send_json = AsyncMock(side_effect=_FakeConnectionClosed())

        with patch("spotdl.utils.web.ConnectionClosed", _FakeConnectionClosed), patch(
            "spotdl.utils.web.app_state"
        ) as mock_app_state:
            mock_app_state.logger.debug = Mock()
            asyncio.run(client.send_update({"type": "state"}))

        self.assertIsNone(client.websocket)


if __name__ == "__main__":
    unittest.main()
