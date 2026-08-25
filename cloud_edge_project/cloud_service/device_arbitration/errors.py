class ArbitrationPayloadConflictError(RuntimeError):
    """The same conflict identity was reused for a different request payload."""
