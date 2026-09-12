import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from fastapi import Request, Response

# Configure basic logging format
# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# )
logger = logging.getLogger("mcp_orchestration.middleware")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log request details, measure processing time, and add headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start_time = time.time()
        
        # Log request receipt
        client_host = request.client.host if request.client else "unknown"
        logger.info(f"Incoming request: {request.method} {request.url.path} from {client_host}")
        
        try:
            response = await call_next(request)
        except Exception as e:
            process_time_ms = (time.time() - start_time) * 1000
            logger.error(
                f"Request failed: {request.method} {request.url.path} - "
                f"Duration: {process_time_ms:.2f}ms - Error: {str(e)}", 
                exc_info=True
            )
            raise e
            
        process_time_ms = (time.time() - start_time) * 1000
        
        # Add processing time to response header for debugging/auditing
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
        
        logger.info(
            f"Completed request: {request.method} {request.url.path} - "
            f"Status: {response.status_code} - "
            f"Duration: {process_time_ms:.2f}ms"
        )
        
        return response
