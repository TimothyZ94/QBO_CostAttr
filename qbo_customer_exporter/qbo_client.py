import requests


class QBOAPIError(RuntimeError):
    pass


class QBOClient:
    def __init__(self, access_token, realm_id, environment="sandbox", timeout=60):
        self.access_token = access_token
        self.realm_id = str(realm_id)
        self.environment = environment
        self.timeout = timeout

        if environment == "sandbox":
            self.base_url = "https://sandbox-quickbooks.api.intuit.com"
        elif environment in {"production", "prod"}:
            self.base_url = "https://quickbooks.api.intuit.com"
        else:
            raise ValueError("environment must be 'sandbox' or 'production'")

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _query(self, query):
        url = f"{self.base_url}/v3/company/{self.realm_id}/query"

        response = requests.get(
            url,
            headers=self.headers,
            params={"query": query},
            timeout=self.timeout,
        )

        if not response.ok:
            intuit_tid = response.headers.get("intuit_tid")
            tid_text = f" Intuit TID: {intuit_tid}" if intuit_tid else ""
            raise QBOAPIError(
                f"QBO API request failed ({response.status_code}).{tid_text}\n"
                f"{response.text}"
            )

        return response.json()

    def _get_customers_by_active_status(self, active, page_size=1000):
        """
        QBO does not support OR in its query language, so active and inactive
        customers are fetched separately when a complete list is required.
        """
        active_text = "true" if active else "false"
        start_position = 1
        all_customers = []

        while True:
            query = (
                "SELECT * FROM Customer "
                f"WHERE Active = {active_text} "
                f"STARTPOSITION {start_position} "
                f"MAXRESULTS {page_size}"
            )

            payload = self._query(query)
            query_response = payload.get("QueryResponse", {})
            page = query_response.get("Customer", []) or []

            all_customers.extend(page)

            if len(page) < page_size:
                break

            start_position += len(page)

        return all_customers

    def get_all_customers(self, page_size=1000, include_inactive=True):
        """
        Return all customer entities.

        include_inactive=True:
            fetch Active=true and Active=false separately, then combine them.
        """
        customers = self._get_customers_by_active_status(
            active=True,
            page_size=page_size,
        )

        if include_inactive:
            customers.extend(
                self._get_customers_by_active_status(
                    active=False,
                    page_size=page_size,
                )
            )

        # De-duplicate defensively by QBO Customer Id.
        deduped = {}
        for customer in customers:
            customer_id = str(customer.get("Id", ""))
            if customer_id:
                deduped[customer_id] = customer

        return list(deduped.values())
