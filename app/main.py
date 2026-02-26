import os
import asyncio
import json
import time
import logging
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from pythonjsonlogger import jsonlogger

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@db:5432/inventory_db")
PESSIMISTIC_LOCK_TIMEOUT_MS = int(os.getenv("PESSIMISTIC_LOCK_TIMEOUT_MS", "2000"))
OPTIMISTIC_MAX_RETRIES = int(os.getenv("OPTIMISTIC_MAX_RETRIES", "3"))
OPTIMISTIC_BASE_BACKOFF_MS = int(os.getenv("OPTIMISTIC_BASE_BACKOFF_MS", "50"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

app = FastAPI()
db_pool: Optional[asyncpg.pool.Pool] = None

# Configure structured JSON logging
logger = logging.getLogger("inventory")
if not logger.handlers:
    logHandler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    logHandler.setFormatter(formatter)
    logger.addHandler(logHandler)
logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as exc:  # pragma: no cover - bubble up
        status_code = 500
        logger.exception("request_error", extra={
            "method": request.method,
            "path": request.url.path,
        })
        raise
    finally:
        duration_ms = int((time.time() - start) * 1000)
        logger.info("http_request", extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "client": request.client.host if request.client else None,
        })
    return response


class OrderRequest(BaseModel):
    productId: int
    quantity: int
    userId: str


@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)


@app.on_event("shutdown")
async def shutdown():
    global db_pool
    if db_pool:
        await db_pool.close()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/products/reset")
async def reset_products():
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Reset to known initial values
            await conn.execute("UPDATE products SET stock = CASE WHEN name = 'Super Widget' THEN 100 WHEN name = 'Mega Gadget' THEN 50 ELSE stock END, version = 1;")
    logger.info("products_reset", extra={})
    return {"message": "Product inventory reset successfully."}


@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, name, stock, version FROM products WHERE id = $1", product_id)
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")
        return dict(row)


@app.post("/api/orders/pessimistic")
async def create_order_pessimistic(req: OrderRequest):
    async with db_pool.acquire() as conn:
        try:
            async with conn.transaction():
                # set local lock timeout
                await conn.execute(f"SET LOCAL lock_timeout = '{PESSIMISTIC_LOCK_TIMEOUT_MS}ms'")
                # acquire row lock
                logger.info("pessimistic_lock_acquire_attempt", extra={"productId": req.productId, "userId": req.userId})
                row = await conn.fetchrow("SELECT id, stock FROM products WHERE id = $1 FOR UPDATE", req.productId)
                if not row:
                    raise HTTPException(status_code=404, detail="Product not found")
                if row["stock"] < req.quantity:
                    # create failed order
                    await conn.execute(
                        "INSERT INTO orders (product_id, quantity_ordered, user_id, status) VALUES ($1,$2,$3,$4)",
                        req.productId,
                        req.quantity,
                        req.userId,
                        "FAILED_OUT_OF_STOCK",
                    )
                    logger.info("pessimistic_insufficient_stock", extra={"productId": req.productId, "stock": row["stock"], "requested": req.quantity, "userId": req.userId})
                    raise HTTPException(status_code=400, detail="Insufficient stock")

                # update stock and insert order
                await conn.execute("UPDATE products SET stock = stock - $1 WHERE id = $2", req.quantity, req.productId)
                res = await conn.fetchrow(
                    "INSERT INTO orders (product_id, quantity_ordered, user_id, status) VALUES ($1,$2,$3,$4) RETURNING id, status, created_at",
                    req.productId,
                    req.quantity,
                    req.userId,
                    "SUCCESS",
                )
                logger.info("pessimistic_order_success", extra={"orderId": res["id"], "productId": req.productId, "quantity": req.quantity, "userId": req.userId})
                return {"orderId": res["id"], "status": res["status"]}
        except asyncpg.exceptions.QueryCanceledError:
            # lock timeout
            logger.warning("pessimistic_lock_timeout", extra={"productId": req.productId, "userId": req.userId})
            raise HTTPException(status_code=409, detail="Lock timeout / conflict")


@app.post("/api/orders/optimistic")
async def create_order_optimistic(req: OrderRequest):
    attempt = 0
    async with db_pool.acquire() as conn:
        while attempt < OPTIMISTIC_MAX_RETRIES:
            attempt += 1
            logger.info("optimistic_attempt", extra={"attempt": attempt, "productId": req.productId, "userId": req.userId})
            async with conn.transaction():
                row = await conn.fetchrow("SELECT id, stock, version FROM products WHERE id = $1", req.productId)
                if not row:
                    raise HTTPException(status_code=404, detail="Product not found")
                if row["stock"] < req.quantity:
                    await conn.execute(
                        "INSERT INTO orders (product_id, quantity_ordered, user_id, status) VALUES ($1,$2,$3,$4)",
                        req.productId,
                        req.quantity,
                        req.userId,
                        "FAILED_OUT_OF_STOCK",
                    )
                    logger.info("optimistic_insufficient_stock", extra={"productId": req.productId, "stock": row["stock"], "requested": req.quantity, "userId": req.userId})
                    raise HTTPException(status_code=400, detail="Insufficient stock")

                # attempt optimistic update
                result = await conn.execute(
                    "UPDATE products SET stock = stock - $1, version = version + 1 WHERE id = $2 AND version = $3 AND stock >= $1",
                    req.quantity,
                    req.productId,
                    row["version"],
                )
                # asyncpg returns command tag like 'UPDATE 1' or 'UPDATE 0'
                updated = int(result.split()[1]) if len(result.split()) > 1 else 0
                if updated == 1:
                    r = await conn.fetchrow(
                        "INSERT INTO orders (product_id, quantity_ordered, user_id, status) VALUES ($1,$2,$3,$4) RETURNING id, status, created_at",
                        req.productId,
                        req.quantity,
                        req.userId,
                        "SUCCESS",
                    )
                    logger.info("optimistic_order_success", extra={"orderId": r["id"], "productId": req.productId, "quantity": req.quantity, "userId": req.userId, "attempt": attempt})
                    return {"orderId": r["id"], "status": r["status"]}
                else:
                    # conflict; retry after backoff
                    backoff_ms = OPTIMISTIC_BASE_BACKOFF_MS * (2 ** (attempt - 1))
                    logger.info("optimistic_conflict_retry", extra={"productId": req.productId, "attempt": attempt, "backoff_ms": backoff_ms, "userId": req.userId})
                    await asyncio.sleep(backoff_ms / 1000.0)
                    continue

        # If we reach here, retries exhausted
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO orders (product_id, quantity_ordered, user_id, status) VALUES ($1,$2,$3,$4)",
                req.productId,
                req.quantity,
                req.userId,
                "FAILED_CONFLICT",
            )
        logger.warning("optimistic_conflict_exhausted", extra={"productId": req.productId, "attempts": attempt, "userId": req.userId})
        raise HTTPException(status_code=409, detail="Conflict after retries")


@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, product_id, quantity_ordered, user_id, status, created_at FROM orders WHERE id = $1", order_id)
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        return dict(row)


@app.get("/api/orders/stats")
async def orders_stats():
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM orders")
        success = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status = 'SUCCESS'")
        failed_oos = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status = 'FAILED_OUT_OF_STOCK'")
        failed_conflict = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status = 'FAILED_CONFLICT'")
        return {
            "totalOrders": int(total),
            "successfulOrders": int(success),
            "failedOutOfStock": int(failed_oos),
            "failedConflict": int(failed_conflict),
        }
