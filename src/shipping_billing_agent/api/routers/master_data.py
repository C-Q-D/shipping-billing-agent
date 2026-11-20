"""Manage the workbench's accounting entities, dictionary, and shared bills."""

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from ...domain import AccountingEntityProfile, ChargeDictionaryItem, ExistingBillRecord
from ...domain.currency import is_valid_iso4217
from ...services import ResourceNotFoundError, ServiceValidationError
from .http import BatchDependency, DecimalJsonResponse, read_decimal_json_body, validate_body

router = APIRouter(prefix="/api/v1", tags=["MasterData"])


class _EntityFields(AccountingEntityProfile):
    model_config = ConfigDict(str_strip_whitespace=True)
    is_default: StrictBool = False


class _DictionaryFields(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    raw_alias: str = Field(min_length=1)
    standard_name: str = Field(min_length=1)
    category: str = "general"


class _BillImport(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    default_accounting_entity: str | None = Field(default=None, min_length=1, max_length=120)
    replace_existing: StrictBool = False
    bills: list[dict[str, Any]]


_STANDARD_SECTIONS = (
    "document_info",
    "transaction_party",
    "charge_details",
    "financial_attributes",
)


def _existing_bill(raw: dict[str, Any], default_entity: str | None) -> ExistingBillRecord:
    values = raw.copy()
    for section in _STANDARD_SECTIONS:
        if section in raw:
            fields = raw[section]
            if not isinstance(fields, dict):
                raise ServiceValidationError("INVALID_EXISTING_BILLS", f"{section} 须为对象")
            for field, value in fields.items():
                values.setdefault(field, value)
    values.setdefault("id", uuid4().hex)
    values.setdefault("accounting_entity", default_entity)
    bill = validate_body(ExistingBillRecord, values, code="INVALID_EXISTING_BILLS")
    if not (
        bill.id.strip()
        and bill.charge_item.strip()
        and 1 <= len(bill.accounting_entity.strip()) <= 120
        and is_valid_iso4217(bill.currency)
    ):
        raise ServiceValidationError(
            "INVALID_EXISTING_BILLS", "历史账单的 ID、主体、费用或币种无效"
        )
    return bill


@router.get("/entities", operation_id="listAccountingEntities")
def list_entities(service: BatchDependency) -> DecimalJsonResponse:
    return DecimalJsonResponse(service.repository.list_accounting_entities())


@router.post("/entities", operation_id="createAccountingEntity", status_code=201)
async def create_entity(request: Request, service: BatchDependency) -> DecimalJsonResponse:
    profile = validate_body(_EntityFields, await read_decimal_json_body(request))
    entity = await run_in_threadpool(service.repository.create_accounting_entity, profile)
    return DecimalJsonResponse(entity, status_code=201)


@router.put("/entities/{entity_id}", operation_id="updateAccountingEntity")
async def update_entity(
    entity_id: str, request: Request, service: BatchDependency
) -> DecimalJsonResponse:
    profile = validate_body(_EntityFields, await read_decimal_json_body(request))
    entity = await run_in_threadpool(
        service.repository.update_accounting_entity, entity_id, profile
    )
    return DecimalJsonResponse(entity)


@router.patch("/entities/{entity_id}", operation_id="patchAccountingEntity")
async def patch_entity(
    entity_id: str, request: Request, service: BatchDependency
) -> DecimalJsonResponse:
    patch = await read_decimal_json_body(request)

    def update() -> dict[str, Any]:
        with service.repository.transaction() as transaction:
            for entity in transaction.list_accounting_entities():
                if entity["id"] == entity_id:
                    profile = validate_body(_EntityFields, entity | patch)
                    return transaction.update_accounting_entity(entity_id, profile)
            raise ResourceNotFoundError("未找到该记账主体")

    return DecimalJsonResponse(await run_in_threadpool(update))


@router.get("/dictionary", operation_id="listChargeDictionary")
def list_dictionary(service: BatchDependency) -> DecimalJsonResponse:
    return DecimalJsonResponse(service.repository.list_charge_dictionary())


@router.post("/dictionary", operation_id="upsertChargeDictionaryEntry", status_code=201)
async def upsert_dictionary(request: Request, service: BatchDependency) -> DecimalJsonResponse:
    fields = validate_body(_DictionaryFields, await read_decimal_json_body(request))
    item = ChargeDictionaryItem(**fields.model_dump())
    entry = await run_in_threadpool(service.repository.upsert_charge_dictionary_entry, item)
    return DecimalJsonResponse(entry, status_code=201)


@router.patch("/dictionary/{entry_id}", operation_id="patchChargeDictionaryEntry")
async def patch_dictionary(
    entry_id: str, request: Request, service: BatchDependency
) -> DecimalJsonResponse:
    patch = await read_decimal_json_body(request)

    def update() -> dict[str, Any]:
        with service.repository.transaction() as transaction:
            for entry in transaction.list_charge_dictionary():
                if entry["id"] == entry_id:
                    fields = validate_body(_DictionaryFields, entry | patch)
                    item = ChargeDictionaryItem(**fields.model_dump())
                    return transaction.upsert_charge_dictionary_entry(item, entry_id=entry_id)
            raise ResourceNotFoundError("未找到该词典条目")

    return DecimalJsonResponse(await run_in_threadpool(update))


@router.delete(
    "/dictionary/{entry_id}", operation_id="deleteChargeDictionaryEntry", status_code=204
)
def delete_dictionary(entry_id: str, service: BatchDependency) -> Response:
    with service.repository.transaction() as transaction:
        for entry in transaction.list_charge_dictionary():
            if entry["id"] == entry_id and not entry["is_builtin"]:
                transaction.delete_charge_dictionary_entry(entry_id)
                return Response(status_code=204)
        raise ResourceNotFoundError("未找到可删除的自定义词典条目")


@router.get("/existing-bills", operation_id="listExistingBills")
def list_bills(
    service: BatchDependency, accounting_entity: str | None = None
) -> DecimalJsonResponse:
    bills = service.repository.list_existing_bills(accounting_entity)
    return DecimalJsonResponse([bill.model_dump() for bill in bills])


@router.post("/existing-bills/import", operation_id="importExistingBills")
async def import_bills(request: Request, service: BatchDependency) -> DecimalJsonResponse:
    fields = validate_body(_BillImport, await read_decimal_json_body(request))

    def save() -> dict[str, int]:
        bills = [_existing_bill(raw, fields.default_accounting_entity) for raw in fields.bills]
        try:
            return service.repository.import_existing_bills(
                bills,
                replace_existing=fields.replace_existing,
                accounting_entity=fields.default_accounting_entity
                if fields.replace_existing
                else None,
            )
        except ValueError as exc:
            raise ServiceValidationError(
                "INVALID_EXISTING_BILLS", "历史账单存在重复 ID 或超出所选主体范围"
            ) from exc

    return DecimalJsonResponse(await run_in_threadpool(save))


@router.delete("/existing-bills", operation_id="clearExistingBills", status_code=204)
def clear_bills(service: BatchDependency, accounting_entity: str | None = None) -> Response:
    service.repository.clear_existing_bills(accounting_entity)
    return Response(status_code=204)
