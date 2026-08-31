"""
bill_customer_mapper.py

Drop-in module for the existing QBOClient class.

Workflow:
1. Popup asks for Start Date / End Date using calendar controls.
2. Popup asks user to select a CSV mapping file.
3. CSV must contain CustomerName, CustomerId, RRID, ParentCustomerId.
4. Query QBO Bills whose TxnDate is within the selected date range.
5. For each Bill, inspect DocNumber. If it contains '@', take the
   text after the last '@' and exact-match it to RRID (case-insensitive).
6. If matched, set CustomerRef on each supported expense line.
7. Matching Bills proceed directly to update.
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

REQUIRED_MAPPING_COLUMNS = {"CustomerName", "CustomerId", "RRID", "ParentCustomerId"}
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
    parent_customer_id: str
    csv_row_number: int


@dataclass(frozen=True)
class BillPlan:
    bill_id: str
    doc_number: str
    txn_date: str
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


# -------------------------- Mapping CSV -----------------------------------

def _normalize_rrid(value: str) -> str:
    return (value or "").strip().casefold()


def load_mapping_csv(csv_path: Path) -> Dict[str, CustomerMapping]:
    """
    Validate and load the customer/RRID mapping.

    Current rules
    -------------
    Required CSV columns:
        CustomerName, CustomerId, RRID, ParentCustomerId

    Blank RRID:
        Ignore that row for direct RRID matching. However, the row is still
        kept in the CustomerId lookup because another row's ParentCustomerId
        may point to it.

    RRID used by exactly one customer:
        Map directly to that row's CustomerName / CustomerId.

    RRID shared by more than one different customer:
        1. Review every customer row sharing that RRID.
        2. If that row's ParentCustomerId is blank, ignore that row for
           resolving the shared RRID.
        3. If ParentCustomerId is populated, find a row anywhere in the CSV
           whose CustomerId exactly matches that ParentCustomerId.
        4. The matched parent row's CustomerName / CustomerId is the customer
           that will be used for QBO CustomerRef.
        5. If multiple child rows resolve to the same parent CustomerId, that
           is valid.
        6. If the shared RRID resolves to more than one different parent
           CustomerId, stop with a detailed ambiguity error.
        7. If no populated ParentCustomerId can be resolved, stop with a
           detailed error.
    """
    if csv_path.suffix.lower() != ".csv":
        raise BillCustomerMapperError(
            "Incorrect mapping file.\n\n"
            "Cause: The selected file is not a CSV file.\n"
            f"Selected file: {csv_path.name}\n\n"
            "Required file type: .csv"
        )

    if not csv_path.exists():
        raise BillCustomerMapperError(
            "Incorrect mapping file.\n\n"
            "Cause: The selected file no longer exists or cannot be found.\n"
            f"Selected path: {csv_path}"
        )

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)

            raw_headers = reader.fieldnames or []
            headers = {h.strip() for h in raw_headers if h is not None}

            missing_columns = REQUIRED_MAPPING_COLUMNS - headers
            if missing_columns:
                found_text = ", ".join(raw_headers) if raw_headers else "(no header row found)"
                missing_text = ", ".join(sorted(missing_columns))

                raise BillCustomerMapperError(
                    "Incorrect mapping file.\n\n"
                    "Cause: One or more required columns are missing from the CSV header.\n\n"
                    f"Missing column(s): {missing_text}\n"
                    f"Columns found: {found_text}\n\n"
                    "Required columns:\n"
                    "• CustomerName\n"
                    "• CustomerId\n"
                    "• RRID\n"
                    "• ParentCustomerId\n\n"
                    "Column names must match exactly, including spelling."
                )

            # Read ALL customer rows first.
            # A parent row may have blank RRID but still needs to be available
            # as a ParentCustomerId -> CustomerId lookup target.
            all_customers: List[CustomerMapping] = []

            for row_number, row in enumerate(reader, start=2):
                name = (row.get("CustomerName") or "").strip()
                customer_id = (row.get("CustomerId") or "").strip()
                rrid = (row.get("RRID") or "").strip()
                parent_customer_id = (row.get("ParentCustomerId") or "").strip()

                if not name and not customer_id and not rrid and not parent_customer_id:
                    continue

                if not customer_id:
                    if not rrid:
                        # Cannot be used for RRID matching or as a parent target.
                        continue

                    raise BillCustomerMapperError(
                        "Incorrect mapping file.\n\n"
                        f"Cause: Row {row_number} has an RRID but CustomerId is blank.\n\n"
                        f"Row values read:\n"
                        f"• CustomerName: {name or '[blank]'}\n"
                        f"• CustomerId: [blank]\n"
                        f"• RRID: {rrid}\n"
                        f"• ParentCustomerId: {parent_customer_id or '[blank]'}"
                    )

                if not name:
                    raise BillCustomerMapperError(
                        "Incorrect mapping file.\n\n"
                        f"Cause: Row {row_number} has CustomerId '{customer_id}' "
                        "but CustomerName is blank.\n\n"
                        "CustomerName is required for any row that can be used "
                        "as a QBO customer mapping or parent lookup target."
                    )

                all_customers.append(
                    CustomerMapping(
                        customer_name=name,
                        customer_id=customer_id,
                        rrid=rrid,
                        parent_customer_id=parent_customer_id,
                        csv_row_number=row_number,
                    )
                )

            if not all_customers:
                raise BillCustomerMapperError(
                    "Incorrect mapping file.\n\n"
                    "Cause: No usable customer rows were found in the CSV."
                )

            # CustomerId -> customer lookup from the full CSV.
            customer_by_id: Dict[str, CustomerMapping] = {}

            for customer in all_customers:
                existing = customer_by_id.get(customer.customer_id)

                if existing is None:
                    customer_by_id[customer.customer_id] = customer
                    continue

                if existing.customer_name != customer.customer_name:
                    raise BillCustomerMapperError(
                        "Incorrect mapping file.\n\n"
                        "Cause: The same CustomerId appears with different CustomerName values.\n\n"
                        f"CustomerId: {customer.customer_id}\n"
                        f"Row {existing.csv_row_number}: {existing.customer_name}\n"
                        f"Row {customer.csv_row_number}: {customer.customer_name}\n\n"
                        "CustomerId must identify one customer consistently."
                    )

            # RRID -> unique direct customer rows.
            rrid_candidates: Dict[str, List[CustomerMapping]] = {}

            for customer in all_customers:
                if not customer.rrid:
                    # Blank RRID is ignored for direct matching.
                    continue

                key = _normalize_rrid(customer.rrid)
                candidates = rrid_candidates.setdefault(key, [])

                if not any(c.customer_id == customer.customer_id for c in candidates):
                    candidates.append(customer)

            if not rrid_candidates:
                raise BillCustomerMapperError(
                    "Incorrect mapping file.\n\n"
                    "Cause: The CSV contains customer data, but no rows with a "
                    "populated RRID were found.\n\n"
                    "Rows with blank RRID are ignored for direct RRID matching."
                )

            mappings: Dict[str, CustomerMapping] = {}

            for rrid_key, candidates in rrid_candidates.items():
                candidates = sorted(candidates, key=lambda c: c.csv_row_number)

                # Only one customer uses the RRID: map directly to that row.
                if len(candidates) == 1:
                    mappings[rrid_key] = candidates[0]
                    continue

                # Shared RRID:
                # - blank ParentCustomerId => ignore that row
                # - populated ParentCustomerId => lookup against CustomerId
                resolved_parents: Dict[str, CustomerMapping] = {}
                ignored_blank_parent_rows: List[CustomerMapping] = []
                unresolved_parent_rows: List[CustomerMapping] = []

                for candidate in candidates:
                    if not candidate.parent_customer_id:
                        ignored_blank_parent_rows.append(candidate)
                        continue

                    parent = customer_by_id.get(candidate.parent_customer_id)

                    if parent is None:
                        unresolved_parent_rows.append(candidate)
                        continue

                    resolved_parents.setdefault(parent.customer_id, parent)

                if unresolved_parent_rows:
                    details = "\n".join(
                        f"• Row {c.csv_row_number}: {c.customer_name} "
                        f"(CustomerId {c.customer_id}, "
                        f"ParentCustomerId={c.parent_customer_id})"
                        for c in unresolved_parent_rows
                    )

                    raise BillCustomerMapperError(
                        "Incorrect mapping file.\n\n"
                        "Cause: A shared RRID contains ParentCustomerId value(s) "
                        "that cannot be found in the CSV CustomerId column.\n\n"
                        f"RRID: {candidates[0].rrid}\n"
                        f"Unresolved row(s):\n{details}\n\n"
                        "For a shared RRID, every populated ParentCustomerId must "
                        "match an existing CustomerId in the mapping CSV."
                    )

                if not resolved_parents:
                    ignored_details = "\n".join(
                        f"• Row {c.csv_row_number}: {c.customer_name} "
                        f"(CustomerId {c.customer_id}, ParentCustomerId=[blank])"
                        for c in ignored_blank_parent_rows
                    )

                    raise BillCustomerMapperError(
                        "Incorrect mapping file.\n\n"
                        "Cause: More than one customer shares this RRID, but no "
                        "usable ParentCustomerId was found.\n\n"
                        f"RRID: {candidates[0].rrid}\n"
                        f"Rows with blank ParentCustomerId were ignored:\n"
                        f"{ignored_details or '(none)'}\n\n"
                        "At least one row sharing the RRID must have a populated "
                        "ParentCustomerId that matches a CustomerId in the CSV."
                    )

                if len(resolved_parents) > 1:
                    parent_details = "\n".join(
                        f"• {p.customer_name} (CustomerId {p.customer_id}, "
                        f"CSV row {p.csv_row_number})"
                        for p in sorted(
                            resolved_parents.values(),
                            key=lambda p: p.csv_row_number,
                        )
                    )

                    source_details = "\n".join(
                        f"• Row {c.csv_row_number}: {c.customer_name} "
                        f"(CustomerId {c.customer_id}, "
                        f"ParentCustomerId={c.parent_customer_id or '[blank]'})"
                        for c in candidates
                    )

                    raise BillCustomerMapperError(
                        "Incorrect mapping file.\n\n"
                        "Cause: The same RRID resolves to more than one different "
                        "parent customer, so the mapping is ambiguous.\n\n"
                        f"RRID: {candidates[0].rrid}\n\n"
                        f"Rows sharing the RRID:\n{source_details}\n\n"
                        f"Parent customers resolved:\n{parent_details}\n\n"
                        "All nonblank ParentCustomerId values for one shared RRID "
                        "must resolve to the same parent customer."
                    )

                # Exactly one parent customer was resolved.
                mappings[rrid_key] = next(iter(resolved_parents.values()))

    except BillCustomerMapperError:
        raise
    except UnicodeDecodeError as exc:
        raise BillCustomerMapperError(
            "Incorrect mapping file.\n\n"
            "Cause: The CSV could not be read as UTF-8 text.\n"
            "The file may use an unsupported encoding or may not actually be a CSV.\n\n"
            "Try opening the file in Excel and saving it as:\n"
            "CSV UTF-8 (Comma delimited) (*.csv)"
        ) from exc
    except csv.Error as exc:
        raise BillCustomerMapperError(
            "Incorrect mapping file.\n\n"
            "Cause: Python could not parse the CSV structure.\n"
            f"CSV parser message: {exc}\n\n"
            "Check for malformed quotes, broken rows, or an incorrect delimiter."
        ) from exc
    except PermissionError as exc:
        raise BillCustomerMapperError(
            "Incorrect mapping file.\n\n"
            "Cause: The file could not be opened because access was denied.\n"
            f"Selected path: {csv_path}\n\n"
            "Close the file in another program if it is locked, or check your file permissions."
        ) from exc
    except OSError as exc:
        raise BillCustomerMapperError(
            "Incorrect mapping file.\n\n"
            "Cause: Windows/Python could not open or read the selected file.\n"
            f"Selected path: {csv_path}\n"
            f"System message: {exc}"
        ) from exc

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

def rrid_after_at(doc_number: str) -> Optional[str]:
    """Return the trimmed text after the last '@' in a Bill DocNumber."""
    if "@" not in doc_number:
        return None
    rrid = doc_number.rsplit("@", 1)[1].strip()
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
    plans: List[BillPlan] = []
    stats = {
        "bills_in_range": 0,
        "docnumber_without_at": 0,
        "rrid_not_mapped": 0,
        "matched_bills": 0,
    }

    for bill in bills:
        stats["bills_in_range"] += 1

        doc_number = str(bill.get("DocNumber") or "").strip()
        rrid = rrid_after_at(doc_number)

        if not rrid:
            stats["docnumber_without_at"] += 1
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
                doc_number=doc_number,
                txn_date=str(bill.get("TxnDate") or ""),
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
        "BillId", "DocNumber", "TxnDate", "RRID",
        "CustomerName", "CustomerId", "SupportedLines", "UpdatedLines",
        "Status", "Message",
    ]

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return path.resolve()



def _terminal_log(level: str, message: str) -> None:
    """
    Print a timestamped progress message to the terminal.

    Example:
        [2026-08-30 21:15:03] [UPDATED] Bill 12345 | RRID RR001 | ABC Parent (100)
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}", flush=True)


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
    except BillCustomerMapperError as exc:
        _show_warning("Mapping File Error", str(exc))
        return None

    try:
        bills = query_bills_by_date(qbo, start_date, end_date)
        plans, stats = build_plans(qbo, bills, mappings)
    except Exception as exc:
        _show_error("QBO Error", str(exc))
        raise

    _terminal_log(
        "INFO",
        f"Bill scan complete | Date range {start_date} to {end_date} | "
        f"Bills found: {stats['bills_in_range']} | "
        f"Matched: {stats['matched_bills']} | "
        f"DocNumber without @: {stats['docnumber_without_at']} | "
        f"RRID not mapped: {stats['rrid_not_mapped']}"
    )

    if not plans:
        _terminal_log("INFO", "No Bills require CustomerRef mapping.")
        _show_info(
            "QBO Bill Customer Mapping",
            "No Bills require mapping.\n\n"
            f"Bills in date range: {stats['bills_in_range']}\n"
            f"DocNumber without @: {stats['docnumber_without_at']}\n"
            f"RRID not found in mapping: {stats['rrid_not_mapped']}",
        )
        return {**stats, "updated_bills": 0, "updated_lines": 0, "failed_bills": 0}


    audit_rows: List[dict] = []
    updated_bills = 0
    updated_lines_total = 0
    failed_bills = 0
    skipped_no_supported_lines = 0
    skipped_already_correct = 0

    for index, plan in enumerate(plans, start=1):
        _terminal_log(
            "START",
            f"[{index}/{len(plans)}] "
            f"Bill {plan.doc_number or '[no DocNumber]'} "
            f"(Id {plan.bill_id}) | "
            f"RRID {plan.rrid} | "
            f"Target {plan.customer_name} ({plan.customer_id})"
        )

        audit = {
            "BillId": plan.bill_id,
            "DocNumber": plan.doc_number,
            "TxnDate": plan.txn_date,
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

            current_doc_number = str(fresh_bill.get("DocNumber") or "").strip()
            current_rrid = rrid_after_at(current_doc_number)

            if _normalize_rrid(current_rrid or "") != _normalize_rrid(plan.rrid):
                audit["Status"] = "SKIPPED"
                audit["Message"] = (
                    "DocNumber changed after the initial scan, so the Bill was not updated. "
                    f"Original DocNumber: {plan.doc_number}; "
                    f"Current DocNumber: {current_doc_number or '[blank]'}."
                )
                _terminal_log(
                    "SKIPPED",
                    f"Bill {plan.doc_number or plan.bill_id} | "
                    f"DocNumber changed before update | "
                    f"Current DocNumber: {current_doc_number or '[blank]'}"
                )
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
                _terminal_log(
                    "SKIPPED",
                    f"Bill {plan.doc_number or plan.bill_id} | "
                    "No supported expense lines found."
                )
                audit_rows.append(audit)
                continue

            if changed_lines == 0:
                skipped_already_correct += 1
                audit["Status"] = "NO CHANGE"
                audit["Message"] = "CustomerRef is already correct on all supported lines."
                _terminal_log(
                    "NO CHANGE",
                    f"Bill {plan.doc_number or plan.bill_id} | "
                    f"CustomerRef already correct | "
                    f"{plan.customer_name} ({plan.customer_id})"
                )
                audit_rows.append(audit)
                continue

            _terminal_log(
                "UPDATING",
                f"Bill {plan.doc_number or plan.bill_id} | "
                f"Changing {changed_lines} of {supported_lines} supported line(s) | "
                f"CustomerRef -> {plan.customer_name} ({plan.customer_id})"
            )

            updated_bill = update_bill(qbo, fresh_bill)
            updated_bills += 1
            updated_lines_total += changed_lines
            audit["Status"] = "UPDATED"
            audit["Message"] = (
                f"Updated successfully. New SyncToken: {updated_bill.get('SyncToken', '')}"
            )

            _terminal_log(
                "UPDATED",
                f"Bill {plan.doc_number or plan.bill_id} | "
                f"Id {plan.bill_id} | "
                f"RRID {plan.rrid} | "
                f"Customer {plan.customer_name} ({plan.customer_id}) | "
                f"Updated lines: {changed_lines} | "
                f"New SyncToken: {updated_bill.get('SyncToken', '')}"
            )

            audit_rows.append(audit)

        except Exception as exc:
            failed_bills += 1
            audit["Status"] = "FAILED"
            audit["Message"] = str(exc).replace("\n", " ")

            _terminal_log(
                "FAILED",
                f"Bill {plan.doc_number or plan.bill_id} | "
                f"Id {plan.bill_id} | "
                f"RRID {plan.rrid} | "
                f"Error: {str(exc).replace(chr(10), ' ')}"
            )

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

    _terminal_log(
        "COMPLETE",
        f"Updated Bills: {updated_bills} | "
        f"Updated Lines: {updated_lines_total} | "
        f"Already correct: {skipped_already_correct} | "
        f"No supported lines: {skipped_no_supported_lines} | "
        f"Failed Bills: {failed_bills} | "
        f"Audit log: {audit_path}"
    )

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
