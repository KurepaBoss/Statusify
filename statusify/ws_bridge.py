"""WebSocket bridge abstractions for Statusify."""


class WsBridge:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.active_ws = None

    def set_connection(self, ws):
        self.active_ws = ws

    def clear_connection(self):
        self.active_ws = None
