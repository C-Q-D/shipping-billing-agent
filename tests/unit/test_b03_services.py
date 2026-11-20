"""Exercise B03 through its services with SQLite, retained uploads, and offline OCR."""

from __future__ import annotations

import ast
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from PIL import Image

from shipping_billing_agent.config.limits import ResourceLimits
from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.domain.models import (
    AccountingEntityProfile,
    AlertCode,
    BatchStage,
    BatchStatus,
    ChargeDictionaryItem,
    ExistingBillRecord,
    OcrStructuredPayload,
    OperationType,
    ParsedDocument,
    PostingType,
    ProcessedBillingRecord,
    ReviewStatus,
    ValidationAlert,
    dumps_decimal_json,
    loads_decimal_json,
)
from shipping_billing_agent.domain.serializer import serialize_records
from shipping_billing_agent.services import BatchService, ReviewService, ServiceValidationError
from shipping_billing_agent.services.batch_service import _BatchScope
from shipping_billing_agent.services.review_service import _restore_decisions
from shipping_billing_agent.storage import BillingRepository, DatabaseManager, OptimisticLockError
from shipping_billing_agent.storage.repositories import ResourceNotFoundError

ENTITY = "NEPTUNE SHIPPING LIMITED"
CUSTOMER = "TARGETING A INC"
FALLBACK = "REMOTE_WITH_LOCAL_FALLBACK"


def _invoice(reference="INV-123", amount="100.00", total=None, charge="Handling Charge") -> str:
    printed_total = total if total is not None else amount
    return (
        f"Invoice No. {reference}\nDate: 2026-10-01\nIssuer: {ENTITY}\nBill To: {CUSTOMER}\n"
        f"{charge} {amount} USD\nTotal USD {printed_total}"
    )


def _invoice_with_fees(*amounts: int) -> str:
    fees = "\n".join(f"Handling Charge {amount} USD" for amount in amounts)
    return _invoice("INV-801", "0", total=str(sum(amounts))).replace("Handling Charge 0 USD", fees)


class FixtureOcr:
    def __init__(self, *, results=None) -> None:
        self.results = results or {}
        self.calls = []

    def recognize_image_bytes(self, content: bytes, filename: str) -> tuple[str, float]:
        self.calls.append((content, filename))
        text = self.results.get(content, _invoice())
        if isinstance(text, Exception):
            raise text
        return text, 0.99


@pytest.fixture
def make_service(tmp_path):
    services = []

    def make(*, ocr=None, limits=None, settings=None, **kwargs):
        if settings is None:
            directory = tmp_path / f"app-{len(services)}"
            settings = AppSettings(
                app_data_dir=directory,
                sqlite_db_path=directory / "test.sqlite",
            )
        service = BatchService(
            settings,
            local_ocr=ocr if ocr is not None else FixtureOcr(),
            limits=limits or ResourceLimits(),
            extractor_mode="rule_only",
            **kwargs,
        )
        services.append(service)
        return service

    yield make
    for service in services:
        service.close()


def _create(service, *files, **options):
    return service.create_batch(
        accounting_entity=ENTITY,
        files=files or [("invoice.png", b"invoice")],
        ocr_mode=FALLBACK,
        **options,
    )


def _bill(bill_id: str, *, entity: str = ENTITY, amount: str = "100.00") -> ExistingBillRecord:
    return ExistingBillRecord(
        id=bill_id,
        accounting_entity=entity,
        reference_no="INV-123",
        charge_item="Handling Charge",
        amount=amount,
        currency="USD",
        posting_type="AR",
    )


@pytest.mark.parametrize(
    ("printed_issuer", "retains_block"),
    [("  HARBOR－ORCHID Logistics  ", True), ("Harbor+Orchid Logistics", False)],
)
def test_same_source_entity_block_uses_printed_parties_under_overrides(
    printed_issuer: str, retains_block: bool
) -> None:
    target = "Harbor+Orchid Logistics"
    profile = AccountingEntityProfile(canonical_name=target)
    scope = _BatchScope(
        profiles=[profile], entity_profile=profile, existing_bills=[], charge_dictionary=[]
    )
    blocker = ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, "原批主体不匹配")
    old = ProcessedBillingRecord(
        id="fee",
        source_doc_id="original",
        accounting_entity=target,
        issuer=target,
        human_overrides={"issuer": target},
        source_values_before_overrides={"issuer": "Harbor-Orchid Logistics"},
        validation_alerts=[blocker, blocker.model_copy()],
    )
    recalculated = old.model_copy(
        update={
            "operation": OperationType.CREATE,
            "review_status": ReviewStatus.AUTO_READY,
            "posting_type": PostingType.AR,
            "source_values_before_overrides": {"issuer": printed_issuer},
            "validation_alerts": [],
        }
    )

    (restored,), _ = _restore_decisions([recalculated], [old], scope, same_source_ids={old.id})

    if retains_block:
        assert (restored.operation, restored.review_status, restored.posting_type) == (
            OperationType.PENDING_REVIEW,
            ReviewStatus.NEEDS_REVIEW,
            None,
        )
        assert restored.validation_alerts == [blocker]
    else:
        assert restored == recalculated


def _logo_png() -> bytes:
    with Image.new("RGB", (140, 55), "white") as image, BytesIO() as buffer:
        image.save(buffer, "PNG")
        return buffer.getvalue()


def _email(attachment: bytes, logo: bytes | None = None) -> bytes:
    mail = EmailMessage()
    mail["From"] = "sender@example.com"
    mail["To"] = "recipient@example.org"
    mail["Subject"] = "Invoice and logo"
    mail.set_content("Please see attached invoice.")
    if logo is not None:
        mail.add_alternative(
            "<p>Please see attached invoice.</p>"
            '<div class="signature"><p>Best regards</p><img src="cid:logo" alt="logo"></div>',
            subtype="html",
        )
        mail.get_payload()[1].add_related(
            logo,
            maintype="image",
            subtype="png",
            cid="<logo>",
            filename="logo.png",
        )
    mail.add_attachment(
        attachment,
        maintype="image",
        subtype="png",
        filename="../invoice.png",
    )
    return mail.as_bytes()


@pytest.mark.parametrize(
    ("files", "urls", "options", "code"),
    [
        ([], [], {}, "EMPTY_INPUT"),
        ([("invoice.pdf", b"a")] * 50, ["https://8.8.8.8/b.pdf"], {}, "TOO_MANY_FILES"),
        ([("invoice.exe", b"a")], [], {}, "UNSUPPORTED_EXTENSION"),
        ([], ["http://127.0.0.1/a.pdf?secret=hidden"], {}, "SSRF_BLOCKED"),
        ([("invoice.pdf", b"")], [], {}, "EMPTY_FILE"),
        ([("invoice.pdf", b"a")], [], {"ocr_mode": "LOCAL_ONLY"}, "INVALID_OCR_MODE"),
        ([("invoice.pdf", b"a")], [], {"existing_bills_json": "{}"}, "INVALID_EXISTING_BILLS"),
    ],
)
def test_rejects_invalid_inputs_before_creating_a_batch(make_service, files, urls, options, code):
    service = make_service()
    with pytest.raises(ServiceValidationError) as error:
        service.create_batch(accounting_entity=ENTITY, files=files, public_urls=urls, **options)

    assert error.value.code == code
    assert "secret=hidden" not in dumps_decimal_json(error.value.details)
    assert not service.repository.list_batches()
    assert not (service.settings.app_data_dir / "uploads").exists()


def test_oversized_stream_stops_at_limit_plus_one_and_cleans_staged_files(make_service):
    limit = 1024
    service = make_service(limits=ResourceLimits(max_file_bytes=limit))
    stream = BytesIO(b"x" * (4 * limit))

    with pytest.raises(ServiceValidationError) as error:
        _create(service, ("good.png", b"ok"), ("large.png", stream))

    assert error.value.code == "FILE_TOO_LARGE"
    assert stream.tell() == limit + 1
    assert not service.repository.list_batches()
    assert not list((service.settings.app_data_dir / "uploads").iterdir())


def test_original_and_eml_attachment_are_retained_verbatim(make_service):
    service = make_service()
    child = b"attachment original bytes"
    email = _email(child)
    detail = _create(service, ("../../invoice.eml", BytesIO(email)))
    persisted = service.repository.get_batch(detail["id"])
    root = next(doc for doc in persisted["documents"] if doc["parent_doc_id"] is None)
    attachment = next(doc for doc in persisted["documents"] if doc["source_kind"] == "IMAGE")
    directory = service.settings.app_data_dir / "uploads" / detail["id"]

    assert detail["stage"] == "COMPLETED"
    assert detail["total_files"] == detail["succeeded_files"] == 1
    assert (attachment["parent_doc_id"], attachment["sanitized_filename"]) == (
        root["id"],
        "invoice.png",
    )
    assert Path(root["stored_path"]).parent == Path(attachment["stored_path"]).parent == directory
    assert Path(root["stored_path"]).read_bytes() == email
    assert Path(attachment["stored_path"]).read_bytes() == child
    assert all("stored_path" not in doc for doc in detail["documents"])


def test_wait_false_commits_intermediate_stages_while_ocr_is_blocked(make_service, monkeypatch):
    entered, release = Event(), Event()

    class BlockingOcr(FixtureOcr):
        def recognize_image_bytes(self, content, filename):
            entered.set()
            assert release.wait(10)
            return super().recognize_image_bytes(content, filename)

    service = make_service(ocr=BlockingOcr())
    stages = []
    advance = service._advance

    def track_stage(lease, stage, version):
        new_version = advance(lease, stage, version)
        stages.append(service.get_batch(lease.batch_id)["stage"])
        return new_version

    monkeypatch.setattr(service, "_advance", track_stage)
    email = _email(b"invoice")
    queued = _create(service, ("inbox.eml", email), wait=False)
    try:
        assert queued["stage"] == "QUEUED"
        assert "records" not in queued
        assert entered.wait(10)
        processing = service.get_batch(queued["id"])
        assert (processing["status"], processing["stage"]) == ("PROCESSING", "EXTRACTING")
        persisted = service.repository.get_batch(queued["id"])
        uploaded = {
            Path(doc["stored_path"]).read_bytes()
            for doc in persisted["documents"]
            if doc["source_kind"] in {"EML", "IMAGE"}
        }
        assert uploaded == {email, b"invoice"}
        with pytest.raises(TimeoutError):
            service.wait_for_batch(queued["id"], timeout=0.01)
    finally:
        release.set()

    complete = service.wait_for_batch(queued["id"], timeout=10)
    assert stages == [
        BatchStage.UNPACKING,
        BatchStage.EXTRACTING,
        BatchStage.MATCHING,
        BatchStage.VALIDATING,
    ]
    assert (complete["status"], complete["stage"], len(complete["records"])) == (
        "COMPLETED",
        "COMPLETED",
        1,
    )
    service.close()
    assert service.wait_for_batch(queued["id"]) == complete


@pytest.mark.parametrize("boundary", ["stage", "commit", "failure", "lost-lease", "new-version"])
def test_superseded_worker_cannot_write_batch_documents_or_records(
    make_service, monkeypatch, boundary
):
    service = make_service()
    entered, release = Event(), Event()
    process_batch = service._process_batch
    run_pipeline = service._run_pipeline
    leases = []

    def capture_lease(lease, version):
        leases.append(lease)
        return process_batch(lease, version)

    def suspend_pipeline(*args, **kwargs):
        if boundary == "stage":
            entered.set()
            assert release.wait(10)
        result = run_pipeline(*args, **kwargs)
        if boundary != "stage":
            entered.set()
            assert release.wait(10)
        if boundary == "failure":
            raise RuntimeError("Worker failed after recovery had committed")
        return result

    monkeypatch.setattr(service, "_process_batch", capture_lease)
    monkeypatch.setattr(service, "_run_pipeline", suspend_pipeline)
    batch_id = _create(service, wait=False)["id"]
    task = service._tasks[batch_id]
    try:
        assert entered.wait(10)
        repository = BillingRepository(DatabaseManager(service.repository.db.path))
        active = repository.get_batch(batch_id)
        assert active["status"] == BatchStatus.PROCESSING and leases[0].active
        if boundary == "lost-lease":
            leases[0].close()
        elif boundary == "new-version":
            repository.update_batch_with_version_check(
                batch_id, active["version"], stage_message="外部操作已修改批次"
            )
        else:
            repository.update_batch_with_version_check(
                batch_id,
                active["version"],
                stage=BatchStage.FAILED,
                status=BatchStatus.PARTIAL_ERROR,
                stage_message="已标记为恢复终态",
                accounting_entity=CUSTOMER,
            )
        expected = repository.get_batch(batch_id)
    finally:
        release.set()

    assert task.result(timeout=10) == service.get_batch(batch_id)
    assert repository.get_batch(batch_id) == expected
    assert not leases[0].active


def test_existing_bill_snapshot_never_reads_from_or_writes_shared_baseline(make_service):
    service = make_service()
    service.repository.create_existing_bill(_bill("shared"))
    payload = (
        '[{"id":"isolated","accounting_entity":"NEPTUNE SHIPPING LIMITED",'
        '"reference_no":"INV-123","charge_item":"Handling Charge","amount":9007199254740991.01,'
        '"currency":"USD","posting_type":"AR"}]'
    )
    snapshot_batch = _create(service, existing_bills_json=payload)
    empty_batch = _create(service, existing_bills_json="[]")
    shared_batch = _create(service)

    assert snapshot_batch["records"][0]["matched_existing_bill_id"] == "isolated"
    assert empty_batch["records"][0]["matched_existing_bill_id"] is None
    assert shared_batch["records"][0]["matched_existing_bill_id"] == "shared"
    assert [bill.id for bill in service.repository.list_existing_bills()] == ["shared"]
    (snapshot,) = service.repository.get_batch(snapshot_batch["id"])["batch_existing_bills"]
    assert snapshot.amount == Decimal("9007199254740991.01")


def test_demo_loader_prefers_environment_and_reports_missing_samples(
    make_service, tmp_path, monkeypatch
):
    service = make_service()
    monkeypatch.setenv("SHIPPING_BILLING_REAL_SAMPLES_DIR", str(tmp_path))
    with pytest.raises(ServiceValidationError) as error:
        service.create_demo_batch()
    assert error.value.code == "DEMO_SAMPLES_MISSING"
    assert not service.repository.list_batches()

    names = ("图片示例.png", "PDF示例账单1.pdf", "PDF 示例账单2.pdf", "深圳容宇 PRE-ALERT test.eml")
    for name in names:
        (tmp_path / name).write_bytes(_email(b"demo") if name.endswith(".eml") else b"demo")
    detail = service.create_demo_batch(ocr_mode=FALLBACK)
    assert detail["total_files"] == 4
    assert {
        doc["original_filename"] for doc in detail["documents"] if not doc["parent_doc_id"]
    } == set(names)


@pytest.mark.parametrize("field", ["raw_charge_name", "resolved_alert_codes", "operation"])
def test_record_patch_rejects_read_only_fields(make_service, field):
    service = make_service()
    batch = _create(service)
    review = ReviewService(service)
    record_id = batch["records"][0]["id"]

    with pytest.raises(ServiceValidationError) as error:
        review.patch_record(batch["id"], record_id, {field: None})

    assert error.value.details["fields"] == [field]
    assert service.get_batch(batch["id"]) == batch


def test_patch_covers_all_billing_fields_preserves_null_and_audits_losslessly(make_service):
    service = make_service()
    batch = _create(service)
    batch_id = batch["id"]
    (before,) = batch["records"]
    record_id = before["id"]
    payload = loads_decimal_json(
        '{"issuer":"Targeting A Inc","recipient":"NEPTUNE SHIPPING LIMITED",'
        '"document_type":"Credit Note","reference_no":null,"document_date":"2026-11-20",'
        '"charge_item":"Manual Label","quantity":1,"unit_price":9007199254740991.01,'
        '"amount":9007199254740991.01,"currency":"US$","payment_term":"CC","posting_type":"AP",'
        '"shipment_refs":{"booking_no":"MANUAL-BOOKING","hbl_no":null},"review_note":"原文修正"}'
    )
    result = ReviewService(service).patch_record(
        batch_id,
        record_id,
        payload | {"expected_version": batch["version"]},
        operator="reviewer",
    )
    (after,) = result["records"]

    assert after["id"] == record_id and after["updated_at"] != before["updated_at"]
    assert after["document_info"] == {
        "document_type": "Credit Note",
        "reference_no": None,
        "document_date": "2026-11-20",
    }
    assert after["transaction_party"] == {
        "issuer": "Targeting A Inc",
        "recipient": ENTITY,
    }
    assert after["charge_details"] == {
        "charge_item": "Manual Label",
        "raw_charge_name": "Handling Charge",
        "quantity": Decimal("1"),
        "unit_price": Decimal("9007199254740991.01"),
        "amount": Decimal("-9007199254740991.01"),
        "currency": "USD",
        "payment_term": "CC",
    }
    assert after["financial_attributes"]["posting_type"] == "AP"
    assert after["shipment_refs"]["booking_no"] == "MANUAL-BOOKING"
    assert after["human_overrides"]["reference_no"] is None
    reopened = BillingRepository(DatabaseManager(service.settings.sqlite_db_path))
    record = reopened.get_billing_record(batch_id, record_id)
    assert record.source_values_before_overrides["reference_no"] == "INV-123"
    (action,) = record.human_review_history
    assert (action.action, action.operator, action.reason) == (
        "PATCH_RECORD",
        "reviewer",
        "原文修正",
    )
    (audit,) = reopened.list_review_audit_logs(batch_id, record_id)
    assert audit["before"]["amount"] == Decimal("100.00")
    assert audit["after"]["amount"] == Decimal("-9007199254740991.01")

    flat = ReviewService(service).patch_record(batch_id, record_id, {"hbl_no": "HBL-123"})
    assert flat["records"][0]["shipment_refs"]["booking_no"] == "MANUAL-BOOKING"
    assert flat["records"][0]["shipment_refs"]["hbl_no"] == "HBL-123"
    assert flat["records"][0]["document_info"]["reference_no"] is None


def test_manual_bill_choice_uses_current_scope_and_cannot_be_combined_with_force_create(
    make_service,
):
    service = make_service()
    service.repository.create_existing_bill(_bill("shared"))
    payload = dumps_decimal_json(
        [
            _bill("selected").model_dump(),
            _bill("different-entity", entity=CUSTOMER).model_dump(),
        ]
    )
    batch = _create(service, existing_bills_json=payload)
    batch_id = batch["id"]
    record_id = batch["records"][0]["id"]
    review = ReviewService(service)

    for other_id in ("shared", "different-entity"):
        with pytest.raises(ServiceValidationError, match="当前批次"):
            review.patch_record(batch_id, record_id, {"matched_existing_bill_id": other_id})
    with pytest.raises(ServiceValidationError) as error:
        review.patch_record(
            batch_id,
            record_id,
            {"matched_existing_bill_id": "selected", "force_create_new": True},
        )
    assert error.value.code == "CONFLICTING_MATCH_SELECTION"

    review.patch_record(batch_id, record_id, {"matched_existing_bill_id": "selected"})
    with pytest.raises(ServiceValidationError, match="不能同时"):
        review.patch_record(batch_id, record_id, {"force_create_new": True})
    created = review.patch_record(
        batch_id,
        record_id,
        {"force_create_new": True, "matched_existing_bill_id": None},
    )["records"][0]
    assert created["operation"] == "CREATE" and created["matched_existing_bill_id"] is None
    assert created["human_overrides"]["matched_existing_bill_id"] is None


def test_concurrent_patches_admit_only_one_writer_and_preserve_other_batch(make_service):
    service = make_service()
    batch = _create(service)
    other = _create(service)
    batch_id = batch["id"]
    record = batch["records"][0]
    review = ReviewService(service)
    barrier = Barrier(2)

    def patch(label):
        barrier.wait(timeout=10)
        return review.patch_record(
            batch_id,
            record["id"],
            {"charge_item": label},
            expected_version=batch["version"],
        )

    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for job in [pool.submit(patch, label) for label in ("Reviewed A", "Reviewed B")]:
            try:
                outcomes.append(job.result(timeout=10))
            except OptimisticLockError as error:
                outcomes.append(error.code)

    assert outcomes.count("BATCH_CONFLICT") == 1
    assert service.get_batch(batch_id)["version"] == batch["version"] + 1
    assert len(service.repository.list_review_audit_logs(batch_id, record["id"])) == 1
    with pytest.raises(OptimisticLockError) as error:
        review.patch_record(
            batch_id, record["id"], {"amount": 5}, expected_updated_at=record["updated_at"]
        )
    assert error.value.code == "RECORD_CONFLICT"
    with pytest.raises(ResourceNotFoundError):
        review.patch_record(other["id"], record["id"], {"amount": 5})
    assert service.get_batch(other["id"]) == other


def test_switch_entity_discards_old_decisions_and_selects_new_scoped_bill(make_service):
    service = make_service()
    repository = service.repository
    target_profile = AccountingEntityProfile(canonical_name=CUSTOMER, aliases=["Targeting Ledger"])
    target = next(
        row for row in repository.list_accounting_entities() if row["canonical_name"] == CUSTOMER
    )
    repository.update_accounting_entity(target["id"], target_profile)
    payload = dumps_decimal_json(
        [
            _bill("neptune").model_dump(),
            _bill("targeting", entity="Targeting Ledger").model_dump(),
        ]
    )
    batch = _create(service, existing_bills_json=payload)
    batch_id = batch["id"]
    assert repository.get_batch(batch_id)["entity_profile_snapshot"].canonical_name == ENTITY
    (record,) = batch["records"]
    review = ReviewService(service)
    review.patch_record(batch_id, record["id"], {"matched_existing_bill_id": "neptune"})
    approved = review.approve_record(batch_id, record["id"])
    with pytest.raises(OptimisticLockError):
        review.switch_entity(batch_id, CUSTOMER, expected_version=batch["version"])

    response = review.switch_entity(batch_id, CUSTOMER, expected_version=approved["version"])
    (switched,) = response["batch"]["records"]
    assert response["reset_approval_count"] == 1
    assert switched["id"] == record["id"]
    assert switched["matched_existing_bill_id"] == "targeting"
    assert switched["financial_attributes"] == {"accounting_entity": CUSTOMER, "posting_type": "AP"}
    assert switched["review_status"] != "APPROVED"
    assert "matched_existing_bill_id" not in switched["human_overrides"]
    assert repository.get_batch(batch_id)["entity_profile_snapshot"] == target_profile


def test_reapply_dictionary_restores_only_valid_approvals_and_keeps_overrides(make_service):
    ocr = FixtureOcr(results={b"storage": _invoice("INV-234", charge="Storage Fee")})
    service = make_service(ocr=ocr)
    batch = _create(service, ("invoice.png", b"invoice"), ("storage.png", b"storage"))
    other = _create(service)
    review = ReviewService(service)
    records = {record["charge_details"]["charge_item"]: record for record in batch["records"]}
    handling = records["Handling Charge"]["id"]
    storage = records["Storage Charge"]["id"]
    review.patch_record(batch["id"], storage, {"charge_item": "Locked Storage"})
    review.approve_record(batch["id"], handling)
    review.approve_record(batch["id"], storage)
    service.repository.upsert_charge_dictionary_entry(
        ChargeDictionaryItem(raw_alias="handling charge", standard_name="New Handling")
    )
    service.repository.upsert_charge_dictionary_entry(
        ChargeDictionaryItem(raw_alias="storage fee", standard_name="New Storage")
    )

    refreshed = review.reapply_dictionary(batch["id"])
    rows = {record["id"]: record for record in refreshed["records"]}
    assert rows[handling]["charge_details"]["charge_item"] == "New Handling"
    assert rows[handling]["review_status"] == "AUTO_READY"
    assert rows[storage]["charge_details"]["charge_item"] == "Locked Storage"
    assert rows[storage]["human_overrides"] == {"charge_item": "Locked Storage"}
    assert rows[storage]["review_status"] == "APPROVED"
    assert service.get_batch(other["id"]) == other


def test_hard_blocker_always_denies_approval_and_reject_requires_reason(make_service):
    service = make_service(ocr=FixtureOcr(results={b"invoice": _invoice(total="120.00")}))
    batch = _create(service)
    batch_id = batch["id"]
    record_id = batch["records"][0]["id"]
    review = ReviewService(service)
    with pytest.raises(ServiceValidationError) as error:
        review.approve_record(batch_id, record_id, "同意入账")

    assert (error.value.status_code, error.value.code) == (422, "HARD_BLOCKER_CANNOT_APPROVE")
    assert error.value.details == {"blocker_codes": ["AMOUNT_TOTAL_MISMATCH"]}
    assert service.get_batch(batch_id) == batch
    for reason in (" x ", "a" * 501):
        with pytest.raises(ServiceValidationError, match="2–500"):
            review.reject_record(batch_id, record_id, reason)
    rejected = review.reject_record(batch_id, record_id, "  需要重新确认  ")["records"][0]
    assert (rejected["review_status"], rejected["reject_reason"]) == ("REJECTED", "需要重新确认")


def test_approve_and_reject_service_validate_trimmed_unicode_reason(make_service):
    service = make_service()
    batch = _create(service)
    batch_id = batch["id"]
    (record,) = batch["records"]
    review = ReviewService(service)
    for decide in (review.approve_record, review.reject_record):
        for reason in ("😀", " A", "核" * 501):
            with pytest.raises(ServiceValidationError) as error:
                decide(batch_id, record["id"], reason)
            assert (error.value.status_code, error.value.code) == (422, "INVALID_REVIEW_REASON")
    assert service.get_batch(batch_id) == batch
    approved = review.approve_record(batch_id, record["id"], " 😀😀 ")
    assert approved["records"][0]["review_status"] == "APPROVED"
    assert service.repository.list_review_audit_logs(batch_id)[0]["reason"] == "😀😀"


def test_acknowledged_warning_is_tied_to_its_code_field_and_current_actual(make_service):
    service = make_service()
    batch = _create(service, ("invoice-a.png", b"a"), ("invoice-b.png", b"b"))
    batch_id = batch["id"]
    first, second = service.repository.list_billing_records(batch_id)
    for row in (first, second):
        row.shipment_refs.container_nos = ["FIRST-BAD", "SECOND-BAD"]
    old_warning = ValidationAlert.from_code(
        AlertCode.CONTEXT_INFERRED_FIELD,
        "发现来源推断",
        field="document_date",
        actual="source",
    )
    first.raw_parse_issues.append(old_warning)
    service.repository.upsert_billing_records(batch_id, [first, second])
    review = ReviewService(service)
    review.reapply_dictionary(batch_id)
    for reason in (None, "a", "a" * 501):
        with pytest.raises(ServiceValidationError, match="2–500"):
            review.approve_record(batch_id, first.id, reason)

    approved = review.approve_record(batch_id, first.id, "已核对原单")
    rows = {record["id"]: record for record in approved["records"]}
    assert rows[first.id]["review_status"] == "APPROVED"
    assert rows[first.id]["operation"] == "CREATE"
    assert {
        (alert["code"], alert["field"], alert["actual"], alert["severity"])
        for alert in rows[first.id]["validation_alerts"]
    } == {
        ("CONTAINER_ISO6346_MISMATCH", "container_nos", token, "RESOLVED")
        for token in ("FIRST-BAD", "SECOND-BAD")
    } | {("CONTEXT_INFERRED_FIELD", "document_date", "source", "RESOLVED")}
    assert {alert["severity"] for alert in rows[second.id]["validation_alerts"]} == {"WARNING"}
    first = service.repository.get_billing_record(batch_id, first.id)
    first.shipment_refs.container_nos = ["FIRST-BAD", "NEW-BAD"]
    first.raw_parse_issues.append(old_warning.model_copy(update={"field": "reference_no"}))
    service.repository.upsert_billing_record(batch_id, first)

    checked = review.reapply_dictionary(batch_id)
    refreshed = next(record for record in checked["records"] if record["id"] == first.id)
    assert refreshed["review_status"] == "NEEDS_REVIEW"
    assert {
        (alert["field"], alert["actual"], alert["severity"])
        for alert in refreshed["validation_alerts"]
    } == {
        ("container_nos", "FIRST-BAD", "RESOLVED"),
        ("container_nos", "NEW-BAD", "WARNING"),
        ("document_date", "source", "RESOLVED"),
        ("reference_no", "source", "WARNING"),
    }


def test_document_totals_patch_updates_snapshot_and_clears_reconciliation_error(make_service):
    service = make_service(ocr=FixtureOcr(results={b"invoice": _invoice(total="120.00")}))
    batch = _create(service)
    (doc,) = batch["documents"]
    updated = ReviewService(service).patch_document_totals(
        batch["id"],
        doc["id"],
        {"total_amount": Decimal("100.00"), "review_note": "核对原件"},
    )
    (record,) = updated["records"]
    (fixed_doc,) = updated["documents"]

    assert record["review_status"] == "AUTO_READY"
    assert record["validation_alerts"] == []
    assert fixed_doc["total_amount"] == Decimal("100.00")
    reopened = BillingRepository(DatabaseManager(service.settings.sqlite_db_path))
    row = reopened.get_document(batch["id"], doc["id"])
    assert row["extraction_snapshot"].total_amount == Decimal("100.00")
    assert row["metadata"]["document_totals_review"]["original"]["total_amount"] == Decimal(
        "120.00"
    )
    retried = ReviewService(service).retry_or_replace_document(batch["id"], doc["id"])
    assert retried["records"][0]["validation_alerts"] == []
    assert retried["documents"][0]["total_amount"] == Decimal("100.00")


def test_totals_rematch_saves_the_shared_baseline_with_the_records(make_service):
    service = make_service()
    repository = service.repository
    batch = _create(service)
    batch_id = batch["id"]
    (row,) = batch["records"]
    ledger = repository.create_existing_bill(_bill("recent-ledger", amount="80"))
    before = repository.get_batch(batch_id)
    assert before["shared_baseline_snapshot"] == []
    review = ReviewService(service)
    with repository.db.connect() as connection:
        connection.execute(
            """CREATE TRIGGER block_snapshot BEFORE UPDATE OF batch_existing_bills_json ON batches
               BEGIN
                   SELECT RAISE(ABORT, 'snapshot write failed');
               END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="snapshot write failed"):
        review.patch_document_totals(batch_id, row["source_doc_id"], {"total_amount": 100})

    assert repository.get_batch(batch_id) == before
    with repository.db.connect() as connection:
        connection.execute("DROP TRIGGER block_snapshot")
    after = review.patch_document_totals(batch_id, row["source_doc_id"], {"total_amount": 100})
    (matched,) = after["records"]
    assert (matched["operation"], matched["matched_existing_bill_id"]) == ("UPDATE", ledger.id)
    assert repository.get_batch(batch_id)["shared_baseline_snapshot"] == [ledger]


def test_totals_patch_corrects_only_the_selected_currency_map(make_service):
    service = make_service()
    batch = _create(service)
    batch_id = batch["id"]
    doc_id = batch["documents"][0]["id"]
    row = service.repository.get_document(batch_id, doc_id)
    snapshot = row["extraction_snapshot"].model_copy(
        update={
            "totals_by_currency": {"USD": Decimal("120"), "EUR": Decimal("25")},
            "taxes_by_currency": {"USD": Decimal("5"), "EUR": Decimal("1")},
        }
    )
    service.repository.update_document_status_and_snapshot(
        batch_id, doc_id, extraction_snapshot=snapshot
    )
    updated = ReviewService(service).patch_document_totals(
        batch_id,
        doc_id,
        {"total_currency": "USD", "tax_amount": None, "total_amount": 100},
    )

    row = service.repository.get_document(batch_id, doc_id)
    assert row["extraction_snapshot"].totals_by_currency == {
        "USD": Decimal("100"),
        "EUR": Decimal("25"),
    }
    assert row["extraction_snapshot"].taxes_by_currency == {"EUR": Decimal("1")}
    assert row["total_amount"] == Decimal("100") and row["tax_amount"] is None
    assert all(
        alert["field"] != "totals_by_currency.USD"
        for alert in updated["records"][0]["validation_alerts"]
    )


def test_add_manual_checks_document_ownership_and_reconciles_all_lines(make_service):
    service = make_service(ocr=FixtureOcr(results={b"invoice": _invoice(total="120.00")}))
    batch = _create(service)
    other = _create(service)
    batch_id = batch["id"]
    doc_id = batch["documents"][0]["id"]
    review = ReviewService(service)
    payload = {"source_doc_id": doc_id, "charge_item": "doc fee", "amount": 20, "currency": "USD"}
    with pytest.raises(ResourceNotFoundError):
        review.add_manual_record(other["id"], payload)
    updated = review.add_manual_record(batch_id, payload)

    assert [row["source_line_index"] for row in updated["records"]] == [0, 1]
    assert {row["review_status"] for row in updated["records"]} == {"AUTO_READY"}
    assert updated["records"][1]["charge_details"]["charge_item"] == "Documentation Fee"
    assert (
        len(service.repository.list_review_audit_logs(batch_id, updated["records"][1]["id"])) == 1
    )
    assert service.get_batch(other["id"]) == other


def test_restart_can_retry_attachment_and_force_ocr_skipped_image_in_same_mail(make_service):
    logo = _logo_png()
    ocr = FixtureOcr(
        results={
            b"fails-once": ValueError("cannot read yet"),
            logo: _invoice("INV-456", "35.00", charge="Storage Fee"),
        }
    )
    service = make_service(ocr=ocr)
    batch = _create(service, ("mail.eml", _email(b"fails-once", logo)))
    batch_id = batch["id"]
    (failure,) = batch["failed_documents"]
    skipped = next(doc for doc in batch["documents"] if doc["status"] == "SKIPPED_NOISE")
    assert set(failure) == {"doc_id", "filename", "error_message", "affected_record_ids"}
    assert batch["status"] == "PARTIAL_ERROR"
    assert all(content != logo for content, _ in ocr.calls)
    other = _create(service, ("other.png", b"other"))

    service.close()
    restarted = make_service(
        ocr=FixtureOcr(results={logo: _invoice("INV-456", "35.00", charge="Storage Fee")}),
        settings=service.settings,
    )
    review = ReviewService(restarted)
    retried = review.retry_or_replace_document(batch_id, failure["doc_id"])
    assert [row["source_doc_id"] for row in retried["records"]] == [failure["doc_id"]]
    forced = review.force_parse_skipped_document(batch_id, skipped["id"])
    assert (
        next(doc for doc in forced["documents"] if doc["id"] == skipped["id"])["status"] == "PARSED"
    )
    assert {row["source_doc_id"] for row in forced["records"]} == {skipped["id"], failure["doc_id"]}
    assert forced["failed_documents"] == [] and forced["status"] == "COMPLETED"
    root_id = next(doc["id"] for doc in forced["documents"] if not doc["parent_doc_id"])
    retried_email = review.retry_or_replace_document(batch_id, root_id)
    assert {row["source_doc_id"] for row in retried_email["records"]} == {
        skipped["id"],
        failure["doc_id"],
    }
    assert restarted.get_batch(other["id"]) == other
    with pytest.raises(ServiceValidationError, match="已跳过"):
        review.force_parse_skipped_document(batch_id, skipped["id"])


def test_replacement_migrates_unique_line_edits_in_place_and_prunes_old_mail_children(make_service):
    service = make_service()
    batch = _create(service, ("mail.eml", _email(b"old-attachment")))
    batch_id = batch["id"]
    root = next(doc for doc in batch["documents"] if not doc["parent_doc_id"])
    (old_record,) = batch["records"]
    other = _create(service)
    review = ReviewService(service)
    review.patch_record(
        batch_id,
        old_record["id"],
        {"amount": 101, "charge_item": "Reviewed Handling"},
    )
    after = review.retry_or_replace_document(batch_id, root["id"], file=("new.png", b"new-image"))
    (new_document,) = after["documents"]
    (migrated,) = after["records"]

    assert new_document["id"] == root["id"]
    assert migrated["id"] == old_record["id"] and migrated["source_doc_id"] == root["id"]
    assert migrated["human_overrides"] == {
        "amount": Decimal(101),
        "charge_item": "Reviewed Handling",
    }
    assert len(service.repository.list_review_audit_logs(batch_id, migrated["id"])) == 1
    assert service.get_batch(other["id"]) == other
    files = list((service.settings.app_data_dir / "uploads" / batch_id).iterdir())
    assert [path.read_bytes() for path in files] == [b"new-image"]


def test_replacement_does_not_guess_between_identical_charge_and_amount_pairs(make_service):
    ocr = FixtureOcr(results={b"second": _invoice("INV-456")})
    service = make_service(ocr=ocr)
    email = EmailMessage()
    email.set_content("Two attached invoices")
    for name in ("first", "second"):
        email.add_attachment(
            name.encode(),
            maintype="image",
            subtype="png",
            filename=f"{name}.png",
        )
    batch = _create(service, ("mail.eml", email.as_bytes()))
    batch_id = batch["id"]
    root = next(doc for doc in batch["documents"] if not doc["parent_doc_id"])
    assert len(batch["records"]) == 2
    old_ids = {record["id"] for record in batch["records"]}
    review = ReviewService(service)
    review.patch_record(batch_id, batch["records"][0]["id"], {"reference_no": "MANUAL"})

    result = review.retry_or_replace_document(batch_id, root["id"], file=("new.png", b"new"))
    (record,) = result["records"]
    assert record["id"] not in old_ids
    assert record["human_overrides"] == {}
    assert record["document_info"]["reference_no"] == "INV-123"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_replacement_moves_the_reviewed_line_to_the_surviving_copy(make_service, action):
    service = make_service(ocr=FixtureOcr(results={b"new": _invoice("INV-NEW")}))
    batch = _create(service, ("first.png", b"same"), ("second.png", b"same"))
    batch_id = batch["id"]
    repository = service.repository
    review = ReviewService(service)
    (row,) = batch["records"]
    record_id = row["id"]
    owner = row["source_doc_id"]
    survivor = next(doc["id"] for doc in batch["documents"] if doc["id"] != owner)
    review.patch_record(batch_id, record_id, {"payment_term": "PP"})
    if action == "approve":
        review.approve_record(batch_id, record_id, "已核对")
    else:
        review.reject_record(batch_id, record_id, "不应入账")
    before = repository.get_billing_record(batch_id, record_id)
    logs = repository.list_review_audit_logs(batch_id, record_id)
    assert [log["action"] for log in logs] == ["PATCH_RECORD", action.upper()]

    after = review.retry_or_replace_document(batch_id, owner, file=("new.png", b"new"))

    assert len(after["records"]) == 2
    migrated = repository.get_billing_record(batch_id, record_id)
    assert migrated is not None
    assert (migrated.source_doc_id, migrated.source_evidence.source_doc_id) == (survivor, survivor)
    assert migrated.reference_no == before.reference_no
    assert migrated.created_at == before.created_at
    assert migrated.human_overrides == before.human_overrides
    assert migrated.human_review_history == before.human_review_history
    assert migrated.review_status == before.review_status
    assert migrated.reject_reason == before.reject_reason
    assert repository.list_review_audit_logs(batch_id) == logs
    new_line = next(row for row in after["records"] if row["source_doc_id"] == owner)
    assert new_line["human_overrides"] == {}


@pytest.mark.parametrize("action", ["approve", "reject", "patch"])
def test_same_source_retry_with_unaligned_reviewed_line_keeps_old_results(make_service, action):
    ocr = FixtureOcr(results={b"same": _invoice("INV-801", "80")})
    service = make_service(ocr=ocr)
    repository = service.repository
    review = ReviewService(service)
    batch = _create(service, ("first.png", b"same"), ("second.png", b"same"))
    batch_id = batch["id"]
    (row,) = batch["records"]
    doc_id = row["source_doc_id"]
    review.patch_record(batch_id, row["id"], {"payment_term": "PP"})
    if action == "approve":
        review.approve_record(batch_id, row["id"], "已核对原件")
    elif action == "reject":
        review.reject_record(batch_id, row["id"], "该行已作废")
    before = repository.list_billing_records(batch_id)
    document_before = repository.get_document(batch_id, doc_id)
    logs = repository.list_review_audit_logs(batch_id)

    ocr.results[b"same"] = _invoice("INV-801", "81", charge="Storage Fee")
    retried = review.retry_or_replace_document(batch_id, doc_id)

    (failure,) = retried["failed_documents"]
    assert failure["doc_id"] == doc_id and "未能对齐" in failure["error_message"]
    assert repository.list_billing_records(batch_id) == before
    assert repository.get_document(batch_id, doc_id) == document_before
    assert repository.list_review_audit_logs(batch_id) == logs


@pytest.mark.parametrize(
    ("new_lines", "expected_index"),
    [
        ("Storage Fee 5 USD\nHandling Fee 81 USD", 1),
        ("Handling Fee 81 USD\nHandling Charge 5 USD", 0),
    ],
    ids=["unique-charge-moved", "repeated-charge-at-same-position"],
)
def test_same_source_retry_aligns_normalized_charges(make_service, new_lines, expected_index):
    ocr = FixtureOcr(results={b"same": _invoice("INV-801", "80")})
    service = make_service(ocr=ocr)
    repository = service.repository
    review = ReviewService(service)
    batch = _create(service, ("invoice.png", b"same"))
    batch_id = batch["id"]
    (row,) = batch["records"]
    approved = review.approve_record(batch_id, row["id"], "已核对原件")
    assert approved["records"][0]["review_status"] == "APPROVED"
    logs = repository.list_review_audit_logs(batch_id)

    ocr.results[b"same"] = _invoice("INV-801", "81", total="86").replace(
        "Handling Charge 81 USD", new_lines
    )
    retried = review.retry_or_replace_document(batch_id, row["source_doc_id"])

    assert retried["failed_documents"] == []
    assert len(retried["records"]) == 2
    handling = next(line for line in retried["records"] if line["id"] == row["id"])
    assert handling["source_line_index"] == expected_index
    assert handling["charge_details"]["charge_item"] == "Handling Charge"
    assert handling["charge_details"]["amount"] == Decimal(81)
    assert handling["review_status"] == "NEEDS_REVIEW"
    assert repository.list_review_audit_logs(batch_id) == logs


def test_same_source_retry_requires_review_when_an_override_masks_amount_change(make_service):
    ocr = FixtureOcr(results={b"same": _invoice("INV-801", "80")})
    service = make_service(ocr=ocr)
    repository = service.repository
    review = ReviewService(service)
    batch = _create(service, ("invoice.png", b"same"))
    batch_id = batch["id"]
    (row,) = batch["records"]
    record_id = row["id"]
    review.patch_record(batch_id, record_id, {"amount": 80})
    review.approve_record(batch_id, record_id, "已核对原件")
    before = repository.get_billing_record(batch_id, record_id)
    logs = repository.list_review_audit_logs(batch_id)

    ocr.results[b"same"] = _invoice("INV-801", "81", total="80")
    review.retry_or_replace_document(batch_id, row["source_doc_id"])
    after = repository.get_billing_record(batch_id, record_id)

    assert (before.raw_explicit_amount, after.raw_explicit_amount) == (80, 81)
    assert before.amount == after.amount == 80
    assert before.human_overrides == after.human_overrides == {"amount": 80}
    assert after.review_status == "NEEDS_REVIEW"
    assert repository.list_review_audit_logs(batch_id) == logs
    assert review.reapply_dictionary(batch_id)["records"][0]["review_status"] == "NEEDS_REVIEW"


def test_same_source_retry_aligns_the_one_remaining_fee_after_matching_content(make_service):
    ocr = FixtureOcr(results={b"same": _invoice_with_fees(80, 5)})
    service = make_service(ocr=ocr)
    repository = service.repository
    review = ReviewService(service)
    batch = _create(service, ("invoice.png", b"same"))
    batch_id = batch["id"]
    rejected, unchanged = batch["records"]
    review.patch_record(batch_id, rejected["id"], {"payment_term": "PP"})
    review.reject_record(batch_id, rejected["id"], "该笔费用作废")
    logs = repository.list_review_audit_logs(batch_id)

    ocr.results[b"same"] = _invoice_with_fees(5, 81)
    retried = review.retry_or_replace_document(batch_id, rejected["source_doc_id"])

    assert retried["failed_documents"] == []
    first, second = retried["records"]
    assert (first["id"], first["charge_details"]["amount"], first["review_status"]) == (
        unchanged["id"], 5, "AUTO_READY"
    )
    assert (second["id"], second["created_at"], second["charge_details"]["amount"]) == (
        rejected["id"], rejected["created_at"], 81
    )
    assert (second["operation"], second["review_status"]) == ("PENDING_REVIEW", "NEEDS_REVIEW")
    saved = repository.get_billing_record(batch_id, rejected["id"])
    assert saved.human_overrides == {"payment_term": "PP"}
    assert [action.action for action in saved.human_review_history] == ["PATCH_RECORD", "REJECT"]
    assert any(
        alert.code == AlertCode.OCR_SUSPICIOUS_FLAG and alert.field == "extraction_snapshot"
        for alert in saved.raw_parse_issues
    )
    assert repository.list_review_audit_logs(batch_id) == logs
    (official,) = serialize_records(repository.list_billing_records(batch_id), "official")
    assert official["charge_details"]["amount"] == 5


@pytest.mark.parametrize(
    ("old_fees", "new_fees"),
    [((80, 5), (81, 6, 7)), ((80, 80), (80, 80, 5))],
    ids=["no-unique-content", "identical-fees"],
)
def test_same_source_retry_holds_all_ambiguous_fees_for_review(make_service, old_fees, new_fees):
    ocr = FixtureOcr(results={b"same": _invoice_with_fees(*old_fees)})
    service = make_service(ocr=ocr)
    repository = service.repository
    review = ReviewService(service)
    batch = _create(service, ("invoice.png", b"same"))
    batch_id = batch["id"]
    reviewed, other = batch["records"]
    review.patch_record(batch_id, reviewed["id"], {"payment_term": "PP"})
    review.reject_record(batch_id, reviewed["id"], "该笔费用作废")
    logs = repository.list_review_audit_logs(batch_id)

    ocr.results[b"same"] = _invoice_with_fees(*new_fees)
    retried = review.retry_or_replace_document(batch_id, reviewed["source_doc_id"])

    assert retried["failed_documents"] == []
    rows = repository.list_billing_records(batch_id)
    assert [row.amount for row in rows] == list(new_fees)
    assert rows[0].id == reviewed["id"]
    assert len({row.id for row in rows}) == 3
    assert other["id"] not in {row.id for row in rows}
    assert [action.action for action in rows[0].human_review_history] == ["PATCH_RECORD", "REJECT"]
    assert rows[0].human_overrides == {}
    assert repository.list_review_audit_logs(batch_id) == logs
    for row in rows:
        assert (row.operation, row.review_status, row.reject_reason) == (
            "PENDING_REVIEW", "NEEDS_REVIEW", None
        )
        (warning,) = [
            alert for alert in row.raw_parse_issues if alert.field == "extraction_snapshot"
        ]
        assert warning.code == AlertCode.OCR_SUSPICIOUS_FLAG
        assert warning in row.validation_alerts
    assert {row["review_status"] for row in review.reapply_dictionary(batch_id)["records"]} == {
        "NEEDS_REVIEW"
    }
    assert serialize_records(repository.list_billing_records(batch_id), "official") == []


def test_same_source_retry_does_not_assign_old_automatic_ids_to_ambiguous_fees(make_service):
    ocr = FixtureOcr(results={b"same": _invoice_with_fees(5, 7)})
    service = make_service(ocr=ocr)
    batch = _create(service, ("invoice.png", b"same"))
    old_ids = {row["id"] for row in batch["records"]}
    assert {row["review_status"] for row in batch["records"]} == {"AUTO_READY"}

    ocr.results[b"same"] = _invoice_with_fees(6, 8)
    retried = ReviewService(service).retry_or_replace_document(
        batch["id"], batch["documents"][0]["id"]
    )

    assert retried["failed_documents"] == []
    assert [row["charge_details"]["amount"] for row in retried["records"]] == [6, 8]
    for row in retried["records"]:
        assert row["id"] not in old_ids
        assert (row["operation"], row["review_status"]) == ("PENDING_REVIEW", "NEEDS_REVIEW")
        assert any(alert["code"] == "OCR_SUSPICIOUS_FLAG" for alert in row["validation_alerts"])


def test_same_source_retry_preserves_an_unaligned_edit_without_reassigning_its_id(make_service):
    ocr = FixtureOcr(results={b"same": _invoice_with_fees(5, 7)})
    service = make_service(ocr=ocr)
    review = ReviewService(service)
    batch = _create(service, ("invoice.png", b"same"))
    batch_id = batch["id"]
    first = batch["records"][0]
    edited = review.patch_record(batch_id, first["id"], {"payment_term": "PP"})
    assert edited["records"][0]["review_status"] == "AUTO_READY"
    repository = service.repository
    before = repository.list_billing_records(batch_id)
    logs = repository.list_review_audit_logs(batch_id)

    ocr.results[b"same"] = _invoice_with_fees(6, 8)
    retried = review.retry_or_replace_document(batch_id, first["source_doc_id"])

    (failure,) = retried["failed_documents"]
    assert "未能对齐" in failure["error_message"]
    assert repository.list_billing_records(batch_id) == before
    assert repository.list_review_audit_logs(batch_id) == logs


def test_same_source_retry_preserves_audit_when_neither_reviewed_fee_can_be_aligned(make_service):
    ocr = FixtureOcr(results={b"same": _invoice_with_fees(80, 5)})
    service = make_service(ocr=ocr)
    repository = service.repository
    review = ReviewService(service)
    batch = _create(service, ("invoice.png", b"same"))
    batch_id = batch["id"]
    for row in batch["records"]:
        review.reject_record(batch_id, row["id"], "该笔费用作废")
    before = repository.list_billing_records(batch_id)
    logs = repository.list_review_audit_logs(batch_id)

    ocr.results[b"same"] = _invoice_with_fees(81, 6).replace(
        "Handling Charge 81 USD\nHandling Charge 6 USD", "Storage Fee 81 USD\nFreight Charge 6 USD"
    )
    retried = review.retry_or_replace_document(batch_id, before[0].source_doc_id)

    (failure,) = retried["failed_documents"]
    assert "未能对齐" in failure["error_message"]
    assert repository.list_billing_records(batch_id) == before
    assert repository.list_review_audit_logs(batch_id) == logs


def test_replacement_cannot_overwrite_an_edit_that_commits_during_ocr(make_service):
    entered, release = Event(), Event()

    class BlockingReplacement(FixtureOcr):
        def recognize_image_bytes(self, content, filename):
            if content == b"replacement":
                entered.set()
                assert release.wait(10)
            return super().recognize_image_bytes(content, filename)

    service = make_service(ocr=BlockingReplacement())
    batch = _create(service)
    (doc,) = batch["documents"]
    (record,) = batch["records"]
    batch_id = batch["id"]
    review = ReviewService(service)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            review.retry_or_replace_document,
            batch_id,
            doc["id"],
            file=("new.png", b"replacement"),
        )
        try:
            assert entered.wait(10)
            patched = review.patch_record(batch_id, record["id"], {"charge_item": "Reviewed"})
        finally:
            release.set()
        with pytest.raises(OptimisticLockError) as error:
            pending.result(timeout=10)

    assert error.value.code == "BATCH_CONFLICT"
    assert service.get_batch(batch_id) == patched
    paths = list((service.settings.app_data_dir / "uploads" / batch_id).iterdir())
    assert [path.read_bytes() for path in paths] == [b"invoice"]


def test_failed_document_alert_covers_parsed_summary_with_no_charge_lines(make_service):
    text = "\n".join(line for line in _invoice().splitlines() if not line.startswith("Handling"))
    service = make_service(ocr=FixtureOcr(results={b"invoice": text}))
    detail = _create(service)
    (document,) = detail["documents"]
    (failed,) = detail["failed_documents"]
    assert document["status"] == "PARSED"
    assert detail["stage"] == "FAILED" and detail["records"] == []
    assert (
        document["id"] == failed["doc_id"] and document["error_message"] == failed["error_message"]
    )


def test_foreign_original_path_is_refused_with_reupload_or_url_hint(make_service):
    source = make_service()
    foreign = _create(source)
    foreign_id = foreign["id"]
    foreign_doc = source.repository.get_batch(foreign_id)["documents"][0]
    service = make_service()
    service.repository.create_batch(
        ENTITY,
        batch_id=foreign_id,
        status="COMPLETED",
        stage="COMPLETED",
        ocr_mode=FALLBACK,
    )
    row = ParsedDocument.model_validate({**foreign_doc, "doc_id": foreign_doc["id"]})
    service.repository.upsert_source_documents(
        foreign_id,
        [row],
        stored_paths={row.doc_id: foreign_doc["stored_path"]},
    )
    review = ReviewService(service)
    with pytest.raises(ServiceValidationError) as error:
        review.retry_or_replace_document(foreign_id, row.doc_id)
    assert error.value.code == "ORIGINAL_FILE_UNAVAILABLE"
    assert "重新上传" in error.value.message and "URL" in error.value.message
    with pytest.raises(ServiceValidationError) as error:
        review.retry_or_replace_document(foreign_id, row.doc_id, file=("no.exe", b"invalid"))
    assert error.value.code == "UNSUPPORTED_EXTENSION"

    result = review.retry_or_replace_document(foreign_id, row.doc_id, file=("valid.png", b"new"))
    assert result["records"][0]["source_doc_id"] == row.doc_id
    assert Path(foreign_doc["stored_path"]).read_bytes() == b"invoice"


def test_public_url_retry_keeps_original_locator_out_of_details(make_service):
    requests = []

    class RemoteOCR:
        def recognize_url(self, url):
            requests.append(url)
            if len(requests) == 1:
                raise ValueError("temporary failure")
            payload = OcrStructuredPayload.model_validate(
                {
                    "items": [
                        {
                            "invoiceNumber": "INV-123",
                            "fileType": "Invoice",
                            "chargeList": [
                                {"name": "Handling Charge", "amount": 20, "currency": "USD"}
                            ],
                        }
                    ],
                    "has_charges": True,
                    "has_metadata": True,
                }
            )
            return SimpleNamespace(payload=payload, formatted_text=_invoice(amount="20.00"))

    service = make_service(remote_ocr=RemoteOCR())
    url = "https://8.8.8.8/invoice.png?token=private"
    batch = service.create_batch(accounting_entity=ENTITY, public_urls=[url])
    (failed,) = batch["documents"]
    result = ReviewService(service).retry_or_replace_document(batch["id"], failed["id"])
    assert requests == [url, url]
    assert batch["stage"] == "FAILED" and batch["status"] == "PARTIAL_ERROR"
    assert result["documents"][0]["status"] == "PARSED"
    assert "token=private" not in dumps_decimal_json(batch)
    assert "token=private" not in dumps_decimal_json(result)


@pytest.mark.parametrize(
    ("header", "patch", "expected_reason"),
    [
        (
            [f"Issuer: {ENTITY}", f"Bill To: {ENTITY}"],
            {"issuer": "CMA CGM (AMERICA) LLC"},
            "记账主体匹配付款方，归入应付台账",
        ),
        (
            [],
            {"posting_type": "AP"},
            "人工确认记账方向，归入应付台账",
        ),
    ],
    ids=["patch-issuer", "patch-posting-type"],
)
def test_patch_party_or_posting_type_clears_stale_unresolvable_entity_reasoning(
    make_service, header, patch, expected_reason
):
    notice = "\n".join(
        [
            "GENERAL NOTICE",
            *header,
            "On Carriage Additional 245.00 USD (CC)",
            "Total USD 245.00",
        ]
    )
    service = make_service(ocr=FixtureOcr(results={b"notice": notice}))
    batch = _create(service, ("notice.png", b"notice"))
    batch_id = batch["id"]
    record = batch["records"][0]
    stale = "当前记账主体无法唯一匹配单据的收款方或付款方"
    assert stale in record["ai_reasoning"]
    assert record["financial_attributes"]["posting_type"] is None

    updated = ReviewService(service).patch_record(batch_id, record["id"], patch)
    rechecked = updated["records"][0]
    assert rechecked["financial_attributes"]["posting_type"] == "AP"
    assert rechecked["review_status"] == "AUTO_READY"
    assert stale not in rechecked["ai_reasoning"]
    assert expected_reason in rechecked["ai_reasoning"]


def test_services_import_neither_fastapi_nor_harness():
    services = Path(__file__).resolve().parents[2] / "src/shipping_billing_agent/services"
    imported = set()
    for path in services.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.update(node.module.split("."))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.update(alias.name.split("."))
    assert imported.isdisjoint({"fastapi", "harness"})
