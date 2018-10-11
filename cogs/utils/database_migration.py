import datetime
import json
import sqlite3
from operator import itemgetter

import asyncpg
import click

LATEST_VERSION = 1


def _progressbar(*args, **kwargs):
    return click.progressbar(*args, **kwargs, fill_char="█", empty_char=" ", show_pos=True)


async def check_database(pool: asyncpg.pool.Pool):
    async with pool.acquire() as con:
        version = await get_version(con)
        if version <= 0:
            await create_database(con)
            await set_version(con, 1)


async def create_database(con: asyncpg.connection.Connection):
    for create_query in tables:
        await con.execute(create_query)
    for f in functions:
        await con.execute(f)
    for trigger in triggers:
        await con.execute(trigger)
    await set_version(con, LATEST_VERSION)


async def set_version(con: asyncpg.connection.Connection, version):
    await con.execute("""
        INSERT INTO global_property (key, value) VALUES ('db_version',$1)
        ON CONFLICT (key)
        DO
         UPDATE
           SET value = EXCLUDED.value;
    """, version)


async def get_version(con: asyncpg.connection.Connection):
    try:
        return await con.fetchval("SELECT value FROM global_property WHERE key = 'db_version'")
    except asyncpg.UndefinedTableError:
        return 0


tables = [
    """
    CREATE TABLE "character" (
        id serial NOT NULL,
        user_id bigint NOT NULL,
        name text NOT NULL,
        level smallint,
        world text,
        vocation text,
        guild text,
        modified timestamp with time zone DEFAULT now(),
        created timestamp with time zone DEFAULT now(),
        PRIMARY KEY (id),
        UNIQUE(name)
    );
    """,
    """
    CREATE TABLE character_death (
        id serial NOT NULL,
        character_id integer NOT NULL,
        level smallint,
        date timestamp with time zone,
        PRIMARY KEY (id),
        FOREIGN KEY (character_id) REFERENCES "character" (id),
        UNIQUE(character_id, date)
    );
    """,
    """
    CREATE TABLE character_death_killer (
        death_id integer NOT NULL,
        position smallint NOT NULL DEFAULT 0,
        name text NOT NULL,
        player boolean,
        FOREIGN KEY (death_id) REFERENCES character_death (id)
    );
    """,
    """
    CREATE TABLE character_levelup (
        id serial NOT NULL,
        character_id integer NOT NULL,
        level smallint,
        date timestamp with time zone DEFAULT now(),
        PRIMARY KEY (id),
        FOREIGN KEY (character_id) REFERENCES "character" (id)
    );
    """,
    """
    CREATE TABLE event (
        id serial NOT NULL,
        user_id bigint NOT NULL,
        server_id bigint NOT NULL,
        name text NOT NULL,
        description text,
        start timestamp with time zone NOT NULL,
        active boolean NOT NULL DEFAULT true,
        reminder smallint NOT NULL DEFAULT 0,
        joinable boolean NOT NULL DEFAULT true,
        slots smallint NOT NULL DEFAULT 0,
        modified timestamp with time zone NOT NULL DEFAULT now(),
        created timestamp with time zone NOT NULL DEFAULT now(),
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE event_participant (
        event_id integer NOT NULL,
        character_id integer NOT NULL,
        FOREIGN KEY (event_id) REFERENCES event (id),
        FOREIGN KEY (character_id) REFERENCES "character" (id),
        UNIQUE(event_id, character_id)
    );
    """,
    """
    CREATE TABLE event_subscriber (
        event_id integer NOT NULL,
        user_id bigint NOT NULL,
        FOREIGN KEY (event_id) REFERENCES event (id),
        UNIQUE(event_id, user_id)
    );""",
    """
    CREATE TABLE highscores (
        world text NOT NULL,
        category text NOT NULL,
        last_scan timestamp with time zone DEFAULT now(),
        PRIMARY KEY (world, category)
    );""",
    """
    CREATE TABLE highscores_entry (
        rank text,
        category text,
        world text,
        name text,
        vocation text,
        value bigint
    );""",
    """
    CREATE TABLE role_auto (
        server_id bigint NOT NULL,
        role_id bigint NOT NULL,
        rule text NOT NULL,
        PRIMARY KEY (server_id, role_id, rule)
    );
    """,
    """
    CREATE TABLE role_joinable (
        server_id bigint NOT NULL,
        role_id bigint NOT NULL,
        PRIMARY KEY (server_id, role_id)
    );
    """,
    """
    CREATE TABLE server_property (
        server_id bigint NOT NULL,
        key text NOT NULL,
        value jsonb,
        PRIMARY KEY (server_id, key)
    );
    """,
    """
    CREATE TABLE server_prefixes(
        server_id bigint NOT NULL,
        prefixes text[] NOT NULL,
        PRIMARY KEY (server_id)
    );
    """,
    """
    CREATE TABLE global_property (
        key text NOT NULL,
        value jsonb,
        PRIMARY KEY (key)
    );
    """,
    """
    CREATE TABLE watchlist_entry (
        id serial NOT NULL,
        name text NOT NULL,
        server_id bigint NOT NULL,
        is_guild bool DEFAULT FALSE,
        reason text,
        user_id bigint,
        created timestamp with time zone  DEFAULT now(),
        PRIMARY KEY(id),
        UNIQUE(name, server_id, is_guild)
    )
    """,
    """
    CREATE TABLE command (
        server_id bigint,
        channel_id bigint NOT NULL,
        user_id bigint NOT NULL,
        date timestamp with time zone NOT NULL DEFAULT now(),
        prefix text NOT NULL,
        command text NOT NULL
    )
    """
]
functions = [
    """
    CREATE FUNCTION update_modified_column() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
    BEGIN
        NEW.modified = now();
        RETURN NEW;
    END;
    $$;
    """
]
triggers = [
    """
    CREATE TRIGGER update_character_modified
    BEFORE UPDATE ON "character"
    FOR EACH ROW EXECUTE PROCEDURE update_modified_column();
    """,
    """
    CREATE TRIGGER update_event_modified
    BEFORE UPDATE ON event
    FOR EACH ROW EXECUTE PROCEDURE update_modified_column();
    """
]


# Legacy SQlite migration
# This may be removed in later versions or kept separate
async def import_legacy_db(pool: asyncpg.pool.Pool, path):
    legacy_conn = sqlite3.connect(path)
    c = legacy_conn.cursor()
    async with pool.acquire() as conn:
        await import_characters(conn, c)
        await import_server_properties(conn, c)
        await import_roles(conn, c)
        await import_events(conn, c)


async def import_characters(conn: asyncpg.Connection, c: sqlite3.Cursor):
    c.execute("""SELECT id, user_id, name, level, vocation, world, guild FROM chars ORDER By id ASC""")
    rows = c.fetchall()
    levelups = []
    deaths = []
    with _progressbar(rows, label="Migrating characters") as bar:
        for row in bar:
            old_id, *char = row
            # Try to insert character, if it exist return existing character's ID
            char_id = await conn.fetchval("""
                    INSERT INTO "character" (user_id, name, level, vocation, world, guild)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT(name) DO UPDATE SET name=EXCLUDED.name RETURNING id""", *char)
            c.execute("SELECT ?, level, date FROM char_levelups WHERE char_id = ? ORDER BY date ASC",
                      (char_id, old_id))
            levelups.extend(c.fetchall())
            c.execute("SELECT ?, level, date, killer, byplayer FROM char_deaths WHERE char_id = ?",
                      (char_id, old_id))
            deaths.extend(c.fetchall())
    deaths = sorted(deaths, key=itemgetter(2))
    skipped_deaths = 0
    with _progressbar(deaths, label="Migrating deaths") as bar:
        for death in bar:
            char_id, level, date, killer, byplayer = death
            byplayer = byplayer == 1
            date = datetime.datetime.utcfromtimestamp(date)
            # If there's another death at the exact same timestamp by the same character, we ignore it
            exists = await conn.fetchrow("""SELECT id FROM character_death
                                                WHERE date = $1 AND character_id = $2""", date, char_id)
            if exists:
                skipped_deaths += 1
                continue
            death_id = await conn.fetchval("""INSERT INTO character_death(character_id, level, date)
                                              VALUES ($1, $2, $3) RETURNING id""", char_id, level, date)
            await conn.execute("""INSERT INTO character_death_killer(death_id, name, player)
                                  VALUES ($1, $2, $3)""", death_id, killer, byplayer)
    if skipped_deaths:
        print(f"Skipped {skipped_deaths:,} duplicate deaths.")
    levelups = sorted(levelups, key=itemgetter(2))
    skipped_levelups = 0
    with _progressbar(levelups, label="Migrating level ups") as bar:
        for levelup in bar:
            char_id, level, date = levelup
            date = datetime.datetime.utcfromtimestamp(date)
            # If there's another levelup within a 15 seconds margin, we ignore it
            exists = await conn.fetchrow("""SELECT id FROM character_levelup
                                                WHERE character_id = $1 AND
                                                GREATEST($2-date,date-$2) <= interval '15' second""", char_id, date)
            if exists:
                skipped_levelups += 1
                continue
            await conn.execute("""INSERT INTO character_levelup(character_id, level, date)
                                      VALUES ($1, $2, $3)""", char_id, level, date)
    if skipped_levelups:
        print(f"Skipped {skipped_levelups:,} duplicate level ups.")


async def import_server_properties(conn: asyncpg.Connection, c: sqlite3.Cursor):
    c.execute("SELECT server_id, name, value FROM server_properties")
    rows = c.fetchall()
    with _progressbar(rows, label="Migrating server properties") as bar:
        for row in bar:
            server, key, value = row
            server = int(server)
            if key == "prefixes":
                await conn.execute("""INSERT INTO server_prefixes(server_id, prefixes) VALUES($1, $2)
                                          ON CONFLICT DO NOTHING""", server, json.loads(value))
                continue

            if key in ["times"]:
                value = json.dumps(json.loads(value))
            elif key == "commandsonly":
                value = json.dumps(bool(value))
            else:
                value = json.dumps(value)
            await conn.execute("""INSERT INTO server_property(server_id, key, value) VALUES($1, $2, $3)
                                      ON CONFLICT(server_id, key) DO NOTHING""", server, key, value)


async def import_events(conn: asyncpg.Connection, c: sqlite3.Cursor):
    c.execute("SELECT id, creator, name, start, active, status, description, server, joinable, slots FROM events")
    rows = c.fetchall()
    event_subscribers = []
    event_participants = []
    with _progressbar(rows, label="Migrating events") as bar:
        for row in bar:
            old_id, creator, name, start, active, status, description, server, joinable, slots = row
            start = datetime.datetime.utcfromtimestamp(start)
            active = bool(active)
            joinable = bool(joinable)
            status = 4 - status
            event_id = await conn.fetchval("""INSERT INTO event(user_id, name, start, active, description, server_id,
                                              joinable, slots, reminder)
                                              VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id""",
                                           creator, name, start, active, description, server, joinable, slots, status)
            c.execute("SELECT ?, user_id FROM event_subscribers WHERE event_id = ?", (event_id, old_id))
            event_subscribers.extend(c.fetchall())
            c.execute("SELECT ?, name FROM event_participants LEFT JOIN chars ON id = char_id WHERE event_id = ?",
                      (event_id, old_id))
            event_participants.extend(c.fetchall())
    with _progressbar(event_subscribers, label="Migrating event subscribers") as bar:
        for row in bar:
            await conn.execute("""INSERT INTO event_subscriber(event_id, user_id) VALUES($1, $2)
                                  ON CONFLICT(event_id, user_id) DO NOTHING""", *row)
    with _progressbar(event_participants, label="Migrating event participants") as bar:
        for row in bar:
            event_id, name = row
            char_id = await conn.fetchval('SELECT id FROM "character" WHERE name = $1', name)
            if char_id is None:
                continue
            await conn.execute("""INSERT INTO event_participant(event_id, character_id) VALUES($1, $2)
                                  ON CONFLICT(event_id, character_id) DO NOTHING""", event_id, char_id)


async def import_roles(conn: asyncpg.Connection, c: sqlite3.Cursor):
    c.execute("SELECT server_id, role_id, guild FROM auto_roles")
    rows = c.fetchall()
    with _progressbar(rows, label="Migrating auto roles") as bar:
        for row in bar:
            await conn.execute("""INSERT INTO role_auto(server_id, role_id, rule) VALUES($1, $2, $3)
                                      ON CONFLICT(server_id, role_id, rule) DO NOTHING""", *row)
    c.execute("SELECT server_id, role_id FROM joinable_roles")
    rows = c.fetchall()
    with _progressbar(rows, label="Migrating joinable roles") as bar:
        for row in bar:
            await conn.execute("""INSERT INTO role_joinable(server_id, role_id) VALUES($1, $2)
                                      ON CONFLICT(server_id, role_id) DO NOTHING""", *row)
