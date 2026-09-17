"""The architectural requirement, as a test.

"Core billing services must NOT directly depend on Razorpay. All Razorpay API
calls must stay inside `payment/razorpay/`." That is the kind of rule a
codebase keeps for about six months and then quietly stops keeping, so it is
checked here rather than trusted to review.

Two checks, and they catch different mistakes:

*   an **import** check, which would catch `from ..razorpay import client`
    appearing in the invoice generator during a late-night fix;
*   a **name** check, which catches the subtler version — a core service
    reading `subscription.razorpay_subscription_id` directly instead of the
    neutral accessor, which compiles, passes every other test, and quietly ties
    the billing engine to one gateway.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

PAYMENT = Path(__file__).resolve().parents[1] / "app" / "services" / "payment"

#: Everything that decides money. The adapter package and the two modules that
#: are *about* the seam are deliberately not in this list.
CORE_MODULES = [
    "app.services.payment.billing_service",
    "app.services.payment.subscription_service",
    "app.services.payment.payment_service",
    "app.services.payment.invoice.generation_service",
    "app.services.payment.proration.calculator",
    "app.services.payment.addon.billing_service",
    "app.services.payment.tax.calculator",
    "app.services.payment.metrics.billing_metrics_service",
]

CORE_FILES = [
    PAYMENT / "billing_service.py",
    PAYMENT / "subscription_service.py",
    PAYMENT / "payment_service.py",
    PAYMENT / "invoice" / "generation_service.py",
    PAYMENT / "proration" / "calculator.py",
    PAYMENT / "addon" / "billing_service.py",
    PAYMENT / "tax" / "calculator.py",
    PAYMENT / "metrics" / "billing_metrics_service.py",
]


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_no_core_billing_file_imports_the_adapter(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert "razorpay" not in name.lower(), f"{path.name} imports {name}"


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_no_core_billing_file_names_a_gateway_outside_its_prose(path: Path):
    """Docstrings and comments may say "Razorpay" — explaining the seam is the
    point of them. Code may not."""
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and "razorpay" in node.attr.lower():
            offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
        if isinstance(node, ast.Name) and "razorpay" in node.id.lower():
            offenders.append(f"{path.name}:{node.lineno} {node.id}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A string literal naming the gateway would mean a hard-coded
            # choice of one; the registry in `gateway.py` is where that lives.
            if "razorpay" in node.value.lower() and len(node.value) < 60:
                offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
    assert not offenders, "gateway named in core billing code: " + ", ".join(offenders)


def test_the_core_services_import_with_no_adapter_installed(monkeypatch):
    """The strongest version of the check: make the adapter unimportable and
    load every core module anyway.

    A deployment that removed `payment/razorpay/` entirely — because it bills
    through something else — must still be able to work out what a clinic owes.
    """

    class Blocker:
        def find_module(self, name, path=None):
            if name.startswith("app.services.payment.razorpay"):
                return self

        def load_module(self, name):  # pragma: no cover - raised, never returns
            raise ImportError(f"the adapter is not installed: {name}")

    for name in list(sys.modules):
        if name.startswith("app.services.payment.razorpay"):
            monkeypatch.delitem(sys.modules, name)

    monkeypatch.setattr(sys, "meta_path", [Blocker(), *sys.meta_path])
    for module in CORE_MODULES:
        monkeypatch.delitem(sys.modules, module, raising=False)
        assert importlib.import_module(module) is not None


def test_the_adapter_is_resolved_by_name_rather_than_imported():
    """`get_gateway` is the only door, and it opens by configuration."""
    from app.services.payment import gateway

    assert gateway.available_gateways() == ["razorpay"]
    with pytest.raises(gateway.GatewayNotConfigured):
        gateway.get_gateway("stripe")


def test_every_file_that_talks_http_lives_in_the_adapter_package():
    """`httpx` appears in exactly one place under `payment/`."""
    users = [
        path.relative_to(PAYMENT).as_posix()
        for path in PAYMENT.rglob("*.py")
        if "httpx" in path.read_text()
    ]
    assert users == ["razorpay/client.py"], users
