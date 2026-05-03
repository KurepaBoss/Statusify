"""Discord Rich Presence update abstractions."""


class RpcController:
    def __init__(self, rate_limit_calls: int, rate_limit_window: float):
        self.rate_limit_calls = rate_limit_calls
        self.rate_limit_window = rate_limit_window

    def update(self, _state):
        """Compatibility hook for future extraction from main.py."""
        return None
