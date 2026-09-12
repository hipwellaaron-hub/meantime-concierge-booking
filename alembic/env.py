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
