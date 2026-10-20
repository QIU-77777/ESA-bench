"""Regenerate checked-in schemas from the canonical V2.0 golden source."""
import argparse, hashlib, json, pathlib, re

def generate(source, destination):
    raw = pathlib.Path(source).read_bytes()
    blocks = re.split(r"^## (?=\S+\ntitle:)", raw.decode(), flags=re.M)[1:]
    tools = []
    for block in blocks:
        header, schema = block.split("inputSchema: ", 1)
        tools.append({"name": header.splitlines()[0], "title": re.search(r"^title: (.*)$", header, re.M)[1], "description": header.split("description: ", 1)[1].strip(), "inputSchema": json.loads(schema)})
    tools = [tool for tool in tools if tool['name'] != 'execute_bash']
    assert len(tools) == 61 and len({t['name'] for t in tools}) == 61
    pathlib.Path(destination).write_text(json.dumps({"source": "mcp/internal/tools/testdata/tools_v2.0.golden", "source_sha256": hashlib.sha256(raw).hexdigest(), "tools": tools}, ensure_ascii=False, indent=2) + "\n")

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('source')
    p.add_argument('--output', default=str(pathlib.Path(__file__).with_name('catalog.json')))
    a = p.parse_args()
    generate(a.source, a.output)
