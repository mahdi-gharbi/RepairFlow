class DomainError(Exception):
    status = 400

    def __init__(self, code: str, detail: str = "", **extra):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.extra = code, detail, extra


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class Invalid(DomainError):
    status = 422


class Forbidden(DomainError):
    status = 403
