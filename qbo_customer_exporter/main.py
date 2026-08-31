from pathlib import Path
import traceback

from config import (
    ENVIRONMENT,
    REDIRECT_URI,
    PAGE_SIZE,
    OUTPUT_CSV,
    REQUEST_TIMEOUT,
)
from credentials_ui import (
    prompt_for_credentials,
    show_success,
    show_error,
)
from oauth_flow import authorize
from qbo_client import QBOClient
from exporter import export_customers_csv
from bill_customer_mapper import run_bill_customer_mapping


def main():
    client_id, client_secret = prompt_for_credentials()

    print(f"Environment: {ENVIRONMENT}")
    print("Opening Intuit authorization in your default browser...")

    auth_client, realm_id = authorize(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=REDIRECT_URI,
        environment=ENVIRONMENT,
    )

    print(f"Connected to QBO company/realm: {realm_id}")
    print("Fetching customers...")

    qbo = QBOClient(
        access_token=auth_client.access_token,
        realm_id=realm_id,
        environment=ENVIRONMENT,
        timeout=REQUEST_TIMEOUT,
    )

    customers = qbo.get_all_customers(
        page_size=PAGE_SIZE,
        include_inactive=True,
    )

    output_path = Path(__file__).resolve().parent / "output" / OUTPUT_CSV
    saved_path = export_customers_csv(customers, output_path)

    print(f"Exported {len(customers)} customers.")
    print(f"Saved to: {saved_path}")

    show_success(
        f"Export complete.\n\n"
        f"Customers: {len(customers)}\n"
        f"Company/Realm ID: {realm_id}\n\n"
        f"Saved to:\n{saved_path}"
    )

    input("getCustomerList() completed. Press Enter to continue...")

    run_bill_customer_mapping(qbo)


if __name__ == "__main__":
    try:
        main()
        
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc()
        show_error(str(exc))
        raise
