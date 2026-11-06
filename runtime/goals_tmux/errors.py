"""Stable, serializable runtime failure categories."""


class DomainError(Exception):
    def __init__(self, code, message, *, status="refused", side_effects="none", data=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.side_effects = side_effects
        self.data = data

    def as_result(self, request_id=None):
        return {
            "schema_version": 1,
            "request_id": request_id,
            "status": self.status,
            "code": self.code,
            "message": str(self),
            "data": self.data,
            "side_effects": self.side_effects,
            "warnings": [],
        }
