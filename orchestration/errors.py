"""Application-level failures shared by orchestration and its adapters."""


class EnvironmentFailure(RuntimeError):
    def __init__(self, message: str, *, quarantine: bool = True):
        super().__init__(message)
        self.quarantine = quarantine


class ExecutionAborted(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SandboxCreationRejected(EnvironmentFailure):
    """The provider explicitly rejected allocation, rather than losing its response."""
