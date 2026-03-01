from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Synthetic ERP", description="Hospital supply and equipment management")


# --- Models ---

class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class SupplyOrderRequest(BaseModel):
    item_id: str
    quantity: int
    department: str
    priority: Priority = Priority.normal


class SupplyOrderResponse(BaseModel):
    order_id: str
    item_id: str
    item_name: str
    quantity: int
    department: str
    priority: str
    status: str
    created_at: str


# --- Seed Data ---

supplies = {
    "SUP-001": {"item_id": "SUP-001", "name": "Surgical Gloves", "stock": 500, "unit": "boxes"},
    "SUP-002": {"item_id": "SUP-002", "name": "Syringes 10ml", "stock": 200, "unit": "boxes"},
    "SUP-003": {"item_id": "SUP-003", "name": "IV Sets", "stock": 150, "unit": "units"},
    "SUP-004": {"item_id": "SUP-004", "name": "Gauze Pads", "stock": 300, "unit": "packs"},
    "SUP-005": {"item_id": "SUP-005", "name": "Surgical Masks", "stock": 400, "unit": "boxes"},
    "SUP-006": {"item_id": "SUP-006", "name": "Saline 0.9% 500ml", "stock": 100, "unit": "bags"},
}

equipment = {
    "EQP-001": {"equipment_id": "EQP-001", "name": "Ventilator", "total": 3, "available": 2, "in_use": 1, "maintenance": 0},
    "EQP-002": {"equipment_id": "EQP-002", "name": "Infusion Pump", "total": 5, "available": 3, "in_use": 2, "maintenance": 0},
    "EQP-003": {"equipment_id": "EQP-003", "name": "Portable X-Ray Machine", "total": 2, "available": 1, "in_use": 0, "maintenance": 1},
    "EQP-004": {"equipment_id": "EQP-004", "name": "Patient Monitor", "total": 4, "available": 3, "in_use": 1, "maintenance": 0},
}

orders: list[dict] = []
order_counter = 0


# --- Endpoints ---

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/inventory")
def list_inventory():
    return {
        "supplies": list(supplies.values()),
        "equipment": list(equipment.values()),
    }


@app.get("/equipment/{equipment_id}")
def get_equipment(equipment_id: str):
    item = equipment.get(equipment_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Equipment {equipment_id} not found")
    status = "available" if item["available"] > 0 else "unavailable"
    return {**item, "status": status}


@app.post("/supply-order", response_model=SupplyOrderResponse)
def place_supply_order(order: SupplyOrderRequest):
    global order_counter

    item = supplies.get(order.item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Supply item {order.item_id} not found")

    if order.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be positive")

    if order.quantity > item["stock"]:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient stock. Available: {item['stock']} {item['unit']}",
        )

    item["stock"] -= order.quantity
    order_counter += 1
    order_id = f"ORD-{order_counter:04d}"

    record = {
        "order_id": order_id,
        "item_id": order.item_id,
        "item_name": item["name"],
        "quantity": order.quantity,
        "department": order.department,
        "priority": order.priority.value,
        "status": "confirmed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    orders.append(record)
    return record
