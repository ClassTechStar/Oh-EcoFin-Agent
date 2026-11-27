"""本地券商契约网关（联调专用，绝不下真实资金）。

实现 `backend/execution/brokers/http_api.py` 约定的三个端点：
    GET    /health
    POST   /orders
    DELETE /orders/{broker_order_id}

用途：在没有真实券商 OpenAPI 时，验证适配器、探测、下单幂等与 UI 链路。
运行：
    python scripts/mock_broker_gateway.py --port 8898
    python scripts/mock_broker_gateway.py --port 8898 --token dev-token --mode filled
然后：
    set FINAGENT_EXECUTION__LIVE_ENABLED=1
    set FINAGENT_EXECUTION__BROKER_KIND=http
    set FINAGENT_EXECUTION__HTTP_BASE_URL=http://127.0.0.1:8898
    set FINAGENT_EXECUTION__HTTP_TOKEN=dev-token
"""

from __future__ import annotations

import argparse
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


class MockOrderRequest(BaseModel):
    client_order_id: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=6)
    side: str
    qty: int = Field(..., ge=1)
    order_type: str = "market"
    price: float | None = Field(None, gt=0)
    limit_price: float | None = Field(None, gt=0)
    strategy_id: str = ""


def create_mock_app(*, token: str = "", mode: str = "accepted") -> FastAPI:
    """mode=accepted 返回已受理；mode=filled 直接返回成交（便于看板演示）。"""
    if mode not in ("accepted", "filled"):
        raise ValueError("mode 只能是 accepted 或 filled")
    app = FastAPI(title="Mock Broker Gateway", version="1.0.0")
    orders: dict[str, dict] = {}

    def _auth(authorization: str | None) -> None:
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="无效令牌")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "mode": mode, "orders": len(orders)}

    @app.post("/orders")
    def create_order(req: MockOrderRequest,
                     authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        if req.client_order_id in orders:      # 幂等：重发返回首次结果
            return orders[req.client_order_id]
        broker_id = f"MB-{uuid.uuid4().hex[:10]}"
        status = "filled" if mode == "filled" else "accepted"
        record = {
            "broker_order_id": broker_id,
            "status": status,
            "fill": ({"price": req.limit_price or req.price or 0.0,
                      "qty": req.qty, "fees": 0.0} if status == "filled" else None),
            "reason": "",
        }
        orders[req.client_order_id] = record
        return record

    @app.delete("/orders/{broker_order_id}")
    def cancel_order(broker_order_id: str,
                     authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        hit = next((o for o in orders.values()
                    if o["broker_order_id"] == broker_order_id), None)
        if hit is None:
            raise HTTPException(status_code=404, detail="委托不存在")
        if hit["status"] != "accepted":
            return {"ok": False, "reason": "已成交/已撤销，不可撤单"}
        hit["status"] = "cancelled"
        return {"ok": True}

    @app.get("/orders")
    def list_orders(authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        return {"orders": list(orders.values())}

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="本地券商契约网关（联调）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument("--token", default="", help="可选 Bearer token")
    parser.add_argument("--mode", choices=("accepted", "filled"), default="accepted")
    args = parser.parse_args()
    app = create_mock_app(token=args.token, mode=args.mode)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
