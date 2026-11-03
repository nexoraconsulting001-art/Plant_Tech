"""JSON answers for the busiest routes without FastAPI's jsonable_encoder walk, byte for byte the same.

A route that returns a plain dict or list gets it walked by jsonable_encoder (one Python call per value, about 8% of
the API's busy CPU in the load test of 3 Nov) and then written by starlette's JSONResponse (json.dumps, in C).
The busiest routes' answers are MongoDB documents that are already JSON values (strings, numbers, booleans, None,
lists, dicts; dates are ISO strings), so `respond(content)` skips the walk: it writes the content with exactly
JSONResponse's json.dumps options, and the rare value json cannot write itself (a datetime, a model) goes through
jsonable_encoder, as before. The bytes are the same as FastAPI's own answer; tests/test_fast_json.py compares
them document by document and route by route.

FastAPI's documented fast path (a response_model or return type, serialised by Pydantic in Rust) is not used: these
routes have no models, and Pydantic writes some values differently from json.dumps (1e-05 as 0.00001, a UTC
datetime ending in "Z" instead of "+00:00", NaN as null instead of an error), so answers to apps already in the
field could change.

Used by GET /api/products (list, pages, search, ids), GET /api/customers/find, GET and PUT /api/orders/cart,
GET /api/invoices/{id} and GET /api/dashboard/today. Not by POST /api/invoices: one call per bill whose answer
costs about 0.1 ms to encode next to a transaction of about 10 ms, and its route function is called directly by
the concurrency tests, which read its dict.

Returning a Response means FastAPI no longer adds the headers and status a route set on its `response: Response`
parameter: pass that parameter as `response` and they are copied.
"""
import json

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.responses import Response

ENABLED = True  # False: FastAPI's own path (jsonable_encoder first), for the comparison tests


class FastJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        # the options of starlette's JSONResponse.render, plus jsonable_encoder for what json cannot write itself
        return json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"),
                          default=jsonable_encoder).encode("utf-8")


def respond(content, response: Response | None = None) -> Response:
    """`content` as FastAPI would answer it, without the jsonable_encoder walk. `response`: the route's injected
    Response, whose status code (when set) and headers are kept."""
    out = FastJSONResponse(content) if ENABLED else JSONResponse(jsonable_encoder(content))
    if response is not None:
        if response.status_code:
            out.status_code = response.status_code
        out.headers.raw.extend(response.headers.raw)
    return out
