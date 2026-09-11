"""Tool definitions: one per Concierge /api/ai/* endpoint.

The descriptions here are not documentation, they are the instructions the
model reads when deciding whether to call something. So each says what it
returns, what it is for, and -- where it matters most -- what it does NOT
tell you. The availability description in particular spells out that a
slot with nothing confirmed is not necessarily a free slot, because
"available" and "nobody else is asking" are different facts and confusing
them is how a date gets offered to two parties.

Every tool but one is read-only. The exception is
propose_event_order_values, which writes a PROPOSAL and applies nothing:
each field waits for a staff approval on the Event Order form. It goes
through post_ai, which has its own allowlist, so a read tool cannot be
talked into a write.
"""

from mcp_server.concierge import call_ai, path_segment, post_ai

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
        "_call": lambda args: call_ai(f"/api/ai/bookings/{path_segment(args['booking_id'], name='booking_id')}/documents"),
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
        "_call": lambda args: call_ai(f"/api/ai/bookings/{path_segment(args['booking_id'], name='booking_id')}/invoices"),
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
            f"/api/ai/bookings/{path_segment(args['booking_id'], name='booking_id')}/events",
            {"limit": args.get("limit")},
        ),
    },
    {
        "name": "event_order_proposal",
        "description": (
            "What is awaiting approval on a booking's Event Order, and what happened to the "
            "last thing proposed. Read this BEFORE proposing: it shows whether an earlier "
            "proposal is still pending (propose again and the pending fields are superseded), "
            "and for anything already decided it shows `applied_value` next to "
            "`proposed_value`, so you can see where a human rewrote a transcription before "
            "approving it. That difference is the calibration signal -- read it and write "
            "closer to what they actually wanted.\n\n"
            "Field states: pending (awaiting a human), approved (written to the Event Order), "
            "rejected, superseded (a newer proposal replaced it before anyone looked), "
            "blocked (the house rules refused the proposal it belonged to).\n\n"
            "This is the MOST RECENT proposal, decided or not -- so a resolved one still "
            "shows what a human did with it. Read `status` to tell them apart: pending "
            "means somebody still has to look, resolved/superseded/rules_blocked mean the "
            "ask is over. `proposal: null` means nothing has ever been proposed on this "
            "booking. Only the latest is returned, so a newer ask hides an older decided "
            "one -- read this before proposing, not as a history of every correction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "The booking reference, e.g. HAM-20271114-AB12C (from `bookings`).",
                },
            },
            "required": ["reference"],
        },
        "_call": lambda args: call_ai(
            f"/api/ai/bookings/{path_segment(args['reference'], name='reference')}/event-order-proposal"
        ),
    },
    {
        "name": "propose_event_order_values",
        "description": (
            "Propose values for the free-text Event Order fields on one booking. This is the "
            "ONLY write available, and it writes a proposal: nothing reaches the Event Order "
            "until a staff member approves it on the Event Order form -- all at once, or field by "
            "field -- where "
            "your text is shown against the value it would replace. There is no tool that "
            "approves, and asking for one is not a gap to work around.\n\n"
            "Transcribe, do not compose. Take the client's own final details and put each "
            "fact in the right field, in run-sheet voice -- 'Cake: client supplying', not "
            "'We have organised a cake'. Never move a fact between fields to make one read "
            "better: a decoration note in Dietaries is the exact error this exists to stop, "
            "and it once took a declared nut allergy down with it.\n\n"
            "Never drop a dietary or allergy detail that is already on the Event Order. If "
            "you are rewriting Dietaries, carry every existing declaration through -- read "
            "`event_order_proposal` and the Event Order first so you know what is there.\n\n"
            "House rules run on what you send and refuse the proposal outright (HTTP 422 with "
            "`rule_codes`) if it puts decoration or supplier language in Dietaries, writes in "
            "the client's voice (first-person pronouns -- I, we, my, our -- OR request phrasing "
            "such as 'would like', 'hoping', 'could you'; state the fact, not the sentence), "
            "empties a field that had a value, or drops a declared dietary. On an 18th, or "
            "any booking with under-18s, a proposed special_notes must carry the RSA line "
            "(say 'RSA' or 'responsible service of alcohol') or it is refused with "
            "rsa_missing; not touching special_notes on such a booking earns a warning "
            "(rsa_absent_on_document), not a refusal. An older Event Order may hold one "
            "merged music/entertainment value: on those, propose music and entertainment "
            "together or the proposal is refused with legacy_music_split -- "
            "`event_order_proposal` shows you which case you are in. A refused proposal is "
            "recorded but never shown to staff, so it helps nobody: read the codes and fix "
            "it rather than re-sending.\n\n"
            "If a tentative or confirmed booking has no Event Order yet, a proposal that passes "
            "the rules CREATES the first draft (from the wizard's answers when the client "
            "submitted one, else from the booking's facts) so there is something to review "
            "against -- nothing is sent; the response says `event_order.created`. On an enquiry "
            "the proposal is stored and waits for a staff member to generate the Event Order. An "
            "Event Order that has already gone out is never touched; revising it is a staff "
            "decision, and the response says the proposal is waiting for that.\n\n"
            "THE FOOD ORDER is proposed as `food_order`: a list of catalogue items and "
            "quantities -- [{menu_item_id, quantity}] (ids from the `catalogue` tool) or "
            "[{name, quantity}] by exact catalogue name. NEVER a price and never a custom "
            "line: a line carrying any price key is refused (food_price_sent); an unknown or "
            "retired item is refused by name (food_unknown_item) with the active items listed; "
            "an item this booking has no price on record for is refused (food_price_unavailable). "
            "The price comes from the catalogue for this booking exactly as the client wizard's "
            "does; staff see the lines and the computed total, approve or change quantities, "
            "and the Event Order's line items AND the draft final invoice are built from the "
            "catalogue (an invoice that has already gone out is left alone). You cannot touch "
            "status or any figure -- those are computed from the catalogue, the wizard and the "
            "booking, and a proposed line with a wrong price is precisely the class of error "
            "this boundary exists to prevent.\n\n"
            "THE TEN FIELDS, by exact name: catering_order_and_service_style, bar_structure, "
            "room_layout_notes, music, entertainment, dietaries, accessibility, decorations, "
            "special_notes, onsite_contact. Any other name (catering_notes, "
            "music_entertainment, ...) is refused before anything is sent, and the refusal "
            "names the valid ones. Each value is at most 2000 characters (Concierge counts "
            "after folding newlines and trimming). `trigger` is at most 30 characters -- a "
            "label like 'client final details', not a sentence. Optional arguments may be "
            "omitted or sent as null."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "The booking reference, e.g. HAM-20271114-AB12C (from `bookings`).",
                },
                "source": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 500,
                    "description": (
                        "Where these values came from, specifically enough that a human can go "
                        "and read it -- 'client email 6 Sep, final details'. Required: an "
                        "untraceable proposal is not reviewable, and this is shown to the "
                        "person approving it. 3 to 500 characters."
                    ),
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "The fields to propose, as name -> value. Send only the fields you are "
                        "actually changing; an omitted field is left exactly as it is."
                    ),
                    "properties": {
                        "catering_order_and_service_style": {"type": "string", "maxLength": 2000},
                        "bar_structure": {"type": "string", "maxLength": 2000},
                        "room_layout_notes": {"type": "string", "maxLength": 2000},
                        "music": {"type": "string", "maxLength": 2000},
                        "entertainment": {"type": "string", "maxLength": 2000},
                        "dietaries": {"type": "string", "maxLength": 2000},
                        "accessibility": {"type": "string", "maxLength": 2000},
                        "decorations": {"type": "string", "maxLength": 2000},
                        "special_notes": {"type": "string", "maxLength": 2000},
                        "onsite_contact": {"type": "string", "maxLength": 2000},
                    },
                    "additionalProperties": False,
                },
                "food_order": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "description": (
                        "Catalogue items and quantities, never a price. Each line: menu_item_id "
                        "(from `catalogue`) or name (exact catalogue name), and quantity (whole "
                        "number, 1-500). One line per item."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "menu_item_id": {"type": "string", "maxLength": 64},
                            "name": {"type": "string", "maxLength": 255},
                            "quantity": {"type": "integer", "minimum": 1, "maximum": 500},
                        },
                        "required": ["quantity"],
                        "additionalProperties": False,
                    },
                },
                "trigger": {
                    "type": "string",
                    "maxLength": 30,
                    "description": (
                        "Short label for why you acted now, e.g. 'client final details'. "
                        "AT MOST 30 CHARACTERS -- longer is refused; put the detail in `source`."
                    ),
                },
                "model": {
                    "type": "string",
                    "maxLength": 80,
                    "description": (
                        "Optional: which model produced these values, e.g. claude-fable-5-1. Shown "
                        "to the approver on the Event Order form beside `source`."
                    ),
                },
            },
            "required": ["reference", "source"],
        },
        "_call": lambda args: post_ai(
            f"/api/ai/bookings/{path_segment(args['reference'], name='reference')}/event-order-proposal",
            {
                "source": args["source"],
                "fields": args.get("fields") or {},
                "food_order": args.get("food_order"),
                "trigger": args.get("trigger"),
                "model": args.get("model"),
            },
        ),
    },
]

for _tool in TOOLS:
    # What is published is what is enforced: an argument the schema does
    # not name is refused by name, and the schema says so.
    _tool["inputSchema"].setdefault("additionalProperties", False)

BY_NAME = {tool["name"]: tool for tool in TOOLS}


def public_tools() -> list[dict]:
    """The tool list as sent to the client -- without the internal _call."""
    return [{k: v for k, v in tool.items() if not k.startswith("_")} for tool in TOOLS]


class ToolArgumentError(ValueError):
    """The arguments do not fit the tool's inputSchema. Says which
    argument and why, so the model fixes its call rather than going
    looking for a deployment problem (2026-09-10: a missing top-level key
    raised KeyError inside the tool and was reported as "Unknown tool")."""


def _check(schema: dict, value: object, *, where: str, problems: list[str]) -> None:
    """The subset of JSON Schema these tools use, checked honestly: type
    (object, string, integer, array), enum, required, unexpected keys,
    minProperties, minLength/maxLength, minimum/maximum, minItems/maxItems
    and items. An explicit null on an OPTIONAL key means
    "absent" -- Concierge accepts None for every optional and every _call
    reads them with .get(), so a client that sends null for what it has
    nothing to say about is not refused. Anything the schema cannot say
    (a booking that does not exist, a house rule) is Concierge's own
    validation, reported inside the tool result."""
    kind = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where} must be one of: " + ", ".join(str(v) for v in schema["enum"]))
        return
    if kind == "object":
        if not isinstance(value, dict):
            problems.append(f"{where} must be an object")
            return
        props = schema.get("properties") or {}
        required = schema.get("required") or ()
        for key in required:
            if key not in value:
                label = key if where == "arguments" else f"{where}.{key}"
                problems.append(f"missing required argument '{label}'")
        if schema.get("additionalProperties") is False:
            unexpected = sorted(k for k in value if k not in props)
            if unexpected:
                what = "field" if where != "arguments" else "argument"
                problems.append(
                    f"unexpected {what}{'s' if len(unexpected) > 1 else ''} "
                    + ", ".join(repr(k) for k in unexpected)
                    + f" in {where}; valid {what}s are: " + ", ".join(props)
                )
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            problems.append(f"{where} must have at least {schema['minProperties']} entry")
        for key, sub in props.items():
            if key not in value:
                continue
            if value[key] is None and key not in required:
                continue  # null on an optional key is the same as leaving it out
            _check(sub, value[key], where=key if where == "arguments" else f"{where}.{key}", problems=problems)
    elif kind == "string":
        if not isinstance(value, str):
            problems.append(f"{where} must be a string")
            return
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{where} must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{where} must be at most {schema['maxLength']} characters (got {len(value)})")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{where} must be an integer")
            return
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{where} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{where} must be at most {schema['maximum']}")
    elif kind == "array":
        if not isinstance(value, list):
            problems.append(f"{where} must be a list")
            return
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{where} must have at least {schema['minItems']} entry")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{where} must have at most {schema['maxItems']} entries")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _check(item_schema, item, where=f"{where}[{index}]", problems=problems)


def validate_arguments(tool: dict, arguments: object) -> None:
    problems: list[str] = []
    _check(tool["inputSchema"], arguments, where="arguments", problems=problems)
    if problems:
        raise ToolArgumentError("; ".join(problems))


def call_tool(name: str, arguments: object) -> dict:
    """Unknown name -> KeyError. Arguments that do not fit the schema ->
    ToolArgumentError, BEFORE anything is sent to Concierge, naming the
    argument. The validator names every missing or unexpected key, so no
    KeyError can come out of a tool for an argument any more."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    if arguments is None:
        arguments = {}
    validate_arguments(tool, arguments)
    return tool["_call"](arguments)


