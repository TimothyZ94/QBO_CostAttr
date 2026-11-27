"""Batch change Invoice locations for one exact QuickBooks customer.

Requires Python 3.10+, tkinter, requests, and intuit-oauth:
    python -m pip install requests intuit-oauth

The credential popups, Intuit AuthClient flow, and production redirect default
follow TimothyZ94/QBO_CostAttr. Each run authorizes anew; credentials and tokens
stay in memory. Review the invoice preview before applying updates.
"""

from __future__ import annotations

import csv
import os
import queue
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tkinter import Tk, StringVar, messagebox, simpledialog, ttk
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import requests
from intuitlib.client import AuthClient
from intuitlib.enums import Scopes


API_HOST = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
PAGE_SIZE = 500
PRODUCTION_REDIRECT_URI = "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"
SANDBOX_REDIRECT_URI = "http://localhost:8000/callback"


class QBOError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    environment: str

    @classmethod
    def validate(cls, client_id: str, client_secret: str, redirect_uri: str,
                 environment: str) -> Config:
        environment = environment.strip().lower()
        if environment not in API_HOST:
            raise QBOError("Choose sandbox or production.")
        client_id, client_secret, redirect_uri = (
            client_id.strip(), client_secret.strip(), redirect_uri.strip())
        if not (client_id and client_secret and redirect_uri):
            raise QBOError("Client ID, Client Secret, and registered redirect URI are required.")
        uri = urlsplit(redirect_uri)
        if uri.scheme not in ("http", "https") or not uri.hostname or uri.query or uri.fragment:
            raise QBOError("Enter the exact registered HTTP(S) redirect URI without query or fragment.")
        try:
            _port = uri.port
        except ValueError as exc:
            raise QBOError("The redirect URI has an invalid port.") from exc
        if uri.username or uri.password:
            raise QBOError("The redirect URI cannot contain a username or password.")
        if uri.scheme == "http" and uri.hostname not in ("localhost", "127.0.0.1"):
            raise QBOError("An HTTP redirect URI must use localhost. Use HTTPS for other hosts.")
        if environment == "production" and uri.scheme != "https":
            raise QBOError("Production authorization requires a registered HTTPS redirect URI.")
        return cls(client_id, client_secret, redirect_uri, environment)

    @classmethod
    def prompt(cls, root: Tk) -> Config | None:
        """Ask on every launch; never save the client secret to a file."""
        while True:
            client_id = simpledialog.askstring(
                "QuickBooks setup 1/4", "Enter your Intuit app Client ID:",
                parent=root)
            if client_id is None:
                return None
            client_secret = simpledialog.askstring(
                "QuickBooks setup 2/4", "Enter your Intuit app Client Secret:",
                show="*", parent=root)
            if client_secret is None:
                return None
            environment = simpledialog.askstring(
                "QuickBooks setup 3/4", "Type sandbox or production for the company you will connect:",
                initialvalue="production", parent=root)
            if environment is None:
                return None
            default_uri = (SANDBOX_REDIRECT_URI if environment.strip().lower() == "sandbox"
                           else PRODUCTION_REDIRECT_URI)
            redirect_uri = simpledialog.askstring(
                "QuickBooks setup 4/4", "Enter the redirect URI registered for this app.\n"
                "After browser authorization, the app will collect or ask for the final redirected URL:",
                initialvalue=default_uri, parent=root)
            if redirect_uri is None:
                return None
            try:
                return cls.validate(client_id, client_secret, redirect_uri, environment)
            except QBOError as exc:
                messagebox.showerror("QuickBooks setup", str(exc), parent=root)


def ref_value(entity: dict, field: str) -> str:
    return str((entity.get(field) or {}).get("value") or "")


def parse_callback(callback_url: str, redirect_uri: str, state: str) -> tuple[str, str]:
    actual, expected = urlsplit(callback_url.strip()), urlsplit(redirect_uri)
    if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path):
        raise QBOError("The callback URL does not match the registered redirect URI.")
    params = parse_qs(actual.query)
    if params.get("state", [None])[0] != state:
        raise QBOError("Authorization state did not match. Reconnect and try again.")
    if "error" in params:
        raise QBOError("Authorization was declined or failed: " + params["error"][0])
    code, realm = params.get("code", [""])[0], params.get("realmId", [""])[0]
    if not code or not realm:
        raise QBOError("The callback URL is missing its authorization code or company ID.")
    return code, realm


def local_callback_url(redirect_uri: str, on_ready: Callable[[], None], timeout: int = 600) -> str:
    """Listen on loopback; notify the GUI when it is safe to open the browser."""
    uri = urlsplit(redirect_uri)
    if uri.scheme != "http" or uri.hostname not in ("localhost", "127.0.0.1"):
        raise QBOError("The local callback must use http://localhost or http://127.0.0.1.")
    callback: list[str] = []
    expected_path = uri.path or "/"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path != expected_path:
                self.send_error(404)
                return
            callback.append(f"{uri.scheme}://{uri.netloc}{self.path}")
            body = b"QuickBooks connected. You may close this tab and return to the app."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    try:
        server = HTTPServer(("127.0.0.1", uri.port or 80), Handler)
    except OSError as exc:
        raise QBOError(f"Cannot listen at {redirect_uri}: {exc}") from exc
    server.timeout = 1
    try:
        on_ready()
        end = time.monotonic() + timeout
        while not callback and time.monotonic() < end:
            server.handle_request()
    finally:
        server.server_close()
    if not callback:
        raise QBOError("Authorization timed out. Click Connect again.")
    return callback[0]


class QuickBooks:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.auth_client = AuthClient(
            client_id=config.client_id,
            client_secret=config.client_secret,
            redirect_uri=config.redirect_uri,
            environment=config.environment,
        )
        self.expires_at = 0.0

    @property
    def realm_id(self) -> str:
        return str(self.auth_client.realm_id or "")

    def authorization_request(self) -> tuple[str, str]:
        """Use the same Intuit SDK flow as QBO_CostAttr's oauth_flow.py."""
        try:
            url = self.auth_client.get_authorization_url([Scopes.ACCOUNTING])
        except Exception as exc:
            raise QBOError(f"Could not prepare Intuit authorization: {exc}") from exc
        state = self.auth_client.state_token
        if not state:
            raise QBOError("Intuit did not generate an OAuth state token.")
        return url, state

    def connect(self, callback_url: str, state: str) -> None:
        code, realm_id = parse_callback(callback_url, self.config.redirect_uri, state)
        try:
            self.auth_client.get_bearer_token(code, realm_id=realm_id)
        except Exception as exc:
            raise QBOError(f"Intuit token exchange failed: {exc}") from exc
        if not self.auth_client.access_token:
            raise QBOError("Intuit did not return an access token.")
        self.expires_at = time.time() + int(self.auth_client.expires_in or 3600) - 90

    def refresh(self) -> None:
        if not self.realm_id or not self.auth_client.refresh_token:
            raise QBOError("No active connection. Click Connect / reconnect first.")
        try:
            self.auth_client.refresh()
        except Exception as exc:
            raise QBOError(f"Intuit token refresh failed: {exc}") from exc
        self.expires_at = time.time() + int(self.auth_client.expires_in or 3600) - 90

    def request(self, method: str, path: str, *, params: dict | None = None,
                payload: dict | None = None) -> dict:
        if time.time() >= self.expires_at:
            self.refresh()
        url = API_HOST[self.config.environment] + f"/v3/company/{self.realm_id}/{path}"
        for attempt in range(3):
            try:
                response = self.session.request(
                    method, url, params=params, json=payload, timeout=45,
                    headers={"Authorization": f"Bearer {self.auth_client.access_token}",
                             "Accept": "application/json",
                             "Content-Type": "application/json"},
                )
            except requests.RequestException as exc:
                raise QBOError(f"Network error: {exc}") from exc
            if response.status_code == 401 and attempt == 0:
                self.refresh()
                continue
            if response.status_code == 429 and attempt < 2:
                time.sleep(60)
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code >= 400 or data.get("Fault"):
                faults = (data.get("Fault") or {}).get("Error") or []
                details = "; ".join(
                    f"{e.get('code', '')} {e.get('Message', '')}: {e.get('Detail', '')}"
                    for e in faults
                ) or response.reason
                raise QBOError(f"QuickBooks HTTP {response.status_code}: {details}")
            return data
        raise QBOError("QuickBooks request could not be completed.")

    def query_all(self, entity: str, where: str = "") -> list[dict]:
        if entity not in ("Customer", "Department", "Invoice"):
            raise ValueError("Unsupported entity")
        result: dict[str, dict] = {}
        position = 1
        while True:
            sql = f"SELECT * FROM {entity}{' WHERE ' + where if where else ''} " \
                  f"STARTPOSITION {position} MAXRESULTS {PAGE_SIZE}"
            data = self.request("GET", "query", params={"query": sql})
            page = (data.get("QueryResponse") or {}).get(entity) or []
            for item in page:
                result[str(item["Id"])] = item
            if len(page) < PAGE_SIZE:
                break
            position += PAGE_SIZE
        return list(result.values())

    def get_invoice(self, invoice_id: str) -> dict:
        return self.request("GET", f"invoice/{invoice_id}")["Invoice"]

    def set_location(self, invoice: dict, location_id: str) -> dict:
        body = {
            "Id": str(invoice["Id"]), "SyncToken": str(invoice["SyncToken"]),
            "sparse": True, "DepartmentRef": {"value": location_id},
        }
        return self.request("POST", "invoice", payload=body)["Invoice"]


class SearchCombo(ttk.Combobox):
    """Editable combobox whose drop-down is filtered by the typed substring."""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, state="normal", **kwargs)
        self.all_values: list[str] = []
        self.bind("<KeyRelease>", self._filter)

    def replace_values(self, values: list[str]) -> None:
        self.all_values = sorted(values, key=str.casefold)
        self.configure(values=self.all_values)
        self.set("")

    def _filter(self, event) -> None:
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        needle = self.get().casefold().strip()
        self.configure(values=[v for v in self.all_values if needle in v.casefold()])


class App:
    def __init__(self, root: Tk, config: Config):
        self.root = root
        self.api = QuickBooks(config)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.stop = threading.Event()
        self.preview: list[dict] = []
        self.customers: dict[str, dict] = {}
        self.locations: dict[str, dict] = {}
        self.company = ""
        self.auth_url: str | None = None
        self.auth_ready = False
        self.manual_callback = False
        self.pending_state: str | None = None
        self.customer = StringVar()
        self.location = StringVar()
        self.status = StringVar(value=f"{config.environment.upper()}: connect or load the saved connection.")
        root.title("QuickBooks Invoice Location Batch")
        root.geometry("1000x610")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        top = ttk.Frame(frame)
        top.pack(fill="x")
        self.connect_button = ttk.Button(top, text="Connect / reconnect", command=self.connect)
        self.connect_button.pack(side="left")
        self.browser_button = ttk.Button(top, text="Open Intuit page", command=self.open_connection_page)
        self.browser_button.pack(side="left", padx=(8, 0))
        self.copy_button = ttk.Button(top, text="Copy link", command=self.copy_connection_link)
        self.copy_button.pack(side="left", padx=(8, 0))
        self.callback_button = ttk.Button(top, text="Paste redirect", command=self.paste_callback)
        self.callback_button.pack(side="left", padx=(8, 0))
        self.load_button = ttk.Button(top, text="Load customers and locations", command=self.load_lists)
        self.load_button.pack(side="left", padx=8)
        ttk.Label(top, textvariable=self.status).pack(side="left", padx=12)
        ttk.Label(frame, text="Customer (type to filter, then select a listed entry)").pack(anchor="w", pady=(12, 0))
        self.customer_combo = SearchCombo(frame, textvariable=self.customer)
        self.customer_combo.pack(fill="x")
        ttk.Label(frame, text="Location (type to filter, then select a listed entry)").pack(anchor="w", pady=(8, 0))
        self.location_combo = SearchCombo(frame, textvariable=self.location)
        self.location_combo.pack(fill="x")
        self.customer.trace_add("write", self.invalidate_preview)
        self.location.trace_add("write", self.invalidate_preview)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=10)
        self.preview_button = ttk.Button(buttons, text="Preview all matching invoices", command=self.load_preview)
        self.preview_button.pack(side="left")
        self.update_button = ttk.Button(buttons, text="Apply location to previewed invoices", command=self.apply)
        self.update_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(buttons, text="Stop after current invoice", command=self.stop.set)
        self.cancel_button.pack(side="left")
        self.columns = ("number", "date", "amount", "old", "action", "id")
        self.table = ttk.Treeview(frame, columns=self.columns, show="headings")
        for col, title, width in (
            ("number", "Invoice #", 130), ("date", "Date", 100),
            ("amount", "Total", 110), ("old", "Current location", 250),
            ("action", "Planned action", 150), ("id", "QBO ID", 90),
        ):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, stretch=col == "old")
        self.table.pack(fill="both", expand=True)
        ttk.Label(frame, text="Exact customer ID only. All invoice dates and payment statuses are included. "
                  "A CSV result log is saved beside this script.").pack(anchor="w", pady=(8, 0))
        self._buttons()
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _buttons(self) -> None:
        available = bool(self.api.realm_id)
        self.connect_button.configure(state="disabled" if self.busy else "normal")
        can_open = bool(self.auth_url and self.auth_ready)
        self.browser_button.configure(state="normal" if can_open else "disabled")
        self.copy_button.configure(state="normal" if can_open else "disabled")
        can_paste = bool(can_open and self.manual_callback and not self.busy)
        self.callback_button.configure(state="normal" if can_paste else "disabled")
        self.load_button.configure(state="normal" if available and not self.busy else "disabled")
        valid = self.customer.get() in self.customers and self.location.get() in self.locations
        self.preview_button.configure(state="normal" if valid and not self.busy else "disabled")
        self.update_button.configure(state="normal" if valid and self.preview and not self.busy else "disabled")
        self.cancel_button.configure(state="normal" if self.busy and self.preview else "disabled")

    def invalidate_preview(self, *_args) -> None:
        if self.preview:
            self.preview = []
            self.table.delete(*self.table.get_children())
            self.status.set("Selection changed. Preview again before updating.")
        self._buttons()

    def _job(self, label: str, function, done) -> None:
        self.busy = True
        self.status.set(label)
        self._buttons()

        def run() -> None:
            try:
                self.events.put(("done", done, function()))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] == "progress":
                    self.status.set(item[1])
                elif item[0] == "open_browser":
                    # Tk and Windows ShellExecute run on this GUI thread.
                    self.auth_ready = True
                    self.open_connection_page()
                elif item[0] == "error":
                    self.busy = False
                    self.auth_url = None
                    self.auth_ready = False
                    self.pending_state = None
                    self.manual_callback = False
                    self.preview = []
                    self.table.delete(*self.table.get_children())
                    self.status.set("Operation stopped; see error.")
                    messagebox.showerror("QuickBooks", item[1])
                    self._buttons()
                else:
                    self.busy = False
                    try:
                        item[1](item[2])
                    except Exception as exc:
                        messagebox.showerror("QuickBooks", str(exc))
                    self._buttons()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def open_connection_page(self) -> None:
        if not self.auth_url or not self.auth_ready:
            return
        details = ""
        try:
            opened = webbrowser.open(self.auth_url, new=2)
            if not opened and os.name == "nt":
                os.startfile(self.auth_url)
                opened = True
        except Exception as exc:
            opened = False
            details = str(exc)
        if opened:
            self.status.set("Authorize in browser; use Open/Copy if needed.")
        else:
            messagebox.showwarning("Open Intuit page",
                                   "The browser did not open. Click Copy link and paste it into your browser.\n"
                                   + details, parent=self.root)
        self._buttons()

    def copy_connection_link(self) -> None:
        if not self.auth_url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.auth_url)
        self.root.update_idletasks()
        self.status.set("Connection link copied. Paste it into your browser's address bar.")

    def paste_callback(self) -> None:
        if not self.manual_callback or not self.pending_state or self.busy:
            return
        callback = simpledialog.askstring(
            "Intuit redirect URL", "After approving access in the browser, copy its FULL final URL and paste it here.\n"
            "It must start with your registered HTTPS redirect URI.", parent=self.root,
        )
        if callback:
            state = self.pending_state
            self._job("Exchanging authorization code...",
                      lambda: (self.api.connect(callback, state), self.api.realm_id)[1],
                      self._connected)

    def connect(self) -> None:
        try:
            url, state = self.api.authorization_request()
        except QBOError as exc:
            messagebox.showerror("QuickBooks connection", str(exc), parent=self.root)
            return
        self.auth_url = url
        self.auth_ready = False
        self.manual_callback = False
        self.pending_state = state
        self._buttons()
        uri = urlsplit(self.api.config.redirect_uri)
        if uri.scheme == "http" and uri.hostname in ("localhost", "127.0.0.1"):
            self.manual_callback = False
            # Bind first, then open the browser.
            def connect_local():
                callback = local_callback_url(
                    self.api.config.redirect_uri,
                    lambda: self.events.put(("open_browser",)),
                )
                self.api.connect(callback, state)
                return self.api.realm_id
            self._job("Waiting for Intuit authorization in your browser...", connect_local, self._connected)
        else:
            self.manual_callback = True
            self.auth_ready = True
            self.open_connection_page()
            self.status.set("Approve in browser, then click Paste redirect.")
            self._buttons()

    def _connected(self, realm: str) -> None:
        self.auth_url = None
        self.auth_ready = False
        self.pending_state = None
        self.manual_callback = False
        self.preview = []
        self.customers = {}
        self.locations = {}
        self.customer_combo.replace_values([])
        self.location_combo.replace_values([])
        self.table.delete(*self.table.get_children())
        self.status.set(f"Connected to {self.api.config.environment} company ID {realm}. Loading lists...")
        self.load_lists()

    def load_lists(self) -> None:
        def fetch():
            prefs = self.api.request("GET", "preferences")["Preferences"]
            if not prefs.get("AccountingInfoPrefs", {}).get("TrackDepartments", False):
                raise QBOError("Location tracking is off in this QuickBooks company. Enable it in company settings.")
            company = self.api.request("GET", f"companyinfo/{self.api.realm_id}")["CompanyInfo"]["CompanyName"]
            customers = self.api.query_all("Customer", "Active IN (true, false)")
            locations = self.api.query_all("Department", "Active = true")
            return company, customers, locations

        self._job("Loading customers and locations...", fetch, self._lists_loaded)

    def _lists_loaded(self, result) -> None:
        company, customers, locations = result
        self.company = company
        self.customers = {
            f"{c.get('FullyQualifiedName') or c.get('DisplayName')} [ID {c['Id']}]": c
            for c in customers
        }
        self.locations = {
            f"{d.get('FullyQualifiedName') or d.get('Name')} [ID {d['Id']}]": d
            for d in locations
        }
        self.customer_combo.replace_values(list(self.customers))
        self.location_combo.replace_values(list(self.locations))
        self.status.set(f"{self.api.config.environment.upper()} · {company} · "
                        f"{len(customers)} customers, {len(locations)} active locations")

    def load_preview(self) -> None:
        customer = self.customers.get(self.customer.get())
        location = self.locations.get(self.location.get())
        if not customer or not location:
            messagebox.showerror("Selection required", "Choose entries from both dropdown lists.")
            return
        customer_id = str(customer["Id"])
        self._job("Fetching every invoice for the selected customer...",
                  lambda: self.api.query_all("Invoice", f"CustomerRef = '{customer_id}'"),
                  lambda rows: self._preview_loaded(rows, customer_id, str(location["Id"])))

    def _preview_loaded(self, rows: list[dict], customer_id: str, location_id: str) -> None:
        if customer_id != str(self.customers.get(self.customer.get(), {}).get("Id")) or \
                location_id != str(self.locations.get(self.location.get(), {}).get("Id")):
            raise QBOError("Selection changed while loading. Preview again.")
        # Guard against an unexpected query match or duplicate page entry.
        self.preview = [r for r in rows if ref_value(r, "CustomerRef") == customer_id]
        self.preview.sort(key=lambda r: (r.get("TxnDate", ""), r.get("DocNumber", ""), r["Id"]))
        self.table.delete(*self.table.get_children())
        changed = 0
        for row in self.preview:
            old_id = ref_value(row, "DepartmentRef")
            old = (row.get("DepartmentRef") or {}).get("name") or (f"ID {old_id}" if old_id else "(none)")
            action = "Already assigned" if old_id == location_id else "Change location"
            changed += old_id != location_id
            self.table.insert("", "end", values=(row.get("DocNumber", ""), row.get("TxnDate", ""),
                                                   row.get("TotalAmt", ""), old, action, row["Id"]))
        self.status.set(f"Preview: {len(self.preview)} invoices; {changed} to change, "
                        f"{len(self.preview) - changed} already assigned.")

    def apply(self) -> None:
        customer = self.customers.get(self.customer.get())
        location = self.locations.get(self.location.get())
        if not customer or not location or not self.preview:
            return
        customer_id, location_id = str(customer["Id"]), str(location["Id"])
        todo = sum(ref_value(row, "DepartmentRef") != location_id for row in self.preview)
        if not messagebox.askyesno(
            "Confirm invoice updates",
            f"Company: {self.company} ({self.api.config.environment})\n"
            f"Customer: {self.customer.get()}\nLocation: {self.location.get()}\n\n"
            f"Previewed: {len(self.preview)} invoices; planned changes: {todo}.\n"
            "This includes paid and historical invoices. Existing locations will be overwritten. Continue?",
        ):
            return
        snapshot = list(self.preview)
        self.stop.clear()
        self._job("Updating invoices...", lambda: self._update(snapshot, customer_id, location_id),
                  self._updated)

    def _update(self, rows: list[dict], customer_id: str, location_id: str):
        log_dir = Path(__file__).resolve().parent / "qbo_location_logs"
        log_dir.mkdir(exist_ok=True)
        path = log_dir / f"invoice_locations_{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}.csv"
        fieldnames = ("environment", "realm_id", "customer_id", "location_id", "invoice_id",
                      "invoice_number", "invoice_date", "old_location_id", "new_location_id",
                      "status", "error")
        tally = {"updated": 0, "already_assigned": 0, "customer_changed": 0, "failed": 0}
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for index, preview in enumerate(rows, 1):
                if self.stop.is_set():
                    break
                invoice_id = str(preview["Id"])
                entry = dict(environment=self.api.config.environment, realm_id=self.api.realm_id,
                             customer_id=customer_id, location_id=location_id, invoice_id=invoice_id,
                             invoice_number=preview.get("DocNumber", ""),
                             invoice_date=preview.get("TxnDate", ""),
                             old_location_id=ref_value(preview, "DepartmentRef"),
                             new_location_id="", status="", error="")
                try:
                    current = self.api.get_invoice(invoice_id)
                    entry["old_location_id"] = ref_value(current, "DepartmentRef")
                    if ref_value(current, "CustomerRef") != customer_id:
                        entry["status"] = "customer_changed"
                    elif entry["old_location_id"] == location_id:
                        entry["status"] = "already_assigned"
                        entry["new_location_id"] = location_id
                    else:
                        try:
                            result = self.api.set_location(current, location_id)
                            if ref_value(result, "DepartmentRef") != location_id:
                                result = self.api.get_invoice(invoice_id)
                            if ref_value(result, "DepartmentRef") != location_id:
                                raise QBOError("Update response did not confirm the requested location.")
                            entry["status"] = "updated"
                            entry["new_location_id"] = location_id
                        except Exception as exc:
                            # A server/network error may occur after the update succeeds.
                            # Read back once; never blindly repeat an ambiguous POST.
                            try:
                                check = self.api.get_invoice(invoice_id)
                                if ref_value(check, "DepartmentRef") == location_id:
                                    entry["status"] = "updated"
                                    entry["new_location_id"] = location_id
                                else:
                                    raise exc
                            except Exception:
                                raise exc
                except Exception as exc:
                    entry["status"] = "failed"
                    entry["error"] = str(exc)
                tally[entry["status"]] += 1
                writer.writerow(entry)
                file.flush()
                self.events.put(("progress", f"Processed {index}/{len(rows)}: "
                                 f"{tally['updated']} updated, {tally['failed']} failed"))
        return path, tally, len(rows), self.stop.is_set()

    def _updated(self, result) -> None:
        path, tally, total, cancelled = result
        self.preview = []  # The preview is stale after any update attempt.
        self.table.delete(*self.table.get_children())
        self.status.set("Stopped after current invoice." if cancelled else "Batch finished. Preview again to refresh.")
        messagebox.showinfo("Batch result", f"{self.company}: {total} previewed\n"
                            f"Updated: {tally['updated']}\nAlready assigned: {tally['already_assigned']}\n"
                            f"Customer changed: {tally['customer_changed']}\nFailed: {tally['failed']}\n"
                            f"Log: {path}\n\nPreview again to check remaining invoices.")

    def close(self) -> None:
        if self.busy:
            self.stop.set()
            messagebox.showinfo("Please wait", "Stopping after the current request. Close the app when it finishes.")
        else:
            self.root.destroy()


def main() -> None:
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        config = Config.prompt(root)
        if config is None:
            root.destroy()
            return
        root.attributes("-topmost", False)
        app = App(root, config)
    except Exception as exc:
        messagebox.showerror("Setup", str(exc))
        root.destroy()
        return
    # Show the main window before opening the default browser for Intuit consent.
    root.deiconify()
    root.after(250, app.connect)
    root.mainloop()


if __name__ == "__main__":
    main()