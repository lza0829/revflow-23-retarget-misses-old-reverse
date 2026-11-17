from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.reverse_retarget import list_retargets, register_retarget

router = APIRouter(tags=["键迁移"])


class RetargetIn(BaseModel):
    old_key: str
    new_key: str


@router.get("/key-retargets")
def get_retargets():
    return list_retargets()


@router.post("/key-retargets")
def post_retarget(body: RetargetIn):
    # 失败闭：HTTP 400，detail 即闭失败登记（冲正指向与敞口已全部回到迁移前）
    return register_retarget(body.old_key, body.new_key)
