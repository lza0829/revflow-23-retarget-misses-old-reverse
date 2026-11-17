from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.reverse_retarget import (
    get_repoint_proof,
    list_retargets,
    register_retarget,
)

router = APIRouter(tags=["键迁移"])


class RetargetIn(BaseModel):
    old_key: str
    new_key: str


@router.get("/key-retargets")
def get_retargets():
    return list_retargets()


@router.get("/key-retargets/{retarget_id}/repoint-proof")
def get_proof(retarget_id: int):
    # 只读：按迁移 id 查重指向证明（旧键、新键、重指向数、未触及键、迁移边）
    return get_repoint_proof(retarget_id)


@router.post("/key-retargets")
def post_retarget(body: RetargetIn):
    # 失败闭：HTTP 400，detail 即闭失败登记（冲正指向与敞口已全部回到迁移前）
    return register_retarget(body.old_key, body.new_key)
