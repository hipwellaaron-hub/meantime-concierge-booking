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
ALLERGEN_CLAIM = "allergen_claim"
RULES_ERROR = "rules_error"

# The sign-off and the venue-specific figures come from the venue profile
# (app.services.venue_profile), never from literals here -- see
# validate(profile=...). These module constants remain as Hamilton's
# values for anything that imported them.
from app.services import venue_profile as _venue_profile  # noqa: E402

REQUIRED_SIGNATURE_NAME = _venue_profile.default().contact_name
REQUIRED_SIGNATURE_VENUE = _venue_profile.default().trading_name
REQUIRED_SIGNATURE_EMAIL = _venue_profile.default().contact_email

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

# The kitchen is 100% gluten free. It is NOT nut free, and no item is
# certified safe for any other allergy. A wrong claim here is a safety
# issue rather than an embarrassment, and it has already gone wrong twice
# in a fortnight in both directions: an item flagged as a nut risk that
# is not one, and a client declining the vegan platter believing two
# others were vegan friendly when none were. Two vegan guests would have
# arrived to nothing.
#
# Deliberately matches the CLAIM, not the topic: "we are not a nut free
# kitchen" must pass, because saying so is exactly right.
_ALLERGEN_CLAIM = re.compile(
    r"(?<!not\s)(?<!isn't\s)(?<!is\snot\s)"
    r"(?:nut|peanut|allergen|dairy|lactose|egg|soy|shellfish|sesame)\s*-?\s*free\b|"
    r"\b(?:safe|suitable|fine|ok(?:ay)?)\s+for\s+(?:\w+\s+){0,2}(?:allerg|anaphyla|coeliac|celiac)|"
    r"\ballergen\s+free\b|\bfree\s+of\s+(?:all\s+)?(?:nuts|allergens|dairy)\b|"
    r"\b(?:no|zero)\s+(?:risk\s+of\s+)?cross\s*-?\s*contamination\b|"
    r"\bguarantee(?:d)?\s+(?:\w+\s+){0,3}(?:allerg|nut\s*-?\s*free)",
    re.IGNORECASE,
)

# Phrasings that correctly DENY a claim. Checked first, so an honest
# sentence is never blocked for containing the words.
_ALLERGEN_DENIAL = re.compile(
    r"(?:not|isn't|is\s+not|aren't|are\s+not|cannot\s+guarantee|can't\s+guarantee|no\s+guarantee)"
    r"[^.\n]{0,40}(?:nut|peanut|allergen|dairy)\s*-?\s*free|"
    r"(?:nut|peanut|allergen)\s*-?\s*free[^.\n]{0,30}(?:we\s+are\s+not|is\s+not|cannot|can't|not\s+a)",
    re.IGNORECASE,
)


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


def validate(
    draft: str, *, client_asked_for_figures: bool = False, profile: "_venue_profile.VenueProfile | None" = None
) -> RuleResult:
    """Check a generated draft against the house rules.

    client_asked_for_figures relaxes exactly one rule: a client who asked
    what they owe may be told. It is passed in by the caller from the
    enquiry, never inferred from the draft itself -- otherwise the draft
    could licence its own violation.
    """
    try:
        return _validate(draft, client_asked_for_figures=client_asked_for_figures, profile=profile)
    except Exception:  # noqa: BLE001 -- fail closed
        logger.exception("Draft rule validation failed")
        return RuleResult(
            violations=[
                RuleViolation(RULES_ERROR, BLOCK, "The house rules could not be checked, so the draft is withheld.")
            ]
        )


def _validate(draft: str, *, client_asked_for_figures: bool, profile=None) -> RuleResult:
    result = RuleResult()
    text = draft or ""

    if "—" in text:
        result.violations.append(
            RuleViolation(EM_DASH, BLOCK, "Contains an em dash. Use commas, full stops or brackets.")
        )

    profile = profile or _venue_profile.default()
    lowered = text.lower()
    missing_signature = [
        label for label in profile.signature_lines if label.lower() not in lowered
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
                f"Mentions a beverage package. The answer is the bar tab, guide ${profile.bar_tab_guide_per_person:.0f} per person.",
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
                f"Offers a walkthrough without the days. It must say {profile.walkthrough_text}, "
                "and that we are closed Monday and Tuesday.",
            )
        )

    claim = _ALLERGEN_CLAIM.search(text)
    if claim and not _ALLERGEN_DENIAL.search(text):
        result.violations.append(
            RuleViolation(
                ALLERGEN_CLAIM, BLOCK,
                "Claims the kitchen or an item is free of an allergen, or safe for one. The kitchen is "
                "100% gluten free and is NOT nut free, and nothing is certified for any other allergy. "
                "This is a safety claim, so it never goes out unchecked.",
                _excerpt(claim),
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
