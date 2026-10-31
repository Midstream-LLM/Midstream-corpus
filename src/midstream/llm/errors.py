"""可供 CLI 和 core 识别的调用错误。"""


class LLMError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int | None = None,
                 response: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.response = response

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "status_code": self.status_code,
                "response": self.response}
