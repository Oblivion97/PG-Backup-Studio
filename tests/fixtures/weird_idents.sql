-- QA fixture: hostile identifiers, literals and comments (newlines, ;, $, quotes, comment markers).
SET client_min_messages = warning;
CREATE SCHEMA "we;ird$schema";
CREATE SCHEMA "new
line";
CREATE SCHEMA "$$";
CREATE SCHEMA "a--b /* c */";

CREATE TYPE "we;ird$schema"."mood;$$" AS ENUM ('a;b', E'new\nline', '$$', 'it''s', '--', '/* x */', E'back\\slash', E'\\.');
CREATE DOMAIN "new
line"."dom;ain" AS text CHECK (VALUE <> ';' AND VALUE <> E'a\nb');

CREATE TABLE "we;ird$schema"."tab
le;1" (
  "id;" serial PRIMARY KEY,
  "a
b" text DEFAULT E'x\ny;z',
  "$1" integer,
  "$$" text DEFAULT '$$;',
  "x""y" "we;ird$schema"."mood;$$" DEFAULT '$$',
  "--c" text,
  "/*c*/" text,
  "E'" text,
  "\" text,
  d "new
line"."dom;ain",
  CONSTRAINT "ck;1" CHECK ("$$" <> ';' AND "--c" IS DISTINCT FROM E'\n--'),
  CONSTRAINT "uq
2" UNIQUE ("a
b", "$1")
);
CREATE INDEX "ix;$$" ON "we;ird$schema"."tab
le;1" (lower("--c")) WHERE "$1" <> 0;
COMMENT ON TABLE "we;ird$schema"."tab
le;1" IS E'line1;\nline2 -- not a comment\n/* nor this */\n\\.\nCOPY x FROM stdin;\n$$ $f$ ''';
COMMENT ON COLUMN "we;ird$schema"."tab
le;1"."$$" IS $q$tricky; $$ ' -- /* $q$;
COMMENT ON SCHEMA "new
line" IS E'multi\nline';

INSERT INTO "we;ird$schema"."tab
le;1" ("a
b", "$1", "$$", "x""y", "--c", "/*c*/", "E'", "\", d)
VALUES (E'tab\there\nnl\r\ncrlf', 1, E'\\.', 'a;b', E'\\N', '\N', 'NULL', E'back\\', 'ok'),
       (NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
       ('', 2, E'\\.\n\\.', E'new\nline', E'\u00fc\u65e5\U0001F600', E'\x01\x1f', ' ', '\', 'x');

CREATE FUNCTION "we;ird$schema"."f;n$$"(a text) RETURNS text LANGUAGE plpgsql AS $body$
BEGIN
  -- comment with ; and $$
  /* block ; */
  RETURN a || ';' || E'\n' || '$$' || $x$ inner ; $x$;
END
$body$;
COMMENT ON FUNCTION "we;ird$schema"."f;n$$"(text) IS 'f; "n"';

CREATE FUNCTION "a--b /* c */"."sqlf"(x int) RETURNS int LANGUAGE sql
BEGIN ATOMIC
  SELECT CASE WHEN x > 0 THEN x ELSE -x END;
END;

CREATE VIEW "$$"."v;iew
x" AS SELECT "id;" AS "i;d", 'semi;colon -- not a comment' AS s, E'a\nb' AS nl FROM "we;ird$schema"."tab
le;1";

CREATE FUNCTION "$$".trg() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
CREATE TRIGGER "trg;
1" BEFORE UPDATE ON "we;ird$schema"."tab
le;1" FOR EACH ROW EXECUTE FUNCTION "$$".trg();
ALTER TABLE "we;ird$schema"."tab
le;1" DISABLE TRIGGER "trg;
1";

CREATE SEQUENCE "new
line"."seq;$";
SELECT setval('"new
line"."seq;$"', 41);

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgbs_test_qa_weird') THEN
    CREATE ROLE pgbs_test_qa_weird NOLOGIN;
  END IF;
END $$;
GRANT SELECT ("a
b") ON "we;ird$schema"."tab
le;1" TO pgbs_test_qa_weird;
GRANT USAGE ON SCHEMA "new
line" TO pgbs_test_qa_weird;
CREATE POLICY "pol;
1" ON "we;ird$schema"."tab
le;1" FOR SELECT TO pgbs_test_qa_weird USING ("$1" > 0);
ALTER TABLE "we;ird$schema"."tab
le;1" ENABLE ROW LEVEL SECURITY;
