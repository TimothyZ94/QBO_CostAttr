import csv
from pathlib import Path


def customer_to_row(customer):
    parent_ref = customer.get("ParentRef") or {}

    return {
        "CustomerName": customer.get("DisplayName", ""),
        "CustomerId": customer.get("Id", ""),
        "FullyQualifiedName": customer.get("FullyQualifiedName", ""),
        "CompanyName": customer.get("CompanyName", ""),
        "Active": customer.get("Active", ""),
        "IsSubCustomer": customer.get("SubCustomer", ""),
        "ParentCustomerId": parent_ref.get("value", ""),
    }


def export_customers_csv(customers, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = [customer_to_row(customer) for customer in customers]
    rows.sort(key=lambda r: (str(r["CustomerName"]).casefold(), str(r["CustomerId"])))

    fieldnames = [
        "CustomerName",
        "CustomerId",
        "FullyQualifiedName",
        "CompanyName",
        "Active",
        "IsSubCustomer",
        "ParentCustomerId",
    ]

    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return output_path.resolve()
