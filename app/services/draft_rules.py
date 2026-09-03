"""House rules enforced as validators, not merely as prompt instructions.

Phase 2 brief section 6. Every rule here is something that has had to be
caught by hand in the last fortnight, and a prompt that usually holds is
not good enough on a client-facing document. So these run AFTER generation
and a violation BLOCKS the draft rather than surfacing it with a warning
that somebody has to notice.

The distinction that matters:

- BLOCK is for a rule where being wrong is expensive or unsafe: disclosing
  the RSA procedure, inventing how many people a platter feeds, quoting a
  balance nobody asked for, promising setup access. A blocked draft costs
  a human ten minutes, which is what happens today anyway.
- WARN is for a rule where the draft is still safe to send but weaker,
  such as omitting the gluten free kitchen. Blocking a draft for missing a
  selling point would be its own kind of wrong.

Fail closed: validate() treats an unexpected error as a block, never as a
pass.
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

BLOCK = "block"
WARN = "warn"

# Rule codes, stable for measurement.
EM_DASH = "em_dash"
SIGNATURE = "signature"
AMOUNT_OWING = "amount_owing"
BEVERAGE_PACKAGE = "beverage_package"
PLATTER_SERVES = "platter_serves"
RSA_PROCEDURE = "rsa_procedure"
PROMISED_ACCESS = "promised_access"
WALKTHROUGH_HOURS = "walkthrough_hours"
GLUTEN_FREE = "gluten_free"
RULES_ERROR = "rules_error"

REQUIRED_SIGNATURE_NAME = "Aaron"
REQUIRED_SIGNATURE_VENUE = "Meantime Hamilton"
REQUIRED_SIGNATURE_EMAIL = "meantimehamilton@gmail.com"

# Walkthroughs are Wednesday to Sunday, 3-5pm. A client once arrived on a
# closed Monday because the closure was left out, so a draft that offers a
# walkthrough must carry the days.
_WALKTHROUGH_MENTION = re.compile(r"walk\s*-?\s*through|walkthrough|come\s+(?:in\s+)?(?:and\s+)?(?:have\s+a\s+)?look|site\s+visit|show\s+you\s+(?:a)?round")
_WALKTHROUGH_DAYS = re.compile(r"wednesday", re.IGNORECASE)
_WALKTHROUGH_CLOSED = re.compile(r"closed\s+(?:on\s+)?monday|monday\s+and\s+tuesday|except\s+monday", re.IGNORECASE)

# "No totals, balances or amounts owing unless the client asked for that
# figure." Quoting a per-head guide is fine; stating what they owe is not.
_AMOUNT_OWING = re.compile(
    r"(?:total|balance|amount|outstanding|owing|due)[^.\n]{0,40}\$\s?[\d,]+|"
    r"\$\s?[\d,]+[^.\n]{0,30}(?:owing|outstanding|due|balance|in total|total)",
    re.IGNORECASE,
)

_BEVERAGE_PACKAGE = re.compile(r"(?:beverage|drinks?|bar)\s+package|package\s+(?:of\s+)?drinks", re.IGNORECASE)

# Never state a platter feeds a fixed number of people.
_PLATTER_SERVES = re.compile(
    r"(?:platter|platters)[^.\n]{0,60}?(?:feeds?|serves?|caters?\s+for)\s+(?:about\s+|around\s+|approximately\s+)?\d+|"
    r"(?:feeds?|serves?)\s+(?:about\s+|around\s+|approximately\s+)?\d+\s*(?:people|guests|pax)[^.\n]{0,40}platter",
    re.IGNORECASE,
)

# The RSA / under-18 checking procedure is never disclosed in writing.
_RSA_PROCEDURE = re.compile(
    r"wristband|arm\s*band|stamp(?:ing|ed|s)?\s+(?:the|their)?\s*hands?|"
    r"we\s+(?:will\s+)?(?:check|scan)\s+(?:their\s+)?id|id\s+(?:check|scan)(?:ing)?\s+(?:at\s+)?(?:the\s+)?door|"
    r"rsa\s+procedure|marked?\s+as\s+(?:a\s+)?minor",
    re.IGNORECASE,
)

# Setup access and vendor bump-in are REQUESTS, never confirmations.
_PROMISED_ACCESS = re.compile(
    r"(?:you\s+(?:can|will\s+be\s+able\s+to)|we(?:'ll| will)\s+have\s+(?:the\s+)?(?:room|space)\s+ready|"
    r"(?:is|are)\s+confirmed|we(?:'ve| have)\s+confirmed|guaranteed?)"
    r"[^.\n]{0,50}(?:set\s*-?\s*up|setup|bump\s*-?\s*in|access\s+from|early\s+access)|"
    r"(?:set\s*-?\s*up|setup|bump\s*-?\s*in|access)[^.\n]{0,40}(?:is\s+confirmed|confirmed\s+for|guaranteed)",
    re.IGNORECASE,
)

_GLUTEN_FREE = re.compile(r"gluten\s*-?\s*free", re.IGNORECASE)


@dataclass
class RuleViolation:
    code: str
    severity: str
    message: str
    excerpt: str = ""


@dataclass
class RuleResult:
    violations: list[RuleViolation] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(v.severity == BLOCK for v in self.violations)

    @property
    def blocks(self) -> list[RuleViolation]:
        return [v for v in self.violations if v.severity == BLOCK]

    @property
    def warnings(self) -> list[RuleViolation]:
        return [v for v in self.violations if v.severity == WARN]

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.violations]


def _excerpt(match: re.Match) -> str:
    return match.group(0).strip()[:120]


def validate(draft: str, *, client_asked_for_figures: bool = False) -> RuleResult:
    """Check a generated draft against the house rules.

    client_asked_for_figures relaxes exactly one rule: a client who asked
    what they owe may be told. It is passed in by the caller from the
    enquiry, never inferred from the draft itself -- otherwise the draft
    could licence its own violation.
    """
    try:
        return _validate(draft, client_asked_for_figures=client_asked_for_figures)
    except Exception:  # noqa: BLE001 -- fail closed
        logger.exception("Draft rule validation failed")
        return RuleResult(
            violations=[
                RuleViolation(RULES_ERROR, BLOCK, "The house rules could not be checked, so the draft is withheld.")
            ]
        )


def _validate(draft: str, *, client_asked_for_figures: bool) -> RuleResult:
    result = RuleResult()
    text = draft or ""

    if "—" in text:
        result.violations.append(
            RuleViolation(EM_DASH, BLOCK, "Contains an em dash. Use commas, full stops or brackets.")
        )

    lowered = text.lower()
    missing_signature = [
        label
        for label, needle in (
            (REQUIRED_SIGNATURE_NAME, REQUIRED_SIGNATURE_NAME.lower()),
            (REQUIRED_SIGNATURE_VENUE, REQUIRED_SIGNATURE_VENUE.lower()),
            (REQUIRED_SIGNATURE_EMAIL, REQUIRED_SIGNATURE_EMAIL.lower()),
        )
        if needle not in lowered
    ]
    if missing_signature:
        result.violations.append(
            RuleViolation(
                SIGNATURE, BLOCK,
                f"Sign-off is missing {', '.join(missing_signature)}.",
            )
        )

    if not client_asked_for_figures:
        match = _AMOUNT_OWING.search(text)
        if match:
            result.violations.append(
                RuleViolation(
                    AMOUNT_OWING, BLOCK,
                    "States a total, balance or amount owing that the client did not ask for.",
                    _excerpt(match),
                )
            )

    match = _BEVERAGE_PACKAGE.search(text)
    if match:
        result.violations.append(
            RuleViolation(
                BEVERAGE_PACKAGE, BLOCK,
                "Mentions a beverage package. The answer is the bar tab, guide $25 per person.",
                _excerpt(match),
            )
        )

    match = _PLATTER_SERVES.search(text)
    if match:
        result.violations.append(
            RuleViolation(
                PLATTER_SERVES, BLOCK,
                "States how many people a platter feeds. Nothing records that. It is roughly 25 pieces, "
                "four entree-sized serves.",
                _excerpt(match),
            )
        )

    match = _RSA_PROCEDURE.search(text)
    if match:
        result.violations.append(
            RuleViolation(
                RSA_PROCEDURE, BLOCK,
                "Describes the RSA or under-18 checking procedure. That is never put in writing.",
                _excerpt(match),
            )
        )

    match = _PROMISED_ACCESS.search(text)
    if match:
        result.violations.append(
            RuleViolation(
                PROMISED_ACCESS, BLOCK,
                "Promises setup access or vendor bump-in. Both are requests, never confirmations.",
                _excerpt(match),
            )
        )

    if _WALKTHROUGH_MENTION.search(text) and not (
        _WALKTHROUGH_DAYS.search(text) and _WALKTHROUGH_CLOSED.search(text)
    ):
        result.violations.append(
            RuleViolation(
                WALKTHROUGH_HOURS, BLOCK,
                "Offers a walkthrough without the days. It must say Wednesday through Sunday, 3 to 5pm, "
                "and that we are closed Monday and Tuesday.",
            )
        )

    if not _GLUTEN_FREE.search(text):
        result.violations.append(
            RuleViolation(
                GLUTEN_FREE, WARN,
                "Does not mention the 100% gluten free kitchen, which is a genuine differentiator.",
            )
        )

    return result
