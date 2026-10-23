"""The Mandate agent boundary.

The language model reads untrusted documents and returns JSON.  It never sets a
card limit, never holds credentials and never moves money.  ``DemoAgent`` is a
deterministic stand-in used offline; ``OpenAICompatibleAgent`` is the live LLM
path and is validated by the application before use.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Protocol

from .mandate_policy_engine import DECISIONS
from .money import MoneyError, parse_money

__all__ = [
    "AgentError",
    "AgentOutputRejected",
    "Agent",
    "DemoAgent",
    "OpenAICompatibleAgent",
    "build_envelope",
    "validate_agent_output",
    "scan_document",
    "load_system_prompt",
    "PROJECT_ROOT",
    "SYSTEM_PROMPT_PATH",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "prompts" / "mandate_system_prompt.txt"

_REQUIRED_OUTPUT = (
    "recommendation",
    "extracted_terms",
    "discrepancies",
    "security_flags",
    "human_action_required",
)

_FORBIDDEN_OUTPUT = {
    "card_controls",
    "controls",
    "card_limits",
    "transaction_limits",
    "passport",
    "approval_status",
}

_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"(system|developer)\s+prompt", re.I),
    re.compile(r"you\s+are\s+now\b", re.I),
    re.compile(r"\b(api|secret|private)\s+key\b", re.I),
    re.compile(r"\bexport\b.*\b(key|credential|token)", re.I),
    re.compile(r"\bset\b.*\b(card\s+)?(limit|cap)\b", re.I),
    re.compile(r"\bapprove\b.*\b(immediately|without approval|now)\b", re.I),
    re.compile(r"\b(transfer|wire|send)\b.*\b(funds|money|usd|dollars)\b", re.I),
    re.compile(r"do\s+not\s+(tell|inform|notify)\b", re.I),
)


class AgentError(RuntimeError):
    """The agent could not produce usable output."""


class AgentOutputRejected(AgentError):
    """The agent returned fields it is not allowed to control."""


class Agent(Protocol):
    def analyze(self, envelope: Mapping[str, Any]) -> dict[str, Any]: ...


def load_system_prompt(path: Path | str = SYSTEM_PROMPT_PATH) -> str:
    prompt_path = Path(path)
    if not prompt_path.is_file():
        raise AgentError(f"system prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def scan_document(text: str, source: str = "doc") -> list[dict[str, Any]]:
    """Return prompt-injection flags.  Flagged text is data, never instructions."""

    flags: list[dict[str, Any]] = []
    for number, line in enumerate(str(text).splitlines(), start=1):
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(line):
                flags.append(
                    {
                        "type": "PROMPT_INJECTION_ATTEMPT",
                        "source": source,
                        "line": number,
                        "excerpt": line.strip()[:220],
                        "disposition": "IGNORED_AS_DATA",
                    }
                )
                break
    return flags


def build_envelope(
    *,
    request_id: str,
    trigger: str,
    trusted_context: Mapping[str, Any],
    policy_result: Mapping[str, Any],
    passport: Mapping[str, Any] | None,
    passport_status: str,
    documents: list[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "trigger": trigger,
        "trusted_context": dict(trusted_context),
        "policy_result": dict(policy_result),
        "passport": dict(passport) if passport else None,
        "passport_status": passport_status,
        "untrusted_documents": [dict(document) for document in documents],
    }


def validate_agent_output(data: Any) -> dict[str, Any]:
    """Validate the model's JSON envelope.

    Extra control fields are a hard failure: if a model tries to set a card
    limit the application rejects the whole response rather than quietly
    stripping it.
    """

    if not isinstance(data, Mapping):
        raise AgentOutputRejected("agent output must be a JSON object")

    forbidden = sorted(_FORBIDDEN_OUTPUT.intersection(data.keys()))
    if forbidden:
        raise AgentOutputRejected(
            "agent output attempted to control money fields: " + ", ".join(forbidden)
        )

    missing = [key for key in _REQUIRED_OUTPUT if key not in data]
    if missing:
        raise AgentOutputRejected("agent output is missing: " + ", ".join(missing))

    recommendation = str(data["recommendation"]).strip().upper()
    if recommendation not in DECISIONS:
        raise AgentOutputRejected(
            f"recommendation must be one of {', '.join(DECISIONS)}"
        )
    if not isinstance(data["extracted_terms"], Mapping):
        raise AgentOutputRejected("extracted_terms must be an object")
    if not data["extracted_terms"]:
        raise AgentOutputRejected("extracted_terms must not be empty")
    for key in ("discrepancies", "security_flags"):
        if not isinstance(data[key], list):
            raise AgentOutputRejected(f"{key} must be an array")
    if not isinstance(data["human_action_required"], str) or not data[
        "human_action_required"
    ].strip():
        raise AgentOutputRejected("human_action_required must be a non-empty string")
    if "approver_summary" in data and not isinstance(data["approver_summary"], str):
        raise AgentOutputRejected("approver_summary must be a string")

    cleaned = dict(data)
    cleaned["recommendation"] = recommendation
    cleaned.setdefault("discrepancies", [])
    cleaned.setdefault("security_flags", [])
    cleaned.setdefault("vendor_questions", [])
    cleaned.setdefault("decision_sufficiency", [])
    return cleaned


def _extract_amount(text: str, label: str) -> str | None:
    pattern = re.compile(
        rf"{re.escape(label)}\s*:\s*USD\s*([0-9][0-9,]*(?:\.[0-9]{{1,2}})?)",
        re.I,
    )
    match = pattern.search(text)
    return match.group(1).replace(",", "") if match else None


def _extract_line(text: str, label: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(label)}\s*:\s*(.+)$", re.I | re.M)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


class DemoAgent:
    """Deterministic offline stand-in for the extraction model.

    It parses the synthetic quote fixture so the demo works without network or
    credentials.  Its output passes through the same validator as a live model.
    It never chooses the policy decision: it echoes the engine result.
    """

    mode = "demo-deterministic"

    def analyze(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        policy = envelope.get("policy_result", {})
        decision = str(policy.get("decision", ""))
        documents = envelope.get("untrusted_documents", [])
        if not documents:
            raise AgentError("envelope has no untrusted documents")
        document = documents[0]
        text = str(document.get("text", ""))
        source = str(document.get("id", "doc-unknown"))

        vendor = _extract_line(text, "Vendor")
        product = _extract_line(text, "Product")
        monthly = _extract_amount(text, "Monthly")
        annual = _extract_amount(text, "Annual")
        mcc = _extract_line(text, "MCC")
        renewal = _extract_line(text, "Renewal date")
        deadline = _extract_line(text, "Cancellation deadline")
        refunds = _extract_line(text, "Refunds")

        missing = [
            name
            for name, value in (
                ("vendor", vendor),
                ("monthly price", monthly),
                ("annual price", annual),
                ("MCC", mcc),
            )
            if not value
        ]
        if missing:
            raise AgentError("quote is missing required fields: " + ", ".join(missing))

        assert vendor is not None and monthly is not None
        assert annual is not None and mcc is not None

        def field(value: Any, *, currency: str | None = None) -> dict[str, Any]:
            result: dict[str, Any] = {
                "value": value,
                "source": source,
                "confidence": "HIGH",
            }
            if currency:
                result["currency"] = currency
            return result

        extracted = {
            "vendor": field(vendor),
            "product": field(product or vendor),
            "monthly_price": field(monthly, currency="USD"),
            "annual_price": field(annual, currency="USD"),
            "billing_cadence": field("MONTHLY"),
            "merchant_category": field(mcc),
            "total_contract_value": field(monthly, currency="USD"),
            "refund_clause": field(refunds or "Not stated in the document."),
            "renewal_date": field(renewal or "NOT_STATED"),
            "cancellation_deadline": field(deadline or "NOT_STATED"),
        }

        discrepancies = self._compare_with_request(
            envelope.get("trusted_context", {}).get("purchase_request", {}),
            vendor,
            monthly,
            annual,
            mcc,
        )
        security_flags = scan_document(text, source)
        currency = "USD"
        if decision == "PROPOSE_ANNUAL":
            action = (
                f"Review and approve the ANNUAL plan terms ({annual} USD, {currency} "
                f"only, MCC {mcc}, {vendor}) so the application can issue a sealed "
                "Decision Passport and derive virtual card controls."
            )
        elif decision == "PROPOSE_MONTHLY":
            action = (
                f"Review and approve the MONTHLY plan terms ({monthly} USD per month, "
                f"{currency} only, MCC {mcc}, {vendor}) so the application can issue a "
                "sealed Decision Passport and derive virtual card controls."
            )
        else:
            action = (
                "Do not approve either plan. The confirmed-cash forecast breaches the "
                "reserve floor; escalate to a human for a funding or timing decision."
            )

        summary = (
            f"Sandbox prototype recommendation using synthetic demo data. "
            f"{policy.get('reason', '')} This is an extraction/explanation aid only; "
            "the policy engine made the money decision and a human must approve the "
            "exact terms before any card is created."
        )

        output = {
            "request_id": envelope.get("request_id"),
            "trigger": envelope.get("trigger", "initial"),
            "recommendation": decision,
            "policy_decision": decision,
            "policy_reason": policy.get("reason", ""),
            "recalculation_trigger": envelope.get("trigger", "initial"),
            "extracted_terms": extracted,
            "passport_status": envelope.get("passport_status", "NOT_PRESENT"),
            "discrepancies": discrepancies,
            "decision_sufficiency": [
                {
                    "criterion": "price_and_currency",
                    "status": "DETERMINATE",
                    "evidence": (
                        f"{source} states {monthly} USD monthly and {annual} USD annual."
                    ),
                },
                {
                    "criterion": "billing_cadence",
                    "status": "DETERMINATE",
                    "evidence": f"{source} offers explicit MONTHLY and ANNUAL options.",
                },
                {
                    "criterion": "merchant_category",
                    "status": "DETERMINATE",
                    "evidence": f"{source} lists MCC {mcc}.",
                },
                {
                    "criterion": "vendor_identity",
                    "status": "DETERMINATE",
                    "evidence": f"{source} identifies vendor {vendor}.",
                },
                {
                    "criterion": "renewal_date",
                    "status": "DETERMINATE" if renewal else "INDETERMINATE",
                    "evidence": f"{source} says renewal date is {renewal or 'not stated'}.",
                },
                {
                    "criterion": "refund_or_cancellation_terms",
                    "status": "DETERMINATE" if refunds else "INDETERMINATE",
                    "evidence": f"{source} says: {refunds or 'not stated'}.",
                },
            ],
            "vendor_questions": [],
            "security_flags": security_flags,
            "reconsider_on": renewal,
            "human_action_required": action,
            "approver_summary": summary,
            "agent_mode": self.mode,
        }
        return validate_agent_output(output)

    @staticmethod
    def _compare_with_request(
        purchase_request: Mapping[str, Any],
        vendor: str,
        monthly: str,
        annual: str,
        mcc: str,
    ) -> list[dict[str, Any]]:
        discrepancies: list[dict[str, Any]] = []
        if not purchase_request:
            return discrepancies
        comparisons = (
            ("vendor", vendor, purchase_request.get("vendor")),
            ("monthly_price", monthly, purchase_request.get("monthly_price")),
            ("annual_price", annual, purchase_request.get("annual_price")),
            ("merchant_category", mcc, purchase_request.get("merchant_category")),
        )
        for field_name, extracted, trusted in comparisons:
            if trusted is None:
                continue
            if str(trusted).strip() != str(extracted).strip():
                discrepancies.append(
                    {
                        "field": field_name,
                        "document_value": str(extracted),
                        "trusted_value": str(trusted),
                        "action": "REQUIRES_HUMAN_REVIEW",
                    }
                )
        return discrepancies


class OpenAICompatibleAgent:
    """Minimal OpenAI-compatible chat client for the live extraction path."""

    mode = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        prompt_path: Path | str = SYSTEM_PROMPT_PATH,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("MANDATE_LLM_BASE_URL")
            or "http://127.0.0.1:11434/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("MANDATE_LLM_MODEL", "qwen2.5-coder:7b")
        self.api_key = api_key if api_key is not None else os.environ.get(
            "MANDATE_LLM_API_KEY", ""
        )
        self.timeout = timeout
        self.system_prompt = load_system_prompt(prompt_path)

    def analyze(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(envelope, ensure_ascii=False, indent=2),
                },
            ],
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AgentError(f"LLM request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"LLM request failed: {exc.reason}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError(f"unexpected LLM response: {payload!r}") from exc

        if content.strip().startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content.strip())
            content = re.sub(r"\s*```$", "", content)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AgentError(f"LLM did not return valid JSON: {exc}") from exc
        parsed["agent_mode"] = self.mode
        return validate_agent_output(parsed)


def _parse_extracted_money(value: Any, field_name: str) -> str:
    if isinstance(value, Mapping):
        value = value.get("value")
    if value is None:
        raise AgentError(f"extracted {field_name} is missing")
    try:
        parse_money(value, field=field_name)
    except MoneyError as exc:
        raise AgentError(str(exc)) from exc
    return str(value)


def terms_from_agent_output(agent_output: Mapping[str, Any]) -> dict[str, Any]:
    """Build a structured purchase request from extracted terms.

    Kept separate from the passport: these values still need human approval.
    """

    terms = agent_output.get("extracted_terms", {})
    if not isinstance(terms, Mapping):
        raise AgentError("extracted_terms is not an object")
    vendor = terms.get("vendor", {})
    mcc = terms.get("merchant_category", {})
    vendor_value = vendor.get("value") if isinstance(vendor, Mapping) else vendor
    mcc_value = mcc.get("value") if isinstance(mcc, Mapping) else mcc
    if not vendor_value or not mcc_value:
        raise AgentError("extracted vendor or merchant_category is missing")
    return {
        "vendor": str(vendor_value),
        "monthly_price": _parse_extracted_money(terms.get("monthly_price"), "monthly_price"),
        "annual_price": _parse_extracted_money(terms.get("annual_price"), "annual_price"),
        "currency": "USD",
        "merchant_category": str(mcc_value),
        "renewal_date": (
            terms.get("renewal_date", {}).get("value")
            if isinstance(terms.get("renewal_date"), Mapping)
            else None
        ),
        "as_of": date.today().isoformat(),
    }
