"""
Per-request SQL query counter. Local development only -- enable with
QUERY_COUNT_DEBUG=true. Requires DEBUG=True, since connection.queries is
only populated then.
"""

from logging import getLogger

from django.db import connection

logger = getLogger(__name__)


class QueryCountDebugMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        total_time = 0.0
        for query in connection.queries:
            logger.debug(query.get("sql", "").replace('"', ""))
            query_time = query.get("time")
            if query_time is None:
                query_time = query.get("duration", 0) / 1000
            total_time += float(query_time)

        logger.info(
            "%s %s -> %d queries in %.3fs",
            request.method,
            request.path,
            len(connection.queries),
            total_time,
        )
        return response
