from lisa.diff import MAX_LINE_CHARS, chunk_file, parse_patch, reveal_invisible, should_skip, unified_patch

PATCH = """@@ -10,3 +10,4 @@ def a():
 x = 1
-y = 2
+y = 3
+z = 4
 return x
\\ No newline at end of file
@@ -40,2 +41,2 @@
 keep()
-old()
+fresh()"""


def test_parse_patch_tracks_new_file_line_numbers():
    first, second = parse_patch(PATCH)
    assert [(line.kind, line.number) for line in first] == [(" ", 10), ("-", None), ("+", 11), ("+", 12), (" ", 13)]
    assert [(line.kind, line.number) for line in second] == [(" ", 41), ("-", None), ("+", 42)]


def test_parse_patch_truncates_enormous_lines():
    [[line]] = parse_patch("@@ -0,0 +1 @@\n+" + "x" * 50_000)
    assert len(line.text) == MAX_LINE_CHARS


def test_chunk_file_keeps_small_hunks_together():
    [chunk] = chunk_file("a.py", PATCH)
    assert chunk.start_line == 11
    assert [line.number for line in chunk.added] == [11, 12, 42]
    assert chunk.diff.startswith("  x = 1\n- y = 2\n+ y = 3")
    assert "\n@@\n" in chunk.diff


def test_chunk_file_splits_by_size_without_losing_lines():
    lines = [f"+line {i} {'x' * 80}" for i in range(50)]
    patch = "@@ -0,0 +1,50 @@\n" + "\n".join(lines) + "\n " + "context " * 20
    chunks = chunk_file("big.py", patch, max_chars=1000)
    assert len(chunks) > 1
    assert [line.number for c in chunks for line in c.added] == list(range(1, 51))
    assert all(len(c.diff) <= 1000 for c in chunks)
    assert all(c.added for c in chunks), "context-only chunks are dropped"


def test_chunk_file_caps_added_lines_per_chunk():
    patch = "@@ -0,0 +1,1000 @@\n" + "\n".join(f"+{i}" for i in range(1000))
    chunks = chunk_file("many.py", patch)
    assert len(chunks) == 5
    assert all(len(c.added) == 200 for c in chunks)


def test_unified_patch_rebuilds_a_parseable_patch():
    patch = unified_patch("a\nb\nc\n", "a\nB\nc\nd\n")
    [chunk] = chunk_file("f.py", patch)
    assert [(line.number, line.text) for line in chunk.added] == [(2, "B"), (4, "d")]


def test_should_skip_lockfiles_binaries_minified_and_vendored():
    for name in ["package-lock.json", "web/yarn.lock", "uv.lock", "static/app.min.js", "vendor/x.go", "logo.PNG"]:
        assert should_skip(name), name
    assert not should_skip("src/lock.py")


def test_invisible_characters_are_made_visible():
    hidden = "ok()  # \u200b\u202eevil\U000e0041"
    assert reveal_invisible(hidden) == "ok()  # <U+200B><U+202E>evil<U+E0041>"
    [[line]] = parse_patch("@@ -0,0 +1 @@\n+" + hidden)
    assert line.text == "ok()  # <U+200B><U+202E>evil<U+E0041>"
