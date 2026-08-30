"""
bill_customer_mapper.py

Drop-in module for the existing QBOClient class.

Workflow:
1. Popup asks for Start Date / End Date using calendar controls.
2. Popup asks user to select a CSV mapping file.
3. CSV must contain CustomerName, CustomerId, RRID.
4. Query QBO Bills whose TxnDate is within the selected date range.
5. For each Bill, inspect VendorRef.name. If it contains '@', take the
   text after the last '@' and exact-match it to RRID (case-insensitive).
6. If matched, set CustomerRef on each supported expense line.
7. Preview/confirmation before writing anything to QBO.
8. Re-fetch each Bill immediately before update so latest SyncToken is used.
9. Write an audit CSV into ./output/.

Supported Bill line types:
- AccountBasedExpenseLineDetail
- ItemBasedExpenseLineDetail

QBO JSON reference keys are lowercase: 'value' and 'name'.
"""

from __future__ import annotations

import csv
import copy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from tkcalendar import DateEntry
except ImportError as exc:
    raise ImportError(
        "tkcalendar is required. Install it with:\n"
        "    pip install tkcalendar"
    ) from exc

REQUIRED_MAPPING_COLUMNS = {"CustomerName", "CustomerId", "RRID"}
PAGE_SIZE = 1000
SUPPORTED_LINE_DETAILS = (
    "AccountBasedExpenseLineDetail",
    "ItemBasedExpenseLineDetail",
)


class BillCustomerMapperError(RuntimeError):
    pass


@dataclass(frozen=True)
class CustomerMapping:
    customer_name: str
    customer_id: str
    rrid: str


@dataclass(frozen=True)
class BillPlan:
    bill_id: str
    doc_number: str
    txn_date: str
    vendor_name: str
    rrid: str
    customer_name: str
    customer_id: str


def _root() -> tk.Tk:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


# ----------------------------- UI -----------------------------------------

def ask_date_range() -> Optional[Tuple[date, date]]:
    """Calendar popup for inclusive Start Date and End Date."""
    root = tk.Tk()
    root.title("QBO Bill Date Range")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    result = {"value": None}

    tk.Label(root, text="Select Bill Date range", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, columnspan=2, padx=18, pady=(16, 12)
    )

    tk.Label(root, text="Start Date:").grid(
        row=1, column=0, sticky="e", padx=(18, 8), pady=7
    )
    start_entry = DateEntry(root, width=14, date_pattern="yyyy-mm-dd")
    start_entry.grid(row=1, column=1, sticky="w", padx=(0, 18), pady=7)

    tk.Label(root, text="End Date:").grid(
        row=2, column=0, sticky="e", padx=(18, 8), pady=7
    )
    end_entry = DateEntry(root, width=14, date_pattern="yyyy-mm-dd")
    end_entry.grid(row=2, column=1, sticky="w", padx=(0, 18), pady=7)

    def submit():
        start_date = start_entry.get_date()
        end_date = end_entry.get_date()
        if start_date > end_date:
            messagebox.showwarning(
                "Invalid Date Range",
                "Start Date cannot be later than End Date.",
                parent=root,
            )
            return
        result["value"] = (start_date, end_date)
        root.destroy()

    def cancel():
        root.destroy()

    button_frame = tk.Frame(root)
    button_frame.grid(row=3, column=0, columnspan=2, pady=(12, 16))
    tk.Button(button_frame, text="Continue", width=12, command=submit).pack(
        side="left", padx=5
    )
    tk.Button(button_frame, text="Cancel", width=12, command=cancel).pack(
        side="left", padx=5
    )

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()
    return result["value"]


def ask_mapping_csv() -> Optional[Path]:
    root = _root()
    try:
        selected = filedialog.askopenfilename(
            parent=root,
            title="Select Customer Mapping CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None


def _show_warning(title: str, message: str) -> None:
    root = _root()
    try:
        messagebox.showwarning(title, message, parent=root)
    finally:
        root.destroy()


def _show_error(title: str, message: str) -> None:
    root = _root()
    try:
        messagebox.showerror(title, message, parent=root)
    finally:
        root.destroy()


def _show_info(title: str, message: str) -> None:
    root = _root()
    try:
        messagebox.showinfo(title, message, parent=root)
    finally:
        root.destroy()


def _confirm_updates(plans: List[BillPlan]) -> bool:
    preview_lines = []
    for plan in plans[:12]:
        preview_lines.append(
            f"• {plan.txn_date} | Bill {plan.doc_number or plan.bill_id} | "
            f"{plan.rrid} → {plan.customer_name} ({plan.customer_id})"
        )
    if len(plans) > 12:
        preview_lines.append(f"... plus {len(plans) - 12} more Bill(s)")

    message = (
        f"{len(plans)} Bill(s) have a matching RRID.\n\n"
        "The module will update CustomerRef on all supported expense lines "
        "for those Bills.\n\n"
        + "\n".join(preview_lines)
        + "\n\nProceed with QBO updates?"
    )

    root = _root()
    try:
        return messagebox.askyesno("Confirm QBO Bill Updates", message, parent=root)
    finally:
        root.destroy()


# -------------------------- Mapping CSV -----------------------------------

def _normalize_rrid(value: str) -> str:
    return (value or "").strip().casefold()


def load_mapping_csv(csv_path: Path) -> Dict[str, CustomerMapping]:
    """Validate and load CustomerName, CustomerId, RRID from CSV header/data."""
    if csv_path.suffix.lower() != ".csv":
        raise BillCustomerMapperError("incorrect mapping file")

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = set(reader.fieldnames or [])
            if not REQUIRED_MAPPING_COLUMNS.issubset(headers):
                raise BillCustomerMapperError("incorrect mapping file")

            mappings: Dict[str, CustomerMapping] = {}

            for row_number, row in enumerate(reader, start=2):
                name = (row.get("CustomerName") or "").strip()
                customer_id = (row.get("CustomerId") or "").strip()
                rrid = (row.get("RRID") or "").strip()

                if not name and not customer_id and not rrid:
                    continue

                if not name or not customer_id or not rrid:
                    raise BillCustomerMapperError(
                        f"incorrect mapping file\n\nRow {row_number} has a blank required value."
                    )

                key = _normalize_rrid(rrid)
                mapping = CustomerMapping(name, customer_id, rrid)

                if key in mappings:
                    old = mappings[key]
                    if old.customer_id != customer_id or old.customer_name != name:
                        raise BillCustomerMapperError(
                            "incorrect mapping file\n\n"
                            f"RRID '{rrid}' is mapped to more than one customer."
                        )
                else:
                    mappings[key] = mapping

    except (UnicodeDecodeError, csv.Error) as exc:
        raise BillCustomerMapperError("incorrect mapping file") from exc

    if not mappings:
        raise BillCustomerMapperError("incorrect mapping file")

    return mappings


# -------------------------- QBO REST --------------------------------------

def _qbo_headers(qbo, include_content_type: bool = False) -> dict:
    headers = dict(qbo.headers)
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _raise_api_error(response: requests.Response, operation: str) -> None:
    if response.ok:
        return
    intuit_tid = response.headers.get("intuit_tid", "")
    tid_text = f"\nIntuit TID: {intuit_tid}" if intuit_tid else ""
    raise BillCustomerMapperError(
        f"{operation} failed ({response.status_code}).{tid_text}\n{response.text}"
    )


def query_bills_by_date(qbo, start_date: date, end_date: date, page_size: int = PAGE_SIZE) -> List[dict]:
    all_bills: List[dict] = []
    start_position = 1

    while True:
        query = (
            "SELECT * FROM Bill "
            f"WHERE TxnDate >= '{start_date.isoformat()}' "
            f"AND TxnDate <= '{end_date.isoformat()}' "
            f"STARTPOSITION {start_position} "
            f"MAXRESULTS {page_size}"
        )

        url = f"{qbo.base_url}/v3/company/{qbo.realm_id}/query"
        response = requests.get(
            url,
            headers=_qbo_headers(qbo),
            params={"query": query},
            timeout=qbo.timeout,
        )
        _raise_api_error(response, "Bill query")

        page = response.json().get("QueryResponse", {}).get("Bill", []) or []
        all_bills.extend(page)

        if len(page) < page_size:
            break
        start_position += len(page)

    return all_bills


def get_bill_by_id(qbo, bill_id: str) -> dict:
    url = f"{qbo.base_url}/v3/company/{qbo.realm_id}/bill/{bill_id}"
    response = requests.get(url, headers=_qbo_headers(qbo), timeout=qbo.timeout)
    _raise_api_error(response, f"Get Bill {bill_id}")
    bill = response.json().get("Bill")
    if not bill:
        raise BillCustomerMapperError(f"QBO did not return Bill data for Bill ID {bill_id}.")
    return bill


def get_vendor_name(qbo, vendor_ref: dict, vendor_cache: Dict[str, str]) -> str:
    if not isinstance(vendor_ref, dict):
        return ""

    name = (vendor_ref.get("name") or "").strip()
    if name:
        return name

    vendor_id = str(vendor_ref.get("value") or "").strip()
    if not vendor_id:
        return ""
    if vendor_id in vendor_cache:
        return vendor_cache[vendor_id]

    url = f"{qbo.base_url}/v3/company/{qbo.realm_id}/vendor/{vendor_id}"
    response = requests.get(url, headers=_qbo_headers(qbo), timeout=qbo.timeout)
    _raise_api_error(response, f"Get Vendor {vendor_id}")

    vendor = response.json().get("Vendor", {}) or {}
    name = (vendor.get("DisplayName") or "").strip()
    vendor_cache[vendor_id] = name
    return name


def update_bill(qbo, bill: dict) -> dict:
    """Full Bill update using the fresh Bill object/SyncToken."""
    if not bill.get("Id"):
        raise BillCustomerMapperError("Bill update payload is missing Id.")
    if bill.get("SyncToken") is None:
        raise BillCustomerMapperError(
            f"Bill {bill.get('Id')} update payload is missing SyncToken."
        )

    payload = copy.deepcopy(bill)
    url = f"{qbo.base_url}/v3/company/{qbo.realm_id}/bill"
    response = requests.post(
        url,
        headers=_qbo_headers(qbo, include_content_type=True),
        json=payload,
        timeout=qbo.timeout,
    )
    _raise_api_error(response, f"Update Bill {bill.get('Id')}")

    updated = response.json().get("Bill")
    if not updated:
        raise BillCustomerMapperError(
            f"QBO update response did not contain Bill {bill.get('Id')}."
        )
    return updated


# ----------------------- Matching / updating ------------------------------

def rrid_after_at(vendor_name: str) -> Optional[str]:
    if "@" not in vendor_name:
        return None
    rrid = vendor_name.rsplit("@", 1)[1].strip()
    return rrid or None


def apply_customer_ref_to_bill_lines(bill: dict, customer_name: str, customer_id: str) -> Tuple[int, int]:
    changed = 0
    supported = 0
    target_ref = {"value": str(customer_id), "name": str(customer_name)}

    for line in bill.get("Line", []) or []:
        detail_type = line.get("DetailType")
        if detail_type not in SUPPORTED_LINE_DETAILS:
            continue

        detail = line.get(detail_type)
        if not isinstance(detail, dict):
            continue

        supported += 1
        existing = detail.get("CustomerRef") or {}
        if (
            str(existing.get("value") or "") == target_ref["value"]
            and str(existing.get("name") or "") == target_ref["name"]
        ):
            continue

        detail["CustomerRef"] = dict(target_ref)
        changed += 1

    return changed, supported


def build_plans(qbo, bills: Iterable[dict], mappings: Dict[str, CustomerMapping]) -> Tuple[List[BillPlan], dict]:
    vendor_cache: Dict[str, str] = {}
    plans: List[BillPlan] = []
    stats = {
        "bills_in_range": 0,
        "vendor_without_at": 0,
        "rrid_not_mapped": 0,
        "matched_bills": 0,
    }

    for bill in bills:
        stats["bills_in_range"] += 1
        vendor_name = get_vendor_name(qbo, bill.get("VendorRef") or {}, vendor_cache)
        rrid = rrid_after_at(vendor_name)
        if not rrid:
            stats["vendor_without_at"] += 1
            continue

        mapping = mappings.get(_normalize_rrid(rrid))
        if not mapping:
            stats["rrid_not_mapped"] += 1
            continue

        bill_id = str(bill.get("Id") or "")
        if not bill_id:
            continue

        plans.append(
            BillPlan(
                bill_id=bill_id,
                doc_number=str(bill.get("DocNumber") or ""),
                txn_date=str(bill.get("TxnDate") or ""),
                vendor_name=vendor_name,
                rrid=rrid,
                customer_name=mapping.customer_name,
                customer_id=mapping.customer_id,
            )
        )
        stats["matched_bills"] += 1

    return plans, stats


# --------------------------- Audit log ------------------------------------

def write_audit_log(rows: List[dict], output_dir: Optional[Path] = None) -> Path:
    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"bill_customer_mapping_{timestamp}.csv"

    fieldnames = [
        "BillId", "DocNumber", "TxnDate", "VendorName", "RRID",
        "CustomerName", "CustomerId", "SupportedLines", "UpdatedLines",
        "Status", "Message",
    ]

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return path.resolve()


# --------------------------- Main entry -----------------------------------

def run_bill_customer_mapping(qbo) -> Optional[dict]:
    """Pass an already-authenticated QBOClient instance."""
    date_range = ask_date_range()
    if date_range is None:
        return None
    start_date, end_date = date_range

    mapping_path = ask_mapping_csv()
    if mapping_path is None:
        return None

    try:
        mappings = load_mapping_csv(mapping_path)
    except BillCustomerMapperError:
        _show_warning("Mapping File", "incorrect mapping file")
        return None

    try:
        bills = query_bills_by_date(qbo, start_date, end_date)
        plans, stats = build_plans(qbo, bills, mappings)
    except Exception as exc:
        _show_error("QBO Error", str(exc))
        raise

    if not plans:
        _show_info(
            "QBO Bill Customer Mapping",
            "No Bills require mapping.\n\n"
            f"Bills in date range: {stats['bills_in_range']}\n"
            f"VendorRef without @: {stats['vendor_without_at']}\n"
            f"RRID not found in mapping: {stats['rrid_not_mapped']}",
        )
        return {**stats, "updated_bills": 0, "updated_lines": 0, "failed_bills": 0}

    if not _confirm_updates(plans):
        return {
            **stats,
            "updated_bills": 0,
            "updated_lines": 0,
            "failed_bills": 0,
            "cancelled": True,
        }

    audit_rows: List[dict] = []
    updated_bills = 0
    updated_lines_total = 0
    failed_bills = 0
    skipped_no_supported_lines = 0
    skipped_already_correct = 0
    vendor_cache: Dict[str, str] = {}

    for plan in plans:
        audit = {
            "BillId": plan.bill_id,
            "DocNumber": plan.doc_number,
            "TxnDate": plan.txn_date,
            "VendorName": plan.vendor_name,
            "RRID": plan.rrid,
            "CustomerName": plan.customer_name,
            "CustomerId": plan.customer_id,
            "SupportedLines": 0,
            "UpdatedLines": 0,
            "Status": "",
            "Message": "",
        }

        try:
            fresh_bill = get_bill_by_id(qbo, plan.bill_id)

            current_vendor_name = get_vendor_name(
                qbo, fresh_bill.get("VendorRef") or {}, vendor_cache
            )
            current_rrid = rrid_after_at(current_vendor_name)
            if _normalize_rrid(current_rrid or "") != _normalize_rrid(plan.rrid):
                audit["Status"] = "SKIPPED"
                audit["Message"] = "VendorRef changed after scan; Bill was not updated."
                audit_rows.append(audit)
                continue

            changed_lines, supported_lines = apply_customer_ref_to_bill_lines(
                fresh_bill,
                customer_name=plan.customer_name,
                customer_id=plan.customer_id,
            )
            audit["SupportedLines"] = supported_lines
            audit["UpdatedLines"] = changed_lines

            if supported_lines == 0:
                skipped_no_supported_lines += 1
                audit["Status"] = "SKIPPED"
                audit["Message"] = (
                    "No AccountBasedExpenseLineDetail or "
                    "ItemBasedExpenseLineDetail lines."
                )
                audit_rows.append(audit)
                continue

            if changed_lines == 0:
                skipped_already_correct += 1
                audit["Status"] = "NO CHANGE"
                audit["Message"] = "CustomerRef is already correct on all supported lines."
                audit_rows.append(audit)
                continue

            updated_bill = update_bill(qbo, fresh_bill)
            updated_bills += 1
            updated_lines_total += changed_lines
            audit["Status"] = "UPDATED"
            audit["Message"] = (
                f"Updated successfully. New SyncToken: {updated_bill.get('SyncToken', '')}"
            )
            audit_rows.append(audit)

        except Exception as exc:
            failed_bills += 1
            audit["Status"] = "FAILED"
            audit["Message"] = str(exc).replace("\n", " ")
            audit_rows.append(audit)

    audit_path = write_audit_log(audit_rows)

    result = {
        **stats,
        "updated_bills": updated_bills,
        "updated_lines": updated_lines_total,
        "failed_bills": failed_bills,
        "skipped_no_supported_lines": skipped_no_supported_lines,
        "skipped_already_correct": skipped_already_correct,
        "audit_log": str(audit_path),
    }

    _show_info(
        "QBO Bill Customer Mapping Complete",
        f"Date range: {start_date} to {end_date}\n\n"
        f"Bills in range: {stats['bills_in_range']}\n"
        f"Matched Bills: {stats['matched_bills']}\n"
        f"Updated Bills: {updated_bills}\n"
        f"Updated Lines: {updated_lines_total}\n"
        f"Already correct: {skipped_already_correct}\n"
        f"No supported lines: {skipped_no_supported_lines}\n"
        f"Failed Bills: {failed_bills}\n\n"
        f"Audit log:\n{audit_path}",
    )

    return result
