import ast, json, sys
src = open("/home/daniel/Storage/Dev/terminal/src/main.py", encoding="utf-8").read()
lines = src.splitlines()
tree = ast.parse(src)
want = ["_labels_db", "_gateway_json", "api_terminal_label_put", "api_terminal_session_end"]
out = {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in want:
        start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
        out[node.name] = "\n".join(lines[start:node.end_lineno]) + "\n"
missing = [w for w in want if w not in out]
if missing:
    sys.exit("not found: " + ", ".join(missing))
json.dump(out, open(".design/context_src.json", "w"))
print({k: len(v.splitlines()) for k, v in out.items()})
