"""Exception groups for talking to the EZ1 local API.

Derived from the behaviour of the library ``apsystems-ez1==2.7.0`` (``APsystemsEZ1M._request``):

API_ERRORS
    Problems while talking to the inverter:
    - ``InverterReturnedError``  device answered with message != SUCCESS (after retries)
    - ``ClientError``            connection refused / dropped, non-JSON answer (ContentTypeError)
    - ``HttpProcessingError``    HTTP status != 200. The library raises aiohttp's
                                 ``HttpBadRequest`` here, which is NOT a ``ClientError``
                                 - it has to be listed separately.
    - ``TimeoutError``           no answer within the timeout (``asyncio.TimeoutError``
                                 is the same class as ``TimeoutError`` since Python 3.11)
    - ``ConnectionError``        raw socket-level errors (kept for safety)

RESPONSE_ERRORS
    The device answered, but the content is missing / malformed
    (``KeyError``, ``TypeError``, ``ValueError`` incl. ``json.JSONDecodeError``).
    This happens with some firmware versions.

Anything else (e.g. ``AttributeError``, ``ImportError``) is deliberately NOT caught,
so that a changed library API or a bug becomes visible in the log with a traceback.
"""

from __future__ import annotations

from aiohttp import ClientError
from aiohttp.http_exceptions import HttpProcessingError
from APsystemsEZ1 import InverterReturnedError

API_ERRORS: tuple[type[BaseException], ...] = (
    InverterReturnedError,
    ClientError,
    HttpProcessingError,
    TimeoutError,
    ConnectionError,
)

RESPONSE_ERRORS: tuple[type[BaseException], ...] = (
    KeyError,
    TypeError,
    ValueError,
)

DEVICE_ERRORS: tuple[type[BaseException], ...] = API_ERRORS
