"""Tool definitions: one per Concierge /api/ai/* endpoint.

The descriptions here are not documentation, they are the instructions the
model reads when deciding whether to call something. So each says what it
returns, what it is for, and -- where it matters most -- what it does NOT
tell you. The availability description in particular spells out that a
slot with nothing confirmed is not necessarily a free slot, because
"available" and "nobody else is asking" are different facts and confusing
them is how a date gets offered to two parties.

All tools are read-only. Writes land here when the Tier 1 endpoints land
in Concierge, each with its own tool and its own description.
"""

from mcp_server.concierge import call_ai

_DATE = {"type": "string", "description": "Date as YYYY-MM-DD."}

TOOLS: list[dict] = [
    {
        "name": "pipeline",
        "description": (
            "Where every live booking and enquiry actually is, in one call. Use this for "
            "'where is everything up to', 'what needs chasing', or any question about the "
            "state of the whole book.\n\n"
            "Returns each record with a computed `stage` -- which is NOT the same as its "
            "stored status. Stage is derived from what has actually happened (documents "
            "sent and signed, deposits invoiced and paid, wizard, Event Order), so a "
            "booking that was only ever sent an agreement cannot read as signed. Stages: "
            "enquiry, replied, offered, signed_unpaid, paid_unsigned, confirmed, "
            "wizard_sent, wizard_submitted, beo_sent, finalised, archived. Later stages "
            "supersede earlier ones, so `confirmed` means both gates are met and the "
            "wizard has not gone out yet.\n\n"
            "Each record also carries `awaiting` (staff or client, from who acted last), "
            "`days_at_stage`, and `contested` -- true when another live booking or enquiry "
            "overlaps the same room and time.\n\n"
            "Two documented limits are returned in the response `notes`: `replied` never "
            "appears yet because staff reply by email and Concierge does not see that, "
            "which also makes `awaiting` over-report 'staff'; and `beo_sent` means the "
            "Event Order was issued, not that a client approved it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": (
                        "Return only this stage. Archived records are excluded unless you "
                        "ask for stage='archived' by name."
                    ),
                    "enum": [
                        "enquiry", "replied", "offered", "signed_unpaid", "paid_unsigned",
                        "confirmed", "wizard_sent", "wizard_submitted", "beo_sent",
                        "finalised", "archived",
                    ],
                },
                "awaiting": {
                    "type": "string",
                    "enum": ["staff", "client"],
                    "description": "Return only records where the ball is with this side.",
                },
            },
        },
        "_call": lambda args: call_ai("/api/ai/pipeline", {
            "stage": args.get("stage"), "awaiting": args.get("awaiting"),
        }),
    },
    {
        "name": "availability",
        "description": (
            "What is touching a room on a date -- ALWAYS call this before telling anyone a "
            "date is free or taken. Never answer an availability question from memory or "
            "from something read earlier in the conversation.\n\n"
            "Returns, per date and per space, three separate lists: `confirmed` bookings, "
            "`tentative` holds (with whether the agreement is signed, whether the deposit "
            "is paid, and when the hold expires), and `open_enquiries` -- every enquiry or "
            "offer already asking for that slot, with the contact name and guest count. "
            "This is the important part: a slot with no confirmed booking is NOT simply "
            "'available' if other parties are already enquiring about it. When "
            "`open_enquiries` or `tentative` is non-empty the slot is contested, and any "
            "reply must say so and name who, rather than describing the date as free.\n\n"
            "TIME-AWARE. A lunch and an evening in the same room on the same day do not "
            "conflict and both appear, so check the times before concluding a room is "
            "taken. Each day also returns `day_of_week`, computed from the date -- use it "
            "to check a client's 'Saturday 21 November' really is a Saturday before "
            "proceeding. Every response carries `as_of`; if more than about ten minutes "
            "pass before you act on it, call again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {**_DATE, "description": "A single date to check (YYYY-MM-DD)."},
                "from": {**_DATE, "description": "Start of a date range. Use with 'to'."},
                "to": {**_DATE, "description": "End of a date range (max 120 days)."},
                "space": {
                    "type": "string",
                    "description": "Limit to one room: loft, mezzanine or lounge. Omit for all rooms.",
                },
            },
        },
        "_call": lambda args: call_ai("/api/ai/availability", {
            "date": args.get("date"), "from": args.get("from"),
            "to": args.get("to"), "space": args.get("space"),
        }),
    },
    {
        "name": "bookings",
        "description": (
            "Look up bookings in full detail, by reference, contact email, or date. Use "
            "this when you need the specifics of a particular booking rather than an "
            "overview.\n\n"
            "Returns event name and type, date, times, room and any linked rooms, guest "
            "counts, the agreed minimum and the room's default, agreement status and "
            "signing time, deposit status, amount and payment time, wizard and Event Order "
            "state, cake permission, and any open flags.\n\n"
            "The date form is also the independent second source for checking "
            "availability: if a claim matters, query `availability` for the slot and this "
            "for the same date and room, and confirm they agree. If they disagree, do not "
            "make the claim -- say the sources disagree."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Booking reference, e.g. HAM-20261128-IO5O3."},
                "email": {"type": "string", "description": "Contact email address."},
                "date": {**_DATE, "description": "All bookings on this date."},
                "space": {
                    "type": "string",
                    "description": "With 'date', limit to one room: loft, mezzanine or lounge.",
                },
            },
        },
        "_call": lambda args: call_ai("/api/ai/bookings", {
            "ref": args.get("ref"), "email": args.get("email"),
            "date": args.get("date"), "space": args.get("space"),
        }),
    },
    {
        "name": "catalogue",
        "description": (
            "Menu items with the price that actually applies. Call this before quoting any "
            "price -- never quote from memory, and never quote a price for something that "
            "is not in this list.\n\n"
            "Returns each item's name, category, price, active flag, dietary markers and "
            "peanut flag. Pass `as_of` with a booking's pricing_locked_at date to get the "
            "prices that apply to that booking: pizzas booked before the May 2026 cutover "
            "hold their legacy price.\n\n"
            "Two things this data does not contain, and you must not invent: serving sizes "
            "(no item records how many people it feeds, so never say a platter serves N), "
            "and confirmed dietary information where `dietary_markers` is null -- null "
            "means unconfirmed, which is different from an empty list meaning confirmed to "
            "carry no marker. A null `price` likewise means the legacy price was never "
            "defined for that item; report it as unknown rather than substituting today's."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "as_of": {
                    **_DATE,
                    "description": (
                        "Resolve prices as they apply to a booking whose pricing was locked "
                        "on this date. Omit for today's prices."
                    ),
                },
            },
        },
        "_call": lambda args: call_ai("/api/ai/catalogue", {"as_of": args.get("as_of")}),
    },
    {
        "name": "booking_documents",
        "description": (
            "Whether a booking's agreement and Event Order exist and what state they are "
            "in: type, version, status (draft, sent, viewed, signed), when it was viewed "
            "or signed, who signed, and whether it is a legacy uploaded PDF.\n\n"
            "Deliberately does NOT return document content or a PDF -- checking that a "
            "contract exists and is signed never requires reading it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "The booking's UUID (from `bookings`)."},
            },
            "required": ["booking_id"],
        },
        "_call": lambda args: call_ai(f"/api/ai/bookings/{args['booking_id']}/documents"),
    },
    {
        "name": "booking_invoices",
        "description": (
            "A booking's invoices: type (deposit or final), status, total, due date, when "
            "paid, and the payments recorded against each one with amount, method and "
            "payer.\n\n"
            "Contains no card details, bank details or payment-processor identifiers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "The booking's UUID (from `bookings`)."},
            },
            "required": ["booking_id"],
        },
        "_call": lambda args: call_ai(f"/api/ai/bookings/{args['booking_id']}/invoices"),
    },
    {
        "name": "booking_events",
        "description": (
            "The audit trail for one booking: every recorded change, in order, with what "
            "changed, the old and new values, who did it and when. Use this to answer "
            "'when did this change and who changed it' from the record instead of "
            "inferring it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "The booking's UUID (from `bookings`)."},
                "limit": {
                    "type": "integer",
                    "description": "Most recent N events (default 200, max 1000).",
                },
            },
            "required": ["booking_id"],
        },
        "_call": lambda args: call_ai(
            f"/api/ai/bookings/{args['booking_id']}/events", {"limit": args.get("limit")}
        ),
    },
]

BY_NAME = {tool["name"]: tool for tool in TOOLS}


def public_tools() -> list[dict]:
    """The tool list as sent to the client -- without the internal _call."""
    return [{k: v for k, v in tool.items() if not k.startswith("_")} for tool in TOOLS]


def call_tool(name: str, arguments: dict) -> dict:
    tool = BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    return tool["_call"](arguments or {})
