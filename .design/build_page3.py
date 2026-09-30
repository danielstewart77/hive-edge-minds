import json, subprocess, sys

NEW_CHAIN = '''def _rotation_chain_targets(sessions: list[dict]) -> dict[str, str]:
    """For each retired ancestor, the conversation at the end of its chain.

    A label is keyed by session id, and a chat-surface rotation retires the
    row it was keyed to in favour of a successor. Walked to the end rather
    than one hop, because a conversation can rotate several times between
    two reads of this table — the browser's migrateLabel only ever sees the
    hop it happened to be watching.

    Pure, and cycle-safe: a rotated_from loop answers nothing rather than
    spinning. The gateway should never emit one, but this runs on every
    labels read, and a hang here is a grid with no names on it at all.
    """
    successor: dict[str, str] = {}
    for session in sessions:
        ancestor = str((session or {}).get("rotated_from") or "")
        current = str((session or {}).get("id") or "")
        if ancestor and current:
            successor[ancestor] = current

    heirs: dict[str, str] = {}
    for ancestor in successor:
        seen = {ancestor}
        node = successor[ancestor]
        while node in successor:
            node = successor[node]
            if node in seen:
                node = ""
                break
            seen.add(node)
        if node:
            heirs[ancestor] = node
    return heirs
'''

NEW_INHERIT = '''def _inherit_rotated_labels(conn: sqlite3.Connection, sessions: list[dict]) -> int:
    """Move each stranded label onto the conversation that replaced it.

    Moved, not copied. The ancestor is retired and can never be opened
    again, and the end route drops a label by the id being ended — which is
    the heir\\'s. A copy left on the ancestor is a name nobody can ever
    delete.

    An heir already carrying a name of its own is left alone: Daniel typed
    that one after the rotation, and a name from before it does not get to
    win. The UPDATE re-keys the row rather than rewriting it, so the colour
    and the original updated_at travel with the name.

    Returns how many rows moved, so a caller can tell "nothing was owed"
    from "nothing happened".
    """
    heirs = _rotation_chain_targets(sessions)
    if not heirs:
        return 0
    held = {
        row["session_id"]
        for row in conn.execute("SELECT session_id FROM terminal_labels")
    }
    moved = 0
    for ancestor, heir in heirs.items():
        if ancestor not in held or heir in held:
            continue
        conn.execute(
            "UPDATE terminal_labels SET session_id = ? WHERE session_id = ?",
            (heir, ancestor),
        )
        moved += 1
    if moved:
        conn.commit()
    return moved
'''

LABELS_DIFF = ''' @app.get("/api/terminal/labels")
 async def api_terminal_labels(user: dict = Depends(require_auth_or_label_writer)):
     """Every label, for the tab that asked. Explicitly uncacheable.
 
     Without a directive a browser may cache a 200 GET heuristically, and a
     stale read here is indistinguishable from a current one — it reads as
     the server having lost labels it still holds. The client asks with
     ``no-store`` as well; both ends, because either alone is one deployment
     away from being the only one.
+
+    A rotation retires the session id a label was keyed to, so this is also
+    where a stranded name is re-keyed onto the conversation that replaced
+    it. On the read, because nothing writes at rotation time: the browser
+    migrates a label only when a tile happened to be attached, and a
+    conversation rotated from Telegram with no tab open loses its name by
+    construction.
     """
     del user
     conn = _labels_db()
     try:
+        # A gateway that cannot be reached costs the inheriting and nothing
+        # else. Every name already held still comes back — a grid of
+        # conversations all reading "skippy" is worse than a stale one.
+        try:
+            sessions = await _gateway_json("/sessions")
+        except HTTPException:
+            sessions = []
+        if isinstance(sessions, list):
+            _inherit_rotated_labels(conn, sessions)
         rows = conn.execute("SELECT session_id, name, color FROM terminal_labels").fetchall()
     finally:
         conn.close()
     return JSONResponse(
         {r["session_id"]: {"name": r["name"], "color": r["color"]} for r in rows},
         headers={"Cache-Control": "no-store"},
     )
'''

def whole(fn):
    out = subprocess.run(
        ["awk", "-v", "fn=" + fn,
         '$0 ~ "^(async )?def "fn"\\\\(" {p=1} p {print} p && /^    return |^    raise / {tail=1} ',
         "/home/daniel/Storage/Dev/terminal/src/main.py"],
        capture_output=True, text=True).stdout
    return out

items = {
 "f1": {"order":1, "name":"api_terminal_labels", "changed":True, "role":"changed",
   "file":"src/main.py", "calls":["_labels_db","_gateway_json","_inherit_rotated_labels"],
   "called_by":[],
   "symbol":{"name":"api_terminal_labels","kind":"existing","resolved":False,
             "path":"src/main.py (proposed edit, shown as a diff)",
             "source":LABELS_DIFF}},
 "f2": {"order":2, "name":"_inherit_rotated_labels", "changed":True, "role":"changed",
   "file":"src/main.py", "calls":["_rotation_chain_targets"],
   "called_by":["api_terminal_labels"],
   "symbol":{"name":"_inherit_rotated_labels","kind":"planned","resolved":False,
             "path":"src/main.py (new)","source":NEW_INHERIT}},
 "f3": {"order":3, "name":"_rotation_chain_targets", "changed":True, "role":"changed",
   "file":"src/main.py", "calls":[], "called_by":["_inherit_rotated_labels"],
   "symbol":{"name":"_rotation_chain_targets","kind":"planned","resolved":False,
             "path":"src/main.py (new)","source":NEW_CHAIN}},
}
print(json.dumps(items))
