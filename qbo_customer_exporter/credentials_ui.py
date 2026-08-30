import tkinter as tk
from tkinter import simpledialog, messagebox


def _make_root():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def prompt_for_credentials():
    """
    Prompt for Client ID and Client Secret on every run.

    The secret is masked in the popup and is kept only in memory.
    Nothing is written to disk by this module.
    """
    root = _make_root()
    try:
        client_id = simpledialog.askstring(
            "QuickBooks Online API",
            "Enter Intuit Client ID:",
            parent=root,
        )
        if client_id is None:
            raise SystemExit("Cancelled by user.")

        client_id = client_id.strip()
        if not client_id:
            messagebox.showerror("Missing Client ID", "Client ID cannot be blank.", parent=root)
            raise SystemExit(1)

        client_secret = simpledialog.askstring(
            "QuickBooks Online API",
            "Enter Intuit Client Secret:",
            show="*",
            parent=root,
        )
        if client_secret is None:
            raise SystemExit("Cancelled by user.")

        client_secret = client_secret.strip()
        if not client_secret:
            messagebox.showerror("Missing Client Secret", "Client Secret cannot be blank.", parent=root)
            raise SystemExit(1)

        return client_id, client_secret
    finally:
        root.destroy()


def prompt_for_callback_url():
    """
    Manual fallback for non-local redirect URIs.

    After Intuit redirects the browser, copy the FULL URL from the browser
    address bar and paste it here.
    """
    root = _make_root()
    try:
        callback_url = simpledialog.askstring(
            "QuickBooks OAuth Callback",
            "Authorization completed.\n\n"
            "Copy the FULL redirected URL from your browser address bar\n"
            "and paste it here:",
            parent=root,
        )
        if not callback_url:
            raise SystemExit("No callback URL supplied.")
        return callback_url.strip()
    finally:
        root.destroy()


def show_success(message):
    root = _make_root()
    try:
        messagebox.showinfo("QBO Customer Export", message, parent=root)
    finally:
        root.destroy()


def show_error(message):
    root = _make_root()
    try:
        messagebox.showerror("QBO Customer Export - Error", message, parent=root)
    finally:
        root.destroy()
