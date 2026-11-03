"""Errors the app can show in the user's language.

`detail` stays the plain English sentence, so older app versions keep showing it as before.
`key` is the same sentence with {{placeholders}} (the app's translation key), `params` fills them,
and `code` lets the app react to a specific error (e.g. offer "Open supplier bill").
Plain HTTPException messages with no placeholders need none of this: the app translates `detail`.
"""
import re

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def render(template: str, params: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(params.get(m.group(1), "")), template)


class AppError(HTTPException):
    def __init__(self, status: int, template: str, code: str = "", **params):
        super().__init__(status, render(template, params))
        self.code, self.key, self.params = code, template, params


def error_response(exc: AppError) -> JSONResponse:
    """The error as the app gets it, with its own headers (e.g. Retry-After); also used outside a route."""
    return JSONResponse({"detail": exc.detail, "code": exc.code, "key": exc.key, "params": exc.params},
                        status_code=exc.status_code, headers=exc.headers)


async def app_error_handler(_request, exc: AppError) -> JSONResponse:
    return error_response(exc)
