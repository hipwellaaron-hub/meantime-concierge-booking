from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from app.config import settings
from app.database import Base
from app import models  # noqa: F401  (registers all model classes with Base.metadata)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Schema objects that exist in the database ON PURPOSE and are deliberately
# NOT declared on the models. autogenerate compares the database against the
# model metadata and proposes dropping anything the metadata does not
# mention, so without this list the next `alembic revision --autogenerate`
# for some unrelated change emits, among the lines you actually wanted:
#
#     op.drop_constraint('fk_bookings_space_venue', 'bookings', ...)
#     op.drop_constraint('uq_spaces_id_venue', 'spaces', ...)
#
# which reads like tidy-up and deletes the constraint that makes "a booking's
# venue cannot disagree with its space's venue" a property of the table.
#
# WHY THEY ARE NOT ON THE MODELS. Declaring the composite FK in Booking's
# __table_args__ gives bookings->spaces a SECOND foreign key relationship,
# and every implicit join between those two tables then raises
# AmbiguousForeignKeysError -- it took the whole suite down when tried
# (2026-09-12). The constraint is for Postgres to enforce, not for the ORM
# to navigate, so it lives in the migration and is protected here instead.
DB_ONLY_OBJECTS = {
    # (type, name)
    ("foreign_key_constraint", "fk_bookings_space_venue"),
    ("unique_constraint", "uq_spaces_id_venue"),
    ("index", "ix_bookings_venue_id"),
}


def include_object(object_, name, type_, reflected, compare_to):
    """Keep autogenerate's hands off the constraints listed above.

    Only shields objects that are REFLECTED (i.e. found in the database with
    no counterpart in the metadata) -- an object the models do declare is
    compared normally.
    """
    if reflected and (type_, name) in DB_ONLY_OBJECTS:
        return False
    return True


# OUTSTANDING, NOT YET DONE -- FOUR more objects have the same hazard and are
# NOT protected here, because they are pre-existing (introduced by ed35a74 and
# the Event Order proposals work) rather than part of the venue change, and
# each needs its migration read to declare it correctly:
#
# Read out of both the migration and the live catalogue on 2026-09-12; the two
# agree, so these are the exact definitions to declare, not a starting point:
#
#   uq_beo_proposal_one_pending_per_booking  (a3f6e1c7d094)
#       Index("uq_beo_proposal_one_pending_per_booking", "booking_id",
#             unique=True, postgresql_where=text("status = 'pending'"))
#       PARTIAL -- confirmed, not assumed. THIS IS THE DANGEROUS ONE:
#       autogenerate proposes dropping it, and accepting that proposal would
#       silently retire "one pending Event Order proposal per booking". The
#       database would begin accepting two, on a path used on live bookings,
#       with nothing raised anywhere.
#
#   ck_beo_proposal_status  (a3f6e1c7d094) on beo_proposals
#       status IN ('pending', 'rules_blocked', 'resolved', 'superseded')
#
#   ck_beo_proposal_field_state  (a3f6e1c7d094) on beo_proposal_fields
#       state IN ('pending', 'approved', 'rejected', 'superseded', 'blocked')
#
#   ix_bookings_parent_booking_id  (c1a8f3b02e77) on bookings
#       Index("ix_bookings_parent_booking_id", "parent_booking_id")  -- plain
#
# The right fix for these is to DECLARE them on their models (unlike the venue
# constraints above, none of them creates a second FK path, so none has the
# AmbiguousForeignKeysError problem that forced the filter). Until that is
# done, autogenerate proposes dropping all four. Check with:
#
#     .venv/Scripts/python.exe -c "
#     import pathlib
#     from sqlalchemy import create_engine
#     from alembic.migration import MigrationContext
#     from alembic.autogenerate import compare_metadata
#     from app.config import settings
#     from app.database import Base
#     import app.models
#     src = pathlib.Path('alembic/env.py').read_text(encoding='utf-8')
#     ns = {}
#     exec(src[src.index('DB_ONLY_OBJECTS'):src.index('# OUTSTANDING')], ns)
#     engine = create_engine(settings.test_database_url)
#     with engine.connect() as conn:
#         ctx = MigrationContext.configure(conn, opts={'include_object': ns['include_object']})
#         for d in compare_metadata(ctx, Base.metadata):
#             print(repr(d)[:200])
#     "
#
# THE include_object IS THE POINT. An earlier version of this snippet built
# the context without it and so reported the three shielded objects as
# removals as well -- 8 differences that could never go down, no matter what
# was fixed. Today it prints 5: the four above, plus the add_fk below.
#
# "Done" is when only the add_fk remains.

# KNOWN, AND DELIBERATELY NOT FILTERED: autogenerate also proposes ADDING an
# FK bookings.venue_id -> venues.id, because Booking declares one on the
# column and d8c3f1a7e920 never built it. That direction is safe -- it adds
# integrity rather than removing it -- and it is redundant anyway: venue_id
# already has to match a real space's venue_id through the composite FK, and
# spaces.venue_id references venues. Left visible rather than filtered, so
# whoever eventually wants it can just take the line.

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
