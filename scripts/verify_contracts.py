from __future__ import annotations

import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import iso4217
import jsonschema

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "workflow" / "contracts"
DIAGRAMS = ROOT / "docs" / "workflow" / "diagrams"


def verify_schema_sql() -> None:
    sql = (CONTRACTS / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)

    tables = sorted(
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    )
    expected = sorted(
        [
            "accounting_entities",
            "baseline_sync_items",
            "batches",
            "billing_records",
            "charge_dictionary",
            "eval_runs",
            "existing_bills",
            "source_documents",
        ]
    )
    assert tables == expected, f"Unexpected tables: {tables}"

    conn.execute(
        "INSERT INTO accounting_entities VALUES (?, ?, ?, ?, ?, ?)",
        (
            "e1",
            "NEPTUNE SHIPPING LIMITED",
            json.dumps(["NEPTUNE SHIPPING LIMITED (LAX)"]),
            1,
            "2026-11-19T00:00:00Z",
            "2026-11-19T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "b1",
            "B-1119-01",
            "NEPTUNE SHIPPING LIMITED",
            "WEB",
            "COMPLETED",
            "COMPLETED",
            "解析完成",
            "REMOTE_ONLY",
            1,
            1,
            0,
            1,
            None,
            "[]",
            "2026-11-19T00:00:00Z",
            "2026-11-19T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO source_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "d1",
            "b1",
            None,
            "PDF 示例账单2.pdf",
            "PDF_2.pdf",
            "application/pdf",
            "PDF",
            "abc123",
            None,
            None,
            "REMOTE_OCR",
            "PARSED",
            None,
            "raw",
            "{}",
            None,
            "[]",
            "{}",
            None,
            None,
            "1410.40",
            "USD",
            "2026-11-19T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO existing_bills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "eb1",
            "EB-1",
            "NEPTUNE SHIPPING LIMITED",
            "Debit Note",
            "DC-108478",
            "2026-10-24",
            "NEPTUNE SHIPPING LIMITED (LAX)",
            "Client",
            "MAEU275752922",
            "CGT260801188",
            None,
            "[]",
            "Handling Charge",
            "HANDLING CHARGE",
            "1.00",
            "65.00",
            "65.00",
            "USD",
            "CC",
            "AR",
            "{}",
            1,
            "2026-11-19T00:00:00Z",
            "2026-11-19T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO billing_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "r1",
            "b1",
            "d1",
            0,
            "UPDATE",
            "AUTO_READY",
            "L1_DOC_CHARGE",
            "eb1",
            "[]",
            "Debit Note",
            "DC-108478",
            "2026-10-24",
            "NEPTUNE SHIPPING LIMITED (LAX)",
            "Client",
            "Handling Charge",
            "HANDLING CHARGE",
            "1.00",
            "65.00",
            "65.00",
            "USD",
            "CC",
            "NEPTUNE SHIPPING LIMITED",
            "AR",
            "{}",
            "{}",
            "[]",
            "[]",
            json.dumps(["Matched L1"]),
            "{}",
            "{}",
            None,
            0.99,
            1,
            "2026-11-19T00:00:00Z",
            "2026-11-19T00:00:00Z",
        ),
    )

    # 8th table: baseline_sync_items idempotency check
    conn.execute(
        "INSERT INTO baseline_sync_items VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bs1", "b1", "r1", "NEPTUNE SHIPPING LIMITED", "2026-11-19T00:00:00Z", "eb1", "2026-11-19T00:01:00Z"),
    )
    try:
        conn.execute(
            "INSERT INTO baseline_sync_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("bs2", "b1", "r1", "NEPTUNE SHIPPING LIMITED", "2026-11-19T00:00:00Z", "eb1", "2026-11-19T00:02:00Z"),
        )
        raise AssertionError("Expected UNIQUE constraint failure on duplicate baseline_sync_items")
    except sqlite3.IntegrityError:
        pass

    # eval_runs supports UNVERIFIED with NULL metrics
    conn.execute(
        "INSERT INTO eval_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ev1", "LIVE_E2E", "UNVERIFIED", "OCR gateway unreachable", "deepseek-flash", 3, 0, 10, None, None, None, None, None, None, None, "{}", "2026-11-19T00:00:00Z"),
    )

    # Reject retry_count = 2
    try:
        conn.execute(
            "INSERT INTO billing_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "r2", "b1", "d1", 1, "CREATE", "AUTO_READY", "NONE", None, "[]",
                "Debit Note", "DC-1", "2026-10-24", "A", "B", "Handling Charge", "HC",
                "1", "10", "10", "USD", "CC", "A", "AR", "{}", "{}", "[]", "[]", "[]", "{}", "{}",
                None, 0.9, 2, "2026-11-19T00:00:00Z", "2026-11-19T00:00:00Z",
            ),
        )
        raise AssertionError("Expected CHECK constraint rejection for retry_count=2")
    except sqlite3.IntegrityError:
        pass

    # Reject non-transactional XXX or US$ currency
    for bad_curr in ("US$", "XXX"):
        try:
            conn.execute(
                "INSERT INTO existing_bills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"eb-{bad_curr}", None, "NEPTUNE", None, None, None, None, None, None, None, None,
                    "[]", "Handling Charge", None, None, None, "10.00", bad_curr, None, "AR", "{}", 1,
                    "2026-11-19T00:00:00Z", "2026-11-19T00:00:00Z",
                ),
            )
            raise AssertionError(f"Expected CHECK rejection for currency {bad_curr}")
        except sqlite3.IntegrityError:
            pass

    # Verify URL source_kind accepts NULL content_sha256 while local PDF rejects NULL content_sha256
    conn.execute(
        "INSERT INTO source_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "d-url",
            "b1",
            None,
            "remote_bill.pdf",
            "remote_bill.pdf",
            "application/pdf",
            "URL",
            None,
            None,
            "https://example.com/remote_bill.pdf",
            "REMOTE_OCR",
            "PARSED",
            None,
            "raw",
            "{}",
            None,
            "[]",
            "{}",
            None,
            None,
            "100.00",
            "USD",
            "2026-11-19T00:00:00Z",
        ),
    )
    try:
        conn.execute(
            "INSERT INTO source_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "d-bad-pdf",
                "b1",
                None,
                "local.pdf",
                "local.pdf",
                "application/pdf",
                "PDF",
                None,
                None,
                None,
                "REMOTE_OCR",
                "PARSED",
                None,
                "raw",
                "{}",
                None,
                "[]",
                "{}",
                None,
                None,
                None,
                None,
                "2026-11-19T00:00:00Z",
            ),
        )
        raise AssertionError("Expected CHECK rejection for local PDF with NULL content_sha256")
    except sqlite3.IntegrityError:
        pass

    print("schema.sql PASSED (8 tables, Decimal text storage, retry<=1, baseline_sync_items, eval_runs NULLs)")


def verify_json_schema() -> None:
    schema = json.loads((CONTRACTS / "billing-output.schema.json").read_text(encoding="utf-8"))
    # Verify schema enum matches iso4217 package excluding XXX/XTS
    expected_codes = {c.code for c in iso4217.Currency} - {"XXX", "XTS"}
    schema_codes = set(schema["$defs"]["ActiveIsoCurrency"]["enum"])
    assert schema_codes == expected_codes, "ActiveIsoCurrency enum drifted from iso4217 package"

    valid_payload = [
        {
            "operation": "UPDATE",
            "document_info": {"document_type": "Debit Note", "reference_no": "DN-202609-001", "document_date": "2026-09-20"},
            "transaction_party": {"issuer": "Destination Agent", "recipient": "Origin Agent"},
            "charge_details": {"charge_item": "Handling Charge", "amount": 40.0, "currency": "USD", "payment_term": "PP"},
            "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": "AP"},
            "audit_trail": {
                "matched_bill_ref": "DN-202609-001 / Handling Charge",
                "ai_reasoning": ["Matched by doc no and charge item"],
            },
        },
        {
            "operation": "CREATE",
            "document_info": {"document_type": "Credit Note", "reference_no": "CN-1", "document_date": "2026-11-10"},
            "transaction_party": {"issuer": "Vendor", "recipient": "Origin Agent"},
            "charge_details": {"charge_item": "Handling Charge", "raw_charge_name": "H/D Fee", "quantity": 1, "unit_price": -40.0, "amount": -40.0, "currency": "USD", "payment_term": "PP"},
            "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": "AP"},
            "audit_trail": {"ai_reasoning": ["Credit note AP reduction -40.00"]},
        },
        {
            "operation": "PENDING_REVIEW",
            "document_info": {"document_type": "Invoice", "reference_no": None, "document_date": None},
            "transaction_party": {"issuer": "Carrier", "recipient": None},
            "charge_details": {"charge_item": "Wharfage", "amount": None, "currency": None, "payment_term": None},
            "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": None},
            "audit_trail": {"ai_reasoning": ["Missing amount and currency, intercepted for review"]},
        },
    ]
    jsonschema.validate(instance=valid_payload, schema=schema)

    def must_fail(item: dict, desc: str) -> None:
        try:
            jsonschema.validate(instance=[item], schema=schema)
            raise AssertionError(f"Expected validation failure for: {desc}")
        except jsonschema.ValidationError:
            pass

    base = {
        "operation": "CREATE",
        "document_info": {"document_type": "Invoice", "reference_no": "1", "document_date": "2026-10-01"},
        "transaction_party": {"issuer": "A", "recipient": "B"},
        "charge_details": {"charge_item": "THC", "amount": 40.0, "currency": "USD", "payment_term": "PP"},
        "financial_attributes": {"accounting_entity": "B", "posting_type": "AP"},
        "audit_trail": {"ai_reasoning": ["reason"]},
    }
    bad_str = json.loads(json.dumps(base))
    bad_str["charge_details"]["amount"] = "40.00"
    must_fail(bad_str, "string amount")

    bad_sym = json.loads(json.dumps(base))
    bad_sym["charge_details"]["currency"] = "US$"
    must_fail(bad_sym, "symbol currency US$")

    bad_zzz = json.loads(json.dumps(base))
    bad_zzz["charge_details"]["currency"] = "ZZZ"
    must_fail(bad_zzz, "fake 3-letter currency ZZZ")

    bad_xxx = json.loads(json.dumps(base))
    bad_xxx["charge_details"]["currency"] = "XXX"
    must_fail(bad_xxx, "non-transactional currency XXX")

    bad_null = json.loads(json.dumps(base))
    bad_null["charge_details"]["amount"] = None
    must_fail(bad_null, "CREATE with null amount")

    bad_upd = json.loads(json.dumps(base))
    bad_upd["operation"] = "UPDATE"
    must_fail(bad_upd, "UPDATE missing audit_trail.matched_bill_ref")

    # Verify Decimal tolerance vs IEEE-754 float trap (abs(1.12 - 1.10) > 0.02 in float!)
    assert abs(Decimal("1.12") - Decimal("1.10")) <= Decimal("0.02")
    print("billing-output.schema.json & Decimal tolerance PASSED (176 ISO codes, all 6 negative cases rejected)")


def _est_w(text: str, fs: float) -> float:
    return sum(fs if ord(c) > 127 else 0.6 * fs for c in text)


def verify_diagrams() -> None:
    expected = [
        "user-billing-flow.html",
        "operation-decision-flow.html",
        "architecture.html",
        "pipeline-sequence.html",
    ]
    for name in expected:
        p = DIAGRAMS / name
        txt = p.read_text(encoding="utf-8")
        assert not re.search(r"[①-⓿❶-➓㉑-㉟㊱-㊿⃣]", txt), f"Circled numeral in {name}"
        assert "<script" not in txt.lower(), f"Script tag in {name}"
        m = re.search(r"<svg.*?</svg>", txt, re.S)
        assert m, f"No SVG in {name}"
        root = ET.fromstring(m.group(0))
        vb = [float(v) for v in root.attrib["viewBox"].split()]
        assert vb[2] <= 712, f"{name} viewBox width {vb[2]} > 712"

        boxes = []
        for t in root.iter("text"):
            s = (t.text or "").strip()
            if not s:
                continue
            cls = t.attrib.get("class", "")
            fs = 15.0 if any(k in cls for k in ("tt", "ly-h", "lane-h", "hh")) else (14.0 if "nn" in cls else 13.0)
            w = _est_w(s, fs)
            x, y = float(t.attrib["x"]), float(t.attrib["y"])
            if any(k in cls for k in ("lane-h", "tt-c", "hh", "hd", "lbl", "nn")):
                x0, x1 = x - w / 2, x + w / 2
            else:
                x0, x1 = x, x + w
            boxes.append((s, x0, y - fs / 2, x1, y + fs / 2))
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]:
                    raise AssertionError(f"{name}: text overlap '{a[0]}' vs '{b[0]}'")
    print("All 4 SVG diagrams PASSED (zero overlap, valid XML, viewBox<=712)")


if __name__ == "__main__":
    verify_schema_sql()
    verify_json_schema()
    verify_diagrams()
